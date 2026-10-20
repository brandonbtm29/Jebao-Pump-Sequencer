"""Smoke tests for the main app module."""

import importlib.metadata
import json
import time

import pytest

import jebao_gui


class FakeResponse:
    status_code = 200
    text = "{}"

    def json(self):
        return {}


def test_installed_version_matches_source():
    assert importlib.metadata.version("jebao-pump-sequencer") == jebao_gui.__version__


def test_new_pump_gets_four_channel_defaults(tmp_path):
    cfg = jebao_gui.ConfigManager(filepath=str(tmp_path / "jebao_config.json"))
    pump = cfg.get_pump("did-1")
    for key in ("volumes", "units", "calibration", "tubing_id", "tubing_od"):
        assert len(pump[key]) == 4
    assert pump["calibration"] == [1.0, 1.0, 1.0, 1.0]
    assert pump["units"][0] == "Vol (mL)"


def test_old_pump_entries_gain_new_keys(tmp_path):
    path = tmp_path / "jebao_config.json"
    path.write_text(json.dumps({"pumps": {"did-1": {"volumes": [1, 2, 3, 4], "calibration": [2, 2, 2, 2]}}}))
    pump = jebao_gui.ConfigManager(filepath=str(path)).get_pump("did-1")
    assert pump["calibration"] == [2, 2, 2, 2]
    assert pump["tubing_id"] == [2.0, 2.0, 2.0, 2.0]
    assert pump["units"] == ["Vol (mL)"] * 4


def test_config_round_trip(tmp_path):
    path = str(tmp_path / "jebao_config.json")
    cfg = jebao_gui.ConfigManager(filepath=path)
    cfg.get_pump("did-1")["calibration"][2] = 3.5
    cfg.save()
    again = jebao_gui.ConfigManager(filepath=path)
    assert again.get_pump("did-1")["calibration"][2] == 3.5
    assert again.data["sequence"] == [] and again.data["saved_sequences"] == {}


def test_example_config_loads(tmp_path):
    example = jebao_gui.os.path.join(jebao_gui.script_dir, "examples", "jebao_config.example.json")
    cfg = jebao_gui.ConfigManager(filepath=example)
    assert cfg.data["sequence"][0]["action"] == "Dose"


def test_checkable_combo_reports_checked_channels(qt_loop):
    combo = jebao_gui.CheckableComboBox()
    combo.addItems(["1", "2", "3", "4"])
    combo.setCheckedItems(["2", "4"])
    assert combo.checkedItems() == ["2", "4"]


def test_window_starts_without_login(window):
    assert window.cloud_token is None
    assert [window.tabs.tabText(i) for i in range(window.tabs.count())] == [
        "Manual",
        "Sequencer",
        "Calibration",
        "Rack",
    ]


def test_control_command_payload(window, qt_loop, monkeypatch):
    """The cloud expects {"attrs": {"channeN": bool}} ("channe" is the API's spelling)."""
    _, loop = qt_loop
    calls = []

    def fake_post(url, headers=None, json=None, **kwargs):
        calls.append((url, headers, json))
        return FakeResponse()

    monkeypatch.setattr(jebao_gui.requests, "post", fake_post)
    window.cloud_token = "token-123"  # noqa: S105 (fake token)
    ok = loop.run_until_complete(window.send_cloud_command("DID42", {"channe3": True}))
    assert ok is True
    url, headers, body = calls[0]
    assert url == "https://usapi.gizwits.com/app/control/DID42"
    assert headers["X-Gizwits-User-token"] == "token-123"
    assert headers["X-Gizwits-Application-Id"] == jebao_gui.APP_ID
    assert body == {"attrs": {"channe3": True}}


def test_no_command_is_sent_when_logged_out(window, qt_loop):
    _, loop = qt_loop
    assert loop.run_until_complete(window.send_cloud_command("DID42", {"channe1": True})) is False


def test_emergency_stop_turns_off_every_channel_on_every_pump(window, qt_loop, monkeypatch):
    _, loop = qt_loop
    sent = []

    async def fake_send(did, attrs):
        sent.append((did, attrs))
        return True

    monkeypatch.setattr(window, "send_cloud_command", fake_send)
    window.cloud_devices = [{"did": "A"}, {"did": "B"}]
    loop.run_until_complete(window._async_stop_all())
    off = {"channe1": False, "channe2": False, "channe3": False, "channe4": False}
    assert sorted(sent, key=lambda s: s[0]) == [("A", off), ("B", off)]


