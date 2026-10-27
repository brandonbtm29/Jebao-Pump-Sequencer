"""The pump guard: no head left running, none running too long (pumpguard.py and its
use in the app)."""

import asyncio
import time

import pytest

import jebao_gui
import pumpguard
from pumpguard import Limit, OnTimeBudget, PumpGuard

MIN = 60.0


# --- the on-time budget -------------------------------------------------------------------


def test_budget_counts_on_time_in_the_window():
    b = OnTimeBudget()
    key = ("A", 1)
    b.start(key, 0)
    b.stop(key, 20 * MIN)
    b.start(key, 50 * MIN)
    assert b.used(key, 60 * MIN, 60 * MIN) == pytest.approx(30 * MIN)  # 20 + 10 still running
    assert b.used(key, 85 * MIN, 60 * MIN) == pytest.approx(35 * MIN)  # 0-20 has slid out of [25, 85]


def test_resume_time_for_a_head_that_ran_30_minutes():
    b = OnTimeBudget()
    key = ("A", 1)
    b.start(key, 0)
    limit = Limit(30 * MIN, 60 * MIN)
    now = 30 * MIN
    assert b.used(key, now, limit.window_s) == pytest.approx(30 * MIN)
    # resting from 30 min on, 5 min of budget is free again once 0-5 min has left the window
    assert b.resume_at(key, now, limit, need=5 * MIN) == pytest.approx(65 * MIN, abs=1)
    assert b.resume_at(key, 10 * MIN, limit, need=5 * MIN) == 10 * MIN  # budget left: now


# --- a guard with a pretend cloud ---------------------------------------------------------


class Cloud:
    """Pretend pumps: commands can be made to fail; reports can be set."""

    def __init__(self):
        self.t = 1000.0
        self.sent = []
        self.fail = 0  # fail this many commands
        self.reports = {}  # did -> {"attr": ..., "updated_at": ...}

    def clock(self):
        return self.t

    async def sleep(self, dt):
        self.t += dt
        await asyncio.sleep(0)

    async def send(self, did, attrs):
        self.sent.append((did, dict(attrs)))
        if self.fail:
            self.fail -= 1
            return False
        return True

    async def read(self, did):
        return self.reports.get(did)

    def guard(self, **kw):
        return PumpGuard(self.send, self.read, clock=self.clock, sleep=self.sleep, **kw)


def run(coro):
    return asyncio.run(coro)


def test_failed_off_is_retried_until_it_works(caplog):
    cloud = Cloud()
    g = cloud.guard()
    stopped = []
    g.on_stopped = stopped.append

    async def go():
        assert await g.command("A", {"channe2": True})
        cloud.fail = 6  # the next six OFFs fail (pump off Wi-Fi)
        assert not await g.command("A", {"channe2": False})
        assert ("A", 2) in g.pending()
        await g._retrying[("A", 2)]

    run(go())
    offs = [a for d, a in cloud.sent if a == {"channe2": False}]
    assert len(offs) == 7  # the failed one, five more failures, then success
    assert g.pending() == {} and stopped == [("A", 2)]
    assert "STILL WON'T STOP" in caplog.text


def test_failed_on_is_followed_by_an_off():
    cloud = Cloud()
    g = cloud.guard()

    async def go():
        cloud.fail = 1
        assert not await g.command("A", {"channe1": True, "channe3": True})
        await asyncio.gather(*g._retrying.values())

    run(go())
    assert ("A", {"channe1": False}) in cloud.sent and ("A", {"channe3": False}) in cloud.sent
    assert g.pending() == {}


def test_turning_on_again_cancels_a_pending_stop():
    cloud = Cloud()
    g = cloud.guard()

    async def go():
        cloud.fail = 1
        await g.command("A", {"channe1": False})  # fails: retrying
        assert ("A", 1) in g.pending()
        await g.command("A", {"channe1": True})  # someone starts it on purpose
        assert g.pending() == {}
        await asyncio.sleep(0)

    run(go())


def test_forgotten_head_is_turned_off():
    cloud = Cloud()
    g = cloud.guard()

    async def go():
        await g.command("A", {"channe4": True})
        await g.check([{"did": "A", "is_online": True}], running={})  # just turned on: left alone
        assert g.pending() == {}
        cloud.t += pumpguard.ORPHAN_GRACE_S + 1
        await g.check([{"did": "A", "is_online": True}], running={("A", 4): "manual"})  # in use
        assert g.pending() == {}
        await g.check([{"did": "A", "is_online": True}], running={})  # nothing runs it any more
        await asyncio.gather(*g._retrying.values())

    run(go())
    assert cloud.sent[-1] == ("A", {"channe4": False})


