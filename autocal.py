"""Automatic calibration of pump heads with a balance.

Every outlet drains into one beaker on the balance, and the heads run one after another.
For each head:

1. **Settle**: wait ``settle_s`` with every pump still, then average the last couple of
   seconds of readings: the starting mass.
2. **Run**: turn the head on and keep it on until about ``volume_ml`` has arrived (going
   by the balance, so a head that has never been calibrated works too), or until
   ``max_run_s``. The on-time is measured the same way doses are timed: from the moment
   the pump confirms ON to the moment OFF is sent.
3. **Settle** again, then average a couple of seconds of readings: the end mass.

Two flow rates come out of it:

- **Δmass rate** = (end mass − starting mass) ÷ density ÷ on-time. This is what a dose of
  that length really delivers, so it is the rate that gets saved.
- **Slope rate** = a straight line fitted to mass against time while the pump runs,
  ignoring the first and last ``ignore_s`` seconds (start-up, drips).

The head passes when the two agree within ``tolerance_pct`` and the line fits well
(R² ≥ ``min_r2``). Otherwise it is redone, up to ``retries`` more times; after that you
decide (Accept or Redo). No Qt here: the pump and the balance are passed in, so the
whole routine can be tested with a pretend balance and a fast clock.
"""

import asyncio
import csv
import logging
import os
import statistics
import time
from dataclasses import dataclass, field

log = logging.getLogger("JebaoGUI.autocal")

LIQUIDS = {  # name -> density (g/mL) near room temperature
    "Water": 0.997,
    "Ethanol": 0.789,
    "Methanol": 0.792,
    "Isopropanol": 0.786,
}
AVERAGE_S = 2.0  # seconds of readings averaged for the starting and end mass
NO_FLOW_AFTER_S = 20.0  # if almost nothing has arrived by then, stop and say so
NO_FLOW_G = 0.05


@dataclass
class Settings:
    volume_ml: float = 10.0
    density: float = 0.997
    settle_s: float = 5.0
    ignore_s: float = 3.0
    tolerance_pct: float = 2.0
    min_r2: float = 0.99
    retries: int = 2
    max_run_s: float = 600.0
    auto_save: bool = True

    def min_run_s(self):
        """Long enough to leave a useful stretch for the fitted line."""
        return 2 * self.ignore_s + 5.0

    def to_dict(self):
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, data):
        s = cls()
        for k, v in (data or {}).items():
            if hasattr(s, k):
                try:
                    setattr(s, k, type(getattr(s, k))(v))
                except (TypeError, ValueError):
                    pass
        return s


def fit_line(ts, ys):
    """Least-squares line through (t, y). Returns (slope, intercept, r2)."""
    n = len(ts)
    if n < 2:
        raise ValueError("Need at least two points.")
    mt, my = sum(ts) / n, sum(ys) / n
    sxx = sum((t - mt) ** 2 for t in ts)
    if sxx == 0:
        raise ValueError("All points have the same time.")
    sxy = sum((t - mt) * (y - my) for t, y in zip(ts, ys, strict=True))
    slope = sxy / sxx
    intercept = my - slope * mt
    syy = sum((y - my) ** 2 for y in ys)
    if syy == 0:
        return slope, intercept, 1.0 if slope == 0 else 0.0
    ss_res = sum((y - (intercept + slope * t)) ** 2 for t, y in zip(ts, ys, strict=True))
    return slope, intercept, max(0.0, 1.0 - ss_res / syy)


def mean_between(samples, t0, t1):
    vals = [g for t, g, _s in samples if t0 <= t <= t1]
    return (statistics.fmean(vals), len(vals)) if vals else (None, 0)


@dataclass
class Attempt:
    """One run of one head."""

    number: int
    t_on: float = 0.0
    t_off: float = 0.0
    start_g: float = 0.0
    end_g: float = 0.0
    slope_rate: float | None = None  # mL/min
    dmass_rate: float | None = None  # mL/min
    diff_pct: float | None = None
    r2: float | None = None
    passed: bool = False
    problem: str = ""
    can_retry: bool = False  # a tolerance or R² miss (worth another try), not a broken setup
    samples: list = field(default_factory=list)  # (t, g, stable) during this attempt

    @property
    def on_time(self):
        return self.t_off - self.t_on

    @property
    def volume_ml(self):
        return None if self.dmass_rate is None else self.dmass_rate * self.on_time / 60.0


