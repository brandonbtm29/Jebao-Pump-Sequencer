"""Sequences: steps, older steps, plans (times, on-time limit rests, checks) and run state."""

from datetime import date

import pytest

import pumpguard
import rack
import sequence as sq

MIN = 60.0


def ctx(rates=None, online=None, dates=None, saved=None, names=None, limit=None, known=None, **more):
    rates = rates if rates is not None else {}
    names = names or {}
    return sq.Context(
        rate=lambda did, ch: rates.get((did, ch), rates.get(did)),
        calibrated_at=lambda did, ch: (dates or {}).get((did, ch)),
        online=lambda did: (online or {}).get(did, True),
        limit=limit or (lambda key: pumpguard.Limit(enabled=False)),
        saved=lambda name: (saved or {}).get(name),
        head_name=lambda did, ch: names.get((did, ch), ""),
        pump_name=lambda did: {"A": "JP 1", "B": "JP 2", "C": "JP 3"}.get(did, did),
        known=known,
        today=date(2026, 10, 30),
        **more,
    )


def dose(heads, ml=None, seconds=None, background=False, label=""):
    step = sq.new_step("dose", heads=heads)
    if seconds is not None:
        step.update(by="time", seconds=seconds)
    else:
        step["volume"] = ml
    step.update(background=background, label=label)
    return step


# --- steps and older steps ------------------------------------------------------------------


def test_older_steps_are_converted():
    old = [
        {"pump_did": "A", "channel": [1, 2], "action": "Dose (Async)", "val1": 6.0, "val2": 0, "val3": 0},
        {"pump_did": "A", "channel": [3], "action": "Staccato", "val1": 250, "val2": 5, "val3": 20},
        {"pump_did": "A", "channel": [1], "action": "Pause", "val1": 30.0, "val2": 0, "val3": 0},
        {"pump_did": None, "channel": [1, 3], "action": "Pause (User Review)", "val1": 0},
        {"pump_did": "A", "channel": [1, 2, 3, 4], "action": "Wait for All", "val1": 0},
        {"pump_did": "A", "channel": 2, "action": "Run Sub-Sequence", "val1": "Elute"},
    ]
    new = sq.normalize_all(old)
    assert [s["kind"] for s in new] == ["dose", "staccato", "wait", "review", "sync", "sequence"]
    assert new[0]["heads"] == [["A", 1], ["A", 2]] and new[0]["background"] and new[0]["volume"] == 6.0
    assert (new[1]["volume"], new[1]["burst"], new[1]["gap"]) == (250, 5, 20)
    assert new[2]["seconds"] == 30.0 and new[2]["lanes"] == [1]
    assert new[3]["lanes"] == [1, 3]
    assert new[4]["lanes"] == [1, 2, 3, 4]
    assert new[5]["name"] == "Elute" and new[5]["lanes"] == [2]


def test_saved_steps_keep_the_old_fields_for_older_copies_of_the_app():
    steps = [
        dose([["A", 1], ["A", 2]], ml=6, background=True),
        sq.new_step("wait", lanes=[2]),
        {**sq.new_step("sequence"), "name": "Elute"},
    ]
    saved = sq.to_saved_all(steps)
    assert saved[0]["action"] == "Dose (Async)" and saved[0]["pump_did"] == "A"
    assert saved[0]["channel"] == [1, 2] and saved[0]["val1"] == 6
    assert saved[1]["action"] == "Pause" and saved[1]["channel"] == [2] and saved[1]["val1"] == 60.0
    assert saved[2]["action"] == "Run Sub-Sequence" and saved[2]["val1"] == "Elute"
    assert sq.normalize_all(saved) == sq.normalize_all(steps)  # the new fields win
    assert sq.same_steps(saved, steps)


def test_bad_values_dont_break_a_step():
    step = sq.normalize({"kind": "dose", "heads": [["A", 9], ["A", "2"], "x"], "volume": "lots"})
    assert step["heads"] == [["A", 2]] and step["volume"] == 5.0
    assert sq.normalize("nonsense")["kind"] == "wait"