def test_cloud_requests_have_a_timeout(window, qt_loop, monkeypatch):
    _, loop = qt_loop
    seen = {}

    def fake_post(url, **kwargs):
        seen.update(kwargs)
        return FakeResponse()

    monkeypatch.setattr(jebao_gui.requests, "post", fake_post)
    window.cloud_token = "token-123"  # noqa: S105 (fake token)
    loop.run_until_complete(window.send_cloud_command("DID42", {"channe1": False}))
    assert seen["timeout"] == jebao_gui.REQUEST_TIMEOUT > 0


def test_a_stalled_request_fails_instead_of_hanging(window, qt_loop, monkeypatch):
    _, loop = qt_loop

    def timed_out(*args, **kwargs):
        raise jebao_gui.requests.Timeout("no answer")

    monkeypatch.setattr(jebao_gui.requests, "post", timed_out)
    window.cloud_token = "token-123"  # noqa: S105 (fake token)
    assert loop.run_until_complete(window.send_cloud_command("DID42", {"channe1": False})) is False


def test_emergency_stop_reports_pumps_it_could_not_stop(window, qt_loop, monkeypatch, caplog):
    _, loop = qt_loop

    async def fake_send(did, attrs):
        if did == "B":
            raise RuntimeError("network down")
        return did != "C"

    monkeypatch.setattr(window, "send_cloud_command", fake_send)
    window.cloud_devices = [
        {"did": "A", "dev_alias": "JP 1"},
        {"did": "B", "dev_alias": "JP 2"},
        {"did": "C", "dev_alias": "JP 3"},
    ]
    with caplog.at_level("INFO", logger="JebaoGUI"):
        loop.run_until_complete(window._async_stop_all())
    errors = [r.getMessage() for r in caplog.records if r.name == "JebaoGUI" and r.levelname == "ERROR"]
    assert len(errors) == 1
    assert "2 of 3" in errors[0] and "JP 2" in errors[0] and "JP 3" in errors[0]
    assert "JP 1" not in errors[0]


def test_emergency_stop_with_no_pumps_says_so(window, qt_loop, caplog):
    _, loop = qt_loop
    window.cloud_devices = []
    with caplog.at_level("INFO", logger="JebaoGUI"):
        loop.run_until_complete(window._async_stop_all())
    assert any(
        r.name == "JebaoGUI" and r.levelname == "ERROR" and "STOP not sent" in r.getMessage()
        for r in caplog.records
    )


def test_unreadable_channel_is_logged_not_hidden(window, caplog):
    from PyQt6.QtWidgets import QComboBox

    window.add_sequence_step("Dose")
    bad = QComboBox()
    bad.addItem("not a channel")
    window.seq_table.setCellWidget(0, 1, bad)
    with caplog.at_level("WARNING", logger="JebaoGUI"):
        steps = window._get_sequence_data()
    assert steps[0]["channel"] == [1]
    assert any("couldn't read the channel" in r.getMessage() for r in caplog.records)


def test_icon_files_exist_and_load(qt_loop):
    from PyQt6.QtGui import QIcon

    for name in ("jebao.png", "jebao.ico", "jebao.icns"):
        path = jebao_gui.os.path.join(jebao_gui.ICON_DIR, name)
        assert jebao_gui.os.path.getsize(path) > 1000, name
    assert not QIcon(jebao_gui.os.path.join(jebao_gui.ICON_DIR, "jebao.png")).isNull()
    app, _ = qt_loop
    jebao_gui.set_app_icon(app)
    assert not app.windowIcon().isNull()


# --- Settings: App box (updates and desktop shortcut) ---------------------------------------


def test_settings_has_update_and_shortcut_buttons(window):
    dlg = jebao_gui.SettingsDialog(window.secrets, window.config, window)
    box = dlg.app_box
    assert box.btn_check.text() == "Check for Updates"
    assert box.btn_shortcut.text() == "Create Desktop Shortcut"
    assert box.main_window is window
    dlg.reject()


def test_busy_reason_blocks_updates_while_pumping(window):
    assert window.busy_reason() == ""
    window.sequence_queue.append({"name": "x", "data": []})
    assert "sequence" in window.busy_reason()
    window.sequence_queue.clear()
    window._motor_jobs = 1
    assert "dose" in window.busy_reason()
    window._motor_jobs = 0