def evaluate(attempt, settings):
    """Work out both rates and whether they agree. Fills in ``attempt``; returns it."""
    s = settings
    on = attempt.on_time
    if on <= 0:
        attempt.problem = "The pump didn't run."
        return attempt
    delivered_g = attempt.end_g - attempt.start_g
    attempt.dmass_rate = delivered_g / s.density / on * 60.0
    window = [
        (t, g)
        for t, g, _st in attempt.samples
        if attempt.t_on + s.ignore_s <= t <= attempt.t_off - s.ignore_s
    ]
    if len(window) < 5 or window[-1][0] - window[0][0] < 2.0:
        attempt.problem = "Too few balance readings while the pump ran to fit a line. Run longer."
        return attempt
    slope, _b, r2 = fit_line([t for t, _ in window], [g for _, g in window])
    attempt.slope_rate = slope / s.density * 60.0
    attempt.r2 = r2
    if attempt.dmass_rate <= 0:
        attempt.problem = "The mass didn't go up."
        return attempt
    attempt.diff_pct = abs(attempt.slope_rate - attempt.dmass_rate) / attempt.dmass_rate * 100.0
    problems = []
    if attempt.diff_pct > s.tolerance_pct:
        problems.append(f"rates {attempt.diff_pct:.1f} % apart (limit {s.tolerance_pct:g} %)")
    if r2 < s.min_r2:
        problems.append(f"R² {r2:.4f} below {s.min_r2:g}")
    attempt.problem = "; ".join(problems)
    attempt.passed = not problems
    attempt.can_retry = bool(problems)
    return attempt


# Head outcomes
WAITING, RUNNING, PASSED, SAVED, FAILED, ACCEPTED, SKIPPED, STOPPED = (
    "waiting",
    "running",
    "passed",
    "saved",
    "failed",
    "accepted",
    "skipped",
    "stopped",
)


@dataclass
class HeadResult:
    did: str
    ch: int
    label: str
    old_rate: float | None = None
    status: str = WAITING
    attempts: list = field(default_factory=list)
    note: str = ""

    @property
    def key(self):
        return (self.did, self.ch)

    @property
    def last(self):
        return self.attempts[-1] if self.attempts else None

    @property
    def best(self):
        """The attempt to use: the passing one, else the one with the closest rates."""
        done = [a for a in self.attempts if a.dmass_rate is not None]
        if not done:
            return None
        passing = [a for a in done if a.passed]
        if passing:
            return passing[-1]
        return min(done, key=lambda a: a.diff_pct if a.diff_pct is not None else float("inf"))

    @property
    def rate(self):
        """The rate that would be saved (Δmass rate of the best attempt)."""
        b = self.best
        return b.dmass_rate if b else None


class Stopped(Exception):
    pass


class HeadProblem(Exception):
    """Something wrong with this head only (no flow): fail it and go on to the next."""


