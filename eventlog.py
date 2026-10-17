"""Keep the system log on disk, so it survives closing the app.

Each computer writes one file per day to ``logs/`` in the app's data folder (next to
the error log that crash_log.py keeps there):
``events-YYYY-MM-DD-<computer>.log``. The computer's name is part of the file name
because the data folder is shared through SyncThing, and two computers writing the
same file would make sync conflicts. When the app opens it shows this computer's
recent history again, and it deletes files older than the number of days set in
Settings (30 by default).

No Qt here, so it can be tested on its own.
"""

import datetime as _dt
import logging
import os
import re
import socket

LOG_DIR_NAME = "logs"
DEFAULT_KEEP_DAYS = 30
_FILE = re.compile(r"^events-(\d{4}-\d{2}-\d{2})-(.+)\.log$")


def computer_name():
    """A file-name-safe name for this computer."""
    name = socket.gethostname().split(".")[0] or "computer"
    return re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-") or "computer"


def log_dir(data_dir):
    return os.path.join(data_dir, LOG_DIR_NAME)


def file_for(folder, day, computer=None):
    return os.path.join(folder, f"events-{day.isoformat()}-{computer or computer_name()}.log")


class DailyFileHandler(logging.Handler):
    """Append each record to today's file for this computer."""

    def __init__(self, folder, computer=None, today=_dt.date.today):
        super().__init__()
        self.folder = folder
        self.computer = computer or computer_name()
        self._today = today
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))

    def emit(self, record):
        try:
            os.makedirs(self.folder, exist_ok=True)
            line = self.format(record).replace("\r", " ").replace("\n", " ⏎ ")
            with open(file_for(self.folder, self._today(), self.computer), "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:  # a log that can't be written must never stop the app
            self.handleError(record)


def _files(folder, computer=None):
    """[(date, path)] of this computer's log files, oldest first."""
    computer = computer or computer_name()
    out = []
    try:
        names = os.listdir(folder)
    except OSError:
        return out
    for name in names:
        m = _FILE.match(name)
        if not m or m.group(2) != computer:
            continue
        try:
            day = _dt.date.fromisoformat(m.group(1))
        except ValueError:
            continue
        out.append((day, os.path.join(folder, name)))
    return sorted(out)


def prune(folder, keep_days, computer=None, today=None):
    """Delete this computer's log files older than ``keep_days``. Returns how many went."""
    today = today or _dt.date.today()
    cutoff = today - _dt.timedelta(days=max(1, int(keep_days)) - 1)
    removed = 0
    for day, path in _files(folder, computer):
        if day < cutoff:
            try:
                os.remove(path)
                removed += 1
            except OSError:
                pass
    return removed


def load_recent(folder, keep_days, max_lines=3000, computer=None, today=None):
    """This computer's history for the last ``keep_days`` days, newest last.

    Returns [(date, line)] where line is "HH:MM:SS LEVEL message", at most ``max_lines``.
    """
    today = today or _dt.date.today()
    cutoff = today - _dt.timedelta(days=max(1, int(keep_days)) - 1)
    lines = []
    for day, path in _files(folder, computer):
        if day < cutoff:
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for raw in f:
                    raw = raw.rstrip("\n")
                    if raw:
                        lines.append((day, raw[11:] if raw[:10] == day.isoformat() else raw))
        except OSError:
            continue
    return lines[-max_lines:]


def format_history(entries, today=None):
    """Turn load_recent() output into display lines with a heading per day."""
    today = today or _dt.date.today()
    out = []
    current = None
    for day, line in entries:
        if day != current:
            current = day
            label = (
                "Today"
                if day == today
                else ("Yesterday" if day == today - _dt.timedelta(days=1) else day.strftime("%a %d %b %Y"))
            )
            out.append(f"── {label} ──")
        time, _, rest = line.partition(" ")
        level, _, message = rest.partition(" ")
        prefix = "" if level == "INFO" else f"{level}: "
        out.append(f"[{time}] {prefix}{message}")
    return out
