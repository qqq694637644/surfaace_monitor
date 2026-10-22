"""CLI for diagnostics, capture, import, SQLite export and report generation."""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from pathlib import Path

from . import __version__
from .monitor import executable_command, run_monitor
from .presentmon import import_csv
from .report import session_summary, write_report
from .storage import Storage

LOG = logging.getLogger(__name__)


def _session_id(store: Storage, requested: str | None) -> str:
    if requested:
        store.session(requested)
        return requested
    all_sessions = store.sessions()
    if not all_sessions:
        raise ValueError("No sessions in database. Start 'monitor' first.")
    return str(all_sessions[0]["id"])


def doctor(args: argparse.Namespace) -> int:
    path = Path(args.bridge)
    if not path.is_file():
        print(f"Missing sensor bridge: {path}", file=sys.stderr)
        print("Download SurfaceSensors-win-x64 from the GitHub Actions build.", file=sys.stderr)
        return 2
    try:
        result = subprocess.run(
            [str(path.resolve()), "--once"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        print(f"Sensor bridge failed: {error}", file=sys.stderr)
        return 2
    if result.returncode != 0:
        print(f"Sensor bridge exited with {result.returncode}: {result.stderr}", file=sys.stderr)
        return 2
    try:
        packet = next(
            json.loads(line) for line in result.stdout.splitlines() if line.strip()
        )
        if packet.get("kind") != "sample" or not isinstance(packet.get("sensors"), list):
            raise ValueError("Invalid response from sensor bridge")
    except (StopIteration, ValueError, AttributeError) as error:
        print(f"Sensor bridge returned invalid JSON: {error}", file=sys.stderr)
        return 2

    grouped: dict[str, list[dict]] = {}
    for sensor in packet["sensors"]:
        if isinstance(sensor, dict):
            grouped.setdefault(str(sensor.get("hardware_type")), []).append(sensor)
    print(f"Bridge ready. {len(packet['sensors'])} active sensors:")
    for name, sensors in sorted(grouped.items()):
        print(f"  {name} ({len(sensors)}):")
        for sensor in sensors:
            if sensor.get("sensor_type") in {"Temperature", "Clock", "Power", "Load"}:
                print(
                    f"    {sensor.get('name')}: {sensor.get('value')} "
                    f"{sensor.get('unit', '')}"
                )
    for needed in ("Cpu", "GpuIntel"):
        if needed not in grouped:
            print(f"WARNING: {needed} sensors unavailable on this hardware/driver.")
    if args.presentmon:
        print(
            "PresentMon executable: "
            + ("found" if Path(args.presentmon).is_file() else "MISSING")
        )
    print("Throttle-reason flags are not inferred from frequency measurements.")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="surfaace-monitor",
        description="Read-only temperature, CPU/iGPU clock, power and Hollow Knight FPS logging"
    )
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)

    diag = commands.add_parser("doctor", help="Test hardware bridge and list real sensors")
    diag.add_argument("--bridge", default="bin/SurfaceSensors.exe")
    diag.add_argument("--presentmon")
    diag.set_defaults(handler=doctor)

    monitor = commands.add_parser("monitor", help="Continuously log hardware and game state")
    monitor.add_argument("--db", default="recordings/surface.db")
    monitor.add_argument("--bridge", default="bin/SurfaceSensors.exe")
    monitor.add_argument("--presentmon", help="Optional PresentMon standalone console executable")
    monitor.add_argument("--captures", default="recordings/presentmon")
    monitor.add_argument("--process", default="Hollow Knight.exe")
    monitor.add_argument("--tag", default="baseline", help="E.g. baseline, PL2-12W")
    monitor.add_argument("--interval", type=float, default=1.0, help="Sensor seconds, min 0.25")
    monitor.add_argument("--duration", type=float, help="Stop after N seconds; default until Ctrl+C")
    monitor.set_defaults(handler=_monitor)

    sessions = commands.add_parser("sessions", help="List recorded sessions")
    sessions.add_argument("--db", default="recordings/surface.db")
    sessions.set_defaults(handler=_sessions)

    export = commands.add_parser("export", help="Export sensor, frame and event CSVs")
    export.add_argument("--db", default="recordings/surface.db")
    export.add_argument("--session", help="Session ID; defaults to latest")
    export.add_argument("--out", default="reports/export")
    export.set_defaults(handler=_export)

    report = commands.add_parser("report", help="Create statistics and minute-binned PNG charts")
    report.add_argument("--db", default="recordings/surface.db")
    report.add_argument("--session", help="Session ID; defaults to latest")
    report.add_argument("--out", default="reports/latest")
    report.add_argument("--summary-only", action="store_true", help="No matplotlib needed")
    report.set_defaults(handler=_report)

    import_cmd = commands.add_parser("import-presentmon", help="Import an existing PresentMon CSV")
    import_cmd.add_argument("--db", default="recordings/surface.db")
    import_cmd.add_argument("--session", required=True)
    import_cmd.add_argument("--csv", required=True)
    import_cmd.add_argument("--pid", required=True, type=int)
    import_cmd.set_defaults(handler=_import)
    return parser


