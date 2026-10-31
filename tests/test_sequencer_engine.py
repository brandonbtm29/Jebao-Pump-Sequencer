"""Running sequences: lanes side by side, skip, pause, stop, failures, Wait for me, sync,
background steps, the queue and the run history."""

import asyncio
import time

import pytest

import sequence as sq


@pytest.fixture
def pumps(window, monkeypatch):
    """Two online pumps at 60 mL/min (1 mL a second) with fake cloud commands."""
    window.cloud_devices = [
        {"did": "A", "dev_alias": "JP 1", "is_online": True},
        {"did": "B", "dev_alias": "JP 2", "is_online": True},
    ]
    for did in ("A", "B"):
        window.config.get_pump(did)["calibration"] = [60.0] * 4
    sent = []
    fail = set()

    async def fake_send(did, attrs):
        sent.append((did, dict(attrs)))
        return not any((did, k, v) in fail for k, v in attrs.items())

    monkeypatch.setattr(window, "send_cloud_command", fake_send)
    return window, sent, fail


def dose(heads, ml, background=False):
    return {**sq.new_step("dose", heads=heads), "volume": ml, "background": background}


def wait(seconds, lanes):
    return {**sq.new_step("wait", lanes=lanes), "seconds": seconds}


def run(loop, window, steps, during=None, name="test"):
    """Run a sequence to the end; ``during(task)`` is a coroutine run alongside it."""

    async def go():
        window.seq_run = None
        task = asyncio.ensure_future(window._async_run_sequence(name, sq.to_saved_all(steps)))
        window.sequence_task = task
        if during is not None:
            await until(lambda: window.seq_run is not None)
            await during(task)
        try:
            return await task
        except asyncio.CancelledError:
            return "cancelled"

    return loop.run_until_complete(go())


async def until(check, timeout=5.0):
    end = time.monotonic() + timeout
    while not check():
        if time.monotonic() > end:
            raise AssertionError("timed out")
        await asyncio.sleep(0.02)


def offs_and_ons(sent, did, ch):
    return [a[f"channe{ch}"] for d, a in sent if d == did and f"channe{ch}" in a]


def test_lanes_run_side_by_side_and_sync(pumps, qt_loop):
    window, sent, _ = pumps
    _, loop = qt_loop
    steps = [
        dose([["A", 1]], 0.4),
        dose([["A", 2]], 0.1),
        sq.new_step("sync", lanes=[1, 2]),
        wait(0.1, [1, 2]),
    ]
    started = time.monotonic()
    outcome = run(loop, window, steps)
    took = time.monotonic() - started
    assert outcome == "done"
    assert 0.4 < took < 2.0  # side by side, not one after the other
    assert offs_and_ons(sent, "A", 1) == [True, False] and offs_and_ons(sent, "A", 2) == [True, False]
    r = window.seq_run
    assert r.outcome == "done" and all(v == "done" for v in r.cells.values())
    assert r.delivered["JP 1 · CH1"] == pytest.approx(0.4, abs=0.15)
    assert window.head_runs == {}
    history = sq.load_history(window.run_history_path)
    assert history[0]["name"] == "test" and history[0]["outcome"] == "done"


def test_mixed_pumps_in_one_lane_run_together(pumps, qt_loop):
    window, sent, _ = pumps
    _, loop = qt_loop
    assert run(loop, window, [dose([["A", 1], ["B", 1]], 0.2)]) == "done"
    assert offs_and_ons(sent, "A", 1) == [True, False] and offs_and_ons(sent, "B", 1) == [True, False]


def test_skip_stops_that_step_and_the_lane_goes_on(pumps, qt_loop):
    window, sent, _ = pumps
    _, loop = qt_loop
    steps = [dose([["A", 1], ["A", 2]], 30), dose([["A", 1], ["A", 2]], 0.1)]

    async def during(task):
        await until(lambda: ("A", 1) in window.head_runs)
        assert window.skip_step(1)  # CH1 only

    started = time.monotonic()
    loop_task = None

    async def during_both(task):
        nonlocal loop_task
        await during(task)
        await until(lambda: ("A", 2) in window.head_runs and window.seq_run.cells[(0, 1)] == "skipped")
        assert window.skip_step(2)

    outcome = run(loop, window, steps, during_both)
    assert outcome == "done" and time.monotonic() - started < 5
    r = window.seq_run
    assert r.cells[(0, 1)] == "skipped" and r.cells[(0, 2)] == "skipped"
    assert r.cells[(1, 1)] == "done" and r.cells[(1, 2)] == "done"
    assert offs_and_ons(sent, "A", 1) == [True, False, True, False]  # skipped dose turned off, next ran
    assert loop_task is None