def test_titles_and_names():
    names = {("A", c): f"EtOH → Col {c}" for c in range(1, 5)}
    step = dose([["A", c] for c in range(1, 5)], ml=6, label="Condition")
    assert sq.title(step) == "Dose 6 mL · Condition"
    assert sq.heads_text(step, ctx(names=names)) == "EtOH → Col 1–4 (JP 1 · CH1–4)"
    assert sq.heads_text(dose([["B", 1], ["B", 3]], ml=1), ctx()) == "JP 2 · CH1, 3"
    stac = {**sq.new_step("staccato"), "volume": 250, "burst": 5, "gap": 20}
    assert sq.describe(stac) == "Staccato 250 mL in 5 mL bursts, 20 s apart"
    assert sq.describe(dose([], seconds=144)) == "Dose 2 min 24 s"
    assert sq.fmt_time(3 * 3600 + 5 * 60) == "3 h 05 min" and sq.fmt_time(45) == "45 s"
    assert sq.compress_numbers([1, 2, 4]) == "1–2, 4"
    assert sq.short_head_name("EtOH → Col 3", 3) == "Col 3" and sq.short_head_name("", 2) == "CH2"
    assert sq.liquid_of("", "JP 4: MeOH") == "MeOH"


# --- plans ------------------------------------------------------------------------------------


def test_lanes_run_side_by_side_and_sync_lines_them_up():
    # CH1 pumps 2 min, CH2 pumps 1 min, then they sync and both wait 30 s.
    steps = [
        dose([["A", 1]], ml=2),
        dose([["A", 2]], ml=1),
        sq.new_step("sync", lanes=[1, 2]),
        {**sq.new_step("wait", lanes=[1, 2]), "seconds": 30},
    ]
    p = sq.plan(steps, ctx(rates={"A": 1.0}))
    assert p.total_s == pytest.approx(150)
    assert p.step_s[0] == pytest.approx(120) and p.step_s[1] == pytest.approx(60)
    assert p.lane_step_s[(2, 2)] == pytest.approx(60)  # CH2 waits for CH1
    assert any(seg.kind == "sync" for seg in p.lanes[2])
    assert p.used_lanes == [1, 2]
    assert p.heads[("A", 1)].volume_ml == pytest.approx(2.0)


def test_mixed_pumps_in_one_lane_run_together():
    steps = [dose([["A", 1], ["B", 1]], ml=3)]
    p = sq.plan(steps, ctx(rates={"A": 1.0, "B": 3.0}))
    assert p.total_s == pytest.approx(180)  # the slower head
    assert p.liquids == {"JP 1": pytest.approx(3.0), "JP 2": pytest.approx(3.0)}


def test_background_step_overlaps_and_sync_waits_for_it():
    steps = [
        dose([["A", 1]], ml=5, background=True),
        {**sq.new_step("wait", lanes=[1]), "seconds": 60},
        sq.new_step("sync", lanes=[1]),
    ]
    p = sq.plan(steps, ctx(rates={"A": 1.0}))
    assert p.total_s == pytest.approx(300)
    assert p.step_s[0] == pytest.approx(300)  # its own time on the card
    assert p.lane_step_s[(1, 2)] == pytest.approx(240)  # the sync waits for the background dose


def test_staccato_bursts_and_gaps():
    stac = {**sq.new_step("staccato", heads=[["A", 1]]), "volume": 10, "burst": 4, "gap": 30}
    assert sq.pump_pattern(stac, 2.0) == [
        ("pump", 120.0),
        ("gap", 30),
        ("pump", 120.0),
        ("gap", 30),
        ("pump", 60.0),
    ]
    assert sq.burst_count(stac) == 3
    p = sq.plan([stac], ctx(rates={"A": 2.0}))
    assert p.total_s == pytest.approx(360)


def test_long_dose_rests_for_the_on_time_limit():
    # 45 min of pumping, limit 30 min in any 60 min: 30 min on, rest until 5 min of budget is
    # free again (at 65 min), then the last 15 min: done at 80 min. The pump guard does the same.
    limit = pumpguard.Limit(on_s=30 * MIN, window_s=60 * MIN)
    steps = [dose([["A", 1]], seconds=45 * MIN)]
    p = sq.plan(steps, ctx(rates={"A": 1.0}, limit=lambda key: limit))
    assert p.total_s == pytest.approx(80 * MIN, abs=2)
    assert p.step_rest_s[0] == pytest.approx(35 * MIN, abs=2)
    assert [seg.kind for seg in p.lanes[1]] == ["pump", "rest", "pump"]
    assert any(c.level == "info" and "on-time limit" in c.text for c in p.checks)
    without = sq.plan(steps, ctx(rates={"A": 1.0}, limit=lambda key: limit), with_limit=False)
    assert without.total_s == pytest.approx(45 * MIN)
    assert sq.resting_need(limit) == 5 * MIN