def _monitor(args: argparse.Namespace) -> int:
    interval_ms = round(args.interval * 1000)
    if not 250 <= interval_ms <= 60000:
        raise ValueError("--interval must be from 0.25 to 60 seconds")
    if not Path(args.bridge).is_file():
        raise ValueError(f"Sensor bridge not found: {args.bridge}. Run 'doctor' first.")
    if args.presentmon and not Path(args.presentmon).is_file():
        raise ValueError(f"PresentMon not found: {args.presentmon}")
    with Storage(args.db) as store:
        try:
            run_monitor(
                store, bridge_command=executable_command(
                    str(Path(args.bridge).resolve()), interval_ms
                ), interval_ms=interval_ms, label=args.tag,
                game_name=args.process, presentmon=(
                    str(Path(args.presentmon).resolve()) if args.presentmon else None
                ), capture_dir=Path(args.captures), duration=args.duration
            )
        except KeyboardInterrupt:
            print("Stopped by Ctrl+C.")
    return 0


def _sessions(args: argparse.Namespace) -> int:
    with Storage(args.db) as store:
        for item in store.sessions():
            status = item["ended_utc"] or "unfinished"
            print(
                f"{item['id']} | {item['label']} | {item['started_utc']} | "
                f"samples={item['sample_count']} captures={item['capture_count']} | {status}"
            )
    return 0


def _export(args: argparse.Namespace) -> int:
    with Storage(args.db) as store:
        files = store.export(_session_id(store, args.session), Path(args.out))
    for path in files:
        print(path)
    return 0


def _report(args: argparse.Namespace) -> int:
    with Storage(args.db) as store:
        sid = _session_id(store, args.session)
        if args.summary_only:
            directory = Path(args.out)
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / "summary.json"
            path.write_text(
                json.dumps(session_summary(store, sid), indent=2, ensure_ascii=False),
                encoding="utf-8"
            )
            paths = [path]
        else:
            paths = write_report(store, sid, Path(args.out))
    for path in paths:
        print(path)
    return 0


def _import(args: argparse.Namespace) -> int:
    csv_path = Path(args.csv)
    if not csv_path.is_file():
        raise ValueError(f"No such CSV: {csv_path}")
    with Storage(args.db) as store:
        sid = _session_id(store, args.session)
        capture_id = store.start_capture(sid, args.pid, csv_path.resolve())
        count, timestamped = import_csv(store, capture_id, csv_path)
        store.finish_capture(capture_id, 0)
        store.add_event(
            sid, "manual_presentmon_import",
            {"csv": str(csv_path), "rows": count, "timestamped": timestamped}
        )
    print(f"Imported {count} frames, {timestamped} with absolute timestamps.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        return int(args.handler(args))
    except (OSError, ValueError, ImportError) as error:
        LOG.error("%s", error)
        return 2


if __name__ == "__main__":
    sys.exit(main())
