"""Run the self-contained C# sensor bridge as a persistent NDJSON producer."""

from __future__ import annotations

import json
import logging
import os
import queue
import signal
import subprocess
import sys
import threading
from collections.abc import Sequence
from typing import Any

LOGGER = logging.getLogger(__name__)


def stop_process(process: subprocess.Popen[str], *, grace_seconds: float = 5.0) -> int | None:
    """Ask an owned child to stop gracefully, then force-stop on timeout."""
    if process.poll() is not None:
        return process.returncode
    if sys.platform == "win32":
        try:
            process.send_signal(signal.CTRL_BREAK_EVENT)
        except (OSError, ValueError):
            pass
    else:
        process.terminate()
    try:
        return process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            return process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            return process.wait(timeout=2)


class BridgeClient:
    def __init__(self, command: Sequence[str], *, max_pending: int = 1000):
        self.command = list(command)
        self.queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=max_pending)
        self.process: subprocess.Popen[str] | None = None
        self.dropped = 0

    def start(self) -> None:
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
        self.process = subprocess.Popen(
            self.command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8",
            errors="replace", bufsize=1, creationflags=flags
        )
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        for line in self.process.stdout:
            try:
                packet = json.loads(line)
            except json.JSONDecodeError:
                LOGGER.warning("Ignoring malformed sensor bridge JSON: %.200s", line)
                continue
            if not isinstance(packet, dict):
                continue
            try:
                self.queue.put_nowait(packet)
            except queue.Full:
                # Prefer the newest sensor sample rather than blocking stdout forever.
                try:
                    self.queue.get_nowait()
                except queue.Empty:
                    pass
                self.dropped += 1
                try:
                    self.queue.put_nowait(packet)
                except queue.Full:
                    self.dropped += 1

    def _read_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        for line in self.process.stderr:
            LOGGER.warning("Sensor bridge: %s", line.rstrip())

    def next_frame(self, timeout: float = 0.25) -> dict[str, Any] | None:
        try:
            return self.queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def poll(self) -> int | None:
        return None if self.process is None else self.process.poll()

    def close(self) -> int | None:
        return None if self.process is None else stop_process(self.process)


def default_bridge_command(executable: str, interval_ms: int) -> list[str]:
    return [executable, "--interval-ms", str(interval_ms)]