def test_pausing_a_lane_turns_its_heads_off_until_resumed(pumps, qt_loop):
    window, sent, _ = pumps
    _, loop = qt_loop

    async def during(task):
        await until(lambda: ("A", 1) in window.head_runs)
        window.pause_lane(1, True)
        await until(lambda: offs_and_ons(sent, "A", 1)[-1] is False)
        await asyncio.sleep(0.3)
        assert window.run_state("A", 1)[0] == "paused"
        assert not task.done()
        window.pause_lane(1, False)

    assert run(loop, window, [dose([["A", 1]], 0.4)], during) == "done"
    assert offs_and_ons(sent, "A", 1) == [True, False, True, False]  # on, paused, on again, done


def test_paused_lane_waits_between_steps_and_its_timers_stop(pumps, qt_loop):
    window, _sent, _ = pumps
    _, loop = qt_loop

    async def during(task):
        await until(lambda: window.seq_run is not None and window.seq_run.lanes[1].status == "timer")
        window.pause_lane(1, True)
        await asyncio.sleep(0.5)
        assert not task.done()  # the 0.3 s timer is held
        window.pause_lane(1, False)

    started = time.monotonic()
    assert run(loop, window, [wait(0.3, [1])], during) == "done"
    assert time.monotonic() - started > 0.7


def test_stop_run_turns_everything_off_and_pauses_the_queue(pumps, qt_loop):
    window, sent, _ = pumps
    _, loop = qt_loop
    window.sequence_queue.append({"name": "next", "data": []})
    steps = [dose([["A", 1]], 30), dose([["A", 2]], 30, background=True), wait(30, [2])]

    async def during(task):
        await until(lambda: ("A", 1) in window.head_runs and ("A", 2) in window.head_runs)
        window.stop_run()

    assert run(loop, window, steps, during) == "cancelled"
    assert offs_and_ons(sent, "A", 1)[-1] is False and offs_and_ons(sent, "A", 2)[-1] is False
    assert window.queue_paused and window.head_runs == {}
    r = window.seq_run
    assert r.outcome == "stopped" and "Stop run" in r.why and "step 1 (CH1)" in r.stopped_at
    assert sq.load_history(window.run_history_path)[0]["outcome"] == "stopped"
    window.sequence_queue.clear()
    window.queue_paused = False


def test_a_pump_that_wont_turn_on_fails_the_run_and_stops_the_rest(pumps, qt_loop, monkeypatch, caplog):
    window, sent, fail = pumps
    _, loop = qt_loop
    fail.add(("B", "channe2", True))
    real_sleep = asyncio.sleep

    async def quick(seconds):  # _turn_on waits 2 s and 5 s between tries
        await real_sleep(min(seconds, 0.01))

    monkeypatch.setattr(asyncio, "sleep", quick)
    steps = [dose([["A", 1]], 30), dose([["B", 2]], 1)]
    with caplog.at_level("ERROR", logger="JebaoGUI"):
        outcome = run(loop, window, steps)
    assert outcome == "failed"
    assert "Couldn't turn JP 2 · CH2 on" in window.seq_run.why
    assert offs_and_ons(sent, "A", 1)[-1] is False  # the other lane was stopped, and its head is off
    assert offs_and_ons(sent, "B", 2)[-1] is False
    assert window.seq_run.cells[(1, 2)] == "failed"
    assert any("failed" in r.getMessage() for r in caplog.records)


def test_wait_for_me_shows_the_banner_until_continue(pumps, qt_loop):
    window, _sent, _ = pumps
    _, loop = qt_loop
    review = {**sq.new_step("review", lanes=[1, 3]), "label": "Check no column ran dry", "sound": False}
    window.current_running_sequence = "Load sample"

    async def during(task):
        await until(lambda: window._ch_review_events.get(1) and window._ch_review_events.get(3))
        text = window.lbl_approval.text()
        assert not window.approval_banner.isHidden()
        assert "CH1, CH3" in text and "Check no column ran dry" in text
        window.approve_channel(1)
        await asyncio.sleep(0.1)
        assert not task.done()
        window.approve_channel(3)

    assert run(loop, window, [review], during) == "done"
    assert window.approval_banner.isHidden()
    window.current_running_sequence = None


def test_skipping_a_sync_lets_the_others_through(pumps, qt_loop):
    window, _sent, _ = pumps
    _, loop = qt_loop
    steps = [wait(30, [1]), sq.new_step("sync", lanes=[1, 2]), wait(0.05, [1, 2])]

    async def during(task):
        await until(lambda: window.seq_run.lanes[2].status == "sync")
        window.skip_step(1)  # CH1's long timer
        await until(lambda: window.seq_run.lanes[1].status in ("sync", "timer", "done"))

    started = time.monotonic()
    assert run(loop, window, steps, during) == "done"
    assert time.monotonic() - started < 5


