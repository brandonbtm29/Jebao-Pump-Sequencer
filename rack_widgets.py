"""The Manual tab (pick heads, dispense) and the Rack tab (arrange pumps, name heads).

Both tabs talk to the main window (``host``), which owns the settings, the pump list
and the motor runs. The host provides:

- ``config`` (ConfigManager), ``cloud_devices`` (list of dicts from the cloud)
- ``get_device_name(did)``, ``rack_layout()``, ``save_rack_layout(layout)``
- ``run_state(did, ch)`` -> None or (state, progress, seconds_left)
- ``dispense(heads, value, unit)``, ``pause_heads(heads)``, ``resume_heads(heads)``,
  ``cancel_heads(heads)``
- ``names_changed()`` (pump or head names were edited)
- ``current_running_sequence`` and ``sequence_queue``
"""

from PyQt6.QtCore import QMimeData, Qt, pyqtSignal
from PyQt6.QtGui import QDrag
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

import rack

MIME_DID = "application/x-jebao-pump"
LIMIT_CHOICES = (5, 10, 15, 20, 25, 30, 40, 45, 50)  # per-head on-time limits offered (minutes)
TILE_SIZES = {
    "S": {"height": 62, "name_px": 12, "small_px": 10},
    "M": {"height": 82, "name_px": 13, "small_px": 11},
    "L": {"height": 108, "name_px": 16, "small_px": 13},
}
COLORS = {
    "running": "#2FB36D",
    "paused": "#E0A33A",
    "cooling": "#4FA3D1",
    "idle": "#6B7079",
    "offline": "#6B7079",
    "selected": "#4C8DFF",
    "warn": "#E67E22",
}


def pump_online(host, did):
    for d in host.cloud_devices:
        if d.get("did") == did:
            return bool(d.get("is_online", False))
    return False


def is_uncalibrated(pump, ch):
    """True for a head still on the 1.0 mL/min default with no calibration date."""
    rate = (pump.get("calibration") or [1.0] * 4)[ch - 1]
    dates = pump.get("calibrated_at") or []
    return rate == 1.0 and not (ch <= len(dates) and dates[ch - 1])


def fmt_seconds(seconds):
    seconds = int(round(seconds))
    if seconds >= 3600:
        return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"
    return f"{seconds // 60}:{seconds % 60:02d}"


# --- Manual tab ------------------------------------------------------------------------------


class HeadTile(QPushButton):
    """One pump head: click to select it. Shows its name, rate and what it is doing."""

    def __init__(self, did, ch, size="M", parent=None):
        super().__init__(parent)
        self.did, self.ch = did, ch
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(3)
        top = QHBoxLayout()
        self.dot = QLabel("●")
        self.lbl_ch = QLabel(f"CH{ch}")
        self.lbl_rate = QLabel()
        top.addWidget(self.dot)
        top.addWidget(self.lbl_ch)
        top.addStretch(1)
        top.addWidget(self.lbl_rate)
        lay.addLayout(top)
        self.lbl_name = QLabel()
        self.lbl_name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        lay.addWidget(self.lbl_name)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(4)
        lay.addWidget(self.bar)
        self.lbl_status = QLabel()
        self.lbl_status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        lay.addWidget(self.lbl_status)
        for w in (self.dot, self.lbl_ch, self.lbl_rate, self.lbl_name, self.lbl_status, self.bar):
            w.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._shown = None
        self.apply_size(size)

    def apply_size(self, size):
        spec = TILE_SIZES.get(size, TILE_SIZES["M"])
        self.setFixedHeight(spec["height"])
        self.lbl_name.setStyleSheet(f"font-size: {spec['name_px']}px; font-weight: 600;")
        small = f"font-size: {spec['small_px']}px;"
        for w in (self.lbl_ch, self.lbl_rate, self.lbl_status):
            w.setStyleSheet(small + " color: grey;")
        self._shown = None

    def show_state(self, name, rate_text, uncalibrated, online, run, status=None):
        """Update the tile; ``run`` is None or (state, progress, seconds_left).

        ``status`` = (text, color, progress or None) replaces the usual status line (the
        Calibration tab uses it to show each head's calibration).
        """
        if not online:
            state, text, color = "offline", "Pump offline", COLORS["offline"]
        elif status is not None:
            text, color, progress = status
            state = "status"
            run = None if progress is None else ("running", progress, 0)
        elif run is None:
            state = "idle"
            text, color = (
                ("Idle · not calibrated", COLORS["warn"]) if uncalibrated else ("Idle", COLORS["idle"])
            )
        else:
            state, progress, left = run
            color = COLORS[state]
            if state == "cooling":
                text = f"Resting (on-time limit) · back in {fmt_seconds(left)}"
            else:
                text = f"{'Running' if state == 'running' else 'Paused'} · {fmt_seconds(left)} left"
        shown = (
            name,
            rate_text,
            uncalibrated,
            state,
            text,
            color,
            self.isChecked(),
            run and round(run[1], 3),
        )
        if shown == self._shown:
            return
        self._shown = shown
        self.setEnabled(online)
        self.lbl_name.setText(name)
        self.lbl_rate.setText(rate_text)
        self.lbl_status.setText(text)
        self.dot.setStyleSheet(f"color: {color};")
        self.bar.setValue(int((run[1] if run else 0) * 1000))
        self.bar.setStyleSheet(
            "QProgressBar { border: none; background: rgba(128,128,128,0.25); border-radius: 2px; }"
            f"QProgressBar::chunk {{ background: {COLORS.get(state, COLORS['running']) if state in ('paused', 'cooling') else COLORS['running']}; }}"
        )
        border = COLORS["selected"] if self.isChecked() else ("#8A8F98" if state != "offline" else "#55595F")
        width = 2 if self.isChecked() else 1
        style = "dashed" if state == "offline" else "solid"
        self.setStyleSheet(
            f"HeadTile {{ text-align: left; border: {width}px {style} {border}; border-radius: 8px;"
            f" background: {'rgba(76,141,255,0.18)' if self.isChecked() else 'rgba(128,128,128,0.10)'}; }}"
        )
        tip = f"{name or 'CH' + str(self.ch)} · {rate_text} mL/min"
        if uncalibrated:
            tip += " · not calibrated yet (Calibration tab)"
        self.setToolTip(tip)
        label = f"Channel {self.ch}, {name}, {text}" + (", selected" if self.isChecked() else "")
        self.setAccessibleName(label)