def test_head_running_without_the_app_is_turned_off_once(caplog):
    cloud = Cloud()
    g = cloud.guard()
    cloud.reports["A"] = {
        "attr": {"channe1": 0, "channe2": 1, "channe5": 1, "switch": 1},
        "updated_at": cloud.t,
    }

    async def go():
        await g.check([{"did": "A", "is_online": True}, {"did": "B", "is_online": False}], running={})
        await asyncio.gather(*g._retrying.values())
        await g.check([{"did": "A", "is_online": True}], running={})  # report not updated yet
        await asyncio.gather(*g._retrying.values())

    run(go())
    assert ("A", {"channe2": False}) in cloud.sent
    assert not any("channe5" in a for _d, a in cloud.sent)  # only the four real heads
    assert caplog.text.count("hadn't started it") == 1


def test_report_from_before_our_command_is_ignored_for_a_while():
    cloud = Cloud()
    g = cloud.guard()

    async def go():
        await g.command("A", {"channe1": True})
        await g.command("A", {"channe1": False})
        cloud.reports["A"] = {"attr": {"channe1": 1}, "updated_at": cloud.t - 5}  # old news
        await g.check([{"did": "A", "is_online": True}], running={})
        assert g.pending() == {}
        cloud.t += pumpguard.REPORT_GRACE_S + 1  # still says ON two minutes later: act
        await g.check([{"did": "A", "is_online": True}], running={})
        assert ("A", 1) in g.pending()
        await asyncio.gather(*g._retrying.values())

    run(go())


def test_over_limit_calibration_head_is_stopped():
    cloud = Cloud()
    over = []
    g = cloud.guard(limit_for=lambda key: Limit(10 * MIN, 60 * MIN))
    g.on_over_limit = over.append

    async def go():
        await g.command("A", {"channe1": True})
        cloud.t += 11 * MIN
        assert g.over_limit(("A", 1))
        # a manual dose rests by itself; a calibration run is stopped by the guard
        await g.check([{"did": "A", "is_online": True}], running={("A", 1): "manual"})
        assert over == []
        await g.check([{"did": "A", "is_online": True}], running={("A", 1): "calibration"})
        await asyncio.gather(*g._retrying.values())

    run(go())
    assert over == [("A", 1)] and cloud.sent[-1] == ("A", {"channe1": False})


def test_no_limit_means_no_limit():
    cloud = Cloud()
    g = cloud.guard(limit_for=lambda key: Limit(enabled=False))

    async def go():
        await g.command("A", {"channe1": True})
        cloud.t += 5 * 3600
        assert not g.over_limit(("A", 1)) and g.budget_left(("A", 1)) == float("inf")

    run(go())


# --- in the app ---------------------------------------------------------------------------


@pytest.fixture
def pumps(window, monkeypatch):
    window.cloud_devices = [{"did": "A", "dev_alias": "JP 1", "is_online": True}]
    sent = []
    state = {"fail": set()}

    async def fake_post(did, attrs):
        sent.append((did, dict(attrs)))
        return not any((k, v) in state["fail"] for k, v in attrs.items())

    monkeypatch.setattr(window, "_post_control", fake_post)
    window.config.get_pump("A")["calibration"] = [60.0] * 4
    return window, sent, state


def test_head_limits_from_settings_and_rack(pumps):
    window, _sent, _state = pumps
    window.config.data["guard"] = {"enabled": True, "on_min": 30, "window_min": 60}
    assert window.head_limit(("A", 1)).on_s == 30 * MIN
    window.config.get_pump("A")["limits"] = [None, "off", 15, None]
    assert not window.head_limit(("A", 2)).enabled
    assert window.head_limit(("A", 3)).on_s == 15 * MIN
    window.config.data["guard"]["enabled"] = False
    assert not window.head_limit(("A", 1)).enabled
    assert window.head_limit(("A", 3)).enabled  # its own limit still applies


def test_sequence_step_sends_off_even_when_cancelled(pumps, qt_loop):
    window, sent, _state = pumps
    _, loop = qt_loop

    async def go():
        task = asyncio.ensure_future(window._timed_run("A", 2, 30.0))
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    loop.run_until_complete(go())
    assert sent[0] == ("A", {"channe2": True}) and sent[-1] == ("A", {"channe2": False})
    assert window.head_runs == {}


