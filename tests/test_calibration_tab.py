"""The Calibration tab, driven the way a person would, with a pretend balance and pumps.

A fast clock stands in for real time, so a several-minute calibration takes milliseconds.
"""

import asyncio
import csv
import os

import pytest

import autocal
import balance
import jebao_gui


class Rig:
    """Pretend pumps (A online, B online, C offline) pouring into one pretend balance."""

    def __init__(self, window, monkeypatch, rates=None):
        self.t = 1000.0
        self.window = window
        self.rates = rates or {}
        self.sent = []
        self.bal = balance.SimulatedBalance(clock=self.clock, noise=0.002)
        self.bal.tick()
        window.cloud_devices = [
            {"did": "A", "dev_alias": "JP 1", "is_online": True},
            {"did": "B", "dev_alias": "JP 2", "is_online": True},
            {"did": "C", "dev_alias": "JP 3", "is_online": False},
        ]
        monkeypatch.setattr(window, "send_cloud_command", self.send)
        window.refresh_pumps()
        tab = window.calibration_tab
        tab.clock, tab.sleep = self.clock, self.sleep
        self.tab = tab

    def clock(self):
        return self.t

    async def sleep(self, dt):
        steps = max(1, int(round(dt / 0.1)))
        for _ in range(steps):
            self.t += dt / steps
            self.bal.tick()
        await asyncio.sleep(0)

    async def send(self, did, attrs):
        self.sent.append((did, dict(attrs)))
        for key, on in attrs.items():
            ch = int(key[-1])
            if on:
                self.bal.rate_g_s = self.rates.get((did, ch), 0.04)
            self.bal.flowing = bool(on)
        return True


@pytest.fixture
def rig(window, monkeypatch):
    return Rig(window, monkeypatch)


def run_calibration(loop, tab, heads, during=None):
    async def go():
        tab.begin(heads)
        if during:
            await during()
        await tab.task

    loop.run_until_complete(go())


def tile(tab, did, ch):
    return next(t for t in tab.grid.tiles() if (t.did, t.ch) == (did, ch))


def test_rack_grid_and_quick_selection(rig):
    tab = rig.tab
    assert [c.did for c in tab.grid.cards] == ["A", "B", "C"]
    tab.select_uncalibrated()
    assert tab.heads() == [("A", 1), ("A", 2), ("A", 3), ("A", 4), ("B", 1), ("B", 2), ("B", 3), ("B", 4)]
    assert "8 heads" in tab.lbl_plan.text() and "80 mL" in tab.lbl_plan.text()  # 10 mL each
    tab.grid.clear_selection()
    assert tab.heads() == []
    tab.grid.select([("B", 2)])
    assert tab.heads() == [("B", 2)]


def test_start_needs_a_balance(rig):
    tab = rig.tab
    tab.grid.select([("A", 1)])
    assert not tab.btn_start.isEnabled() and "Connect the balance" in tab.btn_start.text()
    tab.use_balance(rig.bal)
    assert tab.btn_start.isEnabled() and tab.btn_start.text() == "Calibrate 1 head"
    assert "Connected" in tab.lbl_balance.text() and " g" in tab.lbl_balance.text()


def test_a_full_run_saves_the_rates(rig, qt_loop):
    _, loop = qt_loop
    tab, window = rig.tab, rig.window
    rig.rates = {("A", 1): 0.04, ("A", 2): 0.08}
    tab.use_balance(rig.bal)
    tab.spin_volume.setValue(2.0)
    run_calibration(loop, tab, [("A", 1), ("A", 2)])
    pump = window.config.get_pump("A")
    assert pump["calibration"][0] == pytest.approx(0.04 / 0.997 * 60, rel=0.01)
    assert pump["calibration"][1] == pytest.approx(0.08 / 0.997 * 60, rel=0.01)
    assert pump["calibrated_at"][0] and pump["calibrated_at"][1]
    # one head at a time, each turned off again
    assert rig.sent == [
        ("A", {"channe1": True}),
        ("A", {"channe1": False}),
        ("A", {"channe2": True}),
        ("A", {"channe2": False}),
    ]
    assert window.head_runs == {} and window._motor_jobs == 0 and not tab.running()
    tab.refresh()
    assert tab.results_table.rowCount() == 2
    assert tab.results_table.item(0, 6).text() == "Saved"
    assert "saved" in tile(tab, "A", 1).lbl_status.text()
    # a record of the run is kept in the data folder
    assert tab.csv_path and os.path.exists(tab.csv_path)
    rows = list(csv.reader(open(tab.csv_path, encoding="utf-8")))
    assert rows[3][2] == "1" and rows[3][12] == "True"