class PumpCard(QFrame):
    """One pump: its name and its four head tiles."""

    def __init__(self, tab, did, parent=None):
        super().__init__(parent)
        self.tab, self.did = tab, did
        self.setFrameShape(QFrame.Shape.StyledPanel)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 8)
        head = QHBoxLayout()
        self.dot = QLabel("●")
        self.lbl = QLabel()
        self.lbl.setStyleSheet("font-weight: 600;")
        self.note = QLabel()
        self.note.setStyleSheet("color: grey;")
        btn_all = QPushButton("All 4")
        btn_all.setToolTip("Select or clear all four heads of this pump.")
        btn_all.clicked.connect(lambda: tab.toggle_pump(did))
        head.addWidget(self.dot)
        head.addWidget(self.lbl)
        head.addWidget(self.note)
        head.addStretch(1)
        head.addWidget(btn_all)
        lay.addLayout(head)
        grid = QGridLayout()
        grid.setSpacing(6)
        self.tiles = []
        for ch in range(1, 5):
            tile = HeadTile(did, ch, tab.tile_size)
            tile.setChecked((did, ch) in tab.selected)
            tile.toggled.connect(lambda on, key=(did, ch): tab.set_selected(key, on))
            grid.addWidget(tile, 0, ch - 1)
            self.tiles.append(tile)
        lay.addLayout(grid)

    def refresh(self):
        host = self.tab.host
        online = pump_online(host, self.did)
        self.lbl.setText(host.get_device_name(self.did))
        self.dot.setStyleSheet(f"color: {COLORS['running'] if online else COLORS['offline']};")
        self.note.setText("" if online else "offline: check its Wi-Fi")
        pump = host.config.get_pump(self.did)
        status_for = self.tab.status_for
        for tile in self.tiles:
            rate = pump["calibration"][tile.ch - 1]
            tile.show_state(
                host.config.head_name(self.did, tile.ch) or f"Channel {tile.ch}",
                f"{rate:.2f}",
                is_uncalibrated(pump, tile.ch),
                online,
                host.run_state(self.did, tile.ch),
                status_for(self.did, tile.ch) if status_for else None,
            )


