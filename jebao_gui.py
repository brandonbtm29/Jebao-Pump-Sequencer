"""Jebao Pump Sequencer: PyQt6 app that drives Jebao dosing pumps through the Gizwits cloud API."""

__version__ = "2.9.0"

import os
import sys

# --- Code vs data -----------------------------------------------------------
# Code lives in GitHub Projects/UAkron/<repo>; data (exports, backups, logs) lives
# in SyncThing. The app runs with the data folder as
# its working directory, so relative paths like "Data/" land there.
# Override with UAKRON_APP_DATA_DIR (this app) or UAKRON_DATA_DIR (data root).
script_dir = os.path.dirname(os.path.abspath(__file__))
sys.argv[0] = os.path.abspath(sys.argv[0])  # keeps "Restart" working after chdir
DATA_DIR = os.environ.get("UAKRON_APP_DATA_DIR") or os.path.join(
    os.environ.get("UAKRON_DATA_DIR")
    or os.path.join(os.path.expanduser("~"), "SyncThing", "UAkron Playground"),
    "Jebao Pump",
)
os.makedirs(DATA_DIR, exist_ok=True)
os.chdir(DATA_DIR)
# Settings (config, secrets, ROIs) live in the app folder by default, gitignored.
# The "Settings Folder" option (settings_location.py) can point them at a synced
# folder instead, e.g. "Settings" inside the data folder.
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)
import crash_log
import diagnostics
import discord_notify
import eventlog
import netwatch
import pumpguard
import rack
import sequence
import settings_location
import shortcut
import single_instance
import updater
from calibration_tab import CalibrationTab
from discord_dialog import DiscordDialog
from network_report import NetworkDialog
from rack_widgets import ManualTab, RackTab
from sequencer_tab import ReviewPopup, SequencerTab

APP_DIR = settings_location.app_folder(script_dir)
CONFIG_DIR = settings_location.resolve(APP_DIR, DATA_DIR)
SETTINGS_FILES = ["jebao_config.json", "secrets.json"]
APP_NAME = "Jebao Pump Sequencer"  # also names the single-instance lock
# Window size, position and dividers: per computer, in the app folder (gitignored), never synced
WINDOW_STATE_FILE = "window_state.json"
import asyncio
import collections
import contextlib
import html
import json
import logging
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

import qasync
import requests
from PyQt6.QtCore import QByteArray, QObject, Qt, QThread, QTimer, QUrl, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QColor, QDesktopServices, QIcon, QPalette
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

# Errors (with tracebacks, also from background threads) go to logs/jebao_pump_sequencer.log
# in the data folder; an uncaught error is shown in a dialog instead of closing the app.
# Installed at import, not in main(): without a custom excepthook PyQt6 aborts the whole
# process when a slot raises, which also takes down the test suite.
crash_log.setup("jebao_pump_sequencer", os.path.join(DATA_DIR, "logs"), __version__)

logger = logging.getLogger("JebaoGUI")
logger.setLevel(logging.DEBUG)


APPROVE_STYLE = (
    "QPushButton { background-color: #F08A24; color: #1D1205; font-weight: bold; padding: 6px 14px; }"
)
APPROVE_ALL_STYLE = "QPushButton { background-color: transparent; color: #FFE2C2; border: 1px solid #A8682A; padding: 6px 12px; }"


natural_key = rack.natural_key  # kept here for older imports


@dataclass
class HeadRun:
    """One pump head (pump id + channel) whose motor is running right now."""

    did: str
    ch: int
    duration: float
    source: str  # "manual", "sequence" or "calibration"
    remaining: float = 0.0
    paused: bool = False
    cancelled: bool = False
    resume: asyncio.Event = field(default_factory=asyncio.Event)
    cooling_until: float | None = None  # resting after its on-time limit (time.time())
    lane: int | None = None  # a sequence's lane (CH number) it runs in; ch is the pump's own channel

    @property
    def key(self):
        return (self.did, self.ch)

    @property
    def progress(self):
        if self.duration <= 0:
            return 1.0
        return max(0.0, min(1.0, 1.0 - self.remaining / self.duration))


class _LogBridge(QObject):
    """Carries log lines to the GUI thread. A Qt signal emitted from a background
    thread is delivered on the thread this object lives in (the GUI thread)."""

    line = pyqtSignal(str)

    def __init__(self, text_widget):
        super().__init__()
        self.text_widget = text_widget
        self.line.connect(self.append)

    @pyqtSlot(str)
    def append(self, msg):
        timestamp = datetime.now().strftime("%H:%M:%S")
        try:
            self.text_widget.append(f"[{timestamp}] {msg}")
        except (
            RuntimeError
        ):  # the window is already gone (closing the app); the line is still in the log file
            pass


class GUILogHandler(logging.Handler):
    """Show log records in the window's log box. Safe to log from any thread."""

    def __init__(self, text_widget):
        super().__init__()
        self.text_widget = text_widget
        self._bridge = _LogBridge(text_widget)

    def emit(self, record):
        try:
            msg = self.format(record)
            self._bridge.line.emit(msg)
        except Exception:
            self.handleError(record)

    def append_text(self, msg):
        self._bridge.append(msg)


# --- Cloud API Configuration ---
APP_ID = "c3703c4888ec4736a3a0d9425c321604"
DOMAIN = "usapi.gizwits.com"
# Seconds to wait for the cloud before giving up on a request, so a stalled
# connection can't hold up a dose or the emergency stop indefinitely.
REQUEST_TIMEOUT = 10
# Seconds a head waits in the red bar (OFF not confirmed) before Discord is told
GUARD_NOTIFY_S = 60


class SecretsManager:
    def __init__(self, filepath=os.path.join(CONFIG_DIR, "secrets.json")):  # noqa: B008
        self.filepath = filepath
        self.data = {"username": "", "password": ""}
        self.load()

    def load(self):
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath) as f:
                    self.data = json.load(f)
            except Exception as e:
                logger.error(f"Failed to load secrets: {e}")

    def save(self):
        try:
            with open(self.filepath, "w") as f:
                json.dump(self.data, f, indent=4)
        except Exception as e:
            logger.error(f"Failed to save secrets: {e}")


class ConfigManager:
    def __init__(self, filepath=os.path.join(CONFIG_DIR, "jebao_config.json")):  # noqa: B008
        self.filepath = filepath
        self.data = {"pumps": {}, "sequence": [], "saved_sequences": {}}
        self.load()

    def load(self):
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath) as f:
                    data = json.load(f)
                    if isinstance(data.get("pumps"), list):
                        # Migrate old array config to dict config or just wipe it
                        self.data = {"pumps": {}}
                    else:
                        self.data = data
            except Exception:
                self.data = {"pumps": {}, "sequence": [], "saved_sequences": {}}

            self.data.setdefault("sequence", [])
            self.data.setdefault("saved_sequences", {})

    def save(self):
        try:
            with open(self.filepath, "w") as f:
                json.dump(self.data, f, indent=4)
        except Exception as e:
            logger.error(f"Failed to save config: {e}")

    def head_name(self, did, ch):
        """The name given to a pump head in the Rack tab ("" if none)."""
        names = self.data.get("pumps", {}).get(did, {}).get("head_names") or []
        return str(names[ch - 1]).strip() if 0 < ch <= len(names) and names[ch - 1] else ""

    def set_head_name(self, did, ch, name):
        pump = self.get_pump(did)
        names = (list(pump.get("head_names") or []) + ["", "", "", ""])[:4]
        names[ch - 1] = (name or "").strip()
        pump["head_names"] = names

    def display_name(self, did):
        """The pump's name in this app, if one was set in the Rack tab ("" if not)."""
        return str(self.data.get("pumps", {}).get(did, {}).get("display_name") or "").strip()

    def get_pump(self, did):
        if did not in self.data["pumps"]:
            self.data["pumps"][did] = {
                "volumes": [10.0, 10.0, 10.0, 10.0],
                "units": ["Vol (mL)", "Vol (mL)", "Vol (mL)", "Vol (mL)"],
                "calibration": [1.0, 1.0, 1.0, 1.0],
                "tubing_id": [2.0, 2.0, 2.0, 2.0],
                "tubing_od": [4.0, 4.0, 4.0, 4.0],
            }

        # Retroactive migration for existing saved pumps
        if "tubing_id" not in self.data["pumps"][did]:
            self.data["pumps"][did]["tubing_id"] = [2.0, 2.0, 2.0, 2.0]
            self.data["pumps"][did]["tubing_od"] = [4.0, 4.0, 4.0, 4.0]
        if "units" not in self.data["pumps"][did]:
            self.data["pumps"][did]["units"] = ["Vol (mL)", "Vol (mL)", "Vol (mL)", "Vol (mL)"]

        return self.data["pumps"][did]


def apply_global_theme(app, config):
    theme = config.data.get("theme", "Dark")
    text_size = config.data.get("text_size", "Medium")

    font = app.font()
    if text_size == "Small":
        font.setPointSize(9)
    elif text_size == "Large":
        font.setPointSize(14)
    else:
        font.setPointSize(11)  # Medium
    app.setFont(font)

    app.setStyle("Fusion")
    if theme == "Dark":
        palette = QPalette()
        palette.setColor(QPalette.ColorRole.Window, QColor(53, 53, 53))
        palette.setColor(QPalette.ColorRole.WindowText, Qt.GlobalColor.white)
        palette.setColor(QPalette.ColorRole.Base, QColor(25, 25, 25))
        palette.setColor(QPalette.ColorRole.AlternateBase, QColor(53, 53, 53))
        palette.setColor(QPalette.ColorRole.ToolTipBase, Qt.GlobalColor.white)
        palette.setColor(QPalette.ColorRole.ToolTipText, Qt.GlobalColor.white)
        palette.setColor(QPalette.ColorRole.Text, Qt.GlobalColor.white)
        palette.setColor(QPalette.ColorRole.Button, QColor(53, 53, 53))
        palette.setColor(QPalette.ColorRole.ButtonText, Qt.GlobalColor.white)
        palette.setColor(QPalette.ColorRole.BrightText, Qt.GlobalColor.red)
        palette.setColor(QPalette.ColorRole.Link, QColor(42, 130, 218))
        palette.setColor(QPalette.ColorRole.Highlight, QColor(42, 130, 218))
        palette.setColor(QPalette.ColorRole.HighlightedText, Qt.GlobalColor.black)
        app.setPalette(palette)
    else:
        app.setPalette(app.style().standardPalette())


