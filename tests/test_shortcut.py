"""Tests for shortcut.py. Each system's shortcut is built in a temp folder on any OS."""

import os
import plistlib
import subprocess

import pytest

import shortcut


def test_launch_command_runs_this_copy():
    program, args = shortcut.launch_command("darwin", python="/x/.venv/bin/python")
    assert program == "/x/.venv/bin/python"
    assert args == [shortcut.ENTRY_SCRIPT]
    assert os.path.exists(shortcut.ENTRY_SCRIPT)


def test_windows_prefers_pythonw(tmp_path):
    python = tmp_path / "python.exe"
    python.write_text("")
    (tmp_path / "pythonw.exe").write_text("")
    program, _ = shortcut.launch_command("win32", python=str(python))
    assert program == str(tmp_path / "pythonw.exe")


def test_mac_app_bundle(tmp_path):
    app = shortcut._create_mac(str(tmp_path), version="1.2.0")
    assert app.endswith("Jebao Pump Sequencer.app")
    with open(os.path.join(app, "Contents", "Info.plist"), "rb") as f:
        info = plistlib.load(f)
    assert info["CFBundleExecutable"] == "launch"
    assert info["CFBundleIconFile"] == "AppIcon"
    assert info["CFBundleShortVersionString"] == "1.2.0"
    assert os.path.getsize(os.path.join(app, "Contents", "Resources", "AppIcon.icns")) > 1000
    launcher = os.path.join(app, "Contents", "MacOS", "launch")
    text = open(launcher, encoding="utf-8").read()
    assert text.startswith("#!/bin/bash\n")
    assert "jebao_gui.py" in text
    if os.name != "nt":
        assert os.access(launcher, os.X_OK)
    # Creating it again replaces the old one
    assert shortcut._create_mac(str(tmp_path)) == app


def test_linux_desktop_file(tmp_path):
    path = shortcut._create_linux(str(tmp_path))
    text = open(path, encoding="utf-8").read()
    assert "Name=Jebao Pump Sequencer" in text
    assert "jebao_gui.py" in text and "jebao.png" in text


def test_windows_shortcut_passes_values_safely(tmp_path):
    seen = {}

    def fake_powershell(script, env=None):
        seen.update(env)
        open(env["JPS_LINK"], "w").close()  # pretend Windows made the file
        return subprocess.CompletedProcess([], 0, "", "")

    link = shortcut._create_windows(str(tmp_path), powershell=fake_powershell)
    assert link == str(tmp_path / "Jebao Pump Sequencer.lnk")
    assert seen["JPS_WORKDIR"] == shortcut.ROOT
    assert seen["JPS_ICON"].endswith("jebao.ico,0")
    assert "jebao_gui.py" in seen["JPS_ARGS"]
    assert "$env:JPS_LINK" in shortcut.LNK_SCRIPT


def test_windows_failure_has_a_message(tmp_path):
    def failing(script, env=None):
        return subprocess.CompletedProcess([], 1, "", "Access denied")

    with pytest.raises(shortcut.ShortcutError, match="Access denied"):
        shortcut._create_windows(str(tmp_path), powershell=failing)
