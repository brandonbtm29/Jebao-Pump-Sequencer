"""The Sequencer tab: editing steps, picking heads, saving, the queue, preview and running views."""

import json

import pytest
from PyQt6.QtCore import Qt

import sequence as sq
from sequencer_tab import UNTITLED


@pytest.fixture
def tab(window, monkeypatch):
    window.cloud_devices = [
        {"did": "A", "dev_alias": "JP 1", "is_online": True},
        {"did": "B", "dev_alias": "JP 2", "is_online": False},
    ]
    for did in ("A", "B"):
        pump = window.config.get_pump(did)
        pump["calibration"] = [2.0] * 4
        pump["calibrated_at"] = ["2026-10-29"] * 4
        pump["head_names"] = [f"EtOH → Col {c}" for c in range(1, 5)]
    window.config.data["saved_sequences"] = {}
    window.refresh_pumps()
    t = window.sequencer_tab
    t._set_draft(UNTITLED, [], None)
    return t


def answer(monkeypatch, button):
    from PyQt6.QtWidgets import QMessageBox

    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: button)


def test_add_steps_copies_heads_and_undo_redo(tab):
    tab.add_step("dose")
    assert tab.steps[0]["kind"] == "dose" and tab.steps[0]["heads"] == []
    tab._toggle_pump(0, "A")
    assert tab.steps[0]["heads"] == [["A", c] for c in range(1, 5)]
    tab.add_step("dose")  # a new pumping step starts with the same heads
    assert tab.steps[1]["heads"] == tab.steps[0]["heads"]
    tab.add_step("review")
    assert tab.steps[2]["lanes"] == [1, 2, 3, 4]
    assert tab.list.count() == 3
    tab.undo()
    assert len(tab.steps) == 2
    tab.redo()
    assert len(tab.steps) == 3 and tab.steps[2]["kind"] == "review"


def test_head_chips_and_card_text(tab):
    tab.add_step("dose")
    tab._toggle_head(0, "A", 2)
    tab._toggle_head(0, "A", 1)
    assert tab.steps[0]["heads"] == [["A", 1], ["A", 2]]  # kept in rack order
    tab._rebuild_inspector()
    assert tab._chips[("A", 1)].isChecked() and not tab._chips[("A", 3)].isChecked()
    assert tab._chips[("A", 1)].text() == "Col 1"
    assert "offline" in tab._chips[("B", 1)].toolTip()
    tab._replan()
    card = tab.list.item(0).data(Qt.ItemDataRole.UserRole)
    assert card["where"] == "EtOH → Col 1–2 (JP 1 · CH1–2)"
    assert card["time"] == "2 min 30 s"  # 5 mL at 2 mL/min


def test_edits_are_saved_as_a_draft_and_one_undo_step(tab, window):
    tab.add_step("wait")
    tab._edit(0, key="seconds", seconds=10)
    tab._edit(0, key="seconds", seconds=20)  # typing: the same Undo step
    tab.save_draft()
    saved = window.config.data["sequence"]
    assert saved[0]["kind"] == "wait" and saved[0]["seconds"] == 20 and saved[0]["action"] == "Pause"
    tab.undo()
    assert tab.steps[0]["seconds"] == 60.0


def test_save_open_and_unsaved_changes(tab, window, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox

    tab.add_step("wait")
    assert tab.is_unsaved()
    tab.name_edit.setText("Rinse")
    tab._name_edited()
    assert tab.save()
    assert "Rinse" in window.config.data["saved_sequences"] and not tab.is_unsaved()
    tab._edit(0, seconds=5)
    assert tab.is_unsaved()
    monkeypatch.setattr(QMessageBox, "exec", lambda self: QMessageBox.StandardButton.Discard)
    window.config.data["saved_sequences"]["Other"] = sq.to_saved_all([sq.new_step("review")])
    tab.open_sequence("Other")
    assert tab.source == "Other" and tab.steps[0]["kind"] == "review"
    assert window.config.data["saved_sequences"]["Rinse"][0]["seconds"] == 60.0  # discarded


def test_rename_keeps_sequences_that_run_it_working(tab, window, monkeypatch):
    from PyQt6.QtWidgets import QInputDialog

    lib = window.config.data["saved_sequences"]
    lib["Elute"] = sq.to_saved_all([sq.new_step("wait")])
    lib["Main"] = sq.to_saved_all([{**sq.new_step("sequence"), "name": "Elute"}])
    lib["Old"] = [{"pump_did": None, "channel": [1], "action": "Run Sub-Sequence", "val1": "Elute"}]
    tab.refresh_library()
    for r in range(tab.library.count()):
        if tab.library.item(r).data(Qt.ItemDataRole.UserRole) == "Elute":
            tab.library.setCurrentRow(r)
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("Elute 2", True))
    tab.rename_saved()
    assert "Elute 2" in lib and "Elute" not in lib
    assert sq.normalize(lib["Main"][0])["name"] == "Elute 2"
    assert sq.normalize(lib["Old"][0])["name"] == "Elute 2"