def test_manual_dose_counts_as_busy_until_done(window, qt_loop, monkeypatch):
    _, loop = qt_loop
    seen = []

    async def fake_send(did, attrs):
        seen.append(window._motor_jobs)
        return True

    monkeypatch.setattr(window, "send_cloud_command", fake_send)
    loop.run_until_complete(window._manual_pump_dispense("A", [(1, 0.0)], 0, "time"))
    assert seen == [1, 1]  # busy while the motor commands were sent
    assert window._motor_jobs == 0


def test_check_result_messages(window):
    box = jebao_gui.AppBox(window)
    same = jebao_gui.updater.Release(jebao_gui.__version__, "v" + jebao_gui.__version__, "", "", "")
    box._checked(same)
    assert "latest version" in box.lbl_status.text()
    box._checked(LookupError("Couldn't reach GitHub. Check the internet connection."))
    assert "Couldn't reach GitHub" in box.lbl_status.text()
    assert box.btn_check.isEnabled()


def test_update_refused_while_busy(window, monkeypatch):
    warnings = []
    monkeypatch.setattr(jebao_gui.QMessageBox, "warning", lambda *a: warnings.append(a[2]))
    monkeypatch.setattr(jebao_gui.updater, "update_to", lambda *a, **k: pytest.fail("must not update"))
    box = jebao_gui.AppBox(window)
    window._motor_jobs = 1
    box._update(jebao_gui.updater.Release("9.0.0", "v9.0.0", "", "", ""))
    window._motor_jobs = 0
    assert warnings and "Emergency Stop" in warnings[0]


def test_successful_update_restarts(window, monkeypatch):
    restarted = []
    monkeypatch.setattr(jebao_gui.QMessageBox, "information", lambda *a: None)
    monkeypatch.setattr(window, "restart_app", lambda: restarted.append(True))
    box = jebao_gui.AppBox(window)
    box._updated(jebao_gui.updater.UpdateResult(True, "Updated to v9.0.0."))
    assert restarted == [True]


def test_failed_update_shows_why(window, monkeypatch):
    warnings = []
    monkeypatch.setattr(jebao_gui.QMessageBox, "warning", lambda *a: warnings.append(a[2]))
    monkeypatch.setattr(window, "restart_app", lambda: pytest.fail("must not restart"))
    box = jebao_gui.AppBox(window)
    box._updated(jebao_gui.updater.UpdateResult(False, "Couldn't download the update"))
    assert warnings == ["Couldn't download the update"]


def test_update_can_be_cancelled(window, monkeypatch):
    """The progress box has a Cancel button that stops the update and says so."""
    warnings = []
    monkeypatch.setattr(jebao_gui.QMessageBox, "warning", lambda *a: warnings.append(a[2]))
    monkeypatch.setattr(window, "restart_app", lambda: pytest.fail("must not restart"))

    def fake_update(tag, progress=None, cancel=None):
        progress("Installing the new dependencies…\nCollecting hatchling")
        assert cancel.wait(10)
        return jebao_gui.updater.UpdateResult(
            False, "The update was cancelled, so the app was left on the old version."
        )

    monkeypatch.setattr(jebao_gui.updater, "update_to", fake_update)
    box = jebao_gui.AppBox(window)
    box._update(jebao_gui.updater.Release("9.0.0", "v9.0.0", "", "", ""))
    app = jebao_gui.QApplication.instance()
    deadline = time.monotonic() + 5
    while "Collecting hatchling" not in box._progress.labelText() and time.monotonic() < deadline:
        app.processEvents()
    assert "so far" in box._progress.labelText()
    button = box._progress.findChild(jebao_gui.QPushButton)
    button.click()  # the Cancel button
    assert "Cancelling" in box._progress.labelText() and box._progress.isVisible()
    while not warnings and time.monotonic() < deadline + 5:
        app.processEvents()
    box._task.wait(2000)
    assert warnings and "cancelled" in warnings[0]
    assert box._progress is None and box.btn_check.isEnabled()


def test_shortcut_button(window, monkeypatch):
    shown = []
    monkeypatch.setattr(jebao_gui.shortcut, "create_shortcut", lambda version="": "/Desktop/Jebao.app")
    monkeypatch.setattr(jebao_gui.QMessageBox, "information", lambda *a: shown.append(a[2]))
    jebao_gui.AppBox(window).create_shortcut()
    assert shown == ["Created:\n/Desktop/Jebao.app"]


