from __future__ import annotations

from pathlib import Path

from surfaace_monitor.presentmon import _frame_utc, import_csv
from surfaace_monitor.storage import Storage


def test_timestamp_handles_presentmon_nine_digit_precision():
    actual = _frame_utc("2026-10-22 10:20:30.123456789")
    assert actual is not None
    assert actual.endswith("+00:00")
    assert _frame_utc("NA") is None
    assert _frame_utc("not a date") is None


def test_import_presentmon_and_idempotency(tmp_path: Path):
    file = tmp_path / "frames.csv"
    file.write_text(
        "Application,ProcessID,CPUStartDateTime,MsBetweenPresents\n"
        "Hollow Knight.exe,123,2026-10-22 10:20:30.123456789,16.6667\n"
        "Hollow Knight.exe,123,2026-10-22 10:20:30.140000000,33.3333\n"
        "Hollow Knight.exe,123,NA,10\n"
        "Hollow Knight.exe,123,2026-10-22 10:20:30,NA\n"
        "Hollow Knight.exe,123,2026-10-22 10:20:30,0\n",
        encoding="utf-8"
    )
    with Storage(tmp_path / "data.db") as store:
        sid = store.start_session(
            label="baseline", process_name="Hollow Knight.exe",
            interval_ms=1000, bridge="fake", presentmon="PresentMon"
        )
        capture_id = store.start_capture(sid, 123, file)
        assert import_csv(store, capture_id, file) == (3, 2)
        assert import_csv(store, capture_id, file) == (0, 2)
        assert store.db.execute("SELECT COUNT(*) FROM frames").fetchone()[0] == 3
        fps = store.db.execute("SELECT fps FROM frames ORDER BY id LIMIT 1").fetchone()[0]
        assert 59 < fps < 61
        captured = store.db.execute(
            "SELECT imported_rows FROM captures WHERE id=?", (capture_id,)
        ).fetchone()[0]
        assert captured == 3
