from __future__ import annotations

import csv
from pathlib import Path

import pytest

from surfaace_monitor.report import session_summary, write_report
from surfaace_monitor.storage import Storage


def sample(sequence: int = 1):
    return {
        "kind": "sample",
        "timestamp_utc": "2026-10-22T02:25:00.000Z",
        "sequence": sequence,
        "sensors": [
            {
                "id": "/intelcpu/0/temperature/0",
                "hardware_id": "/intelcpu/0",
                "hardware_name": "Intel i5",
                "hardware_type": "Cpu",
                "name": "CPU Package",
                "sensor_type": "Temperature",
                "unit": "C",
                "value": 71.5
            },
            {
                "id": "/intelcpu/0/clock/0",
                "hardware_id": "/intelcpu/0",
                "hardware_name": "Intel i5",
                "hardware_type": "Cpu",
                "name": "CPU Core Clock",
                "sensor_type": "Clock",
                "unit": "MHz",
                "value": 2199.0
            },
            {"id": "/bad/nan", "value": "NaN"}
        ]
    }


def test_storage_and_csv(tmp_path: Path):
    with Storage(tmp_path / "sub" / "surface.db") as store:
        sid = store.start_session(
            label="baseline", process_name="Hollow Knight.exe",
            interval_ms=1000, bridge="sensor", presentmon=None
        )
        store.add_sample(sid, sample(), None)
        store.add_sample(sid, sample(2), 1234)
        store.add_event(sid, "game_started", {"pid": 1234})
        store.end_session(sid)
        assert len(store.sessions()) == 1
        assert store.db.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == 4
        assert store.db.execute("SELECT game_pid FROM samples ORDER BY id DESC LIMIT 1").fetchone()[0] == 1234
        summary = session_summary(store, sid)
        assert summary["counts"]["samples"] == 2
        assert summary["coverage"][0]["hardware_type"] == "Cpu"
        assert any("GpuIntel" in warning for warning in summary["warnings"])
        paths = store.export(sid, tmp_path / "csv")
        assert len(paths) == 3
        with (tmp_path / "csv" / "sensors.csv").open(encoding="utf-8-sig", newline="") as file:
            rows = list(csv.DictReader(file))
        assert len(rows) == 4
        assert rows[0]["timestamp_utc"].endswith("+00:00")


def test_reports_create_only_available_charts(tmp_path: Path):
    with Storage(tmp_path / "data.db") as store:
        sid = store.start_session(
            label="test", process_name="Hollow Knight.exe",
            interval_ms=1000, bridge="fake", presentmon=None
        )
        store.add_sample(sid, sample(), None)
        paths = write_report(store, sid, tmp_path / "reports")
    assert (tmp_path / "reports" / "summary.json") in paths
    assert (tmp_path / "reports" / "cpu_temperature.png") in paths
    assert not (tmp_path / "reports" / "intel_gpu_temperature.png").exists()


def test_missing_time_zone_rejected(tmp_path: Path):
    with Storage(tmp_path / "data.db") as store:
        sid = store.start_session(
            label="test", process_name="Hollow Knight.exe",
            interval_ms=1000, bridge="fake", presentmon=None
        )
        pkt = sample()
        pkt["timestamp_utc"] = "2026-10-22T02:25:00"
        with pytest.raises(ValueError, match="timezone"):
            store.add_sample(sid, pkt, None)
        assert store.db.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 0