# --- version label, update check at launch, automatic desktop shortcut ---------------------


def _release(version):
    return jebao_gui.updater.Release(version, "v" + version, "Jebao " + version, "notes", "https://example")


def test_version_shown_bottom_right(window):
    assert window.version_button.text() == f"v{jebao_gui.__version__}"
    assert window.statusBar().isAncestorOf(window.version_button)


def test_version_label_turns_orange_when_outdated(window):
    window.show_update_state(_release("99.0.0"))
    assert "Update available: v99.0.0" in window.version_button.text()
    assert "E67E22" in window.version_button.styleSheet()
    window.show_update_state(_release(jebao_gui.__version__))
    assert window.version_button.text() == f"v{jebao_gui.__version__}"
    assert window.version_button.styleSheet() == ""
    window.show_update_state(LookupError("Couldn't reach GitHub."))
    assert "Couldn't reach GitHub" in window.version_button.toolTip()


def test_clicking_outdated_version_offers_the_update(window, monkeypatch):
    offered = []

    class FakeBox:
        def _offer(self, release):
            offered.append(release.version)

        def check(self):
            offered.append("check")

    class FakeDialog:
        app_box = FakeBox()

    monkeypatch.setattr(window, "open_settings", lambda then=None: then(FakeDialog()))
    window.on_version_clicked()
    window.show_update_state(_release("99.0.0"))
    window.on_version_clicked()
    assert offered == ["check", "99.0.0"]


def test_manual_check_also_updates_the_label(window, monkeypatch):
    box = jebao_gui.AppBox(window)
    monkeypatch.setattr(box, "_offer", lambda release: None)  # don't open the dialog
    box._checked(_release("99.0.0"))
    assert "Update available" in window.version_button.text()


def test_desktop_shortcut_made_on_first_launch_and_after_updates(window, monkeypatch):
    made = []
    monkeypatch.setattr(
        jebao_gui.shortcut, "create_shortcut", lambda version="": made.append(version) or "/D/J.app"
    )
    monkeypatch.setattr(window, "_run_in_background", lambda fn, done: done(fn(None)))

    window.maintain_desktop_shortcut()  # first launch
    assert made == [jebao_gui.__version__]
    state = jebao_gui.shortcut.load_state(jebao_gui.APP_DIR)
    assert state["version"] == jebao_gui.__version__ and state["path"] == "/D/J.app"

    window.maintain_desktop_shortcut()  # same version: nothing to do
    assert len(made) == 1

    jebao_gui.shortcut.save_state(jebao_gui.APP_DIR, {**state, "version": "0.9.0"})  # as if just updated
    window.maintain_desktop_shortcut()
    assert len(made) == 2


def test_automatic_shortcut_can_be_turned_off(window, monkeypatch):
    monkeypatch.setattr(
        jebao_gui.shortcut, "create_shortcut", lambda version="": pytest.fail("must not create")
    )
    box = jebao_gui.AppBox(window)
    assert box.chk_auto_shortcut.isChecked()
    box.chk_auto_shortcut.setChecked(False)
    assert jebao_gui.shortcut.load_state(jebao_gui.APP_DIR)["auto"] is False
    window.maintain_desktop_shortcut()


def test_failed_automatic_shortcut_is_logged_and_retried_next_launch(window, caplog):
    with caplog.at_level("WARNING", logger="JebaoGUI"):
        window._shortcut_done(RuntimeError("no desktop"), first=True)
    assert "no desktop" in caplog.text
    assert jebao_gui.shortcut.load_state(jebao_gui.APP_DIR) == {}


# --- persistent system log ------------------------------------------------------------------


def test_history_is_shown_when_the_window_opens(qt_loop, monkeypatch, tmp_path):
    monkeypatch.setattr(jebao_gui, "APP_DIR", str(tmp_path))
    handlers_before = list(jebao_gui.logger.handlers)
    win = jebao_gui.JebaoPumpGUI(history=["── Yesterday ──", "[17:58:41] Sequence done"])
    try:
        text = win.log_text.toPlainText()
        assert text.index("Sequence done") < text.index("App started") < text.index("Initialized")
    finally:
        for handler in jebao_gui.logger.handlers[:]:
            if handler not in handlers_before:
                jebao_gui.logger.removeHandler(handler)
        win.close()