def test_background_step_runs_while_the_next_starts_and_sync_waits(pumps, qt_loop):
    window, sent, _ = pumps
    _, loop = qt_loop
    order = []

    async def during(task):
        await until(lambda: ("A", 1) in window.head_runs)
        await until(lambda: window.seq_run.lanes[1].status == "timer")
        order.append("timer while background dose runs" if ("A", 1) in window.head_runs else "no")

    steps = [
        dose([["A", 1]], 0.5, background=True),
        wait(0.1, [1]),
        sq.new_step("sync", lanes=[1]),
        wait(0.05, [1]),
    ]
    assert run(loop, window, steps, during) == "done"
    assert order == ["timer while background dose runs"]
    assert window.seq_run.cells[(0, 1)] == "done"
    assert offs_and_ons(sent, "A", 1) == [True, False]


def test_failed_background_step_stops_the_run(pumps, qt_loop, monkeypatch):
    window, _sent, fail = pumps
    _, loop = qt_loop
    fail.add(("A", "channe1", True))
    real_sleep = asyncio.sleep

    async def quick(seconds):
        await real_sleep(min(seconds, 0.01))

    monkeypatch.setattr(asyncio, "sleep", quick)
    steps = [dose([["A", 1]], 1, background=True), wait(30, [1])]
    assert run(loop, window, steps) == "failed"
    assert "step 1 on CH1 failed" in window.seq_run.why


def test_cancelling_a_head_in_the_manual_tab_stops_the_run(pumps, qt_loop):
    window, sent, _ = pumps
    _, loop = qt_loop

    async def during(task):
        await until(lambda: ("A", 1) in window.head_runs and ("A", 2) in window.head_runs)
        window.cancel_heads([("A", 1)])

    assert run(loop, window, [dose([["A", 1], ["A", 2]], 30)], during) == "stopped"
    assert offs_and_ons(sent, "A", 2)[-1] is False and window.head_runs == {}


def test_emergency_stop_mid_run(pumps, qt_loop):
    window, sent, _ = pumps
    _, loop = qt_loop
    steps = [dose([["A", 1]], 30, background=True), dose([["A", 2]], 30)]

    async def during(task):
        await until(lambda: len(window.head_runs) == 2)
        await window.emergency_stop_all()

    assert run(loop, window, steps, during) == "cancelled"
    assert window.seq_run.why == "Emergency Stop" and window.head_runs == {}
    assert offs_and_ons(sent, "A", 1)[-1] is False and offs_and_ons(sent, "A", 2)[-1] is False


def test_queue_waits_while_paused_and_pauses_after_a_failed_run(pumps, qt_loop, monkeypatch):
    window, _sent, fail = pumps
    _, loop = qt_loop
    real_sleep = asyncio.sleep

    async def quick(seconds):
        await real_sleep(min(seconds, 0.01))

    monkeypatch.setattr(asyncio, "sleep", quick)
    fail.add(("A", "channe1", True))
    window.queue_paused = True
    window.enqueue("bad", [dose([["A", 1]], 0.1)])
    window.enqueue("good", [dose([["A", 2]], 0.1)])

    async def go():
        worker = asyncio.ensure_future(window._queue_worker())
        await real_sleep(0.2)
        assert window.seq_run is None or window.seq_run.name != "bad"  # paused: nothing started
        window.set_queue_paused(False)
        await until(lambda: window.seq_run is not None and window.seq_run.ended)
        await real_sleep(0.2)
        worker.cancel()

    loop.run_until_complete(go())
    assert window.seq_run.name == "bad" and window.seq_run.outcome == "failed"
    assert window.queue_paused and [q["name"] for q in window.sequence_queue] == ["good"]
    window.sequence_queue.clear()
    window.queue_paused = False


def test_old_sequences_still_run(pumps, qt_loop):
    window, sent, _ = pumps
    _, loop = qt_loop
    old = [
        {"pump_did": "A", "channel": [1, 2], "action": "Dose", "val1": 0.1, "val2": 0, "val3": 0},
        {"pump_did": "A", "channel": [1, 2], "action": "Wait for All", "val1": 0, "val2": 0, "val3": 0},
        {"pump_did": "A", "channel": [1], "action": "Staccato", "val1": 0.2, "val2": 0.1, "val3": 0.05},
    ]

    async def go():
        return await window._async_run_sequence("old", old)

    assert loop.run_until_complete(go()) == "done"
    assert offs_and_ons(sent, "A", 1) == [True, False, True, False, True, False]


def test_problem_steps_stop_the_run_without_pumping(pumps, qt_loop):
    window, sent, _ = pumps
    _, loop = qt_loop
    zero = {**sq.new_step("staccato", heads=[["A", 1]]), "volume": 5, "burst": 0}
    assert run(loop, window, [zero]) == "failed"
    assert "burst size is 0 mL" in window.seq_run.why
    assert not any(a.get("channe1") for _d, a in sent)


