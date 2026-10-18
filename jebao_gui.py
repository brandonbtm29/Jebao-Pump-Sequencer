"""Jebao Pump Sequencer: PyQt6 app that drives Jebao dosing pumps through the Gizwits cloud API."""

__version__ = "1.4.0"

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
import eventlog
import rack
import settings_location
import shortcut
import updater
from rack_widgets import ManualTab, RackTab

APP_DIR = settings_location.app_folder(script_dir)
CONFIG_DIR = settings_location.resolve(APP_DIR, DATA_DIR)
SETTINGS_FILES = ["jebao_config.json", "secrets.json"]
import asyncio
import json
import logging
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime

import qasync
import requests
from PyQt6.QtCore import QEvent, QObject, Qt, QThread, QTimer, QUrl, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QColor, QDesktopServices, QIcon, QPalette, QStandardItem, QStandardItemModel
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QTableWidget,
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


class CheckableComboBox(QComboBox):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setModel(QStandardItemModel(self))
        self.model().dataChanged.connect(self.updateText)
        self.view().setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)

        # Add a dummy item that acts as the display text for the combobox
        self._display_item = QStandardItem("1")
        self.model().appendRow(self._display_item)
        self.view().setRowHidden(0, True)

        self.view().viewport().installEventFilter(self)

    def eventFilter(self, obj, event):
        if obj == self.view().viewport():
            if event.type() == QEvent.Type.MouseButtonRelease:
                index = self.view().indexAt(event.pos())
                if index.isValid() and index.row() > 0:
                    item = self.model().itemFromIndex(index)
                    if item.checkState() == Qt.CheckState.Checked:
                        item.setCheckState(Qt.CheckState.Unchecked)
                    else:
                        item.setCheckState(Qt.CheckState.Checked)
                return True
        return super().eventFilter(obj, event)

    def addItem(self, text, userData=None):
        item = QStandardItem(text)
        item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
        item.setData(Qt.CheckState.Unchecked, Qt.ItemDataRole.CheckStateRole)
        item.setData(userData, Qt.ItemDataRole.UserRole)
        self.model().appendRow(item)

    def addItems(self, texts):
        for text in texts:
            self.addItem(text)

    def checkedItems(self):
        checked = []
        for i in range(1, self.model().rowCount()):
            item = self.model().item(i)
            if item.checkState() == Qt.CheckState.Checked:
                checked.append(item.text())
        return checked

    def setCheckedItems(self, items):
        for i in range(1, self.model().rowCount()):
            item = self.model().item(i)
            if item.text() in items:
                item.setCheckState(Qt.CheckState.Checked)
            else:
                item.setCheckState(Qt.CheckState.Unchecked)
        self.updateText()

    def updateText(self):
        checked = self.checkedItems()
        if checked:
            self._display_item.setText(",".join(checked))
        else:
            self._display_item.setText("None")
        self.setCurrentIndex(0)


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


