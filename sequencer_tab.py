"""The Sequencer tab: build a sequence, preview what it will do, and follow a run.

Three pages, picked with the buttons at the top:

- **Build:** saved sequences and the queue on the left; the open sequence's steps as
  cards in the middle (drag to reorder, select several to copy or delete); the selected
  step's settings on the right, with a head picker by name from the Rack tab; and the
  "Before you run" checks.
- **Preview:** total time (with and without the on-time limit), the finish time for a
  start time, when you're needed, the liquid to prepare, a timeline per lane and a table
  per head. Worked out by ``sequence.plan()``: nothing is sent to the pumps.
- **Running:** the run's progress and times, each lane's current step with pause, skip and
  Continue, a step × lane grid, what's queued next and the recent runs.

The steps, plans and run state are in ``sequence.py``. The main window (``host``) runs the
sequences and provides: ``config``, ``cloud_devices``, ``rack_layout()``,
``get_device_name(did)``, ``head_label(did, ch)``, ``run_state(did, ch)``,
``plan_sequence(steps, name, with_limit)``, ``plan_context()``, ``enqueue(name, steps,
front)``, ``run_active()``, ``stop_run()``, ``pause_lane(ch, paused)``, ``pause_run(paused)``,
``skip_step(ch)``, ``approve_channel(ch)``, ``set_queue_paused(paused)``, and the state
``seq_run``, ``sequence_queue``, ``queue_paused``, ``current_running_sequence``,
``_ch_paused``, ``_ch_review_events``, ``run_history_path``.
"""

import copy
import json
import logging
import os
import time
from datetime import datetime, timedelta

from PyQt6.QtCore import (
    QItemSelectionModel,
    QPoint,
    QPointF,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTime,
    QTimer,
    pyqtSignal,
)
from PyQt6.QtGui import QBrush, QColor, QFont, QKeySequence, QPainter, QPalette, QPen, QPolygonF, QShortcut
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QTimeEdit,
    QToolButton,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

import rack
import sequence as sq

logger = logging.getLogger("JebaoGUI")

UNTITLED = "Untitled sequence"
BADGE = {
    "dose": "DOSE",
    "staccato": "STACCATO",
    "wait": "WAIT",
    "review": "WAIT FOR ME",
    "sync": "SYNC",
    "sequence": "SEQUENCE",
}
# (background, text) for each kind's badge, on the dark and the light theme
KIND_DARK = {
    "dose": ("#1B2A45", "#B3CEFF"),
    "staccato": ("#2A2140", "#D2BDF7"),
    "wait": ("#2C2F35", "#C9CCD2"),
    "review": ("#3A2F14", "#F2CF86"),
    "sync": ("#183020", "#BFE8CF"),
    "sequence": ("#2C2F35", "#C9CCD2"),
}
KIND_LIGHT = {
    "dose": ("#DCE8FF", "#1D4FA8"),
    "staccato": ("#ECE2FB", "#5B3A9A"),
    "wait": ("#E6E7EA", "#3A3E46"),
    "review": ("#FBEFD2", "#7A5200"),
    "sync": ("#DDF3E6", "#1E6B3E"),
    "sequence": ("#E6E7EA", "#3A3E46"),
}
SEGMENT = {  # timeline colours
    "pump": "#4C8DFF",
    "staccato": "#A67CF0",
    "gap": "#A67CF0",
    "rest": "#E0A33A",
    "review": "#F08A24",
    "wait": "#7D828C",
    "sync": "#5B6069",
}
CELL = {  # step × lane grid: (text, colour)
    sq.TODO: ("·", "#8A8F98"),
    sq.NOW: ("▶ now", "#4C8DFF"),
    sq.DONE: ("✓", "#2FB36D"),
    sq.SKIPPED: ("skipped", "#E0A33A"),
    sq.FAILED: ("✕ failed", "#E5534B"),
    sq.STOPPED: ("stopped", "#E5534B"),
}
LEVEL = {  # check dot: (mark, background, colour)
    "error": ("✕", "#4A1F1F", "#F3A3A3"),
    "warn": ("!", "#3A2A10", "#F0B65A"),
    "info": ("i", "#1A2438", "#B3CEFF"),
    "ok": ("✓", "#16241C", "#6FD39B"),
}
PRIMARY = (
    "QPushButton { background-color: #2F6FD6; color: white; font-weight: bold; padding: 6px 14px; }"
    "QPushButton:disabled { background-color: #555A62; color: #C9CCD2; }"
)
GO = (
    "QPushButton { background-color: #28A745; color: white; font-weight: bold; padding: 6px 14px; }"
    "QPushButton:disabled { background-color: #555A62; color: #C9CCD2; }"
)
DANGER = "QPushButton { color: #F3A3A3; border: 1px solid #7A3A3A; padding: 6px 12px; border-radius: 4px; }"
SEGMENTED = (
    "QPushButton { padding: 6px 10px; border: 1px solid #4A4E56; border-radius: 6px; }"
    "QPushButton:checked { background-color: #2F6FD6; color: white; border-color: #4C8DFF; }"
)


def is_dark(widget):
    return widget.palette().color(QPalette.ColorRole.Window).lightness() < 128


def grey(text, size=None):
    lbl = QLabel(text)
    lbl.setStyleSheet("color: grey;" + (f" font-size: {size}px;" if size else ""))
    lbl.setWordWrap(True)
    return lbl


def heading(text, size=14):
    lbl = QLabel(text)
    lbl.setStyleSheet(f"font-weight: 600; font-size: {size}px;")
    return lbl


def check_row(check):
    """A check as a row: a coloured mark and the text."""
    row = QWidget()
    lay = QHBoxLayout(row)
    lay.setContentsMargins(0, 2, 0, 2)
    mark, bg, fg = LEVEL.get(check.level, LEVEL["info"])
    dot = QLabel(mark)
    dot.setFixedSize(20, 20)
    dot.setAlignment(Qt.AlignmentFlag.AlignCenter)
    dot.setStyleSheet(
        f"background: {bg}; color: {fg}; border-radius: 10px; font-weight: 700; font-size: 11px;"
    )
    lay.addWidget(dot, 0, Qt.AlignmentFlag.AlignTop)
    text = QLabel(check.text)
    text.setWordWrap(True)
    text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    lay.addWidget(text, 1)
    row.setAccessibleName(f"{check.level}: {check.text}")
    return row


def clear_layout(layout):
    while layout.count():
        item = layout.takeAt(0)
        w = item.widget()
        if w is not None:
            w.setParent(None)
            w.deleteLater()
        elif item.layout() is not None:
            clear_layout(item.layout())


class FlowLayout(QLayout):
    """Lays widgets out in rows, wrapping onto the next row when there's no room."""

    def __init__(self, parent=None, spacing=6):
        super().__init__(parent)
        self._items = []
        self._spacing = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._layout(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._layout(rect, apply=True)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        return size

    def _layout(self, rect, apply):
        x, y, row_h = rect.x(), rect.y(), 0
        for item in self._items:
            hint = item.sizeHint()
            if x + hint.width() > rect.right() + 1 and row_h > 0:
                x, y = rect.x(), y + row_h + self._spacing
                row_h = 0
            if apply:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + self._spacing
            row_h = max(row_h, hint.height())
        return y + row_h - rect.y()


class DurationEdit(QWidget):
    """A time as minutes and seconds."""

    changed = pyqtSignal(float)

    def __init__(self, seconds=0.0, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.minutes = QSpinBox()
        self.minutes.setRange(0, 24 * 60)
        self.minutes.setSuffix(" min")
        self.secs = QDoubleSpinBox()
        self.secs.setRange(0, 59.9)
        self.secs.setDecimals(1)
        self.secs.setSuffix(" s")
        lay.addWidget(self.minutes)
        lay.addWidget(self.secs)
        self.setValue(seconds)
        self.minutes.valueChanged.connect(lambda _v: self.changed.emit(self.value()))
        self.secs.valueChanged.connect(lambda _v: self.changed.emit(self.value()))

    def value(self):
        return self.minutes.value() * 60.0 + self.secs.value()

    def setValue(self, seconds):
        seconds = max(0.0, float(seconds))
        m = int(seconds // 60)
        for w in (self.minutes, self.secs):
            w.blockSignals(True)
        self.minutes.setValue(m)
        self.secs.setValue(round(seconds - m * 60, 1))
        for w in (self.minutes, self.secs):
            w.blockSignals(False)


# --- step cards --------------------------------------------------------------------------


class StepDelegate(QStyledItemDelegate):
    """Paints a step as a card: number, type badge, title and label, heads, time and flags."""

    HEIGHT = 60

    def sizeHint(self, option, index):
        return QSize(200, self.HEIGHT)  # as wide as the list

    def paint(self, painter, option, index):
        card = index.data(Qt.ItemDataRole.UserRole) or {}
        dark = option.palette.color(QPalette.ColorRole.Window).lightness() < 128
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(option.rect).adjusted(2, 3, -2, -3)
        if card.get("background"):
            r.setLeft(r.left() + 28)
        if selected:
            bg, border = ("#1F2D47", "#4C8DFF") if dark else ("#E3ECFF", "#4C8DFF")
        else:
            bg, border = ("#2A2C30", "#45484F") if dark else ("#FFFFFF", "#C9CCD2")
        if card.get("error") and not selected:
            border = "#8A3A3A"
        painter.setPen(QPen(QColor(border), 1.5 if selected else 1))
        painter.setBrush(QColor(bg))
        painter.drawRoundedRect(r, 8, 8)
        text_c = QColor("#E8E9EC" if dark else "#1D1F23")
        grey_c = QColor("#9AA0A9" if dark else "#5B6069")
        base = QFont(option.font)
        x = r.left() + 10
        # drag handle and number
        painter.setPen(grey_c)
        painter.setFont(base)
        painter.drawText(QRectF(x, r.top(), 14, r.height()), Qt.AlignmentFlag.AlignVCenter, "⋮⋮")
        x += 16
        painter.drawText(
            QRectF(x, r.top(), 22, r.height()),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
            str(card.get("n", "")),
        )
        x += 30
        # badge
        kind = card.get("kind", "wait")
        kbg, kfg = (KIND_DARK if dark else KIND_LIGHT).get(kind, KIND_DARK["wait"])
        badge = QRectF(x, r.center().y() - 11, 92, 22)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(kbg))
        painter.drawRoundedRect(badge, 5, 5)
        small = QFont(base)
        small.setPointSizeF(max(7.0, base.pointSizeF() * 0.78))
        small.setBold(True)
        painter.setFont(small)
        painter.setPen(QColor(kfg))
        painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, BADGE.get(kind, kind.upper()))
        x += 104
        # right side: time and flags
        right = r.right() - 10
        time_font = QFont(base)
        painter.setFont(time_font)
        fm = painter.fontMetrics()
        time_text = card.get("time", "")
        tw = fm.horizontalAdvance(time_text) + 4
        painter.setPen(grey_c)
        painter.drawText(
            QRectF(right - tw, r.top() + 8, tw, 20),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            time_text,
        )
        flag_right = right
        pill_font = QFont(base)
        pill_font.setPointSizeF(max(7.0, base.pointSizeF() * 0.8))
        painter.setFont(pill_font)
        pfm = painter.fontMetrics()
        for text, level in reversed(card.get("flags", [])):
            w = pfm.horizontalAdvance(text) + 16
            pill = QRectF(flag_right - w, r.bottom() - 26, w, 18)
            _mark, pbg, pfg = LEVEL.get(level, LEVEL["info"])
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(pbg))
            painter.drawRoundedRect(pill, 9, 9)
            painter.setPen(QColor(pfg))
            painter.drawText(pill, Qt.AlignmentFlag.AlignCenter, text)
            flag_right -= w + 6
        # title, label and heads
        title_font = QFont(base)
        title_font.setBold(True)
        painter.setFont(title_font)
        tfm = painter.fontMetrics()
        room = max(40.0, right - tw - 10 - x)
        title_text = tfm.elidedText(card.get("title", ""), Qt.TextElideMode.ElideRight, int(room))
        painter.setPen(text_c)
        painter.drawText(QRectF(x, r.top() + 8, room, 20), Qt.AlignmentFlag.AlignVCenter, title_text)
        used = tfm.horizontalAdvance(title_text)
        if card.get("label") and used + 30 < room:
            painter.setFont(base)
            painter.setPen(grey_c)
            label = painter.fontMetrics().elidedText(
                " · " + card["label"], Qt.TextElideMode.ElideRight, int(room - used)
            )
            painter.drawText(
                QRectF(x + used, r.top() + 8, room - used, 20), Qt.AlignmentFlag.AlignVCenter, label
            )
        painter.setFont(pill_font)
        painter.setPen(QColor("#E5534B") if card.get("error") else grey_c)
        room2 = max(40.0, flag_right - 8 - x)
        where = pfm.elidedText(card.get("where", ""), Qt.TextElideMode.ElideRight, int(room2))
        painter.drawText(QRectF(x, r.bottom() - 26, room2, 18), Qt.AlignmentFlag.AlignVCenter, where)
        painter.restore()


