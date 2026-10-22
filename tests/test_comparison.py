from __future__ import annotations

from pathlib import Path

import pytest

from surfaace_monitor.comparison import compare_sessions, write_comparison
from surfaace_monitor.storage import Storage


def _sample(sensor_id: str, kind: str, name: str, value: float):
    return {
        "kind": "sample", "timestamp_utc": "2026-10-22T10:00:00+00:00",
        "sequence": 1,
        "sensors": [{
            "id": sensor_id,
            "hardware_id": "/intelcpu/0",
            "hardware_type": "Cpu",
            "hardware_name": "Intel Core i5",
            "sensor_type": kind, "unit": "C" if kind == "Temperature" else "MHz",
            "name": name, "value": value
        }]
    }


def test_comparison_uses_matching_sensor_ids_not_means_of_all_cpu_sensors(tmp_path: Path):
    with Storage(tmp_path / "test.db") as db:
        a = db.start_session(
            label="baseline", process_name="Hollow Knight.exe",
            interval_ms=1000, bridge="test", presentmon=None
        )
        b = db.start_session(
            label="variant", process_name="Hollow Knight.exe",
            interval_ms=1000, bridge="test", presentmon=None
        )
        db.add_sample(a, _sample("/cpu/package-temperature", "Temperature",
                                 "CPU Package", 70), None)
        db.add_sample(b, _sample("/cpu/package-temperature", "Temperature",
                                 "CPU Package", 65), None)
        db.add_sample(b, _sample("/cpu/clock-only-in-b", "Clock",
                                 "CPU Clock", 1880), None)
        comparison = compare_sessions(db, a, b)
        assert len(comparison["matched_sensors"]) == 1
        matched = comparison["matched_sensors"][0]
        assert matched["absolute_delta"] == pytest.approx(-5)
        assert matched["percent_delta"] == pytest.approx(-100.0 * 5 / 70)
        assert comparison["variant_only_sensor_ids"] == ["/cpu/clock-only-in-b"]
        assert comparison["performance"]["average_fps_delta"] is None
        assert any("Actual CPU clock" in s for s in comparison["warnings"])
        out = write_comparison(db, a, b, tmp_path / "compare.json")
        assert out.exists()
        with pytest.raises(ValueError, match="two distinct"):
            compare_sessions(db, a, a)