class AppBox(QGroupBox):
    """The "App" box in Settings: version, Check for Updates, and Create Desktop Shortcut."""

    def __init__(self, main_window=None, parent=None):
        super().__init__("App", parent)
        self.main_window = main_window
        self._task = None
        self._progress = None
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(f"You are running Jebao Pump Sequencer <b>{__version__}</b>."))
        self.lbl_status = QLabel(
            "The app checks GitHub for a newer release each time it starts, or now with this button."
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
        self._progress = QProgressDialog("Starting…", None, 0, 0, self)
        self._progress.setWindowTitle(f"Updating to {release.version}")
        self._progress.setWindowModality(Qt.WindowModality.ApplicationModal)
        self._progress.setMinimumDuration(0)
        self._progress.show()
        self._start(lambda say: updater.update_to(release.tag, progress=say), self._updated)

    def _updated(self, result):
        if self._progress is not None:
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
            self._task.progress.connect(self._progress.setLabelText)
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
        self.showMaximized()

        self.config = ConfigManager()
        self.secrets = SecretsManager()

        self.cloud_token = None
        self.cloud_devices = []

        self.sequence_queue = []
        self.current_running_sequence = None
        self._motor_jobs = 0  # manual doses and calibration runs in progress
        self.head_runs = {}  # (did, ch) -> HeadRun for every head whose motor is running

        import asyncio

        self._ch_paused = {1: False, 2: False, 3: False, 4: False}
        self._ch_cancelled = {1: False, 2: False, 3: False, 4: False}
        self._ch_resume_events = {
            1: asyncio.Event(),
            2: asyncio.Event(),
            3: asyncio.Event(),
            4: asyncio.Event(),
        }
        self._ch_review_events = {1: None, 2: None, 3: None, 4: None}

        self.setup_ui()
        self._build_status_bar()

        logger.info("Jebao SPE GUI Initialized (Cloud Mode).")

        if self.secrets.data.get("username") and self.secrets.data.get("password"):
            QTimer.singleShot(100, lambda: self._start_task(self.login_and_fetch))

        QTimer.singleShot(200, lambda: self._start_task(self._queue_worker))

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
        banner_layout.setContentsMargins(12, 8, 12, 8)
        self.lbl_approval = QLabel()
        self.lbl_approval.setWordWrap(True)
        banner_layout.addWidget(self.lbl_approval, 1)
        self.btn_approve_channels = []
        for i in range(4):
            btn = QPushButton(f"Approve CH{i + 1}")
            btn.setEnabled(False)
            btn.setVisible(False)
            btn.setStyleSheet(APPROVE_STYLE)
            btn.clicked.connect(lambda checked, ch=i + 1: self.approve_channel(ch))
            self.btn_approve_channels.append(btn)
            banner_layout.addWidget(btn)
        self.btn_approve_all = QPushButton("Approve All")
        self.btn_approve_all.setEnabled(False)
        self.btn_approve_all.setStyleSheet(APPROVE_ALL_STYLE)
        self.btn_approve_all.clicked.connect(self.approve_all)
        banner_layout.addWidget(self.btn_approve_all)
        self.approval_banner.setVisible(False)
        main_layout.addWidget(self.approval_banner)

        # --- Tabs: Manual | Sequencer | Calibration | Rack ---
        self.tabs = QTabWidget()
        self.manual_tab = ManualTab(self)
        self.tabs.addTab(self.manual_tab, "Manual")
        self.tabs.addTab(self.build_sequencer_page(), "Sequencer")
        self.tabs.addTab(self.build_calibration_tab(), "Calibration")
        self.rack_tab = RackTab(self)
        self.tabs.addTab(self.rack_tab, "Rack")

        # 4. Bottom Right: System Log (kept on disk, see eventlog.py)
        log_group = QGroupBox("System Log")
        log_layout = QVBoxLayout()
        log_header = QHBoxLayout()
        keep = int(self.config.data.get("log_keep_days", eventlog.DEFAULT_KEEP_DAYS))
        self.lbl_log_info = QLabel(f"Saved on this computer for {keep} days (change in Settings).")
        self.lbl_log_info.setStyleSheet("color: grey;")
        log_header.addWidget(self.lbl_log_info)
        log_header.addStretch(1)
        btn_log_folder = QPushButton("Open Log Folder")
        btn_log_folder.clicked.connect(self.open_log_folder)
        log_header.addWidget(btn_log_folder)
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

        self.refresh_saved_sequences_ui()

        # Tiles and the dispense panel follow the motor runs a few times a second
        self.ui_timer = QTimer(self)
        self.ui_timer.timeout.connect(self.manual_tab.refresh)
        self.ui_timer.start(300)

        # Start Clock Timer
        self.clock_timer = QTimer(self)
        self.clock_timer.timeout.connect(self.update_clock)
        self.clock_timer.start(1000)
        self.update_clock()

    def open_log_folder(self):
        folder = eventlog.log_dir(DATA_DIR)
        os.makedirs(folder, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(folder))

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
        """Run once after the window opens: check for a newer release and keep the desktop shortcut current."""
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
        if self._motor_jobs:
            return (
                "A dose or calibration run is in progress. Wait for it to finish, or press Emergency "
                "Stop, before updating: restarting mid-dose can leave a pump running."
            )
        return ""

    def save_before_restart(self):
        if hasattr(self, "seq_table"):
            self.auto_save_sequence()
        self.config.save()
        self.secrets.save()

    def restart_app(self):
        logger.info("Restarting application...")
        if hasattr(self, "seq_table"):
            self.auto_save_sequence()
        subprocess.Popen([sys.executable] + sys.argv)  # noqa: S603 (restarts this same app)
        QApplication.quit()

    def approve_channel(self, ch):
        if self._ch_review_events.get(ch):
            self._ch_review_events[ch].set()
            self._ch_review_events[ch] = None
        self.refresh_approval_banner()

    def approve_all(self):
        for ch in range(1, 5):
            self.approve_channel(ch)

    def refresh_approval_banner(self):
        """Show the orange banner while any channel waits for review, with a button per waiting channel."""
        waiting = [ch for ch in range(1, 5) if self._ch_review_events.get(ch)]
        for ch in range(1, 5):
            btn = self.btn_approve_channels[ch - 1]
            btn.setEnabled(ch in waiting)
            btn.setVisible(ch in waiting)
        self.btn_approve_all.setEnabled(bool(waiting))
        self.btn_approve_all.setVisible(len(waiting) > 1)
        if waiting:
            chans = ", ".join(f"CH{ch}" for ch in waiting)
            seq = self.current_running_sequence
            where = f" in “{seq}”" if seq else ""
            self.lbl_approval.setText(
                f"<b>Waiting for your review:</b> {chans}{where}. Check it, then approve to continue."
            )
        self.approval_banner.setVisible(bool(waiting))

    def build_sequencer_page(self):
        """The Sequencer tab: saved sequences and the queue on the left, the step table on the right."""
        page = QSplitter(Qt.Orientation.Horizontal)
        seq_sidebar = QWidget()
        seq_sidebar_layout = QVBoxLayout(seq_sidebar)
        seq_sidebar_layout.setContentsMargins(0, 0, 0, 0)

        seq_sidebar_layout.addWidget(QLabel("<b>Saved Sequences</b>"))
        self.saved_seq_list = QListWidget()
        seq_sidebar_layout.addWidget(self.saved_seq_list)

        saved_btn_layout1 = QHBoxLayout()
        btn_save = QPushButton("💾 Save")
        btn_save.clicked.connect(self.save_sequence_dialog)
        btn_load = QPushButton("📂 Load")
        btn_load.clicked.connect(self.load_selected_sequence)
        btn_queue = QPushButton("▶ Queue")
        btn_queue.clicked.connect(self.queue_selected_sequence)

        saved_btn_layout1.addWidget(btn_save)
        saved_btn_layout1.addWidget(btn_load)
        saved_btn_layout1.addWidget(btn_queue)
        seq_sidebar_layout.addLayout(saved_btn_layout1)

        saved_btn_layout2 = QHBoxLayout()
        btn_rename = QPushButton("✏ Rename")
        btn_rename.clicked.connect(self.rename_selected_sequence)
        btn_delete = QPushButton("🗑 Delete")
        btn_delete.clicked.connect(self.delete_selected_sequence)

        saved_btn_layout2.addWidget(btn_rename)
        saved_btn_layout2.addWidget(btn_delete)
        seq_sidebar_layout.addLayout(saved_btn_layout2)

        seq_sidebar_layout.addWidget(QLabel("<b>Execution Queue</b>"))
        self.queue_list = QListWidget()
        self.queue_list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        seq_sidebar_layout.addWidget(self.queue_list)

        page.addWidget(seq_sidebar)
        page.addWidget(self.build_sequencer_tab())
        page.setSizes([260, 900])
        return page

    def build_calibration_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)

        info = QLabel(
            "<b>Calibration Procedure:</b><br>"
            "Because the MD-4.4 dosing pumps operate at a fixed physical speed, flow rate cannot be altered.<br>"
            "To ensure accurate volume dosing, you must calibrate the flow rate (mL/min) for each channel.<br>"
            "1. Select channels below.<br>2. Click 'Run Selected Channels for 60s' into graduated cylinders or scales.<br>"
            "3. Enter the measured volumes and tubing sizes, then click Save."
        )
        layout.addWidget(info)

        ctrl_layout = QHBoxLayout()
        ctrl_layout.addWidget(QLabel("Select Pump:"))
        self.cal_pump_combo = QComboBox()
        self.cal_pump_combo.currentIndexChanged.connect(self.update_cal_labels)
        ctrl_layout.addWidget(self.cal_pump_combo)
        ctrl_layout.addStretch()
        layout.addLayout(ctrl_layout)

        # --- Mass-to-Volume Density Selector ---
        density_layout = QHBoxLayout()
        density_layout.addWidget(QLabel("Input Mode:"))
        self.cal_input_mode = QComboBox()
        self.cal_input_mode.addItems(["Volume (mL)", "Mass (g)"])
        self.cal_input_mode.currentIndexChanged.connect(self.on_cal_mode_changed)
        density_layout.addWidget(self.cal_input_mode)

        self.lbl_liquid = QLabel("Liquid Type:")
        density_layout.addWidget(self.lbl_liquid)

        self.cal_liquid_type = QComboBox()
        self.cal_liquid_type.addItems(
            ["Water (1.000 g/mL)", "Ethanol (0.789 g/mL)", "Methanol (0.792 g/mL)", "Custom Density"]
        )
        self.cal_liquid_type.currentIndexChanged.connect(self.on_cal_mode_changed)
        density_layout.addWidget(self.cal_liquid_type)

        self.cal_custom_density = QDoubleSpinBox()
        self.cal_custom_density.setRange(0.001, 20.0)
        self.cal_custom_density.setDecimals(3)
        self.cal_custom_density.setValue(1.000)
        self.cal_custom_density.setSuffix(" g/mL")
        density_layout.addWidget(self.cal_custom_density)
        density_layout.addStretch()

        # Initially hide density controls since default is Volume
        self.lbl_liquid.setVisible(False)
        self.cal_liquid_type.setVisible(False)
        self.cal_custom_density.setVisible(False)
        layout.addLayout(density_layout)

        grid = QGridLayout()
        self.cal_checkboxes = []
        self.cal_vol_spins = []
        self.cal_id_spins = []
        self.cal_od_spins = []
        self.cal_rate_labels = []
        self.cal_meas_labels = []

        for i in range(4):
            ch_num = i + 1
            frame = QFrame()
            frame.setFrameShape(QFrame.Shape.StyledPanel)
            flayout = QVBoxLayout(frame)

            cb = QCheckBox(f"Channel {ch_num}")
            cb.setStyleSheet("font-weight: bold;")
            self.cal_checkboxes.append(cb)
            flayout.addWidget(cb)

            lbl_rate = QLabel("Rate: ? mL/min")
            lbl_rate.setStyleSheet("color: #E2E2E2; font-style: italic;")
            self.cal_rate_labels.append(lbl_rate)
            flayout.addWidget(lbl_rate)

            # Tubing inputs
            h1 = QHBoxLayout()
            h1.addWidget(QLabel("ID (mm):"))
            id_spin = QDoubleSpinBox()
            id_spin.setRange(0.1, 20.0)
            id_spin.setValue(2.0)
            self.cal_id_spins.append(id_spin)
            h1.addWidget(id_spin)
            flayout.addLayout(h1)

            h2 = QHBoxLayout()
            h2.addWidget(QLabel("OD (mm):"))
            od_spin = QDoubleSpinBox()
            od_spin.setRange(0.1, 20.0)
            od_spin.setValue(4.0)
            self.cal_od_spins.append(od_spin)
            h2.addWidget(od_spin)
            flayout.addLayout(h2)

            # Volume input
            h3 = QHBoxLayout()
            lbl_meas = QLabel("Meas Vol (mL):")
            self.cal_meas_labels.append(lbl_meas)
            h3.addWidget(lbl_meas)
            vol_spin = QDoubleSpinBox()
            vol_spin.setRange(0.1, 999.9)
            vol_spin.setValue(60.0)
            self.cal_vol_spins.append(vol_spin)
            h3.addWidget(vol_spin)
            flayout.addLayout(h3)

            grid.addWidget(frame, 0, i)

        layout.addLayout(grid)

        btn_layout = QHBoxLayout()
        self.btn_cal_run = QPushButton("Run Selected Channels for 60s")
        self.btn_cal_run.setStyleSheet("background-color: #28A745; color: white; font-weight: bold;")
        self.btn_cal_run.clicked.connect(self.run_calibration)
        btn_layout.addWidget(self.btn_cal_run)

        self.btn_cal_save = QPushButton("Save Calibration")
        self.btn_cal_save.clicked.connect(self.save_calibration)
        btn_layout.addWidget(self.btn_cal_save)

        layout.addLayout(btn_layout)
        layout.addStretch()
        return widget

    def on_cal_mode_changed(self):
        is_mass = self.cal_input_mode.currentText() == "Mass (g)"

        self.lbl_liquid.setVisible(is_mass)
        self.cal_liquid_type.setVisible(is_mass)

        is_custom = is_mass and ("Custom" in self.cal_liquid_type.currentText())
        self.cal_custom_density.setVisible(is_custom)

        label_text = "Meas Mass (g):" if is_mass else "Meas Vol (mL):"
        for lbl in self.cal_meas_labels:
            lbl.setText(label_text)

    def update_cal_labels(self):
        did = self.cal_pump_combo.currentData()
        if not did:
            for lbl in self.cal_rate_labels:
                lbl.setText("Rate: ? mL/min")
            return

        p = self.config.get_pump(did)
        for i in range(4):
            rate = p["calibration"][i]
            self.cal_rate_labels[i].setText(f"Rate: {rate:.3f} mL/min")
            self.cal_id_spins[i].setValue(p["tubing_id"][i])
            self.cal_od_spins[i].setValue(p["tubing_od"][i])

    def run_calibration(self):
        did = self.cal_pump_combo.currentData()
        if not did:
            return

        channels_to_run = [i for i, cb in enumerate(self.cal_checkboxes) if cb.isChecked()]
        if not channels_to_run:
            QMessageBox.warning(self, "No Channels", "Please select at least one channel to calibrate.")
            return

        self.btn_cal_run.setEnabled(False)
        asyncio.create_task(self._async_calibration_run(did, channels_to_run))

    async def _async_calibration_run(self, did, channels):
        cmds = {f"channe{ch + 1}": True for ch in channels}
        off_cmds = {f"channe{ch + 1}": False for ch in channels}
        ch_str = ", ".join([str(ch + 1) for ch in channels])
        self._motor_jobs += 1
        try:
            await self.send_cloud_command(did, cmds)
            logger.info(f"Calibration: Motors ON {ch_str}. Waiting 60 seconds...")
            runs = [self._begin_head_run(did, ch + 1, 60.0, "calibration") for ch in channels]
            try:
                end = time.time() + 60.0
                while time.time() < end and not any(r.cancelled for r in runs):
                    await asyncio.sleep(0.1)
                    for r in runs:
                        r.remaining = max(0.0, end - time.time())
            finally:
                for r in runs:
                    self._end_head_run(r)
            await self.send_cloud_command(did, off_cmds)
            if any(r.cancelled for r in runs):
                logger.warning("Calibration run stopped early. Motors OFF; don't use these volumes.")
            else:
                logger.info("Motors OFF. Please enter the measured volumes and click Save.")
        except Exception as e:
            logger.error(f"Calibration run failed: {e}")
        finally:
            self._motor_jobs -= 1
            self.btn_cal_run.setEnabled(True)

    def save_calibration(self):
        did = self.cal_pump_combo.currentData()
        if not did:
            return

        p = self.config.get_pump(did)
        saved_any = False

        is_mass = self.cal_input_mode.currentText() == "Mass (g)"
        density = 1.000
        if is_mass:
            liq = self.cal_liquid_type.currentText()
            if "Water" in liq:
                density = 1.000
            elif "Ethanol" in liq:
                density = 0.789
            elif "Methanol" in liq:
                density = 0.792
            else:
                density = self.cal_custom_density.value()

        for i, cb in enumerate(self.cal_checkboxes):
            if cb.isChecked():
                val = self.cal_vol_spins[i].value()
                if is_mass:
                    vol = val / density
                    logger.info(f"CH{i + 1}: Converted {val}g / {density:.3f}g/mL = {vol:.3f}mL")
                else:
                    vol = val

                rate = vol  # 60s run means vol == mL/min
                p["calibration"][i] = rate
                dates = (list(p.get("calibrated_at") or []) + [None] * 4)[:4]
                dates[i] = datetime.now().strftime("%Y-%m-%d")
                p["calibrated_at"] = dates
                p["tubing_id"][i] = self.cal_id_spins[i].value()
                p["tubing_od"][i] = self.cal_od_spins[i].value()
                saved_any = True
                logger.info(
                    f"Saved CH{i + 1}: Rate {rate:.3f} mL/min, ID {p['tubing_id'][i]}mm, OD {p['tubing_od'][i]}mm"
                )

        if saved_any:
            self.config.save()
            self.update_cal_labels()
            QMessageBox.information(self, "Saved", "Calibration saved for selected channels.")
        else:
            QMessageBox.warning(self, "No Channels", "Check the boxes for the channels you want to save.")

    def build_sequencer_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)

        self.seq_table = QTableWidget(0, 4)
        self.seq_table.setHorizontalHeaderLabels(["Pump", "Channel", "Action", "Parameters"])
        header = self.seq_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.seq_table.setColumnWidth(0, 250)
        self.seq_table.setColumnWidth(1, 80)
        self.seq_table.setColumnWidth(2, 120)
        self.seq_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        layout.addWidget(self.seq_table)

        btn_layout = QHBoxLayout()
        self.step_type_combo = QComboBox()
        self.step_type_combo.addItems(
            [
                "Dose",
                "Dose (Async)",
                "Staccato",
                "Staccato (Async)",
                "Pause",
                "Wait for All",
                "Run Sub-Sequence",
            ]
        )
        btn_add_step = QPushButton("+ Add Step")
        btn_add_step.clicked.connect(lambda: self.add_sequence_step(self.step_type_combo.currentText()))

        btn_rem_step = QPushButton("- Remove")
        btn_rem_step.clicked.connect(self.remove_sequence_step)

        btn_clear_all = QPushButton("🗑 Clear All")
        btn_clear_all.clicked.connect(self.clear_all_sequence_steps)

        btn_up = QPushButton("🔼 Up")
        btn_up.clicked.connect(self.move_sequence_step_up)
        btn_down = QPushButton("🔽 Down")
        btn_down.clicked.connect(self.move_sequence_step_down)

        btn_layout.addWidget(self.step_type_combo)
        btn_layout.addWidget(btn_add_step)
        btn_layout.addWidget(btn_rem_step)
        btn_layout.addWidget(btn_clear_all)
        btn_layout.addWidget(btn_up)
        btn_layout.addWidget(btn_down)
        layout.addLayout(btn_layout)

        # Save / Load Sequence
        io_layout = QHBoxLayout()
        btn_save_seq = QPushButton("Save Sequence As...")
        btn_save_seq.clicked.connect(self.save_sequence_dialog)
        btn_load_seq = QPushButton("Load Sequence...")
        btn_load_seq.clicked.connect(self.load_sequence_dialog)
        io_layout.addWidget(btn_save_seq)
        io_layout.addWidget(btn_load_seq)
        layout.addLayout(io_layout)

        bottom_layout = QHBoxLayout()
        self.btn_run_seq = QPushButton("▶ Queue Current Table")
        self.btn_run_seq.setStyleSheet(
            "background-color: #28A745; color: white; font-weight: bold; padding: 10px;"
        )
        self.btn_run_seq.clicked.connect(self.queue_current_table)

        bottom_layout.addWidget(self.btn_run_seq)
        layout.addLayout(bottom_layout)

        self.sequence_task = None
        return widget

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

    async def fetch_devices(self):
        if not self.cloud_token:
            return

        url = f"https://{DOMAIN}/app/bindings"
        headers = {"X-Gizwits-Application-Id": APP_ID, "X-Gizwits-User-token": self.cloud_token}
        try:
            resp = await asyncio.to_thread(requests.get, url, headers=headers, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200:
                self.cloud_devices = resp.json().get("devices", [])
                logger.info(f"Found {len(self.cloud_devices)} bound devices.")
                self.refresh_pumps()
            else:
                logger.error(f"Failed to fetch devices: {resp.text}")
        except Exception as e:
            logger.error(f"Fetch devices error: {e}")

    async def send_cloud_command(self, did, attrs):
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
        if hasattr(self, "seq_table") and self.seq_table.rowCount():
            self._apply_sequence_data(self._get_sequence_data())

    def refresh_pumps(self):
        """Show the current pump list and layout in every tab."""
        layout = self.rack_layout()
        dids = [d.get("did") for d in self.cloud_devices]
        order = rack.order(layout, dids)
        self.cloud_devices.sort(
            key=lambda d: order.index(d.get("did")) if d.get("did") in order else len(order)
        )

        if hasattr(self, "cal_pump_combo"):
            current = self.cal_pump_combo.currentData()
            self.cal_pump_combo.blockSignals(True)
            self.cal_pump_combo.clear()
            for d in self.cloud_devices:
                self.cal_pump_combo.addItem(self.get_device_name(d.get("did")), d.get("did"))
            idx = self.cal_pump_combo.findData(current)
            if idx >= 0:
                self.cal_pump_combo.setCurrentIndex(idx)
            self.cal_pump_combo.blockSignals(False)
            self.update_cal_labels()

        # Re-populate sequence table if it's empty
        if hasattr(self, "seq_table") and self.seq_table.rowCount() == 0:
            if self.config.data.get("sequence"):
                self._apply_sequence_data(self.config.data["sequence"])

        if hasattr(self, "manual_tab"):
            self.manual_tab.rebuild()
        if hasattr(self, "rack_tab"):
            self.rack_tab.rebuild()

    def get_device_name(self, did):
        own = self.config.display_name(did)
        if own:
            return own
        for d in self.cloud_devices:
            if d.get("did") == did:
                return d.get("dev_alias") or d.get("mac")
        return did

    def set_status_color(self, label, connected):
        if connected:
            label.setStyleSheet("color: #28A745; font-weight: bold; font-size: 14px;")
            label.setText("CONNECTED")
        else:
            label.setStyleSheet("color: #DC3545; font-weight: bold; font-size: 14px;")
            label.setText("DISCONNECTED")

    # --- Calibration Logic ---
    # --- Sequencer Control ---
    def add_sequence_step(self, action_type):
        row = self.seq_table.rowCount()
        self.seq_table.insertRow(row)

        # Pump Dropdown
        pump_cb = QComboBox()
        for d in self.cloud_devices:
            pump_cb.addItem(self.get_device_name(d.get("did")), userData=d.get("did"))
        self.seq_table.setCellWidget(row, 0, pump_cb)
        # Channel Dropdown
        ch_cb = CheckableComboBox()
        ch_cb.addItems(["1", "2", "3", "4"])
        ch_cb.setCheckedItems(["1"])  # Default to channel 1 checked
        self.seq_table.setCellWidget(row, 1, ch_cb)

        # Action Dropdown
        act_cb = QComboBox()
        act_cb.addItems(
            [
                "Dose",
                "Dose (Async)",
                "Staccato",
                "Staccato (Async)",
                "Pause",
                "Pause (User Review)",
                "Wait for All",
                "Run Sub-Sequence",
            ]
        )
        act_cb.setCurrentText(action_type)
        act_cb.currentTextChanged.connect(self.on_action_type_changed)
        self.seq_table.setCellWidget(row, 2, act_cb)

        # Parameters Widget
        val_widget = QStackedWidget()

        spin_widget = QWidget()
        spin_layout = QHBoxLayout(spin_widget)
        spin_layout.setContentsMargins(0, 0, 0, 0)

        spin1 = QDoubleSpinBox()
        spin1.setDecimals(1)
        spin1.setMaximum(10000.0)
        spin1.setToolTip("Total Volume (mL) / Pause Time (s)")

        spin2 = QDoubleSpinBox()
        spin2.setDecimals(1)
        spin2.setMaximum(1000.0)
        spin2.setToolTip("Burst Volume (mL)")

        spin3 = QDoubleSpinBox()
        spin3.setDecimals(1)
        spin3.setMaximum(3600.0)
        spin3.setToolTip("Wait Time Between Bursts (s)")

        spin_layout.addWidget(spin1)
        spin_layout.addWidget(spin2)
        spin_layout.addWidget(spin3)

        sub_seq_cb = QComboBox()
        sub_seq_cb.setToolTip("Select Saved Sequence")

        val_widget.addWidget(spin_widget)
        val_widget.addWidget(sub_seq_cb)

        val_widget.spins = [spin1, spin2, spin3]
        val_widget.sub_seq_cb = sub_seq_cb

        self.seq_table.setCellWidget(row, 3, val_widget)

        self._update_row_ui(row, action_type)
        self.auto_save_sequence()

    def _update_row_ui(self, row, action_type):
        pump_cb = self.seq_table.cellWidget(row, 0)
        val_widget = self.seq_table.cellWidget(row, 3)
        if not hasattr(val_widget, "spins"):
            return
        spin1, spin2, spin3 = val_widget.spins
        sub_seq_cb = val_widget.sub_seq_cb

        if action_type in ["Pause", "Pause (User Review)", "Wait for All", "Run Sub-Sequence"]:
            pump_cb.setEnabled(False)
            blank_style = "QComboBox { color: transparent; background: transparent; border: none; } \nQComboBox::drop-down { border: none; }"
            pump_cb.setStyleSheet(blank_style)
        else:
            pump_cb.setEnabled(True)
            pump_cb.setStyleSheet("")

        if action_type == "Run Sub-Sequence":
            val_widget.setCurrentIndex(1)
            if sub_seq_cb is not None:
                sub_seq_cb.clear()
                saved = list(self.config.data.get("saved_sequences", {}).keys())
                sub_seq_cb.addItems(saved)
        else:
            val_widget.setCurrentIndex(0)
            if action_type in ["Wait for All", "Pause (User Review)"]:
                spin1.hide()
                spin2.hide()
                spin3.hide()
            elif action_type == "Pause":
                spin1.show()
                spin1.setToolTip("Duration (sec)")
                spin1.setPrefix("Wait: ")
                spin1.setSuffix(" s")
                spin2.hide()
                spin3.hide()
            elif "Staccato" in action_type:
                spin1.show()
                spin1.setToolTip("Total Volume (mL)")
                spin1.setPrefix("")
                spin1.setSuffix(" mL")
                spin2.show()
                spin3.show()
            else:  # Dose, Dose (Async)
                spin1.show()
                spin1.setToolTip("Total Volume (mL)")
                spin1.setPrefix("")
                spin1.setSuffix(" mL")
                spin2.hide()
                spin3.hide()
        if "Dose" in action_type:
            spin1.setToolTip("Volume (mL)")
            spin1.setPrefix("Vol: ")
            spin1.setSuffix(" mL")
            spin2.hide()
            spin3.hide()
        elif action_type == "Pause":
            spin1.setToolTip("Duration (sec)")
            spin1.setPrefix("Wait: ")
            spin1.setSuffix(" s")
            spin2.hide()
            spin3.hide()
        elif "Staccato" in action_type:
            spin1.setToolTip("Total Volume (mL)")
            spin1.setPrefix("Total: ")
            spin1.setSuffix(" mL")
            spin2.setToolTip("Burst Volume (mL)")
            spin2.setPrefix("Burst: ")
            spin2.setSuffix(" mL")
            spin3.setToolTip("Wait Time (sec)")
            spin3.setPrefix("Wait: ")
            spin3.setSuffix(" s")
        elif action_type == "Wait for All":
            spin1.hide()
            spin2.hide()
            spin3.hide()

    def on_action_type_changed(self, action_type):
        sender = self.sender()
        if not sender:
            return
        for r in range(self.seq_table.rowCount()):
            if self.seq_table.cellWidget(r, 2) == sender:
                self._update_row_ui(r, action_type)
                self.auto_save_sequence()
                break

    def remove_sequence_step(self):
        row = self.seq_table.currentRow()
        if row >= 0:
            self.seq_table.removeRow(row)
            self.auto_save_sequence()

    def clear_all_sequence_steps(self):
        reply = QMessageBox.question(
            self,
            "Clear All Steps",
            "Are you sure you want to delete all steps in the current sequence?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.seq_table.setRowCount(0)
            self.auto_save_sequence()

    def move_sequence_step_up(self):
        row = self.seq_table.currentRow()
        if row > 0:
            seq = self._get_sequence_data()
            seq[row], seq[row - 1] = seq[row - 1], seq[row]
            self._apply_sequence_data(seq)
            self.seq_table.selectRow(row - 1)

    def move_sequence_step_down(self):
        row = self.seq_table.currentRow()
        if row >= 0 and row < self.seq_table.rowCount() - 1:
            seq = self._get_sequence_data()
            seq[row], seq[row + 1] = seq[row + 1], seq[row]
            self._apply_sequence_data(seq)
            self.seq_table.selectRow(row + 1)

    async def _queue_worker(self):
        while True:
            if not self.sequence_queue:
                self.current_running_sequence = None
                await asyncio.sleep(0.5)
                continue

            seq_item = self.sequence_queue.pop(0)
            seq_name = seq_item["name"]
            seq_data = seq_item["data"]
            self.current_running_sequence = seq_name
            self.refresh_queue_ui()

            logger.info(f"=== STARTING SEQUENCE: {seq_name} ===")
            try:
                self.sequence_task = asyncio.create_task(self._async_run_sequence(seq_name, seq_data))
                await self.sequence_task
                logger.info(f"=== SEQUENCE '{seq_name}' COMPLETED SUCCESSFULLY ===")
            except asyncio.CancelledError:
                logger.warning(f"=== SEQUENCE '{seq_name}' ABORTED ===")
            except Exception as e:
                logger.error(f"Sequence aborted due to error: {e}")
            finally:
                self.current_running_sequence = None
                self.refresh_queue_ui()

    def queue_current_table(self):
        seq_data = self._get_sequence_data()
        if not seq_data:
            logger.warning("Cannot queue empty sequence.")
            return

        name = f"Ad-hoc Sequence ({len(self.sequence_queue) + 1})"
        self.sequence_queue.append({"name": name, "data": seq_data})
        self.refresh_queue_ui()
        logger.info(f"Added '{name}' to queue.")

    def queue_selected_sequence(self):
        item = self.saved_seq_list.currentItem()
        if not item:
            return
        name = item.text()
        seq_data = self.config.data.get("saved_sequences", {}).get(name)
        if seq_data:
            self.sequence_queue.append({"name": name, "data": seq_data})
            self.refresh_queue_ui()
            logger.info(f"Added '{name}' to queue.")

    def load_selected_sequence(self):
        item = self.saved_seq_list.currentItem()
        if not item:
            return
        name = item.text()
        seq_data = self.config.data.get("saved_sequences", {}).get(name)
        if seq_data:
            self._apply_sequence_data(seq_data)
            logger.info(f"Loaded sequence '{name}' into table.")

    def rename_selected_sequence(self):
        item = self.saved_seq_list.currentItem()
        if not item:
            return
        old_name = item.text()

        new_name, ok = QInputDialog.getText(self, "Rename Sequence", "New Sequence Name:", text=old_name)
        if ok and new_name and new_name != old_name:
            if new_name in self.config.data.get("saved_sequences", {}):
                QMessageBox.warning(self, "Error", "A sequence with this name already exists!")
                return

            seq_data = self.config.data["saved_sequences"].pop(old_name)
            self.config.data["saved_sequences"][new_name] = seq_data
            self.config.save()
            self.refresh_saved_sequences_ui()
            logger.info(f"Renamed sequence '{old_name}' to '{new_name}'")

    def delete_selected_sequence(self):
        item = self.saved_seq_list.currentItem()
        if not item:
            return
        name = item.text()

        reply = QMessageBox.question(
            self,
            "Confirm Delete",
            f"Are you sure you want to delete '{name}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.config.data["saved_sequences"].pop(name, None)
            self.config.save()
            self.refresh_saved_sequences_ui()
            logger.info(f"Deleted sequence '{name}'")

    def refresh_saved_sequences_ui(self):
        # Refresh the sidebar
        self.saved_seq_list.clear()
        saved = self.config.data.get("saved_sequences", {})
        for name in saved.keys():
            self.saved_seq_list.addItem(name)

        # Refresh any open Sub-Sequence dropdowns in the table
        for r in range(self.seq_table.rowCount()):
            act_cb = self.seq_table.cellWidget(r, 2)
            if act_cb and act_cb.currentText() == "Run Sub-Sequence":
                val_widget = self.seq_table.cellWidget(r, 3)
                if val_widget and hasattr(val_widget, "sub_seq_cb"):
                    sub_seq_cb = val_widget.sub_seq_cb
                    if sub_seq_cb is not None:
                        current = sub_seq_cb.currentText()
                        sub_seq_cb.clear()
                        sub_seq_cb.addItems(list(saved.keys()))
                        sub_seq_cb.setToolTip("Select Saved Sequence")
                        if current in saved:
                            sub_seq_cb.setCurrentText(current)

    def refresh_queue_ui(self):
        self.queue_list.clear()
        if self.current_running_sequence:
            item = QListWidgetItem(f"▶ {self.current_running_sequence}")
            item.setForeground(QColor("#28A745"))
            self.queue_list.addItem(item)

        for i, q in enumerate(self.sequence_queue):
            self.queue_list.addItem(f"{i + 1}. {q['name']}")

    async def _async_run_sequence(self, seq_name, seq_data, call_stack=None):
        logger.info("=== STARTING INDEPENDENT MULTI-CHANNEL SEQUENCE ===")
        sync_barriers = {}
        self._ch_running_tasks = {1: [], 2: [], 3: [], 4: []}

        tasks = []
        for ch in [1, 2, 3, 4]:
            task = asyncio.create_task(
                self._run_channel_sequence(ch, seq_name, seq_data, set(), sync_barriers)
            )
            tasks.append(task)

        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            for task in pending:
                task.cancel()

            for task in done:
                exc = task.exception()
                if exc:
                    raise exc

            all_running = []
            for ch in [1, 2, 3, 4]:
                all_running.extend(self._ch_running_tasks[ch])

            if all_running:
                logger.info(
                    "Sequence ended, but async tasks are still running. Waiting for them to finish..."
                )
                await asyncio.gather(*all_running, return_exceptions=True)

            logger.info("=== SPE SEQUENCE COMPLETED SUCCESSFULLY ===")
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            logger.warning("SEQUENCE CANCELLED VIA EMERGENCY STOP.")
        except Exception as e:
            for task in tasks:
                task.cancel()
            logger.error(f"Sequence aborted due to error: {e}")
        finally:
            for ch in range(1, 5):
                if self._ch_review_events.get(ch):
                    self._ch_review_events[ch].set()
                    self._ch_review_events[ch] = None
            self.refresh_approval_banner()

    async def _run_channel_sequence(self, ch, seq_name, seq_data, call_stack, sync_barriers):
        if seq_name in call_stack:
            logger.error(
                f"[CH{ch}] Infinite Recursion Detected! Sub-sequence '{seq_name}' is already running. Aborting this step."
            )
            return

        current_stack = call_stack | {seq_name}
        running_tasks = self._ch_running_tasks[ch]

        for r, step in enumerate(seq_data):
            channels = step.get("channel", [1])
            if isinstance(channels, int) or isinstance(channels, str):
                channels = [int(channels)]

            if ch not in channels:
                continue

            action = step["action"]
            val1 = step.get("val1", 0)
            val2 = step.get("val2", 0)
            val3 = step.get("val3", 0)

            pump_did = step.get("pump_did")
            pump_name = self.get_device_name(pump_did) if pump_did else "Unknown Pump"

            if action == "Run Sub-Sequence":
                sub_name = step.get("val1", "")
                sub_data = self.config.data.get("saved_sequences", {}).get(sub_name)
                if not sub_data:
                    logger.error(f"[CH{ch} Step {r + 1}] FAILED! Sub-sequence '{sub_name}' not found.")
                    continue
                logger.info(f"[CH{ch} Step {r + 1}] -> Entering Sub-Sequence: '{sub_name}'")
                await self._run_channel_sequence(ch, sub_name, sub_data, current_stack, sync_barriers)
                logger.info(f"[CH{ch}] <- Returned from Sub-Sequence: '{sub_name}'")

            elif action == "Pause":
                logger.info(f"[CH{ch} Step {r + 1}] Pausing for {val1} seconds...")
                await self._interruptible_sleep_timer(ch, val1)

            elif action == "Pause (User Review)":
                logger.info(f"[CH{ch} Step {r + 1}] Paused for User Review.")

                event = asyncio.Event()
                self._ch_review_events[ch] = event

                self.refresh_approval_banner()

                await event.wait()
                logger.info(f"[CH{ch} Step {r + 1}] Resumed from User Review.")

            elif action == "Wait for All":
                if running_tasks:
                    logger.info(
                        f"[CH{ch} Step {r + 1}] Waiting for {len(running_tasks)} background tasks to finish..."
                    )
                    await asyncio.gather(*running_tasks, return_exceptions=True)
                    running_tasks.clear()

                barrier_key = f"{seq_name}_{r}"
                if barrier_key not in sync_barriers:
                    sync_barriers[barrier_key] = asyncio.Barrier(len(channels))

                logger.info(f"[CH{ch} Step {r + 1}] Reached Sync Point. Waiting for other channels...")
                try:
                    await sync_barriers[barrier_key].wait()
                except asyncio.BrokenBarrierError:
                    pass
                logger.info(f"[CH{ch} Step {r + 1}] Sync Point complete. Resuming.")

            elif "Dose" in action:
                if "Async" in action:
                    task = asyncio.create_task(self._do_dose(r, pump_did, pump_name, [ch], val1))
                    running_tasks.append(task)
                else:
                    await self._do_dose(r, pump_did, pump_name, [ch], val1)

            elif "Staccato" in action:
                if "Async" in action:
                    task = asyncio.create_task(
                        self._do_staccato(r, pump_did, pump_name, [ch], val1, val2, val3)
                    )
                    running_tasks.append(task)
                else:
                    await self._do_staccato(r, pump_did, pump_name, [ch], val1, val2, val3)

    async def _interruptible_sleep_timer(self, ch, seconds):
        duration_left = seconds
        self._ch_cancelled[ch] = False
        while duration_left > 0:
            if self._ch_cancelled.get(ch, False):
                raise asyncio.CancelledError(f"Channel {ch} sleep cancelled.")

            if self._ch_paused.get(ch, False):
                await self._ch_resume_events[ch].wait()
                if self._ch_cancelled.get(ch, False):
                    raise asyncio.CancelledError(f"Channel {ch} sleep cancelled while paused.")

            chunk = min(0.1, duration_left)
            start_time = time.time()
            await asyncio.sleep(chunk)
            elapsed = time.time() - start_time
            duration_left -= elapsed

    def _begin_head_run(self, did, ch, seconds, source):
        run = HeadRun(did=did, ch=ch, duration=float(seconds), remaining=float(seconds), source=source)
        self.head_runs[run.key] = run
        return run

    def _end_head_run(self, run):
        if self.head_runs.get(run.key) is run:
            del self.head_runs[run.key]

    def head_label(self, did, ch):
        """'JP 1: EtOH · CH2 (EtOH → Col 2)': pump name, channel and, if set, the head's own name."""
        name = self.config.head_name(did, ch)
        text = f"{self.get_device_name(did)} · CH{ch}"
        return f"{text} ({name})" if name else text

    async def _interruptible_motor_run(self, ch, pump_did, seconds, source="sequence"):
        """Keep a head's motor on for ``seconds``, honouring pause and cancel.

        A sequence's channel can be paused or cancelled as a whole (``_ch_paused`` /
        ``_ch_cancelled``); any single head can also be paused or cancelled from the
        Manual tab (its ``HeadRun``).
        """
        channel_key = f"channe{ch}"
        by_channel = source == "sequence"
        if by_channel:
            self._ch_cancelled[ch] = False
        run = self._begin_head_run(pump_did, ch, seconds, source)
        try:
            while run.remaining > 0:
                if run.cancelled or (by_channel and self._ch_cancelled.get(ch, False)):
                    await self.send_cloud_command(pump_did, {channel_key: False})
                    raise asyncio.CancelledError(f"Channel {ch} dose cancelled.")

                if run.paused or (by_channel and self._ch_paused.get(ch, False)):
                    await self.send_cloud_command(pump_did, {channel_key: False})
                    logger.info(f"[{self.head_label(pump_did, ch)}] Paused mid-dose. Waiting to resume...")
                    if run.paused:
                        await run.resume.wait()
                    else:
                        await self._ch_resume_events[ch].wait()
                    if run.cancelled or (by_channel and self._ch_cancelled.get(ch, False)):
                        raise asyncio.CancelledError(f"Channel {ch} dose cancelled while paused.")
                    logger.info(f"[{self.head_label(pump_did, ch)}] Resuming ({run.remaining:.1f} s left).")
                    await self.send_cloud_command(pump_did, {channel_key: True})

                chunk = min(0.1, run.remaining)
                start_time = time.time()
                await asyncio.sleep(chunk)
                run.remaining -= time.time() - start_time
        finally:
            self._end_head_run(run)

    # --- Manual dispensing on several heads at once --------------------------------------

    def run_state(self, did, ch):
        """(state, progress 0-1, seconds left) for a head, or None when its motor is off."""
        run = self.head_runs.get((did, ch))
        if run is None:
            return None
        paused = run.paused or (run.source == "sequence" and self._ch_paused.get(ch, False))
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
        return tasks

    async def _manual_pump_dispense(self, did, items, value, unit):
        """Turn on the given channels of one pump together, then stop each after its own time."""
        self._motor_jobs += 1
        try:
            what = f"{value:g} mL" if unit == "volume" else f"{value:g} s"
            ok = await self.send_cloud_command(did, {f"channe{ch}": True for ch, _ in items})
            if not ok:
                logger.error(f"Couldn't start {self.get_device_name(did)}; nothing was dispensed there.")
                return
            for ch, seconds in items:
                logger.info(f"Dispensing {what} on {self.head_label(did, ch)}: motor on for {seconds:.1f} s.")
            await asyncio.gather(*(self._manual_head(did, ch, seconds) for ch, seconds in items))
        finally:
            self._motor_jobs -= 1

    async def _manual_head(self, did, ch, seconds):
        label = self.head_label(did, ch)
        try:
            await self._interruptible_motor_run(ch, did, seconds, source="manual")
        except asyncio.CancelledError:
            logger.warning(f"{label}: dispensing cancelled.")
            return
        if await self.send_cloud_command(did, {f"channe{ch}": False}):
            logger.info(f"{label}: done.")
        else:
            logger.error(f"{label}: the STOP command failed. Stop it by hand or with Emergency Stop.")

    def pause_heads(self, heads):
        for key in heads:
            run = self.head_runs.get(tuple(key))
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

    async def _do_dose(self, r, pump_did, pump_name, channels, val1):
        if not pump_did:
            logger.error(f"[Step {r + 1}] FAILED! Invalid pump selection.")
            return

        if isinstance(channels, int) or isinstance(channels, str):
            channels = [int(channels)]

        async def run_channel(ch):
            rate = self.config.get_pump(pump_did)["calibration"][ch - 1]
            seconds = (val1 / rate) * 60.0

            logger.info(
                f"[Step {r + 1}] Dosing {val1}mL on {pump_name} CH{ch} (Running motor for {seconds:.1f}s)"
            )

            channel_key = f"channe{ch}"
            await self.send_cloud_command(pump_did, {channel_key: True})
            await self._interruptible_motor_run(ch, pump_did, seconds)
            await self.send_cloud_command(pump_did, {channel_key: False})

        tasks = [run_channel(ch) for ch in channels]
        await asyncio.gather(*tasks)

    async def _do_staccato(self, r, pump_did, pump_name, channels, val1, val2, val3):
        if not pump_did:
            logger.error(f"[Step {r + 1}] FAILED! Invalid pump selection.")
            return

        if isinstance(channels, int) or isinstance(channels, str):
            channels = [int(channels)]

        async def run_channel(ch):
            rate = self.config.get_pump(pump_did)["calibration"][ch - 1]
            total_vol = val1
            burst_vol = val2
            wait_sec = val3

            burst_seconds = (burst_vol / rate) * 60.0
            bursts_needed = int(total_vol // burst_vol)
            remainder_vol = total_vol % burst_vol
            remainder_seconds = (remainder_vol / rate) * 60.0

            logger.info(
                f"[Step {r + 1}] Staccato Dosing {total_vol}mL on {pump_name} CH{ch}. Burst: {burst_vol}mL ({burst_seconds:.1f}s), Wait: {wait_sec}s"
            )
            channel_key = f"channe{ch}"

            for b in range(bursts_needed):
                logger.info(f"  -> [Step {r + 1}] CH{ch} Burst {b + 1}/{bursts_needed}")
                await self.send_cloud_command(pump_did, {channel_key: True})
                await self._interruptible_motor_run(ch, pump_did, burst_seconds)
                await self.send_cloud_command(pump_did, {channel_key: False})
                if b < bursts_needed - 1 or remainder_vol > 0:
                    await self._interruptible_sleep_timer(ch, wait_sec)

            if remainder_vol > 0.01:
                logger.info(
                    f"  -> [Step {r + 1}] CH{ch} Final Burst: {remainder_vol:.1f}mL ({remainder_seconds:.1f}s)"
                )
                await self.send_cloud_command(pump_did, {channel_key: True})
                await self._interruptible_motor_run(ch, pump_did, remainder_seconds)
                await self.send_cloud_command(pump_did, {channel_key: False})

        tasks = [run_channel(ch) for ch in channels]
        await asyncio.gather(*tasks)

    def emergency_stop_all(self):
        logger.info("!!! GLOBAL EMERGENCY STOP INITIATED !!!")
        if self.sequence_queue:
            logger.warning(f"Cleared {len(self.sequence_queue)} pending sequences from the queue.")
            self.sequence_queue.clear()
            self.refresh_queue_ui()

        if self.sequence_task and not self.sequence_task.done():
            self.sequence_task.cancel()
        self.cancel_heads(list(self.head_runs))
        asyncio.create_task(self._async_stop_all())

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
        if failed:
            logger.error(
                f"STOP FAILED for {len(failed)} of {len(dids)} pump(s): {', '.join(failed)}. "
                "Stop them by hand or with the phone app, or press Emergency Stop again."
            )
        else:
            logger.info(f"Sent STOP command to all channels on all {len(dids)} pump(s).")

    # --- Sequence Persistence ---
    def _get_sequence_data(self):
        seq = []
        for r in range(self.seq_table.rowCount()):
            pump_cb = self.seq_table.cellWidget(r, 0)
            ch_cb = self.seq_table.cellWidget(r, 1)
            action = self.seq_table.cellWidget(r, 2).currentText()
            val_widget = self.seq_table.cellWidget(r, 3)
            if not hasattr(val_widget, "spins"):
                continue

            spins = val_widget.spins
            sub_seq_cb = val_widget.sub_seq_cb

            if action == "Run Sub-Sequence":
                val1 = sub_seq_cb.currentText() if sub_seq_cb is not None else ""
                val2 = 0
                val3 = 0
            else:
                val1 = spins[0].value() if spins else 0
                val2 = spins[1].value() if len(spins) > 1 else 0
                val3 = spins[2].value() if len(spins) > 2 else 0

            # channel handling
            channels = [1]
            if hasattr(ch_cb, "checkedItems"):
                checked = ch_cb.checkedItems()
                if checked:
                    channels = [int(c) for c in checked if c.isdigit()]
            else:
                try:
                    channels = [int(ch_cb.currentText())]
                except (AttributeError, TypeError, ValueError) as e:
                    logger.warning(f"Step {r + 1}: couldn't read the channel ({e}); using channel 1.")

            seq.append(
                {
                    "pump_did": pump_cb.currentData(),
                    "channel": channels,
                    "action": action,
                    "val1": val1,
                    "val2": val2,
                    "val3": val3,
                }
            )
        return seq

    def auto_save_sequence(self):
        self.config.data["sequence"] = self._get_sequence_data()
        self.config.save()

    def save_sequence_dialog(self):
        seqs = list(self.config.data.get("saved_sequences", {}).keys())
        name, ok = QInputDialog.getItem(
            self, "Save Sequence", "Sequence Name (select or type new):", seqs, 0, True
        )
        if ok and name:
            if name in self.config.data.get("saved_sequences", {}):
                reply = QMessageBox.question(
                    self,
                    "Overwrite",
                    f"'{name}' already exists. Overwrite?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
                if reply == QMessageBox.StandardButton.No:
                    return

            if "saved_sequences" not in self.config.data:
                self.config.data["saved_sequences"] = {}

            self.config.data["saved_sequences"][name] = self._get_sequence_data()
            self.config.save()
            self.refresh_saved_sequences_ui()
            QMessageBox.information(self, "Saved", f"Sequence '{name}' saved successfully!")

    def load_sequence_dialog(self):
        seqs = list(self.config.data.get("saved_sequences", {}).keys())
        if not seqs:
            QMessageBox.information(self, "No Sequences", "No saved sequences found.")
            return

        name, ok = QInputDialog.getItem(self, "Load Sequence", "Select Sequence:", seqs, 0, False)
        if ok and name:
            self._apply_sequence_data(self.config.data["saved_sequences"][name])

    def _apply_sequence_data(self, seq_data):
        self.seq_table.blockSignals(True)
        self.seq_table.setRowCount(0)
        for s in seq_data:
            self.add_sequence_step(s["action"])
            r = self.seq_table.rowCount() - 1

            # Set pump
            pump_cb = self.seq_table.cellWidget(r, 0)
            idx = pump_cb.findData(s.get("pump_did"))
            if idx >= 0:
                pump_cb.setCurrentIndex(idx)

            # Set channel
            ch_cb = self.seq_table.cellWidget(r, 1)
            ch_val = s.get("channel", 1)
            if hasattr(ch_cb, "setCheckedItems"):
                if isinstance(ch_val, list):
                    ch_cb.setCheckedItems([str(c) for c in ch_val])
                else:
                    ch_cb.setCheckedItems([str(ch_val)])
            else:
                ch_cb.setCurrentText(str(ch_val) if not isinstance(ch_val, list) else str(ch_val[0]))

            # Set values
            val_widget = self.seq_table.cellWidget(r, 3)
            if not hasattr(val_widget, "spins"):
                continue

            spins = val_widget.spins
            sub_seq_cb = val_widget.sub_seq_cb

            if s["action"] == "Run Sub-Sequence" and sub_seq_cb is not None:
                sub_seq_cb.setCurrentText(str(s.get("val1", "")))
            elif spins and len(spins) >= 3:
                try:
                    spins[0].setValue(float(s.get("val1", 0.0)))
                    spins[1].setValue(float(s.get("val2", 0.0)))
                    spins[2].setValue(float(s.get("val3", 0.0)))
                except Exception as e:
                    logger.error(f"Error loading values: {e}")
        self.seq_table.blockSignals(False)
        self.auto_save_sequence()


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
    window.show()
    QTimer.singleShot(1500, window.start_background_tasks)

    with loop:
        loop.run_forever()


if __name__ == "__main__":
    main()
