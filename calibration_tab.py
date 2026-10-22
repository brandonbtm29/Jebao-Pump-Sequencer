"""The Calibration tab: pick heads on the rack, then calibrate them with a balance
(Automatic) or by running them for a set time and entering what came out (Manual).

The tab talks to the main window (``host``), which provides, besides what the Manual tab
uses (see rack_widgets): ``set_head(did, ch, on)`` (coroutine), ``save_head_rate(did,
ch, rate, how)``, ``head_label(did, ch)``, ``head_runs``, ``_motor_jobs``,
``_start_task(fn)`` and ``data_dir``.
"""

import asyncio
import logging
import os
import time
from datetime import datetime

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QPainter, QPen
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QBoxLayout,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

import autocal
import balance
from rack_widgets import COLORS, HeadGrid, is_uncalibrated, pump_online

log = logging.getLogger("JebaoGUI")

GREEN, RED, AMBER, BLUE, GREY = "#2FB36D", "#E05A5A", "#E0A33A", "#4C8DFF", "#8A8F98"
CUSTOM = "Custom…"


def fmt_rate(value, digits=3):
    return "—" if value is None else f"{value:.{digits}f}"


def fmt_minutes(seconds):
    """'about 4 min', 'about 1 h 20 min'."""
    minutes = max(1, int(round(seconds / 60.0)))
    if minutes < 60:
        return f"about {minutes} min"
    return f"about {minutes // 60} h {minutes % 60:02d} min"


# --- live chart of the current run --------------------------------------------------------