class HeadGrid(QWidget):
    """The rack as head tiles that can be selected: shared by the Manual and Calibration tabs.

    ``status_for(did, ch)`` (optional) gives a tile's status line; see HeadTile.show_state.
    Emits ``selection_changed`` when heads are picked or dropped.
    """

    selection_changed = pyqtSignal()

    def __init__(self, host, hint="", status_for=None, scroll=True, parent=None):
        """``scroll=False``: no scroll bar of its own, for a tab that scrolls the grid
        together with other things. ``toolbar`` (name, hint, tile size) can then be placed
        above that tab's scroll area so it stays in view."""
        super().__init__(parent)
        self.host = host
        self.selected = set()
        self.status_for = status_for
        self._container = None
        ui = host.config.data.setdefault("ui", {})
        self.tile_size = ui.get("tile_size", "M") if ui.get("tile_size") in TILE_SIZES else "M"
        self.cards = []

        left = QVBoxLayout(self)
        left.setContentsMargins(0, 0, 0, 0)
        self.toolbar = QWidget()
        self.top = QHBoxLayout(self.toolbar)
        self.top.setContentsMargins(0, 0, 0, 0)
        self.lbl_layout = QLabel()
        self.lbl_layout.setStyleSheet("font-size: 15px; font-weight: 600;")
        self.top.addWidget(self.lbl_layout)
        self.lbl_hint = QLabel(hint)
        self.lbl_hint.setStyleSheet("color: grey;")
        self.top.addWidget(self.lbl_hint)
        self.top.addStretch(1)
        self.top.addWidget(QLabel("Tile size:"))
        self.size_group = QButtonGroup(self)
        self.size_buttons = {}
        for key in ("S", "M", "L"):
            b = QPushButton(key)
            b.setCheckable(True)
            b.setFixedWidth(34)
            b.setChecked(key == self.tile_size)
            b.setStyleSheet(
                "QPushButton:checked { background-color: #2F6FD6; color: white; font-weight: bold; }"
            )
            b.setToolTip({"S": "Small tiles", "M": "Medium tiles", "L": "Large tiles"}[key])
            b.clicked.connect(lambda _c, k=key: self.set_tile_size(k))
            self.size_group.addButton(b)
            self.size_buttons[key] = b
            self.top.addWidget(b)
        left.addWidget(self.toolbar)

        if scroll:
            self.scroll = QScrollArea()
            self.scroll.setWidgetResizable(True)
            self.scroll.setFrameShape(QFrame.Shape.NoFrame)
            left.addWidget(self.scroll, 1)
        else:
            self.scroll = None
            self.body = QVBoxLayout()
            left.addLayout(self.body)

    def rebuild(self):
        """Recreate the stacks and pump cards (after the layout or the pump list changed)."""
        layout = self.host.rack_layout()
        dids = [d.get("did") for d in self.host.cloud_devices]
        known = set(dids)
        self.lbl_layout.setText(layout["name"])
        container = QWidget()
        row = QHBoxLayout(container)
        row.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.cards = []
        groups = [(s["name"], [d for d in s["slots"] if d in known]) for s in layout["stacks"]]
        extra = rack.unplaced(layout, dids)
        if extra:
            groups.append(("Not in the rack (place them in the Rack tab)", extra))
        for title, members in groups:
            if not members and title.startswith("Not in"):
                continue
            box = QGroupBox(title)
            col = QVBoxLayout(box)
            col.setAlignment(Qt.AlignmentFlag.AlignTop)
            for did in members:
                card = PumpCard(self, did)
                col.addWidget(card)
                self.cards.append(card)
            if not members:
                empty = QLabel("No pumps here yet. Add them in the Rack tab.")
                empty.setStyleSheet("color: grey;")
                col.addWidget(empty)
            row.addWidget(box, 1)
        if not self.cards:
            msg = QLabel("No pumps yet. Enter your Jebao login in Settings, then press Refresh.")
            msg.setStyleSheet("color: grey; font-size: 14px;")
            row.addWidget(msg)
        self.selected = {k for k in self.selected if k[0] in known}
        if self.scroll is not None:
            self.scroll.setWidget(container)
        else:
            if self._container is not None:
                self._container.hide()
                self._container.deleteLater()
            self.body.addWidget(container)
        self._container = container
        self.refresh()

    def refresh(self):
        for card in self.cards:
            card.refresh()

    def set_tile_size(self, size, save=True):
        self.tile_size = size
        if save:
            self.host.config.data.setdefault("ui", {})["tile_size"] = size
            self.host.config.save()
            notify = getattr(self.host, "tile_size_changed", None)
            if notify:
                notify(size, self)
        self.size_buttons[size].setChecked(True)
        for card in self.cards:
            for tile in card.tiles:
                tile.apply_size(size)
        self.refresh()

    # --- selection ---

    def tiles(self):
        return [t for c in self.cards for t in c.tiles]

    def set_selected(self, key, on):
        if on:
            self.selected.add(key)
        else:
            self.selected.discard(key)
        self.refresh()
        self.selection_changed.emit()

    def toggle_pump(self, did):
        tiles = [t for t in self.tiles() if t.did == did and t.isEnabled()]
        turn_on = not all(t.isChecked() for t in tiles)
        for t in tiles:
            t.setChecked(turn_on)

    def clear_selection(self):
        for t in self.tiles():
            t.setChecked(False)
        self.selected.clear()
        self.refresh()
        self.selection_changed.emit()

    def select(self, keys):
        keys = set(keys)
        for t in self.tiles():
            t.setChecked((t.did, t.ch) in keys and t.isEnabled())