def test_export_and_import(tab, window, tmp_path, monkeypatch):
    from PyQt6.QtWidgets import QFileDialog

    window.config.data["saved_sequences"]["Wash"] = sq.to_saved_all([sq.new_step("wait")])
    tab.refresh_library()
    tab.library.setCurrentRow(0)
    path = tmp_path / "wash.jebao-sequence.json"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(path), ""))
    tab.export_sequence()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["name"] == "Wash" and data["steps"][0]["kind"] == "wait"
    name = tab.import_file(str(path))
    assert name == "Wash (2)" and "Wash (2)" in window.config.data["saved_sequences"]
    plain = tmp_path / "old.json"
    plain.write_text(json.dumps([{"pump_did": "A", "channel": [1], "action": "Dose", "val1": 1}]))
    assert tab.import_file(str(plain)) == "old"


def test_reorder_by_drag_and_keyboard_moves(tab):
    for kind in ("dose", "wait", "review"):
        tab.add_step(kind)
    item = tab.list.takeItem(0)  # as if the first card were dropped at the end
    tab.list.addItem(item)
    tab._reordered()
    assert [s["kind"] for s in tab.steps] == ["wait", "review", "dose"]
    tab.list.clearSelection()
    tab.list.item(2).setSelected(True)
    tab.move_steps(-1)
    assert [s["kind"] for s in tab.steps] == ["wait", "dose", "review"]
    tab.list.clearSelection()
    tab.list.item(0).setSelected(True)
    tab.copy_steps()
    tab.paste_steps()
    tab.delete_steps()
    assert [s["kind"] for s in tab.steps] == ["wait", "dose", "review"]


def test_change_kind_keeps_label_and_lanes(tab):
    tab.add_step("wait")
    tab._edit(0, label="Dry", cols=[2, 3])
    tab._change_kind(0, "review")
    assert (
        tab.steps[0]["kind"] == "review" and tab.steps[0]["label"] == "Dry" and tab.steps[0]["cols"] == [2, 3]
    )


def test_run_now_puts_it_first_in_the_queue(tab, window):
    window.sequence_queue.append({"name": "later", "data": []})
    window.queue_paused = True
    tab.add_step("wait")
    tab.run_now()
    assert window.sequence_queue[0]["name"] == UNTITLED
    assert window.queue_paused and window._run_one  # this one starts; the rest stay paused
    assert window.sequence_queue[0]["data"][0]["kind"] == "wait"
    assert tab.pages.currentIndex() == 2
    tab.refresh_queue()
    assert tab.queue_list.count() == 2
    tab.queue_list.setCurrentRow(1)
    tab._move_queued(-1)
    assert [q["name"] for q in window.sequence_queue] == ["later", UNTITLED]
    tab._remove_queued()
    assert [q["name"] for q in window.sequence_queue] == [UNTITLED]
    window.sequence_queue.clear()
    window.queue_paused = window._run_one = False