class LibraryDelegate(QStyledItemDelegate):
    """A saved sequence: its name, and under it the steps, time and last run."""

    def sizeHint(self, option, index):
        if index.data(Qt.ItemDataRole.UserRole) is None:
            return super().sizeHint(option, index)
        return QSize(150, 46)

    def paint(self, painter, option, index):
        name = index.data(Qt.ItemDataRole.UserRole)
        if name is None:
            super().paint(painter, option, index)
            return
        meta = index.data(Qt.ItemDataRole.UserRole + 1) or ""
        is_open = bool(index.data(Qt.ItemDataRole.UserRole + 2))
        dark = option.palette.color(QPalette.ColorRole.Window).lightness() < 128
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(option.rect).adjusted(2, 2, -2, -2)
        if selected or is_open:
            painter.setPen(QPen(QColor("#4C8DFF"), 1.2))
            painter.setBrush(QColor("#1F2D47" if dark else "#E3ECFF"))
            painter.drawRoundedRect(r, 7, 7)
        elif hover:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(128, 128, 128, 40))
            painter.drawRoundedRect(r, 7, 7)
        bold = QFont(option.font)
        bold.setBold(True)
        painter.setFont(bold)
        painter.setPen(QColor("#E8E9EC" if dark else "#1D1F23"))
        width = int(r.width() - 16)
        painter.drawText(
            QRectF(r.left() + 8, r.top() + 3, width, 20),
            Qt.AlignmentFlag.AlignVCenter,
            painter.fontMetrics().elidedText(name, Qt.TextElideMode.ElideRight, width),
        )
        small = QFont(option.font)
        small.setPointSizeF(max(7.0, option.font.pointSizeF() * 0.85))
        painter.setFont(small)
        painter.setPen(QColor("#9AA0A9" if dark else "#5B6069"))
        painter.drawText(
            QRectF(r.left() + 8, r.top() + 22, width, 18),
            Qt.AlignmentFlag.AlignVCenter,
            painter.fontMetrics().elidedText(meta, Qt.TextElideMode.ElideRight, width),
        )
        painter.restore()


class StepList(QListWidget):
    """The step cards. Drag to reorder (``reordered`` fires after a drop)."""

    reordered = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setItemDelegate(StepDelegate(self))
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setUniformItemSizes(True)
        self.setStyleSheet("QListWidget { background: transparent; border: none; }")
        self.setAccessibleName("Steps")

    def dropEvent(self, event):
        super().dropEvent(event)
        QTimer.singleShot(0, self.reordered.emit)  # the cards are rebuilt after the drop has finished


# --- preview timeline --------------------------------------------------------------------