class ManualTab(QWidget):
    """The rack as head tiles. Select heads, then dispense a volume or run them for a time."""

    def __init__(self, host, parent=None):
        super().__init__(parent)
        self.host = host
        outer = QHBoxLayout(self)
        self.grid = HeadGrid(host, "Click heads to select them, then dispense on the right.")
        self.grid.selection_changed.connect(self._update_panel)
        outer.addWidget(self.grid, 1)
        outer.addWidget(self._build_panel())
        self.rebuild()

    # The grid does the selecting; these keep the tab's own names working.
    selected = property(lambda self: self.grid.selected)
    cards = property(lambda self: self.grid.cards)
    tile_size = property(lambda self: self.grid.tile_size)

    def tiles(self):
        return self.grid.tiles()

    def set_tile_size(self, size, save=True):
        self.grid.set_tile_size(size, save)

    def toggle_pump(self, did):
        self.grid.toggle_pump(did)

    def clear_selection(self):
        self.grid.clear_selection()

    def select(self, keys):
        self.grid.select(keys)

    def rebuild(self):
        self.grid.rebuild()
        self._update_panel()

    def refresh(self):
        """Update tiles and the panel from the current runs (called a few times a second)."""
        self.grid.refresh()
        self._update_panel()

    # --- right-hand panel ---

    def _build_panel(self):
        panel = QWidget()
        panel.setFixedWidth(320)
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(0, 0, 0, 0)
        box = QGroupBox("Dispense")
        form = QVBoxLayout(box)
        row = QHBoxLayout()
        self.lbl_count = QLabel()
        self.lbl_count.setStyleSheet("font-weight: 600;")
        row.addWidget(self.lbl_count)
        row.addStretch(1)
        btn_clear = QPushButton("Clear")
        btn_clear.clicked.connect(self.clear_selection)
        row.addWidget(btn_clear)
        form.addLayout(row)
        self.lbl_selection = QLabel()
        self.lbl_selection.setWordWrap(True)
        self.lbl_selection.setStyleSheet("color: grey;")
        form.addWidget(self.lbl_selection)

        ui = self.host.config.data.setdefault("ui", {})
        mode_row = QHBoxLayout()
        self.rb_volume = QRadioButton("Volume (mL)")
        self.rb_time = QRadioButton("Time (s)")
        (self.rb_time if ui.get("dispense_unit") == "time" else self.rb_volume).setChecked(True)
        self.rb_volume.toggled.connect(self._mode_changed)
        mode_row.addWidget(self.rb_volume)
        mode_row.addWidget(self.rb_time)
        form.addLayout(mode_row)
        self.spin_amount = QDoubleSpinBox()
        self.spin_amount.setRange(0.01, 100000.0)
        self.spin_amount.setDecimals(2)
        self.spin_amount.setValue(float(ui.get("dispense_amount", 10.0)))
        self.spin_amount.valueChanged.connect(self._amount_changed)
        form.addWidget(self.spin_amount)
        self.lbl_estimate = QLabel()
        self.lbl_estimate.setWordWrap(True)
        self.lbl_estimate.setStyleSheet("color: grey;")
        form.addWidget(self.lbl_estimate)
        self.btn_dispense = QPushButton()
        self.btn_dispense.setStyleSheet(
            "QPushButton { background-color: #2F6FD6; color: white; font-weight: bold; padding: 10px; }"
            "QPushButton:disabled { background-color: #555A62; color: #C9CCD2; }"
        )
        self.btn_dispense.clicked.connect(self.dispense)
        form.addWidget(self.btn_dispense)
        ctl = QHBoxLayout()
        self.btn_pause = QPushButton("Pause")
        self.btn_pause.setToolTip("Pause the selected heads that are running (motor off until you resume).")
        self.btn_pause.clicked.connect(lambda: self.host.pause_heads(sorted(self.selected)))
        self.btn_resume = QPushButton("Resume")
        self.btn_resume.clicked.connect(lambda: self.host.resume_heads(sorted(self.selected)))
        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.setToolTip(
            "Stop the selected heads. A head that is part of a running sequence stops that sequence."
        )
        self.btn_cancel.clicked.connect(lambda: self.host.cancel_heads(sorted(self.selected)))
        for b in (self.btn_pause, self.btn_resume, self.btn_cancel):
            ctl.addWidget(b)
        form.addLayout(ctl)
        lay.addWidget(box)

        running = QGroupBox("Running now")
        rl = QVBoxLayout(running)
        self.lbl_running = QLabel()
        self.lbl_running.setWordWrap(True)
        rl.addWidget(self.lbl_running)
        lay.addWidget(running)
        lay.addStretch(1)
        return panel

    def _mode_changed(self):
        self.host.config.data.setdefault("ui", {})["dispense_unit"] = self.unit()
        self.host.config.save()
        self._update_panel()

    def _amount_changed(self, value):
        self.host.config.data.setdefault("ui", {})["dispense_amount"] = value
        self._update_panel()

    def unit(self):
        return "time" if self.rb_time.isChecked() else "volume"

    # --- actions ---

    def dispense(self):
        heads = sorted(self.selected)
        if heads:
            self.host.config.save()
            self.host.dispense(heads, self.spin_amount.value(), self.unit())

    def _update_panel(self):
        host = self.host
        heads = sorted(self.selected)
        n = len(heads)
        self.lbl_count.setText(f"{n} head{'s' if n != 1 else ''} selected")
        names = [f"{host.get_device_name(d)} · CH{c}" for d, c in heads]
        self.lbl_selection.setText(
            ", ".join(names[:12]) + (f" … and {n - 12} more" if n > 12 else "") or "None"
        )
        value = self.spin_amount.value()
        if self.unit() == "time":
            self.spin_amount.setSuffix(" s")
            vols = [value * host.config.get_pump(d)["calibration"][c - 1] / 60.0 for d, c in heads]
            est = (
                f"About {min(vols):.2f}–{max(vols):.2f} mL per head at the calibrated rates." if vols else ""
            )
        else:
            self.spin_amount.setSuffix(" mL")
            secs = []
            for d, c in heads:
                rate = host.config.get_pump(d)["calibration"][c - 1]
                if rate and rate > 0:
                    secs.append(value / rate * 60.0)
            est = (
                f"Each head runs {fmt_seconds(min(secs))}–{fmt_seconds(max(secs))} (m:ss) at its own rate."
                if secs
                else ""
            )
        if n:
            est += " All start together."
        self.lbl_estimate.setText(est)
        self.btn_dispense.setEnabled(n > 0)
        self.btn_dispense.setText(
            f"Dispense on {n} head{'s' if n != 1 else ''}" if n else "Select heads to dispense"
        )
        running = [h for h in heads if host.run_state(*h)]
        self.btn_pause.setEnabled(bool(running))
        self.btn_resume.setEnabled(
            any(host.run_state(*h) and host.run_state(*h)[0] == "paused" for h in heads)
        )
        self.btn_cancel.setEnabled(bool(running))
        seq = host.current_running_sequence
        queue = [q.get("name", "?") for q in host.sequence_queue]
        lines = [f"Sequence: {seq}" if seq else "No sequence running."]
        if queue:
            lines.append("Queued next: " + ", ".join(queue))
        active = len(getattr(host, "head_runs", {}))
        if active:
            lines.append(f"{active} head{'s' if active != 1 else ''} running.")
        self.lbl_running.setText("\n".join(lines))