class AutoCalibrator:
    """Runs the heads one after another. Look at ``results``, ``current`` and ``phase`` to
    show progress; call ``stop()`` to end early (the running head is turned off first).

    ``set_head(did, ch, on)`` must be a coroutine returning True when the pump took the
    command. ``save(did, ch, rate)`` stores a rate. ``is_busy(did, ch)`` (optional) says a
    head is in use by a dose, so it is skipped. ``clock``/``sleep`` can be replaced in tests.
    """

    def __init__(
        self, balance, set_head, settings, save=None, clock=time.monotonic, sleep=asyncio.sleep, is_busy=None
    ):
        self.balance = balance
        self.set_head = set_head
        self.is_busy = is_busy  # (did, ch) -> True if the head is busy with something else
        self.settings = settings
        self.save = save
        self.clock = clock
        self.sleep = sleep
        self.results = []
        self.current = None  # HeadResult being worked on
        self.attempt = None  # its Attempt in progress
        self.phase = ""  # "settling", "running", "settling after", ""
        self.phase_until = None  # when the current wait ends (None while running)
        self.started = None
        self.finished = False
        self.pause_requested = False
        self.paused = False
        self._stop = False
        self._resume = asyncio.Event()
        self._resume.set()

    # --- controls ---

    def stop(self):
        self._stop = True
        self._resume.set()

    def pause_after_head(self, on=True):
        """Stop before the next head until resumed (the current head finishes)."""
        self.pause_requested = on
        if on:
            self._resume.clear()
        else:
            self._resume.set()

    @property
    def stopping(self):
        return self._stop

    def add(self, did, ch, label, old_rate=None):
        r = HeadResult(did, ch, label, old_rate)
        self.results.append(r)
        return r

    def requeue(self, result):
        """Redo a head (after a run, or while running: it goes to the end of the list)."""
        result.status = WAITING
        result.attempts = []
        result.note = ""
        if result in self.results:
            self.results.remove(result)
        self.results.append(result)

    # --- the run ---

    async def run(self):
        """Calibrate every waiting head. Can be called again after a Redo."""
        self.started = self.clock()
        self.finished = False
        self._stop = False
        self.pause_requested = self.paused = False  # a pause left over from the last run
        self._resume.set()
        try:
            while True:
                nxt = next((r for r in self.results if r.status == WAITING), None)
                if nxt is None or self._stop:
                    break
                if self.pause_requested:
                    self.paused = True
                    await self._resume.wait()
                    self.paused = False
                    if self._stop:
                        break
                if self.is_busy and self.is_busy(nxt.did, nxt.ch):
                    nxt.status, nxt.note = FAILED, "Busy with a dose when its turn came. Redo it later."
                    log.warning("Calibration of %s skipped: %s", nxt.label, nxt.note)
                    continue
                await self._calibrate(nxt)
        finally:
            for r in self.results:
                if r.status == RUNNING:  # interrupted by something unexpected
                    r.status, r.note = (STOPPED, "Stopped.") if self._stop else (FAILED, "Interrupted.")
                elif r.status == WAITING and self._stop:
                    r.status = STOPPED
            self.current = self.attempt = None
            self.phase, self.phase_until = "", None
            self.finished = True

    async def _wait(self, seconds, phase):
        self.phase = phase
        end = self.clock() + seconds
        self.phase_until = end
        while self.clock() < end:
            self._check()
            await self.sleep(min(0.1, end - self.clock()))

    def _check(self):
        if self._stop:
            raise Stopped()
        if not self.balance.fresh(3.0):  # a hiccup is fine; 3 s without a reading is not
            raise RuntimeError(
                self.balance.error or "The balance stopped sending readings. Check its cable and settings."
            )

    async def _calibrate(self, result):
        s = self.settings
        self.current = result
        result.status = RUNNING
        tries = 1 + max(0, int(s.retries))
        try:
            for n in range(1, tries + 1):
                attempt = Attempt(number=n)
                result.attempts.append(attempt)
                self.attempt = attempt
                await self._one_run(result, attempt)
                evaluate(attempt, s)
                self._log_attempt(result, attempt, tries)
                if not attempt.can_retry:
                    break
            best = result.best
            if best is not None and best.passed:
                if s.auto_save and self.save:
                    try:
                        self.save(result.did, result.ch, best.dmass_rate)
                        result.status = SAVED
                    except Exception as e:  # noqa: BLE001 - e.g. the settings file can't be written
                        result.status = PASSED
                        result.note = f"Passed, but saving failed: {e}"
                        log.error("Couldn't save the rate for %s: %s", result.label, e)
                else:
                    result.status = PASSED
            else:
                result.status = FAILED
                result.note = result.last.problem if result.last else ""
        except Stopped:
            result.status = STOPPED
            result.note = "Stopped."
        except HeadProblem as e:
            result.status = FAILED
            result.note = str(e)
            log.error("Calibration of %s: %s", result.label, e)
        except RuntimeError as e:
            result.status = FAILED
            result.note = str(e)
            log.error("Calibration of %s stopped: %s", result.label, e)
            self._stop = True  # a balance or pump problem: don't go on to the next head
        finally:
            self.attempt = None

    async def _one_run(self, result, attempt):
        s = self.settings
        t_start = self.clock()
        await self._wait(s.settle_s, "settling")
        attempt.start_g, n = mean_between(self.balance.samples(), self.clock() - AVERAGE_S, self.clock())
        if n == 0:
            raise RuntimeError("No balance readings. Check that it is connected and sending.")
        target_g = s.volume_ml * s.density
        self.phase, self.phase_until = "running", None
        try:
            started = await self.set_head(result.did, result.ch, True)
        except BaseException:
            await self._turn_off(result)  # the command may have reached the pump anyway
            raise
        if not started:
            # A timeout can come after the pump has already started: make sure it is off.
            await self._turn_off(result)
            raise RuntimeError(f"Couldn't start {result.label}. Check that the pump is online.")
        attempt.t_on = self.clock()
        try:
            while True:
                self._check()
                await self.sleep(0.1)
                now = self.clock()
                recent, n = mean_between(self.balance.samples(since=now - 1.0), now - 1.0, now)
                got = recent - attempt.start_g if n else 0.0  # averaged, so a wobble can't stop it early
                ran = now - attempt.t_on
                if got >= target_g and ran >= s.min_run_s():
                    break
                if ran >= s.max_run_s:
                    break
                if ran >= NO_FLOW_AFTER_S and got < NO_FLOW_G:
                    attempt.problem = "No flow seen. Check the tubing, and that the outlet is in the beaker."
                    break
        finally:
            attempt.t_off = self.clock()
            await self._turn_off(result)
        if attempt.problem:
            attempt.samples = self.balance.samples(since=t_start)
            raise HeadProblem(attempt.problem)
        await self._wait(s.settle_s, "settling after")
        t0 = self.clock()
        await self._wait(AVERAGE_S, "settling after")
        attempt.end_g, n = mean_between(self.balance.samples(), t0, self.clock())
        if n == 0:
            raise RuntimeError("No balance readings after the run.")
        attempt.samples = self.balance.samples(since=t_start)

    async def _turn_off(self, result):
        for _ in range(3):
            if await self.set_head(result.did, result.ch, False):
                return
            await self.sleep(0.5)
        raise RuntimeError(f"Couldn't stop {result.label}! Stop it by hand or press Emergency Stop.")

    def _log_attempt(self, result, a, tries):
        if a.slope_rate is None:
            log.warning("Calibration %s (try %d of %d): %s", result.label, a.number, tries, a.problem)
            return
        msg = (
            f"Calibration {result.label} (try {a.number} of {tries}): slope {a.slope_rate:.3f} vs "
            f"Δmass {a.dmass_rate:.3f} mL/min ({a.diff_pct:.1f} % apart), R² {a.r2:.4f}"
        )
        if a.passed:
            log.info(msg + " — passed.")
        else:
            log.warning(msg + f" — {a.problem}.")

    # --- estimates ---

    def seconds_left(self, rate_for):
        """Rough time left. ``rate_for(did, ch)`` gives the saved mL/min (a guess for new heads)."""
        s = self.settings
        per_try_extra = 2 * s.settle_s + AVERAGE_S
        total = 0.0
        for r in self.results:
            if r.status not in (WAITING, RUNNING):
                continue
            rate = rate_for(r.did, r.ch) or 1.0
            run = max(s.min_run_s(), min(s.max_run_s, s.volume_ml / rate * 60.0))
            total += run + per_try_extra
        if self.attempt is not None and self.attempt.t_on and self.phase == "running":
            total -= self.clock() - self.attempt.t_on
        return max(0.0, total)


