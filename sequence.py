"""Sequences: their steps, what a run of one will do, and what a running one is doing.

A sequence is a list of steps. Each step is a dict like::

    {"kind": "dose", "heads": [["did", 1], ["did", 2]], "by": "volume", "volume": 6.0,
     "seconds": 0.0, "burst": 0.0, "gap": 0.0, "background": False, "label": "Condition",
     "lanes": [1, 2, 3, 4], "name": "", "sound": True, "message": ""}

Kinds (shown with the names in ``LABELS``):

- ``dose``: pump ``volume`` mL (``by`` "volume") or for ``seconds`` (``by`` "time") on each head.
- ``staccato``: pump ``volume`` mL in bursts of ``burst`` mL with ``gap`` s between them.
- ``wait``: a timer of ``seconds``.
- ``review``: wait until someone presses Continue ("Wait for me"), showing ``message``
  (what to do there, e.g. a setup change) in a pop-up and the orange bar.
- ``sync``: the lanes wait for each other, and for what they started in the background.
- ``sequence``: run the saved sequence ``name``.

**Lanes.** The four lanes are the channel numbers CH1–CH4, as the app has always run
them: each lane goes through the steps that use it in order, side by side with the
other lanes. A dose or staccato step uses the lanes of its heads' channels; a lane runs
that step on its own heads only (a step on JP 1 · CH1 and JP 6 · CH1 runs both heads in
lane 1, together). The other kinds use ``lanes``.

**Older sequences.** Steps saved by version 2.6 and older (``{"pump_did", "channel",
"action", "val1", "val2", "val3"}``) are read too. Steps are saved with those fields as
well, so an older copy of the app sharing the same settings folder can still run them.

No Qt here, so it can be tested on its own.
"""

import asyncio
import copy
import json
import math
import os
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime

import pumpguard

KINDS = ("dose", "staccato", "wait", "review", "sync", "sequence")
LABELS = {
    "dose": "Dose",
    "staccato": "Staccato",
    "wait": "Wait (timer)",
    "review": "Wait for me",
    "sync": "Sync columns",
    "sequence": "Run sequence",
}
PUMPING = ("dose", "staccato")
LANES = (1, 2, 3, 4)
STALE_DAYS = 30  # a calibration older than this gets a warning
MAX_ML = 100000.0  # bigger amounts in a saved step are cut to this
MAX_S = 7 * 24 * 3600.0  # and longer times to a week
MAX_BURSTS = 5000  # a staccato step may have at most this many bursts
SIM_LIMIT = 8000  # pumping runs worked out with the on-time limit before a plan stops counting rests
DEFAULT_RATE = 1.0  # what a pump that was never calibrated has (ConfigManager.get_pump)

# 2.6 and older: action name -> (kind, background)
_LEGACY = {
    "Dose": ("dose", False),
    "Dose (Async)": ("dose", True),
    "Staccato": ("staccato", False),
    "Staccato (Async)": ("staccato", True),
    "Pause": ("wait", False),
    "Pause (User Review)": ("review", False),
    "Wait for All": ("sync", False),
    "Run Sub-Sequence": ("sequence", False),
}


# --- steps -------------------------------------------------------------------------------


def _num(value, default=0.0, high=MAX_ML):
    """A number from saved data, between 0 and ``high`` (``default`` if it isn't one)."""
    try:
        v = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(v):
        return default
    return min(max(0.0, v), high)


MAX_MESSAGE = 2000  # characters in a Wait for me message


def _text(value):
    """A message from saved data: text, trimmed, at most MAX_MESSAGE characters."""
    return str(value).strip()[:MAX_MESSAGE] if isinstance(value, (str, int, float)) else ""


def _channel(value):
    """1–4 from 1, "1" or 1.0, else None."""
    try:
        f = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(f) or not f.is_integer() or int(f) not in LANES:
        return None
    return int(f)


def _lanes(value, default=LANES):
    if not isinstance(value, (list, tuple)):
        value = [value]
    out = []
    for v in value:
        ch = _channel(v)
        if ch is not None and ch not in out:
            out.append(ch)
    return sorted(out) if out else list(default)


def _heads(value):
    out = []
    for h in value if isinstance(value, (list, tuple)) else []:
        if not isinstance(h, (list, tuple)) or len(h) < 2:
            continue
        did, ch = h[0], _channel(h[1])
        if did and isinstance(did, (str, int)) and ch is not None and [str(did), ch] not in out:
            out.append([str(did), ch])
    return out


def new_step(kind, heads=None, lanes=None):
    """A new step of ``kind`` with sensible amounts."""
    step = {
        "kind": kind,
        "heads": _heads(heads),
        "lanes": _lanes(lanes),
        "by": "volume",
        "volume": 5.0,
        "seconds": 60.0 if kind == "wait" else 30.0,
        "burst": 1.0,
        "gap": 10.0,
        "background": False,
        "label": "",
        "name": "",
        "sound": True,
        "message": "",
    }
    if kind == "staccato":
        step["volume"] = 10.0
    return step