# --- Rack tab --------------------------------------------------------------------------------


def _drag_did(widget, did):
    mime = QMimeData()
    mime.setData(MIME_DID, did.encode("utf-8"))
    drag = QDrag(widget)
    drag.setMimeData(mime)
    drag.exec(Qt.DropAction.MoveAction)


def _dropped_did(event):
    data = event.mimeData()
    if data.hasFormat(MIME_DID):
        return bytes(data.data(MIME_DID)).decode("utf-8")
    return None


class SlotWidget(QFrame):
    """One place in a stack. Drop a pump on it; drag it out; click it to name it."""

    clicked = pyqtSignal(object)

    def __init__(self, tab, stack, index, did, parent=None):
        super().__init__(parent)
        self.tab, self.stack, self.index, self.did = tab, stack, index, did
        self.setAcceptDrops(True)
        self.setMinimumHeight(44)
        self._press = None
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 4, 6, 4)
        pos = QLabel(str(index + 1))
        pos.setStyleSheet("color: grey;")
        pos.setFixedWidth(18)
        lay.addWidget(pos)
        if did:
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            self.lbl = QLabel("⠿  " + tab.host.get_device_name(did))
            lay.addWidget(self.lbl, 1)
            btn = QPushButton("×")
            btn.setFixedWidth(28)
            btn.setToolTip("Take this pump out of the rack")
            btn.setAccessibleName(f"Remove {tab.host.get_device_name(did)} from the rack")
            btn.clicked.connect(lambda: tab.remove(did))
            lay.addWidget(btn)
        else:
            self.lbl = QLabel("Drag a pump here")
            self.lbl.setStyleSheet("color: grey;")
            lay.addWidget(self.lbl, 1)
        self._style(False)

    def _style(self, hover):
        selected = self.did and self.did == self.tab.current
        if self.did:
            border = "2px solid #4C8DFF" if selected or hover else "1px solid #8A8F98"
            self.setStyleSheet(f"SlotWidget {{ border: {border}; border-radius: 8px; }}")
        else:
            color = "#4C8DFF" if hover or self.tab.picked else "#8A8F98"
            self.setStyleSheet(f"SlotWidget {{ border: 1.5px dashed {color}; border-radius: 8px; }}")

    def mousePressEvent(self, event):
        self._press = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.did and self._press is not None:
            if (
                event.position().toPoint() - self._press
            ).manhattanLength() >= QApplication.startDragDistance():
                self._press = None
                _drag_did(self, self.did)
                return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._press is not None:
            self.tab.slot_clicked(self)
        self._press = None
        super().mouseReleaseEvent(event)

    def dragEnterEvent(self, event):
        if _dropped_did(event):
            event.acceptProposedAction()
            self._style(True)

    def dragLeaveEvent(self, event):
        self._style(False)

    def dropEvent(self, event):
        did = _dropped_did(event)
        if did:
            event.acceptProposedAction()
            self.tab.place(did, self.stack, self.index)