def test_failed_head_can_be_accepted_or_redone(rig, qt_loop, monkeypatch):
    import calibration_tab

    monkeypatch.setattr(
        calibration_tab.QMessageBox, "question", lambda *a: calibration_tab.QMessageBox.StandardButton.Ok
    )
    _, loop = qt_loop
    tab, window = rig.tab, rig.window
    tab.use_balance(rig.bal)
    tab.spin_volume.setValue(2.0)
    tab.spin_retries.setValue(0)
    tab.spin_tol.setValue(0.1)  # very strict, so the pretend balance's wobble fails it
    tab.spin_r2.setValue(1.0)
    run_calibration(loop, tab, [("B", 3)])
    result = tab.cal.results[0]
    assert result.status == autocal.FAILED
    assert window.config.get_pump("B")["calibration"][2] == 1.0  # not saved
    tab.refresh()
    assert tab.results_table.item(0, 6).text().startswith("Failed")
    tab.accept(result)
    assert result.status == autocal.ACCEPTED
    assert window.config.get_pump("B")["calibration"][2] == pytest.approx(result.rate, abs=1e-4)

    # Redo with normal limits: it runs again and passes
    tab.spin_tol.setValue(2.0)
    tab.spin_r2.setValue(0.99)

    async def redo():
        tab.redo(result)
        await tab.task

    loop.run_until_complete(redo())
    assert tab.cal.results[-1].status == autocal.SAVED


def test_emergency_stop_stops_the_calibration(rig, qt_loop):
    _, loop = qt_loop
    tab, window = rig.tab, rig.window
    tab.use_balance(rig.bal)

    async def press_estop():
        while tab.cal.phase != "running":
            await asyncio.sleep(0)
        assert ("A", 1) in window.head_runs  # shows as running on the Manual tab too
        window.emergency_stop_all()

    run_calibration(loop, tab, [("A", 1), ("A", 2)], during=press_estop)
    assert [r.status for r in tab.cal.results] == [autocal.STOPPED, autocal.STOPPED]
    assert ("A", {"channe1": False}) in rig.sent
    assert window._motor_jobs == 0


def test_cancel_on_the_manual_tab_stops_it_too(rig, qt_loop):
    _, loop = qt_loop
    tab, window = rig.tab, rig.window
    tab.use_balance(rig.bal)

    async def cancel_head():
        while tab.cal.phase != "running":
            await asyncio.sleep(0)
        window.cancel_heads([("A", 1)])
        tab._watch_cancel()

    run_calibration(loop, tab, [("A", 1)], during=cancel_head)
    assert tab.cal.results[0].status == autocal.STOPPED


def test_wont_start_with_a_sequence_queued(rig, monkeypatch):
    tab, window = rig.tab, rig.window
    warnings = []
    monkeypatch.setattr(jebao_gui.QMessageBox, "warning", lambda *a: warnings.append(a[2]))
    monkeypatch.setattr(tab, "begin", lambda heads: pytest.fail("must not start"))
    tab.use_balance(rig.bal)
    tab.grid.select([("A", 1)])
    window.sequence_queue.append({"name": "x", "data": []})
    try:
        tab.start()
    finally:
        window.sequence_queue.clear()
    assert warnings and "sequence" in warnings[0]


def test_start_asks_first(rig, monkeypatch):
    import calibration_tab

    tab = rig.tab
    started = []
    monkeypatch.setattr(
        calibration_tab.QMessageBox, "question", lambda *a: calibration_tab.QMessageBox.StandardButton.Ok
    )
    monkeypatch.setattr(tab, "begin", started.append)
    tab.use_balance(rig.bal)
    tab.grid.select([("A", 2), ("A", 1)])
    tab.start()
    assert started == [[("A", 1), ("A", 2)]]  # rack order


