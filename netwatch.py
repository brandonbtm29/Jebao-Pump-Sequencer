"""Keep track of when each pump drops off Wi-Fi and comes back.

The main window asks the Jebao cloud for the pump list every ``POLL_S`` seconds. Each
answer says which pumps are online. This module turns those answers into a history
(``went offline`` / ``back online`` events) and the numbers for the connection report:
drops in the last day and week, time offline, the longest outage, when each pump was
last seen, and how quickly the cloud answered.

The history is saved per computer in the data folder's ``logs/`` (next to the system
log), as ``connection-<computer>.jsonl``, one event per line, and old events are dropped
after the same number of days as the system log. No Qt here, so it can be tested on
its own.
"""

import json
import os
import time
from dataclasses import dataclass, field

POLL_S = 30  # how often the app asks the cloud which pumps are online
DAY = 24 * 3600.0


@dataclass
class PumpState:
    did: str
    name: str = ""
    mac: str = ""
    online: bool | None = None  # None until the first check
    since: float | None = None  # when it last changed (or was first seen)
    last_online: float | None = None  # the last check that found it online


@dataclass
class ConnectionLog:
    """Online/offline history of the pumps on this computer."""

    path: str = ""  # "" = keep it in memory only (tests)
    keep_days: int = 30
    clock: object = time.time
    pumps: dict = field(default_factory=dict)  # did -> PumpState
    events: list = field(default_factory=list)  # {"t", "did", "name", "event"}
    checks: list = field(default_factory=list)  # (t, ok, seconds) of recent cloud checks
    cloud_ok: bool | None = None
    cloud_problem: str = ""

    # --- saving -------------------------------------------------------------------------

    def load(self):
        """Read the saved history (and drop events older than ``keep_days``)."""
        if not self.path or not os.path.exists(self.path):
            return self
        cutoff = self.clock() - self.keep_days * DAY
        kept = []
        try:
            with open(self.path, encoding="utf-8") as f:
                for line in f:
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    if (
                        isinstance(ev, dict)
                        and ev.get("t", 0) >= cutoff
                        and ev.get("event") in ("offline", "online")
                    ):
                        kept.append(ev)
        except OSError:
            return self
        self.events = kept
        if len(kept) != self._saved_lines():
            self._rewrite()
        return self

    def _saved_lines(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                return sum(1 for _ in f)
        except OSError:
            return 0

    def _rewrite(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                for ev in self.events:
                    f.write(json.dumps(ev) + "\n")
            os.replace(tmp, self.path)
        except OSError:
            pass  # the history is a convenience; never let it stop the app

    def _append(self, ev):
        self.events.append(ev)
        if not self.path:
            return
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(ev) + "\n")
        except OSError:
            pass

    # --- what the main window reports ---------------------------------------------------

    def cloud_check(self, ok, seconds=None, problem=""):
        """One attempt to ask the cloud for the pump list: did it answer, and how fast."""
        now = self.clock()
        self.checks.append((now, bool(ok), seconds))
        self.checks = [c for c in self.checks if c[0] >= now - DAY][-5000:]
        changed = self.cloud_ok is not None and self.cloud_ok != bool(ok)
        first_failure = self.cloud_ok is None and not ok
        self.cloud_ok = bool(ok)
        self.cloud_problem = "" if ok else problem
        return changed or first_failure

    def update(self, devices):
        """Record the pump list from the cloud. Returns [(state, "offline"|"online", outage_s)]
        for each pump whose status changed since the last check (not for the first check)."""
        now = self.clock()
        changes = []
        for d in devices:
            did = d.get("did")
            if not did:
                continue
            online = bool(d.get("is_online", False))
            p = self.pumps.get(did)
            if p is None:
                p = self.pumps[did] = PumpState(did)
            p.name = d.get("name") or d.get("dev_alias") or p.name or did
            p.mac = d.get("mac") or p.mac
            if online:
                p.last_online = now
            if p.online is None:
                p.online, p.since = online, now
                continue
            if online != p.online:
                outage = now - p.since if online else 0.0
                p.online, p.since = online, now
                self._append(
                    {"t": now, "did": did, "name": p.name, "event": "online" if online else "offline"}
                )
                changes.append((p, "online" if online else "offline", outage))
        return changes

    # --- the report ---------------------------------------------------------------------

    def outages(self, did, since):
        """[(start, end)] of the pump's offline spells overlapping ``since`` → now (an ongoing
        one ends now)."""
        now = self.clock()
        spells = []
        start = None
        for ev in sorted((e for e in self.events if e["did"] == did), key=lambda e: e["t"]):
            if ev["event"] == "offline":
                start = ev["t"]
            elif ev["event"] == "online" and start is not None:
                spells.append((start, ev["t"]))
                start = None
        p = self.pumps.get(did)
        if p is not None and p.online is False:
            spells.append((start if start is not None else (p.since or now), now))
        return [(max(a, since), b) for a, b in spells if b >= since]

    def summary(self, did):
        """Numbers for one pump's row in the report."""
        now = self.clock()
        day = self.outages(did, now - DAY)
        week = self.outages(did, now - 7 * DAY)

        def drops(t0):
            return sum(1 for e in self.events if e["did"] == did and e["event"] == "offline" and e["t"] >= t0)

        p = self.pumps.get(did) or PumpState(did)
        return {
            "name": p.name,
            "mac": p.mac,
            "online": p.online,
            "since": p.since,
            "last_online": p.last_online,
            "drops_day": drops(now - DAY),
            "drops_week": drops(now - 7 * DAY),
            "offline_day_s": sum(b - a for a, b in day),
            "longest_week_s": max((b - a for a, b in week), default=0.0),
        }

    def cloud_summary(self):
        """How the app's own link to the cloud has been over the last day."""
        now = self.clock()
        day = [c for c in self.checks if c[0] >= now - DAY]
        times = sorted(s for _t, ok, s in day if ok and s is not None)
        return {
            "ok": self.cloud_ok,
            "problem": self.cloud_problem,
            "checks": len(day),
            "failed": sum(1 for _t, ok, _s in day if not ok),
            "median_s": times[len(times) // 2] if times else None,
            "slowest_s": times[-1] if times else None,
            "last": day[-1][0] if day else None,
        }


def history_path(log_folder, computer):
    return os.path.join(log_folder, f"connection-{computer}.jsonl")


def fmt_duration(seconds):
    """'45 s', '3 min', '1 h 05 min', '2 d 3 h'."""
    s = int(round(seconds or 0))
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min"
    if s < 86400:
        return f"{s // 3600} h {s % 3600 // 60:02d} min"
    return f"{s // 86400} d {s % 86400 // 3600} h"