class _Task(QThread):
    """Run one slow function (network, git, pip) off the GUI thread."""

    done = pyqtSignal(object)
    progress = pyqtSignal(str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn

    def run(self):
        try:
            self.done.emit(self._fn(self.progress.emit))
        except LookupError as e:  # an expected problem with a plain message (offline, no releases)
            logger.info(f"Update check: {e}")
            self.done.emit(e)
        except Exception as e:  # handed back to the GUI thread and shown there
            logger.error(f"Background task failed: {e}")
            self.done.emit(e)


# --- Copy Diagnostics ------------------------------------------------------------------------

_MAC_ADDRESS = re.compile(r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b|\b[0-9A-Fa-f]{12}\b")
_PUMP_URL = re.compile(r"(/app/(?:control|devdata)/)[^/\s'\")]+")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def scrub_private(text, private=()):
    """Blank what diagnostics.redact() can't know about: the Jebao login, the cloud token, pump
    IDs (also inside cloud URLs), MAC addresses and e-mail addresses."""
    for value in sorted({str(v) for v in private if v and len(str(v)) >= 4}, key=len, reverse=True):
        text = text.replace(value, "***")
    text = _PUMP_URL.sub(r"\1***", text)
    text = _MAC_ADDRESS.sub("***", text)
    return _EMAIL.sub("***", text)


def diagnostics_text(window=None):
    """The text Settings → App → Copy Diagnostics puts on the clipboard."""
    extra = window.diagnostics_extra() if hasattr(window, "diagnostics_extra") else {}
    private = window.private_values() if hasattr(window, "private_values") else []
    text = diagnostics.report(
        APP_NAME,
        __version__,
        APP_DIR,
        settings_dir=CONFIG_DIR,
        data_dir=DATA_DIR,
        log_path=crash_log.log_path(),
        extra=extra,
    )
    return scrub_private(text, private)


def check_updates_on_start(config):
    """The "Check for updates when the app starts" setting in jebao_config.json (on by default)."""
    return True if config is None else bool(config.data.get("check_updates_on_start", True))


# --- window size and position, kept per computer ---------------------------------------------


def load_window_state(app_dir):
    """The saved window layout for this computer ({} if none or unreadable)."""
    try:
        with open(os.path.join(app_dir, WINDOW_STATE_FILE), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_window_state(app_dir, state):
    """Write the window layout safely: a crash mid-write leaves the old file, not half a file."""
    path = os.path.join(app_dir, WINDOW_STATE_FILE)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, path)


def _sizes(value, count):
    """Splitter sizes from the state file, or None if they don't look right."""
    if isinstance(value, list) and len(value) == count and all(isinstance(v, int) and v >= 0 for v in value):
        return value if sum(value) > 0 else None
    return None


class AppBox(QGroupBox):
    """The "App" box in Settings: version, Check for Updates, diagnostics, and Create Desktop Shortcut."""

    def __init__(self, main_window=None, parent=None):
        super().__init__("App", parent)
        self.main_window = main_window
        self._task = None
        self._progress = None
        self._progress_timer = None
        self._cancel = None
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(f"You are running Jebao Pump Sequencer <b>{__version__}</b>."))
        self.chk_check_on_start = QCheckBox("Check for updates when the app starts")
        self.chk_check_on_start.setToolTip(
            "Asks GitHub for the latest release when the app opens. If there is a newer one, the "
            "version in the bottom-right corner turns orange. Nothing pops up."
        )
        self.chk_check_on_start.setChecked(check_updates_on_start(getattr(main_window, "config", None)))
        self.chk_check_on_start.toggled.connect(self._check_on_start_toggled)
        lay.addWidget(self.chk_check_on_start)
        self.lbl_status = QLabel(
            "Check for Updates asks GitHub for the latest release. Nothing about you or your pumps is sent."
        )
        self.lbl_status.setWordWrap(True)
        self.lbl_status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.lbl_status)
        row = QHBoxLayout()
        self.btn_check = QPushButton("Check for Updates")
        self.btn_check.clicked.connect(self.check)
        btn_page = QPushButton("Open Release Page")
        btn_page.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(updater.RELEASES_PAGE)))
        row.addWidget(self.btn_check)
        row.addWidget(btn_page)
        btn_restart = QPushButton("Restart App")
        btn_restart.setToolTip("Close and reopen the app (for troubleshooting).")
        btn_restart.clicked.connect(self._restart)
        row.addWidget(btn_restart)
        lay.addLayout(row)

        # For bug reports
        diag_row = QHBoxLayout()
        self.btn_log_folder = QPushButton("Open Log Folder")
        self.btn_log_folder.setToolTip("Show the folder with the error log and the system log.")
        self.btn_log_folder.clicked.connect(self.open_log_folder)
        diag_row.addWidget(self.btn_log_folder)
        self.btn_diagnostics = QPushButton("Copy Diagnostics")
        self.btn_diagnostics.setToolTip(
            "Copy the version, this computer's setup and the end of the error log, for a bug report."
        )
        self.btn_diagnostics.clicked.connect(self.copy_diagnostics)
        diag_row.addWidget(self.btn_diagnostics)
        diag_row.addStretch(1)
        lay.addLayout(diag_row)
        self.lbl_diagnostics = QLabel(
            "Copy Diagnostics copies details for a bug report. Your login, pump IDs and MAC addresses are left out."
        )
        self.lbl_diagnostics.setWordWrap(True)
        lay.addWidget(self.lbl_diagnostics)

        where = {
            "win32": "a shortcut on your desktop that opens the app without a console window",
            "darwin": "a Jebao Pump Sequencer app on your desktop (drag it to the Dock or Applications if you like)",
        }.get(sys.platform, "a launcher on your desktop")
        info = QLabel(
            f"Create Desktop Shortcut makes {where}. It starts this copy of the app, so it keeps "
            "working after updates. Pressing it again replaces the old shortcut."
        )
        info.setWordWrap(True)
        lay.addWidget(info)
        self.btn_shortcut = QPushButton("Create Desktop Shortcut")
        self.btn_shortcut.clicked.connect(self.create_shortcut)
        lay.addWidget(self.btn_shortcut)
        self.chk_auto_shortcut = QCheckBox("Keep the desktop shortcut up to date automatically")
        self.chk_auto_shortcut.setToolTip(
            "Creates the shortcut the first time the app starts, and refreshes it (icon and target) after each update."
        )
        self.chk_auto_shortcut.setChecked(shortcut.load_state(APP_DIR).get("auto", True))
        self.chk_auto_shortcut.toggled.connect(self._auto_shortcut_toggled)
        lay.addWidget(self.chk_auto_shortcut)

    def _check_on_start_toggled(self, on):
        config = getattr(self.main_window, "config", None)
        if config is not None:
            config.data["check_updates_on_start"] = bool(on)
            config.save()

    # --- for bug reports -------------------------------------------------------------------

    def open_log_folder(self):
        try:
            diagnostics.open_folder(diagnostics.log_folder(crash_log.log_path()))
        except OSError as e:
            QMessageBox.warning(self, "Open Log Folder", f"Couldn't open the log folder.\n\n{e}")

    def copy_diagnostics(self):
        QApplication.clipboard().setText(diagnostics_text(self.main_window))
        self.lbl_diagnostics.setText("Diagnostics copied. Paste them into a GitHub issue or an email.")

    def _auto_shortcut_toggled(self, on):
        state = shortcut.load_state(APP_DIR)
        state["auto"] = bool(on)
        try:
            shortcut.save_state(APP_DIR, state)
        except OSError as e:
            logger.warning(f"Couldn't save the desktop shortcut setting: {e}")

    def _restart(self):
        busy = self._busy_reason()
        if busy:
            QMessageBox.warning(self, "Restart", busy.replace("updating", "restarting"))
            return
        restart = getattr(self.main_window, "restart_app", None)
        if restart:
            restart()

    # --- desktop shortcut ----------------------------------------------------------------

    def create_shortcut(self):
        try:
            path = shortcut.create_shortcut(version=__version__)
        except shortcut.ShortcutError as e:
            logger.warning(f"Desktop shortcut: {e}")
            QMessageBox.warning(self, "Desktop Shortcut", str(e))
            return
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            logger.error(f"Couldn't create the desktop shortcut: {e}")
            QMessageBox.warning(self, "Desktop Shortcut", f"Couldn't create the shortcut:\n{e}")
            return
        logger.info(f"Created desktop shortcut {path}")
        state = shortcut.load_state(APP_DIR)
        state.update({"version": __version__, "path": path})
        try:
            shortcut.save_state(APP_DIR, state)
        except OSError as e:
            logger.warning(f"Couldn't save the desktop shortcut setting: {e}")
        QMessageBox.information(self, "Desktop Shortcut", f"Created:\n{path}")

    # --- checking for updates ------------------------------------------------------------

    def _busy_reason(self):
        busy = getattr(self.main_window, "busy_reason", None)
        return busy() if busy else ""

    def check(self):
        self.btn_check.setEnabled(False)
        self.lbl_status.setText("Checking GitHub…")
        self._start(lambda _say: updater.latest_release(__version__), self._checked)

    def _checked(self, result):
        self.btn_check.setEnabled(True)
        show = getattr(self.main_window, "show_update_state", None)
        if show:
            show(result)
        if isinstance(result, Exception):
            msg = str(result) if isinstance(result, LookupError) else f"The check failed: {result}"
            self.lbl_status.setText(msg)
            return
        release = result
        if not updater.is_newer(release.version, __version__):
            self.lbl_status.setText(f"You have the latest version ({__version__}).")
            return
        self.lbl_status.setText(f"Version {release.version} is available.")
        self._offer(release)

    def _offer(self, release):
        ready = updater.update_readiness()
        busy = self._busy_reason()
        notes = release.notes.strip()
        if len(notes) > 1500:
            notes = notes[:1500].rsplit("\n", 1)[0] + "\n…"
        box = QMessageBox(self)
        box.setWindowTitle("Update available")
        box.setText(f"<b>{release.name}</b> is available. You have {__version__}.")
        box.setDetailedText(notes or "No release notes.")
        btn_page = box.addButton("Open Release Page", QMessageBox.ButtonRole.HelpRole)
        btn_update = None
        if not ready.ok:
            box.setInformativeText(ready.reason)
        elif busy:
            box.setInformativeText(busy)
        else:
            box.setInformativeText(
                "Update now? The app saves your settings and sequence, downloads the new version "
                "and restarts. You'll need to log in again only if your login isn't saved."
            )
            btn_update = box.addButton("Update and Restart", QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        clicked = box.clickedButton()
        if clicked is btn_page:
            QDesktopServices.openUrl(QUrl(release.url))
        elif btn_update is not None and clicked is btn_update:
            self._update(release)

    # --- updating ------------------------------------------------------------------------

    def _update(self, release):
        busy = self._busy_reason()  # something may have started while the box was open
        if busy:
            QMessageBox.warning(self, "Update", busy)
            return
        save = getattr(self.main_window, "save_before_restart", None)
        if save:
            save()
        self.btn_check.setEnabled(False)
        self._cancel = threading.Event()
        self._progress_text = "Starting…"
        self._progress_started = time.monotonic()
        self._progress = QProgressDialog("Starting…", "Cancel", 0, 0, self)
        self._progress.setWindowTitle(f"Updating to {release.version}")
        self._progress.setWindowModality(Qt.WindowModality.ApplicationModal)
        self._progress.setMinimumDuration(0)
        self._progress.setMinimumWidth(460)
        self._progress.setAutoClose(False)
        self._progress.setAutoReset(False)
        self._progress.canceled.connect(self._cancel_update)
        self._progress.show()
        self._progress_timer = QTimer(self)
        self._progress_timer.timeout.connect(self._show_progress)
        self._progress_timer.start(1000)
        cancel = self._cancel
        self._start(lambda say: updater.update_to(release.tag, progress=say, cancel=cancel), self._updated)

    def _set_progress(self, text):
        self._progress_text = text
        self._show_progress()

    def _show_progress(self):
        if self._progress is None:
            return
        secs = int(time.monotonic() - self._progress_started)
        self._progress.setLabelText(f"{self._progress_text}\n\n{secs // 60}:{secs % 60:02d} so far")

    def _cancel_update(self):
        if self._cancel is not None:
            self._cancel.set()
        if self._progress is not None:
            self._progress.show()  # stays up until the update has actually stopped
            self._progress.setCancelButtonText("Cancelling…")
            self._progress_text = "Cancelling: putting everything back as it was…"
            self._show_progress()

    def _updated(self, result):
        if getattr(self, "_progress_timer", None) is not None:
            self._progress_timer.stop()
            self._progress_timer = None
        if self._progress is not None:
            self._progress.canceled.disconnect(self._cancel_update)
            self._progress.close()
            self._progress = None
        self.btn_check.setEnabled(True)
        if isinstance(result, Exception):
            result = updater.UpdateResult(False, f"The update failed: {result}")
        if not result.ok:
            self.lbl_status.setText("The update didn't finish. See the message for details.")
            QMessageBox.warning(self, "Update", result.message)
            return
        self.lbl_status.setText(result.message + " Restarting…")
        QMessageBox.information(self, "Update", result.message + "\n\nThe app will now restart.")
        restart = getattr(self.main_window, "restart_app", None)
        if restart:
            restart()

    def _start(self, fn, on_done):
        self._task = _Task(fn, self)
        self._task.done.connect(on_done)
        if self._progress is not None:
            self._task.progress.connect(self._set_progress)
        self._task.start()


class SettingsDialog(QDialog):
    def __init__(self, secrets_manager, config_manager, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumSize(560, 300)
        self.secrets = secrets_manager
        self.config = config_manager

        self.original_theme = self.config.data.get("theme", "Dark")
        self.original_size = self.config.data.get("text_size", "Medium")

        layout = QFormLayout(self)

        layout.addRow(QLabel("<b>Cloud API</b>"))
        self.user_input = QLineEdit(self.secrets.data.get("username", ""))
        self.pass_input = QLineEdit(self.secrets.data.get("password", ""))
        self.pass_input.setEchoMode(QLineEdit.EchoMode.Password)

        layout.addRow("Username (Email/Phone):", self.user_input)
        layout.addRow("Password:", self.pass_input)

        layout.addRow(QLabel("<b>Global UI Options</b>"))
        self.theme_combo = QComboBox()
        self.theme_combo.addItems(["Dark", "Light"])
        self.theme_combo.setCurrentText(self.original_theme)
        self.theme_combo.currentTextChanged.connect(self.preview_changes)
        layout.addRow("Interface Theme:", self.theme_combo)

        self.size_combo = QComboBox()
        self.size_combo.addItems(["Small", "Medium", "Large"])
        self.size_combo.setCurrentText(self.original_size)
        self.size_combo.currentTextChanged.connect(self.preview_changes)
        layout.addRow("Text Size:", self.size_combo)

        self.log_days_spin = QSpinBox()
        self.log_days_spin.setRange(1, 3650)
        self.log_days_spin.setSuffix(" days")
        self.log_days_spin.setValue(int(self.config.data.get("log_keep_days", eventlog.DEFAULT_KEEP_DAYS)))
        self.log_days_spin.setToolTip(
            "How long the system log is kept on this computer. Older days are deleted when the app starts."
        )
        layout.addRow("Keep System Log For:", self.log_days_spin)
        btn_network = QPushButton("Pump Connections…")
        btn_network.setToolTip("When each pump dropped off Wi-Fi and came back, and router tips.")
        btn_network.clicked.connect(lambda: getattr(parent, "open_network_report", lambda: None)())
        layout.addRow("Wi-Fi:", btn_network)
        btn_discord = QPushButton("Notifications…")
        btn_discord.setToolTip("Post to a Discord channel when a channel waits for you or a run ends.")
        btn_discord.clicked.connect(
            lambda: getattr(parent, "open_discord_settings", lambda _d=None: None)(self)
        )
        layout.addRow("Discord:", btn_discord)

        layout.addRow(QLabel("<b>Pump safety</b>"))
        guard = self.config.data.get("guard", {})
        self.chk_limit = QCheckBox("Limit how long each head can run")
        self.chk_limit.setChecked(bool(guard.get("enabled", True)))
        self.chk_limit.setToolTip(
            "A dose that reaches the limit turns the head off to rest, then carries on when it "
            "may (the full volume still goes in, just later). Each head can have its own limit "
            "or none, in the Rack tab; a head's own limit applies even when this is off, over "
            "the window set here."
        )
        self.spin_limit_on = QSpinBox()
        self.spin_limit_on.setRange(1, 600)
        self.spin_limit_on.setSuffix(" min")
        self.spin_limit_on.setValue(int(guard.get("on_min", 30)))
        self.spin_limit_window = QSpinBox()
        self.spin_limit_window.setRange(5, 1440)
        self.spin_limit_window.setSuffix(" min")
        self.spin_limit_window.setValue(int(guard.get("window_min", 60)))
        limit_row = QHBoxLayout()
        limit_row.addWidget(QLabel("At most"))
        limit_row.addWidget(self.spin_limit_on)
        limit_row.addWidget(QLabel("in any"))
        limit_row.addWidget(self.spin_limit_window)
        limit_row.addStretch(1)
        layout.addRow(self.chk_limit)
        layout.addRow("On-time limit:", limit_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.app_box = AppBox(parent, self)
        layout.addRow(self.app_box)
        layout.addRow(
            settings_location.settings_folder_box(
                APP_DIR,
                DATA_DIR,
                SETTINGS_FILES,
                save_cb=lambda: (config_manager.save(), secrets_manager.save()),
                restart_cb=getattr(parent, "restart_app", None),
            )
        )
        layout.addRow(buttons)
        # Tall enough for the wrapped explanations, instead of squashing them.
        width = 560
        self.resize(width, max(self.sizeHint().height(), layout.totalHeightForWidth(width)))

    def accept(self):
        self.secrets.data["username"] = self.user_input.text().strip()
        self.secrets.data["password"] = self.pass_input.text()
        self.secrets.save()

        self.config.data["theme"] = self.theme_combo.currentText()
        self.config.data["text_size"] = self.size_combo.currentText()
        self.config.data["log_keep_days"] = self.log_days_spin.value()
        self.config.data["guard"] = {
            "enabled": self.chk_limit.isChecked(),
            "on_min": min(self.spin_limit_on.value(), self.spin_limit_window.value()),
            "window_min": self.spin_limit_window.value(),
        }
        self.config.save()
        eventlog.prune(eventlog.log_dir(DATA_DIR), self.log_days_spin.value())

        apply_global_theme(QApplication.instance(), self.config)
        super().accept()

    def preview_changes(self):
        self.config.data["theme"] = self.theme_combo.currentText()
        self.config.data["text_size"] = self.size_combo.currentText()
        apply_global_theme(QApplication.instance(), self.config)

    def reject(self):
        self.config.data["theme"] = self.original_theme
        self.config.data["text_size"] = self.original_size
        apply_global_theme(QApplication.instance(), self.config)
        super().reject()


class JebaoPumpGUI(QMainWindow):
    def __init__(self, history=None):
        self._history = history or []
        super().__init__()
        self.setWindowTitle("Jebao Multi-Pump SPE Sequencer (Cloud Edition)")
        self.setMinimumSize(1100, 800)
        # Size and position from last time on this computer (Qt keeps it on a screen that is
        # still there); the first time, full screen as before.
        self._window_state = load_window_state(APP_DIR)
        geometry = self._window_state.get("geometry")
        restored = False
        if isinstance(geometry, str):
            try:
                restored = self.restoreGeometry(QByteArray.fromHex(geometry.encode("ascii")))
            except (UnicodeEncodeError, ValueError):
                restored = False
        if not restored:
            self.showMaximized()

        self.config = ConfigManager()
        self.secrets = SecretsManager()

        self.cloud_token = None
        self.cloud_devices = []

        self.sequence_queue = []  # [{"name", "data": steps}], run one after another
        self.current_running_sequence = None
        self.sequence_task = None
        self.queue_paused = False  # the next queued run waits until this is False
        self.seq_run = None  # sequence.RunState of the running (or last) run
        self._stop_reason = ""
        self._run_problem = None
        self._lane_tasks = {}
        self._ch_running_tasks = {1: [], 2: [], 3: [], 4: []}  # background steps per lane
        self._ch_step_tasks = {}  # lane -> the step it is on (Skip cancels it)
        self._ch_current = {}  # lane -> sequence.Flat it is on
        self._skip = {}
        self._sync_points = {}
        self._ch_review_titles = {}  # lane -> what the waiting step is, for the banner
        self._ch_review_messages = {}  # lane -> the waiting step's message (what to do there)
        self._review_at = {}  # lane -> path of the Wait for me step it waits at
        self._review_popups = {}  # step path -> ReviewPopup showing that step's message
        self._run_one = False  # start the next queued run even though the queue is paused
        self._told_calibration = False
        self._motor_jobs = 0  # manual doses and calibration runs in progress
        self.head_runs = {}  # (did, ch) -> HeadRun for every head whose motor is running
        self._dose_tasks = set()  # manual doses in progress (Emergency Stop cancels them)
        self.data_dir = DATA_DIR
        self.run_history_path = sequence.history_path(eventlog.log_dir(DATA_DIR), eventlog.computer_name())
        # When each pump drops off Wi-Fi and comes back (see netwatch.py)
        self.netlog = netwatch.ConnectionLog(
            path=netwatch.history_path(eventlog.log_dir(DATA_DIR), eventlog.computer_name()),
            keep_days=int(self.config.data.get("log_keep_days", eventlog.DEFAULT_KEEP_DAYS)),
        ).load()
        self._watching = False
        self._relogging = False
        self.guard = pumpguard.PumpGuard(
            send=lambda did, attrs: self._post_control(did, attrs),
            read=lambda did: self._read_pump(did),
            label=lambda did, ch: self.head_label(did, ch),
            limit_for=lambda key: self.head_limit(key),
        )
        self.guard.on_stopped = self._guard_stopped
        self.guard.on_over_limit = self._guard_over_limit

        # Per sequence lane, keyed by CH number (CH1–4 in Stack A, CH5–8 in Stack B, …)
        self._ch_paused = {}
        self._ch_cancelled = {}
        self._ch_resume_events = collections.defaultdict(asyncio.Event)
        self._ch_review_events = {}
        self._groups_key = None
        self._groups = {}
        self._run_lane_of = {}  # (did, ch) -> lane, fixed when a run starts

        # Discord notifications (discord_notify.py): sent from their own thread
        self.discord = discord_notify.Notifier()
        self._guard_seen = {}  # head -> when it first showed in the red bar
        self._guard_told = set()  # heads Discord has been told about
        self._offline_told = set()  # pumps Discord was told went offline while in use

        self.setup_ui()
        self._build_status_bar()
        self._restore_splitters()

        logger.info("Jebao SPE GUI Initialized (Cloud Mode).")

        if self.secrets.data.get("username") and self.secrets.data.get("password"):
            QTimer.singleShot(100, lambda: self._start_task(self.login_and_fetch))

        self._queue_task = None
        QTimer.singleShot(200, self._start_queue)

    def _start_queue(self):
        self._queue_task = self._start_task(self._queue_worker)

    def _start_task(self, coro_fn):
        """Start an async job on the app's event loop (skipped when no loop is running, as in tests)."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return None
        return asyncio.create_task(coro_fn())

    def setup_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)

        # The clock lives in the status bar (see _build_status_bar).
        self.lbl_clock = QLabel()

        # --- Top bar: connection, Refresh, Settings ---
        top = QHBoxLayout()
        title = QLabel("Jebao Pump Sequencer")
        title.setStyleSheet("font-size: 15px; font-weight: 600;")
        top.addWidget(title)
        top.addStretch(1)
        self.status_indicator = QLabel()
        self.set_status_color(self.status_indicator, False)
        top.addWidget(self.status_indicator)
        self.btn_refresh = QPushButton("Refresh")
        self.btn_refresh.setToolTip("Log in again and reload the pumps on your account.")
        self.btn_refresh.clicked.connect(lambda: asyncio.create_task(self.login_and_fetch()))
        top.addWidget(self.btn_refresh)
        self.btn_settings = QPushButton("⚙ Settings")
        self.btn_settings.clicked.connect(self.open_settings)
        top.addWidget(self.btn_settings)
        main_layout.addLayout(top)

        # --- Approval banner: shown only while a sequence step waits for review ---
        self.approval_banner = QFrame()
        self.approval_banner.setStyleSheet(
            "QFrame { background-color: #4A2F12; border: 1px solid #8A5420; border-radius: 6px; }"
            "QLabel { color: #FFE2C2; border: none; }"
        )
        banner_layout = QHBoxLayout(self.approval_banner)
        self._banner_layout = banner_layout
        banner_layout.setContentsMargins(12, 8, 12, 8)
        self.lbl_approval = QLabel()
        self.lbl_approval.setWordWrap(True)
        banner_layout.addWidget(self.lbl_approval, 1)
        self.btn_approve_channels = []  # index = CH number − 1; more are added for more stacks
        self.btn_approve_all = QPushButton("Continue all")
        self.btn_approve_all.setEnabled(False)
        self.btn_approve_all.setStyleSheet(APPROVE_ALL_STYLE)
        self.btn_approve_all.clicked.connect(self.approve_all)
        banner_layout.addWidget(self.btn_approve_all)
        self._approve_buttons(4)
        self.approval_banner.setVisible(False)
        main_layout.addWidget(self.approval_banner)

        # Red bar while the pump guard is still trying to turn a head off
        self.guard_bar = QFrame()
        self.guard_bar.setStyleSheet("QFrame { background-color: #7A1F1F; border-radius: 6px; }")
        bar = QHBoxLayout(self.guard_bar)
        bar.setContentsMargins(10, 6, 10, 6)
        self.guard_banner = QLabel()
        self.guard_banner.setWordWrap(True)
        self.guard_banner.setStyleSheet("color: white; font-weight: bold; background: transparent;")
        bar.addWidget(self.guard_banner, 1)
        self.btn_guard_forget = QPushButton("I've switched it off")
        self.btn_guard_forget.setToolTip(
            "Stop checking these heads: only once you've switched the pump off or unplugged it yourself."
        )
        self.btn_guard_forget.clicked.connect(self.forget_guard_heads)
        bar.addWidget(self.btn_guard_forget)
        self.guard_bar.setVisible(False)
        main_layout.addWidget(self.guard_bar)

        # --- Tabs: Manual | Sequencer | Calibration | Rack ---
        self.tabs = QTabWidget()
        self.manual_tab = ManualTab(self)
        self.tabs.addTab(self.manual_tab, "Manual")
        self.sequencer_tab = SequencerTab(self)
        self.seq_splitter = self.sequencer_tab.splitter  # its sidebar divider is remembered
        self.tabs.addTab(self.sequencer_tab, "Sequencer")
        self.calibration_tab = CalibrationTab(self)
        self.tabs.addTab(self.calibration_tab, "Calibration")
        self.rack_tab = RackTab(self)
        self.tabs.addTab(self.rack_tab, "Rack")

        # 4. Bottom Right: System Log (kept on disk, see eventlog.py)
        log_group = QGroupBox("System Log")
        self.log_group = log_group
        log_layout = QVBoxLayout()
        log_header = QHBoxLayout()
        keep = int(self.config.data.get("log_keep_days", eventlog.DEFAULT_KEEP_DAYS))
        self.lbl_log_info = QLabel(f"Saved on this computer for {keep} days (change in Settings).")
        self.lbl_log_info.setStyleSheet("color: grey;")
        # Ignored width: a long latest line (shown while the log is hidden) is cut off, not widening the window
        self.lbl_log_info.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        log_header.addWidget(self.lbl_log_info, 1)
        btn_log_folder = QPushButton("Open Log Folder")
        btn_log_folder.clicked.connect(self.open_log_folder)
        log_header.addWidget(btn_log_folder)
        self.btn_log_toggle = QPushButton("▾ Hide")
        self.btn_log_toggle.setToolTip("Shrink the log to one line to give the tabs more room.")
        self.btn_log_toggle.clicked.connect(lambda: self.set_log_collapsed(not self.log_collapsed))
        log_header.addWidget(self.btn_log_toggle)
        log_layout.addLayout(log_header)
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setStyleSheet(
            "background-color: #1E1E1E; color: #D4D4D4; font-family: Consolas, Menlo, monospace;"
        )
        for line in self._history:
            self.log_text.append(line)
        if self._history:
            self.log_text.append(f"── App started {datetime.now():%H:%M:%S} ──")
        log_layout.addWidget(self.log_text)
        log_group.setLayout(log_layout)

        # The log sits under every tab; drag the divider to give it more or less room.
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self.tabs)
        splitter.addWidget(log_group)
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([640, 170])
        main_layout.addWidget(splitter, 1)
        self.main_splitter = splitter
        self.log_collapsed = False
        self._log_sizes = None
        self.log_text.textChanged.connect(self._show_last_log_line)
        if self.config.data.get("ui", {}).get("log_collapsed"):
            self.set_log_collapsed(True)

        # Emergency Stop spanning all
        self.btn_estop = QPushButton("🚨 EMERGENCY STOP ALL PUMPS 🚨")
        self.btn_estop.setStyleSheet(
            "background-color: darkred; color: white; font-weight: bold; font-size: 16px; padding: 15px;"
        )
        self.btn_estop.clicked.connect(self.emergency_stop_all)
        main_layout.addWidget(self.btn_estop)

        gui_handler = GUILogHandler(self.log_text)
        gui_handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(gui_handler)

        # Tiles and the dispense panel follow the motor runs a few times a second
        self.ui_timer = QTimer(self)
        self.ui_timer.timeout.connect(self._refresh_visible_tab)
        self.ui_timer.start(300)

        # Start Clock Timer
        self.clock_timer = QTimer(self)
        self.clock_timer.timeout.connect(self.update_clock)
        self.clock_timer.start(1000)
        self.update_clock()

        # The red bar (and Discord) also follow the guard between pump checks
        self.guard_timer = QTimer(self)
        self.guard_timer.timeout.connect(self.refresh_guard_banner)
        self.guard_timer.start(5000)

    def set_log_collapsed(self, collapsed):
        """Shrink the System Log to its header (showing the latest line), or open it again."""
        if collapsed == self.log_collapsed:
            return
        self.log_collapsed = collapsed
        split = self.main_splitter
        if collapsed:
            self._log_sizes = split.sizes()
        self.log_text.setVisible(not collapsed)
        self.btn_log_toggle.setText("▴ Show" if collapsed else "▾ Hide")
        self.btn_log_toggle.setToolTip(
            "Open the log again." if collapsed else "Shrink the log to one line to give the tabs more room."
        )
        if collapsed:
            self.log_group.setMaximumHeight(self.log_group.sizeHint().height())
            total = sum(split.sizes())
            split.setSizes([total - self.log_group.maximumHeight(), self.log_group.maximumHeight()])
        else:
            self.log_group.setMaximumHeight(16777215)  # QWIDGETSIZE_MAX
            if self._log_sizes and self._log_sizes[1] > 60:
                split.setSizes(self._log_sizes)
            else:
                total = sum(split.sizes())
                split.setSizes([total - 170, 170])
        self._show_last_log_line()
        self.config.data.setdefault("ui", {})["log_collapsed"] = collapsed
        self.config.save()

    def _show_last_log_line(self):
        if not self.log_collapsed:
            keep = int(self.config.data.get("log_keep_days", eventlog.DEFAULT_KEEP_DAYS))
            self.lbl_log_info.setText(f"Saved on this computer for {keep} days (change in Settings).")
            self.lbl_log_info.setToolTip("")
            return
        last = self.log_text.document().lastBlock().text().strip()
        self.lbl_log_info.setText(last or "No messages yet.")
        self.lbl_log_info.setToolTip(last)

    def open_log_folder(self):
        folder = eventlog.log_dir(DATA_DIR)
        os.makedirs(folder, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(folder))

    # --- window layout, remembered per computer (window_state.json in the app folder) ---------

    def _restore_splitters(self):
        """Put the dividers (tabs | System Log, and the Sequencer's sidebar) back where they were."""
        log_split = _sizes(self._window_state.get("log_splitter"), 2)
        if log_split:
            if self.log_collapsed:
                self._log_sizes = log_split  # used when the log is shown again
            else:
                self.main_splitter.setSizes(log_split)
        seq_split = _sizes(self._window_state.get("sequencer_splitter"), 2)
        if seq_split:
            self.seq_splitter.setSizes(seq_split)  # applied when the Sequencer tab is first shown
        # Until the Sequencer tab has been opened its divider reports made-up sizes, so only a
        # divider moved this time is saved; otherwise the one from last time is kept.
        self._seq_split_moved = False
        self.seq_splitter.splitterMoved.connect(self._seq_splitter_moved)

    def _seq_splitter_moved(self, _pos, _index):
        self._seq_split_moved = True

    def remember_window_state(self):
        """Save the window's size, position and dividers for next time, on this computer only."""
        state = {"geometry": self.saveGeometry().toHex().data().decode("ascii")}
        log_split = _sizes(self._log_sizes if self.log_collapsed else self.main_splitter.sizes(), 2)
        if log_split:
            state["log_splitter"] = list(log_split)
        if self._seq_split_moved:
            seq_split = _sizes(self.seq_splitter.sizes(), 2)
        else:
            seq_split = _sizes(self._window_state.get("sequencer_splitter"), 2)
        if seq_split:
            state["sequencer_splitter"] = seq_split
        try:
            save_window_state(APP_DIR, state)
        except OSError as e:
            logger.warning(f"Couldn't save the window size and position: {e}")

    def update_clock(self):
        now = datetime.now().strftime("%A, %B %d, %Y | %I:%M:%S %p")
        self.lbl_clock.setText(now)

    def open_settings(self, then=None):
        dlg = SettingsDialog(self.secrets, self.config, self)
        if then:
            QTimer.singleShot(0, lambda: then(dlg))
        if dlg.exec():
            asyncio.create_task(self.login_and_fetch())

    # --- version label and update check ------------------------------------------------------

    def _build_status_bar(self):
        self._available_release = None
        self._background_tasks = []
        self.version_button = QPushButton(f"v{__version__}")
        self.version_button.setFlat(True)
        self.version_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.version_button.setToolTip("Jebao Pump Sequencer version. Click to check for updates.")
        self.version_button.clicked.connect(self.on_version_clicked)
        self.lbl_clock.setStyleSheet("color: grey; padding-left: 6px;")
        self.statusBar().addWidget(self.lbl_clock)
        self.pumps_button = QPushButton("Pumps: —")
        self.pumps_button.setFlat(True)
        self.pumps_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.pumps_button.setToolTip("How many pumps are on Wi-Fi. Click for the connection report.")
        self.pumps_button.clicked.connect(self.open_network_report)
        self.statusBar().addPermanentWidget(self.pumps_button)
        self.statusBar().addPermanentWidget(self.version_button)

    def show_update_state(self, result):
        """Show in the bottom-right label whether a newer release is out."""
        if isinstance(result, updater.Release) and updater.is_newer(result.version, __version__):
            self._available_release = result
            self.version_button.setText(f"v{__version__}  ·  Update available: v{result.version}")
            self.version_button.setStyleSheet("color: #E67E22; font-weight: bold;")
            self.version_button.setToolTip("A newer version is out. Click to see what changed and update.")
            logger.info(
                f"Version {result.version} is available (you have {__version__}). Click the version in the bottom right to update."
            )
        elif isinstance(result, updater.Release):
            self._available_release = None
            self.version_button.setText(f"v{__version__}")
            self.version_button.setStyleSheet("")
            self.version_button.setToolTip(f"Up to date (latest release: v{result.version}).")
        elif isinstance(result, Exception):
            self.version_button.setToolTip(f"Couldn't check for updates: {result}")

    def on_version_clicked(self):
        release = self._available_release
        if release is not None:
            self.open_settings(then=lambda dlg: dlg.app_box._offer(release))
        else:
            self.open_settings(then=lambda dlg: dlg.app_box.check())

    def _run_in_background(self, fn, on_done):
        task = _Task(fn, self)
        task.done.connect(on_done)
        task.finished.connect(lambda: self._background_tasks.remove(task))
        self._background_tasks.append(task)
        task.start()
        return task

    def start_background_tasks(self):
        """Run once after the window opens: check for a newer release (unless turned off in
        Settings → App; it only changes the version label) and keep the desktop shortcut current."""
        if check_updates_on_start(self.config):
            self._run_in_background(lambda _say: updater.latest_release(__version__), self.show_update_state)
        self.maintain_desktop_shortcut()

    # --- desktop shortcut kept up to date -----------------------------------------------------

    def maintain_desktop_shortcut(self):
        """Create the desktop shortcut on first launch and refresh it after each update."""
        state = shortcut.load_state(APP_DIR)
        if not shortcut.needs_refresh(state, __version__):
            return
        first = not state.get("version")
        self._run_in_background(
            lambda _say: shortcut.create_shortcut(version=__version__),
            lambda result: self._shortcut_done(result, first),
        )

    def _shortcut_done(self, result, first):
        if isinstance(result, Exception):
            logger.warning(f"Couldn't create the desktop shortcut: {result}")
            return
        state = shortcut.load_state(APP_DIR)
        state.update({"version": __version__, "path": result})
        shortcut.save_state(APP_DIR, state)
        logger.info(f"Desktop shortcut {'created' if first else 'updated'}: {result}")

    def busy_reason(self):
        """Why the app shouldn't restart right now, or '' if it can."""
        task = getattr(self, "sequence_task", None)
        running = task is not None and not task.done()
        if running or self.current_running_sequence or self.sequence_queue:
            return (
                "A sequence is running or queued. Wait for it to finish, or press Emergency Stop, "
                "before updating: restarting mid-dose can leave a pump running."
            )
        if self.guard.pending():
            return (
                "The app is still trying to turn a pump head off (see the red bar). Wait until it "
                "has, or switch that pump off by hand: closing now would stop it trying."
            )
        if self.guard._in_flight:
            return "A pump command is on its way. Try again in a few seconds."
        if self._motor_jobs or self.head_runs:
            return (
                "A dose or calibration run is in progress. Wait for it to finish, or press Emergency "
                "Stop, before updating: restarting mid-dose can leave a pump running."
            )
        return ""

    def closeEvent(self, event):
        """Closing while pumps run would leave them running: offer to stop them first."""
        busy = self.busy_reason()
        if not busy or getattr(self, "_closing", False):
            if hasattr(self, "calibration_tab") and self.calibration_tab.bal is not None:
                self.calibration_tab.bal.close()
            self.remember_window_state()
            if self._queue_task is not None and not self._queue_task.done():
                self._queue_task.cancel()  # nothing is running or queued (or it was all stopped)
            self.discord.close(timeout=2.0)  # what's left to tell Discord, briefly
            event.accept()
            return
        event.ignore()
        reply = QMessageBox.question(
            self,
            "Pumps are running",
            "A sequence, dose or calibration is still running. Closing now would leave pumps "
            "running.\n\nStop every pump and quit?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._closing = True
        self._start_task(self._stop_then_close)

    async def _stop_then_close(self):
        await self.emergency_stop_all()
        cal = getattr(self, "calibration_tab", None)
        if cal is not None and cal.task is not None and not cal.task.done():
            await asyncio.wait({cal.task}, timeout=15)  # let it send its own OFF too
        for i in range(40):  # up to 20 s for every OFF to go through and be confirmed
            if not self.guard.pending(all_=True):
                break
            if i % 10 == 9 and self.cloud_token:
                await self.fetch_devices(quiet=True)  # read the pumps' reports again now
            await asyncio.sleep(0.5)
        pending = self.guard.pending(all_=True)
        if pending:
            names = ", ".join(self.head_label(*k) for k in pending)
            reply = QMessageBox.question(
                self,
                "Pump not confirmed off",
                f"Couldn't confirm that these heads are off: {names}.\n\nIf the app closes, nothing "
                "will keep trying to stop them. Close anyway (only if you've switched that pump off "
                "by hand), or stay open and keep trying?",
                QMessageBox.StandardButton.Close | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if reply != QMessageBox.StandardButton.Close:
                self._closing = False
                return
            logger.warning(f"Closed while these heads weren't confirmed off: {names}.")
        self._closing = True
        self.close()

    def auto_save_sequence(self):
        """Save the Sequencer tab's sequence being edited (it is also saved as you edit)."""
        tab = getattr(self, "sequencer_tab", None)
        if tab is not None:
            tab.save_draft()

    def save_before_restart(self):
        self.auto_save_sequence()
        self.config.save()
        self.secrets.save()

    def restart_app(self):
        logger.info("Restarting application...")
        self.auto_save_sequence()
        self.remember_window_state()
        single_instance.release()  # or the new copy would find this one running and quit
        subprocess.Popen([sys.executable] + sys.argv)  # noqa: S603 (restarts this same app)
        QApplication.quit()

    # --- Copy Diagnostics (Settings → App) ----------------------------------------------------

    def diagnostics_extra(self):
        """A few lines about the pumps and balance for a bug report: counts only, no IDs."""
        total = len(self.cloud_devices)
        online = sum(1 for d in self.cloud_devices if d.get("is_online", False))
        extra = {
            "Jebao cloud": "logged in" if self.cloud_token else "not logged in",
            "Pumps on the account": f"{total} ({online} online)" if total else "none listed",
            "Pumps in the settings": len(self.config.data.get("pumps") or {}),
        }
        cal = getattr(self, "calibration_tab", None)
        bal = getattr(cal, "bal", None)
        if bal is None:
            extra["Balance"] = "not connected"
        else:
            extra["Balance"] = f"connected ({cal._bal_name()})" + (f", {bal.error}" if bal.error else "")
        extra["Discord"] = self._discord_status()
        return extra

    def private_values(self):
        """What Copy Diagnostics must never show: the Jebao login, the cloud token, pump IDs and MACs."""
        values = [self.secrets.data.get("username"), self.secrets.data.get("password"), self.cloud_token]
        webhook = self.secrets.data.get("discord_webhook")
        values += [webhook, discord_notify.webhook_token(webhook)]
        for d in self.cloud_devices:
            values += [d.get("did"), d.get("mac"), d.get("passcode"), d.get("product_key")]
        values += list(self.config.data.get("pumps") or {})
        for stack in (self.config.data.get("rack") or {}).get("stacks") or []:
            if isinstance(stack, dict):
                values += [s for s in stack.get("slots") or [] if isinstance(s, str)]
        return [str(v) for v in values if v]

    def approve_channel(self, ch):
        if self._ch_review_events.get(ch):
            self._ch_review_events[ch].set()
            self._ch_review_events[ch] = None
        self.refresh_approval_banner()

    def approve_all(self):
        for ch in list(self._ch_review_events):
            self.approve_channel(ch)

    def _approve_buttons(self, count):
        """Make sure there is a Continue button for CH1 … CH``count``."""
        while len(self.btn_approve_channels) < count:
            ch = len(self.btn_approve_channels) + 1
            btn = QPushButton(f"Continue CH{ch}")
            btn.setEnabled(False)
            btn.setVisible(False)
            btn.setStyleSheet(APPROVE_STYLE)
            btn.clicked.connect(lambda checked, ch=ch: self.approve_channel(ch))
            self.btn_approve_channels.append(btn)
            self._banner_layout.insertWidget(self._banner_layout.indexOf(self.btn_approve_all), btn)

    def refresh_approval_banner(self):
        """Show the orange banner while any channel waits for review, with a button per waiting channel."""
        waiting = sorted(ch for ch, event in self._ch_review_events.items() if event)
        self._approve_buttons(max(waiting, default=0))
        for ch, btn in enumerate(self.btn_approve_channels, start=1):
            btn.setEnabled(ch in waiting)
            btn.setVisible(ch in waiting)
            btn.setText(f"Continue CH{ch}" if len(waiting) <= 4 else f"CH{ch}")
        self.btn_approve_all.setEnabled(bool(waiting))
        self.btn_approve_all.setVisible(len(waiting) > 1)
        if waiting:
            chans = ", ".join(f"CH{ch}" for ch in waiting)
            seq = self.current_running_sequence
            where = f" in “{html.escape(seq)}”" if seq else ""
            titles = [self._ch_review_titles.get(ch) for ch in waiting]
            if titles and all(titles) and len(set(titles)) == 1:
                what = f" ({html.escape(titles[0])})"
            elif any(titles):
                what = (
                    " ("
                    + "; ".join(
                        f"CH{ch}: {html.escape(t)}" for ch, t in zip(waiting, titles, strict=True) if t
                    )
                    + ")"
                )
            else:
                what = ""
            text = f"<b>Waiting for you:</b> {chans}{where}{what}. Check, then press Continue."
            messages = {}
            for ch in waiting:
                msg = self._ch_review_messages.get(ch)
                if msg:
                    messages.setdefault(msg, []).append(ch)
            for msg, chs in messages.items():
                who = "" if len(messages) == 1 else f"<b>{sequence.lanes_text(chs)}:</b> "
                body = html.escape(msg).replace("\n", "<br>")
                text += f"<br><span style='font-size: 14px;'>⚠ {who}{body}</span>"
            self.lbl_approval.setText(text)
        self.approval_banner.setVisible(bool(waiting))

    # --- Cloud API Networking Logic ---
    async def login_and_fetch(self):
        user = self.secrets.data.get("username")
        pw = self.secrets.data.get("password")
        if not user or not pw:
            logger.warning("No credentials. Please click Cloud Settings.")
            return

        self.btn_refresh.setEnabled(False)
        self.set_status_color(self.status_indicator, False)
        logger.info("Logging into Gizwits Cloud...")
        if not self._watching:  # keeps trying even if this first login fails
            self._watching = True
            self._start_task(self._watch_pumps)

        url = f"https://{DOMAIN}/app/login"
        headers = {"X-Gizwits-Application-Id": APP_ID, "Content-Type": "application/json"}
        data = {"username": user, "password": pw}

        try:
            resp = await asyncio.to_thread(
                requests.post, url, headers=headers, json=data, timeout=REQUEST_TIMEOUT
            )
            if resp.status_code == 200:
                self.cloud_token = resp.json().get("token")
                logger.info("Login successful. Fetching devices...")
                self.set_status_color(self.status_indicator, True)
                await self.fetch_devices()
            else:
                logger.error(f"Login failed: {resp.text}")
                self.cloud_token = None
        except Exception as e:
            logger.error(f"Cloud API error: {e}")
        finally:
            self.btn_refresh.setEnabled(True)

    async def fetch_devices(self, quiet=False):
        """Ask the cloud for the pump list (and which pumps are online). ``quiet`` is for the
        regular background check: only changes are logged."""
        if not self.cloud_token:
            return

        url = f"https://{DOMAIN}/app/bindings"
        headers = {"X-Gizwits-Application-Id": APP_ID, "X-Gizwits-User-token": self.cloud_token}
        started = time.monotonic()
        try:
            resp = await asyncio.to_thread(requests.get, url, headers=headers, timeout=REQUEST_TIMEOUT)
        except Exception as e:
            self._cloud_check(False, f"no answer: {e.__class__.__name__}")
            if not quiet:
                logger.error(f"Fetch devices error: {e}")
            return
        took = time.monotonic() - started
        if resp.status_code != 200:
            if resp.status_code in (400, 401, 403) and "token" in resp.text.lower() and not self._relogging:
                # The cloud session ran out (the app was open for days): log in again.
                logger.info("The cloud session expired; logging in again.")
                self._relogging = True
                try:
                    await self.login_and_fetch()
                finally:
                    self._relogging = False
                return
            self._cloud_check(False, f"error {resp.status_code}")
            logger.error(f"Failed to fetch devices: {resp.text}")
            return
        self._cloud_check(True, None, took)
        devices = resp.json().get("devices", [])
        known = {d.get("did") for d in self.cloud_devices}
        if not quiet or {d.get("did") for d in devices} != known:
            self.cloud_devices = devices
            if not quiet:
                logger.info(f"Found {len(self.cloud_devices)} bound devices.")
            self.refresh_pumps()
        else:
            # Same pumps: just update who is online, without rebuilding the tabs.
            fresh = {d.get("did"): d for d in devices}
            for d in self.cloud_devices:
                d.update(fresh.get(d.get("did"), {}))
        self._note_online_changes()
        running = {key: run.source for key, run in self.head_runs.items()}
        await self.guard.check(self.cloud_devices, running)
        self.refresh_guard_banner()
        if not self._watching:
            self._watching = True
            self._start_task(self._watch_pumps)

    def _cloud_check(self, ok, problem=None, seconds=None):
        if self.netlog.cloud_check(ok, seconds, problem or ""):
            if ok:
                logger.info("Reached the Jebao cloud again.")
            else:
                logger.error(
                    f"Can't reach the Jebao cloud from this computer ({problem}). Pump status is unknown "
                    "until it answers again; check this computer's internet connection."
                )
        self._update_pump_count()

    def _note_online_changes(self):
        named = [dict(d, name=self.get_device_name(d.get("did"))) for d in self.cloud_devices]
        for p, event, outage in self.netlog.update(named):
            if event == "offline":
                logger.warning(f"{p.name} went offline (lost its Wi-Fi or power).")
            else:
                logger.info(f"{p.name} is back online after {netwatch.fmt_duration(outage)}.")
            self._tell_discord(self._notify_online, p, event, outage)
        self._update_pump_count()

    async def _watch_pumps(self):
        """Every POLL_S seconds, ask the cloud which pumps are online."""
        login_wait = 0
        backoff = 60
        while True:
            await asyncio.sleep(netwatch.POLL_S)
            if self._relogging:
                continue
            try:
                if self.cloud_token:
                    login_wait, backoff = 0, 60
                    await self.fetch_devices(quiet=True)
                elif self.secrets.data.get("username") and self.secrets.data.get("password"):
                    # Logged out (a failed login): keep trying, after 1, 2, 4 … up to 10 min,
                    # since the guard needs the cloud to check and stop pumps.
                    login_wait -= netwatch.POLL_S
                    if login_wait <= 0:
                        await self.login_and_fetch()
                        if not self.cloud_token:
                            login_wait, backoff = backoff, min(backoff * 2, 600)
            except Exception as e:  # never let the watcher die
                logger.debug(f"Pump check failed: {e}")

    def check_pumps_now(self):
        self._start_task(lambda: self.fetch_devices(quiet=True))

    def _update_pump_count(self):
        btn = getattr(self, "pumps_button", None)
        if btn is None:
            return
        total = len(self.cloud_devices)
        online = sum(1 for d in self.cloud_devices if d.get("is_online", False))
        if self.netlog.cloud_ok is False:
            btn.setText("Pumps: cloud unreachable")
            btn.setStyleSheet("color: #DC3545; font-weight: bold;")
        elif not total:
            btn.setText("Pumps: —")
            btn.setStyleSheet("color: grey;")
        else:
            btn.setText(f"Pumps: {online}/{total} online")
            btn.setStyleSheet("" if online == total else "color: #E67E22; font-weight: bold;")

    def open_network_report(self):
        NetworkDialog(self, self).exec()

    # --- Discord notifications (discord_notify.py, Settings → Discord) -------------------------

    def open_discord_settings(self, parent=None):
        DiscordDialog(self, parent or self).exec()

    def _discord_status(self):
        """For Copy Diagnostics: on or off, and how sending went (no URL or IDs)."""
        s = discord_notify.settings(self.config.data.get("discord"))
        if not s["enabled"]:
            return "off"
        url = self.secrets.data.get("discord_webhook", "")
        if discord_notify.check_url(url):
            return "on, but the webhook URL is missing or wrong"
        events = sum(s["events"].values())
        text = f"on ({events} of {len(s['events'])} events), {self.discord.sent} sent"
        if self.discord.failed:
            text += f", {self.discord.failed} not sent (last: {self.discord.last_problem})"
        return text

    def _tell_discord(self, fn, *args, **kwargs):
        """Run a notification hook. A bug in one must never get in the way of a run."""
        try:
            fn(*args, **kwargs)
        except Exception as e:
            logger.warning(f"Discord notification skipped ({type(e).__name__}: {discord_notify.scrub(e)}).")

    def discord_footer(self):
        return f"{eventlog.computer_name()} · Jebao Pump Sequencer {__version__}"

    def _discord_text(self, text):
        """Pump IDs and MAC addresses never go to Discord (a pump with no name shows its ID)."""
        text = str(text or "")
        hidden = [d.get("did") for d in self.cloud_devices] + [d.get("mac") for d in self.cloud_devices]
        hidden += list(self.config.data.get("pumps") or {})
        for value in sorted({str(v) for v in hidden if v and len(str(v)) >= 4}, key=len, reverse=True):
            text = text.replace(value, "a pump")
        return _MAC_ADDRESS.sub("a pump", text)

    def notify(self, event, title, body="", fields=(), items=(), group="", ping=None):
        """Tell Discord about ``event`` (a key of ``discord_notify.EVENTS``), if it's switched on
        in Settings → Discord. Returns True if it was queued. Never waits for Discord."""
        s = discord_notify.settings(self.config.data.get("discord"))
        url = self.secrets.data.get("discord_webhook", "")
        if not s["enabled"] or not s["events"].get(event) or not url:
            return False
        clean = self._discord_text
        note = discord_notify.Note(
            event,
            clean(title),
            body=clean(body),
            fields=[(clean(name), clean(value)) for name, value in fields],
            items=[clean(i) for i in items],
            group=group,
            ping=s["pings"].get(event, False) if ping is None else ping,
        )
        return self.discord.send(url, note, s["mention_id"], s["mention_kind"], self.discord_footer())

    def discord_test(self, url, values, on_done=None):
        """Settings → Discord → Send a test message, with the settings in the dialog."""
        s = discord_notify.settings(values)
        note = discord_notify.Note(
            "test",
            "Test message from the Jebao Pump Sequencer",
            body="Notifications from this computer will look like this.",
            fields=[("Computer", eventlog.computer_name())],
            ping=bool(s["mention_id"]),
        )
        return self.discord.send(
            url, note, s["mention_id"], s["mention_kind"], self.discord_footer(), on_done
        )

    def _notify_review(self, ch, item, message):
        name = self.current_running_sequence or (self.seq_run.name if self.seq_run else "")
        self.notify(
            "review",
            "Waiting for you: {items}",
            body=message,
            fields=[
                ("Sequence", name),
                ("Step", f"{item.top + 1}: {sequence.title(item.step)}"),
                ("To go on", "Press Continue in the app"),
            ],
            items=[f"CH{ch}"],
            group=f"review:{id(self.seq_run)}:{item.path}",
        )

    def _queue_text(self, outcome):
        waiting = len(self.sequence_queue)
        if not waiting:
            return ""
        more = f"{waiting} more waiting"
        if outcome != "done" or self.queue_paused:
            return f"Paused, {more}"
        return f"Next: “{self.sequence_queue[0].get('name') or 'Untitled sequence'}” ({more})"

    def _notify_outcome(self, run):
        fields = [("Took", sequence.fmt_time(run.elapsed()))]
        queue_text = self._queue_text(run.outcome)
        if run.outcome == "done":
            skipped = sum(1 for s in run.cells.values() if s == sequence.SKIPPED)
            if skipped:
                fields.append(("Skipped", f"{skipped} step{'s' if skipped != 1 else ''}"))
            given = [(label, ml) for label, ml in run.delivered.items() if ml > 0.005]
            if given:
                lines = [f"{label}: {ml:.2f} mL" for label, ml in given[:12]]
                if len(given) > 12:
                    lines.append(f"and {len(given) - 12} more")
                fields.append(("Delivered", "\n".join(lines)))
            if queue_text:
                fields.append(("Queue", queue_text))
            self.notify("done", f"Finished “{run.name}”", fields=fields)
            return
        if queue_text:
            fields.append(("Queue", queue_text))
        if run.outcome == "failed":
            self.notify(
                "failed",
                f"“{run.name}” failed",
                body=f"{run.why}. Every head it used was sent OFF.",
                fields=fields,
            )
        else:
            at = f" (at {run.stopped_at})" if run.stopped_at else ""
            self.notify("stopped", f"“{run.name}” stopped", body=f"{run.why}{at}.", fields=fields)

    def _notify_guard(self, pending, now=None):
        """Tell Discord about heads that have waited ``GUARD_NOTIFY_S`` for a confirmed OFF, and
        when none are left."""
        now = time.monotonic() if now is None else now
        for key in [k for k in self._guard_seen if k not in pending]:
            del self._guard_seen[key]
        for key in pending:
            self._guard_seen.setdefault(key, now)
        due = [
            k for k, t in self._guard_seen.items() if now - t >= GUARD_NOTIFY_S and k not in self._guard_told
        ]
        if due:
            self._guard_told |= set(due)
            self.notify(
                "guard",
                "Can't confirm these heads are OFF: {items}",
                body=(
                    "The app keeps trying to turn them off. If a pump is offline, unplug it or switch it "
                    "off by hand, then press “I've switched it off” in the app."
                ),
                items=[f"{self.head_label(*k)} ({pending[k]})" for k in due],
                group="guard",
            )
        if self._guard_told and not pending:
            self._guard_told = set()
            self.notify("guard", "All clear: no heads are left to confirm OFF", ping=False)

    def _pump_in_use(self, did):
        if any(key[0] == did for key in self.head_runs):
            return True
        return self.run_active() and any(key[0] == did for key in self._run_lane_of)

    def _notify_online(self, p, event, outage):
        if event == "offline":
            if not self._pump_in_use(p.did):
                return
            self._offline_told.add(p.did)
            on = [self.head_label(*key) for key in self.head_runs if key[0] == p.did]
            fields = [("Sequence", self.current_running_sequence or "")]
            if on:
                fields.append(("Heads that were on", "\n".join(on)))
            self.notify(
                "offline",
                f"{p.name} went offline",
                body=(
                    "It lost its Wi-Fi or power while in use. A head that was on may keep pumping "
                    "until the pump is back online (the app then turns it off). Switch it off by "
                    "hand if you can."
                ),
                fields=fields,
            )
        elif p.did in self._offline_told:
            self._offline_told.discard(p.did)
            self.notify(
                "offline",
                f"{p.name} is back online",
                body=f"After {netwatch.fmt_duration(outage)}.",
                ping=False,
            )

    async def send_cloud_command(self, did, attrs):
        """Send a command through the pump guard (see pumpguard.py), which keeps track of
        what each head should be doing and makes sure OFFs get through."""
        return await self.guard.command(did, attrs)

    async def _post_control(self, did, attrs):
        if not self.cloud_token:
            logger.error("Not logged in.")
            return False

        url = f"https://{DOMAIN}/app/control/{did}"
        headers = {
            "X-Gizwits-Application-Id": APP_ID,
            "X-Gizwits-User-token": self.cloud_token,
            "Content-Type": "application/json",
        }
        data = {"attrs": attrs}
        try:
            resp = await asyncio.to_thread(
                requests.post, url, headers=headers, json=data, timeout=REQUEST_TIMEOUT
            )
            if resp.status_code == 200:
                return True
            else:
                logger.error(f"Control command failed: {resp.status_code} {resp.text}")
                return False
        except Exception as e:
            logger.error(f"Send command error: {e}")
            return False

    async def _read_pump(self, did):
        """The pump's own latest report ({"attr": {...}, "updated_at": ...}), or None."""
        if not self.cloud_token:
            return None
        url = f"https://{DOMAIN}/app/devdata/{did}/latest"
        headers = {"X-Gizwits-Application-Id": APP_ID, "X-Gizwits-User-token": self.cloud_token}
        try:
            resp = await asyncio.to_thread(requests.get, url, headers=headers, timeout=REQUEST_TIMEOUT)
        except Exception:
            return None
        if resp.status_code != 200:
            return None
        try:
            return resp.json()
        except ValueError:
            return None

    # --- Pump guard: no head left running, none running too long ---

    def head_limit(self, key):
        """The on-time limit for a head: the default from Settings, or its own (Rack tab)."""
        g = self.config.data.get("guard", {})
        default = pumpguard.Limit(
            on_s=float(g.get("on_min", 30)) * 60,
            window_s=float(g.get("window_min", 60)) * 60,
            enabled=bool(g.get("enabled", True)),
        )
        did, ch = key
        own = (self.config.data.get("pumps", {}).get(did, {}).get("limits") or [None] * 4)[ch - 1 : ch]
        own = own[0] if own else None
        if own == "off":
            return pumpguard.Limit(default.on_s, default.window_s, enabled=False)
        if isinstance(own, (int, float)) and own > 0:
            return pumpguard.Limit(min(float(own) * 60, default.window_s), default.window_s, enabled=True)
        return default

    def _guard_stopped(self, key):
        """A head the guard had to keep stopping is confirmed off."""
        run = self.head_runs.get(key)
        if run is not None and run.source == "calibration" and not self.calibration_running():
            self._end_head_run(run)
        self.refresh_guard_banner()

    def _guard_over_limit(self, key):
        run = self.head_runs.get(key)
        if run is not None:
            run.cancelled = True  # e.g. a calibration run: Cancel stops it
            run.resume.set()

    def refresh_guard_banner(self):
        banner = getattr(self, "guard_banner", None)
        if banner is None:
            return
        pending = self.guard.pending()
        self._tell_discord(self._notify_guard, pending)
        if not pending:
            self.guard_bar.setVisible(False)
            return
        names = ", ".join(f"{self.head_label(*k)} ({why})" for k, why in pending.items())
        banner.setText(
            f"⚠ Making sure these heads are OFF, the app keeps retrying: {names}. "
            "If a pump is offline, unplug it or switch it off by hand."
        )
        self.guard_bar.setVisible(True)

    def forget_guard_heads(self):
        pending = self.guard.pending(all_=True)
        if not pending:
            return
        names = ", ".join(self.head_label(*k) for k in pending)
        reply = QMessageBox.question(
            self,
            "Stop checking",
            f"Have you switched these off yourself (or unplugged the pump)?\n\n{names}\n\n"
            "The app will stop trying to turn them off. If one reconnects still running, it is "
            "turned off then.",
        )
        if reply == QMessageBox.StandardButton.Yes:
            for key in pending:
                self.guard.forget(key, "you switched it off by hand")
            self.refresh_guard_banner()

    # --- Pumps and the rack ---
    def rack_layout(self):
        """The saved physical layout. The first time (2.0), every pump goes into one stack in the old order."""
        data = self.config.data.get("rack")
        if not data:
            dids = [d.get("did") for d in self.cloud_devices]
            if not dids:
                return rack.default_layout([])
            old = [d for d in self.config.data.get("pump_order", []) if d in dids]
            data = rack.default_layout(old + [d for d in dids if d not in old])
            self.config.data["rack"] = data
            self.config.save()
            logger.info("Put all pumps in one stack. Arrange them as they sit on the bench in the Rack tab.")
        return rack.normalize(data)

    def save_rack_layout(self, layout):
        self.config.data["rack"] = rack.normalize(layout)
        self.config.save()
        self.refresh_pumps()

    def names_changed(self):
        """Pump or head names were edited in the Rack tab: show them everywhere."""
        self.refresh_pumps()

    def refresh_pumps(self):
        """Show the current pump list and layout in every tab."""
        layout = self.rack_layout()
        dids = [d.get("did") for d in self.cloud_devices]
        order = rack.order(layout, dids)
        self.cloud_devices.sort(
            key=lambda d: order.index(d.get("did")) if d.get("did") in order else len(order)
        )

        if hasattr(self, "sequencer_tab"):
            self.sequencer_tab.rebuild()
        if hasattr(self, "manual_tab"):
            self.manual_tab.rebuild()
        if hasattr(self, "calibration_tab"):
            self.calibration_tab.rebuild()
        if hasattr(self, "rack_tab"):
            self.rack_tab.rebuild()

    def tile_size_changed(self, size, source):
        """Tile size picked in one tab: use it in the other one too."""
        for tab in (getattr(self, "manual_tab", None), getattr(self, "calibration_tab", None)):
            grid = getattr(tab, "grid", None)
            if grid is not None and grid is not source:
                grid.set_tile_size(size, save=False)

    def _refresh_visible_tab(self):
        """Tiles and panels follow the motor runs a few times a second (only the tab in view)."""
        self.calibration_tab._watch_cancel()
        self.refresh_guard_banner()
        current = self.tabs.currentWidget()
        if current in (self.manual_tab, self.calibration_tab, self.sequencer_tab):
            current.refresh()
        running = self.run_active()
        name = "Sequencer ●" if running else "Sequencer"
        index = self.tabs.indexOf(self.sequencer_tab)
        if self.tabs.tabText(index) != name:
            self.tabs.setTabText(index, name)

    def calibration_running(self):
        tab = getattr(self, "calibration_tab", None)
        return bool(tab and tab.running())

    async def set_head(self, did, ch, on):
        """Turn one head on or off. True when the pump took the command."""
        return await self.send_cloud_command(did, {f"channe{ch}": bool(on)})

    def save_head_rate(self, did, ch, rate, how):
        """Store a head's flow rate (mL/min) and today's date, and say so in the log."""
        pump = self.config.get_pump(did)
        old = pump["calibration"][ch - 1]
        pump["calibration"][ch - 1] = round(float(rate), 4)
        dates = (list(pump.get("calibrated_at") or []) + [None] * 4)[:4]
        dates[ch - 1] = datetime.now().strftime("%Y-%m-%d")
        pump["calibrated_at"] = dates
        self.config.save()
        logger.info(f"Saved {self.head_label(did, ch)}: {rate:.3f} mL/min (was {old:.3f}), from {how}.")

    def get_device_name(self, did):
        own = self.config.display_name(did)
        if own:
            return own
        for d in self.cloud_devices:
            if d.get("did") == did:
                return d.get("dev_alias") or d.get("mac")
        return did

    def cloud_name(self, did):
        """The pump's own name on the Jebao cloud (as set in the Jebao Aqua phone app), "" if none."""
        for d in self.cloud_devices:
            if d.get("did") == did:
                return str(d.get("dev_alias") or "").strip()
        return ""

    def cloud_note(self, did):
        """'Jebao app: Doser 3' when the pump has a different name in this app, else ""."""
        alias = self.cloud_name(did)
        if alias and alias != self.get_device_name(did):
            return f"Jebao app: {alias}"
        return ""

    def set_status_color(self, label, connected):
        if connected:
            label.setStyleSheet("color: #28A745; font-weight: bold; font-size: 14px;")
            label.setText("CONNECTED")
        else:
            label.setStyleSheet("color: #DC3545; font-weight: bold; font-size: 14px;")
            label.setText("DISCONNECTED")

    # --- Sequences: the queue and the runs ---------------------------------------------------
    # The steps, plans and run state are in sequence.py; the tab is sequencer_tab.py.

    def _head_rate(self, did, ch):
        """A head's calibrated flow rate in mL/min, or None (a pump the app hasn't seen gets none)."""
        pump = (self.config.data.get("pumps") or {}).get(did)
        try:
            rate = float((pump or {}).get("calibration", [])[ch - 1])
        except (TypeError, ValueError, IndexError):
            return None
        return rate if rate > 0 else None

    def plan_context(self):
        """What sequence.plan() needs to know about the pumps, from the settings and the cloud."""
        pumps = self.config.data.get("pumps") or {}
        listed = {d.get("did"): d for d in self.cloud_devices}

        def calibrated_at(did, ch):
            dates = (pumps.get(did) or {}).get("calibrated_at") or []
            return dates[ch - 1] if 0 < ch <= len(dates) else None

        def online(did):
            d = listed.get(did)
            return None if d is None else bool(d.get("is_online", False))

        return sequence.Context(
            rate=self._head_rate,
            calibrated_at=calibrated_at,
            online=online,
            limit=self.head_limit,
            saved=lambda name: (self.config.data.get("saved_sequences") or {}).get(name),
            head_name=self.config.head_name,
            pump_name=self.get_device_name,
            known=(lambda did: did in listed) if listed else None,
            lane_of=self.channel_number,
            all_lanes=self.all_lanes,
        )

    def plan_sequence(self, steps, name=None, with_limit=True):
        return sequence.plan(steps, self.plan_context(), name=name, with_limit=with_limit)

    def enqueue(self, name, steps, front=False):
        """Add a sequence to the queue (``front``: next). False if it has no steps."""
        steps = sequence.normalize_all(steps)
        if not steps:
            logger.warning("Nothing to run: the sequence has no steps.")
            return False
        item = {"name": name or "Untitled sequence", "data": sequence.to_saved_all(steps)}
        if front:
            self.sequence_queue.insert(0, item)
        else:
            self.sequence_queue.append(item)
        self.refresh_queue_ui()
        running = self.sequence_task is not None and not self.sequence_task.done()
        if self.queue_paused:
            logger.info(f"Added “{item['name']}” to the queue (the queue is paused).")
        elif running or len(self.sequence_queue) > 1:
            logger.info(f"Added “{item['name']}” to the queue.")
        return True

    def run_active(self):
        task = self.sequence_task
        return task is not None and not task.done()

    def set_queue_paused(self, paused):
        if paused == self.queue_paused:
            return
        self.queue_paused = paused
        if paused:
            logger.info("Queue paused: the next sequence won't start until you untick “Pause the queue”.")
        elif self.sequence_queue:
            logger.info("Queue running again.")
        self.refresh_queue_ui()

    def run_stopping(self):
        """True while a run is being stopped (its heads are being turned off)."""
        run = self.seq_run
        return bool(run is not None and run.stopping and not run.ended)

    def stop_run(self, why="you pressed Stop run"):
        """Stop the running sequence (its pumps turn off) and pause the queue."""
        if not self.run_active() or self.run_stopping():
            return
        self.queue_paused = True
        self._stop_reason = why
        if self.seq_run is not None:
            self.seq_run.stopping = True  # from now on Stop and Skip do nothing more
        logger.warning(f"Stopping “{self.current_running_sequence}”: {why}. The queue is paused.")
        self.sequence_task.cancel()
        for tasks in self._ch_running_tasks.values():
            for task in tasks:
                if not task.done():
                    task.cancel()
        self.refresh_queue_ui()

    def lanes_in_use(self):
        run = self.seq_run
        if run is None or run.ended:
            return []
        return [ch for ch, ls in run.lanes.items() if ls.status not in ("idle", "done")]

    def pause_lane(self, ch, paused):
        """Pause one lane (CH1–CH4) of the running sequence: its heads turn off and its
        timers stop until it is resumed. Waits for you and sync points aren't affected."""
        if self._ch_paused.get(ch) == paused:
            return
        self._ch_paused[ch] = paused
        if paused:
            self._ch_resume_events[ch].clear()
            logger.info(f"[CH{ch}] Paused.")
        else:
            self._ch_resume_events[ch].set()
            logger.info(f"[CH{ch}] Resumed.")

    def pause_run(self, paused):
        for ch in self.lanes_in_use() or self.all_lanes():
            self.pause_lane(ch, paused)

    def skip_step(self, ch):
        """Skip the step a lane is on now (its heads turn off); the lane goes on with the next one."""
        task = self._ch_step_tasks.get(ch)
        item = self._ch_current.get(ch)
        if task is None or task.done() or item is None or self.run_stopping():
            return False
        self._skip[ch] = item.path
        logger.warning(f"[CH{ch} · step {item.top + 1}] Skipping {sequence.title(item.step)}.")
        task.cancel()
        return True

    async def _queue_worker(self):
        if getattr(self, "_queue_worker_on", False):
            return  # one is enough: two would start two runs at once
        self._queue_worker_on = True
        try:
            await self._work_the_queue()
        finally:
            self._queue_worker_on = False

    async def _work_the_queue(self):
        while True:
            try:
                await self._queue_step()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # never let the queue stop by itself
                logger.error(f"The sequence queue hit an error ({e}); it carries on.")
                if not self.run_active():
                    self.current_running_sequence = None
                await asyncio.sleep(1)

    async def _queue_step(self):
        """Start the next queued run if it may start (and wait for it), else wait a moment."""
        held = self.queue_paused and not self._run_one
        if not self.sequence_queue or held or self.run_active():
            if not self.run_active():
                self.current_running_sequence = None
            await asyncio.sleep(0.5)
            return
        if self.calibration_running():
            if not self._told_calibration:
                logger.info("A sequence is queued: it will start when the calibration has finished.")
                self._told_calibration = True
            await asyncio.sleep(0.5)
            return
        self._told_calibration = False
        self._run_one = False
        await self._run_queued(self.sequence_queue.pop(0))

    async def _run_queued(self, seq_item):
        seq_name = seq_item.get("name") or "Untitled sequence"
        self.current_running_sequence = seq_name
        self._stop_reason = ""
        outcome = "stopped"
        try:
            self.refresh_queue_ui()
            self.sequence_task = asyncio.create_task(
                self._async_run_sequence(seq_name, seq_item.get("data") or [])
            )
            outcome = await self.sequence_task
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise  # the app is closing
            outcome = "stopped"
        except Exception as e:
            logger.error(f"“{seq_name}” stopped because of an error: {e}")
            outcome = "failed"
        finally:
            self.current_running_sequence = None
            self.refresh_queue_ui()
        if outcome != "done" and self.sequence_queue and not self.queue_paused:
            self.queue_paused = True
            logger.warning(
                "The queue is paused because that run didn't finish. Untick “Pause the queue” "
                "in the Sequencer tab to start the next one."
            )
            self.refresh_queue_ui()

    def run_next_now(self):
        """Start the first queued run even though the queue is paused (the rest stay paused)."""
        if self.queue_paused:
            self._run_one = True

    def queue_current_table(self):
        """The Sequencer tab's sequence, to the end of the queue."""
        tab = getattr(self, "sequencer_tab", None)
        if tab is not None:
            tab.add_to_queue()

    def refresh_saved_sequences_ui(self):
        tab = getattr(self, "sequencer_tab", None)
        if tab is not None:
            tab.refresh_library()

    def refresh_queue_ui(self):
        tab = getattr(self, "sequencer_tab", None)
        if tab is not None:
            tab.refresh_queue()

    async def _async_run_sequence(self, seq_name, seq_data, call_stack=None):
        """Run a sequence: each lane (CH1–CH4) goes through its own steps, side by side.

        Returns "done", "failed" (a step failed, e.g. a pump wouldn't turn on, and the rest
        was stopped) or "stopped" (a head was cancelled in the Manual tab). Stop run and
        Emergency Stop cancel it. However it ends, every head it ran is sent OFF and the run
        goes into the run history.
        """
        steps = sequence.normalize_all(seq_data)
        plan = self.plan_sequence(steps, name=seq_name)
        run = sequence.RunState(seq_name, steps, plan.flat, plan=plan)
        self.seq_run = run
        lanes = sorted({ch for item in plan.flat for ch in item.lanes})
        # Which lane each head runs in, fixed for this run (moving pumps in the rack meanwhile
        # doesn't move their steps to other lanes halfway through)
        self._run_lane_of = {
            key: self.channel_number(*key) for item in plan.flat for key in sequence.heads_of(item.step)
        }
        self._ch_running_tasks = {ch: [] for ch in lanes}
        self._ch_step_tasks = {}
        self._ch_current = {}
        self._skip = {}
        self._sync_points = {}
        self._run_problem = None
        for ch in set(lanes) | set(self._ch_paused):
            self._ch_paused[ch] = False
            self._ch_resume_events[ch].set()
        for check in plan.checks:
            if check.level == "error":
                logger.warning(f"[{seq_name}] {check.text}")
        logger.info(
            f"=== Starting “{seq_name}”: {len(steps)} step{'s' if len(steps) != 1 else ''} on "
            f"{sequence.lanes_text(lanes)}, about {sequence.fmt_time(plan.total_s)} ==="
        )
        self._lane_tasks = {ch: asyncio.create_task(self._run_lane(ch, run)) for ch in lanes}
        outcome, why = "done", ""
        try:
            pending = set(self._lane_tasks.values())
            while pending and outcome == "done":
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    if task.cancelled():
                        outcome, why = self._run_problem or (
                            "stopped",
                            "a head was stopped in the Manual tab",
                        )
                    elif task.exception() is not None:
                        outcome, why = "failed", str(task.exception()).rstrip(".")
        except asyncio.CancelledError:
            outcome, why = "stopped", self._stop_reason or "Emergency Stop"
            raise
        finally:
            run.stopping = True
            leftovers = list(self._lane_tasks.values())
            for tasks in self._ch_running_tasks.values():
                leftovers += tasks
            for task in leftovers:
                if not task.done():
                    task.cancel()
            # Each sends its own OFF. Another Stop or Emergency Stop meanwhile doesn't cut
            # this short: the run only ends once they have.
            deadline = time.monotonic() + 15
            while leftovers and time.monotonic() < deadline:
                waiting = [t for t in leftovers if not t.done()]
                if not waiting:
                    break
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.wait(waiting, timeout=max(0.05, deadline - time.monotonic()))
            if leftovers:
                for task in leftovers:
                    if task.done() and not task.cancelled():
                        task.exception()
            for ch in set(lanes) | set(self._ch_review_events) | set(self._ch_paused):
                if self._ch_review_events.get(ch):
                    self._ch_review_events[ch].set()
                    self._ch_review_events[ch] = None
                self._ch_paused[ch] = False
                self._ch_resume_events[ch].set()
            self._ch_review_titles = {}
            self._ch_review_messages = {}
            self._review_at = {}
            for popup in list(self._review_popups.values()):
                popup.done(0)
            self._review_popups = {}
            self.refresh_approval_banner()
            run.finish(outcome, why)
            self._log_outcome(run)
            self._tell_discord(self._notify_outcome, run)
            try:
                sequence.append_history(self.run_history_path, run.record())
            except OSError as e:
                logger.warning(f"Couldn't save the run to the run history: {e}")
            tab = getattr(self, "sequencer_tab", None)
            if tab is not None:
                tab.run_finished(run)
        return outcome

    def _log_outcome(self, run):
        took = sequence.fmt_time(run.elapsed())
        if run.outcome == "done":
            skipped = sum(1 for s in run.cells.values() if s == sequence.SKIPPED)
            extra = f", {skipped} step{'s' if skipped != 1 else ''} skipped" if skipped else ""
            logger.info(f"=== “{run.name}” finished in {took}{extra}. ===")
        elif run.outcome == "failed":
            logger.error(
                f"=== “{run.name}” failed after {took}: {run.why}. Every head it used was sent OFF. ==="
            )
        else:
            at = f" at {run.stopped_at}" if run.stopped_at else ""
            logger.warning(f"=== “{run.name}” stopped{at} after {took} ({run.why}). ===")

    def _fail_run(self, why, outcome="failed"):
        """A background step went wrong: stop the whole run."""
        if self._run_problem is None:
            self._run_problem = (outcome, why)
        for task in getattr(self, "_lane_tasks", {}).values():
            if not task.done():
                task.cancel()

    async def _run_lane(self, ch, run):
        """One lane: its steps in order. A skipped step is cancelled (its heads turn OFF) and
        the lane goes on; anything else that stops a step stops the run."""
        me = asyncio.current_task()
        background = self._ch_running_tasks[ch]
        for item in [it for it in run.flat if ch in it.lanes]:
            while self._ch_paused.get(ch, False):  # paused between steps
                await asyncio.sleep(0.1)
            self._skip.pop(ch, None)
            self._ch_current[ch] = item
            run.begin(ch, item)
            where = f"CH{ch} · step {item.top + 1}"
            step = item.step
            if step["kind"] in sequence.PUMPING and step["background"]:
                task = asyncio.create_task(self._run_step(ch, item, run, background=True))
                background.append(task)
                run.background(ch, item, True)
                task.add_done_callback(lambda t, item=item: self._background_done(ch, item, run, t))
                logger.info(f"[{where}] {sequence.title(step)}: started in the background.")
                continue
            task = asyncio.create_task(self._run_step(ch, item, run))
            self._ch_step_tasks[ch] = task
            try:
                await task
            except asyncio.CancelledError:
                if me.cancelling() or self._skip.get(ch) != item.path:
                    raise
                if step["kind"] == "sync":
                    self._sync_points.setdefault(item.path, sequence.SyncPoint(item.lanes)).arrive(ch)
                run.end(ch, item, sequence.SKIPPED)
                continue
            except Exception as e:
                run.end(ch, item, sequence.FAILED)
                logger.error(f"[{where}] {sequence.title(step)} failed: {e}")
                raise
            finally:
                self._ch_step_tasks.pop(ch, None)
            run.end(ch, item, sequence.DONE)
        left = [t for t in background if not t.done()]
        if left:
            ls = run.lanes[ch]
            ls.status, ls.title = "sync", "Waiting for its background steps"
            ls.detail = ""
            await asyncio.wait(left)
        run.lane_finished(ch)

    def _background_done(self, ch, item, run, task):
        run.background(ch, item, False)
        if getattr(run, "stopping", False) or run.ended:
            run.end(ch, item, sequence.STOPPED if task.cancelled() else sequence.DONE)
            return
        where = f"CH{ch} · step {item.top + 1}"
        if task.cancelled():
            run.end(ch, item, sequence.STOPPED)
            self._fail_run("a head was stopped in the Manual tab", outcome="stopped")
            return
        exc = task.exception()
        if exc is None:
            run.end(ch, item, sequence.DONE)
            logger.info(f"[{where}] {sequence.title(item.step)} (background): done.")
            return
        run.end(ch, item, sequence.FAILED)
        logger.error(f"[{where}] {sequence.title(item.step)} (background) failed: {exc}")
        self._fail_run(f"step {item.top + 1} on CH{ch} failed: {str(exc).rstrip('.')}")

    async def _run_step(self, ch, item, run, background=False):
        step, kind = item.step, item.step["kind"]
        where = f"CH{ch} · step {item.top + 1}"
        ls = run.lanes[ch]
        if kind == "wait":
            logger.info(f"[{where}] {sequence.title(step)}.")

            def tick(left):
                ls.detail = f"{sequence.fmt_clock(left)} left"

            await self._interruptible_sleep_timer(ch, step["seconds"], tick=tick)
        elif kind == "review":
            event = asyncio.Event()
            message = step.get("message", "")
            self._ch_review_events[ch] = event
            self._ch_review_titles[ch] = f"step {item.top + 1}" + (
                f": {step['label']}" if step["label"] else ""
            )
            self._ch_review_messages[ch] = message
            self._review_at[ch] = item.path
            ls.detail = message
            self.refresh_approval_banner()
            self._tell_discord(self._notify_review, ch, item, message)
            if message:
                self._show_review_popup(item)
            if step.get("sound"):
                QApplication.beep()
            said = f" Message: {message}" if message else ""
            logger.info(
                f"[{where}] Waiting for you ({sequence.title(step)}). Press Continue CH{ch} to go on.{said}"
            )
            try:
                await event.wait()
            finally:
                if self._ch_review_events.get(ch) is event:
                    self._ch_review_events[ch] = None
                self._ch_review_titles.pop(ch, None)
                self._ch_review_messages.pop(ch, None)
                self._review_at.pop(ch, None)
                self._review_popup_done(ch, item.path)
                self.refresh_approval_banner()
            logger.info(f"[{where}] Continued.")
        elif kind == "sync":
            mine = [t for t in self._ch_running_tasks.get(ch, []) if not t.done()]
            if mine:
                ls.detail = f"waiting for {len(mine)} background step{'s' if len(mine) != 1 else ''}"
                await asyncio.wait(mine)
            point = self._sync_points.setdefault(item.path, sequence.SyncPoint(item.lanes))
            if not point.event.is_set():
                others = [c for c in point.waiting_for() if c != ch]
                ls.detail = "waiting for " + sequence.lanes_text(others) if others else ""
                logger.info(f"[{where}] Sync: waiting for the other channels.")
            await point.wait(ch)
        else:
            problem = sequence.step_problem(step)
            if problem:
                raise RuntimeError(f"{sequence.title(step)}: {problem}, so the run stops here")
            heads = [key for key in sequence.heads_of(step) if self._lane_for(key) == ch]
            await self._together([self._run_head(ch, item, run, key, background) for key in heads])

    def _show_review_popup(self, item):
        """Pop up a Wait for me step's message, once for all the channels waiting at it."""
        popup = self._review_popups.get(item.path)
        if popup is None:
            popup = ReviewPopup(
                self,
                name=self.current_running_sequence or "",
                step_number=item.top + 1,
                title=sequence.title(item.step),
                message=item.step.get("message", ""),
            )
            self._review_popups[item.path] = popup
            popup.finished.connect(lambda _r, path=item.path, p=popup: self._review_popup_closed(path, p))
        for ch, path in self._review_at.items():
            if path == item.path:
                popup.add_lane(ch)
        popup.show()
        popup.raise_()
        popup.activateWindow()
        QApplication.alert(self)  # flashes the taskbar button / bounces the Dock icon

    def _review_popup_closed(self, path, popup):
        if self._review_popups.get(path) is popup:
            del self._review_popups[path]

    def _review_popup_done(self, ch, path):
        popup = self._review_popups.get(path)
        if popup is not None:
            popup.remove_lane(ch)

    async def _run_head(self, lane, item, run, key, background=False):
        """One head's part of a dose or staccato step, in ``lane`` (its CH number).
        ``key`` is (pump, the pump's own channel): what the commands go to."""
        did, ch = key
        step = item.step
        label = self.head_label(did, ch)
        rate = self._head_rate(did, ch)
        pattern = sequence.pump_pattern(step, rate)
        if pattern is None:
            raise RuntimeError(f"{label} has no flow rate. Calibrate it first.")
        bursts = sum(1 for what, _ in pattern if what == "pump")
        pumping = sum(s for what, s in pattern if what == "pump")
        where = f"CH{lane} · step {item.top + 1}"
        if (did, ch) in self.head_runs:  # a background step or a manual dose still has it
            logger.info(f"[{where}] Waiting for {label} to finish what it is doing first.")
            if not background:
                run.lanes[lane].detail = f"waiting for {label} to be free"
            while (did, ch) in self.head_runs:
                await asyncio.sleep(0.1)
        logger.info(f"[{where}] {sequence.describe(step)} on {label}: motor on {sequence.fmt_time(pumping)}.")
        ls = run.lanes[lane]
        done = 0
        for what, seconds in pattern:
            if what == "gap":

                def tick(left, done=done):
                    if not background:
                        ls.detail = f"burst {done} of {bursts} done · next in {sequence.fmt_clock(left)}"

                await self._interruptible_sleep_timer(lane, seconds, tick=tick)
                continue
            done += 1
            if bursts > 1 and not background:
                ls.detail = f"burst {done} of {bursts}"
            meter = []
            try:
                await self._timed_run(did, ch, seconds, meter=meter, lane=lane)
            finally:
                if rate and meter:
                    run.add_delivered(label, meter[0] * rate / 60.0, lane=lane, background=background)

    async def _together(self, coros):
        """Run several heads' jobs at once. If one fails or is cancelled, the others are
        stopped too (each sends its own OFF), and the problem is passed on."""
        tasks = [asyncio.create_task(c) for c in coros]
        pending = set(tasks)
        problem = None
        try:
            while pending and problem is None:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    if task.cancelled():
                        problem = problem or asyncio.CancelledError("a head was cancelled")
                    elif task.exception() is not None:
                        problem = task.exception()
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            await asyncio.wait(tasks)
            raise
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.wait(pending)
        for task in tasks:  # read every outcome, so none is reported as "never retrieved"
            if not task.cancelled():
                task.exception()
        if problem is not None:
            raise problem

    async def _interruptible_sleep_timer(self, ch, seconds, tick=None):
        """Wait ``seconds``, stopping the clock while lane ``ch`` is paused."""
        left = float(seconds)
        while left > 0:
            while self._ch_paused.get(ch, False):
                await asyncio.sleep(0.1)
            chunk = min(0.1, left)
            started = time.monotonic()
            await asyncio.sleep(chunk)
            left -= time.monotonic() - started
            if tick is not None:
                tick(max(0.0, left))

    def _begin_head_run(self, did, ch, seconds, source, lane=None):
        run = HeadRun(
            did=did, ch=ch, duration=float(seconds), remaining=float(seconds), source=source, lane=lane
        )
        self.head_runs[run.key] = run
        return run

    def _end_head_run(self, run):
        if self.head_runs.get(run.key) is run:
            del self.head_runs[run.key]

    def head_label(self, did, ch):
        """'JP 6: Sample · CH5 (Sample → Col 5)': pump name, CH number and, if set, the head's
        own name. ``ch`` is the pump's own channel (1–4); the CH number follows the rack."""
        name = self.config.head_name(did, ch)
        text = f"{self.get_device_name(did)} · CH{self.channel_number(did, ch)}"
        return f"{text} ({name})" if name else text

    # --- CH numbers: CH1–4 in Stack A, CH5–8 in Stack B, … (rack.channel_number) -------------

    def _channel_groups(self):
        dids = tuple(d.get("did") for d in self.cloud_devices)
        layout = self.config.data.get("rack")
        key = (json.dumps(layout, sort_keys=True, default=str), dids)
        if key != self._groups_key:
            self._groups = rack.channel_groups(rack.normalize(layout or {}), dids)
            self._groups_key = key
        return self._groups

    def channel_number(self, did, ch):
        """The CH number shown (and the sequence lane) for a pump's own channel ``ch``."""
        return rack.channel_number(self._channel_groups(), did, ch)

    def all_lanes(self):
        """Every CH number the rack has: CH1–4, plus CH5–8 with a second stack in use, …"""
        groups = set(self._channel_groups().values()) or {0}
        return sorted(g * rack.CHANNELS + c for g in groups for c in sequence.CHANNELS)

    def _lane_for(self, key):
        """The lane a head runs in during the current run (else its CH number now)."""
        lane = self._run_lane_of.get(tuple(key))
        return lane if lane is not None else self.channel_number(*key)

    async def _interruptible_motor_run(self, ch, pump_did, seconds, source="sequence", meter=None, lane=None):
        """Keep a head's motor on for ``seconds``, honouring pause and cancel.

        ``ch`` is the pump's own channel (what the commands go to). A sequence's lane (its CH
        number, ``lane``; the pump channel if not given) can be paused or cancelled as a
        whole (``_ch_paused`` / ``_ch_cancelled``); any single head can also be paused or
        cancelled from the Manual tab (its ``HeadRun``). ``meter`` (a list) gets the seconds
        it really pumped.
        """
        channel_key = f"channe{ch}"
        by_channel = source == "sequence"
        lane = ch if lane is None else lane
        if by_channel:
            self._ch_cancelled[lane] = False
        run = self._begin_head_run(pump_did, ch, seconds, source, lane=lane if by_channel else None)
        try:
            while run.remaining > 0:
                if run.cancelled or (by_channel and self._ch_cancelled.get(lane, False)):
                    await self.send_cloud_command(pump_did, {channel_key: False})
                    raise asyncio.CancelledError(f"CH{lane} dose cancelled.")

                if run.paused or (by_channel and self._ch_paused.get(lane, False)):
                    await self.send_cloud_command(pump_did, {channel_key: False})
                    logger.info(f"[{self.head_label(pump_did, ch)}] Paused mid-dose. Waiting to resume...")
                    while (
                        run.paused or (by_channel and self._ch_paused.get(lane, False))
                    ) and not run.cancelled:
                        await asyncio.sleep(0.1)  # resumed, or cancelled, from either place
                    if run.cancelled or (by_channel and self._ch_cancelled.get(lane, False)):
                        raise asyncio.CancelledError(f"CH{lane} dose cancelled while paused.")
                    logger.info(f"[{self.head_label(pump_did, ch)}] Resuming ({run.remaining:.1f} s left).")
                    if self.guard.over_limit(run.key):
                        continue  # rest first; it turns on again after
                    await self._turn_on(pump_did, ch)

                if self.guard.over_limit(run.key):
                    await self._rest_head(run, by_channel)
                    continue

                chunk = min(0.1, run.remaining)
                start_time = time.time()
                await asyncio.sleep(chunk)
                run.remaining -= time.time() - start_time
        finally:
            self._end_head_run(run)
            if meter is not None:
                meter.append(max(0.0, run.duration - max(0.0, run.remaining)))

    async def _rest_head(self, run, by_channel):
        """The head used up its on-time budget: turn it off, wait, then carry on."""
        did, ch = run.key
        lane = run.lane if run.lane is not None else ch
        label = self.head_label(did, ch)
        limit = self.head_limit(run.key)
        await self.send_cloud_command(did, {f"channe{ch}": False})
        run.cooling_until = self.guard.resume_at(run.key)
        logger.warning(
            f"[{label}] Reached its on-time limit ({limit.text()}). Resting until "
            f"{datetime.fromtimestamp(run.cooling_until):%H:%M}, then the dose carries on "
            f"({run.remaining / 60:.1f} min of pumping left)."
        )
        self._tell_discord(
            self.notify,
            "rest",
            "Resting (on-time limit): {items}",
            body="Each turns on again by itself when it may. The full volume still goes in, just later.",
            items=[f"{label} until {datetime.fromtimestamp(run.cooling_until):%H:%M}"],
            group="rest",
        )
        try:
            while time.time() < run.cooling_until:
                if run.cancelled or (by_channel and self._ch_cancelled.get(lane, False)):
                    raise asyncio.CancelledError(f"CH{lane} dose cancelled while resting.")
                await asyncio.sleep(min(1.0, max(0.05, run.cooling_until - time.time())))
                run.cooling_until = max(run.cooling_until, self.guard.resume_at(run.key))
        finally:
            run.cooling_until = None
        if run.paused or (by_channel and self._ch_paused.get(lane, False)):
            return  # paused while resting: it turns on again when resumed
        logger.info(f"[{label}] Rested; carrying on ({run.remaining / 60:.1f} min of pumping left).")
        await self._turn_on(did, ch)

    # --- Manual dispensing on several heads at once --------------------------------------

    def run_state(self, did, ch):
        """(state, progress 0-1, seconds left) for a head, or None when its motor is off."""
        run = self.head_runs.get((did, ch))
        if run is None:
            return None
        if run.cooling_until is not None:
            return ("cooling", run.progress, max(0.0, run.cooling_until - time.time()))
        lane = run.lane if run.lane is not None else ch
        paused = run.paused or (run.source == "sequence" and self._ch_paused.get(lane, False))
        return ("paused" if paused else "running", run.progress, max(0.0, run.remaining))

    def dispense_seconds(self, did, ch, value, unit):
        """Motor on-time for a dose of ``value`` mL (unit "volume") or ``value`` s (unit "time")."""
        if unit == "time":
            return float(value)
        rate = self.config.get_pump(did)["calibration"][ch - 1]
        if not rate or rate <= 0:
            raise ValueError(f"{self.head_label(did, ch)} has no flow rate. Calibrate it first.")
        return float(value) / rate * 60.0

    def dispense(self, heads, value, unit):
        """Start dispensing on several heads at once. Returns the started tasks.

        ``heads`` is a list of (did, ch); ``unit`` is "volume" (mL) or "time" (s).
        Heads that are already running, on an offline pump or without a flow rate are skipped.
        """
        online = {d.get("did"): d.get("is_online", True) for d in self.cloud_devices}
        plan = {}
        for did, ch in heads:
            label = self.head_label(did, ch)
            if (did, ch) in self.head_runs:
                logger.warning(f"{label} is already running; skipped.")
                continue
            if not online.get(did, False):
                logger.warning(f"{label}: the pump is offline; skipped.")
                continue
            try:
                seconds = self.dispense_seconds(did, ch, value, unit)
            except ValueError as e:
                logger.warning(f"{e} Skipped.")
                continue
            plan.setdefault(did, []).append((ch, seconds))
        tasks = []
        for did, items in plan.items():
            task = self._start_task(
                lambda did=did, items=items: self._manual_pump_dispense(did, items, value, unit)
            )
            if task is not None:
                tasks.append(task)
                self._dose_tasks.add(task)
                task.add_done_callback(self._dose_tasks.discard)
        return tasks

    async def _manual_pump_dispense(self, did, items, value, unit):
        """Turn on the given channels of one pump together, then stop each after its own time."""
        self._motor_jobs += 1
        try:
            what = f"{value:g} mL" if unit == "volume" else f"{value:g} s"
            start_now = {f"channe{ch}": True for ch, _ in items if not self.guard.over_limit((did, ch))}
            ok = await self.send_cloud_command(did, start_now) if start_now else True
            if not ok:
                logger.error(
                    f"Couldn't start {self.get_device_name(did)}; nothing was dispensed there "
                    "(the app makes sure it is off, in case the command got through anyway)."
                )
                return
            for ch, seconds in items:
                logger.info(f"Dispensing {what} on {self.head_label(did, ch)}: motor on for {seconds:.1f} s.")
            await asyncio.gather(*(self._manual_head(did, ch, seconds) for ch, seconds in items))
        finally:
            self._motor_jobs -= 1

    async def _manual_head(self, did, ch, seconds):
        label = self.head_label(did, ch)
        outcome = "done"
        try:
            await self._interruptible_motor_run(ch, did, seconds, source="manual")
        except asyncio.CancelledError:
            outcome = "cancelled"
            logger.warning(f"{label}: dispensing cancelled.")
        except RuntimeError as e:  # couldn't turn it on again after a pause or a rest
            outcome = "failed"
            logger.error(f"{label}: {e}")
        finally:
            stopped = await self.send_cloud_command(did, {f"channe{ch}": False})
        if not stopped:
            logger.error(f"{label}: the STOP command failed; the app keeps retrying (see the red bar).")
        elif outcome == "done":
            logger.info(f"{label}: done.")

    def pause_heads(self, heads):
        for key in heads:
            run = self.head_runs.get(tuple(key))
            if run and run.source == "calibration":
                logger.warning(
                    f"{self.head_label(*run.key)} is being calibrated: use Stop in the Calibration tab."
                )
                continue
            if run and not run.paused:
                run.paused = True
                run.resume.clear()

    def resume_heads(self, heads):
        for key in heads:
            run = self.head_runs.get(tuple(key))
            if run and run.paused:
                run.paused = False
                run.resume.set()

    def cancel_heads(self, heads):
        for key in heads:
            run = self.head_runs.get(tuple(key))
            if run:
                run.cancelled = True
                run.resume.set()

    async def _timed_run(self, did, ch, seconds, source="sequence", meter=None, lane=None):
        """ON, keep it on for ``seconds`` (pausing, cancelling and resting as needed), OFF.
        The OFF is sent however the run ends, even if the step is cancelled or fails.
        ``ch`` is the pump's own channel; ``lane`` the sequence lane (CH number) it pauses with."""
        channel_key = f"channe{ch}"
        try:
            if not self.guard.over_limit((did, ch)):  # over it: the run rests first, then turns on
                await self._turn_on(did, ch)
            await self._interruptible_motor_run(ch, did, seconds, source, meter=meter, lane=lane)
        finally:
            await self.send_cloud_command(did, {channel_key: False})

    async def _turn_on(self, did, ch):
        """Turn a head on for a dose, retrying a little. If it won't, the dose stops with an
        error instead of counting down as if it were pumping."""
        for wait in (0, 2, 5):
            if wait:
                await asyncio.sleep(wait)
            if await self.send_cloud_command(did, {f"channe{ch}": True}):
                return
            if time.time() < self.guard.hold_until:
                break  # Emergency Stop: don't keep trying
        message = (
            f"Couldn't turn {self.head_label(did, ch)} on (cloud or Wi-Fi problem), so this dose "
            "was stopped rather than counted as delivered."
        )
        logger.error(message)  # also shown for Async steps, whose errors are otherwise collected quietly
        raise RuntimeError(message)

    def emergency_stop_all(self):
        logger.info("!!! GLOBAL EMERGENCY STOP INITIATED !!!")
        if self.sequence_queue:
            logger.warning(f"Cleared {len(self.sequence_queue)} pending sequences from the queue.")
            self.sequence_queue.clear()
            self.refresh_queue_ui()

        self.guard.hold(15)  # nothing may turn on for a moment, even a command already on its way
        self._stop_reason = "Emergency Stop"
        if self.sequence_task and not self.sequence_task.done():
            if self.seq_run is not None and not self.seq_run.ended:
                self.seq_run.stopping = True
            self.sequence_task.cancel()
        for tasks in getattr(self, "_ch_running_tasks", {}).values():  # Async Dose/Staccato steps
            for task in tasks:
                if not task.done():
                    task.cancel()
        for task in list(self._dose_tasks):
            if not task.done():
                task.cancel()
        self.cancel_heads(list(self.head_runs))
        if hasattr(self, "calibration_tab"):
            self.calibration_tab.stop()
        return asyncio.create_task(self._async_stop_all())

    async def _async_stop_all(self):
        tasks = []
        # Stop all channels on all cloud devices
        attrs = {"channe1": False, "channe2": False, "channe3": False, "channe4": False}
        dids = [d.get("did") for d in self.cloud_devices]
        if not dids:
            logger.error("STOP not sent: no pumps are listed. Stop the pumps by hand or with the phone app.")
            return
        for did in dids:
            tasks.append(self.send_cloud_command(did, attrs))

        results = await asyncio.gather(*tasks, return_exceptions=True)
        failed = [self.get_device_name(did) for did, ok in zip(dids, results, strict=True) if ok is not True]
        stopped = {did for did, ok in zip(dids, results, strict=True) if ok is True}
        for run in list(self.head_runs.values()):  # a calibration head whose own OFF had failed
            if run.source == "calibration" and run.did in stopped and not self.calibration_running():
                self._end_head_run(run)
        if failed:
            logger.error(
                f"STOP FAILED for {len(failed)} of {len(dids)} pump(s): {', '.join(failed)}. "
                "Stop them by hand or with the phone app, or press Emergency Stop again."
            )
        else:
            logger.info(f"Sent STOP command to all channels on all {len(dids)} pump(s).")


ICON_DIR = os.path.join(script_dir, "assets")


def set_app_icon(app):
    """Show the app's own icon in the window, the Dock and the Windows taskbar."""
    if sys.platform == "win32":
        # Without this, Windows groups the app under python.exe and shows Python's icon.
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("brandonbtm29.JebaoPumpSequencer")
        except (AttributeError, OSError):
            pass
    icon = os.path.join(ICON_DIR, "jebao.ico" if sys.platform == "win32" else "jebao.png")
    if os.path.exists(icon):
        app.setWindowIcon(QIcon(icon))


def main():
    app = QApplication(sys.argv)
    # Only one copy at a time: two would send the same pumps conflicting commands. Opening the
    # app again brings the running copy's window forward instead (a QLocalServer signal, which
    # the qasync loop delivers like any other Qt event).
    instance_guard = single_instance.acquire(APP_NAME)
    if instance_guard is None:
        sys.exit(0)
    set_app_icon(app)

    # Load config to apply theme early
    config = ConfigManager()
    apply_global_theme(app, config)

    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)

    # The system log is kept on disk: show recent history, then keep writing to it.
    folder = eventlog.log_dir(DATA_DIR)
    keep_days = int(config.data.get("log_keep_days", eventlog.DEFAULT_KEEP_DAYS))
    eventlog.prune(folder, keep_days)
    history = eventlog.format_history(eventlog.load_recent(folder, keep_days))
    logger.addHandler(eventlog.DailyFileHandler(folder))

    window = JebaoPumpGUI(history=history)
    instance_guard.set_window(window)
    window.instance_guard = instance_guard  # held for the app's lifetime
    window.show()
    QTimer.singleShot(1500, window.start_background_tasks)

    with loop:
        loop.run_forever()


if __name__ == "__main__":
    main()