def test_manual_start_that_fails_is_still_turned_off(pumps, qt_loop, monkeypatch):
    window, sent, state = pumps
    _, loop = qt_loop
    monkeypatch.setattr(pumpguard, "FOLLOW_UP_S", 0.05)
    state["fail"] = {("channe1", True)}

    async def go():
        await asyncio.gather(*window.dispense([("A", 1)], 0.2, "time"))
        await asyncio.gather(*window.guard._retrying.values())

    loop.run_until_complete(go())
    # OFF right away, and once more a little later in case the ON arrives late
    assert sent == [("A", {"channe1": True}), ("A", {"channe1": False}), ("A", {"channe1": False})]
    assert window.guard.pending() == {}


def test_dose_rests_at_the_limit_and_carries_on(pumps, qt_loop, monkeypatch, caplog):
    window, sent, _state = pumps
    _, loop = qt_loop
    monkeypatch.setattr(window, "head_limit", lambda key: Limit(on_s=0.4, window_s=1.0))
    seen = []

    async def watch():
        while True:
            st = window.run_state("A", 1)
            if st:
                seen.append(st[0])
            await asyncio.sleep(0.05)

    async def go():
        w = asyncio.ensure_future(watch())
        started = time.time()
        await window._timed_run("A", 1, 0.7, source="manual")
        w.cancel()
        return time.time() - started

    took = loop.run_until_complete(go())
    cmds = [a["channe1"] for _d, a in sent]
    assert cmds[:3] == [True, False, True] and cmds[-1] is False  # ran, rested, ran again, off
    assert "cooling" in seen and "running" in seen
    assert took >= 1.0  # it waited for the window to free up
    assert "Reached its on-time limit" in caplog.text and "carrying on" in caplog.text


def test_startup_check_turns_off_a_head_left_running(pumps, qt_loop, monkeypatch, caplog):
    window, sent, _state = pumps
    _, loop = qt_loop

    async def fake_read(did):
        return {"attr": {"channe1": 0, "channe2": 1, "channe3": 0, "channe4": 0}, "updated_at": time.time()}

    monkeypatch.setattr(window, "_read_pump", fake_read)

    class Resp:
        status_code = 200
        text = ""

        def json(self):
            return {"devices": [{"did": "A", "dev_alias": "JP 1", "is_online": True}]}

    monkeypatch.setattr(jebao_gui.requests, "get", lambda *a, **k: Resp())
    window.cloud_token = "fake-session"  # noqa: S105 - not a real token

    async def go():
        await window.fetch_devices()
        await asyncio.gather(*window.guard._retrying.values())

    loop.run_until_complete(go())
    window._watching = True
    assert ("A", {"channe2": False}) in sent
    assert "JP 1 · CH2 is running but the app hadn't started it" in caplog.text


def test_guard_banner_and_busy_while_a_stop_is_pending(pumps):
    window, _sent, _state = pumps
    window.guard.must_off[("A", 3)] = "OFF didn't go through"
    try:
        window.refresh_guard_banner()
        assert not window.guard_bar.isHidden() and "JP 1 · CH3" in window.guard_banner.text()
        assert "still trying to turn a pump head off" in window.busy_reason()
    finally:
        window.guard.must_off.clear()
    window.refresh_guard_banner()
    assert window.guard_bar.isHidden()


def test_resting_dose_that_reports_on_is_turned_off():
    cloud = Cloud()
    g = cloud.guard()

    async def go():
        await g.command("A", {"channe1": True})
        await g.command("A", {"channe1": False})  # paused or resting: meant to be off
        cloud.t += 5
        cloud.reports["A"] = {"attr": {"channe1": 1}, "updated_at": cloud.t}  # but it says ON
        await g.check([{"did": "A", "is_online": True}], running={("A", 1): "manual"})
        await asyncio.gather(*g._retrying.values())

    run(go())
    assert cloud.sent[-1] == ("A", {"channe1": False})


# --- review fixes: races, offline pumps, cancellation, Emergency Stop ---------------------


def test_report_while_an_on_is_on_its_way_is_not_acted_on():
    cloud = Cloud()
    g = cloud.guard()
    gate = asyncio.Event

    async def go():
        release = gate()
        slow_sent = []

        async def slow_send(did, attrs):
            slow_sent.append(dict(attrs))
            if attrs.get("channe1"):
                await release.wait()  # the ON reply takes a while
            return True

        g.send = slow_send
        on = asyncio.ensure_future(g.command("A", {"channe1": True}))
        await asyncio.sleep(0)
        cloud.reports["A"] = {"attr": {"channe1": 1}, "updated_at": cloud.t}  # pump already on
        await g.check([{"did": "A", "is_online": True}], running={})
        release.set()
        assert await on
        await asyncio.sleep(0)
        assert g.pending() == {} and slow_sent == [{"channe1": True}]  # no OFF was sent

    run(go())


