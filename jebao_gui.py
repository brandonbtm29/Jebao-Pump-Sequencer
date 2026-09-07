import sys
import os
import json
import asyncio
import logging
import traceback
import subprocess
import requests
import time
from datetime import datetime

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QLabel, QLineEdit, QPushButton, QTextEdit,
    QGroupBox, QMessageBox, QFrame, QDoubleSpinBox, QListWidget,
    QListWidgetItem, QInputDialog, QSplitter, QTabWidget, QComboBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
    QDialog, QDialogButtonBox, QFormLayout, QStackedWidget, QCheckBox
)
from PyQt6.QtCore import Qt, QTimer, QEvent
from PyQt6.QtGui import QColor, QPalette, QStandardItemModel, QStandardItem

import qasync

def log_uncaught_exceptions(ex_cls, ex, tb):
    with open("crash_log.txt", "w") as f:
        traceback.print_exception(ex_cls, ex, tb, file=f)
    sys.__excepthook__(ex_cls, ex, tb)

sys.excepthook = log_uncaught_exceptions

logger = logging.getLogger("JebaoGUI")
logger.setLevel(logging.DEBUG)

class GUILogHandler(logging.Handler):
    def __init__(self, text_widget):
        super().__init__()
        self.text_widget = text_widget

    def emit(self, record):
        msg = self.format(record)
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.call_soon_threadsafe(self.append_text, msg)
        else:
            self.append_text(msg)
            
    def append_text(self, msg):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.text_widget.append(f"[{timestamp}] {msg}")

# --- Cloud API Configuration ---
APP_ID = "c3703c4888ec4736a3a0d9425c321604"
DOMAIN = "usapi.gizwits.com"