def test_logging_after_the_log_box_is_gone_is_harmless(qt_loop):
    from PyQt6 import sip
    from PyQt6.QtWidgets import QTextEdit

    box = QTextEdit()
    handler = jebao_gui.GUILogHandler(box)
    sip.delete(box)
    record = jebao_gui.logging.LogRecord(
        "JebaoGUI", jebao_gui.logging.INFO, __file__, 1, "late line", None, None
    )
    handler.emit(record)  # must not raise, even with no excepthook installed


def test_log_retention_is_a_setting(window, monkeypatch):
    pruned = []
    monkeypatch.setattr(jebao_gui.eventlog, "prune", lambda folder, days: pruned.append(days))
    dlg = jebao_gui.SettingsDialog(window.secrets, window.config, window)
    assert dlg.log_days_spin.value() == window.config.data.get("log_keep_days", 30)
    dlg.log_days_spin.setValue(45)
    dlg.accept()
    assert window.config.data["log_keep_days"] == 45 and pruned == [45]


# --- quick wins: approvals banner, sorting, no Restart button, clock --------------------------


def test_approval_banner_only_while_waiting(window):
    import asyncio

    assert window.approval_banner.isHidden()
    window.current_running_sequence = "Load sample"
    window._ch_review_events[1] = asyncio.Event()
    window._ch_review_events[3] = asyncio.Event()
    window.refresh_approval_banner()
    assert not window.approval_banner.isHidden()
    assert "CH1, CH3" in window.lbl_approval.text() and "Load sample" in window.lbl_approval.text()
    assert not window.btn_approve_channels[0].isHidden() and window.btn_approve_channels[1].isHidden()
    window.approve_all()
    assert window._ch_review_events[1] is None and window.approval_banner.isHidden()


def test_natural_sort_key():
    names = ["JP 10: Sample", "JP 2: Water", "jp 1: EtOH", "JP 1b"]
    assert sorted(names, key=jebao_gui.natural_key) == ["jp 1: EtOH", "JP 1b", "JP 2: Water", "JP 10: Sample"]


def test_first_run_puts_pumps_in_one_stack_in_the_old_order(window):
    window.config.data.pop("rack", None)
    window.config.data["pump_order"] = ["b", "a"]
    window.cloud_devices = [
        {"did": d, "dev_alias": n, "is_online": True}
        for d, n in [("a", "JP 1"), ("b", "JP 2"), ("c", "JP 3")]
    ]
    window.refresh_pumps()
    assert window.config.data["rack"]["stacks"][0]["slots"] == ["b", "a", "c"]
    assert [d["did"] for d in window.cloud_devices] == ["b", "a", "c"]
    window.config.data.pop("rack", None)


def test_restart_lives_in_settings_and_clock_in_status_bar(window):
    assert not hasattr(window, "btn_restart")
    assert window.statusBar().isAncestorOf(window.lbl_clock)
    dlg = jebao_gui.SettingsDialog(window.secrets, window.config, window)
    texts = [b.text() for b in dlg.app_box.findChildren(jebao_gui.QPushButton)]
    assert "Restart App" in texts
    dlg.reject()


def test_logging_from_a_background_thread_is_safe(window, qt_loop):
    """Logging from a worker thread (e.g. a failed update check) used to raise
    'There is no current event loop in thread' and could stop the app."""
    import threading

    from PyQt6.QtCore import QCoreApplication

    errors = []
    t = threading.Thread(target=lambda: _log_or_record(errors), name="worker")
    t.start()
    t.join(5)
    for _ in range(20):
        QCoreApplication.processEvents()
    assert errors == []
    assert "hello from a worker thread" in window.log_text.toPlainText()


def _log_or_record(errors):
    try:
        jebao_gui.logger.info("hello from a worker thread")
    except Exception as e:  # noqa: BLE001 - the test records any failure
        errors.append(e)


def test_failed_update_check_in_a_qthread_reaches_the_label(window, qt_loop, monkeypatch):
    from PyQt6.QtCore import QCoreApplication

    def offline(_version):
        raise LookupError("Couldn't reach GitHub. Check the internet connection.")

    monkeypatch.setattr(jebao_gui.updater, "latest_release", offline)
    task = window._run_in_background(
        lambda _say: jebao_gui.updater.latest_release("x"), window.show_update_state
    )
    assert task.wait(5000)
    for _ in range(20):
        QCoreApplication.processEvents()
    assert "Couldn't reach GitHub" in window.version_button.toolTip()
    assert "Update check: Couldn't reach GitHub" in window.log_text.toPlainText()
