"""Tests for the shared settings-folder logic."""

import json
import os

import pytest

import settings_location as sl


@pytest.fixture(autouse=True)
def no_env_override(monkeypatch):
    monkeypatch.delenv(sl.ENV_VAR, raising=False)


def test_default_is_the_app_folder(tmp_path):
    app, data = tmp_path / "app", tmp_path / "data"
    app.mkdir()
    assert sl.resolve(str(app), str(data)) == str(app)


def test_relative_pointer_is_inside_the_data_folder(tmp_path):
    app, data = tmp_path / "app", tmp_path / "data"
    app.mkdir()
    (app / sl.POINTER).write_text(json.dumps({"settings_dir": "Settings"}))
    resolved = sl.resolve(str(app), str(data))
    assert resolved == os.path.normpath(str(data / "Settings"))
    assert os.path.isdir(resolved)


def test_environment_variable_wins(tmp_path, monkeypatch):
    app = tmp_path / "app"
    app.mkdir()
    (app / sl.POINTER).write_text(json.dumps({"settings_dir": "Settings"}))
    monkeypatch.setenv(sl.ENV_VAR, str(tmp_path / "elsewhere"))
    assert sl.resolve(str(app), str(tmp_path / "data")) == os.path.normpath(str(tmp_path / "elsewhere"))


def test_portable_paths(tmp_path):
    data = tmp_path / "data"
    assert sl.portable(str(data / "Settings"), str(data)) == "Settings"
    home_path = os.path.join(os.path.expanduser("~"), "some", "folder")
    assert sl.portable(home_path, str(data)) == "~/some/folder"


def test_change_copies_missing_files_and_keeps_existing(tmp_path):
    app, data = tmp_path / "app", tmp_path / "data"
    app.mkdir()
    (app / "a.json").write_text("old a")
    (app / "b.json").write_text("old b")
    target = data / "Settings"
    target.mkdir(parents=True)
    (target / "b.json").write_text("synced b")

    new_dir, copied, kept = sl.change(str(app), str(data), str(target), ["a.json", "b.json"])

    assert new_dir == os.path.normpath(str(target))
    assert copied == ["a.json"] and kept == ["b.json"]
    assert (target / "b.json").read_text() == "synced b"
    assert json.loads((app / sl.POINTER).read_text()) == {"settings_dir": "Settings"}
