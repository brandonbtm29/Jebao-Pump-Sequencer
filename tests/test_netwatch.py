"""Pump Wi-Fi history (netwatch.py), the background check and the connection report."""

import json

import pytest

import jebao_gui
import netwatch


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def devs(**online):
    return [
        {"did": did, "dev_alias": f"JP {did}", "mac": f"aa:bb:{did}", "is_online": on}
        for did, on in online.items()
    ]


def test_first_check_records_without_events():
    log = netwatch.ConnectionLog(clock=Clock())
    assert log.update(devs(A=True, B=False)) == []
    assert log.events == []
    assert log.pumps["B"].online is False and log.pumps["A"].mac == "aa:bb:A"


def test_drops_and_reconnects_are_counted():
    clock = Clock()
    log = netwatch.ConnectionLog(clock=clock)
    log.update(devs(A=True, B=True))
    clock.t += 60
    changes = log.update(devs(A=False, B=True))
    assert [(p.did, ev) for p, ev, _ in changes] == [("A", "offline")]
    clock.t += 150
    ((p, ev, outage),) = log.update(devs(A=True, B=True))
    assert ev == "online" and outage == pytest.approx(150)
    clock.t += 30
    log.update(devs(A=False, B=True))  # still offline when the report is made
    clock.t += 40
    s = log.summary("A")
    assert s["drops_day"] == 2 and s["drops_week"] == 2
    assert s["offline_day_s"] == pytest.approx(190)  # 150 s + the 40 s still going
    assert s["longest_week_s"] == pytest.approx(150)
    assert s["online"] is False and s["last_online"] == pytest.approx(
        clock.t - 70
    )  # the check before it dropped again
    assert log.summary("B")["drops_day"] == 0


def test_history_is_saved_and_old_events_dropped(tmp_path):
    clock = Clock()
    path = str(tmp_path / "logs" / "connection-PC.jsonl")
    log = netwatch.ConnectionLog(path=path, clock=clock, keep_days=7)
    log.update(devs(A=True))
    clock.t += 10
    log.update(devs(A=False))
    clock.t += 10
    log.update(devs(A=True))
    lines = [json.loads(x) for x in open(path, encoding="utf-8")]
    assert [e["event"] for e in lines] == ["offline", "online"]
    # eight days later, a new session: the old events are gone from the file too
    clock.t += 8 * netwatch.DAY
    again = netwatch.ConnectionLog(path=path, clock=clock, keep_days=7).load()
    assert again.events == []
    assert open(path, encoding="utf-8").read() == ""


def test_cloud_checks():
    clock = Clock()
    log = netwatch.ConnectionLog(clock=clock)
    assert log.cloud_check(True, 0.4) is False  # first answer: nothing to report
    assert log.cloud_check(True, 0.6) is False
    assert log.cloud_check(False, None, "no answer") is True  # lost: say so once
    assert log.cloud_check(False, None, "no answer") is False
    assert log.cloud_check(True, 2.0) is True  # back
    c = log.cloud_summary()
    assert (
        c["checks"] == 5
        and c["failed"] == 2
        and c["median_s"] == pytest.approx(0.6)
        and c["slowest_s"] == 2.0
    )


def test_fmt_duration():
    assert netwatch.fmt_duration(42) == "42 s"
    assert netwatch.fmt_duration(180) == "3 min"
    assert netwatch.fmt_duration(3900) == "1 h 05 min"
    assert netwatch.fmt_duration(2 * 86400 + 3 * 3600) == "2 d 3 h"


# --- in the app ------------------------------------------------------------------------


class Resp:
    def __init__(self, status=200, data=None, text=""):
        self.status_code, self._data, self.text = status, data or {}, text

    def json(self):
        return self._data


@pytest.fixture
def cloud(window, monkeypatch):
    """The window logged in, with a fake pump list the test can change."""
    state = {"devices": devs(A=True, B=True), "status": 200, "text": ""}

    def fake_get(url, headers=None, timeout=None):
        if state.get("raise"):
            raise jebao_gui.requests.ConnectionError("network down")
        return Resp(state["status"], {"devices": [dict(d) for d in state["devices"]]}, state["text"])

    monkeypatch.setattr(jebao_gui.requests, "get", fake_get)
    monkeypatch.setattr(window, "netlog", netwatch.ConnectionLog())
    window.cloud_token = "fake-session"  # noqa: S105 - not a real token
    return window, state


def fetch(loop, window):
    loop.run_until_complete(window.fetch_devices(quiet=True))


def test_background_check_logs_drops_and_updates_tiles(cloud, qt_loop, caplog):
    window, state = cloud
    _, loop = qt_loop
    loop.run_until_complete(window.fetch_devices())  # first, at login
    assert window.pumps_button.text() == "Pumps: 2/2 online"
    grid_before = window.manual_tab.cards[0]
    state["devices"] = devs(A=False, B=True)
    fetch(loop, window)
    assert "JP A went offline" in caplog.text
    assert window.pumps_button.text() == "Pumps: 1/2 online"
    assert window.manual_tab.cards[0] is grid_before  # same pumps: tabs not rebuilt
    window.manual_tab.refresh()
    tile = next(t for t in window.manual_tab.tiles() if t.did == "A")
    assert tile.lbl_status.text() == "Pump offline"
    state["devices"] = devs(A=True, B=True)
    fetch(loop, window)
    assert "JP A is back online after" in caplog.text
    window._watching = True  # (the background loop was started; nothing else to check here)


def test_cloud_unreachable_is_reported_once(cloud, qt_loop, caplog):
    window, state = cloud
    _, loop = qt_loop
    fetch(loop, window)
    state["raise"] = True
    fetch(loop, window)
    fetch(loop, window)
    assert caplog.text.count("Can't reach the Jebao cloud") == 1
    assert window.pumps_button.text() == "Pumps: cloud unreachable"
    state["raise"] = False
    fetch(loop, window)
    assert "Reached the Jebao cloud again" in caplog.text


def test_expired_session_logs_in_again(cloud, qt_loop, monkeypatch):
    window, state = cloud
    _, loop = qt_loop
    relogged = []

    async def fake_login():
        relogged.append(True)

    monkeypatch.setattr(window, "login_and_fetch", fake_login)
    state["status"], state["text"] = 400, '{"error_code": 9004, "error_message": "token invalid!"}'
    fetch(loop, window)
    assert relogged == [True]


def test_connection_report(cloud, qt_loop):
    import network_report

    window, state = cloud
    _, loop = qt_loop
    fetch(loop, window)
    state["devices"] = devs(A=False, B=True)
    fetch(loop, window)
    dlg = network_report.NetworkDialog(window)
    assert dlg.table.rowCount() == 2
    row_a = [dlg.table.item(0, c).text() for c in range(dlg.table.columnCount())]
    assert row_a[1] == "Offline" and row_a[3] == "1" and row_a[8] == "aa:bb:A"
    assert "1 of 2 pumps online" in dlg.lbl_cloud.text()
    text = dlg.report_text()
    assert "JP A\tOFFLINE" in text and "aa:bb:B" in text
    dlg.timer.stop()
    dlg.close()
