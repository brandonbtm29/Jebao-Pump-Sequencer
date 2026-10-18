"""Per-head motor runs: dispensing on several heads at once, pause, resume, cancel."""

import asyncio

import pytest

import jebao_gui


@pytest.fixture
def pumps(window, monkeypatch):
    """Two online pumps and one offline, with fake cloud commands."""
    window.cloud_devices = [
        {"did": "A", "dev_alias": "JP 1", "is_online": True},
        {"did": "B", "dev_alias": "JP 2", "is_online": True},
        {"did": "C", "dev_alias": "JP 3", "is_online": False},
    ]
    sent = []

    async def fake_send(did, attrs):
        sent.append((did, dict(attrs)))
        return True

    monkeypatch.setattr(window, "send_cloud_command", fake_send)
    return window, sent


def dispense_and_wait(loop, window, *args):
    """Call dispense() on the running event loop (as the app does) and wait for it to finish."""

    async def go():
        tasks = window.dispense(*args)
        await asyncio.gather(*tasks)
        return tasks

    return loop.run_until_complete(go())


def test_dispense_by_time_on_two_pumps(pumps, qt_loop):
    window, sent = pumps
    _, loop = qt_loop
    tasks = dispense_and_wait(loop, window, [("A", 1), ("A", 3), ("B", 2)], 0.2, "time")
    assert len(tasks) == 2  # one job per pump
    assert ("A", {"channe1": True, "channe3": True}) in sent  # both heads of a pump start together
    assert ("B", {"channe2": True}) in sent
    for off in [("A", {"channe1": False}), ("A", {"channe3": False}), ("B", {"channe2": False})]:
        assert off in sent
    assert window.head_runs == {} and window._motor_jobs == 0


def test_volume_uses_each_heads_rate(pumps):
    window, _ = pumps
    window.config.get_pump("A")["calibration"][1] = 2.0
    assert window.dispense_seconds("A", 2, 10.0, "volume") == pytest.approx(300.0)
    assert window.dispense_seconds("A", 2, 45, "time") == 45.0
    window.config.get_pump("A")["calibration"][2] = 0
    with pytest.raises(ValueError):
        window.dispense_seconds("A", 3, 1.0, "volume")


def test_offline_running_and_uncalibrated_heads_are_skipped(pumps, caplog):
    window, sent = pumps
    window.config.get_pump("B")["calibration"][3] = 0
    window.head_runs[("A", 1)] = jebao_gui.HeadRun("A", 1, 10, "sequence", remaining=5)
    with caplog.at_level("WARNING", logger="JebaoGUI"):
        tasks = window.dispense([("A", 1), ("B", 4), ("C", 1)], 5, "volume")
    assert tasks == [] and sent == []
    text = caplog.text
    assert "already running" in text and "offline" in text and "no flow rate" in text
    window.head_runs.clear()


def test_pause_resume_and_progress(pumps, qt_loop):
    window, sent = pumps
    _, loop = qt_loop

    async def scenario():
        tasks = window.dispense([("A", 2)], 0.6, "time")
        await asyncio.sleep(0.25)
        state, progress, left = window.run_state("A", 2)
        assert state == "running" and 0 < progress < 1 and 0 < left < 0.6
        window.pause_heads([("A", 2)])
        await asyncio.sleep(0.3)
        assert window.run_state("A", 2)[0] == "paused"
        frozen = window.head_runs[("A", 2)].remaining
        await asyncio.sleep(0.2)
        assert window.head_runs[("A", 2)].remaining == pytest.approx(frozen)
        window.resume_heads([("A", 2)])
        await asyncio.gather(*tasks)

    loop.run_until_complete(scenario())
    offs = [a for d, a in sent if a == {"channe2": False}]
    ons = [a for d, a in sent if a == {"channe2": True}]
    assert len(ons) == 2 and len(offs) == 2  # paused = off, resumed = on, then off at the end
    assert window.run_state("A", 2) is None


def test_cancel_stops_only_that_head(pumps, qt_loop, caplog):
    window, sent = pumps
    _, loop = qt_loop

    async def scenario():
        tasks = window.dispense([("A", 1), ("A", 2)], 0.5, "time")
        await asyncio.sleep(0.15)
        window.cancel_heads([("A", 1)])
        await asyncio.gather(*tasks)

    with caplog.at_level("INFO", logger="JebaoGUI"):
        loop.run_until_complete(scenario())
    assert "JP 1 · CH1: dispensing cancelled" in caplog.text
    assert "JP 1 · CH2: done" in caplog.text


def test_failed_start_dispenses_nothing(pumps, qt_loop, monkeypatch, caplog):
    window, sent = pumps
    _, loop = qt_loop

    async def refuse(did, attrs):
        return False

    monkeypatch.setattr(window, "send_cloud_command", refuse)
    with caplog.at_level("ERROR", logger="JebaoGUI"):
        dispense_and_wait(loop, window, [("A", 1)], 5, "time")
    assert "Couldn't start JP 1" in caplog.text and window.head_runs == {}


def test_head_names_show_in_labels(pumps):
    window, _ = pumps
    window.config.set_head_name("A", 3, "EtOH → Col 3")
    assert window.head_label("A", 3) == "JP 1 · CH3 (EtOH → Col 3)"
    assert window.head_label("A", 1) == "JP 1 · CH1"
    window.config.get_pump("A")["display_name"] = "Ethanol pump"
    assert window.get_device_name("A") == "Ethanol pump"