def normalize(step):
    """A clean copy of a step in the current format (an older one is converted)."""
    if not isinstance(step, dict):
        return new_step("wait")
    if step.get("kind") in KINDS:
        out = new_step(step["kind"])
        out.update(
            {
                "heads": _heads(step.get("heads")),
                "lanes": _lanes(step.get("lanes")),
                "by": "time" if step.get("by") == "time" and step["kind"] == "dose" else "volume",
                "volume": _num(step.get("volume"), out["volume"]),
                "seconds": _num(step.get("seconds"), out["seconds"], MAX_S),
                "burst": _num(step.get("burst"), out["burst"]),
                "gap": _num(step.get("gap"), out["gap"], MAX_S),
                "background": bool(step.get("background")) and step["kind"] in PUMPING,
                "label": str(step.get("label") or "").strip(),
                "name": str(step.get("name") or ""),
                "sound": bool(step.get("sound", True)),
                "message": _text(step.get("message")) if step["kind"] == "review" else "",
            }
        )
        return out
    action = str(step.get("action"))
    channels = _lanes(step.get("channel", [1]), default=[1])
    if action not in _LEGACY:  # 2.6 skipped a step it didn't know: so does this
        out = new_step("wait", lanes=channels)
        out.update(seconds=0.0, label=f"Unknown step “{action}”", sound=False)
        return out
    kind, background = _LEGACY[action]
    out = new_step(kind, lanes=channels)
    did = step.get("pump_did")
    v1, v2, v3 = (step.get(k, 0) for k in ("val1", "val2", "val3"))
    if kind in PUMPING:
        out["heads"] = [[str(did), ch] for ch in channels] if did and isinstance(did, (str, int)) else []
        out["volume"] = _num(v1)
        out["burst"] = _num(v2)
        out["gap"] = _num(v3, high=MAX_S)
    elif kind == "wait":
        out["seconds"] = _num(v1, high=MAX_S)
    elif kind == "sequence":
        out["name"] = str(v1 or "")
    out["background"] = background
    out["sound"] = False  # 2.6 didn't beep
    return out


def normalize_all(steps):
    return [normalize(s) for s in steps or []]


def old_app_can_run(step):
    """True when version 2.6 and older can run the step exactly as saved for them: a pumping
    step on one pump, by volume (2.6 had no time doses and one pump per step)."""
    if step["kind"] not in PUMPING:
        return True
    pumps = {did for did, _ch in step["heads"]}
    return len(pumps) <= 1 and not (step["kind"] == "dose" and step["by"] == "time")


def to_saved(step):
    """What goes in the settings file: the current fields plus the 2.6 ones (see the module
    docstring). A step 2.6 can't run exactly is saved for it as a "Pause (User Review)", so an
    older copy of the app stops there and asks, instead of pumping the wrong thing."""
    s = normalize(step)
    out = copy.deepcopy(s)
    kind = s["kind"]
    if not old_app_can_run(s):
        out.update(pump_did=None, channel=lanes_of(s) or [1], val1=0, val2=0, val3=0)
        out["action"] = "Pause (User Review)"
        return out
    if kind in PUMPING:
        action = "Dose" if kind == "dose" else "Staccato"
        if s["background"]:
            action += " (Async)"
        heads = s["heads"]
        out["pump_did"] = heads[0][0] if heads else None
        out["channel"] = sorted({ch for _d, ch in heads}) or [1]
        out["val1"] = s["volume"]
        out["val2"] = s["burst"] if kind == "staccato" else 0
        out["val3"] = s["gap"] if kind == "staccato" else 0
    else:
        action = {"wait": "Pause", "review": "Pause (User Review)", "sync": "Wait for All"}.get(
            kind, "Run Sub-Sequence"
        )
        out["pump_did"] = None
        out["channel"] = list(s["lanes"])
        out["val1"] = s["seconds"] if kind == "wait" else (s["name"] if kind == "sequence" else 0)
        out["val2"] = 0
        out["val3"] = 0
    out["action"] = action
    return out


def to_saved_all(steps):
    return [to_saved(s) for s in steps or []]


def same_steps(a, b):
    """True when two step lists do the same thing (whatever format they are in)."""
    return normalize_all(a) == normalize_all(b)


def heads_of(step):
    return [tuple(h) for h in step.get("heads") or []]


def lanes_of(step):
    """The lanes a step runs in: its heads' channels, or its ``lanes``."""
    if step.get("kind") in PUMPING:
        return sorted({ch for _d, ch in heads_of(step)})
    return list(step.get("lanes") or LANES)


def step_problem(step):
    """Why a pumping step can't run as it is (None if it can). The run stops at such a step."""
    kind = step["kind"]
    if kind not in PUMPING:
        return None
    if not step["heads"]:
        return "no heads picked"
    if kind == "dose" and step["by"] == "time":
        if step["seconds"] <= 0:
            return "the time is 0"
    elif step["volume"] <= 0:
        return "the volume is 0 mL"
    if kind == "staccato":
        if step["burst"] <= 0:
            return "the burst size is 0 mL"
        n = burst_count(step)
        if n > MAX_BURSTS:
            least = step["volume"] / MAX_BURSTS
            return f"{n} bursts is too many (at most {MAX_BURSTS}): make the bursts at least {least:.2g} mL"
    return None


