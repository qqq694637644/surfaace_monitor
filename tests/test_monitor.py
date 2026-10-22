from __future__ import annotations

import sys
from pathlib import Path

from surfaace_monitor.monitor import run_monitor
from surfaace_monitor.storage import Storage

FAKE_BRIDGE = """
import datetime
import json
import time
n = 0
while True:
    n += 1
    stamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
    print(json.dumps({
        "kind": "sample", "timestamp_utc": stamp, "sequence": n,
        "sensors": [{
            "id": "/cpu/temp", "hardware_id": "/cpu", "hardware_name": "Fake CPU",
            "hardware_type": "Cpu", "name": "CPU Package",
            "sensor_type": "Temperature", "unit": "C", "value": 70 + n % 5
        }]
    }), flush=True)
    time.sleep(0.10)
"""


def test_long_running_main_loop_with_fake_bridge(tmp_path: Path):
    with Storage(tmp_path / "sample.db") as store:
        sid = run_monitor(
            store, bridge_command=[sys.executable, "-u", "-c", FAKE_BRIDGE],
            interval_ms=250, duration=1.5, capture_dir=tmp_path / "captures",
            game_finder=lambda _: None, status_interval=60
        )
        count = store.db.execute(
            "SELECT COUNT(*) FROM samples WHERE session_id=?", (sid,)
        ).fetchone()[0]
        assert count > 2
        assert store.session(sid)["ended_utc"] is not None
        events = {
            row[0] for row in store.db.execute(
                "SELECT kind FROM events WHERE session_id=?", (sid,)
            )
        }
        assert "bridge_connected" in events
