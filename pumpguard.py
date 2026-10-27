"""The pump guard: make sure no pump head is left running, and none runs too long.

Every ON and OFF the app sends goes through :meth:`PumpGuard.command`, so the guard
always knows what the app *meant* each head to be doing. It then makes sure that is
what really happens:

- **OFF until it works.** An OFF that fails (cloud error, pump off Wi-Fi) puts the
  head on the *must stop* list, and the guard keeps re-sending OFF, with growing
  gaps, until it gets through. An ON that fails might still have reached the pump
  (a reply that timed out), so it is followed by an OFF in the same way.
- **What the pumps say.** Every check (30 s) the guard reads each online pump's own
  report of its channels from the cloud. A head that reports ON while the app has it
  OFF (a lost OFF, a pump that reconnected, the phone app, or something left from
  before the app started) is turned off, and logged.
- **Forgotten heads.** A head the app turned on, but that no dose, sequence step or
  calibration is running any more, is turned off.
- **On-time budget.** Each head may run at most ``on_s`` seconds in any ``window_s``
  seconds (30 min in 60 min by default; per head it can be different or off).
  Doses pause to let the head cool and carry on when the budget allows, using
  :meth:`over_limit` and :meth:`resume_at`. Anything else over the limit is turned off.

No Qt here: sending, reading and logging are passed in, so it can be tested on its own.
"""

import asyncio
import contextlib
import logging
import re
import time
from dataclasses import dataclass

log = logging.getLogger("JebaoGUI")

CHECK_S = 30  # how often the pumps' own reports are read (with the online check)
ORPHAN_GRACE_S = 20  # an ON with nothing running it is left alone this long
REPORT_GRACE_S = 120  # a pump report older than our last command is ignored this long
FOLLOW_UP_S = 12  # after a cancelled or failed ON, OFF is sent again this much later
CONFIRM_GRACE_S = 45  # an OFF not yet confirmed by the pump's report shows as pending after this
CLOCK_SLACK_S = 60  # the cloud's report times and this PC's clock may differ by this much
KEEP_HISTORY_S = 25 * 3600  # on-time history kept (the longest window in Settings is 24 h)
RETRY_DELAYS = (1, 2, 5, 10, 20, 30)  # seconds between OFF retries (then every 30 s)
_CH = re.compile(r"^channe(\d)$")


@dataclass
class Limit:
    on_s: float = 30 * 60
    window_s: float = 60 * 60
    enabled: bool = True

    def text(self):
        if not self.enabled:
            return "no limit"
        return f"{self.on_s / 60:g} min in any {self.window_s / 60:g} min"


class OnTimeBudget:
    """When each head was on, to work out how long it ran in the last ``window``."""

    def __init__(self):
        self.intervals = {}  # key -> [[start, end or None], ...]

    def start(self, key, t):
        spans = self.intervals.setdefault(key, [])
        if not spans or spans[-1][1] is not None:
            spans.append([t, None])

    def stop(self, key, t):
        spans = self.intervals.get(key)
        if spans and spans[-1][1] is None:
            spans[-1][1] = max(t, spans[-1][0])

    def is_on(self, key):
        spans = self.intervals.get(key)
        return bool(spans) and spans[-1][1] is None

    def used(self, key, now, window, off_from=None):
        """Seconds on during [now - window, now]. ``off_from``: count a running head as
        stopped at that time (to look ahead while it rests)."""
        t0 = now - window
        total = 0.0
        for a, b in self.intervals.get(key, []):
            end = b if b is not None else (off_from if off_from is not None else now)
            end = min(end, now)
            if end > t0:
                total += end - max(a, t0)
        return max(0.0, total)

    def prune(self, now, keep):
        for key, spans in self.intervals.items():
            self.intervals[key] = [s for s in spans if s[1] is None or s[1] >= now - keep]

    def resume_at(self, key, now, limit, need):
        """Earliest time (≥ now) when, resting from now on, at least ``need`` seconds of
        budget are free again."""
        target = limit.on_s - need
        if self.used(key, now, limit.window_s, off_from=now) <= target:
            return now
        lo, hi = now, now + limit.window_s
        for _ in range(40):  # used() only goes down while resting: bisect
            mid = (lo + hi) / 2
            if self.used(key, mid, limit.window_s, off_from=now) <= target:
                hi = mid
            else:
                lo = mid
        return hi


