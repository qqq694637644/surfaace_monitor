"""Read-only, resilient long-running sensor and game-process sampling."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable, Sequence

import psutil

from .bridge_client import BridgeClient, default_bridge_command
from .presentmon import CaptureManager
from .storage import Storage

LOGGER = logging.getLogger(__name__)


def find_process(name: str) -> int | None:
    """Locate the oldest exact-name match, not unrelated similarly named apps."""
    matches: list[tuple[float, int]] = []
    for proc in psutil.process_iter(attrs=["pid", "name", "create_time"]):
        try:
            info = proc.info
            if str(info.get("name") or "").casefold() == name.casefold():
                matches.append((float(info.get("create_time") or 0), int(info["pid"])))
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
    return min(matches)[1] if matches else None


def run_monitor(
    store: Storage, *,
    bridge_command: Sequence[str],
    interval_ms: int = 1000,
    game_name: str = "Hollow Knight.exe",
    label: str = "baseline",
    presentmon: str | None = None,
    capture_dir: Path = Path("recordings/presentmon"),
    duration: float | None = None,
    game_finder: Callable[[str], int | None] = find_process,
    status_interval: float = 30.0,
) -> str:
    if interval_ms < 250 or interval_ms > 60000:
        raise ValueError("Bridge interval must be 250..60000 milliseconds")
    if duration is not None and duration <= 0:
        raise ValueError("Duration must be positive")

    sid = store.start_session(
        label=label, process_name=game_name, interval_ms=interval_ms,
        bridge=" ".join(bridge_command), presentmon=presentmon
    )
    captures = CaptureManager(store, sid, presentmon, capture_dir)
    bridge: BridgeClient | None = None
    game_pid: int | None = None
    total = 0
    last_drop_count = 0
    startup = time.monotonic()
    next_check = startup
    next_status = startup + status_interval
    next_retry = startup
    last_sample = startup
    print(f"Session: {sid} | Tag: {label} | Game: {game_name}", flush=True)

    try:
        while True:
            now = time.monotonic()
            if duration is not None and now - startup >= duration:
                break

            if now >= next_check:
                try:
                    current = game_finder(game_name)
                except (psutil.Error, OSError) as error:
                    LOGGER.warning("Process scan failed: %s", error)
                    current = game_pid

                if current != game_pid:
                    if game_pid is not None:
                        captures.stop()
                        store.add_event(sid, "game_stopped", {"pid": game_pid})
                    game_pid = current
                    if game_pid is not None:
                        store.add_event(sid, "game_started", {"pid": game_pid})
                        try:
                            captures.start(game_pid)
                        except OSError as error:
                            store.add_event(sid, "presentmon_start_error", {"error": str(error)})
                            LOGGER.warning("PresentMon cannot start: %s", error)
                elif captures.check_exited():
                    LOGGER.warning("PresentMon exited while the game is still running")
                next_check = now + 1.0

            if bridge is None and now >= next_retry:
                candidate = BridgeClient(bridge_command)
                try:
                    candidate.start()
                    bridge = candidate
                    last_sample = now
                    last_drop_count = 0
                    store.add_event(sid, "bridge_connected")
                except OSError as error:
                    store.add_event(sid, "bridge_start_error", {"error": str(error)})
                    LOGGER.error("Cannot start sensor bridge: %s", error)
                    next_retry = now + 5.0

            packet = bridge.next_frame(timeout=0.25) if bridge is not None else None
            if packet is not None:
                try:
                    store.add_sample(sid, packet, game_pid)
                    last_sample = time.monotonic()
                    total += 1
                except (KeyError, TypeError, ValueError) as error:
                    store.add_event(sid, "sensor_packet_invalid", {"error": str(error)})
                    LOGGER.warning("Ignoring invalid sensor packet: %s", error)

            if bridge is not None:
                if bridge.dropped > last_drop_count:
                    lost = bridge.dropped - last_drop_count
                    last_drop_count = bridge.dropped
                    store.add_event(sid, "sensor_queue_overflow", {"dropped": lost})

                if (bridge.poll() is not None and bridge.queue.empty()) or (
                    time.monotonic() - last_sample > max(15.0, interval_ms / 1000 * 5)
                ):
                    code = bridge.close()
                    store.add_event(sid, "bridge_disconnected", {"return_code": code})
                    LOGGER.warning("Sensor bridge disconnected (exit code %s); retrying", code)
                    bridge = None
                    next_retry = time.monotonic() + 5.0

            if time.monotonic() >= next_status:
                LOGGER.info(
                    "Session %s: %s hardware samples; game PID: %s",
                    sid[:8], total, game_pid
                )
                next_status = time.monotonic() + status_interval
    finally:
        if bridge is not None:
            bridge.close()
        captures.stop()
        if game_pid is not None:
            store.add_event(sid, "game_stopped", {"pid": game_pid, "reason": "monitor_exit"})
        store.end_session(sid)
        print(f"Saved session {sid}: {total} sensor samples", flush=True)
    return sid


def executable_command(bridge_path: str, interval_ms: int) -> list[str]:
    return default_bridge_command(bridge_path, interval_ms)
