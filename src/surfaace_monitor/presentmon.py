"""PresentMon capture management and conservative CSV-to-SQLite import."""

from __future__ import annotations

import csv
import logging
import math
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .bridge_client import stop_process
from .storage import Storage

LOGGER = logging.getLogger(__name__)
DATETIME_PATTERN = re.compile(r"^(\d{4}-\d{1,2}-\d{1,2}[ T]\d{1,2}:\d{2}:\d{2})(?:\.(\d+))?$")


def _number(value: Any) -> float | None:
    if value is None or str(value).strip() in ("", "NA", "N/A"):
        return None
    try:
        number = float(value)
    except (ValueError, TypeError):
        return None
    return number if math.isfinite(number) else None


def _frame_utc(value: str | None) -> str | None:
    """PresentMon --date_time emits *local* wall-clock timestamps, up to nanoseconds."""
    if not value or value == "NA":
        return None
    match = DATETIME_PATTERN.fullmatch(value.strip())
    if not match:
        return None
    date_time, fraction = match.groups()
    fraction = (fraction or "")[:6].ljust(6, "0")
    try:
        local_time = datetime.fromisoformat(f"{date_time}.{fraction}")
        # astimezone() resolves naive local time using the host's timezone/DST.
        return local_time.astimezone(timezone.utc).isoformat(timespec="milliseconds")
    except ValueError:
        return None


def import_csv(store: Storage, capture_id: str, path: Path) -> tuple[int, int]:
    """Import valid frame intervals. No synthetic FPS or timestamps are generated."""
    if not path.is_file():
        return 0, 0
    inserted = 0
    timestamped = 0
    pending: list[tuple[int, str | None, int | None, float | None, float | None]] = []

    with path.open("r", newline="", encoding="utf-8-sig", errors="replace") as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames:
            return 0, 0
        for source_row, item in enumerate(reader, start=2):
            elapsed_ms = None
            for key in ("MsBetweenPresents", "msBetweenPresents", "FrameTime"):
                elapsed_ms = _number(item.get(key))
                if elapsed_ms is not None:
                    break
            if elapsed_ms is None or elapsed_ms <= 0:
                continue

            timestamp = _frame_utc(item.get("CPUStartDateTime"))
            pid_str = item.get("ProcessID")
            try:
                pid = int(pid_str) if pid_str else None
            except ValueError:
                pid = None

            pending.append((source_row, timestamp, pid, elapsed_ms, 1000.0 / elapsed_ms))
            if timestamp is not None:
                timestamped += 1
            if len(pending) >= 2000:
                inserted += store.add_frames(capture_id, pending)
                pending.clear()

    if pending:
        inserted += store.add_frames(capture_id, pending)
    store.set_imported_rows(capture_id)
    return inserted, timestamped


class CaptureManager:
    """Starts PresentMon only when the target game PID exists."""

    def __init__(self, store: Storage, sid: str, executable: str | None, directory: Path):
        self.store = store
        self.sid = sid
        self.executable = executable
        self.directory = directory
        self.process: subprocess.Popen[str] | None = None
        self.log_file: Any = None
        self.capture_id: str | None = None
        self.csv_path: Path | None = None

    def start(self, game_pid: int) -> None:
        if not self.executable:
            return
        self.directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = self.directory / f"presentmon_{self.sid[:8]}_{game_pid}_{stamp}.csv"
        logfile = path.with_suffix(".log")
        handle = logfile.open("w", encoding="utf-8")
        command = [
            self.executable, "--process_id", str(game_pid),
            "--output_file", str(path.resolve()),
            "--date_time", "--no_console_stats", "--terminate_on_proc_exit",
            "--session_name", f"SurfaceMonitor-{self.sid[:8]}-{game_pid}"
        ]
        try:
            flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
            process = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=handle,
                stderr=subprocess.STDOUT, creationflags=flags
            )
        except (OSError, ValueError):
            handle.close()
            raise
        self.process = process
        self.csv_path = path
        self.log_file = handle
        self.capture_id = self.store.start_capture(self.sid, game_pid, path)
        self.store.add_event(self.sid, "presentmon_started", {"pid": game_pid, "csv": str(path)})

    def check_exited(self) -> bool:
        if self.process is None or self.process.poll() is None:
            return False
        self.stop()
        return True

    def stop(self) -> tuple[int, int]:
        if self.process is None:
            return 0, 0
        process = self.process
        code = stop_process(process, grace_seconds=8)
        assert self.capture_id is not None and self.csv_path is not None
        if self.log_file is not None:
            self.log_file.close()

        self.store.finish_capture(self.capture_id, code)
        count, timestamped = 0, 0
        try:
            count, timestamped = import_csv(self.store, self.capture_id, self.csv_path)
        except (OSError, UnicodeError, csv.Error) as error:
            self.store.add_event(self.sid, "presentmon_import_error", {"error": str(error)})
            LOGGER.exception("PresentMon CSV import failed")
        self.store.add_event(
            self.sid, "presentmon_stopped",
            {"return_code": code, "frames_imported": count, "frames_timestamped": timestamped,
             "csv": str(self.csv_path)}
        )
        self.process, self.capture_id, self.csv_path, self.log_file = None, None, None, None
        return count, timestamped
