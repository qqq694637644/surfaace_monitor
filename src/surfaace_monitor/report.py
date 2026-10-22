"""Minute-binned graphics, coverage audit, and factual session summary."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from .storage import Storage


def session_summary(store: Storage, sid: str) -> dict[str, Any]:
    session = dict(store.session(sid))
    counters = store.db.execute(
        """SELECT (SELECT COUNT(*) FROM samples WHERE session_id=?) AS samples,
                  (SELECT COUNT(*) FROM frames f JOIN captures c ON c.id=f.capture_id
                   WHERE c.session_id=?) AS frames,
                  (SELECT COUNT(*) FROM captures WHERE session_id=?) AS captures""",
        (sid, sid, sid)
    ).fetchone()
    coverage = store.db.execute(
        """SELECT s.hardware_type, s.sensor_type, COUNT(DISTINCT s.id) AS sensors,
                  MIN(r.value) AS min_value, MAX(r.value) AS max_value,
                  AVG(r.value) AS average_value
           FROM readings r JOIN sensors s ON s.id=r.sensor_id
           JOIN samples a ON a.id=r.sample_id WHERE a.session_id=?
           GROUP BY s.hardware_type, s.sensor_type
           ORDER BY s.hardware_type, s.sensor_type""",
        (sid,)
    ).fetchall()
    perf = store.db.execute(
        """SELECT AVG(f.fps) AS average_fps, MIN(f.fps) AS min_fps,
                  MAX(f.frametime_ms) AS max_frametime_ms,
                  SUM(CASE WHEN f.timestamp_utc IS NOT NULL THEN 1 ELSE 0 END)
                  AS correlated_frames
           FROM frames f JOIN captures c ON c.id=f.capture_id WHERE c.session_id=?""",
        (sid,)
    ).fetchone()
    available = {(r["hardware_type"], r["sensor_type"]) for r in coverage}
    warnings = []
    for kind, measurement in [
        ("Cpu", "Temperature"), ("Cpu", "Clock"), ("Cpu", "Power"),
        ("GpuIntel", "Temperature"), ("GpuIntel", "Clock")
    ]:
        if (kind, measurement) not in available:
            warnings.append(f"No {kind} {measurement} sensor reported; value is unavailable.")
    if counters["frames"] == 0:
        warnings.append("No PresentMon frame data imported. FPS/frametime unavailable.")
    elif perf["correlated_frames"] == 0:
        warnings.append("PresentMon rows have no absolute timestamps; cannot correlate FPS and temperatures.")
    if ("AcpiThermalZone", "Temperature") in available:
        warnings.append(
            "ACPI firmware thermal-zone temperatures are NOT CPU package/core temperatures."
        )
    if ("WindowsPerfCounter", "Performance") in available:
        warnings.append(
            "Windows PDH Processor Performance is a relative percentage, NOT measured CPU MHz."
        )
    for record in coverage:
        if (record["hardware_type"] == "Cpu" and record["sensor_type"] == "Power"
                and record["min_value"] == 0 and record["max_value"] == 0):
            warnings.append("CPU power sensors reported only 0 W; may be inaccessible, not true idle power.")
    warnings.append(
        "Frequency drops alone do not prove thermal throttling or PL1/PL2 throttling. "
        "Confirm with supported limit flags / HWiNFO or controlled A/B tests."
    )
    return {
        "session": session,
        "counts": dict(counters),
        "coverage": [dict(r) for r in coverage],
        "performance": dict(perf),
        "warnings": warnings,
    }


def _draw_series(
    path: Path, title: str, unit: str,
    data: dict[str, list[tuple[datetime, float]]]
) -> bool:
    if not data:
        return False
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    figure, ax = plt.subplots(figsize=(12, 5))
    for label, points in data.items():
        points.sort()
        ax.plot([p[0] for p in points], [p[1] for p in points], label=label, linewidth=1.2)
    ax.set_title(title)
    ax.set_xlabel("Local timeline shown in UTC")
    ax.set_ylabel(unit)
    ax.grid(alpha=0.3)
    ax.legend(loc="best", fontsize="small")
    figure.autofmt_xdate()
    figure.tight_layout()
    figure.savefig(path, dpi=140)
    plt.close(figure)
    return True


def write_report(store: Storage, sid: str, out_dir: Path) -> list[Path]:
    summary = session_summary(store, sid)
    out_dir.mkdir(parents=True, exist_ok=True)
    summary_path = out_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    created = [summary_path]

    for hardware_type, sensor_type, filename, unit in [
        ("Cpu", "Temperature", "cpu_temperature.png", "C"),
        ("GpuIntel", "Temperature", "intel_gpu_temperature.png", "C"),
        ("Cpu", "Clock", "cpu_clock.png", "MHz"),
        ("GpuIntel", "Clock", "intel_gpu_clock.png", "MHz"),
        ("Cpu", "Power", "cpu_power.png", "W"),
        ("GpuIntel", "Load", "intel_gpu_load.png", "%"),
        ("WindowsPerfCounter", "Performance", "cpu_performance_percent.png", "%"),
        ("AcpiThermalZone", "Temperature", "acpi_thermal_zones.png", "C"),
    ]:
        rows = store.db.execute(
            """SELECT substr(a.timestamp_utc, 1, 16) AS minute, s.id,
                      s.hardware_name, s.name, AVG(r.value) AS average_value,
                      COUNT(*) AS n
               FROM readings r JOIN sensors s ON s.id=r.sensor_id
               JOIN samples a ON a.id=r.sample_id
               WHERE a.session_id=? AND s.hardware_type=? AND s.sensor_type=?
               GROUP BY minute, s.id ORDER BY minute""",
            (sid, hardware_type, sensor_type)
        )
        series: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
        for row in rows:
            label = f"{row['name']} ({row['id']})"
            series[label].append((
                datetime.fromisoformat(row["minute"] + ":00+00:00"),
                row["average_value"]
            ))
        # Select a manageable set of sensors; keep package/total/core-max first.
        ranked = sorted(
            series,
            key=lambda key: (
                0 if any(x in key.casefold() for x in ("package", "total", "max", "gpu core")) else 1,
                -len(series[key]), key
            )
        )
        selected = {key: series[key] for key in ranked[:8]}
        target = out_dir / filename
        title = f"{hardware_type} {sensor_type} (minute averages)"
        if hardware_type == "AcpiThermalZone":
            title += " — NOT CPU package"
        if hardware_type == "WindowsPerfCounter":
            title += " — relative performance, NOT MHz"
        if _draw_series(target, title, unit, selected):
            created.append(target)

    rows = store.db.execute(
        """SELECT substr(f.timestamp_utc, 1, 16) AS minute, AVG(f.fps) AS fps
           FROM frames f JOIN captures c ON c.id=f.capture_id
           WHERE c.session_id=? AND f.timestamp_utc IS NOT NULL
           GROUP BY minute ORDER BY minute""",
        (sid,)
    )
    fps_points = [
        (datetime.fromisoformat(row["minute"] + ":00+00:00"), row["fps"])
        for row in rows
    ]
    fps_path = out_dir / "game_fps.png"
    if _draw_series(fps_path, "Hollow Knight frame rate (minute averages)", "FPS",
                    {"PresentMon FPS": fps_points} if fps_points else {}):
        created.append(fps_path)
    return created