def estimate_seconds(settings, rates):
    """Rough total time for heads with these saved rates (mL/min), one try each."""
    s = settings
    total = 0.0
    for rate in rates:
        rate = rate if rate and rate > 0 else 1.0
        total += max(s.min_run_s(), min(s.max_run_s, s.volume_ml / rate * 60.0)) + 2 * s.settle_s + AVERAGE_S
    return total


# --- saving the results ------------------------------------------------------------------


def write_csv(path, results, settings, balance_name=""):
    """Write a summary row per attempt and return the path. Raw readings go to *_raw.csv."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["# Jebao automatic calibration", time.strftime("%Y-%m-%d %H:%M:%S"), balance_name])
        w.writerow(["# settings"] + [f"{k}={v}" for k, v in settings.to_dict().items()])
        w.writerow(
            [
                "head",
                "pump_id",
                "channel",
                "try",
                "on_time_s",
                "start_g",
                "end_g",
                "delivered_mL",
                "slope_rate_mL_min",
                "dmass_rate_mL_min",
                "diff_pct",
                "r2",
                "passed",
                "old_rate_mL_min",
                "result",
                "note",
            ]
        )
        for r in results:
            for a in r.attempts or [None]:
                if a is None:
                    w.writerow(
                        [
                            r.label,
                            r.did,
                            r.ch,
                            "",
                            "",
                            "",
                            "",
                            "",
                            "",
                            "",
                            "",
                            "",
                            "",
                            r.old_rate,
                            r.status,
                            r.note,
                        ]
                    )
                    continue
                w.writerow(
                    [
                        r.label,
                        r.did,
                        r.ch,
                        a.number,
                        f"{a.on_time:.2f}",
                        f"{a.start_g:.4f}",
                        f"{a.end_g:.4f}",
                        "" if a.volume_ml is None else f"{a.volume_ml:.3f}",
                        "" if a.slope_rate is None else f"{a.slope_rate:.4f}",
                        "" if a.dmass_rate is None else f"{a.dmass_rate:.4f}",
                        "" if a.diff_pct is None else f"{a.diff_pct:.2f}",
                        "" if a.r2 is None else f"{a.r2:.5f}",
                        a.passed,
                        r.old_rate,
                        r.status,
                        a.problem or r.note,
                    ]
                )
    raw = os.path.splitext(path)[0] + "_raw.csv"
    with open(raw, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["head", "try", "seconds_from_pump_on", "grams", "stable"])
        for r in results:
            for a in r.attempts:
                for t, g, st in a.samples:
                    w.writerow([r.label, a.number, f"{t - a.t_on:.2f}", f"{g:.4f}", int(bool(st))])
    return path
