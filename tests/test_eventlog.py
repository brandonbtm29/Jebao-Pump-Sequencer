"""Tests for eventlog.py: daily files, pruning and reloading history."""

import datetime as dt
import logging

import eventlog

D = dt.date


def make_logger(handler):
    log = logging.getLogger(f"test-eventlog-{id(handler)}")
    log.setLevel(logging.INFO)
    log.propagate = False
    log.addHandler(handler)
    return log


def test_writes_one_file_per_day_and_computer(tmp_path):
    day = [D(2026, 10, 16)]
    log = make_logger(eventlog.DailyFileHandler(str(tmp_path), computer="LAB-PC", today=lambda: day[0]))
    log.info("first")
    day[0] = D(2026, 10, 17)
    log.warning("second\nline")
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["events-2026-10-16-LAB-PC.log", "events-2026-10-17-LAB-PC.log"]
    text = (tmp_path / "events-2026-10-17-LAB-PC.log").read_text(encoding="utf-8")
    assert "WARNING second ⏎ line" in text and text.count("\n") == 1


def test_history_reloads_with_day_headings(tmp_path):
    (tmp_path / "events-2026-10-16-LAB-PC.log").write_text(
        "2026-10-16 17:58:41 INFO Sequence done\n", encoding="utf-8"
    )
    (tmp_path / "events-2026-10-17-LAB-PC.log").write_text(
        "2026-10-17 09:00:01 INFO Login successful\n2026-10-17 09:00:05 ERROR Control command failed\n",
        encoding="utf-8",
    )
    (tmp_path / "events-2026-10-17-OTHER-MAC.log").write_text(
        "2026-10-17 09:00:00 INFO not mine\n", encoding="utf-8"
    )
    entries = eventlog.load_recent(str(tmp_path), 30, computer="LAB-PC", today=D(2026, 10, 17))
    lines = eventlog.format_history(entries, today=D(2026, 10, 17))
    assert lines == [
        "── Yesterday ──",
        "[17:58:41] Sequence done",
        "── Today ──",
        "[09:00:01] Login successful",
        "[09:00:05] ERROR: Control command failed",
    ]


def test_prune_keeps_only_the_last_n_days(tmp_path):
    for day in ("2026-09-01", "2026-09-17", "2026-09-18", "2026-10-17"):
        (tmp_path / f"events-{day}-LAB-PC.log").write_text("x\n")
    (tmp_path / "events-2026-09-01-OTHER-MAC.log").write_text("x\n")  # another computer's: left alone
    (tmp_path / "notes.txt").write_text("keep me")
    removed = eventlog.prune(str(tmp_path), 30, computer="LAB-PC", today=D(2026, 10, 17))
    assert removed == 2
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "events-2026-09-01-OTHER-MAC.log",
        "events-2026-09-18-LAB-PC.log",
        "events-2026-10-17-LAB-PC.log",
        "notes.txt",
    ]


def test_history_is_capped(tmp_path):
    lines = "".join(f"2026-10-17 09:00:{i % 60:02d} INFO line {i}\n" for i in range(50))
    (tmp_path / "events-2026-10-17-LAB-PC.log").write_text(lines)
    entries = eventlog.load_recent(str(tmp_path), 30, max_lines=10, computer="LAB-PC", today=D(2026, 10, 17))
    assert len(entries) == 10 and entries[-1][1].endswith("line 49")


def test_missing_folder_is_fine(tmp_path):
    assert eventlog.load_recent(str(tmp_path / "nope"), 30) == []
    assert eventlog.prune(str(tmp_path / "nope"), 30) == 0


def test_computer_name_is_file_safe():
    assert all(c.isalnum() or c in "_-" for c in eventlog.computer_name())