def test_sub_sequences_open_up_on_their_lanes_and_cant_run_themselves():
    elute = sq.to_saved_all([dose([["A", 1], ["A", 2]], ml=1)])
    loop = sq.to_saved_all([{**sq.new_step("sequence"), "name": "Loop"}])
    saved = {"Elute": elute, "Loop": loop}
    steps = [{**sq.new_step("sequence", lanes=[2]), "name": "Elute"}]
    flat, checks = sq.flatten(steps, ctx(saved=saved))
    assert [(f.path, f.lanes) for f in flat] == [((0, 0), (2,))]  # only CH2 goes in
    assert not checks
    _, checks = sq.flatten([{**sq.new_step("sequence"), "name": "Loop"}], ctx(saved=saved))
    assert checks and "run itself" in checks[0].text
    _, checks = sq.flatten([{**sq.new_step("sequence"), "name": "Gone"}], ctx(saved=saved))
    assert checks and "no saved sequence" in checks[0].text


def test_checks_before_a_run():
    steps = [
        dose([], ml=1),
        dose([["A", 1]], ml=1),
        dose([["B", 2]], ml=1),
        dose([["C", 3]], ml=0),
    ]
    p = sq.plan(
        steps,
        ctx(
            rates={"A": 2.0, "B": 1.0},
            online={"A": True, "B": True, "C": False},
            dates={("A", 1): "2026-09-01"},
            known=lambda did: True,
        ),
    )
    texts = [(c.level, c.text) for c in p.checks]
    assert ("error", "Step 1: no heads picked.") in texts
    assert any(lvl == "warn" and "calibrated 59 days ago" in t for lvl, t in texts)
    assert any(lvl == "warn" and "default 1 mL/min" in t for lvl, t in texts)
    assert any(lvl == "error" and "the volume is 0 mL" in t for lvl, t in texts)
    assert any(lvl == "error" and "no flow rate" in t for lvl, t in texts)  # C has none
    assert any(lvl == "warn" and "JP 3 is offline" in t for lvl, t in texts)
    assert not any(lvl == "ok" and "online" in t for lvl, t in texts)
    assert sq.plan([], ctx()).errors


def test_time_doses_dont_need_a_rate():
    p = sq.plan([dose([["A", 1]], seconds=30)], ctx(known=lambda did: True))
    assert p.total_s == pytest.approx(30) and not p.errors
    assert any(c.level == "warn" and "no flow rate" in c.text for c in p.checks)


# --- a run in progress -----------------------------------------------------------------------


def test_run_state_tracks_cells_and_how_it_stopped():
    steps = [dose([["A", 1], ["A", 2]], ml=1), sq.new_step("wait", lanes=[1, 2])]
    c = ctx(rates={"A": 1.0})
    p = sq.plan(steps, c)
    now = [1000.0]
    run = sq.RunState("Test", steps, p.flat, plan=p, clock=lambda: now[0])
    assert run.cells == {(0, 1): "todo", (0, 2): "todo", (1, 1): "todo", (1, 2): "todo"}
    assert 3 not in run.lanes  # only the lanes the sequence uses
    first = p.flat[0]
    run.begin(1, first)
    run.begin(2, first)
    assert run.cells[(0, 1)] == "now" and run.lanes[1].status == "pumping"
    now[0] += 30
    assert run.left_s() == pytest.approx(30 + 60)
    run.end(1, first, "done")
    run.end(2, first, "skipped")
    assert run.cells[(0, 1)] == "done" and run.cells[(0, 2)] == "skipped"
    assert run.next_title(1) == "Wait 1 min"
    run.begin(1, p.flat[1])
    run.add_delivered("JP 1 · CH1", 1.0)
    run.finish("stopped", "you pressed Stop")
    rec = run.record()
    assert rec["outcome"] == "stopped" and "step 2 (CH1)" in rec["stopped_at"]
    assert rec["delivered"] == {"JP 1 · CH1": 1.0} and rec["skipped"] == 1
    assert run.cells[(1, 1)] == "stopped"
    assert "stopped at step 2 (CH1)" in sq.history_text(rec)