class TrayList(QListWidget):
    """Pumps that are not in the rack. Drag them into a slot, or drop a slot's pump here."""

    def __init__(self, tab, parent=None):
        super().__init__(parent)
        self.tab = tab
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)

    def startDrag(self, supported):
        item = self.currentItem()
        if item is not None:
            _drag_did(self, item.data(Qt.ItemDataRole.UserRole))

    def dragEnterEvent(self, event):
        if _dropped_did(event):
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        if _dropped_did(event):
            event.acceptProposedAction()

    def dropEvent(self, event):
        did = _dropped_did(event)
        if did:
            event.acceptProposedAction()
            self.tab.remove(did)


class RackTab(QWidget):
    """Set up the physical arrangement: stacks, which pump goes where, and names."""

    def __init__(self, host, parent=None):
        super().__init__(parent)
        self.host = host
        self.current = None  # pump whose names are being edited
        self.picked = None  # pump picked in the tray, for click-to-place

        outer = QVBoxLayout(self)
        bar = QHBoxLayout()
        title = QLabel("Physical setup")
        title.setStyleSheet("font-size: 15px; font-weight: 600;")
        bar.addWidget(title)
        hint = QLabel("Manual and Sequencer show the pumps in this arrangement.")
        hint.setStyleSheet("color: grey;")
        bar.addWidget(hint)
        bar.addStretch(1)
        bar.addWidget(QLabel("Name:"))
        self.edit_name = QLineEdit()
        self.edit_name.setFixedWidth(160)
        self.edit_name.editingFinished.connect(self._rename_layout)
        bar.addWidget(self.edit_name)
        bar.addWidget(QLabel("Stacks:"))
        self.spin_stacks = QSpinBox()
        self.spin_stacks.setRange(1, rack.MAX_STACKS)
        self.spin_stacks.valueChanged.connect(self._resize)
        bar.addWidget(self.spin_stacks)
        bar.addWidget(QLabel("Pumps per stack:"))
        self.spin_slots = QSpinBox()
        self.spin_slots.setRange(1, rack.MAX_SLOTS)
        self.spin_slots.valueChanged.connect(self._resize)
        bar.addWidget(self.spin_slots)
        btn_auto = QPushButton("Auto-arrange A→Z")
        btn_auto.setToolTip("Fill the stacks with every pump in name order (JP 2 before JP 10).")
        btn_auto.clicked.connect(self.auto_arrange)
        bar.addWidget(btn_auto)
        outer.addLayout(bar)

        body = QHBoxLayout()
        tray_box = QGroupBox("Not placed")
        tl = QVBoxLayout(tray_box)
        self.chk_sort = QCheckBox("Sort A→Z")
        self.chk_sort.setChecked(bool(host.config.data.setdefault("ui", {}).get("tray_sorted", True)))
        self.chk_sort.toggled.connect(self._sort_toggled)
        tl.addWidget(self.chk_sort)
        self.tray = TrayList(self)
        self.tray.itemClicked.connect(self._tray_clicked)
        tl.addWidget(self.tray, 1)
        tray_hint = QLabel(
            "Drag a pump into a slot, or click it and then click an empty slot. New pumps appear here."
        )
        tray_hint.setWordWrap(True)
        tray_hint.setStyleSheet("color: grey;")
        tl.addWidget(tray_hint)
        tray_box.setFixedWidth(240)
        self.tray_box = tray_box
        body.addWidget(tray_box)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        body.addWidget(self.scroll, 1)

        names = QGroupBox("Names")
        names.setFixedWidth(300)
        nl = QVBoxLayout(names)
        self.lbl_current = QLabel("Click a pump to name it and its heads.")
        self.lbl_current.setWordWrap(True)
        nl.addWidget(self.lbl_current)
        form = QFormLayout()
        self.edit_pump = QLineEdit()
        self.edit_pump.editingFinished.connect(self._save_names)
        form.addRow("Pump name:", self.edit_pump)
        self.edit_heads = []
        self.limit_combos = []
        for ch in range(1, 5):
            e = QLineEdit()
            e.setPlaceholderText("e.g. Water → Col 3")
            e.editingFinished.connect(self._save_names)
            form.addRow(f"CH{ch}:", e)
            self.edit_heads.append(e)
            combo = QComboBox()
            combo.addItem("Default on-time limit", None)
            combo.addItem("No limit", "off")
            for m in LIMIT_CHOICES:
                combo.addItem(f"At most {m} min", m)
            combo.setToolTip(
                "How long this head may run in any window (the window and the default are in "
                "Settings → Pump safety). A dose that reaches it rests, then carries on."
            )
            combo.currentIndexChanged.connect(lambda _i, ch=ch: self._save_limit(ch))
            form.addRow("", combo)
            self.limit_combos.append(combo)
        nl.addLayout(form)
        tip = QLabel(
            "Head names show on the Manual tiles and in the log. Leave a name empty to use the default."
        )
        tip.setWordWrap(True)
        tip.setStyleSheet("color: grey;")
        nl.addWidget(tip)
        nl.addStretch(1)
        body.addWidget(names)
        outer.addLayout(body, 1)
        self._set_names_enabled(False)
        self.rebuild()

    # --- building ---

    def dids(self):
        return [d.get("did") for d in self.host.cloud_devices]

    def rebuild(self):
        layout = self.host.rack_layout()
        for w, v in ((self.spin_stacks, len(layout["stacks"])), (self.spin_slots, layout["slots_per_stack"])):
            w.blockSignals(True)
            w.setValue(v)
            w.blockSignals(False)
        if not self.edit_name.hasFocus():
            self.edit_name.setText(layout["name"])

        tray = rack.unplaced(layout, self.dids())
        if self.chk_sort.isChecked():
            tray.sort(key=lambda d: rack.natural_key(self.host.get_device_name(d)))
        self.tray.clear()
        for did in tray:
            item = QListWidgetItem("⠿  " + self.host.get_device_name(did))
            item.setData(Qt.ItemDataRole.UserRole, did)
            item.setToolTip("Drag into a slot")
            self.tray.addItem(item)
            if did == self.picked:
                item.setSelected(True)
        if self.picked not in tray:
            self.picked = None
        self.tray_box.setTitle(f"Not placed ({len(tray)})")

        container = QWidget()
        row = QHBoxLayout(container)
        row.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.slots = []
        for si, stack in enumerate(layout["stacks"]):
            box = QGroupBox()
            col = QVBoxLayout(box)
            col.setAlignment(Qt.AlignmentFlag.AlignTop)
            name = QLineEdit(stack["name"])
            name.setAccessibleName("Stack name")
            name.editingFinished.connect(lambda si=si, e=name: self._rename_stack(si, e.text()))
            col.addWidget(name)
            for i, did in enumerate(stack["slots"]):
                slot = SlotWidget(self, si, i, did)
                col.addWidget(slot)
                self.slots.append(slot)
            row.addWidget(box, 1)
        self.scroll.setWidget(container)
        if self.current and self.current not in self.dids():
            self.current = None
        self._load_names()

    def _apply(self, layout):
        self.host.save_rack_layout(layout)  # the host saves and refreshes both tabs

    # --- editing the layout ---

    def place(self, did, stack, slot):
        self.picked = None
        self._apply(rack.place(self.host.rack_layout(), did, stack, slot))

    def remove(self, did):
        self._apply(rack.remove(self.host.rack_layout(), did))

    def auto_arrange(self):
        names = {d: self.host.get_device_name(d) for d in self.dids()}
        self._apply(rack.auto_arrange(self.host.rack_layout(), self.dids(), names))

    def _resize(self):
        self._apply(rack.resize(self.host.rack_layout(), self.spin_stacks.value(), self.spin_slots.value()))

    def _rename_layout(self):
        layout = self.host.rack_layout()
        text = self.edit_name.text().strip() or rack.DEFAULT_NAME
        if text != layout["name"]:
            layout["name"] = text
            self._apply(layout)

    def _rename_stack(self, index, text):
        layout = self.host.rack_layout()
        text = text.strip() or rack.stack_name(index)
        if index < len(layout["stacks"]) and text != layout["stacks"][index]["name"]:
            layout["stacks"][index]["name"] = text
            self._apply(layout)

    def _sort_toggled(self, on):
        self.host.config.data.setdefault("ui", {})["tray_sorted"] = bool(on)
        self.host.config.save()
        self.rebuild()

    # --- clicks: pick from the tray, place, or choose a pump to name ---

    def _tray_clicked(self, item):
        did = item.data(Qt.ItemDataRole.UserRole)
        self.picked = None if self.picked == did else did
        self.current = did
        self._load_names()
        for slot in self.slots:
            slot._style(False)

    def slot_clicked(self, slot):
        if slot.did is None:
            if self.picked:
                self.place(self.picked, slot.stack, slot.index)
            return
        self.current = slot.did
        self.picked = None
        for s in self.slots:
            s._style(False)
        self._load_names()

    # --- names ---

    def _set_names_enabled(self, on):
        for e in [self.edit_pump, *self.edit_heads, *getattr(self, "limit_combos", [])]:
            e.setEnabled(on)

    def _save_limit(self, ch):
        did = self.current
        if not did or getattr(self, "_loading", False):
            return
        pump = self.host.config.get_pump(did)
        limits = (list(pump.get("limits") or []) + [None] * 4)[:4]
        limits[ch - 1] = self.limit_combos[ch - 1].currentData()
        pump["limits"] = limits
        self.host.config.save()

    def _load_names(self):
        did = self.current
        self._set_names_enabled(bool(did))
        if not did:
            self.lbl_current.setText("Click a pump to name it and its heads.")
            for e in [self.edit_pump, *self.edit_heads]:
                e.clear()
            return
        cfg = self.host.config
        cloud = next(
            (d.get("dev_alias") or d.get("mac") for d in self.host.cloud_devices if d.get("did") == did), did
        )
        self.lbl_current.setText(f"<b>{self.host.get_device_name(did)}</b>")
        self.edit_pump.setPlaceholderText(cloud)
        self.edit_pump.setText(cfg.display_name(did))
        for ch, e in enumerate(self.edit_heads, start=1):
            e.setText(cfg.head_name(did, ch))
        limits = (list(cfg.get_pump(did).get("limits") or []) + [None] * 4)[:4]
        self._loading = True
        try:
            for combo, value in zip(self.limit_combos, limits, strict=True):
                idx = combo.findData(value)
                combo.setCurrentIndex(idx if idx >= 0 else 0)
        finally:
            self._loading = False

    def _save_names(self):
        did = self.current
        if not did:
            return
        cfg = self.host.config
        changed = False
        name = self.edit_pump.text().strip()
        if name != cfg.display_name(did):
            cfg.get_pump(did)["display_name"] = name
            changed = True
        for ch, e in enumerate(self.edit_heads, start=1):
            if e.text().strip() != cfg.head_name(did, ch):
                cfg.set_head_name(did, ch, e.text())
                changed = True
        if changed:
            cfg.save()
            self.host.names_changed()
