"""The Manual and Rack tabs, driven the way a person would: clicks, drags (by method), names."""

import pytest

import jebao_gui
import rack


@pytest.fixture
def bench(window, monkeypatch):
    """Five pumps (JP 4 offline) in a 1 × 3 rack, two not placed."""
    window.cloud_devices = [
        {"did": f"D{n}", "dev_alias": f"JP {n}", "is_online": n != 4} for n in (5, 3, 1, 2, 4)
    ]
    layout = rack.resize(rack.default_layout(["D1", "D2", "D3"]), 1, 3)
    monkeypatch.setitem(window.config.data, "rack", layout)
    for n in range(1, 6):
        window.config.get_pump(f"D{n}")["calibration"] = [2.0, 2.0, 2.0, 2.0]
    window.refresh_pumps()
    calls = []
    monkeypatch.setattr(
        window, "dispense", lambda heads, value, unit: calls.append((heads, value, unit)) or []
    )
    return window, calls


def clear_names(window):
    """Earlier tests name pumps in the shared test settings: start these from no names."""
    for n in range(1, 6):
        pump = window.config.get_pump(f"D{n}")
        pump["display_name"] = ""
        pump["head_names"] = ["", "", "", ""]
    window.refresh_pumps()


def settle():
    """Let the Rack tab rebuild: it does that just after the click or edit that changed something."""
    jebao_gui.QApplication.processEvents()


def tile(window, did, ch):
    return next(t for t in window.manual_tab.tiles() if (t.did, t.ch) == (did, ch))


def test_manual_tab_shows_rack_order_then_unplaced(bench):
    window, _ = bench
    dids = [c.did for c in window.manual_tab.cards]
    assert dids == ["D1", "D2", "D3", "D5", "D4"]  # rack first, then "Not in the rack"


def test_select_heads_and_dispense(bench):
    window, calls = bench
    tab = window.manual_tab
    tile(window, "D2", 3).click()
    tab.toggle_pump("D1")  # "All 4"
    assert tab.selected == {("D1", 1), ("D1", 2), ("D1", 3), ("D1", 4), ("D2", 3)}
    assert tab.btn_dispense.text() == "Dispense on 5 heads"
    tab.rb_time.setChecked(True)
    tab.spin_amount.setValue(30)
    tab.btn_dispense.click()
    heads, value, unit = calls[-1]
    assert heads == sorted(tab.selected) and value == 30 and unit == "time"
    tab.toggle_pump("D1")
    assert tab.selected == {("D2", 3)}
    tab.clear_selection()
    assert not tab.btn_dispense.isEnabled()


def test_offline_pump_tiles_are_disabled(bench):
    window, _ = bench
    assert not tile(window, "D4", 1).isEnabled()
    assert tile(window, "D4", 1).lbl_status.text() == "Pump offline"


def test_tiles_show_running_and_paused_heads(bench):
    window, _ = bench
    window.head_runs[("D1", 2)] = jebao_gui.HeadRun("D1", 2, 100, "manual", remaining=25)
    paused = jebao_gui.HeadRun("D2", 1, 60, "sequence", remaining=30)
    paused.paused = True
    window.head_runs[("D2", 1)] = paused
    window.manual_tab.refresh()
    assert tile(window, "D1", 2).lbl_status.text() == "Running · 0:25 left"
    assert tile(window, "D1", 2).bar.value() == 750
    assert tile(window, "D2", 1).lbl_status.text().startswith("Paused")
    window.head_runs.clear()


def test_estimate_uses_each_heads_rate(bench):
    window, _ = bench
    tab = window.manual_tab
    window.config.get_pump("D1")["calibration"][0] = 4.0
    tab.select([("D1", 1), ("D2", 1)])
    tab.rb_volume.setChecked(True)
    tab.spin_amount.setValue(10)
    assert "2:30–5:00" in tab.lbl_estimate.text()


def test_tile_size_is_remembered(bench):
    window, _ = bench
    window.manual_tab.set_tile_size("L")
    assert window.config.data["ui"]["tile_size"] == "L"
    assert tile(window, "D1", 1).height() == 108


def test_uncalibrated_head_is_flagged(bench):
    window, _ = bench
    window.config.get_pump("D3")["calibration"][1] = 1.0
    window.manual_tab.refresh()
    assert "not calibrated" in tile(window, "D3", 2).lbl_status.text()


# --- Rack tab -------------------------------------------------------------------------------


def test_tray_lists_unplaced_pumps_sorted(bench):
    window, _ = bench
    tray = window.rack_tab.tray
    assert [tray.item(i).text() for i in range(tray.count())] == ["⠿  JP 4", "⠿  JP 5"]


def test_drop_on_a_slot_places_and_swaps(bench):
    window, _ = bench
    tab = window.rack_tab
    tab.place("D5", 0, 0)  # what a drop of JP 5 on slot 1 does
    assert window.config.data["rack"]["stacks"][0]["slots"] == ["D5", "D2", "D3"]
    settle()
    assert [c.did for c in window.manual_tab.cards][:3] == ["D5", "D2", "D3"]  # Manual follows
    tab.remove("D2")  # drop back on the tray, or the × button
    assert window.config.data["rack"]["stacks"][0]["slots"] == ["D5", None, "D3"]


def test_click_a_tray_pump_then_an_empty_slot(bench):
    window, _ = bench
    tab = window.rack_tab
    tab.remove("D3")
    settle()
    item = next(tab.tray.item(i) for i in range(tab.tray.count()) if tab.tray.item(i).text().endswith("JP 5"))
    tab._tray_clicked(item)
    empty = next(s for s in tab.slots if s.did is None)
    tab.slot_clicked(empty)
    assert window.config.data["rack"]["stacks"][0]["slots"][2] == "D5"