def test_problems_ask_before_running(tab, window, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox

    tab.add_step("dose")  # no heads: a problem
    answer(monkeypatch, QMessageBox.StandardButton.No)
    tab.add_to_queue()
    assert window.sequence_queue == []
    answer(monkeypatch, QMessageBox.StandardButton.Yes)
    tab.add_to_queue()
    assert len(window.sequence_queue) == 1
    window.sequence_queue.clear()


def test_preview_shows_totals_and_timeline(tab):
    tab.add_step("dose")
    tab._toggle_pump(0, "A")
    tab.add_step("review")
    tab.show_page(1)
    assert tab.card_total.value.text() == "2 min 30 s"
    assert "after 2 min 30 s" in tab.card_needed.value.text()
    assert "EtOH 20 mL" in tab.card_liquid.value.text()
    assert tab.head_table.rowCount() == 4
    lane, seg = tab.timeline.segment_at(
        type("P", (), {"x": lambda self: tab.timeline.LEFT + 5, "y": lambda self: tab.timeline.TOP + 15})()
    )
    assert lane == 1 and seg is not None and seg.kind == "pump"


def test_running_view_follows_a_run(tab, window):
    tab.add_step("dose")
    tab._toggle_head(0, "A", 1)
    tab.add_step("review")
    plan = window.plan_sequence(tab.steps)
    run = sq.RunState("Test", tab.steps, plan.flat, plan=plan)
    run.begin(1, plan.flat[0])
    window.seq_run = run
    tab.show_page(2)
    assert not tab.rn_header.isHidden() and tab.rn_state.text() == "RUNNING"
    assert tab.grid.item(0, 1).text() == "▶ now"
    assert tab.lane_cards[1].state.text() == "PUMPING"
    run.end(1, plan.flat[0])
    run.finish("done")
    tab.refresh_running()
    assert tab.rn_state.text() == "FINISHED" and tab.grid.item(0, 1).text() == "✓"
    window.seq_run = None


def test_old_step_table_opens_as_its_saved_sequence(window):
    from sequencer_tab import SequencerTab

    old = [{"pump_did": "A", "channel": [1], "action": "Dose", "val1": 1.0, "val2": 0, "val3": 0}]
    window.config.data["saved_sequences"] = {"Prime": old}
    window.config.data["sequence"] = old
    window.config.data.pop("sequence_name", None)
    window.config.data.pop("sequence_source", None)
    t = SequencerTab(window)
    assert t.name == "Prime" and t.source == "Prime" and not t.is_unsaved()
    assert t.steps[0]["kind"] == "dose" and t.steps[0]["heads"] == [["A", 1]]


def test_odd_saved_data_doesnt_break_the_tab(window, tmp_path):
    from sequencer_tab import SequencerTab

    odd = {"kind": "staccato", "volume": 1e308, "burst": 1e-300, "heads": [["A", 1.0]], "lanes": 2.0}
    window.config.data["sequence"] = [odd, {"action": "Mystery"}, "junk"]
    window.config.data.pop("sequence_source", None)
    t = SequencerTab(window)
    assert len(t.steps) == 3 and t.plan.errors
    assert t.steps[0]["heads"] == [["A", 1]] and t.steps[0]["volume"] == sq.MAX_ML
    assert t.steps[1]["kind"] == "wait" and "Mystery" in t.steps[1]["label"]
    path = tmp_path / "odd.json"
    path.write_text(json.dumps({"name": "Odd", "steps": [odd]}))
    assert t.import_file(str(path)) == "Odd"
    t.refresh_library()


def test_wait_for_me_message_is_edited_in_the_settings(tab):
    from PyQt6.QtWidgets import QPlainTextEdit

    tab.add_step("review")
    editor = tab.insp.findChild(QPlainTextEdit)
    assert editor is not None
    editor.setPlainText("Swap the sample line to tube 2.")
    assert tab.steps[0]["message"] == "Swap the sample line to tube 2."
    tab._replan()
    card = tab.list.item(0).data(Qt.ItemDataRole.UserRole)
    assert "Swap the sample line to tube 2." in card["where"]
    tab.show_page(1)
    assert "Swap the sample line" in tab.card_needed.note.text()


def select_saved(tab, name):
    for r in range(tab.library.count()):
        if tab.library.item(r).data(Qt.ItemDataRole.UserRole) == name:
            tab.library.setCurrentRow(r)


def test_each_sequence_keeps_its_own_checks(tab, window, monkeypatch, tmp_path):
    from PyQt6.QtWidgets import QFileDialog, QInputDialog

    from sequencer_tab import ChecksDialog

    window.config.data["saved_sequence_checks"] = {}
    tab.add_step("dose")
    tab._toggle_head(0, "A", 1)
    assert tab.checks == sq.DEFAULT_CHECKS
    dlg = ChecksDialog("Mix", tab.checks)
    dlg.combos["same_channel"].setCurrentIndex(dlg.combos["same_channel"].findData("off"))
    dlg.spin_days.setValue(7)
    wanted = dlg.values()
    assert wanted["same_channel"] == "off" and wanted["calibration_days"] == 7
    monkeypatch.setattr(ChecksDialog, "exec", lambda self: 1)
    monkeypatch.setattr(ChecksDialog, "values", lambda self: wanted)
    tab.edit_checks()
    assert tab.checks["same_channel"] == "off" and tab.checks["calibration_days"] == 7
    tab.save_draft()  # (soon after a change, by itself)
    assert window.config.data["sequence_checks"]["same_channel"] == "off"  # in the draft
    tab.undo()
    assert tab.checks == sq.DEFAULT_CHECKS
    tab.redo()
    assert tab.save("Mix")
    assert window.config.data["saved_sequence_checks"]["Mix"]["same_channel"] == "off"
    assert not tab.is_unsaved()
    # a new sequence starts from the defaults; opening Mix again brings its checks back
    tab.new_sequence()
    assert tab.checks == sq.DEFAULT_CHECKS
    tab.open_sequence("Mix")
    assert tab.checks["same_channel"] == "off"
    # the plan and the queue use them
    assert not any("into CH" in c.text for c in tab.plan.checks)
    # rename, duplicate, export and import carry them; delete drops them
    tab.refresh_library()
    select_saved(tab, "Mix")
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("Mix 2", True))
    tab.rename_saved()
    checks = window.config.data["saved_sequence_checks"]
    assert "Mix" not in checks and checks["Mix 2"]["same_channel"] == "off"
    select_saved(tab, "Mix 2")
    tab.duplicate_saved()
    assert checks["Mix 2 (copy)"]["calibration_days"] == 7
    path = tmp_path / "mix.json"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(path), ""))
    select_saved(tab, "Mix 2")
    tab.export_sequence()
    assert json.loads(path.read_text(encoding="utf-8"))["checks"]["same_channel"] == "off"
    name = tab.import_file(str(path))
    assert checks[name]["same_channel"] == "off"
    answer(
        monkeypatch, __import__("PyQt6.QtWidgets", fromlist=["QMessageBox"]).QMessageBox.StandardButton.Yes
    )
    select_saved(tab, "Mix 2 (copy)")
    tab.delete_saved()
    assert "Mix 2 (copy)" not in checks