class PumpGuard:
    """See the module docstring. ``send(did, attrs)`` and ``read(did)`` are coroutines:
    ``send`` returns True when the cloud took the command, ``read`` returns the pump's
    latest report ``{"attr": {...}, "updated_at": epoch s}`` or None."""

    def __init__(self, send, read=None, label=None, limit_for=None, clock=time.time, sleep=asyncio.sleep):
        self.send = send
        self.read = read
        self.label = label or (lambda did, ch: f"{did} · CH{ch}")
        self.limit_for = limit_for or (lambda key: Limit())
        self.clock = clock
        self.sleep = sleep
        self.budget = OnTimeBudget()
        self.app_on = {}  # key -> True/False: what the app last told the head to do
        self.last_cmd = {}  # key -> when the app last sent the head a command
        self.must_off = {}  # key -> why it must be stopped (OFF not through yet)
        self.unconfirmed = {}  # key -> (when, why): OFF accepted while its pump was offline
        self.online = {}  # did -> online at the last check
        self.hold_until = 0.0  # no ON before this (just after Emergency Stop)
        self.on_stopped = None  # callback(key) once a must-stop head is confirmed off
        self.on_over_limit = None  # callback(key) for a head over its limit
        self._locks = {}  # key -> asyncio.Lock: one command at a time per head
        self._in_flight = {}  # key -> ON/OFF being sent right now
        self._follow_up = {}  # key -> send OFF once more this many s after it went through
        self._retrying = {}  # key -> task re-sending OFF
        self._tasks = set()  # other background commands (kept so they aren't lost)
        self._reported = {}  # key -> what we last logged about a report (avoid repeats)
        self.read_ok = None  # False once reading reports failed (shown once)

    # --- every command goes through here --------------------------------------------------

    def _lock(self, key):
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        return lock

    def hold(self, seconds):
        """Refuse every ON for a while (Emergency Stop)."""
        self.hold_until = max(self.hold_until, self.clock() + seconds)

    async def command(self, did, attrs):
        """Send ``attrs`` (e.g. {"channe2": True}) and keep track of what it means.

        Commands to the same head never overlap (one lock per head), so an OFF can't
        overtake an ON or the other way round."""
        heads = [(int(m.group(1)), bool(v)) for k, v in attrs.items() if (m := _CH.match(k))]
        keys = sorted({(did, ch) for ch, _on in heads})
        async with contextlib.AsyncExitStack() as stack:
            for key in keys:
                await stack.enter_async_context(self._lock(key))
            if any(on for _ch, on in heads) and self.clock() < self.hold_until:
                log.warning(f"{did}: ON refused right after Emergency Stop.")
                return False
            now = self.clock()
            for ch, on in heads:
                self.last_cmd[(did, ch)] = now
                self._in_flight[(did, ch)] = on
            try:
                ok = await self.send(did, attrs)
            except asyncio.CancelledError:
                # Cancelled while waiting for the cloud: the command may still go through.
                self._after(did, heads, False)
                raise
            except Exception as e:  # noqa: BLE001 - treat like a failed command
                log.error(f"Command to {did} failed: {e}")
                ok = False
            finally:
                for key in keys:
                    self._in_flight.pop(key, None)
            self._after(did, heads, ok)
        return ok

    def _after(self, did, heads, ok):
        now = self.clock()
        for ch, on in heads:
            key = (did, ch)
            if ok:
                was_on = self.app_on.get(key) or self.budget.is_on(key) or key in self.must_off
                self.app_on[key] = on
                self.must_off.pop(key, None)
                self._reported.pop(key, None)
                if on:
                    self.unconfirmed.pop(key, None)
                    self.budget.start(key, now)
                else:
                    self.budget.stop(key, now)
                    if was_on:  # (an OFF to a head that was off anyway needs no confirming)
                        self._sent_off(key, now)
            else:
                # A failed OFF: keep trying. A failed ON may still have reached the pump
                # (the reply can time out after the cloud passed it on): stop it to be
                # sure, and once more a little later in case the ON arrives late.
                self.app_on[key] = False
                if on:
                    self.budget.start(key, now)  # counts until confirmed off
                why = (
                    "OFF didn't go through" if not on else "ON may have reached it although the reply failed"
                )
                self.stop(did, ch, why, again_after=FOLLOW_UP_S if on else 0)

    def _sent_off(self, key, now):
        """The cloud took an OFF for a head that was on. The cloud says yes even to a pump
        that has just dropped off Wi-Fi, so the OFF only counts once the pump's own report
        shows the head off (checked every 30 s). If reports can't be read, only pumps
        already known to be offline are kept waiting."""
        offline = not self.online.get(key[0], True)
        if offline or self.read_ok is not False:
            why = (
                "pump offline: OFF sent, waiting for it to reconnect"
                if offline
                else "waiting for the pump to confirm OFF"
            )
            self.unconfirmed[key] = (now, why)
        else:
            self.unconfirmed.pop(key, None)

    def stop(self, did, ch, why, quiet=False, again_after=0):
        """Put a head on the must-stop list and keep sending OFF until it works."""
        key = (did, ch)
        self.app_on[key] = False
        if key not in self.must_off and not quiet:
            log.warning(f"{self.label(did, ch)}: making sure it is off ({why}).")
        self.must_off[key] = why
        if again_after:
            self._follow_up[key] = max(self._follow_up.get(key, 0), again_after)
        task = self._retrying.get(key)
        if task is None or task.done():
            try:
                asyncio.get_running_loop()
            except RuntimeError:  # no event loop running (tests that only check bookkeeping)
                return
            self._retrying[key] = asyncio.ensure_future(self._keep_stopping(key))

    async def _keep_stopping(self, key):
        did, ch = key
        attempt = 0
        warned = False
        while True:
            async with self._lock(key):
                if key not in self.must_off:  # turned on on purpose meanwhile
                    return
                try:
                    ok = await self.send(did, {f"channe{ch}": False})
                except Exception:  # noqa: BLE001
                    ok = False
                if ok:
                    now = self.clock()
                    self.must_off.pop(key, None)
                    self.app_on[key] = False
                    self.last_cmd[key] = now
                    self.budget.stop(key, now)
                    self._sent_off(key, now)
                    if self.unconfirmed.get(key, (0, ""))[1].startswith("pump offline"):
                        log.warning(
                            f"{self.label(did, ch)}: OFF sent, but its pump is offline; waiting to confirm."
                        )
                    else:
                        log.info(f"{self.label(did, ch)}: stopped.")
                        if self.on_stopped:
                            self.on_stopped(key)
                    break
            if attempt == 3 and not warned:
                warned = True
                log.error(
                    f"{self.label(did, ch)} STILL WON'T STOP (pump offline or cloud unreachable). "
                    "The app keeps trying; if you are near it, unplug that pump."
                )
            await self.sleep(RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)])
            attempt += 1
        # A late-arriving ON (after a cancelled or timed-out ON) would undo that OFF: repeat it.
        delay = self._follow_up.pop(key, 0)
        if delay:
            sent_at = self.last_cmd.get(key)
            await self.sleep(delay)
            async with self._lock(key):
                if self.app_on.get(key) is False and self.last_cmd.get(key) == sent_at:
                    try:
                        await self.send(did, {f"channe{ch}": False})
                    except Exception as e:  # noqa: BLE001 - the report check is the backstop
                        log.debug(f"Follow-up OFF to {self.label(did, ch)} failed: {e}")

    # --- the on-time budget ---------------------------------------------------------------

    def over_limit(self, key):
        limit = self.limit_for(key)
        if not limit.enabled:
            return False
        return self.budget.used(key, self.clock(), limit.window_s) >= limit.on_s

    def resume_at(self, key, need=None):
        """When a resting head may run again (with ``need`` s of budget, by default a
        sixth of the limit, at least a minute, so it doesn't flick on and off)."""
        limit = self.limit_for(key)
        if not limit.enabled:
            return self.clock()
        need = need if need is not None else min(limit.on_s, max(60.0, limit.on_s / 6))
        return self.budget.resume_at(key, self.clock(), limit, need)

    def budget_left(self, key):
        limit = self.limit_for(key)
        if not limit.enabled:
            return float("inf")
        return max(0.0, limit.on_s - self.budget.used(key, self.clock(), limit.window_s))

    # --- the regular check ------------------------------------------------------------------

    async def check(self, devices, running):
        """``devices``: the pump list (with ``is_online``). ``running``: {key: source} of
        the heads a dose, sequence or calibration is running right now."""
        now = self.clock()
        self.budget.prune(now, KEEP_HISTORY_S)
        online = {d.get("did"): bool(d.get("is_online", False)) for d in devices}
        self.online = dict(online)
        if devices:  # pumps no longer on the account can't be checked: stop waiting for them
            for key in [k for k in (*self.must_off, *self.unconfirmed) if k[0] not in online]:
                self.forget(key, "its pump is no longer on the account")
        # 1. heads the app turned on that nothing is running any more
        for key, on in list(self.app_on.items()):
            if on and key not in running and now - self.last_cmd.get(key, 0) > ORPHAN_GRACE_S:
                self.stop(*key, "it was on but nothing in the app was running it")
        # 2. over the on-time limit and not resting by itself (e.g. calibration)
        for key, on in list(self.app_on.items()):
            if on and self.over_limit(key) and running.get(key) not in ("manual", "sequence"):
                log.warning(f"{self.label(*key)} reached its on-time limit ({self.limit_for(key).text()}).")
                if self.on_over_limit:
                    self.on_over_limit(key)
                self.stop(*key, "on-time limit reached")
        # 3. must-stop heads whose retry gave up or never started
        for key in list(self.must_off):
            task = self._retrying.get(key)
            if (task is None or task.done()) and online.get(key[0], False):
                self.stop(*key, self.must_off[key])
        # 4. what the pumps themselves report
        if self.read is None:
            return
        for did, is_on in online.items():
            if not is_on:
                continue
            try:
                report = await self.read(did)
            except Exception:  # noqa: BLE001
                report = None
            if not report or not isinstance(report.get("attr"), dict):
                if self.read_ok is not False:
                    log.warning("Couldn't read the pumps' own reports; checking by commands only.")
                self.read_ok = False
                for key, (_t, why) in list(self.unconfirmed.items()):
                    if key[0] == did and not why.startswith("pump offline"):
                        self.unconfirmed.pop(key, None)  # can't be confirmed: trust the OFF
                continue
            self.read_ok = True
            updated = report.get("updated_at") or 0
            for name, value in report["attr"].items():
                m = _CH.match(name)
                if not m or int(m.group(1)) > 4:
                    continue
                key = (did, int(m.group(1)))
                if key in self._in_flight:
                    continue  # a command to it is on its way: judge it next time
                reported_on = bool(value)
                sent = self.last_cmd.get(key, 0)
                fresh = not updated or updated >= sent
                if not reported_on:
                    if self._reported.get(key) == "on":
                        self._reported.pop(key, None)
                    if key in self.unconfirmed:
                        t_off, why = self.unconfirmed[key]
                        if not updated or updated >= t_off - CLOCK_SLACK_S:
                            self.unconfirmed.pop(key, None)
                            if why.startswith("pump offline"):
                                log.info(f"{self.label(*key)}: its pump reconnected and reports it off.")
                            if self.on_stopped:
                                self.on_stopped(key)
                    if (
                        self.app_on.get(key)
                        and key in running
                        and updated
                        and updated >= sent + 3
                        and now - sent > 15
                        and self._reported.get(key) != "off"
                    ):
                        # The app has it on for a dose, but the pump says it is off.
                        self._reported[key] = "off"
                        log.warning(f"{self.label(*key)} reports OFF during its dose; sending ON again.")
                        task = asyncio.ensure_future(self.command(did, {f"channe{key[1]}": True}))
                        self._tasks.add(task)
                        task.add_done_callback(self._tasks.discard)
                    continue
                if self.app_on.get(key):
                    continue  # meant to be on (a paused or resting dose is meant to be off)
                if not fresh and now - sent < REPORT_GRACE_S:
                    continue  # that report is from before our last command
                if key in self.must_off:
                    continue  # already being stopped
                self.budget.start(key, now)
                first = self._reported.get(key) != "on"
                if first:
                    self._reported[key] = "on"
                    log.warning(
                        f"{self.label(*key)} is running but the app hadn't started it (a lost OFF, a pump "
                        "that reconnected, the phone app, or left from before). Turning it off."
                    )
                self.stop(*key, "running without the app", quiet=not first)

    def close(self):
        """Stop the background retries (the app is closing)."""
        for task in [*self._retrying.values(), *self._tasks]:
            if not task.done():
                task.cancel()
        self._retrying.clear()
        self._tasks.clear()

    def forget(self, key, why):
        """Stop trying to turn a head off (someone switched it off by hand, or its pump is gone)."""
        if key in self.must_off or key in self.unconfirmed:
            log.warning(f"{self.label(*key)}: no longer checked ({why}).")
        self.must_off.pop(key, None)
        self.unconfirmed.pop(key, None)
        task = self._retrying.pop(key, None)
        if task is not None and not task.done():
            task.cancel()

    def pending(self, all_=False):
        """Heads still waiting for a confirmed OFF: {key: why}. An OFF that went through
        normally gets ``CONFIRM_GRACE_S`` for the pump to confirm it before it shows here."""
        now = self.clock()
        out = {
            key: why
            for key, (t, why) in self.unconfirmed.items()
            if all_ or why.startswith("pump offline") or now - t > CONFIRM_GRACE_S
        }
        out.update(self.must_off)
        return out