def test_run_history_is_kept_per_computer(tmp_path, monkeypatch):
    path = sq.history_path(str(tmp_path), "LAB PC/1")
    assert path.endswith("runs-LAB_PC_1.jsonl")
    monkeypatch.setattr(sq, "KEEP_RUNS", 3)
    for i in range(8):
        sq.append_history(path, {"name": f"run {i}", "started": "2026-10-30T15:30:00", "outcome": "done"})
    with open(path, "a", encoding="utf-8") as f:
        f.write("not json\n")
    runs = sq.load_history(path, limit=10)
    assert [r["name"] for r in runs][0] == "run 7" and len(runs) <= 6
    assert sq.last_runs_by_name(path)["run 7"] == "2026-10-30T15:30:00"
    assert sq.load_history(str(tmp_path / "none.jsonl")) == []


def test_steps_an_older_copy_cant_run_exactly_make_it_ask():
    mixed = dose([["A", 1], ["B", 2]], ml=1)
    timed = dose([["A", 1]], seconds=30)
    for step in (mixed, timed):
        saved = sq.to_saved(step)
        assert saved["action"] == "Pause (User Review)" and saved["pump_did"] is None
        assert sq.normalize(saved) == sq.normalize(step)  # this version still reads it fully
    assert sq.to_saved(dose([["A", 1], ["A", 3]], ml=1))["action"] == "Dose"
    p = sq.plan([mixed], ctx(rates={"A": 1.0, "B": 1.0}))
    assert any("version 2.7" in c.text for c in p.checks)


def test_unknown_old_steps_do_nothing():
    step = sq.normalize({"pump_did": "A", "channel": [1], "action": "Flush", "val1": 5})
    assert step["kind"] == "wait" and step["seconds"] == 0 and "Flush" in step["label"]


def test_odd_values_are_clamped():
    step = sq.normalize({"kind": "dose", "heads": [["A", 2.0], ["A", "x"], {"did": "A"}], "volume": "1e999"})
    assert step["heads"] == [["A", 2]] and step["volume"] == 5.0
    assert sq.normalize({"kind": "wait", "seconds": 1e12, "lanes": 3.0})["seconds"] == sq.MAX_S
    assert sq.normalize({"kind": "wait", "lanes": 3.0})["lanes"] == [3]
    huge = {**sq.new_step("staccato", heads=[["A", 1]]), "volume": 1e308, "burst": 1e-300}
    huge = sq.normalize(huge)
    assert sq.step_problem(huge).startswith("1000000000000 bursts is too many")
    assert sq.pump_pattern(huge, 1.0) is None


def test_problem_steps_are_named():
    assert sq.step_problem(dose([], ml=1)) == "no heads picked"
    assert sq.step_problem(dose([["A", 1]], ml=0)) == "the volume is 0 mL"
    zero = {**sq.new_step("staccato", heads=[["A", 1]]), "burst": 0}
    assert sq.step_problem(zero) == "the burst size is 0 mL"
    assert sq.pump_pattern(zero, 1.0) is None  # never one long run instead


def test_long_staccato_plans_quickly():
    import time

    limit = pumpguard.Limit(on_s=30 * MIN, window_s=60 * MIN)
    many = {
        **sq.new_step("staccato", heads=[["A", c] for c in range(1, 5)]),
        "volume": 250,
        "burst": 0.25,
        "gap": 5,
    }
    started = time.monotonic()
    p = sq.plan([many] * 3, ctx(rates={"A": 2.4}, limit=lambda key: limit))
    assert time.monotonic() - started < 3.0
    assert p.total_s > 3 * 250 / 2.4 * 60  # rests counted
    too_many = {**many, "burst": 0.01}
    p = sq.plan([too_many], ctx(rates={"A": 2.4}, limit=lambda key: limit))
    assert any("too many" in c.text for c in p.errors) and 0 in p.unknown