def test_off_to_an_offline_pump_stays_pending_until_it_reports_off():
    cloud = Cloud()
    g = cloud.guard()
    stopped = []
    g.on_stopped = stopped.append

    async def go():
        await g.command("A", {"channe2": True})
        await g.check([{"did": "A", "is_online": False}], running={("A", 2): "manual"})
        await g.command("A", {"channe2": False})  # the cloud takes it, but the pump is away
        assert ("A", 2) in g.pending()
        cloud.t += 600
        cloud.reports["A"] = {"attr": {"channe2": 0}, "updated_at": cloud.t}
        await g.check([{"did": "A", "is_online": True}], running={})  # back, and reports off
        assert g.pending() == {} and stopped == [("A", 2)]

    run(go())


def test_cancelled_on_is_stopped_and_stopped_again_later(monkeypatch):
    cloud = Cloud()
    g = cloud.guard()
    monkeypatch.setattr(pumpguard, "FOLLOW_UP_S", 5)

    async def go():
        async def hang(did, attrs):
            if attrs.get("channe3"):
                await asyncio.sleep(3600)
            return await cloud.send(did, attrs)

        g.send = hang
        task = asyncio.ensure_future(g.command("A", {"channe3": True}))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.gather(*g._retrying.values())
        assert g.pending() == {}

    run(go())
    assert cloud.sent == [("A", {"channe3": False}), ("A", {"channe3": False})]


def test_emergency_stop_hold_refuses_on():
    cloud = Cloud()
    g = cloud.guard()

    async def go():
        g.hold(15)
        assert not await g.command("A", {"channe1": True})
        assert await g.command("A", {"channe1": False})
        cloud.t += 16
        assert await g.command("A", {"channe1": True})

    run(go())
    assert cloud.sent == [("A", {"channe1": False}), ("A", {"channe1": True})]


def test_head_that_reports_off_during_its_dose_is_turned_on_again(caplog):
    cloud = Cloud()
    g = cloud.guard()

    async def go():
        await g.command("A", {"channe1": True})
        cloud.t += 30
        cloud.reports["A"] = {"attr": {"channe1": 0}, "updated_at": cloud.t}
        await g.check([{"did": "A", "is_online": True}], running={("A", 1): "sequence"})
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await g.check([{"did": "A", "is_online": True}], running={("A", 1): "sequence"})  # not again
        await asyncio.sleep(0)

    run(go())
    assert [a for _d, a in cloud.sent] == [{"channe1": True}, {"channe1": True}]
    assert caplog.text.count("reports OFF during its dose") == 1


def test_long_windows_keep_their_history():
    cloud = Cloud()
    g = cloud.guard(limit_for=lambda key: Limit(120 * MIN, 480 * MIN))

    async def go():
        await g.command("A", {"channe1": True})
        cloud.t += 50 * MIN
        await g.command("A", {"channe1": False})
        cloud.t += 4 * 3600
        await g.check([], running={})
        assert g.budget_left(("A", 1)) == pytest.approx(70 * MIN)

    run(go())


def test_emergency_stop_cancels_manual_doses_and_holds_on(pumps, qt_loop):
    window, sent, _state = pumps
    _, loop = qt_loop

    async def go():
        tasks = window.dispense([("A", 1), ("A", 2)], 30.0, "time")
        await asyncio.sleep(0.2)
        await window.emergency_stop_all()
        await asyncio.gather(*tasks, return_exceptions=True)
        assert all(t.done() for t in tasks)
        assert not await window.send_cloud_command("A", {"channe1": True})  # held for a moment

    loop.run_until_complete(go())
    assert window.head_runs == {} and not window._dose_tasks
    assert sent[-1] != ("A", {"channe1": True})
    window.guard.hold_until = 0