def pump_pattern(step, rate):
    """What one head does for a pumping step: [("pump", s), ("gap", s), ...], or None when
    the time can't be worked out (no flow rate for a volume, or a step with a problem)."""
    kind = step["kind"]
    if step_problem(step) not in (None, "no heads picked"):
        return None
    if kind == "dose" and step["by"] == "time":
        return [("pump", step["seconds"])]
    if not rate or rate <= 0:
        return None
    if kind == "dose":
        return [("pump", step["volume"] / rate * 60.0)]
    total, burst, gap = step["volume"], step["burst"], step["gap"]
    out = []
    n = int(total // burst)
    rest = total - n * burst
    for b in range(n):
        out.append(("pump", burst / rate * 60.0))
        if b < n - 1 or rest > 1e-9:
            out.append(("gap", gap))
    if rest > 0.01:
        out.append(("pump", rest / rate * 60.0))
    return out


def burst_count(step):
    if step["kind"] != "staccato" or step["burst"] <= 0:
        return 1
    ratio = step["volume"] / step["burst"]
    if ratio > 10 * MAX_BURSTS:
        return int(min(ratio, 1e12))
    n = int(step["volume"] // step["burst"])
    return n + (1 if step["volume"] - n * step["burst"] > 0.01 else 0)


# --- words -------------------------------------------------------------------------------


def fmt_time(seconds, approx=False):
    """'45 s', '2 min 24 s', '10 min', '3 h 05 min' ('≈ ' in front with ``approx``)."""
    if seconds is None:
        return "?"
    s = max(0.0, float(seconds))
    if s < 10 and s != int(s):
        text = f"{s:.1f} s"
    elif s < 60:
        text = f"{s:.0f} s"
    elif s < 3600:
        m, sec = divmod(int(round(s)), 60)
        text = f"{m} min" + (f" {sec} s" if sec else "")
    else:
        h, rem = divmod(int(round(s / 60)), 60)
        text = f"{h} h {rem:02d} min"
    return ("≈ " if approx else "") + text


def fmt_clock(seconds):
    """'1:05:09' or '5:09' for a countdown."""
    s = max(0, int(round(seconds or 0)))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def fmt_ml(ml):
    return f"{ml:.1f} mL" if ml < 100 and abs(ml - round(ml)) > 0.05 else f"{ml:.0f} mL"


def compress_numbers(nums):
    """[1, 2, 3, 4] -> '1–4', [1, 3] -> '1, 3', [1, 2, 4] -> '1–2, 4'."""
    nums = sorted(set(nums))
    parts = []
    i = 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        if j > i:
            parts.append(f"{nums[i]}–{nums[j]}")
        else:
            parts.append(str(nums[i]))
        i = j + 1
    return ", ".join(parts)


def lanes_text(lanes):
    return "CH" + compress_numbers(lanes) if lanes else "no channel"


_TRAILING = re.compile(r"^(.*?)(\d+)$")


def compress_names(names):
    """['EtOH → Col 1', 'EtOH → Col 2', 'EtOH → Col 3'] -> 'EtOH → Col 1–3'."""
    names = list(dict.fromkeys(n for n in names if n))
    if len(names) < 2:
        return names[0] if names else ""
    parts = [_TRAILING.match(n) for n in names]
    if all(parts) and len({p.group(1) for p in parts}) == 1:
        return parts[0].group(1) + compress_numbers(int(p.group(2)) for p in parts)
    return ", ".join(names)


def short_head_name(name, ch):
    """The chip label for a head: the part after '→' of its name ('Col 1'), or 'CH1'."""
    if name and "→" in name:
        tail = name.split("→", 1)[1].strip()
        if tail:
            return tail
    return name or f"CH{ch}"


def liquid_of(head_name, pump_name):
    """The liquid a head pumps: 'EtOH' from 'EtOH → Col 1', else from the pump name
    ('JP 1: EtOH' -> 'EtOH'), else the pump name."""
    if head_name and "→" in head_name:
        left = head_name.split("→", 1)[0].strip()
        if left:
            return left
    if pump_name and ":" in pump_name:
        right = pump_name.split(":", 1)[1].strip()
        if right:
            return right
    return pump_name or "?"


def describe(step):
    """A short title: 'Dose 6 mL', 'Staccato 250 mL in 5 mL bursts, 20 s apart', 'Wait 10 min'."""
    k = step["kind"]
    if k == "dose":
        return "Dose " + (fmt_time(step["seconds"]) if step["by"] == "time" else f"{step['volume']:g} mL")
    if k == "staccato":
        if step["burst"] <= 0:
            return f"Staccato {step['volume']:g} mL"
        return f"Staccato {step['volume']:g} mL in {step['burst']:g} mL bursts, {fmt_time(step['gap'])} apart"
    if k == "wait":
        return "Wait " + fmt_time(step["seconds"])
    if k == "review":
        return "Wait for me"
    if k == "sync":
        return "Sync columns"
    return f"Run “{step['name']}”" if step["name"] else "Run sequence (none picked)"


def title(step):
    """describe() plus the label: 'Dose 6 mL · Condition'."""
    text = describe(step)
    return f"{text} · {step['label']}" if step.get("label") else text


# --- what the app knows about the pumps (for plans and checks) -----------------------------


class Context:
    """What a plan needs to know, passed in so this module needs no Qt and no app.

    ``rate(did, ch)``: mL/min or None; ``calibrated_at(did, ch)``: 'YYYY-MM-DD' or None;
    ``online(did)``: True, False or None (unknown); ``limit((did, ch))``: a pumpguard.Limit;
    ``saved(name)``: a saved sequence's steps or None; ``head_name(did, ch)`` and
    ``pump_name(did)``: names from the Rack tab.
    """

    def __init__(
        self,
        rate=None,
        calibrated_at=None,
        online=None,
        limit=None,
        saved=None,
        head_name=None,
        pump_name=None,
        known=None,
        today=None,
    ):
        self.rate = rate or (lambda did, ch: None)
        self.calibrated_at = calibrated_at or (lambda did, ch: None)
        self.online = online or (lambda did: None)
        self.limit = limit or (lambda key: pumpguard.Limit())
        self.saved = saved or (lambda name: None)
        self.head_name = head_name or (lambda did, ch: "")
        self.pump_name = pump_name or (lambda did: did)
        self.known = known  # did -> bool: is the pump on the account (None: don't check)
        self.today = today or date.today()

    def label(self, did, ch):
        """'JP 1 · CH2 (EtOH → Col 2)', as in the log."""
        name = self.head_name(did, ch)
        text = f"{self.pump_name(did)} · CH{ch}"
        return f"{text} ({name})" if name else text

    def liquid(self, did, ch):
        return liquid_of(self.head_name(did, ch), self.pump_name(did))


def heads_text(step, ctx):
    """'EtOH → Col 1–4 (JP 1 · CH1–4)' for a pumping step, grouped by pump."""
    heads = heads_of(step)
    if not heads:
        return "No heads picked"
    groups = {}
    for did, ch in heads:
        groups.setdefault(did, []).append(ch)
    parts = []
    for did, chs in groups.items():
        where = f"{ctx.pump_name(did)} · CH{compress_numbers(chs)}"
        names = [ctx.head_name(did, ch) for ch in sorted(chs)]
        named = compress_names(names) if all(names) else ""
        parts.append(f"{named} ({where})" if named else where)
    return " · ".join(parts)


def where_text(step, ctx):
    """The second line of a step card."""
    k = step["kind"]
    if k in PUMPING:
        return heads_text(step, ctx)
    lanes = lanes_text(step["lanes"])
    if k == "review":
        first = step.get("message", "").splitlines()[0] if step.get("message") else ""
        if first:
            return f"{lanes} stop here · “{first}”"
        return f"{lanes} stop here until you continue"
    if k == "sync":
        return f"{lanes} wait for each other and for their background steps"
    if k == "sequence":
        sub = ctx.saved(step["name"]) if step["name"] else None
        n = len(sub) if sub else 0
        return f"{lanes} · {n} step{'s' if n != 1 else ''}" if sub is not None else f"{lanes} · not found"
    return lanes


# --- flattening sub-sequences ------------------------------------------------------------


@dataclass
class Flat:
    """One step as it will run: ``path`` is its place (top-level index, then the index
    inside each sub-sequence), ``lanes`` the lanes it runs in."""

    path: tuple
    step: dict
    lanes: tuple
    via: tuple = ()  # the sub-sequence names it is inside

    @property
    def top(self):
        return self.path[0]


@dataclass
class Check:
    level: str  # "error" (won't work), "warn" (probably not what you want), "info", "ok"
    text: str
    top: int | None = None


def flatten(steps, ctx, name=None):
    """Every step that will run, with sub-sequences opened up. Returns (flat, checks)."""
    out, checks = [], []

    def walk(items, path, allowed, stack, via):
        for i, step in enumerate(items):
            p = path + (i,)
            where = f"Step {p[0] + 1}" + (f" (in “{via[-1]}”)" if via else "")
            lanes = tuple(ln for ln in lanes_of(step) if ln in allowed)
            if step["kind"] == "sequence":
                sub_name = step["name"]
                sub = ctx.saved(sub_name) if sub_name else None
                if not sub_name:
                    checks.append(Check("error", f"{where}: no sequence picked to run.", p[0]))
                elif sub is None:
                    checks.append(Check("error", f"{where}: there's no saved sequence “{sub_name}”.", p[0]))
                elif sub_name in stack:
                    checks.append(
                        Check("error", f"{where}: “{sub_name}” would run itself, round and round.", p[0])
                    )
                elif lanes:
                    walk(normalize_all(sub), p, set(lanes), stack | {sub_name}, via + (sub_name,))
                continue
            out.append(Flat(p, step, lanes, via))

    walk(normalize_all(steps), (), set(LANES), {name} if name else set(), ())
    return out, checks


# --- the plan: what a run will do --------------------------------------------------------


@dataclass
class Segment:
    kind: str  # "pump", "rest", "gap", "wait", "review", "sync"
    start: float
    end: float
    top: int
    text: str = ""
    background: bool = False


@dataclass
class HeadPlan:
    key: tuple
    label: str
    liquid: str
    rate: float | None
    volume_ml: float = 0.0
    pump_s: float = 0.0
    rest_s: float = 0.0
    steps: set = field(default_factory=set)
    volume_known: bool = True


@dataclass
class Plan:
    total_s: float = 0.0
    lanes: dict = field(default_factory=lambda: {ln: [] for ln in LANES})
    lane_step_s: dict = field(default_factory=dict)  # (lane, top) -> seconds in that step
    step_s: dict = field(default_factory=dict)  # top -> seconds (slowest lane; None: unknown)
    step_rest_s: dict = field(default_factory=dict)  # top -> the longest any head rests in it
    heads: dict = field(default_factory=dict)  # (did, ch) -> HeadPlan
    liquids: dict = field(default_factory=dict)  # liquid -> mL
    reviews: list = field(default_factory=list)  # (time, top, lane)
    checks: list = field(default_factory=list)
    flat: list = field(default_factory=list)
    used_lanes: list = field(default_factory=list)
    unknown: set = field(default_factory=set)  # tops whose time couldn't be worked out
    limit_cut: bool = False  # too long to count every on-time limit rest

    @property
    def errors(self):
        return [c for c in self.checks if c.level == "error"]

    @property
    def rest_s(self):
        return sum(self.step_rest_s.values())

    def first_review(self):
        return min(self.reviews) if self.reviews else None


def resting_need(limit):
    """How much on-time a resting head waits for before it runs again (as the pump guard)."""
    return pumpguard.resting_need(limit)


def _pump_with_limit(budget, key, t, seconds, limit):
    """Pump ``seconds`` from ``t`` under the on-time limit. Returns (end, [(kind, a, b)])."""
    out = []
    left = float(seconds)
    need = resting_need(limit)
    spans = budget.intervals.get(key)
    if spans and limit.enabled:  # only the last window counts: drop older runs (keeps it fast)
        cutoff = t - limit.window_s
        drop = 0
        while drop < len(spans) and spans[drop][1] is not None and spans[drop][1] < cutoff:
            drop += 1
        if drop:
            del spans[:drop]
    for _ in range(100000):
        if left <= 1e-9:
            break
        if not limit.enabled:
            budget.start(key, t)
            budget.stop(key, t + left)
            out.append(("pump", t, t + left))
            return t + left, out
        used = budget.used(key, t, limit.window_s)
        if used >= limit.on_s - 1e-6:
            resume = max(budget.resume_at(key, t, limit, need), t + 1.0)
            out.append(("rest", t, resume))
            t = resume
            continue
        budget.start(key, t)
        if used + left <= limit.on_s or budget.used(key, t + left, limit.window_s) < limit.on_s:
            x = left
        else:  # on-time only grows while it runs: find when it reaches the limit
            lo, hi = 0.0, left
            for _ in range(50):
                mid = (lo + hi) / 2
                if budget.used(key, t + mid, limit.window_s) >= limit.on_s:
                    hi = mid
                else:
                    lo = mid
            x = max(hi, 1e-3)
        budget.stop(key, t + x)
        out.append(("pump", t, t + x))
        t += x
        left -= x
    return t, out


def plan(steps, ctx, name=None, with_limit=True):
    """Work out what a run of ``steps`` will do, without sending anything to the pumps."""
    steps = normalize_all(steps)
    p = Plan()
    flat, p.checks = flatten(steps, ctx, name)
    p.flat = flat
    budget = pumpguard.OnTimeBudget()
    off = pumpguard.Limit(enabled=False)
    t = {ln: 0.0 for ln in LANES}
    bg = {ln: [] for ln in LANES}  # when each lane's background steps end
    head_busy = {}  # (did, ch) -> until when a background step uses it
    bg_span = {}  # top -> how long its background run takes
    work = [0]  # pumping runs worked out so far
    head_rest = {}  # (top, head) -> how long that head rests in that step
    used_lanes = set()
    warned = set()
    for top in range(len(steps)):
        p.step_s[top] = 0.0
        p.step_rest_s[top] = 0.0

    def add_lane_time(lane, top, seconds):
        p.lane_step_s[(lane, top)] = p.lane_step_s.get((lane, top), 0.0) + seconds

    for item in flat:
        st, top = item.step, item.top
        kind = st["kind"]
        if kind == "sync":
            if not item.lanes:
                continue
            for lane in item.lanes:
                used_lanes.add(lane)
                if bg[lane]:
                    start = t[lane]
                    t[lane] = max([t[lane]] + bg[lane])
                    bg[lane] = []
                    if t[lane] > start:
                        p.lanes[lane].append(
                            Segment("sync", start, t[lane], top, "waits for its background steps")
                        )
                        add_lane_time(lane, top, t[lane] - start)
            meet = max(t[lane] for lane in item.lanes)
            for lane in item.lanes:
                if meet > t[lane]:
                    p.lanes[lane].append(Segment("sync", t[lane], meet, top, "waits for the other channels"))
                    add_lane_time(lane, top, meet - t[lane])
                add_lane_time(lane, top, 0.0)
                t[lane] = meet
            continue
        for lane in item.lanes:
            used_lanes.add(lane)
            start = t[lane]
            if kind == "wait":
                t[lane] = start + st["seconds"]
                p.lanes[lane].append(Segment("wait", start, t[lane], top, describe(st)))
                add_lane_time(lane, top, st["seconds"])
                continue
            if kind == "review":
                p.reviews.append((start, top, lane))
                p.lanes[lane].append(Segment("review", start, start, top, title(st)))
                add_lane_time(lane, top, 0.0)
                continue
            # dose / staccato: this lane's heads, together
            ends = [start]
            lane_segs = []
            for key in [h for h in heads_of(st) if h[1] == lane]:
                rate = ctx.rate(*key)
                hp = p.heads.get(key)
                if hp is None:
                    hp = p.heads[key] = HeadPlan(key, ctx.label(*key), ctx.liquid(*key), rate)
                hp.steps.add(top)
                pattern = pump_pattern(st, rate)
                if pattern is None:
                    p.unknown.add(top)
                    continue
                if head_busy.get(key, -1) > start + 1e-6 and (key, top) not in warned:
                    warned.add((key, top))
                    p.checks.append(
                        Check(
                            "warn",
                            f"Step {top + 1}: {ctx.label(*key)} is still busy with a background step "
                            "when this one starts. Add a Sync columns step before it.",
                            top,
                        )
                    )
                limit = ctx.limit(key) if with_limit and work[0] < SIM_LIMIT else off
                if with_limit and work[0] >= SIM_LIMIT:
                    p.limit_cut = True
                now, segs = start, []
                for what, seconds in pattern:
                    if what == "gap":
                        segs.append(("gap", now, now + seconds))
                        now += seconds
                        continue
                    now, ran = _pump_with_limit(budget, key, now, seconds, limit)
                    work[0] += 1
                    segs.extend(ran)
                    hp.pump_s += seconds
                    if rate:
                        hp.volume_ml += seconds * rate / 60.0
                    else:
                        hp.volume_known = False
                rest = sum(b - a for k, a, b in segs if k == "rest")
                hp.rest_s += rest
                head_rest[(top, key)] = head_rest.get((top, key), 0.0) + rest
                p.step_rest_s[top] = max(p.step_rest_s.get(top, 0.0), head_rest[(top, key)])
                ends.append(now)
                if st["background"]:
                    head_busy[key] = max(head_busy.get(key, 0.0), now)
                if now >= max(e for e in ends):
                    lane_segs = segs
            end = max(ends)
            for k, a, b in lane_segs:
                p.lanes[lane].append(Segment(k, a, b, top, title(st), background=st["background"]))
            if st["background"]:
                bg[lane].append(end)
                bg_span[top] = max(bg_span.get(top, 0.0), end - start)
                add_lane_time(lane, top, 0.0)
            else:
                t[lane] = end
                add_lane_time(lane, top, end - start)

    lane_end = {ln: max([t[ln]] + bg[ln]) for ln in LANES}
    p.total_s = max(lane_end.values()) if flat else 0.0
    p.used_lanes = sorted(used_lanes)
    for (_lane, top), seconds in p.lane_step_s.items():
        p.step_s[top] = max(p.step_s.get(top, 0.0), seconds)
    for top, seconds in bg_span.items():  # a background step's own time, for its card
        p.step_s[top] = max(p.step_s.get(top, 0.0), seconds)
    for hp in p.heads.values():
        if hp.volume_known:
            p.liquids[hp.liquid] = p.liquids.get(hp.liquid, 0.0) + hp.volume_ml
    _check(steps, flat, p, ctx, with_limit)
    return p


def _check(steps, flat, p, ctx, with_limit):
    """The "Before you run" list."""
    checks = p.checks
    if not steps:
        checks.insert(0, Check("error", "There are no steps yet. Add one below."))
        return
    seen_heads = {}
    for item in flat:
        st, top = item.step, item.top
        n = f"Step {top + 1}" + (f" (in “{item.via[-1]}”)" if item.via else "")
        kind = st["kind"]
        if kind in PUMPING:
            problem = step_problem(st)
            if problem:
                checks.append(Check("error", f"{n}: {problem}.", top))
                if not heads_of(st):
                    continue
            elif kind == "staccato" and st["burst"] > 0:
                rates = [r for r in (ctx.rate(*h) for h in heads_of(st)) if r]
                shortest = st["burst"] / max(rates) * 60 if rates else None
                if shortest is not None and shortest < 3:
                    checks.append(
                        Check(
                            "warn",
                            f"{n}: each burst is only {fmt_time(shortest)} of pumping. The cloud takes about "
                            "a second to switch a pump, so bursts this short aren't accurate.",
                            top,
                        )
                    )
            if kind == "staccato" and st["burst"] >= st["volume"] > 0:
                checks.append(
                    Check("info", f"{n}: the burst is as big as the whole volume, so it's one burst.", top)
                )
            for key in heads_of(st):
                if item.lanes and key[1] not in item.lanes:
                    continue
                seen_heads.setdefault(key, top)
        elif kind == "wait" and st["seconds"] <= 0:
            checks.append(Check("info", f"{n}: the wait is 0 s, so it does nothing.", top))
    # each head once: calibration and the pump being there
    offline, unknown_pumps, used_pumps = set(), set(), []
    for key, top in seen_heads.items():
        did, ch = key
        label = ctx.label(did, ch)
        if did not in used_pumps:
            used_pumps.append(did)
        if ctx.known is not None and not ctx.known(did):
            unknown_pumps.add(did)
            continue
        if ctx.online(did) is False:
            offline.add(did)
        needs_rate = any(
            it.step["kind"] in PUMPING
            and not (it.step["kind"] == "dose" and it.step["by"] == "time")
            and key in heads_of(it.step)
            for it in flat
        )
        rate = ctx.rate(did, ch)
        when = ctx.calibrated_at(did, ch)
        if not rate or rate <= 0:
            level = "error" if needs_rate else "warn"
            checks.append(Check(level, f"Step {top + 1}: {label} has no flow rate. Calibrate it first.", top))
        elif not when and abs(rate - DEFAULT_RATE) < 1e-9 and needs_rate:
            checks.append(
                Check(
                    "warn",
                    f"Step {top + 1}: {label} looks uncalibrated (still the default 1 mL/min).",
                    top,
                )
            )
        elif when and needs_rate:
            try:
                age = (ctx.today - date.fromisoformat(str(when))).days
            except ValueError:
                age = None
            if age is not None and age > STALE_DAYS:
                checks.append(
                    Check("warn", f"Step {top + 1}: {label} was last calibrated {age} days ago.", top)
                )
    for did in sorted(unknown_pumps, key=str):
        checks.append(Check("error", f"{ctx.pump_name(did)} isn't on your Jebao account (any more)."))
    for did in sorted(offline, key=str):
        checks.append(
            Check(
                "warn", f"{ctx.pump_name(did)} is offline right now. Its steps will fail if it stays offline."
            )
        )
    rested = [top for top, s in p.step_rest_s.items() if s > 0.5]
    if with_limit and rested:
        steps_txt = ", ".join(str(t + 1) for t in rested)
        checks.append(
            Check(
                "info",
                f"Step{'s' if len(rested) > 1 else ''} {steps_txt} "
                f"{'run' if len(rested) > 1 else 'runs'} longer than the on-time limit: the heads rest "
                f"up to {fmt_time(p.rest_s, approx=True)} in all, counted in the times shown.",
                rested[0],
            )
        )
    if p.unknown:
        checks.append(
            Check("info", "Some times are unknown (a head without a flow rate, or a step with a problem).")
        )
    if p.limit_cut:
        checks.append(
            Check(
                "info",
                "This sequence is too long to work out every on-time limit rest: the later steps' "
                "times leave them out, so it may take longer than shown.",
            )
        )
    if any(not old_app_can_run(s) for s in steps):
        checks.append(
            Check(
                "info",
                "Some steps (more than one pump, or a dose by time) need version 2.7 or newer. A "
                "computer still on an older version stops at them and asks for you instead.",
            )
        )
    if used_pumps and not offline and not unknown_pumps and ctx.known is not None:
        names = compress_names(ctx.pump_name(d) for d in used_pumps)
        checks.append(Check("ok", f"Every pump this sequence uses is online ({names})."))
    if p.liquids:
        checks.append(Check("ok", "Liquid needed: " + liquids_text(p.liquids) + "."))


def liquids_text(liquids):
    return ", ".join(f"{name} {fmt_ml(ml)}" for name, ml in sorted(liquids.items(), key=lambda kv: -kv[1]))


# --- a run in progress -------------------------------------------------------------------


class SyncPoint:
    """Where lanes wait for each other (a Sync columns step). A lane that skips the step
    counts as arrived, so the others don't wait for it for ever."""

    def __init__(self, lanes):
        self.expected = set(lanes)
        self.arrived = set()
        self.event = asyncio.Event()
        if not self.expected:
            self.event.set()

    def arrive(self, lane):
        self.arrived.add(lane)
        if self.expected <= self.arrived:
            self.event.set()

    def waiting_for(self):
        return sorted(self.expected - self.arrived)

    async def wait(self, lane):
        self.arrive(lane)
        await self.event.wait()


TODO, NOW, DONE, SKIPPED, FAILED, STOPPED = "todo", "now", "done", "skipped", "failed", "stopped"


@dataclass
class LaneState:
    lane: int
    status: str = "waiting"  # "pumping", "timer", "review", "sync", "done", "stopped", "idle"
    top: int | None = None
    path: tuple = ()
    title: str = ""
    detail: str = ""
    started: float = 0.0
    ml: float = 0.0  # delivered in the current step
    background: list = field(default_factory=list)  # titles of its background steps running now


class RunState:
    """What a run is doing, step by step and lane by lane, for the Running view and the history."""

    def __init__(self, name, steps, flat, plan=None, clock=time.time):
        self.name = name
        self.steps = normalize_all(steps)
        self.flat = flat
        self.plan = plan
        self.clock = clock
        self.started = clock()
        self.ended = None
        self.outcome = None  # "done", "stopped", "failed"
        self.why = ""
        self.lanes = {ln: LaneState(ln) for ln in LANES}
        self._todo = {}  # (top, lane) -> flat steps left in that cell
        for item in flat:
            for lane in item.lanes:
                key = (item.top, lane)
                self._todo[key] = self._todo.get(key, 0) + 1
        self.cells = {key: TODO for key in self._todo}
        self._mixed = {}  # (top, lane) -> outcomes so far
        for ln in LANES:
            if not any(lane == ln for (_t, lane) in self._todo):
                self.lanes[ln].status = "idle"
        self.delivered = {}  # head label -> mL
        self.stopped_at = ""
        self.stopping = False  # set by the engine while it stops what's left

    # changes, from the engine
    def begin(self, lane, item, detail=""):
        ls = self.lanes[lane]
        ls.top, ls.path, ls.title = item.top, item.path, title(item.step)
        ls.detail, ls.started, ls.ml = detail, self.clock(), 0.0
        ls.status = {"dose": "pumping", "staccato": "pumping", "wait": "timer"}.get(
            item.step["kind"], item.step["kind"]
        )
        if self.cells.get((item.top, lane)) == TODO:
            self.cells[(item.top, lane)] = NOW

    def end(self, lane, item, outcome=DONE):
        key = (item.top, lane)
        if key not in self._todo:
            return
        self._todo[key] -= 1
        self._mixed.setdefault(key, []).append(outcome)
        if outcome in (FAILED, STOPPED):
            self.cells[key] = outcome
        elif self._todo[key] <= 0:
            got = self._mixed[key]
            self.cells[key] = SKIPPED if all(o == SKIPPED for o in got) else DONE

    def background(self, lane, item, running):
        bgs = self.lanes[lane].background
        text = title(item.step)
        if running:
            bgs.append(text)
            if self.cells.get((item.top, lane)) == TODO:
                self.cells[(item.top, lane)] = NOW
        elif text in bgs:
            bgs.remove(text)

    def lane_finished(self, lane):
        self.lanes[lane].status = "done"
        self.lanes[lane].title = ""
        self.lanes[lane].detail = ""

    def finish(self, outcome, why=""):
        self.ended = self.clock()
        self.outcome = outcome
        self.why = why
        if outcome != "done":
            where = [
                f"step {ls.top + 1} ({lanes_text([ln])})"
                for ln, ls in self.lanes.items()
                if ls.top is not None and ls.status not in ("done", "idle")
            ]
            self.stopped_at = ", ".join(where)
            for key, state in self.cells.items():
                if state == NOW:
                    self.cells[key] = STOPPED if outcome == "stopped" else FAILED
            for ls in self.lanes.values():
                if ls.status not in ("done", "idle"):
                    ls.status = "stopped"

    def add_delivered(self, label, ml, lane=None, background=False):
        if ml > 0:
            self.delivered[label] = self.delivered.get(label, 0.0) + ml
            if lane in self.lanes and not background:
                self.lanes[lane].ml += ml

    # reading, for the Running view
    def elapsed(self):
        return (self.ended or self.clock()) - self.started

    def progress(self):
        if not self.cells:
            return 1.0
        done = sum(1 for s in self.cells.values() if s in (DONE, SKIPPED))
        return done / len(self.cells)

    def left_s(self):
        """About how long until it finishes (from the plan), or None."""
        if self.plan is None or self.ended:
            return None if self.plan is None else 0.0
        now = self.clock()
        worst = 0.0
        for ln in LANES:
            left = 0.0
            for (lane, top), seconds in self.plan.lane_step_s.items():
                if lane != ln:
                    continue
                state = self.cells.get((top, lane))
                if state == TODO:
                    left += seconds
                elif state == NOW:
                    ls = self.lanes[ln]
                    spent = now - ls.started if ls.top == top else 0.0
                    left += max(0.0, seconds - spent)
            worst = max(worst, left)
        return worst

    def step_count(self):
        return len(self.steps)

    def next_title(self, lane):
        ls = self.lanes[lane]
        if ls.path:
            later = [it for it in self.flat if lane in it.lanes and it.path > ls.path]
        else:
            later = [it for it in self.flat if lane in it.lanes]
        return title(later[0].step) if later else ""

    def record(self):
        """One line of the run history."""
        return {
            "name": self.name,
            "started": datetime.fromtimestamp(self.started).isoformat(timespec="seconds"),
            "ended": datetime.fromtimestamp(self.ended or self.clock()).isoformat(timespec="seconds"),
            "seconds": round(self.elapsed(), 1),
            "outcome": self.outcome or "stopped",
            "why": self.why,
            "steps": len(self.steps),
            "stopped_at": self.stopped_at,
            "done": sum(1 for s in self.cells.values() if s == DONE),
            "skipped": sum(1 for s in self.cells.values() if s == SKIPPED),
            "cells": len(self.cells),
            "delivered": {k: round(v, 2) for k, v in self.delivered.items()},
        }


# --- run history (one file per computer, in the logs folder) -----------------------------

KEEP_RUNS = 500


def history_path(log_dir, computer):
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", computer or "this-computer")
    return os.path.join(log_dir, f"runs-{safe}.jsonl")


def append_history(path, record):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    lines = _read_lines(path)
    if len(lines) > KEEP_RUNS * 2:  # trim now and then
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(line + "\n" for line in lines[-KEEP_RUNS:])
        os.replace(tmp, path)


def _read_lines(path):
    try:
        with open(path, encoding="utf-8") as f:
            return [line.rstrip("\n") for line in f if line.strip()]
    except OSError:
        return []


def load_history(path, limit=20):
    """The latest runs, newest first."""
    out = []
    for line in reversed(_read_lines(path)):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("name") is not None:
            out.append(rec)
        if len(out) >= limit:
            break
    return out


def history_text(rec):
    """'Tue 28 Oct 15:30 · 3 h 32 min · done' or '… · stopped at step 3 (CH2)'."""
    try:
        when = datetime.fromisoformat(rec.get("started", "")).strftime("%a %d %b %H:%M")
    except (TypeError, ValueError):
        when = "?"
    outcome = rec.get("outcome", "?")
    text = f"{when} · {fmt_time(rec.get('seconds', 0))} · "
    if outcome == "done":
        text += "finished"
        if rec.get("skipped"):
            text += f" ({rec['skipped']} skipped)"
    else:
        text += outcome + (f" at {rec['stopped_at']}" if rec.get("stopped_at") else "")
    return text


def last_runs_by_name(path):
    """name -> the latest run's start time (ISO)."""
    out = {}
    for line in _read_lines(path):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("name"):
            out[rec["name"]] = rec.get("started", "")
    return out