def test_wait_for_me_keeps_its_message():
    step = sq.normalize(
        {**sq.new_step("review", lanes=[1, 2]), "message": "  Move the lines to the tubes.\nThen continue.  "}
    )
    assert step["message"] == "Move the lines to the tubes.\nThen continue."
    assert sq.where_text(step, ctx()) == "CH1–2 stop here · “Move the lines to the tubes.”"
    assert sq.normalize(sq.to_saved(step))["message"] == step["message"]
    assert sq.to_saved(step)["action"] == "Pause (User Review)"  # older copies still stop there
    assert sq.normalize({**sq.new_step("wait"), "message": "x"})["message"] == ""  # only on Wait for me
    long = sq.normalize({**sq.new_step("review"), "message": "x" * 5000})
    assert len(long["message"]) == sq.MAX_MESSAGE
    assert sq.normalize({**sq.new_step("review"), "message": {"bad": 1}})["message"] == ""


def two_stacks(**kw):
    """Pump A in Stack A (CH1–4), pump B in Stack B (CH5–8)."""
    groups = {"A": 0, "B": 1}
    return ctx(
        lane_of=lambda did, ch: rack.channel_number(groups, did, ch),
        all_lanes=lambda: list(range(1, 9)),
        **kw,
    )


def test_stack_b_heads_are_ch5_to_8_and_run_as_their_own_lanes():
    c = two_stacks(rates={"A": 1.0, "B": 1.0}, names={("B", 1): "Sample → Col 5"})
    steps = [dose([["A", 1]], ml=2), dose([["B", 1]], ml=1)]
    p = sq.plan(steps, c)
    assert p.used_lanes == [1, 5]
    assert p.total_s == pytest.approx(120)  # side by side: before, the same lane, one after the other
    assert sq.heads_text(steps[1], c) == "Sample → Col 5 (JP 2 · CH5)"
    assert c.label("B", 4) == "JP 2 · CH8"
    assert sq.heads_in_lane(dose([["A", 1], ["B", 1]], ml=1), 5, c) == [("B", 1)]


def test_older_steps_cover_the_stacks_the_sequence_pumps_on():
    c = two_stacks()
    old_review = {"pump_did": None, "channel": [1, 3], "action": "Pause (User Review)", "val1": 0}
    assert sq.lanes_of(sq.normalize(old_review), c) == [1, 3, 5, 7]  # CH1 then meant CH1 and CH5
    only_a = [dose([["A", 1], ["A", 3]], ml=1), old_review]
    flat, _ = sq.flatten(only_a, c)
    assert flat[1].lanes == (1, 3)  # no Stack B heads in it: no empty CH5 and CH7 lanes
    both = [dose([["A", 1], ["B", 1]], ml=1), old_review]
    flat, _ = sq.flatten(both, c)
    assert flat[1].lanes == (1, 5)  # CH3 has no pumping here either
    assert sq.lanes_of(sq.normalize(old_review), sq.for_steps(c, both)) == [1, 5]
    from_28 = {**sq.new_step("sync"), "lanes": [2]}
    from_28.pop("cols")
    assert sq.lanes_of(sq.normalize(from_28), c) == [2, 6]
    new = sq.new_step("review", cols=[5, 6])
    assert sq.lanes_of(new, c) == [5, 6] and new["lanes"] == [1, 2]
    saved = sq.to_saved(new)
    assert saved["cols"] == [5, 6] and saved["lanes"] == [1, 2] and saved["channel"] == [1, 2]
    assert sq.normalize(saved) == sq.normalize(new)
    assert sq.normalize({**new, "cols": [0, 99, "x", 7.0]})["cols"] == [7]


def test_a_review_on_stack_b_only_stops_stack_b():
    c = two_stacks(rates={"A": 1.0, "B": 1.0})
    steps = [
        dose([["A", 1], ["B", 1]], ml=1),
        sq.new_step("review", cols=[5]),
        dose([["A", 1], ["B", 1]], ml=1),
    ]
    flat, _ = sq.flatten(steps, c)
    assert [(f.top, f.lanes) for f in flat] == [(0, (1, 5)), (1, (5,)), (2, (1, 5))]
