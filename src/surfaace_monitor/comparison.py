"""Compare two recorded sessions without manufacturing missing throttling metrics."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .report import session_summary
from .storage import Storage


def _sensor_averages(store: Storage, sid: str) -> dict[str, dict[str, Any]]:
    sql = """
    SELECT s.id, s.hardware_type, s.hardware_name, s.name, s.sensor_type, s.unit,
           MIN(r.value) AS minimum, MAX(r.value) AS maximum, AVG(r.value) AS average,
           COUNT(*) AS reading_count
    FROM samples x JOIN readings r ON r.sample_id=x.id
    JOIN sensors s ON s.id=r.sensor_id
    WHERE x.session_id=?
    GROUP BY s.id
    ORDER BY s.hardware_type, s.sensor_type, s.id
    """
    return {row["id"]: dict(row) for row in store.db.execute(sql, (sid,))}


def compare_sessions(store: Storage, baseline: str, variant: str) -> dict[str, Any]:
    if baseline == variant:
        raise ValueError("Comparison requires two distinct sessions")
    left = session_summary(store, baseline)
    right = session_summary(store, variant)
    first = _sensor_averages(store, baseline)
    second = _sensor_averages(store, variant)
    rows: list[dict[str, Any]] = []

    for sensor_id in sorted(first.keys() & second.keys()):
        a, b = first[sensor_id], second[sensor_id]
        # Sensor IDs are stable identifiers from LHM or fixed Windows fallbacks.
        if (a["hardware_type"], a["sensor_type"], a["unit"]) != (
            b["hardware_type"], b["sensor_type"], b["unit"]
        ):
            continue
        change = b["average"] - a["average"]
        rows.append({
            "id": sensor_id,
            "hardware_type": a["hardware_type"],
            "sensor_type": a["sensor_type"],
            "hardware_name": a["hardware_name"],
            "name": a["name"],
            "unit": a["unit"],
            "baseline_readings": a["reading_count"],
            "variant_readings": b["reading_count"],
            "baseline_average": a["average"],
            "variant_average": b["average"],
            "absolute_delta": change,
            "percent_delta": (change / abs(a["average"]) * 100
                              if a["average"] != 0 else None),
            "baseline_min": a["minimum"], "baseline_max": a["maximum"],
            "variant_min": b["minimum"], "variant_max": b["maximum"]
        })

    fps_a = left["performance"]["average_fps"]
    fps_b = right["performance"]["average_fps"]
    warnings = [
        "These are descriptive comparisons of two recorded sessions, NOT causal proof.",
        "Control game scene, power mode, room temperature, graphics settings and test duration.",
        "CPU thermal/power limit throttling cannot be inferred from clock drops alone."
    ]
    if fps_a is None or fps_b is None:
        warnings.append("FPS comparison unavailable: one or both sessions have no PresentMon frames.")
    if not any(r["hardware_type"] == "Cpu" and r["sensor_type"] == "Temperature"
               for r in rows):
        warnings.append("CPU-package temperature comparison unavailable: no matched CPU temperature sensor.")
    if not any(r["hardware_type"] == "Cpu" and r["sensor_type"] == "Clock"
               for r in rows):
        warnings.append("Actual CPU clock comparison unavailable: no matched CPU clock sensor.")
    if not any(r["hardware_type"] == "GpuIntel" and r["sensor_type"] == "Clock"
               for r in rows):
        warnings.append("Intel iGPU clock comparison unavailable: no matched GPU clock sensor.")
    if any(r["sensor_type"] == "Power" and r["baseline_max"] == 0 and
           r["variant_max"] == 0 for r in rows):
        warnings.append("At least one power sensor returned only 0 W in both sessions: likely unsupported.")
    if any(r["hardware_type"] == "AcpiThermalZone" for r in rows):
        warnings.append("ACPI thermal-zone comparisons do not measure CPU package temperature.")
    if any(r["hardware_type"] == "WindowsPerfCounter" for r in rows):
        warnings.append("PDH Processor Performance is a relative percentage, not measured MHz.")

    return {
        "baseline": {key: left["session"][key]
                     for key in ("id", "label", "started_utc", "ended_utc")},
        "variant": {key: right["session"][key]
                    for key in ("id", "label", "started_utc", "ended_utc")},
        "performance": {
            "baseline_frames": left["counts"]["frames"],
            "variant_frames": right["counts"]["frames"],
            "baseline_average_fps": fps_a,
            "variant_average_fps": fps_b,
            "average_fps_delta": fps_b - fps_a if fps_a is not None and fps_b is not None else None
        },
        "matched_sensors": rows,
        "baseline_only_sensor_ids": sorted(first.keys() - second.keys()),
        "variant_only_sensor_ids": sorted(second.keys() - first.keys()),
        "warnings": warnings
    }


def write_comparison(store: Storage, baseline: str, variant: str, destination: Path) -> Path:
    result = compare_sessions(store, baseline, variant)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return destination