class MassChart(QWidget):
    """Mass against time for the head being calibrated, with the fitted line."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(220)
        self.points = []  # (seconds from pump on, grams above the starting mass)
        self.ignore = (None, None)  # (start, end) of the stretch used for the fit
        self.fit = None  # (slope g/s, intercept g) over that stretch
        self.title = ""
        self.markers = []
        self.setAccessibleName("Mass against time for the current calibration run")

    def set_data(self, points, ignore=(None, None), fit=None, title="", markers=()):
        """``markers``: [(seconds, label)] drawn as dashed lines (when ON and OFF were sent)."""
        self.points, self.ignore, self.fit, self.title = points, ignore, fit, title
        self.markers = list(markers)
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pal = self.palette()
        fg = pal.color(pal.ColorRole.WindowText)
        grid = QColor(fg)
        grid.setAlpha(40)
        faint = QColor(fg)
        faint.setAlpha(140)
        left, top, right, bottom = 62, 20, 12, 20
        area = QRectF(left, top, max(10, self.width() - left - right), max(10, self.height() - top - bottom))
        p.setPen(QPen(grid))
        p.drawRect(area)
        small = QFont(self.font())
        small.setPointSizeF(max(7.0, small.pointSizeF() - 1.5))
        p.setFont(small)
        if not self.points:
            p.setPen(faint)
            p.drawText(
                area, Qt.AlignmentFlag.AlignCenter, self.title or "The live mass reading appears here."
            )
            return
        ts = [t for t, _ in self.points]
        gs = [g for _, g in self.points]
        t0, t1 = min(0.0, min(ts)), max(max(ts), 1.0)
        g0, g1 = min(0.0, min(gs)), max(max(gs), 0.05)
        pad = (g1 - g0) * 0.08
        g0, g1 = g0 - pad, g1 + pad

        def xy(t, g):
            return QPointF(
                area.left() + (t - t0) / (t1 - t0) * area.width(),
                area.bottom() - (g - g0) / (g1 - g0) * area.height(),
            )

        # the stretch used for the fit
        a, b = self.ignore
        if a is not None and b is not None and b > a:
            band = QColor(BLUE)
            band.setAlpha(28)
            x0, x1 = xy(a, g0).x(), xy(min(b, t1), g0).x()
            p.fillRect(QRectF(x0, area.top(), max(0.0, x1 - x0), area.height()), band)
        # axes labels
        p.setPen(faint)
        for frac in (0.0, 0.5, 1.0):
            g = g0 + (g1 - g0) * frac
            pt = xy(t0, g)
            p.drawText(QRectF(0, pt.y() - 8, left - 6, 16), Qt.AlignmentFlag.AlignRight, f"{g:.2f} g")
        p.drawText(
            QRectF(area.left(), area.bottom() + 4, area.width(), 18),
            Qt.AlignmentFlag.AlignLeft,
            f"{t0:.0f} s",
        )
        p.drawText(
            QRectF(area.left(), area.bottom() + 4, area.width(), 18),
            Qt.AlignmentFlag.AlignRight,
            f"{t1:.0f} s  (0 = ON)",
        )
        p.drawText(
            QRectF(area.left(), 0, area.width(), top - 2),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            "Grams added · blue = part used for the fit",
        )
        # when ON and OFF were sent
        for t, label in self.markers:
            if t0 <= t <= t1:
                pen = QPen(faint, 1)
                pen.setStyle(Qt.PenStyle.DotLine)
                p.setPen(pen)
                x = xy(t, g0).x()
                p.drawLine(QPointF(x, area.top()), QPointF(x, area.bottom()))
                p.drawText(QRectF(x + 3, area.top() + 2, 40, 14), Qt.AlignmentFlag.AlignLeft, label)
        # data
        p.setPen(QPen(QColor(GREEN), 2))
        pts = [xy(t, g) for t, g in self.points]
        for i in range(1, len(pts)):
            p.drawLine(pts[i - 1], pts[i])
        if self.fit and a is not None and b is not None:
            slope, icpt = self.fit
            pen = QPen(QColor(BLUE), 2)
            pen.setStyle(Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.drawLine(xy(a, icpt + slope * a), xy(b, icpt + slope * b))


class _RunArea(QWidget):
    """Tells the tab its width so it can arrange the live view and the results."""

    def __init__(self, on_width, parent=None):
        super().__init__(parent)
        self._on_width = on_width

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._on_width(event.size().width())


# --- the tab ------------------------------------------------------------------------------


class CalibrationTab(QWidget):
    COL_RESULT, COL_ACTIONS = 7, 8

    def __init__(self, host, parent=None):
        super().__init__(parent)
        self.host = host
        self.bal = None
        self.cal = None  # autocal.AutoCalibrator of the current or last run
        self.task = None
        self.csv_path = ""
        self.clock, self.sleep = time.monotonic, asyncio.sleep  # replaced in tests
        self.manual_ran_s = None  # length of the last timed run started here
        cfg = host.config.data.setdefault("autocal", {})
        self.settings = autocal.Settings.from_dict(cfg.get("settings"))

        outer = QHBoxLayout(self)
        self.grid = HeadGrid(
            host, "Select the heads to calibrate.", status_for=self.tile_status, scroll=False
        )
        self.grid.selection_changed.connect(self.update_panel)
        for text, tip, fn in (
            ("Uncalibrated", "Select every head that has never been calibrated.", self.select_uncalibrated),
            ("All", "Select every head on the pumps that are online.", self.select_all),
            ("None", "Clear the selection.", self.grid.clear_selection),
        ):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            self.grid.top.insertWidget(2 + ["Uncalibrated", "All", "None"].index(text), b)
        # The rack, the live view and the results scroll together; the toolbar stays put.
        left = QVBoxLayout()
        left.addWidget(self.grid.toolbar)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        col = QVBoxLayout(content)
        col.setContentsMargins(0, 0, 4, 0)
        col.addWidget(self.grid)
        self.run_area = self._build_run_area()
        self.run_area.setMinimumHeight(520)
        col.addWidget(self.run_area)
        col.addStretch(1)
        self.scroll.setWidget(content)
        left.addWidget(self.scroll, 1)
        outer.addLayout(left, 1)
        outer.addWidget(self._build_panel())
        self.rebuild()

    # --- layout -------------------------------------------------------------------------

    def _build_panel(self):
        panel = QWidget()
        panel.setFixedWidth(380)
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(0, 0, 0, 0)
        modes = QHBoxLayout()
        self.mode_group = QButtonGroup(self)
        self.btn_auto = QPushButton("Automatic (balance)")
        self.btn_manual = QPushButton("Manual (timed run)")
        for i, b in enumerate((self.btn_auto, self.btn_manual)):
            b.setCheckable(True)
            b.setStyleSheet(
                "QPushButton:checked { background-color: #2F6FD6; color: white; font-weight: bold; }"
            )
            self.mode_group.addButton(b, i)
            modes.addWidget(b)
        self.btn_auto.setChecked(True)
        lay.addLayout(modes)
        self.pages = QStackedWidget()
        self.pages.addWidget(self._build_auto_page())
        self.pages.addWidget(self._scroll(self._build_manual_page()))
        self.mode_group.idClicked.connect(self._mode_clicked)
        lay.addWidget(self.pages, 1)
        return panel

    @staticmethod
    def _scroll(widget):
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        area.setWidget(widget)
        return area

    def _build_auto_page(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 4, 4, 0)
        cfg = self.host.config.data.setdefault("autocal", {})

        box = QGroupBox("1 · Balance")
        form = QVBoxLayout(box)
        self.combo_brand = QComboBox()
        for key, label in balance.BRANDS.items():
            self.combo_brand.addItem(label, key)
        idx = self.combo_brand.findData(cfg.get("brand", "mettler"))
        self.combo_brand.setCurrentIndex(max(0, idx))
        form.addWidget(self.combo_brand)
        port_row = QHBoxLayout()
        self.combo_port = QComboBox()
        self.combo_port.setMinimumWidth(180)
        self.combo_port.setToolTip("The USB or RS-232 port the balance is plugged into.")
        port_row.addWidget(self.combo_port, 1)
        btn_ports = QPushButton("↻")
        btn_ports.setFixedWidth(34)
        btn_ports.setToolTip("Look for ports again (after plugging the balance in).")
        btn_ports.clicked.connect(self.refresh_ports)
        port_row.addWidget(btn_ports)
        self.btn_connect = QPushButton("Connect")
        self.btn_connect.clicked.connect(self.toggle_balance)
        port_row.addWidget(self.btn_connect)
        form.addLayout(port_row)
        self.lbl_balance = QLabel("Not connected.")
        self.lbl_balance.setWordWrap(True)
        form.addWidget(self.lbl_balance)
        hint = QLabel("9600 baud, 8N1. Bonvoisin: turn on continuous output.")
        hint.setStyleSheet("color: grey; font-size: 11px;")
        hint.setToolTip(
            "Mettler Toledo: the app asks for each reading (MT-SICS SI), so only the baud rate "
            "matters. Bonvoisin and similar balances: set them to send readings continuously. "
            "Set the balance to grams."
        )
        form.addWidget(hint)
        lay.addWidget(box)
        self.refresh_ports()

        box = QGroupBox("2 · Run settings")
        form = QGridLayout(box)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(6)
        s = self.settings
        self.spin_volume = self._dspin(0.5, 1000, 1, s.volume_ml, " mL")
        self.spin_volume.setToolTip("Each head runs until about this much has reached the beaker.")
        self._cell(form, 0, 0, "Volume per head", self.spin_volume)
        self.combo_liquid = QComboBox()
        for name, rho in autocal.LIQUIDS.items():
            self.combo_liquid.addItem(f"{name} · {rho:.3f} g/mL", rho)
        self.combo_liquid.addItem(CUSTOM, None)
        self.spin_density = self._dspin(0.1, 5.0, 3, s.density, " g/mL")
        liquid = cfg.get("liquid", "Water")
        idx = next((i for i, n in enumerate(autocal.LIQUIDS) if n == liquid), self.combo_liquid.count() - 1)
        self.combo_liquid.setCurrentIndex(idx)
        self.combo_liquid.currentIndexChanged.connect(self._liquid_changed)
        self._cell(form, 1, 0, "Liquid", self.combo_liquid)
        self._cell(form, 1, 1, "Density", self.spin_density)
        self.spin_settle = self._dspin(1, 120, 0, s.settle_s, " s")
        self.spin_settle.setToolTip(
            "Wait at least this long before and after each run. If the reading is still changing "
            "(a pump that stopped late, a drip), it waits until it is still."
        )
        self._cell(form, 2, 0, "Settle before/after", self.spin_settle)
        self.spin_ignore = self._dspin(0, 60, 0, s.ignore_s, " s")
        self.spin_ignore.setToolTip(
            "Leave out this much at the start and end of each run when fitting the line."
        )
        self._cell(form, 2, 1, "Ignore start/end", self.spin_ignore)
        self.spin_tol = self._dspin(0.1, 50, 1, s.tolerance_pct, " %")
        self.spin_tol.setToolTip(
            "The flow must be steady: lines fitted to the first and second half of the run "
            "must agree within this."
        )
        self._cell(form, 3, 0, "Steady within", self.spin_tol)
        self.spin_r2 = self._dspin(0.5, 1.0, 3, s.min_r2, "")
        self.spin_r2.setSingleStep(0.001)
        self._cell(form, 3, 1, "Minimum R²", self.spin_r2)
        self.spin_retries = QSpinBox()
        self.spin_retries.setRange(0, 10)
        self.spin_retries.setValue(s.retries)
        self.spin_retries.setSuffix(" times")
        self._cell(form, 4, 0, "Redo up to", self.spin_retries)
        self.spin_max = self._dspin(20, 3600, 0, s.max_run_s, " s")
        self.spin_max.setToolTip("Stop a head after this long even if the volume hasn't been reached.")
        self._cell(form, 0, 1, "Longest run", self.spin_max)
        self.chk_autosave = QCheckBox("Save passing heads automatically")
        self.chk_autosave.setChecked(s.auto_save)
        self.chk_autosave.setText("Auto-save\npassing heads")
        form.addWidget(self.chk_autosave, 4, 1)
        for w in (
            self.spin_volume,
            self.spin_settle,
            self.spin_ignore,
            self.spin_tol,
            self.spin_r2,
            self.spin_max,
        ):
            w.valueChanged.connect(self._settings_changed)
        self.spin_density.valueChanged.connect(self._settings_changed)
        self.spin_retries.valueChanged.connect(self._settings_changed)
        self.chk_autosave.toggled.connect(self._settings_changed)
        lay.addWidget(self._scroll(box), 1)
        self._liquid_changed()

        box = QGroupBox("3 · Run")
        form = QVBoxLayout(box)
        self.lbl_plan = QLabel()
        self.lbl_plan.setWordWrap(True)
        form.addWidget(self.lbl_plan)
        self.btn_start = QPushButton()
        self.btn_start.setStyleSheet(
            "QPushButton { background-color: #2F6FD6; color: white; font-weight: bold; padding: 10px; }"
            "QPushButton:disabled { background-color: #555A62; color: #C9CCD2; }"
        )
        self.btn_start.clicked.connect(self.start)
        form.addWidget(self.btn_start)
        ctl = QHBoxLayout()
        self.btn_pause = QPushButton("Pause after this head")
        self.btn_pause.setCheckable(True)
        self.btn_pause.toggled.connect(self._pause_toggled)
        self.btn_stop = QPushButton("Stop")
        self.btn_stop.setToolTip("Turn off the head being calibrated and stop.")
        self.btn_stop.clicked.connect(self.stop)
        ctl.addWidget(self.btn_pause)
        ctl.addWidget(self.btn_stop)
        form.addLayout(ctl)
        lay.addWidget(box)
        return page

    def _build_manual_page(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 4, 4, 0)
        intro = QLabel(
            "Each selected head runs for a set time into its own container. Then enter how much "
            "each one delivered and save."
        )
        intro.setWordWrap(True)
        lay.addWidget(intro)
        box = QGroupBox("1 · Run")
        form = QFormLayout(box)
        self.spin_manual_s = self._dspin(5, 3600, 0, 60.0, " s")
        form.addRow("Run time", self.spin_manual_s)
        self.btn_manual_run = QPushButton()
        self.btn_manual_run.clicked.connect(self.run_manual)
        form.addRow(self.btn_manual_run)
        lay.addWidget(box)

        box = QGroupBox("2 · Enter what came out")
        v = QVBoxLayout(box)
        row = QHBoxLayout()
        self.combo_manual_unit = QComboBox()
        self.combo_manual_unit.addItem("mL", None)
        for name, rho in autocal.LIQUIDS.items():
            self.combo_manual_unit.addItem(f"g of {name.lower()} ({rho:.3f})", rho)
        row.addWidget(QLabel("Measured in"))
        row.addWidget(self.combo_manual_unit, 1)
        v.addLayout(row)
        self.manual_table = QTableWidget(0, 3)
        self.manual_table.setHorizontalHeaderLabels(["Head", "Measured", "New rate"])
        self.manual_table.verticalHeader().setVisible(False)
        self.manual_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.manual_table.setMinimumHeight(220)
        v.addWidget(self.manual_table)
        self.combo_manual_unit.currentIndexChanged.connect(self._manual_rates)
        self.spin_manual_s.valueChanged.connect(self._manual_time_changed)
        self.btn_manual_save = QPushButton("Save rates")
        self.btn_manual_save.clicked.connect(self.save_manual)
        v.addWidget(self.btn_manual_save)
        lay.addWidget(box)
        lay.addStretch(1)
        return page

    def _build_run_area(self):
        area = _RunArea(self._arrange_run_area)
        lay = QBoxLayout(QBoxLayout.Direction.LeftToRight, area)
        lay.setContentsMargins(0, 0, 0, 0)
        self.run_layout = lay

        now = QGroupBox("Now calibrating")
        nv = QVBoxLayout(now)
        self.lbl_now_sub = QLabel("Nothing running.")
        self.lbl_now_sub.setStyleSheet("color: grey;")
        self.lbl_now = QLabel("")
        self.lbl_now.setStyleSheet("font-size: 15px; font-weight: 600;")
        self.lbl_now.setWordWrap(True)
        nv.addWidget(self.lbl_now_sub)
        nv.addWidget(self.lbl_now)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(6)
        nv.addWidget(self.bar)
        self.chart = MassChart()
        nv.addWidget(self.chart, 1)
        stats = QHBoxLayout()
        self.stat_labels = {}
        for key, title in (
            ("mass", "Added"),
            ("slope", "Flow rate"),
            ("r2", "Fit R²"),
            ("delays", "Start / stop delay"),
        ):
            col = QVBoxLayout()
            t = QLabel(title)
            t.setStyleSheet("color: grey; font-size: 11px;")
            val = QLabel("—")
            val.setStyleSheet("font-family: Menlo, Consolas, monospace; font-size: 15px;")
            col.addWidget(t)
            col.addWidget(val)
            stats.addLayout(col)
            self.stat_labels[key] = val
        nv.addLayout(stats)
        lay.addWidget(now, 1)
        self.now_box = now

        res = QGroupBox("Results")
        rv = QVBoxLayout(res)
        cols = [
            ("Head", "", 150),
            ("Rate\nmL/min", "Flow rate: the slope of the fitted line. This is what gets saved.", 78),
            ("Change\nvs old", "How much the new rate differs from the one saved before.", 74),
            ("R²", "How well a straight line fits the readings.", 70),
            ("Steady", "How much the first and second half of the run differed.", 66),
            (
                "Delays (s)\nstart / stop",
                "How late the pump really started / stopped after the app sent ON / OFF. "
                "These are cloud delays: shown, not counted against the head.",
                96,
            ),
            ("Old\nmL/min", "The rate saved before.", 74),
            ("Result", "", 0),
            ("", "", 150),
        ]
        self.results_table = QTableWidget(0, len(cols))
        self.results_table.setHorizontalHeaderLabels([c[0] for c in cols])
        hdr = self.results_table.horizontalHeader()
        hdr.setDefaultAlignment(Qt.AlignmentFlag.AlignCenter)
        hdr.setMinimumSectionSize(40)
        for col, (_name, tip, width) in enumerate(cols):
            if tip:
                self.results_table.horizontalHeaderItem(col).setToolTip(tip)
            if col == self.COL_RESULT:
                hdr.setSectionResizeMode(col, QHeaderView.ResizeMode.Stretch)
            else:
                hdr.setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)
                self.results_table.setColumnWidth(col, width)
        self.results_table.verticalHeader().setVisible(False)
        self.results_table.verticalHeader().setDefaultSectionSize(34)
        self.results_table.setAlternatingRowColors(True)
        self.results_table.setShowGrid(False)
        self.results_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.results_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.results_table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.results_table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.results_table.setMinimumHeight(200)
        rv.addWidget(self.results_table, 1)
        note = QLabel(
            "Rate = slope of the fitted line (mL/min), saved when R² and Steady pass. Delays = how "
            "late the pump started / stopped after ON / OFF, from where the line meets the mass "
            "before and after: cloud delays, which change from command to command."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: grey; font-size: 11px;")
        rv.addWidget(note)
        btns = QHBoxLayout()
        self.lbl_csv = QLabel()
        self.lbl_csv.setStyleSheet("color: grey; font-size: 11px;")
        self.lbl_csv.setWordWrap(True)
        btns.addWidget(self.lbl_csv, 1)
        self.btn_export = QPushButton("Export CSV…")
        self.btn_export.clicked.connect(self.export_csv)
        self.btn_save = QPushButton("Save passing rates")
        self.btn_save.setToolTip("Save every head that passed but wasn't saved yet.")
        self.btn_save.clicked.connect(self.save_passing)
        self.btn_clear = QPushButton("Clear")
        self.btn_clear.clicked.connect(self.clear_results)
        for b in (self.btn_export, self.btn_save, self.btn_clear):
            btns.addWidget(b)
        rv.addLayout(btns)
        lay.addWidget(res, 2)
        self._table_shown = None
        return area

    WIDE_ENOUGH = 1150  # px: below this the live view goes above the results instead of beside

    def _arrange_run_area(self, width):
        """Side by side on wide windows; stacked on narrower ones so the table gets the width."""
        if not hasattr(self, "run_area"):
            return
        stacked = width < self.WIDE_ENOUGH
        want = QBoxLayout.Direction.TopToBottom if stacked else QBoxLayout.Direction.LeftToRight
        if self.run_layout.direction() != want:
            self.run_layout.setDirection(want)
            self.run_layout.setStretch(0, 0 if stacked else 1)
            self.run_layout.setStretch(1, 1 if stacked else 2)
            self.now_box.setMaximumHeight(380 if stacked else 16777215)
            self.run_area.setMinimumHeight(820 if stacked else 520)

    @staticmethod
    def _cell(grid, row, col, label, widget):
        box = QVBoxLayout()
        box.setSpacing(2)
        lbl = QLabel(label)
        lbl.setStyleSheet("color: grey; font-size: 11px;")
        box.addWidget(lbl)
        box.addWidget(widget)
        grid.addLayout(box, row, col)

    @staticmethod
    def _dspin(lo, hi, decimals, value, suffix):
        w = QDoubleSpinBox()
        w.setRange(lo, hi)
        w.setDecimals(decimals)
        w.setValue(float(value))
        w.setSuffix(suffix)
        return w

    # --- grid and selection --------------------------------------------------------------

    def rebuild(self):
        self.grid.rebuild()
        self.update_panel()

    def refresh(self):
        """Called a few times a second by the main window."""
        self._watch_cancel()
        self.grid.refresh()
        self.update_panel()
        self.update_run_view()
        self._update_balance_label()

    def select_uncalibrated(self):
        keys = []
        for card in self.grid.cards:
            pump = self.host.config.get_pump(card.did)
            keys += [(card.did, ch) for ch in range(1, 5) if is_uncalibrated(pump, ch)]
        self.grid.select(keys)

    def select_all(self):
        self.grid.select([(c.did, ch) for c in self.grid.cards for ch in range(1, 5)])

    def heads(self):
        """Selected heads in rack order."""
        order = [(c.did, ch) for c in self.grid.cards for ch in range(1, 5)]
        return [k for k in order if k in self.grid.selected]

    def result_for(self, key):
        if self.cal is None:
            return None
        return next((r for r in self.cal.results if r.key == key), None)

    def tile_status(self, did, ch):
        r = self.result_for((did, ch))
        if r is not None:
            if r.status == autocal.WAITING:
                waiting = [x for x in self.cal.results if x.status == autocal.WAITING]
                return (f"Queued · {waiting.index(r) + 1} of {len(waiting)}", GREY, None)
            if r.status == autocal.RUNNING:
                return (self._phase_text(r), GREEN, self._progress())
            rate = fmt_rate(r.rate)
            return {
                autocal.SAVED: (f"✓ {rate} mL/min · saved", GREEN, None),
                autocal.ACCEPTED: (f"✓ {rate} mL/min · accepted", GREEN, None),
                autocal.PASSED: (f"✓ {rate} mL/min · not saved yet", AMBER, None),
                autocal.FAILED: ("✗ " + (r.note or "failed"), RED, None),
                autocal.STOPPED: ("Stopped", GREY, None),
            }.get(r.status)
        pump = self.host.config.get_pump(did)
        dates = pump.get("calibrated_at") or []
        if ch <= len(dates) and dates[ch - 1] and not self.host.run_state(did, ch):
            return (f"Calibrated {dates[ch - 1]}", COLORS["idle"], None)
        return None

    # --- settings ------------------------------------------------------------------------

    def _mode_clicked(self, idx):
        self.pages.setCurrentIndex(idx)
        self.run_area.setVisible(idx == 0)  # the live view and results are for automatic runs
        self.update_panel()

    def _liquid_changed(self):
        rho = self.combo_liquid.currentData()
        custom = rho is None
        self.spin_density.setEnabled(custom)
        if not custom:
            self.spin_density.setValue(rho)
        self._settings_changed()

    def _settings_changed(self, *_):
        s = self.settings
        s.volume_ml = self.spin_volume.value()
        s.density = self.spin_density.value()
        s.settle_s = self.spin_settle.value()
        s.ignore_s = self.spin_ignore.value()
        s.tolerance_pct = self.spin_tol.value()
        s.min_r2 = self.spin_r2.value()
        s.retries = self.spin_retries.value()
        s.max_run_s = self.spin_max.value()
        s.auto_save = self.chk_autosave.isChecked()
        cfg = self.host.config.data.setdefault("autocal", {})
        cfg["settings"] = s.to_dict()
        text = self.combo_liquid.currentText()
        cfg["liquid"] = text.split(" · ")[0] if text != CUSTOM else "Custom"
        if hasattr(self, "lbl_plan"):
            self.update_panel()

    def save_settings(self):
        cfg = self.host.config.data.setdefault("autocal", {})
        cfg["brand"] = self.combo_brand.currentData()
        cfg["port"] = self.combo_port.currentData() or cfg.get("port", "")
        self.host.config.save()

    # --- balance -------------------------------------------------------------------------

    def refresh_ports(self):
        want = self.combo_port.currentData() or self.host.config.data.get("autocal", {}).get("port", "")
        self.combo_port.clear()
        ports = balance.list_ports()
        for dev, desc in ports:
            self.combo_port.addItem(f"{dev} ({desc})" if desc else dev, dev)
        if not ports:
            self.combo_port.addItem("No serial ports found", None)
        idx = self.combo_port.findData(want)
        if idx >= 0:
            self.combo_port.setCurrentIndex(idx)

    def toggle_balance(self):
        if self.bal is not None:
            if self.running():
                QMessageBox.warning(self, "Balance", "Stop the calibration before disconnecting the balance.")
                return
            self.bal.close()
            self.bal = None
            log.info("Balance disconnected.")
        else:
            port = self.combo_port.currentData()
            if not port:
                QMessageBox.warning(self, "Balance", "Plug the balance in, press ↻, then pick its port.")
                return
            bal = balance.SerialBalance(port, self.combo_brand.currentData())
            try:
                bal.open()
            except balance.BalanceError as e:
                QMessageBox.warning(self, "Balance", str(e))
                return
            self.bal = bal
            self.save_settings()
        self._update_balance_label()
        self.update_panel()

    def use_balance(self, bal):
        """Use an already open balance (tests, or a pretend one)."""
        self.bal = bal
        self._update_balance_label()
        self.update_panel()

    def _update_balance_label(self):
        b = self.bal
        self.btn_connect.setText("Disconnect" if b else "Connect")
        self.combo_brand.setEnabled(b is None)
        self.combo_port.setEnabled(b is None)
        if b is None:
            self.lbl_balance.setText("Not connected.")
            self.lbl_balance.setStyleSheet("color: grey;")
            return
        last = b.latest()
        if b.error:
            text, color = "⚠ " + b.error, RED
        elif last is None or not b.fresh(3.0):
            text, color = f"Connected on {b.port}, but no readings yet. Check the balance's settings.", AMBER
        else:
            text = f"● Connected on {b.port} · {last[1]:.3f} g{'' if last[2] else ' (settling)'}"
            color = GREEN
        self.lbl_balance.setText(text)
        self.lbl_balance.setStyleSheet(f"color: {color};")

    # --- the plan and the controls -------------------------------------------------------

    def running(self):
        if self.task is not None:
            return not self.task.done()
        return self.cal is not None and self.cal.started is not None and not self.cal.finished

    def update_panel(self):
        heads = self.heads()
        n = len(heads)
        s = self.settings
        busy = self.running()
        rates = [self.host.config.get_pump(d)["calibration"][c - 1] for d, c in heads]
        if n:
            secs = autocal.estimate_seconds(s, rates)
            added = n * s.volume_ml
            plan = (
                f"<b>{n} head{'s' if n != 1 else ''}</b>, one after another: {fmt_minutes(secs)} "
                f"with no redos. Adds about {added:.0f} mL ({added * s.density:.0f} g) to the beaker: "
                "keep the total under the balance's capacity."
            )
            if any(not pump_online(self.host, d) for d, _ in heads):
                plan += "<br><span style='color:#E67E22'>Some selected pumps are offline.</span>"
        else:
            plan = "Select heads on the rack, or use Uncalibrated / All above it."
        if busy and self.cal is not None:
            left = self.cal.seconds_left(lambda d, c: self.host.config.get_pump(d)["calibration"][c - 1])
            plan = f"Running: {fmt_minutes(left)} left." if left > 30 else "Running: almost done."
        self.lbl_plan.setText(plan)
        ready = self.bal is not None and n > 0 and not busy
        self.btn_start.setEnabled(ready)
        if busy:
            self.btn_start.setText("Calibrating…")
        elif self.bal is None:
            self.btn_start.setText("Connect the balance to start")
        else:
            self.btn_start.setText(f"Calibrate {n} head{'s' if n != 1 else ''}" if n else "Select heads")
        self.btn_pause.setEnabled(busy)
        self.btn_stop.setEnabled(busy)
        if not busy and self.btn_pause.isChecked():
            self.btn_pause.blockSignals(True)
            self.btn_pause.setChecked(False)
            self.btn_pause.blockSignals(False)
        self.btn_pause.setText(
            "Paused · Resume" if (self.cal and self.cal.paused) else "Pause after this head"
        )
        self.btn_manual_run.setText(
            f"Run {n} head{'s' if n != 1 else ''} for {self.spin_manual_s.value():.0f} s"
            if n
            else "Select heads"
        )
        self.btn_manual_run.setEnabled(n > 0 and not busy)
        self._sync_manual_table(heads)

    def ready_to_run(self, heads, title="Can't start yet"):
        """Check everything before any head runs, then ask for the outlets to go in the
        beaker. True to go ahead."""
        host = self.host
        problems = []
        if self.bal is None:
            problems.append("Connect the balance first.")
        elif not self.bal.fresh(3.0):
            problems.append("The balance isn't sending readings yet.")
        if host.current_running_sequence or host.sequence_queue:
            problems.append("A sequence is running or queued. Calibrate when no sequence is running.")
        busy = [host.head_label(*h) for h in heads if h in host.head_runs]
        if busy:
            problems.append("These heads are running right now: " + ", ".join(busy))
        offline = sorted({host.get_device_name(d) for d, _ in heads if not pump_online(host, d)})
        if offline:
            problems.append("These pumps are offline: " + ", ".join(offline))
        if problems:
            QMessageBox.warning(self, title, "\n\n".join(problems))
            return False
        n = len(heads)
        reply = QMessageBox.question(
            self,
            "Calibrate",
            f"Put {'every selected outlet' if n > 1 else 'the outlet'} into the beaker on the balance, "
            f"then press OK.\n\n{n} head{'s' if n != 1 else ''} will run one at a time and add about "
            f"{n * self.settings.volume_ml:.0f} mL in total.",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
        )
        return reply == QMessageBox.StandardButton.Ok

    def start(self):
        heads = self.heads()
        if not heads or self.bal is None or self.running():
            return
        if not self.ready_to_run(heads):
            return
        self._settings_changed()
        self.host.config.save()
        self.begin(heads)

    def begin(self, heads):
        """Start calibrating ``heads`` (no questions asked)."""
        host = self.host
        settings = autocal.Settings.from_dict(self.settings.to_dict())
        self.cal = autocal.AutoCalibrator(
            self.bal,
            self._set_head,
            settings,
            save=self._save_auto,
            clock=self.clock,
            sleep=self.sleep,
            is_busy=self._busy_elsewhere,
        )
        for did, ch in heads:
            self.cal.add(did, ch, host.head_label(did, ch), host.config.get_pump(did)["calibration"][ch - 1])
        self.csv_path = ""
        log.info(
            f"Automatic calibration of {len(heads)} head(s): {settings.volume_ml:g} mL each, "
            f"density {settings.density:.3f} g/mL, within {settings.tolerance_pct:g} %, R² ≥ {settings.min_r2:g}."
        )
        self._launch()

    def _launch(self):
        self.task = self.host._start_task(self._run)
        self.update_panel()

    async def _run(self):
        host = self.host
        host._motor_jobs += 1
        try:
            await self.cal.run()
        except Exception as e:  # noqa: BLE001 - shown in the log, the app keeps going
            log.error(f"Automatic calibration stopped: {e}")
        finally:
            host._motor_jobs -= 1
            self._finish()

    def _finish(self):
        cal = self.cal
        if cal is None:
            return
        counts = {}
        for r in cal.results:
            counts[r.status] = counts.get(r.status, 0) + 1
        summary = ", ".join(f"{v} {k}" for k, v in counts.items())
        log.info(f"Automatic calibration finished: {summary}.")
        try:
            folder = os.path.join(self.host.data_dir, "calibrations")
            name = f"autocal-{datetime.now():%Y-%m-%d-%H%M%S}.csv"
            self.csv_path = autocal.write_csv(
                os.path.join(folder, name), cal.results, cal.settings, self._bal_name()
            )
            self.lbl_csv.setText(f"Saved to calibrations/{name}")
        except OSError as e:
            log.warning(f"Couldn't save the calibration results: {e}")
        self.update_panel()
        self.update_run_view()

    def _bal_name(self):
        return "" if self.bal is None else f"{balance.BRANDS.get(self.bal.brand, '')} on {self.bal.port}"

    def stop(self):
        if self.cal is not None and self.running():
            log.warning("Automatic calibration: stopping.")
            self.cal.stop()

    def _pause_toggled(self, on):
        if self.cal is not None:
            self.cal.pause_after_head(on)
            log.info(
                "Automatic calibration will pause after this head."
                if on
                else "Automatic calibration resumed."
            )
        self.update_panel()

    def _watch_cancel(self):
        """Cancel from the Manual tab or Emergency Stop also stops the calibration.
        Also keeps the head's time left (shown on the Manual tab) roughly right."""
        self._retry_stuck()
        cal = self.cal
        if cal is None or cal.current is None or not self.running():
            return
        run = self.host.head_runs.get(cal.current.key)
        if run is None or run.source != "calibration":
            return
        if run.cancelled:
            cal.stop()
        elif cal.attempt is not None and cal.attempt.t_on:
            run.remaining = max(0.0, run.duration - (cal.clock() - cal.attempt.t_on))

    def _retry_stuck(self):
        """A head whose OFF failed stays listed as running. When someone cancels it (Manual
        tab Cancel, or Emergency Stop), try turning it off again."""
        if self.running():
            return
        for key, run in list(self.host.head_runs.items()):
            if run.source == "calibration" and run.cancelled and not getattr(run, "retrying", False):
                run.retrying = True
                self.host._start_task(lambda key=key, run=run: self._off_again(key, run))

    async def _off_again(self, key, run):
        try:
            if await self.host.set_head(*key, False):
                self.host._end_head_run(run)
                log.info(f"{self.host.head_label(*key)}: stopped.")
            else:
                run.cancelled = False  # cancel again to retry
                log.error(f"{self.host.head_label(*key)}: still couldn't stop it. Use the pump's own button.")
        finally:
            run.retrying = False

    # --- what the calibrator calls -------------------------------------------------------

    def _busy_elsewhere(self, did, ch):
        run = self.host.head_runs.get((did, ch))
        return run is not None and run.source != "calibration"

    async def _set_head(self, did, ch, on):
        host = self.host
        ok = await host.set_head(did, ch, on)
        if on and ok:
            rate = host.config.get_pump(did)["calibration"][ch - 1] or 1.0
            est = max(
                self.settings.min_run_s(), min(self.settings.max_run_s, self.settings.volume_ml / rate * 60)
            )
            host._begin_head_run(did, ch, est, "calibration")
        elif not on:
            run = host.head_runs.get((did, ch))
            if run is not None and run.source == "calibration" and ok:
                host._end_head_run(run)
        return ok

    def _save_auto(self, did, ch, rate):
        self.host.save_head_rate(did, ch, rate, "automatic calibration")

    # --- the live view and results -------------------------------------------------------

    def _phase_text(self, r):
        cal = self.cal
        if cal.phase == "settling":
            left = (cal.phase_until or 0) - cal.clock()
            return f"Settling · {left:.0f} s" if left >= 0.5 else "Waiting for a still reading"
        if cal.phase == "starting":
            return "Turning the pump on…"
        if cal.phase == "running":
            return f"Running · {self._added():.2f} mL"
        if cal.phase == "settling after":
            return "Settling after the run"
        return "Starting…"

    def _added(self):
        """mL added so far in the current run."""
        cal, a = self.cal, self.cal.attempt if self.cal else None
        if a is None or not a.t_on or self.bal is None:
            return 0.0
        last = self.bal.latest()
        return 0.0 if last is None else max(0.0, (last[1] - a.start_g) / cal.settings.density)

    def _progress(self):
        cal = self.cal
        if cal is None or cal.attempt is None:
            return 0.0
        if cal.phase == "running":
            return min(1.0, self._added() / max(cal.settings.volume_ml, 1e-9))
        return 1.0 if cal.phase == "settling after" else 0.0

    def update_run_view(self):
        cal = self.cal
        if cal is None:
            self.lbl_now_sub.setText("Nothing running.")
            self.lbl_now.setText("")
            self.bar.setValue(0)
            self.chart.set_data([], title="The live mass reading appears here.")
            for v in self.stat_labels.values():
                v.setText("—")
            self._fill_results()
            return
        r = cal.current
        if r is not None and cal.attempt is not None:
            total = len(cal.results)
            pos = cal.results.index(r) + 1
            tries = 1 + cal.settings.retries
            self.lbl_now_sub.setText(
                f"Head {pos} of {total} · try {cal.attempt.number} of {tries} · {self._phase_text(r)}"
            )
            self.lbl_now.setText(r.label)
            self.bar.setValue(int(self._progress() * 1000))
            self._show_attempt(cal.attempt, live=True)
        else:
            shown = next((x for x in reversed(cal.results) if x.last is not None), None)
            if cal.paused:
                self.lbl_now_sub.setText("Paused. Press Resume to go on.")
            elif self.running():
                self.lbl_now_sub.setText("Starting…")
            else:
                self.lbl_now_sub.setText("Finished. Last run shown.")
            self.lbl_now.setText(shown.label if shown else "")
            self.bar.setValue(1000 if shown else 0)
            if shown:
                self._show_attempt(shown.last, live=False)
        self._fill_results()

    def _show_attempt(self, a, live):
        s = self.cal.settings
        if self.bal is not None and live:
            samples = self.bal.samples(since=(a.t_on - s.settle_s) if a.t_on else None)[-3000:]
        else:
            samples = a.samples
        if not a.t_on:
            waiting = (
                "Turning the pump on…"
                if self.cal.phase == "starting"
                else "Settling: waiting for a still reading…"
            )
            self.chart.set_data([], title=waiting)
            self._stats(a, None)
            return
        pts = [(t - a.t_on, g - a.start_g) for t, g, _st in samples]
        end = (a.t_off or self.cal.clock()) - a.t_on
        if a.fit_t0 is not None:  # after the run: the stretch the result was worked out from
            lo, hi = a.fit_t0 - a.t_on, a.fit_t1 - a.t_on
        else:
            lo, hi = s.ignore_s, end - s.ignore_s
        fit = None
        window = [(t, g) for t, g in pts if lo <= t <= hi]
        if len(window) >= 5 and window[-1][0] - window[0][0] >= 2:
            slope, icpt, r2 = autocal.fit_line([t for t, _ in window], [g for _, g in window])
            fit = (slope, icpt)
            live_slope, live_r2 = slope / s.density * 60, r2
        else:
            live_slope = live_r2 = None
        self.chart.set_data(
            pts,
            (lo, hi) if hi > lo else (None, None),
            fit,
            markers=[(0.0, "ON")] + ([(end, "OFF")] if a.t_off else []),
        )
        self._stats(a, (live_slope, live_r2))

    def _stats(self, a, live):
        if not a.t_on:
            added = 0.0
        elif a.dmass_rate is None:
            added = self._added()
        else:
            added = (a.end_g - a.start_g) / self.cal.settings.density
        self.stat_labels["mass"].setText(f"{added:.2f} mL")
        slope = a.slope_rate if a.slope_rate is not None else (live[0] if live else None)
        r2 = a.r2 if a.r2 is not None else (live[1] if live else None)
        self.stat_labels["slope"].setText(fmt_rate(slope) + (" mL/min" if slope is not None else ""))
        self.stat_labels["r2"].setText("—" if r2 is None else f"{r2:.4f}")
        self.stat_labels["delays"].setText(
            "after stop"
            if a.start_delay is None
            else f"{round(a.start_delay) + 0:+d} / {round(a.stop_delay) + 0:+d} s"
        )

    def _fill_results(self):
        cal = self.cal
        rows = cal.results if cal else []
        shown = tuple(
            (r.key, r.status, len(r.attempts), r.rate, r.last and r.last.steady_pct, r.note) for r in rows
        )
        if shown != self._table_shown:
            self._table_shown = shown
            self._rebuild_results(rows)
        # the running head's line follows what it is doing, without rebuilding the table
        for i, r in enumerate(rows):
            if r.status == autocal.RUNNING:
                item = self.results_table.item(i, self.COL_RESULT)
                if item is not None:
                    tries = 1 + cal.settings.retries
                    n = len(r.attempts) or 1
                    item.setText(f"{self._phase_text(r)} · try {n} of {tries}")

    def _rebuild_results(self, rows):
        cal = self.cal
        t = self.results_table
        t.setRowCount(len(rows))
        names = {
            autocal.WAITING: ("Waiting", GREY),
            autocal.RUNNING: ("Running…", BLUE),
            autocal.SAVED: ("Saved", GREEN),
            autocal.ACCEPTED: ("Accepted by you", GREEN),
            autocal.PASSED: ("Passed · not saved yet", AMBER),
            autocal.FAILED: ("Failed", RED),
            autocal.STOPPED: ("Stopped", GREY),
        }
        right = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        center = Qt.AlignmentFlag.AlignCenter
        left = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        for i, r in enumerate(rows):
            b = r.best
            text, color = names.get(r.status, (r.status, GREY))
            tries = f" after {len(r.attempts)} tries" if len(r.attempts) > 1 else ""
            if r.status == autocal.FAILED:
                text = f"Failed{tries}" + (f": {r.note}" if r.note else "")
            elif r.status in (autocal.SAVED, autocal.ACCEPTED, autocal.PASSED):
                if r.note:
                    text += f" · {r.note}"
                elif b and b.delay_note:
                    text += f" · pump {b.delay_note}"
            change = None
            if b and b.slope_rate and r.old_rate:
                change = (b.slope_rate - r.old_rate) / r.old_rate * 100.0
            cells = [
                (f"{self.host.get_device_name(r.did)} · CH{r.ch}", left, None, r.label),
                (fmt_rate(b.slope_rate) if b else "—", right, None, None),
                ("—" if change is None else f"{change:+.1f} %", right, None, None),
                ("—" if not b or b.r2 is None else f"{b.r2:.4f}", right, None, None),
                ("—" if not b or b.steady_pct is None else f"{b.steady_pct:.1f} %", right, None, None),
                (
                    "—"
                    if not b or b.start_delay is None
                    else f"{round(b.start_delay) + 0:+d} / {round(b.stop_delay) + 0:+d}",
                    center,
                    AMBER if b and b.delay_note else None,
                    (
                        f"The pump {b.delay_note} (rate over the commanded time: "
                        f"{fmt_rate(b.dmass_rate)} mL/min). Timed doses are off by about this much."
                    )
                    if b and b.delay_note
                    else None,
                ),
                (fmt_rate(r.old_rate), right, GREY, None),
                (text, left, color, r.note or text),
            ]
            if b and b.steady_pct is not None and b.steady_pct > cal.settings.tolerance_pct:
                cells[4] = (cells[4][0], right, RED, None)
            if b and b.r2 is not None and b.r2 < cal.settings.min_r2:
                cells[3] = (cells[3][0], right, RED, None)
            for col, (val, align, fg, tip) in enumerate(cells):
                item = QTableWidgetItem(val)
                item.setTextAlignment(align)
                if fg:
                    item.setForeground(QColor(fg))
                if tip:
                    item.setToolTip(tip)
                t.setItem(i, col, item)
            t.setCellWidget(i, self.COL_ACTIONS, self._actions(r))

    def _actions(self, r):
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(4, 2, 4, 2)
        lay.setSpacing(6)
        if r.status in (autocal.FAILED, autocal.PASSED) and r.rate:
            acc = QPushButton("Accept" if r.status == autocal.FAILED else "Save")
            acc.setToolTip(f"Save {r.rate:.3f} mL/min (the flow rate) for this head.")
            acc.clicked.connect(lambda _c=False, r=r: self.accept(r))
            acc.setFixedWidth(64)
            lay.addWidget(acc)
        if r.status in (autocal.FAILED, autocal.PASSED, autocal.STOPPED, autocal.SAVED, autocal.ACCEPTED):
            redo = QPushButton("Redo")
            redo.setToolTip("Calibrate this head again.")
            redo.clicked.connect(lambda _c=False, r=r: self.redo(r))
            redo.setFixedWidth(64)
            lay.addWidget(redo)
        lay.addStretch(1)
        return w

    def accept(self, r):
        if not r.rate:
            return
        self.host.save_head_rate(r.did, r.ch, r.rate, "automatic calibration (accepted)")
        r.status = autocal.ACCEPTED
        self._table_shown = None
        self.update_run_view()

    def redo(self, r):
        if self.cal is None:
            return
        if not self.running():
            if not self.ready_to_run([r.key], "Can't redo yet"):
                return
            self.cal.settings = autocal.Settings.from_dict(self.settings.to_dict())
            self.cal.requeue(r)
            self._launch()
        else:
            self.cal.requeue(r)  # its turn comes after the heads still waiting
        self._table_shown = None
        self.update_run_view()

    def save_passing(self):
        if self.cal is None:
            return
        for r in self.cal.results:
            if r.status == autocal.PASSED and r.rate:
                self.host.save_head_rate(r.did, r.ch, r.rate, "automatic calibration")
                r.status = autocal.SAVED
        self._table_shown = None
        self.update_run_view()

    def clear_results(self):
        if self.running():
            return
        self.cal = None
        self.lbl_csv.setText("")
        self._table_shown = None
        self.update_run_view()

    def export_csv(self):
        if self.cal is None or not self.cal.results:
            QMessageBox.information(self, "Export", "There are no results yet.")
            return
        default = os.path.join(os.path.expanduser("~"), f"autocal-{datetime.now():%Y-%m-%d-%H%M}.csv")
        path, _ = QFileDialog.getSaveFileName(self, "Export calibration results", default, "CSV (*.csv)")
        if path:
            autocal.write_csv(path, self.cal.results, self.cal.settings, self._bal_name())
            log.info(f"Calibration results exported to {path} (raw readings in *_raw.csv).")

    # --- manual (timed run) --------------------------------------------------------------

    def run_manual(self):
        heads = self.heads()
        if not heads:
            return
        secs = self.spin_manual_s.value()
        reply = QMessageBox.question(
            self,
            "Timed run",
            f"Put each selected outlet into its own container, then press OK.\n\n"
            f"{len(heads)} head(s) will run together for {secs:.0f} s.",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
        )
        if reply == QMessageBox.StandardButton.Ok:
            self.manual_ran_s = secs  # rates use the time that really ran, even if the box changes
            self.host.dispense(heads, secs, "time")
            self._manual_rates()

    def _sync_manual_table(self, heads):
        t = self.manual_table
        current = [t.item(i, 0).data(Qt.ItemDataRole.UserRole) for i in range(t.rowCount()) if t.item(i, 0)]
        if current == heads:
            self._manual_rates()
            return
        old = {
            t.item(i, 0).data(Qt.ItemDataRole.UserRole): t.cellWidget(i, 1).value()
            for i in range(t.rowCount())
            if t.item(i, 0) and t.cellWidget(i, 1)
        }
        for row in range(t.rowCount()):
            w = t.cellWidget(row, 1)
            if w is not None:
                w.hide()  # removed widgets are only deleted once the event loop runs again
            t.removeCellWidget(row, 1)
        t.setRowCount(0)
        t.setRowCount(len(heads))
        for i, key in enumerate(heads):
            item = QTableWidgetItem(f"{self.host.get_device_name(key[0])} · CH{key[1]}")
            item.setToolTip(self.host.head_label(*key))
            item.setData(Qt.ItemDataRole.UserRole, key)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            t.setItem(i, 0, item)
            spin = self._dspin(0, 100000, 3, old.get(key, 0.0), "")
            spin.valueChanged.connect(self._manual_rates)
            t.setCellWidget(i, 1, spin)
            t.setItem(i, 2, QTableWidgetItem("—"))
        self._manual_rates()

    def _manual_time_changed(self, *_):
        self.manual_ran_s = None  # a new run time: the next rates are for a run of this length
        self._manual_rates()

    def manual_rate(self, measured):
        """mL/min from what one head delivered in the timed run."""
        rho = self.combo_manual_unit.currentData()
        ml = measured / rho if rho else measured
        return ml / (self.manual_ran_s or self.spin_manual_s.value()) * 60.0

    def _manual_rates(self, *_):
        t = self.manual_table
        any_value = False
        for i in range(t.rowCount()):
            spin = t.cellWidget(i, 1)
            if spin is None:
                continue
            v = spin.value()
            any_value |= v > 0
            item = t.item(i, 2)
            if item is None:
                item = QTableWidgetItem()
                t.setItem(i, 2, item)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            item.setText(f"{self.manual_rate(v):.3f}" if v > 0 else "—")
        self.btn_manual_save.setEnabled(any_value)

    def save_manual(self):
        t = self.manual_table
        saved = 0
        for i in range(t.rowCount()):
            key = t.item(i, 0).data(Qt.ItemDataRole.UserRole)
            v = t.cellWidget(i, 1).value()
            if v > 0:
                self.host.save_head_rate(key[0], key[1], self.manual_rate(v), "timed run")
                saved += 1
        if saved:
            QMessageBox.information(self, "Saved", f"Saved new rates for {saved} head(s).")
        self.grid.refresh()
