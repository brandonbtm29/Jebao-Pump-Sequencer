"""The physical arrangement of the pumps: stacks of pumps, as they sit on the bench.

A layout is plain data, saved in ``jebao_config.json`` under ``"rack"``::

    {"name": "Lab bench", "slots_per_stack": 5,
     "stacks": [{"name": "Stack A", "slots": ["did1", "did2", None, ...]}, ...]}

Each pump (by its cloud device id, ``did``) is in at most one slot. Pumps on the
account that are in no slot are "not placed". The Manual and Sequencer tabs show the
pumps in this order. No Qt here, so it can be tested on its own.
"""

import copy
import re

DEFAULT_NAME = "Lab bench"
MAX_STACKS = 8
MAX_SLOTS = 12


def natural_key(text):
    """Sort key that orders numbers by value: 'JP 2' comes before 'JP 10'."""
    return [
        (0, int(part), "") if part.isdigit() else (1, 0, part.lower())
        for part in re.split(r"(\d+)", text or "")
    ]


def stack_name(index):
    """'Stack A', 'Stack B', ..."""
    return "Stack " + chr(ord("A") + index) if index < 26 else f"Stack {index + 1}"


def default_layout(dids):
    """One stack holding every pump, in the given order (used the first time 2.0 runs)."""
    dids = list(dict.fromkeys(d for d in dids if d))
    n = max(1, min(len(dids), MAX_SLOTS)) if dids else 4
    stacks = []
    for start in range(0, max(len(dids), 1), n):
        chunk = dids[start : start + n]
        stacks.append({"name": stack_name(len(stacks)), "slots": chunk + [None] * (n - len(chunk))})
    return normalize({"name": DEFAULT_NAME, "slots_per_stack": n, "stacks": stacks[:MAX_STACKS]})


def normalize(layout):
    """A clean copy: sizes in range, every stack exactly ``slots_per_stack`` long, no pump twice."""
    layout = copy.deepcopy(layout) if isinstance(layout, dict) else {}
    try:
        n = int(layout.get("slots_per_stack") or 0)
    except (TypeError, ValueError):
        n = 0
    stacks = [s for s in layout.get("stacks") or [] if isinstance(s, dict)]
    if n < 1:
        n = max([len(s.get("slots") or []) for s in stacks] + [1])
    n = max(1, min(n, MAX_SLOTS))
    if not stacks:
        stacks = [{"name": stack_name(0), "slots": []}]
    stacks = stacks[:MAX_STACKS]
    seen = set()
    clean = []
    for i, s in enumerate(stacks):
        slots = []
        for did in list(s.get("slots") or [])[:n]:
            if did and did not in seen:
                seen.add(did)
                slots.append(did)
            else:
                slots.append(None)
        slots += [None] * (n - len(slots))
        clean.append({"name": str(s.get("name") or stack_name(i)), "slots": slots})
    return {"name": str(layout.get("name") or DEFAULT_NAME), "slots_per_stack": n, "stacks": clean}


def placed(layout):
    """Pump ids in slots, stack by stack, top to bottom."""
    return [d for s in layout["stacks"] for d in s["slots"] if d]


def unplaced(layout, dids):
    """Pump ids on the account that are in no slot, in the order given."""
    inside = set(placed(layout))
    return [d for d in dids if d and d not in inside]


def find(layout, did):
    """(stack, slot) of a pump, or None."""
    for si, s in enumerate(layout["stacks"]):
        for i, d in enumerate(s["slots"]):
            if d == did:
                return si, i
    return None


def place(layout, did, stack, slot):
    """Put a pump in a slot. A pump already there swaps to where this one came from
    (or goes to "not placed" if this one came from there). Returns a new layout."""
    layout = normalize(layout)
    if not (0 <= stack < len(layout["stacks"]) and 0 <= slot < layout["slots_per_stack"]):
        raise IndexError("No such slot.")
    origin = find(layout, did)
    occupant = layout["stacks"][stack]["slots"][slot]
    if origin is not None:
        layout["stacks"][origin[0]]["slots"][origin[1]] = occupant if occupant != did else None
    layout["stacks"][stack]["slots"][slot] = did
    return layout


def remove(layout, did):
    """Take a pump out of the rack (back to "not placed")."""
    layout = normalize(layout)
    where = find(layout, did)
    if where:
        layout["stacks"][where[0]]["slots"][where[1]] = None
    return layout


def resize(layout, stacks, slots_per_stack):
    """Change the number of stacks and pumps per stack. Pumps that no longer fit become "not placed"."""
    layout = normalize(layout)
    stacks = max(1, min(int(stacks), MAX_STACKS))
    n = max(1, min(int(slots_per_stack), MAX_SLOTS))
    out = []
    for i in range(stacks):
        old = layout["stacks"][i] if i < len(layout["stacks"]) else {"name": stack_name(i), "slots": []}
        slots = (old["slots"] + [None] * n)[:n]
        out.append({"name": old["name"], "slots": slots})
    return normalize({"name": layout["name"], "slots_per_stack": n, "stacks": out})


def auto_arrange(layout, dids, names):
    """Fill the stacks with every pump in name order (JP 1, JP 2, … JP 10), top to bottom,
    left to right. Pumps that don't fit stay "not placed". ``names`` maps did → name."""
    layout = normalize(layout)
    ordered = sorted(dict.fromkeys(d for d in dids if d), key=lambda d: natural_key(names.get(d, d)))
    it = iter(ordered)
    for s in layout["stacks"]:
        s["slots"] = [next(it, None) for _ in range(layout["slots_per_stack"])]
    return layout


def order(layout, dids):
    """Every pump id: the placed ones in rack order, then the not-placed ones."""
    return placed(layout) + unplaced(layout, dids)