def test_a_second_stop_while_stopping_is_ignored(pumps, qt_loop):
    window, sent, _ = pumps
    _, loop = qt_loop

    async def during(task):
        await until(lambda: ("A", 1) in window.head_runs)
        window.stop_run()
        assert window.run_stopping() or task.done()
        window.stop_run()  # pressed again: nothing more happens
        assert not window.skip_step(1)

    assert run(loop, window, [dose([["A", 1]], 30)], during) == "cancelled"
    assert offs_and_ons(sent, "A", 1)[-1] is False and window.head_runs == {}
    window.queue_paused = False


def test_a_step_waits_for_its_head_to_be_free(pumps, qt_loop):
    window, sent, _ = pumps
    _, loop = qt_loop
    # The background dose still has JP 1 · CH1 when the next step wants it: it waits, then runs.
    steps = [dose([["A", 1]], 0.4, background=True), dose([["A", 1]], 0.1)]
    assert run(loop, window, steps) == "done"
    assert offs_and_ons(sent, "A", 1) == [True, False, True, False]


def test_run_now_starts_one_run_while_the_queue_stays_paused(pumps, qt_loop):
    window, _sent, _ = pumps
    _, loop = qt_loop
    window.queue_paused = True
    window.enqueue("first", [dose([["A", 1]], 0.1)])
    window.enqueue("second", [dose([["A", 2]], 0.1)])
    window.run_next_now()

    async def go():
        worker = asyncio.ensure_future(window._queue_worker())
        await until(
            lambda: window.seq_run is not None and window.seq_run.name == "first" and window.seq_run.ended
        )
        await asyncio.sleep(0.8)
        worker.cancel()

    loop.run_until_complete(go())
    assert [q["name"] for q in window.sequence_queue] == ["second"] and window.queue_paused
    window.sequence_queue.clear()
    window.queue_paused = False


def test_wait_for_me_message_pops_up_and_is_in_the_bar(pumps, qt_loop):
    window, _sent, _ = pumps
    _, loop = qt_loop
    note = "Move the sample lines to the collection tubes."
    steps = [
        wait(0.3, [2]),  # CH2 gets to the Wait for me step later than CH1
        {**sq.new_step("review", lanes=[1, 2]), "label": "Collect", "message": note, "sound": False},
    ]
    window.current_running_sequence = "Elute"
    seen = {}

    async def during(task):
        await until(lambda: window._review_popups)
        popup = next(iter(window._review_popups.values()))
        assert popup.isVisible() and popup.message.text() == note
        assert popup.lanes == {1} and popup.btn_continue.text() == "Continue CH1"
        assert note in window.lbl_approval.text()
        assert window.seq_run.lanes[1].detail == note
        popup.btn_later.click()  # Not yet: the pop-up closes, the run keeps waiting
        assert not window._review_popups and not task.done()
        await until(lambda: window._review_popups)  # CH2 arrives: it pops up again, for both
        popup = next(iter(window._review_popups.values()))
        seen["lanes"] = set(popup.lanes)
        popup.btn_continue.click()

    assert run(loop, window, steps, during, name="Elute") == "done"
    assert seen["lanes"] == {1, 2}
    assert not window._review_popups and window.approval_banner.isHidden()
    window.current_running_sequence = None


def test_continuing_from_the_bar_closes_the_pop_up(pumps, qt_loop):
    window, _sent, _ = pumps
    _, loop = qt_loop
    steps = [{**sq.new_step("review", lanes=[3]), "message": "Swap the waste bottle.", "sound": False}]

    async def during(task):
        await until(lambda: window._review_popups)
        window.approve_channel(3)

    assert run(loop, window, steps, during) == "done"
    assert window._review_popups == {}


def test_stopping_a_run_closes_its_pop_up(pumps, qt_loop):
    window, _sent, _ = pumps
    _, loop = qt_loop
    steps = [{**sq.new_step("review", lanes=[1]), "message": "Check the column.", "sound": False}]

    async def during(task):
        await until(lambda: window._review_popups)
        window.stop_run()

    assert run(loop, window, steps, during) == "cancelled"
    assert window._review_popups == {} and window.approval_banner.isHidden()
    window.queue_paused = False


def test_wait_for_me_without_a_message_doesnt_pop_up(pumps, qt_loop):
    window, _sent, _ = pumps
    _, loop = qt_loop

    async def during(task):
        await until(lambda: window._ch_review_events.get(1))
        assert window._review_popups == {}
        window.approve_channel(1)

    assert run(loop, window, [{**sq.new_step("review", lanes=[1]), "sound": False}], during) == "done"