class Timeline(QWidget):
    """The plan drawn per lane: pumping, resting, waits for you, timers and sync waits."""

    ROW = 38
    LEFT = 120
    TOP = 26

    def __init__(self, parent=None):
        super().__init__(parent)
        self.plan = None
        self.steps = []
        self.subs = {}
        self.setMouseTracking(True)
        self.setMinimumHeight(self.TOP + 4 * self.ROW + 10)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAccessibleName("Timeline by channel")

    def set_plan(self, plan, steps, subs):
        self.plan, self.steps, self.subs = plan, steps, subs
        lanes = self._lanes()
        self.setFixedHeight(self.TOP + max(1, len(lanes)) * self.ROW + 10)
        self.update()

    def _lanes(self):
        if self.plan is None:
            return []
        return self.plan.used_lanes or []

    def _x(self, t):
        total = max(1.0, self.plan.total_s if self.plan else 1.0)
        return self.LEFT + (self.width() - self.LEFT - 12) * t / total

    def _ticks(self):
        total = max(1.0, self.plan.total_s)
        for step in (60, 300, 600, 900, 1800, 3600, 7200, 10800, 21600):
            if total / step <= 8:
                break
        out = []
        t = 0.0
        while t <= total + 1e-6:
            out.append(t)
            t += step
        return out

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        dark = is_dark(self)
        grey_c = QColor("#9AA0A9" if dark else "#5B6069")
        text_c = QColor("#E8E9EC" if dark else "#1D1F23")
        if not self.plan or not self.plan.flat:
            p.setPen(grey_c)
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Nothing to show yet: add steps first.")
            return
        small = QFont(self.font())
        small.setPointSizeF(max(7.0, self.font().pointSizeF() * 0.85))
        p.setFont(small)
        for t in self._ticks():
            x = self._x(t)
            p.setPen(QPen(QColor(128, 128, 128, 60), 1))
            p.drawLine(int(x), self.TOP - 4, int(x), self.height() - 6)
            p.setPen(grey_c)
            label = "0" if t == 0 else sq.fmt_time(t)
            box = QRectF(x - 40, 2, 80, 18)
            if box.right() > self.width() - 2:
                box.moveRight(self.width() - 2)
            p.drawText(box, Qt.AlignmentFlag.AlignCenter, label)
        for row, lane in enumerate(self._lanes()):
            y = self.TOP + row * self.ROW
            p.setPen(text_c)
            bold = QFont(self.font())
            bold.setBold(True)
            p.setFont(bold)
            p.drawText(QRectF(4, y + 2, self.LEFT - 8, 18), Qt.AlignmentFlag.AlignVCenter, f"CH{lane}")
            p.setFont(small)
            p.setPen(grey_c)
            sub = p.fontMetrics().elidedText(
                self.subs.get(lane, ""), Qt.TextElideMode.ElideRight, self.LEFT - 10
            )
            p.drawText(QRectF(4, y + 18, self.LEFT - 8, 16), Qt.AlignmentFlag.AlignVCenter, sub)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(128, 128, 128, 28))
            p.drawRoundedRect(QRectF(self.LEFT, y + 6, self.width() - self.LEFT - 12, self.ROW - 12), 4, 4)
            for seg in self.plan.lanes.get(lane, []):
                self._draw(p, seg, y)
        p.end()

    def _color(self, seg):
        if seg.kind in ("pump", "gap"):
            kind = self.steps[seg.top]["kind"] if seg.top < len(self.steps) else "dose"
            if kind == "staccato" or seg.kind == "gap":
                return QColor(SEGMENT["staccato"])
            return QColor(SEGMENT["pump"])
        return QColor(SEGMENT.get(seg.kind, "#7D828C"))

    def _seg_rect(self, seg, y):
        x0, x1 = self._x(seg.start), self._x(seg.end)
        top, h = y + 8, self.ROW - 16
        if seg.background:
            top, h = y + self.ROW - 16, 6
        return QRectF(x0, top, max(1.5, x1 - x0), h)

    def _draw(self, p, seg, y):
        color = self._color(seg)
        if seg.kind == "review":
            x = self._x(seg.start)
            p.setBrush(color)
            p.setPen(Qt.PenStyle.NoPen)
            cy = y + self.ROW / 2
            p.drawPolygon(QPolygonF(self._diamond(x, cy)))
            return
        rect = self._seg_rect(seg, y)
        if seg.kind == "gap":
            color.setAlpha(70)
        elif seg.kind == "sync":
            color.setAlpha(120)
        p.setPen(Qt.PenStyle.NoPen)
        if seg.kind == "rest":
            p.setBrush(QBrush(color, Qt.BrushStyle.BDiagPattern))
            p.setPen(QPen(color, 1))
        else:
            p.setBrush(color)
        p.drawRoundedRect(rect, 2, 2)

    @staticmethod
    def _diamond(x, cy):
        return [QPointF(x, cy - 8), QPointF(x + 7, cy), QPointF(x, cy + 8), QPointF(x - 7, cy)]

    def segment_at(self, pos):
        if not self.plan:
            return None, None
        row = int((pos.y() - self.TOP) // self.ROW)
        lanes = self._lanes()
        if not 0 <= row < len(lanes):
            return None, None
        lane = lanes[row]
        y = self.TOP + row * self.ROW
        best = None
        for seg in self.plan.lanes.get(lane, []):
            if seg.kind == "review":
                if abs(self._x(seg.start) - pos.x()) <= 8:
                    return lane, seg
                continue
            r = self._seg_rect(seg, y).adjusted(-2, -4, 2, 4)
            if r.contains(pos.x(), pos.y()):
                best = seg
        return lane, best

    def mouseMoveEvent(self, event):
        lane, seg = self.segment_at(event.position())
        if seg is None:
            QToolTip.hideText()
            return
        what = {
            "pump": "pumping",
            "gap": "between bursts",
            "rest": "resting for the on-time limit",
            "review": "waits for you",
            "wait": "timer",
            "sync": seg.text or "waiting for the other channels",
        }.get(seg.kind, seg.kind)
        step = self.steps[seg.top] if seg.top < len(self.steps) else None
        name = sq.title(step) if step else seg.text
        span = (
            f"{sq.fmt_time(seg.start)} → {sq.fmt_time(seg.end)}"
            if seg.end > seg.start
            else sq.fmt_time(seg.start)
        )
        bg = " (background)" if seg.background else ""
        QToolTip.showText(
            event.globalPosition().toPoint(),
            f"Step {seg.top + 1} · {name}{bg}\nCH{lane}: {what}, {span}",
            self,
        )


class StatCard(QFrame):
    def __init__(self, title, parent=None):
        super().__init__(parent)
        self.setObjectName("statCard")
        self.setStyleSheet(
            "#statCard { border: 1px solid rgba(128,128,128,0.35); border-radius: 10px;"
            " background: rgba(128,128,128,0.08); }"
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        self.title = grey(title, 12)
        self.value = QLabel("—")
        self.value.setStyleSheet("font-size: 18px; font-weight: 600;")
        self.value.setWordWrap(True)
        self.note = grey("", 12)
        lay.addWidget(self.title)
        lay.addWidget(self.value)
        lay.addWidget(self.note)
        lay.addStretch(1)
        self.extra = lay


class LaneCard(QFrame):
    """One lane in the Running view: what it is doing, with pause, skip and Continue."""

    def __init__(self, tab, lane, parent=None):
        super().__init__(parent)
        self.tab, self.lane = tab, lane
        self.setObjectName("laneCard")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        top = QHBoxLayout()
        top.addWidget(heading(f"CH{lane}", 14))
        top.addStretch(1)
        self.state = QLabel()
        top.addWidget(self.state)
        lay.addLayout(top)
        self.step = grey("", 12)
        self.now = QLabel()
        self.now.setWordWrap(True)
        self.now.setStyleSheet("font-weight: 600;")
        self.detail = QLabel()
        self.detail.setWordWrap(True)
        self.heads = grey("", 12)
        self.next = grey("", 12)
        for w in (self.step, self.now, self.detail, self.heads, self.next):
            lay.addWidget(w)
        lay.addStretch(1)
        row = QHBoxLayout()
        self.btn_continue = QPushButton("Continue")
        self.btn_continue.setStyleSheet(
            "QPushButton { background-color: #F08A24; color: #1D1205; font-weight: bold; padding: 6px 12px; }"
        )
        self.btn_continue.clicked.connect(lambda: tab.host.approve_channel(lane))
        self.btn_pause = QPushButton("Pause")
        self.btn_pause.clicked.connect(self._toggle_pause)
        self.btn_skip = QPushButton("Skip step")
        self.btn_skip.setToolTip(
            "Stop this channel's current step (its heads turn off) and go on with the next."
        )
        self.btn_skip.clicked.connect(lambda: tab.host.skip_step(lane))
        row.addWidget(self.btn_continue)
        row.addWidget(self.btn_pause)
        row.addWidget(self.btn_skip)
        lay.addLayout(row)
        self._shown = None

    def _toggle_pause(self):
        host = self.tab.host
        host.pause_lane(self.lane, not host._ch_paused.get(self.lane, False))
        self.tab.refresh_running()

    def show_state(self, run):
        host = self.tab.host
        ls = run.lanes[self.lane]
        paused = bool(host._ch_paused.get(self.lane)) and not run.ended
        waiting = bool(host._ch_review_events.get(self.lane)) and not run.ended
        heads = []
        resting = False
        for (did, ch), hr in sorted(getattr(host, "head_runs", {}).items()):
            if hr.source != "sequence" or (hr.lane if hr.lane is not None else ch) != self.lane:
                continue
            st = host.run_state(did, ch)
            if not st:
                continue
            name = host.head_label(did, ch)
            if st[0] == "cooling":
                resting = True
                heads.append(f"{name}: resting, {sq.fmt_clock(st[2])} left")
            elif st[0] == "paused":
                heads.append(f"{name}: paused")
            else:
                heads.append(f"{name}: pumping, {sq.fmt_clock(st[2])} left")
        status = ls.status
        if run.ended and status not in ("idle", "done"):
            status = "stopped"
        label, color = {
            "idle": ("NOT USED", "#8A8F98"),
            "done": ("DONE", "#2FB36D"),
            "stopped": ("STOPPED", "#E5534B"),
            "pumping": ("PUMPING", "#2FB36D"),
            "timer": ("TIMER", "#9AA0A9"),
            "sync": ("SYNCING", "#9AA0A9"),
            "review": ("WAITING FOR YOU", "#F08A24"),
        }.get(status, (status.upper(), "#9AA0A9"))
        if waiting:
            label, color = "WAITING FOR YOU", "#F08A24"
        elif paused:
            label, color = "PAUSED", "#E0A33A"
        elif resting:
            label, color = "RESTING", "#4FA3D1"
        n = run.step_count()
        step_text = (
            f"Step {ls.top + 1} of {n}" if ls.top is not None and status not in ("idle", "done") else ""
        )
        detail = ls.detail
        if ls.ml > 0 and status == "pumping":
            detail = (detail + " · " if detail else "") + f"{sq.fmt_ml(ls.ml)} delivered so far"
        if ls.background:
            detail = (detail + "\n" if detail else "") + "In the background: " + ", ".join(ls.background)
        nxt = run.next_title(self.lane) if status not in ("idle", "done", "stopped") else ""
        shown = (
            label,
            step_text,
            ls.title,
            detail,
            tuple(heads),
            nxt,
            paused,
            waiting,
            bool(run.ended),
            run.stopping,
        )
        if shown == self._shown:
            return
        self._shown = shown
        self.state.setText(label)
        self.state.setStyleSheet(
            f"color: {color}; font-weight: 700; font-size: 11px; border: 1px solid {color};"
            " border-radius: 9px; padding: 1px 8px;"
        )
        self.step.setText(step_text)
        self.now.setText(
            ls.title
            if status not in ("idle", "done")
            else ("" if status == "idle" else "Finished its steps.")
        )
        self.detail.setText(detail)
        self.detail.setVisible(bool(detail))
        self.heads.setText("\n".join(heads))
        self.heads.setVisible(bool(heads))
        self.next.setText(f"Next: {nxt}" if nxt else "")
        self.next.setVisible(bool(nxt))
        live = not run.ended and not run.stopping and status not in ("idle", "done")
        self.btn_continue.setVisible(waiting and not run.stopping)
        self.btn_pause.setVisible(live)
        self.btn_pause.setText("Resume" if paused else "Pause")
        self.btn_skip.setVisible(live and status != "idle")
        border = color if (waiting or paused) else "rgba(128,128,128,0.35)"
        self.setStyleSheet(
            f"#laneCard {{ border: 1px solid {border}; border-radius: 10px; background: rgba(128,128,128,0.08); }}"
        )
        self.setEnabled(status != "idle")
        self.setAccessibleName(f"CH{self.lane}: {label}. {step_text}. {ls.title}")


class SequencerTab(QWidget):
    """See the module docstring."""

    def __init__(self, host, parent=None):
        super().__init__(parent)
        self.host = host
        self.undo_stack, self.redo_stack = [], []
        self._last_key, self._last_key_t = None, 0.0
        self._clipboard = []
        self._meta_cache = {}
        self._online_sig = None
        self._history_cache = (None, [])
        self._grid_cache = {}
        self._building = False
        self.plan = None
        self._plan_nolimit = None
        self._load_draft()

        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 6, 4, 0)
        nav = QHBoxLayout()
        self.nav_group = QButtonGroup(self)
        self.nav_buttons = []
        for i, text in enumerate(("Build", "Preview", "Running")):
            b = QPushButton(text)
            b.setCheckable(True)
            b.setStyleSheet(SEGMENTED)
            b.clicked.connect(lambda _c, i=i: self.show_page(i))
            self.nav_group.addButton(b)
            self.nav_buttons.append(b)
            nav.addWidget(b)
        nav.addSpacing(12)
        self.nav_status = QPushButton()
        self.nav_status.setFlat(True)
        self.nav_status.setCursor(Qt.CursorShape.PointingHandCursor)
        self.nav_status.clicked.connect(lambda: self.show_page(2))
        nav.addWidget(self.nav_status)
        nav.addStretch(1)
        lay.addLayout(nav)

        self.pages = QStackedWidget()
        self.pages.addWidget(self._build_page())
        self.pages.addWidget(self._preview_page())
        self.pages.addWidget(self._running_page())
        lay.addWidget(self.pages, 1)

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(600)
        self._save_timer.timeout.connect(self.save_draft)
        self._plan_timer = QTimer(self)
        self._plan_timer.setSingleShot(True)
        self._plan_timer.setInterval(150)
        self._plan_timer.timeout.connect(self._replan)
        self._shortcuts()

        self._fill_steps(select=[0] if self.steps else [])
        self._replan()
        self.refresh_library()
        self.refresh_queue()
        self.show_page(0)

    # --- the draft (the sequence being edited) ---------------------------------------------

    def _library(self):
        return self.host.config.data.setdefault("saved_sequences", {})

    def _load_draft(self):
        cfg = self.host.config.data
        try:
            draft = cfg.get("sequence")
            self.steps = sq.normalize_all(draft if isinstance(draft, list) else [])
        except Exception as e:  # odd data in the settings file: start empty rather than not at all
            logger.error(f"Couldn't read the sequence being edited ({e}); starting with an empty one.")
            self.steps = []
        lib = cfg.get("saved_sequences") or {}
        source = cfg.get("sequence_source")
        self.source = source if source in lib else None
        self.name = str(cfg.get("sequence_name") or "").strip()
        if self.source is None and self.steps and not self.name:
            # From 2.6: the step table had no name. If it is a saved sequence, it opens as that one.
            for name, steps in lib.items():
                if sq.same_steps(steps, self.steps):
                    self.source = self.name = name
                    break
        self.name = self.name or self.source or UNTITLED

    def save_draft(self):
        """Keep the sequence being edited in the settings, so it is there next time."""
        self._save_timer.stop()
        cfg = self.host.config.data
        cfg["sequence"] = sq.to_saved_all(self.steps)
        cfg["sequence_name"] = self.name
        cfg["sequence_source"] = self.source
        self.host.config.save()

    def is_unsaved(self):
        saved = self._library().get(self.source) if self.source else None
        if saved is None:
            return bool(self.steps)
        return self.name != self.source or not sq.same_steps(saved, self.steps)

    def _snapshot(self):
        return (self.name, copy.deepcopy(self.steps))

    def _before_change(self, key=None):
        """Remember the state for Undo. Quick edits of the same thing (typing a number) are one step."""
        now = time.monotonic()
        if key is not None and key == self._last_key and now - self._last_key_t < 1.5:
            self._last_key_t = now
            return
        self._last_key, self._last_key_t = key, now
        self.undo_stack.append(self._snapshot())
        del self.undo_stack[:-100]
        self.redo_stack.clear()

    def _changed(self, structure=False, select=None, inspector=False):
        """Something in the draft changed: save it soon, and work out the plan again."""
        if structure:
            self._fill_steps(select=select)
        self._save_timer.start()
        self._plan_timer.start()
        self._update_bar()
        if inspector:
            self._rebuild_inspector()

    def undo(self):
        if not self.undo_stack:
            return
        self.redo_stack.append(self._snapshot())
        self.name, self.steps = self.undo_stack.pop()
        self._last_key = None
        self.name_edit.setText(self.name)
        self._changed(structure=True, select=self._selected_rows() or [0], inspector=True)

    def redo(self):
        if not self.redo_stack:
            return
        self.undo_stack.append(self._snapshot())
        self.name, self.steps = self.redo_stack.pop()
        self._last_key = None
        self.name_edit.setText(self.name)
        self._changed(structure=True, select=self._selected_rows() or [0], inspector=True)

    def _confirm_leave(self):
        """Before opening another sequence: offer to save unsaved changes. False to stay."""
        if not self.is_unsaved():
            return True
        box = QMessageBox(self)
        box.setWindowTitle("Unsaved changes")
        box.setText(f"Save the changes to “{self.name}” first?")
        box.setStandardButtons(
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel
        )
        box.setDefaultButton(QMessageBox.StandardButton.Save)
        answer = box.exec()
        if answer == QMessageBox.StandardButton.Cancel:
            return False
        if answer == QMessageBox.StandardButton.Save:
            return self.save()
        return True

    def _set_draft(self, name, steps, source):
        self.name, self.steps, self.source = name, sq.normalize_all(steps), source
        self.undo_stack.clear()
        self.redo_stack.clear()
        self._last_key = None
        self.name_edit.setText(self.name)
        self._changed(structure=True, select=[0] if self.steps else [], inspector=True)
        self.refresh_library()

    def open_sequence(self, name):
        lib = self._library()
        if name not in lib or (name == self.source and not self.is_unsaved()):
            return
        if not self._confirm_leave():
            self.refresh_library()
            return
        self._set_draft(name, lib[name], name)
        self.show_page(0)

    def new_sequence(self):
        if not self._confirm_leave():
            return
        self._set_draft(UNTITLED, [], None)

    def save(self, name=None):
        """Save to the library under ``name`` (default: the name at the top). False if not saved."""
        name = (name or self.name or "").strip() or UNTITLED
        lib = self._library()
        if name in lib and name != self.source:
            reply = QMessageBox.question(
                self, "Replace sequence", f"There's already a sequence called “{name}”. Replace it?"
            )
            if reply != QMessageBox.StandardButton.Yes:
                return False
        lib[name] = sq.to_saved_all(self.steps)
        self.name = self.source = name
        self.name_edit.setText(name)
        self.save_draft()
        self._update_bar()
        self.refresh_library()
        logger.info(f"Saved the sequence “{name}” ({len(self.steps)} steps).")
        return True

    def save_as(self):
        name, ok = QInputDialog.getText(self, "Save as", "Name for the copy:", text=f"{self.name} (copy)")
        if ok and name.strip():
            self.save(name.strip())

    def _name_edited(self):
        text = self.name_edit.text().strip() or UNTITLED
        if text != self.name:
            self._before_change(("name",))
            self.name = text
            self._changed()

    # --- the Build page ----------------------------------------------------------------------

    def _build_page(self):
        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(self._sidebar())
        split.addWidget(self._editor())
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setChildrenCollapsible(False)
        split.setSizes([260, 1000])
        self.splitter = split
        return split

    def _sidebar(self):
        w = QWidget()
        w.setMinimumWidth(210)
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 6, 0)
        top = QHBoxLayout()
        top.addWidget(heading("Sequences"))
        top.addStretch(1)
        self.btn_new = QPushButton("+ New")
        self.btn_new.setToolTip("Start a new, empty sequence.")
        self.btn_new.clicked.connect(self.new_sequence)
        top.addWidget(self.btn_new)
        lay.addLayout(top)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search sequences")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda _t: self.refresh_library())
        pal = self.search.palette()
        pal.setColor(QPalette.ColorRole.PlaceholderText, QColor("#8A8F98"))
        self.search.setPalette(pal)
        lay.addWidget(self.search)
        self.library = QListWidget()
        self.library.setAccessibleName("Saved sequences")
        self.library.setItemDelegate(LibraryDelegate(self.library))
        self.library.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.library.setMouseTracking(True)
        self.library.itemClicked.connect(self._library_clicked)
        self.library.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.library.customContextMenuRequested.connect(self._library_menu)
        lay.addWidget(self.library, 3)
        row1 = QHBoxLayout()
        row2 = QHBoxLayout()
        for text, fn, tip, row in (
            ("Duplicate", self.duplicate_saved, "Make a copy of the selected sequence.", row1),
            ("Rename", self.rename_saved, "Rename the selected sequence.", row1),
            ("Delete", self.delete_saved, "Delete the selected sequence from the list.", row1),
            (
                "Import…",
                self.import_sequence,
                "Add a sequence from a file exported on this or another computer.",
                row2,
            ),
            (
                "Export…",
                self.export_sequence,
                "Save the selected sequence to a file. It includes the pumps' IDs, so it works with the same Jebao account.",
                row2,
            ),
        ):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            row.addWidget(b)
        lay.addLayout(row1)
        lay.addLayout(row2)

        lay.addSpacing(8)
        lay.addWidget(heading("Queue"))
        self.queue_list = QListWidget()
        self.queue_list.setAccessibleName("Queue")
        self.queue_list.setMaximumHeight(170)
        lay.addWidget(self.queue_list, 1)
        qrow = QHBoxLayout()
        for text, tip, fn in (
            ("↑", "Move up", lambda: self._move_queued(-1)),
            ("↓", "Move down", lambda: self._move_queued(1)),
            ("✕", "Remove from the queue", self._remove_queued),
        ):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.setAccessibleName(tip)
            b.setFixedWidth(40)
            b.clicked.connect(fn)
            qrow.addWidget(b)
        qrow.addStretch(1)
        lay.addLayout(qrow)
        self.chk_pause_queue = QCheckBox("Pause the queue after this run")
        self.chk_pause_queue.setToolTip("The next queued sequence won't start until you untick this.")
        self.chk_pause_queue.toggled.connect(self.host.set_queue_paused)
        lay.addWidget(self.chk_pause_queue)
        lay.addWidget(grey("Queued runs start one after another.", 12))
        return w

    def _editor(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(6, 0, 0, 0)
        row1 = QHBoxLayout()
        self.name_edit = QLineEdit(self.name)
        self.name_edit.setAccessibleName("Sequence name")
        self.name_edit.setStyleSheet("QLineEdit { font-size: 17px; font-weight: 600; padding: 2px 4px; }")
        self.name_edit.editingFinished.connect(self._name_edited)
        row1.addWidget(self.name_edit, 1)
        self.unsaved = QLabel("● Unsaved changes")
        self.unsaved.setStyleSheet(
            "background: #3A2F14; color: #F2CF86; border-radius: 9px; padding: 2px 10px; font-size: 12px;"
        )
        row1.addWidget(self.unsaved)
        self.btn_undo = QPushButton("Undo")
        self.btn_undo.setToolTip("Undo (Ctrl+Z)")
        self.btn_undo.clicked.connect(self.undo)
        self.btn_redo = QPushButton("Redo")
        self.btn_redo.setToolTip("Redo (Ctrl+Shift+Z)")
        self.btn_redo.clicked.connect(self.redo)
        self.btn_save = QPushButton("Save")
        self.btn_save.setToolTip("Save to the list of sequences (Ctrl+S).")
        self.btn_save.clicked.connect(lambda: self.save())
        self.btn_save_as = QPushButton("Save as…")
        self.btn_save_as.clicked.connect(self.save_as)
        for b in (self.btn_undo, self.btn_redo, self.btn_save, self.btn_save_as):
            row1.addWidget(b)
        lay.addLayout(row1)
        row2 = QHBoxLayout()
        self.meta = grey("", 12)
        row2.addWidget(self.meta, 1)
        self.btn_preview = QPushButton("Preview timeline")
        self.btn_preview.setToolTip(
            "See what the run will do: times, a timeline per channel and the liquid needed."
        )
        self.btn_preview.clicked.connect(lambda: self.show_page(1))
        self.btn_queue = QPushButton("Add to queue")
        self.btn_queue.clicked.connect(self.add_to_queue)
        self.btn_run = QPushButton("▶ Run now")
        self.btn_run.setStyleSheet(GO)
        self.btn_run.clicked.connect(self.run_now)
        for b in (self.btn_preview, self.btn_queue, self.btn_run):
            row2.addWidget(b)
        lay.addLayout(row2)

        body = QSplitter(Qt.Orientation.Horizontal)
        body.setChildrenCollapsible(False)
        left = QWidget()
        left.setMinimumWidth(340)
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 4, 6, 0)
        ll.addWidget(heading("Steps", 13))
        ll.addWidget(
            grey(
                "Each channel (CH1–CH4) runs its own heads, side by side. Drag to reorder; Ctrl-click to pick several.",
                12,
            )
        )
        self.list = StepList()
        self.list.itemSelectionChanged.connect(self._selection_changed)
        self.list.reordered.connect(self._reordered)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._steps_menu)
        steps_box = QWidget()
        sl = QVBoxLayout(steps_box)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.addWidget(self.list, 1)
        palette = QWidget()
        pal = FlowLayout(palette)
        add = QLabel("Add step:")
        add.setStyleSheet("color: grey; font-size: 12px;")
        add.setMinimumHeight(28)
        pal.addWidget(add)
        for kind in sq.KINDS:
            b = QPushButton("+ " + sq.LABELS[kind])
            b.setToolTip(f"Add a “{sq.LABELS[kind]}” step after the selected one.")
            b.clicked.connect(lambda _c, k=kind: self.add_step(k))
            pal.addWidget(b)
        sl.addWidget(palette)
        self.checks_box = QFrame()
        self.checks_box.setObjectName("checksBox")
        self.checks_box.setStyleSheet(
            "#checksBox { border: 1px solid rgba(128,128,128,0.35); border-radius: 10px; background: rgba(128,128,128,0.06); }"
        )
        cl = QVBoxLayout(self.checks_box)
        cl.setContentsMargins(12, 8, 12, 8)
        ch = QHBoxLayout()
        self.checks_title = heading("Before you run", 13)
        ch.addWidget(self.checks_title)
        ch.addStretch(1)
        link = QPushButton("See the timeline and totals →")
        link.setFlat(True)
        link.setStyleSheet("color: #8AB4FF;")
        link.setCursor(Qt.CursorShape.PointingHandCursor)
        link.clicked.connect(lambda: self.show_page(1))
        ch.addWidget(link)
        cl.addLayout(ch)
        checks_scroll = QScrollArea()
        checks_scroll.setWidgetResizable(True)
        checks_scroll.setFrameShape(QFrame.Shape.NoFrame)
        checks_scroll.setMinimumHeight(40)
        checks_scroll.setStyleSheet("QScrollArea { background: transparent; }")
        inner = QWidget()
        inner.setStyleSheet("background: transparent;")
        self.checks_layout = QVBoxLayout(inner)
        self.checks_layout.setContentsMargins(0, 0, 0, 0)
        checks_scroll.setWidget(inner)
        cl.addWidget(checks_scroll)
        # Drag the divider to give the steps or the checks more room.
        self.steps_split = QSplitter(Qt.Orientation.Vertical)
        self.steps_split.setChildrenCollapsible(False)
        self.steps_split.addWidget(steps_box)
        self.steps_split.addWidget(self.checks_box)
        self.steps_split.setStretchFactor(0, 3)
        self.steps_split.setStretchFactor(1, 1)
        self.steps_split.setSizes([480, 150])
        ll.addWidget(self.steps_split, 1)
        body.addWidget(left)

        self.insp_scroll = QScrollArea()
        self.insp_scroll.setWidgetResizable(True)
        self.insp_scroll.setMinimumWidth(300)
        self.insp_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.insp = QWidget()
        self.insp_lay = QVBoxLayout(self.insp)
        self.insp_lay.setContentsMargins(8, 4, 8, 10)
        self.insp_scroll.setWidget(self.insp)
        self._insp_wide = None
        self.insp_scroll.viewport().installEventFilter(self)
        body.addWidget(self.insp_scroll)
        body.setStretchFactor(0, 3)
        body.setStretchFactor(1, 2)
        body.setSizes([620, 380])
        lay.addWidget(body, 1)
        return w

    def _shortcuts(self):
        page = self.pages.widget(0)
        for keys, fn in (
            (QKeySequence.StandardKey.Undo, self.undo),
            (QKeySequence.StandardKey.Redo, self.redo),
            ("Ctrl+Shift+Z", self.redo),
            (QKeySequence.StandardKey.Save, lambda: self.save()),
            ("Ctrl+D", self.duplicate_steps),
            (QKeySequence.StandardKey.Copy, self.copy_steps),
            (QKeySequence.StandardKey.Paste, self.paste_steps),
            ("Alt+Up", lambda: self.move_steps(-1)),
            ("Alt+Down", lambda: self.move_steps(1)),
        ):
            sc = QShortcut(QKeySequence(keys), page)
            sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            sc.activated.connect(fn)
        delete = QShortcut(QKeySequence(QKeySequence.StandardKey.Delete), self.list)
        delete.setContext(Qt.ShortcutContext.WidgetShortcut)
        delete.activated.connect(self.delete_steps)
        backspace = QShortcut(QKeySequence("Backspace"), self.list)
        backspace.setContext(Qt.ShortcutContext.WidgetShortcut)
        backspace.activated.connect(self.delete_steps)

    def eventFilter(self, obj, event):
        # The step type buttons go in 3 columns when the settings pane is wide, else 2.
        if obj is self.insp_scroll.viewport() and event.type() == event.Type.Resize:
            wide = event.size().width() >= 380
            if self._insp_wide is not None and wide != self._insp_wide:
                self._insp_wide = wide
                QTimer.singleShot(0, self._rebuild_inspector)
        return super().eventFilter(obj, event)

    def show_page(self, index):
        self.pages.setCurrentIndex(index)
        self.nav_buttons[index].setChecked(True)
        if index == 1:
            self.refresh_preview()
        elif index == 2:
            self._grid_cache = {}
            self.refresh_running()
        self._update_nav()

    # --- steps: the cards --------------------------------------------------------------------

    def _selected_rows(self):
        return sorted(self.list.row(item) for item in self.list.selectedItems())

    def _fill_steps(self, select=None):
        """Show the steps as cards again (after adding, deleting or moving steps)."""
        self._building = True
        self.list.clear()
        for i, _step in enumerate(self.steps):
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole + 1, i)
            self.list.addItem(item)
        self._update_cards()
        for i in select or []:
            if 0 <= i < self.list.count():
                self.list.item(i).setSelected(True)
                self.list.setCurrentRow(i, QItemSelectionModel.SelectionFlag.Select)
        self._building = False
        self._selection_changed()

    def _ctx(self):
        """What the app knows about the pumps, for this sequence (older steps' channels cover
        the stacks it pumps on: see sequence.Context.expand)."""
        return sq.for_steps(self.host.plan_context(), self.steps, self.name)

    def _card(self, i, step):
        plan, ctx = self.plan, self._ctx()
        kind = step["kind"]
        seconds = plan.step_s.get(i) if plan else None
        rest = plan.step_rest_s.get(i, 0.0) if plan else 0.0
        if kind == "review":
            time_text = "until you continue"
        elif kind == "sync":
            time_text = sq.fmt_time(seconds) if seconds and seconds > 0.5 else "—"
        elif plan is not None and i in plan.unknown:
            time_text = "?"
        else:
            time_text = sq.fmt_time(seconds, approx=rest > 0.5 or kind == "sequence")
        checks = [c for c in (plan.checks if plan else []) if c.top == i and c.level != "ok"]
        errors = [c for c in checks if c.level == "error"]
        flags = []
        if step.get("background"):
            flags.append(("in background", "info"))
        if rest > 0.5:
            flags.append(("rests for on-time limit", "warn"))
        if errors:
            flags.append(("✕ problem" if len(errors) == 1 else f"✕ {len(errors)} problems", "error"))
        elif any(c.level == "warn" for c in checks):
            flags.append(("check", "warn"))
        return {
            "n": i + 1,
            "kind": kind,
            "title": sq.describe(step),
            "label": step.get("label", ""),
            "where": sq.where_text(step, ctx),
            "time": time_text,
            "flags": flags,
            "background": bool(step.get("background")),
            "error": bool(errors),
            "tip": "\n".join(c.text for c in checks),
        }

    def _update_cards(self):
        for i in range(min(self.list.count(), len(self.steps))):
            item = self.list.item(i)
            card = self._card(i, self.steps[i])
            item.setData(Qt.ItemDataRole.UserRole, card)
            item.setToolTip(card["tip"])
            item.setData(
                Qt.ItemDataRole.AccessibleTextRole,
                f"Step {i + 1}: {card['title']}. {card['where']}. {card['time']}",
            )
        self.list.viewport().update()

    def _selection_changed(self):
        if self._building:
            return
        self._rebuild_inspector()

    def _reordered(self):
        order = [self.list.item(r).data(Qt.ItemDataRole.UserRole + 1) for r in range(self.list.count())]
        if order == list(range(len(self.steps))) or sorted(order) != list(range(len(self.steps))):
            self._fill_steps(select=self._selected_rows())
            return
        moved = {old for old in order if order.index(old) != old}
        self._before_change()
        self.steps = [self.steps[old] for old in order]
        new_rows = [r for r, old in enumerate(order) if old in moved]
        self._changed(structure=True, select=new_rows or [0], inspector=True)

    def _lanes_so_far(self, before=None):
        """The lanes (CH numbers) the pumping steps use, for a new timer or review step; or all."""
        steps = self.steps[:before] if before is not None else self.steps
        number = self.host.channel_number
        lanes = sorted({number(d, ch) for s in steps if s["kind"] in sq.PUMPING for d, ch in sq.heads_of(s)})
        return lanes or self.host.all_lanes()

    def _previous_heads(self, i):
        for s in reversed(self.steps[:i]):
            if s["kind"] in sq.PUMPING and s["heads"]:
                return copy.deepcopy(s["heads"])
        return []

    def add_step(self, kind):
        rows = self._selected_rows()
        at = rows[-1] + 1 if rows else len(self.steps)
        step = sq.new_step(kind, heads=self._previous_heads(at), cols=self._lanes_so_far(at))
        if kind in ("dose", "staccato") and at > 0:
            prev = self.steps[at - 1]
            if prev["kind"] == kind:
                for key in ("volume", "burst", "gap", "by", "seconds"):
                    step[key] = prev[key]
        self._before_change()
        self.steps.insert(at, sq.normalize(step))
        self._changed(structure=True, select=[at], inspector=True)
        self.list.scrollToItem(self.list.item(at))

    def delete_steps(self):
        rows = self._selected_rows()
        if not rows:
            return
        self._before_change()
        for r in reversed(rows):
            del self.steps[r]
        keep = min(rows[0], len(self.steps) - 1)
        self._changed(structure=True, select=[keep] if keep >= 0 else [], inspector=True)

    def duplicate_steps(self):
        rows = self._selected_rows()
        if not rows:
            return
        self._before_change()
        copies = [copy.deepcopy(self.steps[r]) for r in rows]
        at = rows[-1] + 1
        self.steps[at:at] = copies
        self._changed(structure=True, select=list(range(at, at + len(copies))), inspector=True)

    def copy_steps(self):
        rows = self._selected_rows()
        if rows:
            self._clipboard = [copy.deepcopy(self.steps[r]) for r in rows]

    def paste_steps(self):
        if not self._clipboard:
            return
        rows = self._selected_rows()
        at = rows[-1] + 1 if rows else len(self.steps)
        self._before_change()
        self.steps[at:at] = copy.deepcopy(self._clipboard)
        self._changed(structure=True, select=list(range(at, at + len(self._clipboard))), inspector=True)

    def move_steps(self, delta):
        rows = self._selected_rows()
        if not rows or (delta < 0 and rows[0] == 0) or (delta > 0 and rows[-1] == len(self.steps) - 1):
            return
        self._before_change()
        order = list(range(len(self.steps)))
        for r in rows if delta < 0 else reversed(rows):
            order[r], order[r + delta] = order[r + delta], order[r]
        self.steps = [self.steps[i] for i in order]
        self._changed(structure=True, select=[r + delta for r in rows], inspector=True)

    def _steps_menu(self, pos):
        menu = QMenu(self)
        has = bool(self._selected_rows())
        for text, fn, on in (
            ("Duplicate (Ctrl+D)", self.duplicate_steps, has),
            ("Copy (Ctrl+C)", self.copy_steps, has),
            ("Paste (Ctrl+V)", self.paste_steps, bool(self._clipboard)),
            ("Move up (Alt+↑)", lambda: self.move_steps(-1), has),
            ("Move down (Alt+↓)", lambda: self.move_steps(1), has),
            ("Delete (Del)", self.delete_steps, has),
        ):
            act = menu.addAction(text)
            act.setEnabled(on)
            act.triggered.connect(fn)
        menu.exec(self.list.viewport().mapToGlobal(pos))

    def _edit(self, i, key=None, inspector=False, **changes):
        """Change fields of step ``i`` (one Undo step for quick edits of the same field)."""
        if not 0 <= i < len(self.steps):
            return
        self._before_change((i, key or tuple(sorted(changes))))
        self.steps[i].update(changes)
        self.steps[i] = sq.normalize(self.steps[i])
        self._changed(inspector=inspector)

    # --- the plan, the bar and the checks ------------------------------------------------------

    def _replan(self):
        self._plan_timer.stop()
        try:
            self.plan = self.host.plan_sequence(self.steps, name=self.name)
        except Exception as e:  # odd saved data must not break the tab
            logger.error(f"Couldn't work out what the sequence will do: {e}")
            self.plan = sq.Plan(
                checks=[sq.Check("error", f"Couldn't work out what the sequence will do: {e}")]
            )
        self._plan_nolimit = None
        self._update_cards()
        self._update_checks()
        self._update_bar()
        self._update_estimate()
        if self.pages.currentIndex() == 1:
            self.refresh_preview()

    def _update_bar(self):
        n = len(self.steps)
        self.unsaved.setVisible(self.is_unsaved())
        self.btn_undo.setEnabled(bool(self.undo_stack))
        self.btn_redo.setEnabled(bool(self.redo_stack))
        plan = self.plan
        parts = [f"{n} step{'s' if n != 1 else ''}"]
        if plan is not None and plan.used_lanes:
            parts.append(sq.lanes_text(plan.used_lanes))
        if plan is not None and n:
            parts.append(f"about {sq.fmt_time(plan.total_s)}")
            if plan.reviews:
                parts[-1] += " plus your checks"
        last = self._last_runs().get(self.name)
        if last:
            parts.append("last run " + _day(last))
        self.meta.setText(" · ".join(parts))
        for b in (self.btn_preview, self.btn_queue, self.btn_run):
            b.setEnabled(n > 0)
        self.btn_run.setToolTip(
            "Start it now." if not self.host.run_active() else "Start it as soon as the running one finishes."
        )

    def _update_checks(self):
        clear_layout(self.checks_layout)
        checks = self.plan.checks if self.plan else []
        order = {"error": 0, "warn": 1, "info": 2, "ok": 3}
        shown = sorted(checks, key=lambda c: order.get(c.level, 4))
        for c in shown:
            self.checks_layout.addWidget(check_row(c))
        if not shown:
            self.checks_layout.addWidget(grey("Nothing to check.", 12))
        self.checks_layout.addStretch(1)
        counts = []
        for level, word in (("error", "problem"), ("warn", "warning")):
            n = sum(1 for c in checks if c.level == level)
            if n:
                counts.append(f"{n} {word}{'s' if n != 1 else ''}")
        self.checks_title.setText("Before you run" + (" · " + ", ".join(counts) if counts else " · all good"))
        color = "#F3A3A3" if any(c.level == "error" for c in checks) else ("#F0B65A" if counts else "")
        self.checks_title.setStyleSheet(
            "font-weight: 600; font-size: 13px;" + (f" color: {color};" if color else "")
        )

    # --- the inspector (the selected step's settings) ------------------------------------------

    def _rebuild_inspector(self):
        lay = self.insp_lay
        clear_layout(lay)
        self._chips = {}
        self._estimate = None
        self._warn_box = None
        rows = self._selected_rows()
        if not self.steps:
            lay.addWidget(heading("No steps yet", 13))
            lay.addWidget(grey("Add one with the buttons under the list: Dose, Staccato, Wait (timer) …"))
            lay.addStretch(1)
            return
        if not rows:
            lay.addWidget(grey("Pick a step to change it."))
            lay.addStretch(1)
            return
        if len(rows) > 1:
            lay.addWidget(heading(f"{len(rows)} steps selected", 13))
            row = QHBoxLayout()
            for text, fn in (("Duplicate", self.duplicate_steps), ("Delete", self.delete_steps)):
                b = QPushButton(text)
                b.clicked.connect(fn)
                row.addWidget(b)
            row.addStretch(1)
            lay.addLayout(row)
            lay.addWidget(grey("Drag them to move them together, or Copy (Ctrl+C) and Paste (Ctrl+V)."))
            lay.addStretch(1)
            return
        i = rows[0]
        step = self.steps[i]
        kind = step["kind"]
        top = QHBoxLayout()
        top.addWidget(heading(f"Step {i + 1}", 15))
        top.addStretch(1)
        for text, fn in (("Duplicate", self.duplicate_steps), ("Delete", self.delete_steps)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            top.addWidget(b)
        lay.addLayout(top)

        grid = QGridLayout()
        group = QButtonGroup(self.insp)
        self._insp_wide = self.insp_scroll.viewport().width() >= 380
        cols = 3 if self._insp_wide else 2
        for n, k in enumerate(sq.KINDS):
            b = QPushButton(sq.LABELS[k])
            b.setCheckable(True)
            b.setChecked(k == kind)
            b.setStyleSheet(SEGMENTED)
            b.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            b.setToolTip(f"Make this a “{sq.LABELS[k]}” step.")
            b.clicked.connect(lambda _c, k=k: self._change_kind(i, k))
            group.addButton(b)
            grid.addWidget(b, n // cols, n % cols)
        lay.addLayout(grid)

        lay.addWidget(grey("Label (optional)", 12))
        label = QLineEdit(step["label"])
        label.setPlaceholderText("e.g. Condition, Load sample, Wash")
        label.textEdited.connect(lambda t: self._edit(i, key="label", label=t))
        lay.addWidget(label)

        if kind in sq.PUMPING:
            self._head_picker(i, step)
            self._amount(i, step)
        elif kind == "wait":
            lay.addWidget(grey("Wait for", 12))
            dur = DurationEdit(step["seconds"])
            dur.changed.connect(lambda v: self._edit(i, key="seconds", seconds=v))
            lay.addWidget(dur)
            self._lane_picker(i, step)
        elif kind == "review":
            lay.addWidget(grey("Message (optional): what to do here, such as a setup change", 12))
            message = QPlainTextEdit(step.get("message", ""))
            message.setPlaceholderText(
                "e.g. Move the sample lines from the waste to the collection tubes, then press Continue."
            )
            message.setFixedHeight(84)
            message.setTabChangesFocus(True)
            message.setAccessibleName("Message")
            message.textChanged.connect(lambda: self._edit(i, key="message", message=message.toPlainText()))
            lay.addWidget(message)
            lay.addWidget(
                grey(
                    "When a channel gets here, the message shows in the orange bar at the top until you "
                    "press Continue.",
                    11,
                )
            )
            self._lane_picker(i, step)
            sound = QCheckBox("Play a sound when it's waiting")
            sound.setChecked(bool(step.get("sound", True)))
            sound.toggled.connect(lambda on: self._edit(i, sound=on))
            lay.addWidget(sound)
        elif kind == "sync":
            self._lane_picker(i, step)
        elif kind == "sequence":
            lay.addWidget(grey("Sequence to run", 12))
            combo = QComboBox()
            names = sorted(self._library(), key=rack.natural_key)
            if step["name"] and step["name"] not in names:
                names.insert(0, step["name"])
            combo.addItem("Pick one…", "")
            for n in names:
                combo.addItem(n, n)
            combo.setCurrentIndex(max(0, combo.findData(step["name"])))
            combo.currentIndexChanged.connect(lambda _x: self._edit(i, name=combo.currentData() or ""))
            lay.addWidget(combo)
            self._lane_picker(i, step)

        box = QFrame()
        box.setObjectName("estimateBox")
        box.setStyleSheet(
            "#estimateBox { border: 1px solid rgba(128,128,128,0.35); border-radius: 8px; background: rgba(128,128,128,0.08); }"
        )
        bl = QVBoxLayout(box)
        bl.setContentsMargins(10, 8, 10, 8)
        bl.addWidget(heading("This step", 12))
        self._estimate = QLabel()
        self._estimate.setWordWrap(True)
        bl.addWidget(self._estimate)
        self._warn_box = QVBoxLayout()
        bl.addLayout(self._warn_box)
        lay.addWidget(box)
        lay.addStretch(1)
        self._insp_row = i
        self._update_estimate()

    def _change_kind(self, i, kind):
        old = self.steps[i]
        if old["kind"] == kind:
            return
        lanes = sq.lanes_of(old, self._ctx()) or self._lanes_so_far(i)
        new = sq.new_step(kind, heads=old["heads"] or self._previous_heads(i), cols=lanes)
        new["label"] = old["label"]
        if kind in sq.PUMPING and old["kind"] in sq.PUMPING:
            new["volume"] = old["volume"]
        if kind == "wait" and old["kind"] == "dose" and old["by"] == "time":
            new["seconds"] = old["seconds"]
        self._before_change()
        self.steps[i] = sq.normalize(new)
        self._changed(inspector=True)
        self._update_cards()

    def _section(self, text):
        self.insp_lay.addSpacing(6)
        lbl = heading(text, 13)
        self.insp_lay.addWidget(lbl)
        return lbl

    def _head_picker(self, i, step):
        host = self.host
        self._heads_title = self._section(f"Heads ({len(step['heads'])} selected)")
        quick = QHBoxLayout()
        prev = self._previous_heads(i)
        b_prev = QPushButton("Same as before")
        b_prev.setToolTip("Pick the same heads as the previous Dose or Staccato step.")
        b_prev.setEnabled(bool(prev))
        b_prev.clicked.connect(lambda: self._set_heads(i, prev))
        b_none = QPushButton("Clear")
        b_none.setToolTip("Pick no heads.")
        b_none.clicked.connect(lambda: self._set_heads(i, []))
        quick.addWidget(b_prev)
        quick.addWidget(b_none)
        quick.addStretch(1)
        self.insp_lay.addLayout(quick)
        layout = host.rack_layout()
        account = [d.get("did") for d in host.cloud_devices]
        groups = []
        for stack in layout["stacks"]:
            dids = [d for d in stack["slots"] if d]
            if dids:
                groups.append((stack["name"], dids))
        loose = rack.unplaced(layout, account)
        if loose:
            groups.append(("Not in the rack", loose))
        listed = {d for _n, dids in groups for d in dids}
        missing = [d for d in dict.fromkeys(did for did, _ch in step["heads"]) if d not in listed]
        if missing:
            groups.append(("Not on your Jebao account", missing))
        if not groups:
            self.insp_lay.addWidget(
                grey("No pumps yet. Log in (⚙ Settings) so the app can list your pumps.", 12)
            )
            return
        ctx = host.plan_context()
        for name, dids in groups:
            self.insp_lay.addWidget(grey(name, 11))
            for did in dids:
                row = QHBoxLayout()
                row.setSpacing(3)
                online = ctx.online(did)
                pump_name = host.get_device_name(did)
                pump = QPushButton()
                pump.setFlat(True)
                width = 130 if self._insp_wide else 96
                pump.setFixedWidth(width)
                pump.setText(pump.fontMetrics().elidedText(pump_name, Qt.TextElideMode.ElideRight, width - 8))
                pump.setStyleSheet("text-align: left;" + (" color: #7D828C;" if online is False else ""))
                cloud = getattr(host, "cloud_note", lambda _did: "")(did)
                pump.setToolTip(
                    f"{pump_name}"
                    + (f" ({cloud})" if cloud else "")
                    + (" (offline right now)" if online is False else "")
                    + ": click to pick or clear all four heads."
                )
                pump.clicked.connect(lambda _c, did=did: self._toggle_pump(i, did))
                row.addWidget(pump)
                for ch in sq.CHANNELS:
                    head = host.config.head_name(did, ch)
                    chip = QPushButton(sq.short_head_name(head, host.channel_number(did, ch)))
                    chip.setCheckable(True)
                    chip.setMinimumWidth(30)
                    chip.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
                    chip.clicked.connect(lambda _c, did=did, ch=ch: self._toggle_head(i, did, ch))
                    self._chips[(did, ch)] = chip
                    row.addWidget(chip)
                self.insp_lay.addLayout(row)
        self.insp_lay.addWidget(
            grey("Names come from the Rack tab. Dashed = not calibrated; grey = pump offline.", 11)
        )
        self._style_chips(i)

    def _style_chips(self, i):
        step = self.steps[i]
        picked = {tuple(h) for h in step["heads"]}
        ctx = self.host.plan_context()
        dark = is_dark(self)
        for (did, ch), chip in self._chips.items():
            on = (did, ch) in picked
            online = ctx.online(did)
            rate = ctx.rate(did, ch)
            when = ctx.calibrated_at(did, ch)
            uncal = rate is None or (not when and abs(rate - sq.DEFAULT_RATE) < 1e-9)
            border = "1px dashed #D0802C" if uncal else ("1px solid #4C8DFF" if on else "1px solid #4A4E56")
            if on:
                bg, fg = "#2F6FD6", "white"
            elif online is False:
                bg, fg = "rgba(128,128,128,0.04)", "#6B7079"
            else:
                bg, fg = "rgba(128,128,128,0.14)", ("#C9CCD2" if dark else "#2C2F35")
            chip.blockSignals(True)
            chip.setChecked(on)
            chip.blockSignals(False)
            chip.setStyleSheet(
                f"QPushButton {{ border: {border}; border-radius: 6px; background: {bg}; color: {fg};"
                " padding: 5px 4px; font-size: 12px; }"
            )
            tip = [self.host.head_label(did, ch)]
            tip.append(f"{rate:.3f} mL/min" if rate else "no flow rate")
            if uncal:
                tip.append("not calibrated yet")
            elif when:
                tip.append(f"calibrated {when}")
            if online is False:
                tip.append("pump offline")
            chip.setToolTip(" · ".join(tip))
            chip.setAccessibleName(", ".join(tip) + (", selected" if on else ""))
        if getattr(self, "_heads_title", None) is not None:
            self._heads_title.setText(f"Heads ({len(picked)} selected)")

    def _set_heads(self, i, heads):
        self._edit(i, key="heads", heads=copy.deepcopy(heads))
        self._style_chips(i)
        self._update_cards()

    def _toggle_head(self, i, did, ch):
        heads = [list(h) for h in self.steps[i]["heads"]]
        if [did, ch] in heads:
            heads.remove([did, ch])
        else:
            heads.append([did, ch])
        heads.sort(key=lambda h: (self._pump_order(h[0]), h[1]))
        self._set_heads(i, heads)

    def _toggle_pump(self, i, did):
        heads = [list(h) for h in self.steps[i]["heads"]]
        mine = [h for h in heads if h[0] == did]
        if len(mine) == 4:
            heads = [h for h in heads if h[0] != did]
        else:
            heads = [h for h in heads if h[0] != did] + [[did, ch] for ch in sq.CHANNELS]
        heads.sort(key=lambda h: (self._pump_order(h[0]), h[1]))
        self._set_heads(i, heads)

    def _pump_order(self, did):
        order = rack.order(self.host.rack_layout(), [d.get("did") for d in self.host.cloud_devices])
        return order.index(did) if did in order else len(order)

    def _amount(self, i, step):
        kind = step["kind"]
        self._section("Amount")
        by_time = kind == "dose" and step["by"] == "time"
        if kind == "dose":
            seg = QHBoxLayout()
            grp = QButtonGroup(self.insp)
            for text, by in (("Volume (mL)", "volume"), ("Time (min : s)", "time")):
                b = QPushButton(text)
                b.setCheckable(True)
                b.setChecked(step["by"] == by)
                b.setStyleSheet(SEGMENTED)
                b.clicked.connect(lambda _c, by=by: self._edit(i, inspector=True, by=by))
                grp.addButton(b)
                seg.addWidget(b)
            self.insp_lay.addLayout(seg)
        form = QGridLayout()
        if by_time:
            form.addWidget(grey("Time", 12), 0, 0)
            dur = DurationEdit(step["seconds"])
            dur.changed.connect(lambda v: self._edit(i, key="seconds", seconds=v))
            form.addWidget(dur, 0, 1)
        else:
            form.addWidget(grey("Total volume" if kind == "staccato" else "Volume", 12), 0, 0)
            vol = QDoubleSpinBox()
            vol.setRange(0, 100000)
            vol.setDecimals(2)
            vol.setSuffix(" mL")
            vol.setValue(step["volume"])
            vol.valueChanged.connect(lambda v: self._edit(i, key="volume", volume=v))
            form.addWidget(vol, 0, 1)
        if kind == "staccato":
            form.addWidget(grey("Burst", 12), 1, 0)
            burst = QDoubleSpinBox()
            burst.setRange(0, 10000)
            burst.setDecimals(2)
            burst.setSuffix(" mL")
            burst.setValue(step["burst"])
            burst.valueChanged.connect(lambda v: self._edit(i, key="burst", burst=v))
            form.addWidget(burst, 1, 1)
            form.addWidget(grey("Wait between", 12), 2, 0)
            gap = DurationEdit(step["gap"])
            gap.changed.connect(lambda v: self._edit(i, key="gap", gap=v))
            form.addWidget(gap, 2, 1)
        self.insp_lay.addLayout(form)
        bg = QCheckBox("Run in the background")
        bg.setChecked(bool(step["background"]))
        bg.toggled.connect(lambda on: (self._edit(i, background=on), self._update_cards()))
        self.insp_lay.addWidget(bg)
        self.insp_lay.addWidget(
            grey(
                "In the background, the next step starts right away. A later Sync columns step waits for it.",
                11,
            )
        )

    def _lane_picker(self, i, step):
        self._section("Channels")
        grid = QGridLayout()
        lanes = set(sq.lanes_of(step, self._ctx()))
        every = sorted(set(self.host.all_lanes()) | lanes)
        for n, ch in enumerate(every):
            b = QPushButton(f"CH{ch}")
            b.setCheckable(True)
            b.setChecked(ch in lanes)
            b.setStyleSheet(SEGMENTED)
            b.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            b.clicked.connect(lambda on, ch=ch: self._toggle_lane(i, ch, on))
            grid.addWidget(b, n // 4, n % 4)
        b_all = QPushButton("All")
        b_all.setToolTip("Every channel: " + sq.lanes_text(self.host.all_lanes()))
        b_all.clicked.connect(lambda: self._edit(i, inspector=True, cols=self.host.all_lanes()))
        grid.addWidget(b_all, 0, 4)
        self.insp_lay.addLayout(grid)
        kind = step["kind"]
        text = {
            "wait": "These channels wait; the others carry on.",
            "review": "Each of these channels stops here until you press Continue on the orange bar "
            "(or in the Running view).",
            "sync": "Every one of these channels waits here until all of them, and anything they "
            "started in the background, have caught up.",
            "sequence": "The saved sequence runs on these channels (each only its own heads).",
        }.get(kind, "")
        self.insp_lay.addWidget(grey(text, 11))

    def _toggle_lane(self, i, ch, on):
        lanes = set(sq.lanes_of(self.steps[i], self._ctx()))
        if on:
            lanes.add(ch)
        else:
            lanes.discard(ch)
        if not lanes:  # at least one: put it back
            self._rebuild_inspector()
            return
        self._edit(i, key="cols", cols=sorted(lanes))
        self._update_cards()

    def _update_estimate(self):
        if getattr(self, "_estimate", None) is None:
            return
        i = getattr(self, "_insp_row", None)
        if i is None or i >= len(self.steps):
            return
        try:
            self._estimate.setText(self._estimate_text(i, self.steps[i]))
        except RuntimeError:  # the label was just replaced
            return
        clear_layout(self._warn_box)
        for c in self.plan.checks if self.plan else []:
            if c.top == i and c.level in ("error", "warn", "info"):
                self._warn_box.addWidget(check_row(c))
        if getattr(self, "_chips", None):
            self._style_chips(i)

    def _estimate_text(self, i, step):
        plan, ctx = self.plan, self._ctx()
        kind = step["kind"]
        lanes = sq.lanes_text(sq.lanes_of(step, ctx))
        if kind in sq.PUMPING:
            heads = sq.heads_of(step)
            if not heads:
                return "Pick the heads this step pumps on."
            rates = [ctx.rate(*h) for h in heads]
            known = [r for r in rates if r]
            patterns = [sq.pump_pattern(step, r) for r in rates]
            pumping = [sum(s for k, s in pat if k == "pump") for pat in patterns if pat]
            parts = []
            if step["kind"] == "dose" and step["by"] == "time":
                parts.append(f"Each head pumps {sq.fmt_time(step['seconds'])}")
                if known:
                    lo, hi = min(known) * step["seconds"] / 60, max(known) * step["seconds"] / 60
                    parts[-1] += f", about {lo:.2f} mL" + (f"–{hi:.2f} mL" if hi - lo > 0.005 else "")
                parts[-1] += "."
            elif pumping:
                lo, hi = min(pumping), max(pumping)
                rng = sq.fmt_time(lo) if hi - lo < 0.5 else f"{sq.fmt_time(lo)}–{sq.fmt_time(hi)}"
                rate_txt = f"{min(known):.2f}" + (
                    f"–{max(known):.2f}" if max(known) - min(known) > 0.005 else ""
                )
                parts.append(f"Each head pumps {rng} at its calibrated rate ({rate_txt} mL/min).")
            if len(pumping) < len(heads) and not (step["kind"] == "dose" and step["by"] == "time"):
                parts.append("A head without a flow rate can't be timed: calibrate it first.")
            if kind == "staccato" and step["burst"] > 0:
                n = sq.burst_count(step)
                gaps = max(0, n - 1) * step["gap"]
                parts.append(f"{n} bursts each, with {sq.fmt_time(gaps)} of waits between them.")
            if len(heads) > 1:
                parts.append(f"All {len(heads)} heads start together ({lanes}).")
            rest = plan.step_rest_s.get(i, 0.0) if plan else 0.0
            if rest > 0.5:
                parts.append(
                    f"With the on-time limit the heads rest {sq.fmt_time(rest, approx=True)} in all, so this "
                    f"step takes {sq.fmt_time(plan.step_s.get(i), approx=True)}."
                )
            if step["background"]:
                parts.append("It runs in the background: the next step starts right away.")
            return " ".join(parts)
        if kind == "wait":
            return f"No pumping. {lanes} wait {sq.fmt_time(step['seconds'])}."
        if kind == "review":
            return "No pumping. Each channel waits until you continue."
        if kind == "sync":
            t = plan.step_s.get(i) if plan else 0
            extra = f" Here the fastest waits about {sq.fmt_time(t)}." if t and t > 0.5 else ""
            return "No pumping. Waits for the slowest channel and the background steps." + extra
        sub = self._library().get(step["name"]) if step["name"] else None
        if sub is None:
            return "Pick a saved sequence to run here."
        t = plan.step_s.get(i) if plan else None
        return f"Runs the {len(sub)} steps of “{step['name']}” here, about {sq.fmt_time(t)}."

    # --- the library (saved sequences) --------------------------------------------------------

    def _last_runs(self):
        path = getattr(self.host, "run_history_path", None)
        if not path:
            return {}
        try:
            stamp = os.path.getmtime(path)
        except OSError:
            return {}
        if self._history_cache[0] != stamp:
            self._history_cache = (stamp, sq.last_runs_by_name(path))
        return self._history_cache[1]

    def _meta_for(self, steps):
        key = json.dumps(steps, sort_keys=True, default=str)
        if key not in self._meta_cache:
            try:
                plan = self.host.plan_sequence(steps)
                t = sq.fmt_time(plan.total_s, approx=plan.rest_s > 0.5) if steps else "empty"
            except Exception:  # an odd saved sequence must not break the list
                t = "?"
            self._meta_cache[key] = t
        return self._meta_cache[key]

    def refresh_library(self):
        lib = self._library()
        q = self.search.text().strip().lower()
        last = self._last_runs()
        current = self.library.currentItem()
        keep = current.data(Qt.ItemDataRole.UserRole) if current else self.source
        self.library.clear()
        names = sorted(lib, key=rack.natural_key)
        for name in names:
            if q and q not in name.lower():
                continue
            steps = lib[name] or []
            meta = f"{len(steps)} step{'s' if len(steps) != 1 else ''} · {self._meta_for(steps)}"
            if name in last:
                meta += " · ran " + _day(last[name])
            if name == self.source:
                meta = "open" + (", edited" if self.is_unsaved() else "") + " · " + meta
            item = QListWidgetItem(name)
            item.setData(Qt.ItemDataRole.UserRole, name)
            item.setData(Qt.ItemDataRole.UserRole + 1, meta)
            item.setData(Qt.ItemDataRole.UserRole + 2, name == self.source)
            item.setToolTip(f"{name}\n{meta}\nClick to open it; right-click for more.")
            item.setData(Qt.ItemDataRole.AccessibleTextRole, f"{name}, {meta}")
            self.library.addItem(item)
            if name == keep:
                self.library.setCurrentItem(item)
        if not names:
            item = QListWidgetItem("No saved sequences yet.\nBuild one, then press Save.")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.library.addItem(item)
        if hasattr(self, "unsaved"):
            self._update_bar()

    def _library_clicked(self, item):
        # Opened a moment later: opening refills this list, which mustn't happen inside its own click.
        name = item.data(Qt.ItemDataRole.UserRole)
        QTimer.singleShot(0, lambda: self.open_sequence(name))

    def _library_selected(self):
        item = self.library.currentItem()
        name = item.data(Qt.ItemDataRole.UserRole) if item else None
        return name if name in self._library() else (self.source if self.source in self._library() else None)

    def _library_menu(self, pos):
        item = self.library.itemAt(pos)
        name = item.data(Qt.ItemDataRole.UserRole) if item else None
        if name not in self._library():
            return
        self.library.setCurrentItem(item)
        menu = QMenu(self)
        menu.addAction("Open").triggered.connect(lambda: self.open_sequence(name))
        menu.addAction("Add to queue").triggered.connect(lambda: self._queue_saved(name))
        menu.addSeparator()
        menu.addAction("Duplicate").triggered.connect(self.duplicate_saved)
        menu.addAction("Rename…").triggered.connect(self.rename_saved)
        menu.addAction("Export…").triggered.connect(self.export_sequence)
        menu.addAction("Delete").triggered.connect(self.delete_saved)
        menu.exec(self.library.viewport().mapToGlobal(pos))

    def _unique(self, name):
        lib = self._library()
        if name not in lib:
            return name
        n = 2
        while f"{name} ({n})" in lib:
            n += 1
        return f"{name} ({n})"

    def duplicate_saved(self):
        name = self._library_selected()
        if not name:
            return
        new = self._unique(f"{name} (copy)")
        self._library()[new] = copy.deepcopy(self._library()[name])
        self.host.config.save()
        self.refresh_library()
        logger.info(f"Copied the sequence “{name}” as “{new}”.")

    def rename_saved(self):
        old = self._library_selected()
        if not old:
            return
        new, ok = QInputDialog.getText(self, "Rename sequence", "New name:", text=old)
        new = (new or "").strip()
        if not ok or not new or new == old:
            return
        lib = self._library()
        if new in lib:
            QMessageBox.warning(self, "Rename sequence", f"There's already a sequence called “{new}”.")
            return
        lib[new] = lib.pop(old)
        for steps in lib.values():  # sequences that run it keep working
            for s in steps:
                if s.get("kind") == "sequence" and s.get("name") == old:
                    s["name"] = new
                    s["val1"] = new
                elif s.get("action") == "Run Sub-Sequence" and s.get("val1") == old and "kind" not in s:
                    s["val1"] = new
        for s in self.steps:
            if s["kind"] == "sequence" and s["name"] == old:
                s["name"] = new
        if self.source == old:
            self.source = new
            if self.name == old:
                self.name = new
                self.name_edit.setText(new)
        self.save_draft()
        self.refresh_library()
        self._replan()
        logger.info(f"Renamed the sequence “{old}” to “{new}”.")

    def delete_saved(self):
        name = self._library_selected()
        if not name:
            return
        users = [n for n, steps in self._library().items() if n != name and _runs(steps, name)]
        extra = f"\n\nThese sequences run it as a step: {', '.join(users)}." if users else ""
        reply = QMessageBox.question(self, "Delete sequence", f"Delete “{name}”?{extra}")
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._library().pop(name, None)
        if self.source == name:
            self.source = None
        self.save_draft()
        self.refresh_library()
        self._replan()
        logger.info(f"Deleted the sequence “{name}”.")

    def export_sequence(self):
        name = self._library_selected()
        steps = self._library()[name] if name else sq.to_saved_all(self.steps)
        name = name or self.name
        path, _ = QFileDialog.getSaveFileName(
            self, "Export sequence", f"{name}.jebao-sequence.json", "Sequences (*.json)"
        )
        if not path:
            return
        data = {"app": "Jebao Pump Sequencer", "kind": "sequence", "name": name, "steps": steps}
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except OSError as e:
            QMessageBox.warning(self, "Export sequence", f"Couldn't save the file: {e}")
            return
        logger.info(f"Exported the sequence “{name}” to {path}.")

    def import_sequence(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Import sequence", "", "Sequences (*.json);;All files (*)"
        )
        if path:
            self.import_file(path)

    def import_file(self, path):
        """Add a sequence from a file (from Export, or a plain list of steps). Returns its name."""
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, "Import sequence", f"Couldn't read that file: {e}")
            return None
        if isinstance(data, list):
            data = {
                "name": os.path.splitext(os.path.basename(path))[0].replace(".jebao-sequence", ""),
                "steps": data,
            }
        steps = data.get("steps") if isinstance(data, dict) else None
        if not isinstance(steps, list):
            QMessageBox.warning(self, "Import sequence", "That file doesn't hold a sequence.")
            return None
        name = self._unique(str(data.get("name") or "Imported sequence").strip() or "Imported sequence")
        self._library()[name] = sq.to_saved_all(steps)
        self.host.config.save()
        self.refresh_library()
        unknown = {did for s in sq.normalize_all(steps) for did, _ch in s["heads"]}
        listed = {d.get("did") for d in self.host.cloud_devices}
        if listed and unknown - listed:
            logger.warning(f"Imported “{name}”: some of its pumps aren't on your Jebao account.")
        else:
            logger.info(f"Imported the sequence “{name}” ({len(steps)} steps).")
        return name

    # --- queue and runs ----------------------------------------------------------------------

    def _ready_to_run(self, plan, name):
        errors = plan.errors
        if not errors:
            return True
        text = "\n".join(f"• {c.text}" for c in errors[:8])
        more = f"\n… and {len(errors) - 8} more." if len(errors) > 8 else ""
        reply = QMessageBox.question(
            self,
            "Run anyway?",
            f"“{name}” has problems:\n\n{text}{more}\n\nSteps with a problem will stop the run. Run it anyway?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return reply == QMessageBox.StandardButton.Yes

    def add_to_queue(self):
        self._replan()
        if self.steps and self._ready_to_run(self.plan, self.name):
            if self.host.enqueue(self.name, self.steps):
                logger.info(f"“{self.name}” is in the queue (position {len(self.host.sequence_queue)}).")

    def _queue_saved(self, name):
        steps = self._library().get(name)
        if steps and self._ready_to_run(self.host.plan_sequence(steps, name=name), name):
            self.host.enqueue(name, steps)

    def run_now(self):
        self._replan()
        if not self.steps or not self._ready_to_run(self.plan, self.name):
            return
        busy = self.host.run_active()
        if busy:
            reply = QMessageBox.question(
                self,
                "A sequence is running",
                f"“{self.host.current_running_sequence}” is running. Start “{self.name}” as soon as it "
                "finishes (first in the queue)?",
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
        if self.host.enqueue(self.name, self.steps, front=True):
            self.host.run_next_now()  # even if the queue is paused; the rest stay paused
            if not busy:
                self.show_page(2)

    def _queue_rows(self):
        """(text, queue index or None) for the queue lists: the running one, then the queued ones."""
        host = self.host
        rows = []
        if host.current_running_sequence:
            run = host.seq_run
            left = run.left_s() if run is not None and not run.ended else None
            extra = f" · ≈ {sq.fmt_time(left)} left" if left else ""
            rows.append((f"▶ RUNNING · {host.current_running_sequence}{extra}", None))
        for i, q in enumerate(host.sequence_queue):
            steps = q.get("data") or []
            rows.append((f"{i + 1}. {q.get('name', '?')} · {self._meta_for(steps)}", i))
        return rows

    def refresh_queue(self):
        rows = self._queue_rows()
        for lst in (self.queue_list, getattr(self, "rn_queue", None)):
            if lst is None:
                continue
            current = lst.currentRow()
            lst.clear()
            for text, index in rows:
                item = QListWidgetItem(text)
                item.setData(Qt.ItemDataRole.UserRole, index)
                if index is None:
                    item.setForeground(QColor("#2FB36D"))
                lst.addItem(item)
            if not rows:
                item = QListWidgetItem("Nothing queued.")
                item.setFlags(Qt.ItemFlag.NoItemFlags)
                lst.addItem(item)
            elif 0 <= current < lst.count():
                lst.setCurrentRow(current)
        for chk in (self.chk_pause_queue, getattr(self, "rn_pause_queue", None)):
            if chk is not None and chk.isChecked() != self.host.queue_paused:
                chk.blockSignals(True)
                chk.setChecked(self.host.queue_paused)
                chk.blockSignals(False)

    def _queued_index(self, lst=None):
        item = (lst or self.queue_list).currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _move_queued(self, delta, lst=None):
        i = self._queued_index(lst)
        q = self.host.sequence_queue
        if i is None or not 0 <= i + delta < len(q):
            return
        q[i], q[i + delta] = q[i + delta], q[i]
        self.refresh_queue()
        target = lst or self.queue_list
        for r in range(target.count()):
            if target.item(r).data(Qt.ItemDataRole.UserRole) == i + delta:
                target.setCurrentRow(r)

    def _remove_queued(self, lst=None):
        i = self._queued_index(lst)
        q = self.host.sequence_queue
        if i is None or not 0 <= i < len(q):
            return
        gone = q.pop(i)
        self.refresh_queue()
        logger.info(f"Took “{gone.get('name')}” out of the queue.")

    # --- the Preview page --------------------------------------------------------------------

    def _preview_page(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 6, 8, 8)
        top = QHBoxLayout()
        back = QPushButton("← Back to steps")
        back.clicked.connect(lambda: self.show_page(0))
        top.addWidget(back)
        self.pv_title = heading("Preview", 16)
        top.addWidget(self.pv_title, 1)
        self.pv_run = QPushButton("▶ Run now")
        self.pv_run.setStyleSheet(GO)
        self.pv_run.clicked.connect(self.run_now)
        top.addWidget(self.pv_run)
        lay.addLayout(top)
        lay.addWidget(
            grey(
                "What the run will do, worked out from the calibrated flow rates and the on-time limit. "
                "Nothing is sent to the pumps.",
                12,
            )
        )
        cards = QHBoxLayout()
        self.card_total = StatCard("Total time")
        self.card_finish = StatCard("If started at")
        self.start_time = QTimeEdit(QTime.currentTime())
        self.start_time.setDisplayFormat("HH:mm")
        self.start_time.timeChanged.connect(lambda _t: self.refresh_preview())
        self.card_finish.extra.insertWidget(1, self.start_time)
        self.card_needed = StatCard("You're needed")
        self.card_liquid = StatCard("Liquid to prepare")
        for c in (self.card_total, self.card_finish, self.card_needed, self.card_liquid):
            c.setMinimumWidth(180)
            cards.addWidget(c, 1)
        lay.addLayout(cards)

        th = QHBoxLayout()
        th.addWidget(heading("Timeline by channel", 13))
        th.addStretch(1)
        grp = QButtonGroup(w)
        self.pv_with = QPushButton("With on-time limit")
        self.pv_without = QPushButton("Without")
        for b in (self.pv_with, self.pv_without):
            b.setCheckable(True)
            b.setStyleSheet(SEGMENTED)
            b.clicked.connect(lambda _c: self.refresh_preview())
            grp.addButton(b)
            th.addWidget(b)
        self.pv_with.setChecked(True)
        lay.addLayout(th)
        self.timeline = Timeline()
        lay.addWidget(self.timeline)
        legend = QHBoxLayout()
        for color, text in (
            (SEGMENT["pump"], "Dose"),
            (SEGMENT["staccato"], "Staccato (pumping)"),
            (SEGMENT["rest"], "Resting for the on-time limit"),
            (SEGMENT["review"], "Waits for you"),
            (SEGMENT["wait"], "Timer"),
            (SEGMENT["sync"], "Waiting for other channels"),
        ):
            sw = QLabel()
            sw.setFixedSize(12, 12)
            sw.setStyleSheet(f"background: {color}; border-radius: 3px;")
            legend.addWidget(sw)
            legend.addWidget(grey(text, 11))
            legend.addSpacing(10)
        legend.addStretch(1)
        lay.addLayout(legend)
        lay.addWidget(grey("Thin bars are steps running in the background. Point at a bar for details.", 11))

        lay.addWidget(heading("Per head", 13))
        self.head_table = QTableWidget(0, 7)
        self.head_table.setHorizontalHeaderLabels(
            ["Head", "Liquid", "Rate (mL/min)", "Volume", "Pumping", "Resting", "Check"]
        )
        self.head_table.verticalHeader().setVisible(False)
        self.head_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.head_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        hh = self.head_table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for c in range(1, 7):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        self.head_table.setMinimumHeight(140)
        lay.addWidget(self.head_table)
        lay.addWidget(heading("Checks", 13))
        self.pv_checks = QVBoxLayout()
        lay.addLayout(self.pv_checks)
        lay.addStretch(1)
        scroll.setWidget(w)
        return scroll

    def _lane_subs(self, plan):
        """A short name for each lane in the timeline: the heads' short names ('Col 1')."""
        ctx = self.host.plan_context()
        out = {}
        for lane in plan.used_lanes:
            names = []
            for key in plan.heads:
                if ctx.lane_of(*key) == lane:
                    names.append(sq.short_head_name(ctx.head_name(*key), lane))
            names = list(dict.fromkeys(n for n in names if n != f"CH{lane}"))
            out[lane] = ", ".join(names[:3]) + ("…" if len(names) > 3 else "")
        return out

    def refresh_preview(self):
        if self._plan_timer.isActive():  # an edit that hasn't been worked out yet
            self._replan()
            return
        if self.plan is None:
            return
        plan = self.plan
        if self._plan_nolimit is None:
            try:
                self._plan_nolimit = self.host.plan_sequence(self.steps, name=self.name, with_limit=False)
            except Exception:
                self._plan_nolimit = plan
        free = self._plan_nolimit
        self.pv_title.setText(f"Preview · {self.name}")
        self.pv_run.setEnabled(bool(self.steps))
        self.card_total.value.setText(sq.fmt_time(plan.total_s) if self.steps else "—")
        notes = []
        if plan.rest_s > 0.5:
            notes.append(f"{sq.fmt_time(free.total_s)} without the on-time limit")
        if plan.reviews:
            notes.append("plus the time you take at the Wait for me steps")
        if plan.unknown:
            notes.append("some steps can't be timed (no flow rate)")
        self.card_total.note.setText("; ".join(notes))
        start_t = self.start_time.time()
        now = datetime.now()
        start = now.replace(hour=start_t.hour(), minute=start_t.minute(), second=0, microsecond=0)
        if start < now - timedelta(minutes=1):
            start += timedelta(days=1)
        finish = start + timedelta(seconds=plan.total_s)
        self.card_finish.value.setText("finishes ≈ " + _clock(finish, start))
        self.card_finish.note.setText("" if start.date() == now.date() else "(tomorrow)")
        first = plan.first_review()
        if first:
            t, top, lane = first
            when = start + timedelta(seconds=t)
            self.card_needed.value.setText(f"after {sq.fmt_time(t)} (≈ {_clock(when, start)})")
            n = len({tp for _t, tp, _l in plan.reviews})
            more = f" · {n} Wait for me steps" if n > 1 else ""
            note = f"Step {top + 1} · {sq.title(self.steps[top])} (CH{lane}){more}"
            message = self.steps[top].get("message", "")
            if message:
                note += f"\n“{message.splitlines()[0]}”"
            self.card_needed.note.setText(note)
        else:
            self.card_needed.value.setText("Not during the run")
            self.card_needed.note.setText("There are no Wait for me steps.")
        if plan.liquids:
            self.card_liquid.value.setText(sq.liquids_text(plan.liquids))
            self.card_liquid.note.setText(
                f"≈ {sq.fmt_ml(sum(plan.liquids.values()))} in all, through to waste"
            )
        else:
            self.card_liquid.value.setText("—")
            self.card_liquid.note.setText("")
        shown = plan if self.pv_with.isChecked() else free
        self.timeline.set_plan(shown, self.steps, self._lane_subs(shown))

        ctx = self.host.plan_context()
        heads = sorted(plan.heads.values(), key=lambda h: (self._pump_order(h.key[0]), h.key[1]))
        self.head_table.setRowCount(len(heads))
        warn_by_head = {}
        for c in plan.checks:
            for hp in heads:
                if hp.label in c.text and c.level in ("error", "warn"):
                    warn_by_head.setdefault(hp.key, c.text.split(": ", 1)[-1])
        for r, hp in enumerate(heads):
            online = ctx.online(hp.key[0])
            check = warn_by_head.get(hp.key) or ("pump offline" if online is False else "✓")
            values = [
                hp.label,
                hp.liquid,
                f"{hp.rate:.3f}" if hp.rate else "—",
                sq.fmt_ml(hp.volume_ml) if hp.volume_known else "?",
                sq.fmt_time(hp.pump_s),
                sq.fmt_time(hp.rest_s) if hp.rest_s > 0.5 else "—",
                check,
            ]
            for c, v in enumerate(values):
                item = QTableWidgetItem(v)
                if c == 6 and v != "✓":
                    item.setForeground(QColor("#F0B65A"))
                self.head_table.setItem(r, c, item)
        self.head_table.setFixedHeight(min(420, 34 + 30 * max(1, len(heads))))
        clear_layout(self.pv_checks)
        for c in plan.checks:
            self.pv_checks.addWidget(check_row(c))

    # --- the Running page --------------------------------------------------------------------

    def _running_page(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 6, 8, 8)
        self.rn_empty = grey(
            "Nothing is running. Build a sequence and press ▶ Run now, or add it to the queue.", 13
        )
        lay.addWidget(self.rn_empty)
        self.rn_header = QFrame()
        self.rn_header.setObjectName("runHeader")
        self.rn_header.setStyleSheet(
            "#runHeader { border: 1px solid rgba(128,128,128,0.35); border-radius: 10px; background: rgba(128,128,128,0.08); }"
        )
        hl = QVBoxLayout(self.rn_header)
        hl.setContentsMargins(14, 10, 14, 10)
        r1 = QHBoxLayout()
        self.rn_state = QLabel()
        r1.addWidget(self.rn_state)
        self.rn_name = heading("", 16)
        r1.addWidget(self.rn_name, 1)
        self.btn_pause_all = QPushButton("Pause all")
        self.btn_pause_all.setToolTip("Pause every channel: heads turn off and timers stop until you resume.")
        self.btn_pause_all.clicked.connect(self._toggle_pause_all)
        self.btn_skip = QToolButton()
        self.btn_skip.setText("Skip step…")
        self.btn_skip.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.skip_menu = QMenu(self.btn_skip)
        self.skip_menu.aboutToShow.connect(self._fill_skip_menu)
        self.btn_skip.setMenu(self.skip_menu)
        self.btn_stop = QPushButton("Stop run…")
        self.btn_stop.setStyleSheet(DANGER)
        self.btn_stop.clicked.connect(self._stop_clicked)
        for b in (self.btn_pause_all, self.btn_skip, self.btn_stop):
            r1.addWidget(b)
        hl.addLayout(r1)
        self.rn_times = grey("", 12)
        hl.addWidget(self.rn_times)
        self.rn_progress = QProgressBar()
        self.rn_progress.setRange(0, 1000)
        self.rn_progress.setTextVisible(False)
        self.rn_progress.setFixedHeight(8)
        hl.addWidget(self.rn_progress)
        lay.addWidget(self.rn_header)

        self.lane_grid = QGridLayout()
        self.lane_cards = {}  # lane -> LaneCard, made for the lanes the run uses (_lay_out_lanes)
        self._card_lanes = None
        self._grid_lanes = []
        lay.addLayout(self.lane_grid)

        lay.addWidget(heading("Progress by step", 13))
        self.grid = QTableWidget(0, 1)
        self.grid.setHorizontalHeaderLabels(["Step"])
        self.grid.verticalHeader().setVisible(False)
        self.grid.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.grid.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        gh = self.grid.horizontalHeader()
        gh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        lay.addWidget(self.grid)

        bottom = QHBoxLayout()
        nxt = QVBoxLayout()
        nxt.addWidget(heading("Up next", 13))
        self.rn_queue = QListWidget()
        self.rn_queue.setMaximumHeight(160)
        nxt.addWidget(self.rn_queue)
        qrow = QHBoxLayout()
        for text, tip, fn in (
            ("↑", "Move up", lambda: self._move_queued(-1, self.rn_queue)),
            ("↓", "Move down", lambda: self._move_queued(1, self.rn_queue)),
            ("✕", "Remove from the queue", lambda: self._remove_queued(self.rn_queue)),
        ):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.setAccessibleName(tip)
            b.setFixedWidth(40)
            b.clicked.connect(fn)
            qrow.addWidget(b)
        qrow.addStretch(1)
        nxt.addLayout(qrow)
        self.rn_pause_queue = QCheckBox("Pause the queue after this run")
        self.rn_pause_queue.toggled.connect(self.host.set_queue_paused)
        nxt.addWidget(self.rn_pause_queue)
        bottom.addLayout(nxt, 1)
        hist = QVBoxLayout()
        hist.addWidget(heading("Recent runs", 13))
        self.rn_history = QListWidget()
        self.rn_history.setMaximumHeight(200)
        hist.addWidget(self.rn_history)
        hist.addWidget(
            grey(
                "Every run is kept with its times and what it delivered, so you can see where a stopped "
                "run had got to (the runs file is in the log folder).",
                11,
            )
        )
        bottom.addLayout(hist, 1)
        lay.addLayout(bottom)
        lay.addStretch(1)
        scroll.setWidget(w)
        self.refresh_history()
        return scroll

    def _toggle_pause_all(self):
        lanes = self.host.lanes_in_use()
        paused = any(self.host._ch_paused.get(ch) for ch in lanes)
        self.host.pause_run(not paused)
        self.refresh_running()

    def _fill_skip_menu(self):
        self.skip_menu.clear()
        run = self.host.seq_run
        if run is None or run.ended:
            self.skip_menu.addAction("Nothing to skip").setEnabled(False)
            return
        any_lane = False
        for ch in self.host.lanes_in_use():
            ls = run.lanes[ch]
            if ls.top is None or ch not in self.host._ch_step_tasks:
                continue
            any_lane = True
            act = self.skip_menu.addAction(f"CH{ch}: step {ls.top + 1} · {ls.title}")
            act.triggered.connect(lambda _c, ch=ch: self.host.skip_step(ch))
        if any_lane:
            self.skip_menu.addSeparator()
            self.skip_menu.addAction("Every channel's current step").triggered.connect(
                lambda: [self.host.skip_step(ch) for ch in self.host.lanes_in_use()]
            )
        else:
            self.skip_menu.addAction("Nothing to skip right now").setEnabled(False)

    def _stop_clicked(self):
        name = self.host.current_running_sequence or "the sequence"
        reply = QMessageBox.question(
            self,
            "Stop run",
            f"Stop “{name}”? Every pump it is using turns off, and the queue is paused.",
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.host.stop_run()

    def refresh_history(self):
        path = getattr(self.host, "run_history_path", None)
        runs = sq.load_history(path, limit=15) if path else []
        self.rn_history.clear()
        for rec in runs:
            item = QListWidgetItem(f"{rec.get('name')}\n{sq.history_text(rec)}")
            delivered = rec.get("delivered") or {}
            tip = sq.history_text(rec)
            if rec.get("why"):
                tip += f"\n{rec['why']}"
            if delivered:
                tip += "\nDelivered: " + ", ".join(f"{k} {sq.fmt_ml(v)}" for k, v in delivered.items())
            item.setToolTip(tip)
            color = {"done": "#2FB36D", "failed": "#E5534B"}.get(rec.get("outcome"), "#E0A33A")
            item.setForeground(QColor(color))
            self.rn_history.addItem(item)
        if not runs:
            item = QListWidgetItem("No runs yet on this computer.")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.rn_history.addItem(item)

    def refresh_running(self):
        host = self.host
        run = host.seq_run
        self.rn_empty.setVisible(run is None)
        self.rn_header.setVisible(run is not None)
        for card in self.lane_cards.values():
            card.setVisible(run is not None)
        self.grid.setVisible(run is not None)
        if run is None:
            return
        self._lay_out_lanes(run)
        live = run.ended is None
        stopping = live and run.stopping
        lanes = host.lanes_in_use()
        paused = live and lanes and all(host._ch_paused.get(ch) for ch in lanes)
        waiting = live and any(host._ch_review_events.values())
        if not live:
            state, color = {
                "done": ("FINISHED", "#2FB36D"),
                "failed": ("FAILED", "#E5534B"),
            }.get(run.outcome, ("STOPPED", "#E0A33A"))
        elif stopping:
            state, color = "STOPPING", "#E5534B"
        elif waiting:
            state, color = "WAITING FOR YOU", "#F08A24"
        elif paused:
            state, color = "PAUSED", "#E0A33A"
        else:
            state, color = "RUNNING", "#2FB36D"
        self.rn_state.setText(state)
        self.rn_state.setStyleSheet(
            f"color: {color}; font-weight: 700; font-size: 12px; border: 1px solid {color};"
            " border-radius: 10px; padding: 2px 10px;"
        )
        used = sorted({ln for it in run.flat for ln in it.lanes})
        self.rn_name.setText(f"{run.name}  ·  {sq.lanes_text(used)}")
        started = datetime.fromtimestamp(run.started)
        parts = [f"Started {started:%H:%M}", f"{'running' if live else 'took'} {sq.fmt_clock(run.elapsed())}"]
        if live:
            left = run.left_s()
            if left is not None:
                finish = datetime.now() + timedelta(seconds=left)
                parts.append(f"≈ {sq.fmt_time(left)} left")
                parts.append(f"finishes about {_clock(finish, datetime.now())}")
                if run.plan is not None and run.plan.reviews:
                    parts[-1] += " plus your checks"
        else:
            ended = datetime.fromtimestamp(run.ended)
            parts.append(f"ended {ended:%H:%M}")
            if run.outcome != "done":
                parts.append((f"stopped at {run.stopped_at}" if run.stopped_at else "") + f" ({run.why})")
        self.rn_times.setText(" · ".join(p for p in parts if p))
        self.rn_progress.setValue(int(run.progress() * 1000))
        self.btn_pause_all.setVisible(live and not stopping)
        self.btn_pause_all.setText(
            "Resume all" if any(host._ch_paused.get(ch) for ch in lanes) else "Pause all"
        )
        self.btn_skip.setVisible(live and not stopping)
        self.btn_stop.setVisible(live and not stopping)
        for ln, card in self.lane_cards.items():
            card.show_state(run)
            card.setVisible(ln in used)
        self._refresh_grid(run)

    def _lay_out_lanes(self, run):
        """A card per lane the run uses, four to a row (eight with two stacks)."""
        used = sorted({ln for it in run.flat for ln in it.lanes}) or sorted(run.lanes)
        if used == self._card_lanes:
            return
        self._card_lanes = used
        for card in self.lane_cards.values():
            self.lane_grid.removeWidget(card)
            card.setParent(None)
            card.deleteLater()
        self.lane_cards = {ln: LaneCard(self, ln) for ln in used}
        for n, ln in enumerate(used):
            self.lane_grid.addWidget(self.lane_cards[ln], n // 4, n % 4)

    def _refresh_grid(self, run):
        n = len(run.steps)
        lanes = sorted({ln for it in run.flat for ln in it.lanes}) or sorted(run.lanes)
        if self.grid.rowCount() != n or self._grid_cache.get("run") is not run or lanes != self._grid_lanes:
            self._grid_cache = {"run": run}
            self._grid_lanes = lanes
            self.grid.setColumnCount(1 + len(lanes))
            self.grid.setHorizontalHeaderLabels(["Step"] + [f"CH{ln}" for ln in lanes])
            gh = self.grid.horizontalHeader()
            gh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            for c in range(1, 1 + len(lanes)):
                gh.setSectionResizeMode(c, QHeaderView.ResizeMode.Fixed)
                self.grid.setColumnWidth(c, 96 if len(lanes) <= 4 else 72)
            self.grid.setRowCount(n)
            for r, step in enumerate(run.steps):
                self.grid.setItem(r, 0, QTableWidgetItem(f"{r + 1}. {sq.title(step)}"))
                for c in range(1, 1 + len(lanes)):
                    self.grid.setItem(r, c, QTableWidgetItem(""))
            self.grid.setFixedHeight(min(360, 32 + 28 * max(1, n)))
        for r in range(n):
            for c, ln in enumerate(lanes, start=1):
                state = run.cells.get((r, ln))
                if self._grid_cache.get((r, ln)) == state:
                    continue
                self._grid_cache[(r, ln)] = state
                item = self.grid.item(r, c)
                if state is None:
                    item.setText("")
                    continue
                text, color = CELL.get(state, (state, "#9AA0A9"))
                item.setText(text)
                item.setForeground(QColor(color))
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

    # --- called by the main window ------------------------------------------------------------

    def refresh(self):
        """A few times a second while the tab is in view."""
        self._update_nav()
        if self.pages.currentIndex() == 2:
            self.refresh_running()
        sig = tuple(sorted((d.get("did"), bool(d.get("is_online"))) for d in self.host.cloud_devices))
        if sig != self._online_sig and self.pages.currentIndex() != 2:
            self._online_sig = sig  # a pump went offline or came back: check again
            self._plan_timer.start()

    def _update_nav(self):
        host = self.host
        run = host.seq_run
        if host.run_active() and run is not None:
            left = run.left_s()
            waiting = sorted(ch for ch, event in host._ch_review_events.items() if event)
            text = f"● Running “{run.name}”"
            if waiting:
                text = f"● Waiting for you: {sq.lanes_text(waiting)} · “{run.name}”"
            elif left:
                text += f" · ≈ {sq.fmt_time(left)} left"
            self.nav_status.setText(text)
            self.nav_status.setStyleSheet(f"color: {'#F08A24' if waiting else '#2FB36D'}; font-weight: 600;")
            self.nav_status.setVisible(True)
            self.nav_buttons[2].setText("Running ●")
        else:
            queued = len(host.sequence_queue)
            if queued and host.queue_paused:
                self.nav_status.setText(f"Queue paused · {queued} waiting")
                self.nav_status.setStyleSheet("color: #E0A33A;")
            else:
                self.nav_status.setText("")
            self.nav_status.setVisible(bool(self.nav_status.text()))
            self.nav_buttons[2].setText("Running")

    def rebuild(self):
        """Pumps or names changed: show them again."""
        self._meta_cache.clear()
        self._replan()
        self._rebuild_inspector()
        self.refresh_library()

    def run_finished(self, run):
        self.refresh_history()
        self.refresh_queue()
        self._history_cache = (None, [])
        self._update_bar()
        self.refresh_library()
        if self.pages.currentIndex() == 2:
            self.refresh_running()


def _clock(when, ref):
    """'18:15', or 'Fri 02:10' on another day."""
    return f"{when:%H:%M}" if when.date() == ref.date() else f"{when:%a %H:%M}"


def _day(iso):
    """'today', 'yesterday' or 'Tue 28 Oct' for an ISO time."""
    try:
        when = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return "?"
    today = datetime.now().date()
    if when.date() == today:
        return f"today {when:%H:%M}"
    if when.date() == today - timedelta(days=1):
        return "yesterday"
    return f"{when:%a %d %b}"


def _runs(steps, name):
    """True when saved ``steps`` run the sequence ``name`` as a step."""
    return any(s["kind"] == "sequence" and s["name"] == name for s in sq.normalize_all(steps))
