"""SQLite time-series storage. Missing telemetry is never replaced by estimates."""

from __future__ import annotations

import csv
import json
import math
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def normalized_utc(value: str) -> str:
    timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        raise ValueError("Sensor timestamps must include a timezone")
    return timestamp.astimezone(timezone.utc).isoformat(timespec="milliseconds")


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    process_name TEXT NOT NULL,
    interval_ms INTEGER NOT NULL,
    bridge TEXT NOT NULL,
    presentmon TEXT,
    started_utc TEXT NOT NULL,
    ended_utc TEXT
);
CREATE TABLE IF NOT EXISTS sensors (
    id TEXT PRIMARY KEY,
    hardware_id TEXT NOT NULL,
    hardware_name TEXT NOT NULL,
    hardware_type TEXT NOT NULL,
    name TEXT NOT NULL,
    sensor_type TEXT NOT NULL,
    unit TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    timestamp_utc TEXT NOT NULL,
    sequence INTEGER,
    game_pid INTEGER
);
CREATE INDEX IF NOT EXISTS idx_samples_session_time
    ON samples(session_id, timestamp_utc);
CREATE TABLE IF NOT EXISTS readings (
    sample_id INTEGER NOT NULL REFERENCES samples(id),
    sensor_id TEXT NOT NULL REFERENCES sensors(id),
    value REAL NOT NULL,
    PRIMARY KEY(sample_id, sensor_id)
);
CREATE INDEX IF NOT EXISTS idx_readings_sensor ON readings(sensor_id);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    timestamp_utc TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS captures (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    game_pid INTEGER NOT NULL,
    csv_path TEXT NOT NULL,
    started_utc TEXT NOT NULL,
    ended_utc TEXT,
    return_code INTEGER,
    imported_rows INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS frames (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    capture_id TEXT NOT NULL REFERENCES captures(id),
    source_row INTEGER NOT NULL,
    timestamp_utc TEXT,
    game_pid INTEGER,
    frametime_ms REAL,
    fps REAL,
    UNIQUE(capture_id, source_row)
);
CREATE INDEX IF NOT EXISTS idx_frames_capture_time
    ON frames(capture_id, timestamp_utc);
"""


class Storage:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout = 30000")
        self.db.execute("PRAGMA journal_mode = WAL")
        self.db.executescript(SCHEMA)
        self._known_sensors = {
            row[0] for row in self.db.execute("SELECT id FROM sensors")
        }

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "Storage":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def start_session(
        self, *, label: str, process_name: str, interval_ms: int,
        bridge: str, presentmon: str | None
    ) -> str:
        sid = uuid.uuid4().hex
        with self.db:
            self.db.execute(
                """INSERT INTO sessions
                   (id, label, process_name, interval_ms, bridge, presentmon, started_utc)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (sid, label, process_name, interval_ms, bridge, presentmon, utc_now())
            )
        return sid

    def end_session(self, sid: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE sessions SET ended_utc = ? WHERE id = ?",
                (utc_now(), sid)
            )

    def add_event(self, sid: str, kind: str, detail: dict[str, Any] | None = None) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO events (session_id, timestamp_utc, kind, detail) VALUES (?, ?, ?, ?)",
                (sid, utc_now(), kind, json.dumps(detail or {}, ensure_ascii=False))
            )

    def add_sample(self, sid: str, packet: dict[str, Any], game_pid: int | None) -> int:
        if packet.get("kind") != "sample" or not isinstance(packet.get("sensors"), list):
            raise ValueError("Invalid sensor packet")
        timestamp = normalized_utc(packet["timestamp_utc"])
        rows = []
        for sensor in packet["sensors"]:
            if not isinstance(sensor, dict) or not sensor.get("id"):
                continue
            try:
                value = float(sensor["value"])
            except (KeyError, ValueError, TypeError):
                continue
            if not math.isfinite(value):
                continue
            sensor_id = str(sensor["id"])
            meta = (
                sensor_id, str(sensor.get("hardware_id", "")),
                str(sensor.get("hardware_name", "")),
                str(sensor.get("hardware_type", "")),
                str(sensor.get("name", "")),
                str(sensor.get("sensor_type", "")),
                str(sensor.get("unit", ""))
            )
            rows.append((meta, value))

        with self.db:
            cur = self.db.execute(
                """INSERT INTO samples (session_id, timestamp_utc, sequence, game_pid)
                   VALUES (?, ?, ?, ?)""",
                (sid, timestamp, packet.get("sequence"), game_pid)
            )
            sample_id = cur.lastrowid
            for meta, value in rows:
                if meta[0] not in self._known_sensors:
                    self.db.execute(
                        """INSERT OR IGNORE INTO sensors
                           (id, hardware_id, hardware_name, hardware_type,
                            name, sensor_type, unit)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        meta
                    )
                    self._known_sensors.add(meta[0])
                self.db.execute(
                    "INSERT OR REPLACE INTO readings (sample_id, sensor_id, value) VALUES (?, ?, ?)",
                    (sample_id, meta[0], value)
                )
        return int(sample_id)

    def start_capture(self, sid: str, game_pid: int, csv_path: Path) -> str:
        capture_id = uuid.uuid4().hex
        with self.db:
            self.db.execute(
                """INSERT INTO captures
                   (id, session_id, game_pid, csv_path, started_utc)
                   VALUES (?, ?, ?, ?, ?)""",
                (capture_id, sid, game_pid, str(csv_path), utc_now())
            )
        return capture_id

    def finish_capture(self, capture_id: str, return_code: int | None) -> None:
        with self.db:
            self.db.execute(
                "UPDATE captures SET ended_utc = ?, return_code = ? WHERE id = ?",
                (utc_now(), return_code, capture_id)
            )

    def add_frames(
        self,
        capture_id: str,
        frames: Iterable[tuple[int, str | None, int | None, float | None, float | None]]
    ) -> int:
        before = self.db.total_changes
        with self.db:
            self.db.executemany(
                """INSERT OR IGNORE INTO frames
                   (capture_id, source_row, timestamp_utc, game_pid, frametime_ms, fps)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                ((capture_id, *frame) for frame in frames)
            )
        return self.db.total_changes - before

    def set_imported_rows(self, capture_id: str) -> None:
        with self.db:
            self.db.execute(
                """UPDATE captures SET imported_rows =
                    (SELECT COUNT(*) FROM frames WHERE capture_id = ?)
                   WHERE id = ?""",
                (capture_id, capture_id)
            )

    def sessions(self) -> list[sqlite3.Row]:
        return self.db.execute(
            """SELECT s.*, (SELECT COUNT(*) FROM samples x WHERE x.session_id=s.id)
               AS sample_count, (SELECT COUNT(*) FROM captures c WHERE c.session_id=s.id)
               AS capture_count
               FROM sessions s ORDER BY started_utc DESC"""
        ).fetchall()

    def session(self, sid: str) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
        if row is None:
            raise ValueError(f"Unknown session: {sid}")
        return row

    def export(self, sid: str, output_dir: Path) -> list[Path]:
        self.session(sid)
        output_dir.mkdir(parents=True, exist_ok=True)
        queries = {
            "sensors.csv": """SELECT a.timestamp_utc, a.game_pid, a.sequence,
                        s.hardware_type, s.hardware_name, s.name AS sensor_name,
                        s.sensor_type, s.unit, s.id AS sensor_id, r.value
                        FROM readings r
                        JOIN samples a ON a.id=r.sample_id
                        JOIN sensors s ON s.id=r.sensor_id
                        WHERE a.session_id=? ORDER BY a.id, s.id""",
            "frames.csv": """SELECT f.timestamp_utc, f.game_pid, f.frametime_ms,
                         f.fps, f.capture_id, f.source_row
                         FROM frames f JOIN captures c ON c.id=f.capture_id
                         WHERE c.session_id=? ORDER BY f.id""",
            "events.csv": """SELECT timestamp_utc, kind, detail
                         FROM events WHERE session_id=? ORDER BY id"""
        }
        created = []
        for filename, query in queries.items():
            path = output_dir / filename
            cursor = self.db.execute(query, (sid,))
            with path.open("w", newline="", encoding="utf-8-sig") as file:
                writer = csv.writer(file)
                writer.writerow([column[0] for column in cursor.description])
                writer.writerows(cursor)
            created.append(path)
        return created
