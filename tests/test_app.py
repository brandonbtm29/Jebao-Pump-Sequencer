"""Smoke tests for the main app module."""

import importlib.metadata
import json

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
    assert window.tabs.count() == 3


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