def test_dose_fails_loudly_if_the_pump_wont_turn_on(pumps, qt_loop, monkeypatch):
    window, sent, state = pumps
    _, loop = qt_loop
    monkeypatch.setattr(pumpguard, "FOLLOW_UP_S", 0.01)
    monkeypatch.setattr(asyncio, "sleep", _fast_sleep(asyncio.sleep))
    state["fail"] = {("channe2", True)}

    async def go():
        with pytest.raises(RuntimeError, match="Couldn't turn"):
            await window._timed_run("A", 2, 5.0)
        await asyncio.gather(*window.guard._retrying.values())

    loop.run_until_complete(go())
    assert [a["channe2"] for _d, a in sent].count(True) == 3  # three tries
    assert sent[-1] == ("A", {"channe2": False}) and window.guard.pending() == {}


def _fast_sleep(real):
    async def sleep(dt, *a, **k):
        return await real(min(dt, 0.01), *a, **k)

    return sleep


def test_closing_waits_for_unconfirmed_offs(pumps, qt_loop, monkeypatch):
    window, _sent, _state = pumps
    _, loop = qt_loop
    asked = []
    answer = {"button": jebao_gui.QMessageBox.StandardButton.Cancel}

    def question(*a, **k):
        asked.append(a[2])
        return answer["button"]

    monkeypatch.setattr(jebao_gui.QMessageBox, "question", question)
    monkeypatch.setattr(asyncio, "sleep", _fast_sleep(asyncio.sleep))
    window.guard.online["A"] = False  # the pump is off Wi-Fi: its OFFs can't be confirmed
    window.guard.unconfirmed[("A", 4)] = (time.time(), "pump offline: OFF sent, waiting for it to reconnect")
    window.show()
    try:
        loop.run_until_complete(window._stop_then_close())
        assert asked and "JP 1 · CH4" in asked[0]
        assert window.isVisible() and not window._closing  # stayed open, still trying
        answer["button"] = jebao_gui.QMessageBox.StandardButton.Close
        loop.run_until_complete(window._stop_then_close())
        assert not window.isVisible()  # "Close anyway"
    finally:
        window.guard.unconfirmed.clear()
        window.guard.hold_until = 0


def test_off_waits_for_the_pumps_own_confirmation():
    cloud = Cloud()
    g = cloud.guard()

    async def go():
        cloud.reports["A"] = {"attr": {"channe1": 0}, "updated_at": cloud.t}
        await g.check([{"did": "A", "is_online": True}], running={})  # reports can be read
        await g.command("A", {"channe1": True})
        await g.command("A", {"channe1": False})
        assert g.pending() == {}  # within the grace: no red bar for a normal OFF
        assert ("A", 1) in g.pending(all_=True)
        cloud.t += pumpguard.CONFIRM_GRACE_S + 1
        assert ("A", 1) in g.pending()  # not confirmed in time: shown
        cloud.reports["A"] = {"attr": {"channe1": 0}, "updated_at": cloud.t}
        await g.check([{"did": "A", "is_online": True}], running={})
        assert g.pending(all_=True) == {}

    run(go())


def test_without_reports_only_offline_pumps_wait():
    cloud = Cloud()
    g = cloud.guard()
    g.read_ok = False

    async def go():
        await g.command("A", {"channe1": True})
        await g.command("A", {"channe1": False})
        assert g.pending(all_=True) == {}

    run(go())


def test_pump_removed_from_the_account_is_forgotten(caplog):
    cloud = Cloud()
    g = cloud.guard()
    g.unconfirmed[("B", 2)] = (cloud.t, "pump offline: OFF sent, waiting for it to reconnect")

    async def go():
        await g.check([{"did": "A", "is_online": True}], running={})

    run(go())
    assert g.pending(all_=True) == {} and "no longer on the account" in caplog.text


def test_switched_off_by_hand(pumps, monkeypatch):
    window, _sent, _state = pumps
    monkeypatch.setattr(
        jebao_gui.QMessageBox, "question", lambda *a, **k: jebao_gui.QMessageBox.StandardButton.Yes
    )
    window.guard.online["A"] = False
    window.guard.unconfirmed[("A", 1)] = (time.time(), "pump offline: OFF sent, waiting for it to reconnect")
    window.refresh_guard_banner()
    assert not window.guard_bar.isHidden()
    window.btn_guard_forget.click()
    assert window.guard.pending(all_=True) == {} and window.guard_bar.isHidden()


def test_offs_waiting_for_a_report_are_trusted_if_reports_cant_be_read():
    cloud = Cloud()
    g = cloud.guard()

    async def go():
        await g.command("A", {"channe1": True})
        await g.command("A", {"channe1": False})
        assert ("A", 1) in g.pending(all_=True)
        await g.check([{"did": "A", "is_online": True}], running={})  # no report available
        assert g.pending(all_=True) == {}

    run(go())