def test_a_two_heads_check_asks_before_running(tab, window, monkeypatch):
    asked = []
    from PyQt6.QtWidgets import QMessageBox

    monkeypatch.setattr(
        QMessageBox, "question", lambda *a, **k: asked.append(a[2]) or QMessageBox.StandardButton.No
    )
    tab._set_draft("Mix", [{**sq.new_step("dose", heads=[["A", 1], ["B", 1]]), "volume": 1}], None)
    window.queue_paused = True
    tab.add_to_queue()
    assert asked and "would pump into CH1 at the same time" in asked[0] and window.sequence_queue == []
    tab.checks = sq.checks_settings({"same_channel": "warn"})
    tab.add_to_queue()
    assert window.sequence_queue[-1]["checks"]["same_channel"] == "warn"
    window.sequence_queue.clear()
    window.queue_paused = False


def test_adding_duplicating_and_deleting_steps_opens_no_windows(tab, window, qt_loop):
    """The step settings are redrawn without any widget becoming a window of its own for a
    moment (that flashed small windows on Windows and made each change slow)."""
    from PyQt6.QtCore import QEvent, QObject

    app = qt_loop[0]
    window.show()
    window.tabs.setCurrentWidget(tab)
    tab.show_page(0)
    app.processEvents()
    windows = []

    class Watch(QObject):
        def eventFilter(self, obj, event):
            if (
                event.type() == QEvent.Type.Show
                and obj.isWidgetType()
                and obj.isWindow()
                and obj is not window
            ):
                windows.append(type(obj).__name__)
            return False

    watch = Watch()
    app.installEventFilter(watch)
    try:
        for kind in ("dose", "review", "wait", "staccato"):
            tab.add_step(kind)
            app.processEvents()
        tab.list.setCurrentRow(1)
        app.processEvents()
        tab.duplicate_steps()
        app.processEvents()
        tab.delete_steps()
        app.processEvents()
        tab.undo()
        app.processEvents()
    finally:
        app.removeEventFilter(watch)
    assert windows == []