def test_manual_timed_run(rig, monkeypatch):
    import calibration_tab

    tab, window = rig.tab, rig.window
    calls = []
    monkeypatch.setattr(
        window, "dispense", lambda heads, value, unit: calls.append((heads, value, unit)) or []
    )
    monkeypatch.setattr(
        calibration_tab.QMessageBox, "question", lambda *a: calibration_tab.QMessageBox.StandardButton.Ok
    )
    monkeypatch.setattr(calibration_tab.QMessageBox, "information", lambda *a: None)
    tab.btn_manual.click()
    assert tab.pages.currentIndex() == 1
    tab.grid.select([("A", 1), ("B", 4)])
    assert tab.btn_manual_run.text() == "Run 2 heads for 60 s"
    tab.run_manual()
    assert calls == [([("A", 1), ("B", 4)], 60.0, "time")]
    assert tab.manual_table.rowCount() == 2
    tab.manual_table.cellWidget(0, 1).setValue(2.5)  # mL in 60 s
    assert tab.manual_table.item(0, 2).text() == "2.500"
    tab.combo_manual_unit.setCurrentIndex(2)  # grams of ethanol
    tab.manual_table.cellWidget(1, 1).setValue(1.578)
    tab.save_manual()
    assert window.config.get_pump("A")["calibration"][0] == pytest.approx(2.5 / 0.789, rel=1e-3)
    assert window.config.get_pump("B")["calibration"][3] == pytest.approx(2.0, rel=1e-3)


def test_tile_size_follows_between_tabs(rig):
    rig.window.manual_tab.set_tile_size("S")
    assert rig.tab.grid.tile_size == "S"
    rig.tab.grid.set_tile_size("L")
    assert rig.window.manual_tab.tile_size == "L"
    assert rig.window.config.data["ui"]["tile_size"] == "L"


def test_settings_are_remembered(rig):
    tab, window = rig.tab, rig.window
    tab.spin_volume.setValue(5.0)
    tab.combo_liquid.setCurrentIndex(1)  # ethanol
    tab.save_settings()
    cfg = window.config.data["autocal"]
    assert cfg["settings"]["volume_ml"] == 5.0
    assert cfg["settings"]["density"] == pytest.approx(0.789)
    assert cfg["liquid"] == "Ethanol"


def test_redo_checks_first(rig, monkeypatch):
    import calibration_tab

    tab, window = rig.tab, rig.window
    warnings = []
    monkeypatch.setattr(calibration_tab.QMessageBox, "warning", lambda *a: warnings.append(a[2]))
    tab.use_balance(rig.bal)
    tab.cal = autocal.AutoCalibrator(rig.bal, None, autocal.Settings())
    r = tab.cal.add("A", 1, "JP 1 · CH1")
    r.status = autocal.FAILED
    window.head_runs[("A", 1)] = jebao_gui.HeadRun("A", 1, 60, "manual", remaining=30)
    try:
        tab.redo(r)
    finally:
        window.head_runs.clear()
    assert warnings and "running right now" in warnings[0]
    assert r.status == autocal.FAILED and tab.task is None


def test_pause_is_refused_for_a_head_being_calibrated(rig):
    window = rig.window
    run = jebao_gui.HeadRun("A", 1, 60, "calibration", remaining=30)
    window.head_runs[("A", 1)] = run
    try:
        window.pause_heads([("A", 1)])
        assert not run.paused
    finally:
        window.head_runs.clear()


def test_a_head_that_would_not_stop_is_retried_when_cancelled(rig, qt_loop):
    _, loop = qt_loop
    tab, window = rig.tab, rig.window
    run = jebao_gui.HeadRun("A", 2, 60, "calibration", remaining=0)
    window.head_runs[("A", 2)] = run
    assert "running" in window.busy_reason()
    window.cancel_heads([("A", 2)])

    async def go():
        tab._watch_cancel()
        for _ in range(20):
            await asyncio.sleep(0)

    loop.run_until_complete(go())
    assert ("A", {"channe2": False}) in rig.sent
    assert window.head_runs == {}


def test_manual_rates_use_the_time_that_ran(rig, monkeypatch):
    import calibration_tab

    tab, window = rig.tab, rig.window
    monkeypatch.setattr(window, "dispense", lambda *a: [])
    monkeypatch.setattr(
        calibration_tab.QMessageBox, "question", lambda *a: calibration_tab.QMessageBox.StandardButton.Ok
    )
    tab.btn_manual.click()
    tab.grid.select([("A", 1)])
    tab.spin_manual_s.setValue(30)
    tab.run_manual()
    tab.manual_table.cellWidget(0, 1).setValue(1.0)
    assert tab.manual_table.item(0, 2).text() == "2.000"  # 1 mL in 30 s


def test_rack_and_results_scroll_together(rig):
    tab = rig.tab
    assert tab.grid.scroll is None  # no scroll bar of its own
    content = tab.scroll.widget()
    assert content.isAncestorOf(tab.grid) and content.isAncestorOf(tab.run_area)
    assert not content.isAncestorOf(tab.grid.toolbar)  # tile size and quick picks stay in view
