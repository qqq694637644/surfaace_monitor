"""Optional read-only Windows PDH performance and ACPI thermal-zone telemetry.

Neither represents a direct CPU-package temperature or actual core MHz.
"""

from __future__ import annotations

import ctypes
import json
import logging
import math
import queue
import subprocess
import sys
import threading
from ctypes import wintypes
from datetime import datetime, timezone
from typing import Any

LOGGER = logging.getLogger(__name__)


def pawnio_installed() -> bool | None:
    """Match LibreHardwareMonitor's PawnIO uninstall-registry check."""
    if sys.platform != "win32":
        return None
    import winreg

    key_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\PawnIO"
    for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path, 0,
                                winreg.KEY_READ | view):
                return True
        except OSError:
            continue
    return False


class _PdhFormattedValue(ctypes.Structure):
    _fields_ = [
        ("status", wintypes.DWORD),
        ("value", ctypes.c_double)
    ]


class WindowsProcessorPerformance:
    """Windows % Processor Performance: relative-to-nominal %, NOT measured MHz."""

    COUNTER = r"\Processor Information(_Total)\% Processor Performance"

    def __init__(self) -> None:
        self._library: Any = None
        self._query = wintypes.HANDLE()
        self._counter = wintypes.HANDLE()

    def open(self) -> bool:
        if sys.platform != "win32":
            return False
        try:
            lib = ctypes.WinDLL("pdh.dll")
            lib.PdhOpenQueryW.argtypes = [
                wintypes.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(wintypes.HANDLE)
            ]
            lib.PdhOpenQueryW.restype = wintypes.LONG
            lib.PdhAddEnglishCounterW.argtypes = [
                wintypes.HANDLE, wintypes.LPCWSTR, ctypes.c_size_t,
                ctypes.POINTER(wintypes.HANDLE)
            ]
            lib.PdhAddEnglishCounterW.restype = wintypes.LONG
            lib.PdhCollectQueryData.argtypes = [wintypes.HANDLE]
            lib.PdhCollectQueryData.restype = wintypes.LONG
            lib.PdhGetFormattedCounterValue.argtypes = [
                wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                ctypes.POINTER(_PdhFormattedValue)
            ]
            lib.PdhGetFormattedCounterValue.restype = wintypes.LONG
            lib.PdhCloseQuery.argtypes = [wintypes.HANDLE]
            lib.PdhCloseQuery.restype = wintypes.LONG
            self._library = lib
            if lib.PdhOpenQueryW(None, 0, ctypes.byref(self._query)) != 0:
                return False
            if lib.PdhAddEnglishCounterW(
                self._query, self.COUNTER, 0, ctypes.byref(self._counter)
            ) != 0:
                self.close()
                return False
            lib.PdhCollectQueryData(self._query)  # prime counter
            return True
        except (OSError, ValueError, AttributeError) as error:
            LOGGER.warning("Windows PDH unavailable: %s", error)
            self.close()
            return False

    def read_sensors(self) -> list[dict[str, Any]]:
        if self._library is None or not self._query.value or not self._counter.value:
            return []
        if self._library.PdhCollectQueryData(self._query) != 0:
            return []
        result = _PdhFormattedValue()
        status = self._library.PdhGetFormattedCounterValue(
            self._counter, 0x00000200 | 0x00008000, None, ctypes.byref(result)
        )
        if status != 0 or result.status not in (0, 1) or not math.isfinite(result.value):
            return []
        return [{
            "id": "/windows/pdh/processor-performance-percent",
            "hardware_id": "/windows/pdh/processor-information",
            "hardware_name": "Windows Processor Information (_Total)",
            "hardware_type": "WindowsPerfCounter",
            "name": "Processor Performance vs Nominal (NOT MHz)",
            "sensor_type": "Performance",
            "unit": "%",
            "value": result.value
        }]

    def close(self) -> None:
        if self._library is not None and self._query.value:
            self._library.PdhCloseQuery(self._query)
            self._query = wintypes.HANDLE()
            self._counter = wintypes.HANDLE()


_ACPI_QUERY = (
    "$ErrorActionPreference='Stop'; "
    "$zones = @(Get-CimInstance -Namespace root/wmi "
    "-ClassName MSAcpi_ThermalZoneTemperature | "
    "Select-Object InstanceName,CurrentTemperature); "
    "ConvertTo-Json -InputObject $zones -Compress"
)


def read_acpi_zones() -> list[dict[str, Any]]:
    """Sample firmware thermal zones, explicitly distinct from CPU core/package."""
    if sys.platform != "win32":
        return []
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _ACPI_QUERY],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=10, creationflags=flags, check=False
    )
    if result.returncode:
        raise OSError(f"ACPI WMI query failed: {result.stderr.strip()[:300]}")
    zones = json.loads(result.stdout)
    if isinstance(zones, dict):
        zones = [zones]
    if not isinstance(zones, list):
        return []
    sensors: list[dict[str, Any]] = []
    for zone in zones:
        if not isinstance(zone, dict):
            continue
        name = str(zone.get("InstanceName") or "")
        try:
            celsius = float(zone["CurrentTemperature"]) / 10.0 - 273.15
        except (ValueError, TypeError, KeyError):
            continue
        if not name or not math.isfinite(celsius) or not -50 <= celsius <= 150:
            continue
        sensors.append({
            "id": "/windows/acpi/" + name.casefold().replace("\\", "/"),
            "hardware_id": "/windows/acpi",
            "hardware_name": "ACPI Firmware Thermal Zones (NOT CPU package)",
            "hardware_type": "AcpiThermalZone",
            "name": name,
            "sensor_type": "Temperature",
            "unit": "C",
            "value": round(celsius, 2)
        })
    return sensors


class AcpiThermalCollector:
    """Poll WMI slowly in a background thread to avoid disturbing game sampling."""

    def __init__(self, interval_seconds: float = 30.0):
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._frames: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=2)
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if sys.platform != "win32":
            return
        self._thread = threading.Thread(target=self._work, daemon=True)
        self._thread.start()

    def _work(self) -> None:
        while not self._stop.is_set():
            try:
                sensors = read_acpi_zones()
                if sensors:
                    packet = {
                        "kind": "sample",
                        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                        "sequence": None,
                        "sensors": sensors
                    }
                    if self._frames.full():
                        try:
                            self._frames.get_nowait()
                        except queue.Empty:
                            pass
                    self._frames.put_nowait(packet)
            except (OSError, ValueError, json.JSONDecodeError) as error:
                LOGGER.warning("ACPI thermal zone data unavailable: %s", error)
            self._stop.wait(self.interval_seconds)

    def next_packet(self) -> dict[str, Any] | None:
        try:
            return self._frames.get_nowait()
        except queue.Empty:
            return None

    def close(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=11)