class SecretsManager:
    def __init__(self, filepath="secrets.json"):
        self.filepath = filepath
        self.data = {"username": "", "password": ""}
        self.load()

    def load(self):
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, 'r') as f:
                    self.data = json.load(f)
            except Exception as e:
                logger.error(f"Failed to load secrets: {e}")

    def save(self):
        try:
            with open(self.filepath, 'w') as f:
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
    def __init__(self, filepath="jebao_config.json"):
        self.filepath = filepath
        self.data = {"pumps": {}, "sequence": [], "saved_sequences": {}}
        self.load()

    def load(self):
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, 'r') as f:
                    data = json.load(f)
                    if isinstance(data.get("pumps"), list):
                        # Migrate old array config to dict config or just wipe it
                        self.data = {"pumps": {}}
                    else:
                        self.data = data
            except Exception as e:
                self.data = {"pumps": {}, "sequence": [], "saved_sequences": {}}
                
            self.data.setdefault("sequence", [])
            self.data.setdefault("saved_sequences", {})

    def save(self):
        try:
            with open(self.filepath, 'w') as f:
                json.dump(self.data, f, indent=4)
        except Exception as e:
            logger.error(f"Failed to save config: {e}")

    def get_pump(self, did):
        if did not in self.data["pumps"]:
            self.data["pumps"][did] = {
                "volumes": [10.0, 10.0, 10.0, 10.0],
                "units": ["Vol (mL)", "Vol (mL)", "Vol (mL)", "Vol (mL)"],
                "calibration": [1.0, 1.0, 1.0, 1.0],
                "tubing_id": [2.0, 2.0, 2.0, 2.0],
                "tubing_od": [4.0, 4.0, 4.0, 4.0]
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
    if text_size == "Small": font.setPointSize(9)
    elif text_size == "Large": font.setPointSize(14)
    else: font.setPointSize(11) # Medium
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

class SettingsDialog(QDialog):
    def __init__(self, secrets_manager, config_manager, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumSize(400, 300)
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
        
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)
        
    def accept(self):
        self.secrets.data["username"] = self.user_input.text().strip()
        self.secrets.data["password"] = self.pass_input.text()
        self.secrets.save()
        
        self.config.data["theme"] = self.theme_combo.currentText()
        self.config.data["text_size"] = self.size_combo.currentText()
        self.config.save()
        
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
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Jebao Multi-Pump SPE Sequencer (Cloud Edition)")
        self.setMinimumSize(1100, 800)
        self.showMaximized()
        
        self.config = ConfigManager()
        self.secrets = SecretsManager()
        
        self.cloud_token = None
        self.cloud_devices = []
        self.current_pump_did = None
        
        self.sequence_queue = []
        self.current_running_sequence = None
        
        import asyncio
        self._ch_paused = {1: False, 2: False, 3: False, 4: False}
        self._ch_cancelled = {1: False, 2: False, 3: False, 4: False}
        self._ch_resume_events = {1: asyncio.Event(), 2: asyncio.Event(), 3: asyncio.Event(), 4: asyncio.Event()}
        self._ch_review_events = {1: None, 2: None, 3: None, 4: None}
        
        self.setup_ui()
        
        logger.info("Jebao SPE GUI Initialized (Cloud Mode).")
        
        if self.secrets.data.get("username") and self.secrets.data.get("password"):
            QTimer.singleShot(100, lambda: asyncio.create_task(self.login_and_fetch()))
            
        QTimer.singleShot(200, lambda: asyncio.create_task(self._queue_worker()))

    def setup_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)

        # --- HEADER (Clock) ---
        self.lbl_clock = QLabel()
        self.lbl_clock.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_clock.setStyleSheet("font-size: 16px; font-weight: bold; padding: 5px;")
        main_layout.addWidget(self.lbl_clock)

        # --- TOP SPLITTER (Queue & Main UI) ---
        top_splitter = QSplitter(Qt.Orientation.Horizontal)
        
        # 1. Left Sidebar: Sequence Manager & Queue
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
        
        # Pending Approvals Section
        seq_sidebar_layout.addWidget(QLabel("<b>Pending Approvals</b>"))
        
        appr_grid = QGridLayout()
        self.btn_approve_channels = []
        for i in range(4):
            btn = QPushButton(f"Approve CH {i+1}")
            btn.setEnabled(False)
            btn.setStyleSheet("background-color: #6C757D; color: white;")
            btn.clicked.connect(lambda checked, ch=i+1: self.approve_channel(ch))
            self.btn_approve_channels.append(btn)
            appr_grid.addWidget(btn, i//2, i%2)
            
        seq_sidebar_layout.addLayout(appr_grid)
        
        self.btn_approve_all = QPushButton("Approve All")
        self.btn_approve_all.setEnabled(False)
        self.btn_approve_all.setStyleSheet("background-color: #6C757D; color: white;")
        self.btn_approve_all.clicked.connect(self.approve_all)
        seq_sidebar_layout.addWidget(self.btn_approve_all)
        
        top_splitter.addWidget(seq_sidebar)
        
        # 2. Center Panel: Tabs
        self.center_panel = QWidget()
        center_layout = QVBoxLayout(self.center_panel)
        center_layout.setContentsMargins(0, 0, 0, 0)
        
        self.lbl_active_pump = QLabel("<h3>Select a Pump Below</h3>")
        self.lbl_active_pump.setAlignment(Qt.AlignmentFlag.AlignCenter)
        center_layout.addWidget(self.lbl_active_pump)

        self.tabs = QTabWidget()
        self.tabs.addTab(self.build_manual_tab(), "🎮 Manual Control")
        self.tabs.addTab(self.build_calibration_tab(), "⚖ Calibration")
        self.tabs.addTab(self.build_sequencer_tab(), "⏱ Sequencer")
        center_layout.addWidget(self.tabs)
        
        top_splitter.addWidget(self.center_panel)
        top_splitter.setSizes([250, 850])
        main_layout.addWidget(top_splitter, 3)
        
        # --- BOTTOM SPLITTER (Pumps & Event Log) ---
        bottom_splitter = QSplitter(Qt.Orientation.Horizontal)
        
        # 3. Bottom Left: Pump List & Settings
        bottom_left = QWidget()
        bl_layout = QVBoxLayout(bottom_left)
        bl_layout.setContentsMargins(0, 0, 0, 0)
        
        bl_header = QHBoxLayout()
        bl_header.addWidget(QLabel("<b>Connected Pumps</b>"))
        
        self.status_indicator = QLabel("DISCONNECTED")
        self.status_indicator.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.set_status_color(self.status_indicator, False)
        bl_header.addWidget(self.status_indicator)
        
        bl_layout.addLayout(bl_header)
        
        self.pump_list = QListWidget()
        self.pump_list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.pump_list.model().rowsMoved.connect(self.save_pump_order)
        self.pump_list.currentItemChanged.connect(self.on_pump_selected)
        bl_layout.addWidget(self.pump_list)
        
        # Cloud settings buttons horizontal row
        cloud_btn_layout = QHBoxLayout()
        self.btn_settings = QPushButton("⚙ Settings")
        self.btn_settings.clicked.connect(self.open_settings)
        cloud_btn_layout.addWidget(self.btn_settings)
        
        self.btn_refresh = QPushButton("Refresh")
        self.btn_refresh.clicked.connect(lambda: asyncio.create_task(self.login_and_fetch()))
        cloud_btn_layout.addWidget(self.btn_refresh)
        
        self.btn_restart = QPushButton("🔄 Restart")
        self.btn_restart.clicked.connect(self.restart_app)
        cloud_btn_layout.addWidget(self.btn_restart)
        bl_layout.addLayout(cloud_btn_layout)
        
        bottom_splitter.addWidget(bottom_left)
        
        # 4. Bottom Right: Event Log
        log_group = QGroupBox("Live Event Log")
        log_layout = QVBoxLayout()
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setStyleSheet("background-color: #1E1E1E; color: #D4D4D4; font-family: Consolas;")
        log_layout.addWidget(self.log_text)
        log_group.setLayout(log_layout)
        bottom_splitter.addWidget(log_group)
        
        bottom_splitter.setSizes([250, 850])
        main_layout.addWidget(bottom_splitter, 1)
        
        # Emergency Stop spanning all
        self.btn_estop = QPushButton("🚨 EMERGENCY STOP ALL PUMPS 🚨")
        self.btn_estop.setStyleSheet("background-color: darkred; color: white; font-weight: bold; font-size: 16px; padding: 15px;")
        self.btn_estop.clicked.connect(self.emergency_stop_all)
        main_layout.addWidget(self.btn_estop)
        
        gui_handler = GUILogHandler(self.log_text)
        gui_handler.setFormatter(logging.Formatter('%(message)s'))
        logger.addHandler(gui_handler)
        
        self.refresh_saved_sequences_ui()

        # Start Clock Timer
        self.clock_timer = QTimer(self)
        self.clock_timer.timeout.connect(self.update_clock)
        self.clock_timer.start(1000)
        self.update_clock()

    def update_clock(self):
        now = datetime.now().strftime("%A, %B %d, %Y | %I:%M:%S %p")
        self.lbl_clock.setText(now)

    def open_settings(self):
        dlg = SettingsDialog(self.secrets, self.config, self)
        if dlg.exec():
            asyncio.create_task(self.login_and_fetch())

    def restart_app(self):
        logger.info("Restarting application...")
        if hasattr(self, 'seq_table'):
            self.auto_save_sequence()
        subprocess.Popen([sys.executable] + sys.argv)
        QApplication.quit()

    def toggle_channel_pause(self, ch, btn):
        if self._ch_paused[ch]:
            # It is currently paused, we want to resume
            self._ch_paused[ch] = False
            self._ch_resume_events[ch].set()
            btn.setText("Pause")
            btn.setStyleSheet("background-color: #FFA500; color: black; padding: 5px;")
        else:
            # It is currently running, we want to pause
            self._ch_paused[ch] = True
            self._ch_resume_events[ch].clear()
            btn.setText("Resume")
            btn.setStyleSheet("background-color: #28A745; color: white; padding: 5px;")

    def cancel_channel_dose(self, ch):
        self._ch_cancelled[ch] = True
        # If it was paused, resume it so the loop can process the cancellation
        if self._ch_paused[ch]:
            self._ch_paused[ch] = False
            self._ch_resume_events[ch].set()
            # Note: The Pause button UI won't reset automatically here, but that's a minor visual detail.
        
        if self.current_pump_did:
            asyncio.create_task(self.send_cloud_command(self.current_pump_did, {f"channe{ch}": False}))
        logger.info(f"[CH{ch}] Cancellation requested by user.")

    def approve_channel(self, ch):
        if self._ch_review_events.get(ch):
            self._ch_review_events[ch].set()
            self._ch_review_events[ch] = None
            self.btn_approve_channels[ch-1].setEnabled(False)
            self.btn_approve_channels[ch-1].setStyleSheet("background-color: #6C757D; color: white;")
        
        # Check if any other channels are still waiting
        if not any(self._ch_review_events.values()):
            self.btn_approve_all.setEnabled(False)
            self.btn_approve_all.setStyleSheet("background-color: #6C757D; color: white;")

    def approve_all(self):
        for ch in range(1, 5):
            self.approve_channel(ch)

    def build_manual_tab(self):
        widget = QWidget()
        layout = QGridLayout(widget)
        self.channel_spins = []
        self.channel_units = []
        
        for i in range(4):
            ch_num = i + 1
            frame = QFrame()
            frame.setFrameShape(QFrame.Shape.StyledPanel)
            flayout = QVBoxLayout(frame)
            header = QLabel(f"Channel {ch_num}")
            header.setAlignment(Qt.AlignmentFlag.AlignCenter)
            header.setStyleSheet("font-weight: bold;")
            flayout.addWidget(header)
            
            vol_layout = QHBoxLayout()
            unit_combo = QComboBox()
            unit_combo.addItems(["Vol (mL)", "Time (s)"])
            unit_combo.currentIndexChanged.connect(self.save_current_pump_state)
            self.channel_units.append(unit_combo)
            vol_layout.addWidget(unit_combo)
            
            spin = QDoubleSpinBox()
            spin.setRange(0.1, 9999.9)
            spin.setValue(10.0)
            spin.valueChanged.connect(self.save_current_pump_state)
            self.channel_spins.append(spin)
            vol_layout.addWidget(spin)
            flayout.addLayout(vol_layout)
            
            btn_dose = QPushButton(f"Dose CH {ch_num}")
            btn_dose.setStyleSheet("background-color: #0078D7; color: white; padding: 5px;")
            btn_dose.clicked.connect(lambda checked, ch=ch_num, s=spin, u=unit_combo: self.trigger_dose(self.current_pump_did, ch, s.value(), u.currentText()))
            flayout.addWidget(btn_dose)
            
            btn_pause = QPushButton("Pause")
            btn_pause.setStyleSheet("background-color: #FFA500; color: black; padding: 5px;")
            btn_pause.clicked.connect(lambda checked, ch=ch_num, btn=btn_pause: self.toggle_channel_pause(ch, btn))
            flayout.addWidget(btn_pause)
            
            btn_cancel = QPushButton("Cancel Dose")
            btn_cancel.setStyleSheet("background-color: #DC3545; color: white; padding: 5px;")
            btn_cancel.clicked.connect(lambda checked, ch=ch_num: self.cancel_channel_dose(ch))
            flayout.addWidget(btn_cancel)
            
            row = i // 2
            col = i % 2
            layout.addWidget(frame, row, col)
            
        return widget

    def build_calibration_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        
        info = QLabel("<b>Calibration Procedure:</b><br>"
                      "Because the MD-4.4 dosing pumps operate at a fixed physical speed, flow rate cannot be altered.<br>"
                      "To ensure accurate volume dosing, you must calibrate the flow rate (mL/min) for each channel.<br>"
                      "1. Select channels below.<br>2. Click 'Run Selected Channels for 60s' into graduated cylinders or scales.<br>"
                      "3. Enter the measured volumes and tubing sizes, then click Save.")
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
        self.cal_liquid_type.addItems(["Water (1.000 g/mL)", "Ethanol (0.789 g/mL)", "Methanol (0.792 g/mL)", "Custom Density"])
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
        is_mass = (self.cal_input_mode.currentText() == "Mass (g)")
        
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
            for lbl in self.cal_rate_labels: lbl.setText("Rate: ? mL/min")
            return
            
        p = self.config.get_pump(did)
        for i in range(4):
            rate = p["calibration"][i]
            self.cal_rate_labels[i].setText(f"Rate: {rate:.3f} mL/min")
            self.cal_id_spins[i].setValue(p["tubing_id"][i])
            self.cal_od_spins[i].setValue(p["tubing_od"][i])

    def run_calibration(self):
        did = self.cal_pump_combo.currentData()
        if not did: return
        
        channels_to_run = [i for i, cb in enumerate(self.cal_checkboxes) if cb.isChecked()]
        if not channels_to_run:
            QMessageBox.warning(self, "No Channels", "Please select at least one channel to calibrate.")
            return
            
        self.btn_cal_run.setEnabled(False)
        asyncio.create_task(self._async_calibration_run(did, channels_to_run))
        
    async def _async_calibration_run(self, did, channels):
        cmds = {f"channe{ch+1}": True for ch in channels}
        off_cmds = {f"channe{ch+1}": False for ch in channels}
        ch_str = ", ".join([str(ch+1) for ch in channels])
        try:
            await self.send_cloud_command(did, cmds)
            logger.info(f"Calibration: Motors ON {ch_str}. Waiting 60 seconds...")
            await asyncio.sleep(60.0)
            await self.send_cloud_command(did, off_cmds)
            logger.info("Motors OFF. Please enter the measured volumes and click Save.")
        except Exception as e:
            logger.error(f"Calibration run failed: {e}")
        finally:
            self.btn_cal_run.setEnabled(True)

    def save_calibration(self):
        did = self.cal_pump_combo.currentData()
        if not did: return
        
        p = self.config.get_pump(did)
        saved_any = False
        
        is_mass = (self.cal_input_mode.currentText() == "Mass (g)")
        density = 1.000
        if is_mass:
            liq = self.cal_liquid_type.currentText()
            if "Water" in liq: density = 1.000
            elif "Ethanol" in liq: density = 0.789
            elif "Methanol" in liq: density = 0.792
            else: density = self.cal_custom_density.value()
        
        for i, cb in enumerate(self.cal_checkboxes):
            if cb.isChecked():
                val = self.cal_vol_spins[i].value()
                if is_mass:
                    vol = val / density
                    logger.info(f"CH{i+1}: Converted {val}g / {density:.3f}g/mL = {vol:.3f}mL")
                else:
                    vol = val
                    
                rate = vol  # 60s run means vol == mL/min
                p["calibration"][i] = rate
                p["tubing_id"][i] = self.cal_id_spins[i].value()
                p["tubing_od"][i] = self.cal_od_spins[i].value()
                saved_any = True
                logger.info(f"Saved CH{i+1}: Rate {rate:.3f} mL/min, ID {p['tubing_id'][i]}mm, OD {p['tubing_od'][i]}mm")
                
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
        self.step_type_combo.addItems(["Dose", "Dose (Async)", "Staccato", "Staccato (Async)", "Pause", "Wait for All", "Run Sub-Sequence"])
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
        self.btn_run_seq.setStyleSheet("background-color: #28A745; color: white; font-weight: bold; padding: 10px;")
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
            resp = await asyncio.to_thread(requests.post, url, headers=headers, json=data)
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
        if not self.cloud_token: return
        
        url = f"https://{DOMAIN}/app/bindings"
        headers = {"X-Gizwits-Application-Id": APP_ID, "X-Gizwits-User-token": self.cloud_token}
        try:
            resp = await asyncio.to_thread(requests.get, url, headers=headers)
            if resp.status_code == 200:
                self.cloud_devices = resp.json().get("devices", [])
                logger.info(f"Found {len(self.cloud_devices)} bound devices.")
                self.populate_sidebar()
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
            "Content-Type": "application/json"
        }
        data = {"attrs": attrs}
        try:
            resp = await asyncio.to_thread(requests.post, url, headers=headers, json=data)
            if resp.status_code == 200:
                return True
            else:
                logger.error(f"Control command failed: {resp.status_code} {resp.text}")
                return False
        except Exception as e:
            logger.error(f"Send command error: {e}")
            return False

    # --- Pump Management ---
    def populate_sidebar(self):
        self.pump_list.blockSignals(True)
        self.pump_list.clear()
        
        if hasattr(self, 'cal_pump_combo'):
            self.cal_pump_combo.blockSignals(True)
            self.cal_pump_combo.clear()
            
        pump_order = self.config.data.get("pump_order", [])
        def sort_key(d):
            did = d.get("did")
            if did in pump_order:
                return pump_order.index(did)
            return 9999
            
        self.cloud_devices.sort(key=sort_key)
        
        for d in self.cloud_devices:
            name = d.get("dev_alias") or d.get("mac")
            did = d.get("did")
            is_online = d.get("is_online", False)
            status = " (Online)" if is_online else " (Offline)"
            
            item = QListWidgetItem(f"{name}{status}")
            item.setData(Qt.ItemDataRole.UserRole, did)
            self.pump_list.addItem(item)
            
            if hasattr(self, 'cal_pump_combo'):
                self.cal_pump_combo.addItem(name, did)
            
        self.pump_list.blockSignals(False)
        if hasattr(self, 'cal_pump_combo'):
            self.cal_pump_combo.blockSignals(False)
            self.update_cal_labels()
            
        # Re-populate sequence table if it's empty
        if hasattr(self, 'seq_table') and self.seq_table.rowCount() == 0:
            if self.config.data.get("sequence"):
                self._apply_sequence_data(self.config.data["sequence"])

    def get_device_name(self, did):
        for d in self.cloud_devices:
            if d.get("did") == did:
                return d.get("dev_alias") or d.get("mac")
        return did

    def on_pump_selected(self, current, previous):
        if not current: return
        self.current_pump_did = current.data(Qt.ItemDataRole.UserRole)
        name = self.get_device_name(self.current_pump_did)
        
        self.lbl_active_pump.setText(f"<h3>{name}</h3>")
        self.update_manual_labels()
        
        if hasattr(self, 'cal_pump_combo'):
            idx = self.cal_pump_combo.findData(self.current_pump_did)
            if idx >= 0:
                self.cal_pump_combo.setCurrentIndex(idx)

    def save_pump_order(self, parent, start, end, destination, row):
        order = []
        for i in range(self.pump_list.count()):
            order.append(self.pump_list.item(i).data(Qt.ItemDataRole.UserRole))
        self.config.data["pump_order"] = order
        self.config.save()

    def update_manual_labels(self):
        if not self.current_pump_did: return
        pump_data = self.config.get_pump(self.current_pump_did)
        
        for spin in self.channel_spins: spin.blockSignals(True)
        for combo in self.channel_units: combo.blockSignals(True)
        
        for i, spin in enumerate(self.channel_spins):
            if i < len(pump_data["volumes"]): 
                spin.setValue(pump_data["volumes"][i])
            if i < len(pump_data.get("units", [])):
                idx = self.channel_units[i].findText(pump_data["units"][i])
                if idx >= 0:
                    self.channel_units[i].setCurrentIndex(idx)
                    
        for combo in self.channel_units: combo.blockSignals(False)
        for spin in self.channel_spins: spin.blockSignals(False)
        
        self.update_cal_labels()

    def save_current_pump_state(self):
        if not self.current_pump_did: return
        pump_data = self.config.get_pump(self.current_pump_did)
        pump_data["volumes"] = [spin.value() for spin in self.channel_spins]
        pump_data["units"] = [combo.currentText() for combo in self.channel_units]
        self.config.save()

    def set_status_color(self, label, connected):
        if connected:
            label.setStyleSheet("color: #28A745; font-weight: bold; font-size: 14px;")
            label.setText("CONNECTED")
        else:
            label.setStyleSheet("color: #DC3545; font-weight: bold; font-size: 14px;")
            label.setText("DISCONNECTED")

    # --- Calibration Logic ---
    # --- Manual & Sequencer Control ---
    def trigger_dose(self, did, channel, value, unit="Vol (mL)"):
        if not did: return
        asyncio.create_task(self._async_dose(did, channel, value, unit))

    async def _async_dose(self, did, channel, value, unit):
        try:
            if unit == "Time (s)":
                seconds = value
                logger.info(f"Dosing for {seconds:.1f}s on CH{channel}...")
            else:
                rate = self.config.get_pump(did)["calibration"][channel-1]
                seconds = (value / rate) * 60.0
                logger.info(f"Dosing {value}mL on CH{channel}. Motor running for {seconds:.1f}s...")
                
            channel_key = f"channe{channel}"
            
            await self.send_cloud_command(did, {channel_key: True})
            await self._interruptible_motor_run(channel, did, seconds)
            await self.send_cloud_command(did, {channel_key: False})
            
            logger.info(f"Dose complete.")
        except asyncio.CancelledError:
            logger.warning(f"Manual Dose on CH{channel} was cancelled.")
        except Exception as e:
            logger.error(f"Dose error: {e}")

    def add_sequence_step(self, action_type):
        row = self.seq_table.rowCount()
        self.seq_table.insertRow(row)
        
        # Pump Dropdown
        pump_cb = QComboBox()
        for d in self.cloud_devices:
            name = d.get("dev_alias") or d.get("mac")
            pump_cb.addItem(name, userData=d.get("did"))
        self.seq_table.setCellWidget(row, 0, pump_cb)
        # Channel Dropdown
        ch_cb = CheckableComboBox()
        ch_cb.addItems(["1", "2", "3", "4"])
        ch_cb.setCheckedItems(["1"]) # Default to channel 1 checked
        self.seq_table.setCellWidget(row, 1, ch_cb)
        
        # Action Dropdown
        act_cb = QComboBox()
        act_cb.addItems(["Dose", "Dose (Async)", "Staccato", "Staccato (Async)", "Pause", "Pause (User Review)", "Wait for All", "Run Sub-Sequence"])
        act_cb.setCurrentText(action_type)
        act_cb.currentTextChanged.connect(self.on_action_type_changed)
        self.seq_table.setCellWidget(row, 2, act_cb)
        
        # Parameters Widget
        val_widget = QStackedWidget()
        
        spin_widget = QWidget()
        spin_layout = QHBoxLayout(spin_widget)
        spin_layout.setContentsMargins(0,0,0,0)
        
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
        ch_cb = self.seq_table.cellWidget(row, 1)
        val_widget = self.seq_table.cellWidget(row, 3)
        if not hasattr(val_widget, 'spins'): return
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
            else: # Dose, Dose (Async)
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
        if not sender: return
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
        reply = QMessageBox.question(self, "Clear All Steps", "Are you sure you want to delete all steps in the current sequence?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            self.seq_table.setRowCount(0)
            self.auto_save_sequence()

    def move_sequence_step_up(self):
        row = self.seq_table.currentRow()
        if row > 0:
            seq = self._get_sequence_data()
            seq[row], seq[row-1] = seq[row-1], seq[row]
            self._apply_sequence_data(seq)
            self.seq_table.selectRow(row-1)

    def move_sequence_step_down(self):
        row = self.seq_table.currentRow()
        if row >= 0 and row < self.seq_table.rowCount() - 1:
            seq = self._get_sequence_data()
            seq[row], seq[row+1] = seq[row+1], seq[row]
            self._apply_sequence_data(seq)
            self.seq_table.selectRow(row+1)

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
        if not item: return
        name = item.text()
        seq_data = self.config.data.get("saved_sequences", {}).get(name)
        if seq_data:
            self.sequence_queue.append({"name": name, "data": seq_data})
            self.refresh_queue_ui()
            logger.info(f"Added '{name}' to queue.")

    def load_selected_sequence(self):
        item = self.saved_seq_list.currentItem()
        if not item: return
        name = item.text()
        seq_data = self.config.data.get("saved_sequences", {}).get(name)
        if seq_data:
            self._apply_sequence_data(seq_data)
            logger.info(f"Loaded sequence '{name}' into table.")
            
    def rename_selected_sequence(self):
        item = self.saved_seq_list.currentItem()
        if not item: return
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
        if not item: return
        name = item.text()
        
        reply = QMessageBox.question(self, "Confirm Delete", f"Are you sure you want to delete '{name}'?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
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
                if val_widget and hasattr(val_widget, 'sub_seq_cb'):
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
            self.queue_list.addItem(f"{i+1}. {q['name']}")

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
                if exc: raise exc
                
            all_running = []
            for ch in [1, 2, 3, 4]:
                all_running.extend(self._ch_running_tasks[ch])
                
            if all_running:
                logger.info("Sequence ended, but async tasks are still running. Waiting for them to finish...")
                await asyncio.gather(*all_running, return_exceptions=True)
                        
            logger.info("=== SPE SEQUENCE COMPLETED SUCCESSFULLY ===")
        except asyncio.CancelledError:
            for task in tasks: task.cancel()
            logger.warning("SEQUENCE CANCELLED VIA EMERGENCY STOP.")
        except Exception as e:
            for task in tasks: task.cancel()
            logger.error(f"Sequence aborted due to error: {e}")
        finally:
            for ch in range(1, 5):
                if self._ch_review_events.get(ch):
                    self._ch_review_events[ch].set()
                    self._ch_review_events[ch] = None
                self.btn_approve_channels[ch-1].setEnabled(False)
                self.btn_approve_channels[ch-1].setStyleSheet("background-color: #6C757D; color: white;")
            self.btn_approve_all.setEnabled(False)
            self.btn_approve_all.setStyleSheet("background-color: #6C757D; color: white;")

    async def _run_channel_sequence(self, ch, seq_name, seq_data, call_stack, sync_barriers):
        if seq_name in call_stack:
            logger.error(f"[CH{ch}] Infinite Recursion Detected! Sub-sequence '{seq_name}' is already running. Aborting this step.")
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
                    logger.error(f"[CH{ch} Step {r+1}] FAILED! Sub-sequence '{sub_name}' not found.")
                    continue
                logger.info(f"[CH{ch} Step {r+1}] -> Entering Sub-Sequence: '{sub_name}'")
                await self._run_channel_sequence(ch, sub_name, sub_data, current_stack, sync_barriers)
                logger.info(f"[CH{ch}] <- Returned from Sub-Sequence: '{sub_name}'")
                
            elif action == "Pause":
                logger.info(f"[CH{ch} Step {r+1}] Pausing for {val1} seconds...")
                await self._interruptible_sleep_timer(ch, val1)
                
            elif action == "Pause (User Review)":
                logger.info(f"[CH{ch} Step {r+1}] Paused for User Review.")
                
                event = asyncio.Event()
                self._ch_review_events[ch] = event
                
                # Enable the UI buttons
                self.btn_approve_channels[ch-1].setEnabled(True)
                self.btn_approve_channels[ch-1].setStyleSheet("background-color: #28A745; color: white;")
                self.btn_approve_all.setEnabled(True)
                self.btn_approve_all.setStyleSheet("background-color: #0078D7; color: white;")
                
                await event.wait()
                logger.info(f"[CH{ch} Step {r+1}] Resumed from User Review.")
                
            elif action == "Wait for All":
                if running_tasks:
                    logger.info(f"[CH{ch} Step {r+1}] Waiting for {len(running_tasks)} background tasks to finish...")
                    await asyncio.gather(*running_tasks, return_exceptions=True)
                    running_tasks.clear()
                    
                barrier_key = f"{seq_name}_{r}"
                if barrier_key not in sync_barriers:
                    sync_barriers[barrier_key] = asyncio.Barrier(len(channels))
                
                logger.info(f"[CH{ch} Step {r+1}] Reached Sync Point. Waiting for other channels...")
                try:
                    await sync_barriers[barrier_key].wait()
                except asyncio.BrokenBarrierError:
                    pass
                logger.info(f"[CH{ch} Step {r+1}] Sync Point complete. Resuming.")
                
            elif "Dose" in action:
                if "Async" in action:
                    task = asyncio.create_task(self._do_dose(r, pump_did, pump_name, [ch], val1))
                    running_tasks.append(task)
                else:
                    await self._do_dose(r, pump_did, pump_name, [ch], val1)
                    
            elif "Staccato" in action:
                if "Async" in action:
                    task = asyncio.create_task(self._do_staccato(r, pump_did, pump_name, [ch], val1, val2, val3))
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

    async def _interruptible_motor_run(self, ch, pump_did, seconds):
        channel_key = f"channe{ch}"
        duration_left = seconds
        self._ch_cancelled[ch] = False
        
        while duration_left > 0:
            if self._ch_cancelled.get(ch, False):
                await self.send_cloud_command(pump_did, {channel_key: False})
                raise asyncio.CancelledError(f"Channel {ch} dose cancelled.")
                
            if self._ch_paused.get(ch, False):
                await self.send_cloud_command(pump_did, {channel_key: False})
                logger.info(f"[CH{ch}] Paused mid-dose! Waiting to resume...")
                await self._ch_resume_events[ch].wait()
                
                if self._ch_cancelled.get(ch, False):
                    raise asyncio.CancelledError(f"Channel {ch} dose cancelled while paused.")
                    
                logger.info(f"[CH{ch}] Resuming mid-dose! ({duration_left:.1f}s remaining)")
                await self.send_cloud_command(pump_did, {channel_key: True})
            
            chunk = min(0.1, duration_left)
            start_time = time.time()
            await asyncio.sleep(chunk)
            elapsed = time.time() - start_time
            duration_left -= elapsed

    async def _do_dose(self, r, pump_did, pump_name, channels, val1):
        if not pump_did:
            logger.error(f"[Step {r+1}] FAILED! Invalid pump selection.")
            return
            
        if isinstance(channels, int) or isinstance(channels, str):
            channels = [int(channels)]
            
        async def run_channel(ch):
            rate = self.config.get_pump(pump_did)["calibration"][ch-1]
            seconds = (val1 / rate) * 60.0
            
            logger.info(f"[Step {r+1}] Dosing {val1}mL on {pump_name} CH{ch} (Running motor for {seconds:.1f}s)")
            
            channel_key = f"channe{ch}"
            await self.send_cloud_command(pump_did, {channel_key: True})
            await self._interruptible_motor_run(ch, pump_did, seconds)
            await self.send_cloud_command(pump_did, {channel_key: False})
            
        tasks = [run_channel(ch) for ch in channels]
        await asyncio.gather(*tasks)

    async def _do_staccato(self, r, pump_did, pump_name, channels, val1, val2, val3):
        if not pump_did:
            logger.error(f"[Step {r+1}] FAILED! Invalid pump selection.")
            return
            
        if isinstance(channels, int) or isinstance(channels, str):
            channels = [int(channels)]
            
        async def run_channel(ch):
            rate = self.config.get_pump(pump_did)["calibration"][ch-1]
            total_vol = val1
            burst_vol = val2
            wait_sec = val3
            
            burst_seconds = (burst_vol / rate) * 60.0
            bursts_needed = int(total_vol // burst_vol)
            remainder_vol = total_vol % burst_vol
            remainder_seconds = (remainder_vol / rate) * 60.0
            
            logger.info(f"[Step {r+1}] Staccato Dosing {total_vol}mL on {pump_name} CH{ch}. Burst: {burst_vol}mL ({burst_seconds:.1f}s), Wait: {wait_sec}s")
            channel_key = f"channe{ch}"
            
            for b in range(bursts_needed):
                logger.info(f"  -> [Step {r+1}] CH{ch} Burst {b+1}/{bursts_needed}")
                await self.send_cloud_command(pump_did, {channel_key: True})
                await self._interruptible_motor_run(ch, pump_did, burst_seconds)
                await self.send_cloud_command(pump_did, {channel_key: False})
                if b < bursts_needed - 1 or remainder_vol > 0:
                    await self._interruptible_sleep_timer(ch, wait_sec)
                    
            if remainder_vol > 0.01:
                logger.info(f"  -> [Step {r+1}] CH{ch} Final Burst: {remainder_vol:.1f}mL ({remainder_seconds:.1f}s)")
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
        asyncio.create_task(self._async_stop_all())

    async def _async_stop_all(self):
        tasks = []
        # Stop all channels on all cloud devices
        attrs = {"channe1": False, "channe2": False, "channe3": False, "channe4": False}
        for d in self.cloud_devices:
            did = d.get("did")
            tasks.append(self.send_cloud_command(did, attrs))
        
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("Sent STOP command to all channels across all pumps.")

    # --- Sequence Persistence ---
    def _get_sequence_data(self):
        seq = []
        for r in range(self.seq_table.rowCount()):
            pump_cb = self.seq_table.cellWidget(r, 0)
            ch_cb = self.seq_table.cellWidget(r, 1)
            action = self.seq_table.cellWidget(r, 2).currentText()
            val_widget = self.seq_table.cellWidget(r, 3)
            if not hasattr(val_widget, 'spins'): continue
            
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
            if hasattr(ch_cb, 'checkedItems'):
                checked = ch_cb.checkedItems()
                if checked:
                    channels = [int(c) for c in checked if c.isdigit()]
            else:
                try: channels = [int(ch_cb.currentText())]
                except: pass
                
            seq.append({
                "pump_did": pump_cb.currentData(),
                "channel": channels,
                "action": action,
                "val1": val1,
                "val2": val2,
                "val3": val3
            })
        return seq

    def auto_save_sequence(self):
        self.config.data["sequence"] = self._get_sequence_data()
        self.config.save()

    def save_sequence_dialog(self):
        seqs = list(self.config.data.get("saved_sequences", {}).keys())
        name, ok = QInputDialog.getItem(self, "Save Sequence", "Sequence Name (select or type new):", seqs, 0, True)
        if ok and name:
            if name in self.config.data.get("saved_sequences", {}):
                reply = QMessageBox.question(self, "Overwrite", f"'{name}' already exists. Overwrite?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
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
            if idx >= 0: pump_cb.setCurrentIndex(idx)
            
            # Set channel
            ch_cb = self.seq_table.cellWidget(r, 1)
            ch_val = s.get("channel", 1)
            if hasattr(ch_cb, 'setCheckedItems'):
                if isinstance(ch_val, list):
                    ch_cb.setCheckedItems([str(c) for c in ch_val])
                else:
                    ch_cb.setCheckedItems([str(ch_val)])
            else:
                ch_cb.setCurrentText(str(ch_val) if not isinstance(ch_val, list) else str(ch_val[0]))
            
            # Set values
            val_widget = self.seq_table.cellWidget(r, 3)
            if not hasattr(val_widget, 'spins'): continue
            
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

def main():
    app = QApplication(sys.argv)
    
    # Load config to apply theme early
    config = ConfigManager()
    apply_global_theme(app, config)
    
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)
    
    window = JebaoPumpGUI()
    window.show()
    
    with loop:
        loop.run_forever()

if __name__ == "__main__":
    main()
