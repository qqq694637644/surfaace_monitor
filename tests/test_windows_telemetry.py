from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from surfaace_monitor.report import session_summary, write_report
from surfaace_monitor.storage import Storage
from surfaace_monitor import windows_telemetry as telemetry


def _packet(hardware_type: str, sensor_type: str, unit: str, name: str, value: float):
    return {
        "kind": "sample",
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "sequence": 1,
        "sensors": [{
            "id": f"/{hardware_type}/{sensor_type}".lower(),
            "hardware_id": f"/{hardware_type}".lower(),
            "hardware_name": f"Fake {hardware_type}",
            "hardware_type": hardware_type,
            "name": name,
            "sensor_type": sensor_type,
            "unit": unit,
            "value": value
        }]
    }


def test_optional_fallback_not_misidentified_as_cpu_package(tmp_path: Path):
    with Storage(tmp_path / "test.db") as store:
        sid = store.start_session(
            label="baseline", process_name="Hollow Knight.exe", interval_ms=1000,
            bridge="fake", presentmon=None
        )
        store.add_sample(
            sid, _packet("WindowsPerfCounter", "Performance", "%",
                         "Processor Performance vs Nominal (NOT MHz)", 215.5), None
        )
        store.add_sample(
            sid, _packet("AcpiThermalZone", "Temperature", "C",
                         "ACPI\\ThermalZone\\TZ01_0", 29.5), None
        )
        summary = session_summary(store, sid)
        warnings = summary["warnings"]
        assert any("No Cpu Temperature" in warning for warning in warnings)
        assert any("No Cpu Clock" in warning for warning in warnings)
        assert any("NOT measured CPU MHz" in warning for warning in warnings)
        assert any("NOT CPU package" in warning for warning in warnings)
        files = write_report(store, sid, tmp_path / "reports")
    assert tmp_path / "reports" / "cpu_performance_percent.png" in files
    assert tmp_path / "reports" / "acpi_thermal_zones.png" in files
    assert not (tmp_path / "reports" / "cpu_temperature.png").exists()


def test_acpi_collector_is_bounded_and_stops(monkeypatch):
    monkeypatch.setattr(telemetry.sys, "platform", "win32")
    monkeypatch.setattr(
        telemetry, "read_acpi_zones",
        lambda: [{"id": "/acpi/test", "hardware_type": "AcpiThermalZone",
                  "sensor_type": "Temperature", "name": "test", "unit": "C", "value": 32.0}]
    )
    collector = telemetry.AcpiThermalCollector(interval_seconds=0.03)
    collector.start()
    try:
        time.sleep(0.12)
        packet = collector.next_packet()
        assert packet is not None
        assert packet["sensors"][0]["value"] == 32.0
        assert collector._frames.qsize() <= 2
    finally:
        collector.close()


def test_pawnio_nonwindows_has_no_claim(monkeypatch):
    monkeypatch.setattr(telemetry.sys, "platform", "linux")
    assert telemetry.pawnio_installed() is None
    assert telemetry.read_acpi_zones() == []
    pdh = telemetry.WindowsProcessorPerformance()
    assert pdh.open() is False
    assert pdh.read_sensors() == []
    pdh.close()
