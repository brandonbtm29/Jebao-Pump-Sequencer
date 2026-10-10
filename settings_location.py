"""Where this app keeps its settings files (config.json, secrets.json, ...).

Shared by the UAkron lab apps; an identical copy lives in each repo.

* Default: the app's own folder (the repo clone, or the folder of the .exe in a
  frozen build). Settings files there are gitignored, so a fresh clone starts
  from the app's defaults.
* A per-machine pointer file, ``settings_location.json`` in the app folder
  (also gitignored), can move them, e.g. ``{"settings_dir": "Settings"}``.
  A relative path is relative to the app's data folder, so "Settings" means
  ``~/SyncThing/UAkron Playground/<App>/Settings`` and syncs between computers.
  ``~/...`` and absolute paths work too.
* The ``UAKRON_SETTINGS_DIR`` environment variable overrides the pointer.

Every app shows this as a "Settings Folder" box in its settings.
"""
import json
import os
import shutil
import sys

POINTER = "settings_location.json"
ENV_VAR = "UAKRON_SETTINGS_DIR"


def app_folder(script_dir):
    """The app's own folder: next to the executable when frozen, else the script folder."""
    return os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else script_dir


def read_pointer(app_dir):
    try:
        with open(os.path.join(app_dir, POINTER), encoding="utf-8") as f:
            return (json.load(f).get("settings_dir") or "").strip()
    except (OSError, ValueError, AttributeError):
        return ""


def expand(value, data_dir):
    path = os.path.expanduser(value)
    if not os.path.isabs(path):
        path = os.path.join(data_dir, path)
    return os.path.normpath(path)


def resolve(app_dir, data_dir):
    """Folder this app should read and write its settings in (created if missing)."""
    value = os.environ.get(ENV_VAR) or read_pointer(app_dir)
    if not value:
        return app_dir
    path = expand(value, data_dir)
    os.makedirs(path, exist_ok=True)
    return path


def portable(path, data_dir):
    """Store a folder so the pointer works on Windows and macOS alike:
    relative inside the data folder, ``~/...`` under the home folder, else absolute."""
    path = os.path.normpath(os.path.abspath(path))
    data_dir = os.path.normpath(os.path.abspath(data_dir))
    home = os.path.normpath(os.path.expanduser("~"))
    norm = os.path.normcase
    if norm(path) == norm(data_dir) or norm(path).startswith(norm(data_dir) + os.sep):
        return os.path.relpath(path, data_dir).replace(os.sep, "/")
    if norm(path).startswith(norm(home) + os.sep):
        return "~/" + os.path.relpath(path, home).replace(os.sep, "/")
    return path


def change(app_dir, data_dir, new_path, files):
    """Point the app at ``new_path`` ('' = back to the app folder).

    Settings files missing in the new folder are copied from the current one;
    files already there are kept (so a second computer picks up the synced
    settings instead of overwriting them). Nothing is deleted.
    Returns (new_dir, copied, kept).
    """
    old_dir = resolve(app_dir, data_dir)
    value = portable(new_path, data_dir) if new_path else ""
    with open(os.path.join(app_dir, POINTER), "w", encoding="utf-8") as f:
        json.dump({"settings_dir": value}, f, indent=2)
    new_dir = expand(value, data_dir) if value else app_dir
    os.makedirs(new_dir, exist_ok=True)
    copied, kept = [], []
    for name in files:
        src, dst = os.path.join(old_dir, name), os.path.join(new_dir, name)
        if os.path.normcase(os.path.abspath(src)) == os.path.normcase(os.path.abspath(dst)):
            continue
        if os.path.exists(dst):
            kept.append(name)
        elif os.path.exists(src):
            shutil.copy2(src, dst)
            copied.append(name)
    return new_dir, copied, kept


def settings_folder_box(app_dir, data_dir, files, save_cb=None, restart_cb=None, parent=None):
    """PyQt6 group box: shows the settings folder and lets the user change it."""
    from PyQt6.QtCore import Qt, QUrl
    from PyQt6.QtGui import QDesktopServices
    from PyQt6.QtWidgets import (QFileDialog, QGroupBox, QHBoxLayout, QLabel,
                                 QMessageBox, QPushButton, QVBoxLayout)

    box = QGroupBox("Settings Folder", parent)
    lay = QVBoxLayout(box)
    info = QLabel("Where this app keeps its settings (" + ", ".join(files) + "). "
                  "Choose a folder inside SyncThing to share settings between computers. "
                  "Default: the app's own folder.")
    info.setWordWrap(True)
    lay.addWidget(info)
    current = QLabel()
    current.setWordWrap(True)
    current.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    lay.addWidget(current)
    row = QHBoxLayout()
    btn_change, btn_default, btn_open = QPushButton("Change..."), QPushButton("Use Default"), QPushButton("Open Folder")
    for b in (btn_change, btn_default, btn_open):
        row.addWidget(b)
    lay.addLayout(row)

    def refresh():
        cur = resolve(app_dir, data_dir)
        tag = "  (default: app folder)" if os.path.normcase(cur) == os.path.normcase(app_dir) else ""
        if os.environ.get(ENV_VAR):
            tag += f"  (set by {ENV_VAR})"
        current.setText("<b>Current:</b> " + cur + tag)

    def apply(new_path):
        if save_cb:
            try:
                save_cb()
            except Exception:
                pass
        try:
            new_dir, copied, kept = change(app_dir, data_dir, new_path, files)
        except OSError as e:
            QMessageBox.warning(box, "Settings Folder", f"Could not change the settings folder:\n{e}")
            return
        refresh()
        lines = [f"Settings folder is now:\n{new_dir}"]
        if copied:
            lines.append("Copied current settings there: " + ", ".join(copied))
        if kept:
            lines.append("Using the settings already in that folder: " + ", ".join(kept))
        lines.append("Restart now to load settings from the new folder?")
        ans = QMessageBox.question(box, "Settings Folder", "\n\n".join(lines))
        if ans == QMessageBox.StandardButton.Yes and restart_cb:
            restart_cb()

    def on_change():
        start = resolve(app_dir, data_dir)
        path = QFileDialog.getExistingDirectory(box, "Choose Settings Folder", start)
        if path:
            apply(path)

    btn_change.clicked.connect(on_change)
    btn_default.clicked.connect(lambda: apply(""))
    btn_open.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(resolve(app_dir, data_dir))))
    refresh()
    return box