def test_resize_and_auto_arrange(bench):
    window, _ = bench
    tab = window.rack_tab
    tab.spin_stacks.setValue(2)
    tab.spin_slots.setValue(2)
    tab.auto_arrange()
    settle()
    stacks = window.config.data["rack"]["stacks"]
    assert [s["slots"] for s in stacks] == [["D1", "D2"], ["D3", "D4"]]
    assert len(tab.slots) == 4 and tab.tray.count() == 1


def test_names_are_saved_and_shown(bench):
    window, _ = bench
    tab = window.rack_tab
    slot = next(s for s in tab.slots if s.did == "D2")
    tab.slot_clicked(slot)
    tab.edit_pump.setText("Water pump")
    tab.edit_heads[2].setText("Water → Col 3")
    tab._save_names()
    assert window.get_device_name("D2") == "Water pump"
    assert window.config.head_name("D2", 3) == "Water → Col 3"
    settle()
    assert tile(window, "D2", 3).lbl_name.text() == "Water → Col 3"
    cal_tile = next(t for t in window.calibration_tab.grid.tiles() if (t.did, t.ch) == ("D2", 3))
    assert cal_tile.lbl_name.text() == "Water → Col 3"


def _drop_event(did):
    from PyQt6.QtCore import QMimeData, QPointF, Qt
    from PyQt6.QtGui import QDropEvent

    import rack_widgets

    mime = QMimeData()
    mime.setData(rack_widgets.MIME_DID, did.encode())
    event = QDropEvent(
        QPointF(5, 5),
        Qt.DropAction.MoveAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    return event, mime  # keep mime alive while the event is used


def test_real_drop_events_on_slots_and_tray(bench):
    window, _ = bench
    tab = window.rack_tab
    target = next(s for s in tab.slots if s.did == "D3")
    event, _mime = _drop_event("D4")
    target.dropEvent(event)
    assert window.config.data["rack"]["stacks"][0]["slots"] == ["D1", "D2", "D4"]
    event, _mime = _drop_event("D1")
    tab.tray.dropEvent(event)
    assert window.config.data["rack"]["stacks"][0]["slots"] == [None, "D2", "D4"]


def test_renaming_then_clicking_in_the_rack_doesnt_crash(bench):
    # Typing a name and then clicking a stack name or a × button used to rebuild the rack
    # while Qt was still handling that click, on the widget it had just deleted: a crash.
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QLineEdit, QPushButton

    window, _ = bench
    clear_names(window)
    window.show()
    window.activateWindow()
    window.tabs.setCurrentWidget(window.rack_tab)
    settle()
    tab = window.rack_tab
    tab.slot_clicked(next(s for s in tab.slots if s.did == "D1"))
    tab.edit_pump.setFocus()
    settle()
    QTest.keyClicks(tab.edit_pump, "EtOH pump")
    QTest.mouseClick(tab.scroll.widget().findChildren(QLineEdit)[0], Qt.MouseButton.LeftButton)
    settle()
    assert window.get_device_name("D1") == "EtOH pump"
    tab.edit_heads[0].setFocus()
    settle()
    QTest.keyClicks(tab.edit_heads[0], "EtOH to Col 1")  # keyClicks can only type ASCII
    remove = [b for b in tab.scroll.widget().findChildren(QPushButton) if b.text() == "×"][-1]
    QTest.mouseClick(remove, Qt.MouseButton.LeftButton)
    settle()
    assert window.config.head_name("D1", 1) == "EtOH to Col 1"
    assert window.config.data["rack"]["stacks"][0]["slots"] == ["D1", "D2", None]
    window.hide()


def test_names_typed_for_one_pump_stay_with_it(bench):
    window, _ = bench
    clear_names(window)
    tab = window.rack_tab
    tab.slot_clicked(next(s for s in tab.slots if s.did == "D1"))
    tab.edit_pump.setText("EtOH pump")  # typed, still in the box
    tab.slot_clicked(next(s for s in tab.slots if s.did == "D2"))  # then the next pump clicked
    settle()
    assert window.get_device_name("D1") == "EtOH pump"
    assert window.config.display_name("D2") == "" and tab.edit_pump.text() == ""
    tab._save_names()  # leaving D2's empty box changes nothing
    assert window.config.display_name("D2") == ""


def test_jebao_app_name_is_shown_next_to_your_own(bench):
    window, _ = bench
    clear_names(window)
    window.config.get_pump("D1")["display_name"] = "EtOH pump"
    next(d for d in window.cloud_devices if d["did"] == "D1").update(
        mac="AABBCCDDEEFF", product_name="Doser 2.4"
    )
    window.refresh_pumps()
    assert window.cloud_name("D1") == "JP 1" and window.cloud_note("D1") == "Jebao app: JP 1"
    assert window.cloud_note("D2") == ""  # no own name: the Jebao name is the name
    card = next(c for c in window.manual_tab.cards if c.did == "D1")
    card.refresh()
    assert "Jebao app: JP 1" in card.note.text()
    tab = window.rack_tab
    slot = next(s for s in tab.slots if s.did == "D1")
    assert slot.toolTip() == "Jebao app: JP 1"
    tab.slot_clicked(slot)
    text = tab.lbl_current.text()
    assert "Name in the Jebao app: JP 1" in text and "Doser 2.4" in text and "AABBCCDDEEFF" in text
    from network_report import NetworkDialog

    rows = NetworkDialog(window, window).rows()
    assert any(r["name"] == "EtOH pump (Jebao app: JP 1)" for r in rows)
