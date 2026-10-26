"""The connection report: how each pump's Wi-Fi link and this computer's link to the
Jebao cloud have been doing. Opened from the pump count in the status bar or from Settings.

The main window (``host``) provides ``netlog`` (netwatch.ConnectionLog),
``get_device_name(did)``, ``cloud_devices``, ``check_pumps_now()``, ``open_log_folder()``
and ``config``.
"""

import time
from datetime import datetime

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QGuiApplication
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

import netwatch
from netwatch import fmt_duration

GREEN, RED, AMBER, GREY = "#2FB36D", "#E05A5A", "#E0A33A", "#8A8F98"

TIPS = (
    "<b>If pumps keep dropping off the travel router</b> (settings to look for in its admin page):"
    "<ul style='margin-top:2px'>"
    "<li>Give the pumps a 2.4 GHz network of their own: turn off band steering / “smart connect”, "
    "and use WPA2-PSK (AES), not WPA3 or WPA2/WPA3 mixed.</li>"
    "<li>Fix the 2.4 GHz channel to 1, 6 or 11 at 20 MHz width, and allow 802.11b/g/n.</li>"
    "<li>Make the DHCP pool and the client limit big enough for every pump plus phones and PCs, "
    "and reserve an address for each pump (the MAC addresses are in the table).</li>"
    "<li>Power the router from a wall adapter, not a PC USB port, and update its firmware.</li>"
    "<li>If it gets its internet over Wi-Fi (repeater/WISP mode), a wired uplink is steadier. "
    "A nightly scheduled reboot is a good stop-gap.</li>"
    "</ul>"
)


def _ago(t, now):
    if t is None:
        return "—"
    return "just now" if now - t < 5 else f"{fmt_duration(now - t)} ago"


def _clock(t):
    if t is None:
        return "—"
    d = datetime.fromtimestamp(t)
    return d.strftime("%H:%M") if d.date() == datetime.now().date() else d.strftime("%d %b %H:%M")


class NetworkDialog(QDialog):
    COLS = [
        ("Pump", 170),
        ("Now", 80),
        ("Since", 110),
        ("Drops\n24 h", 60),
        ("Drops\n7 days", 60),
        ("Offline\n24 h", 80),
        ("Longest\noutage (7 d)", 100),
        ("Last seen\nonline", 100),
        ("MAC address", 140),
    ]

    def __init__(self, host, parent=None):
        super().__init__(parent)
        self.host = host
        self.setWindowTitle("Pump connections")
        self.resize(1000, 560)
        lay = QVBoxLayout(self)
        self.lbl_cloud = QLabel()
        self.lbl_cloud.setWordWrap(True)
        self.lbl_cloud.setTextFormat(Qt.TextFormat.RichText)
        lay.addWidget(self.lbl_cloud)

        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels([c[0] for c in self.COLS])
        hdr = self.table.horizontalHeader()
        hdr.setDefaultAlignment(Qt.AlignmentFlag.AlignCenter)
        for i, (_name, width) in enumerate(self.COLS):
            self.table.setColumnWidth(i, width)
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(30)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        lay.addWidget(self.table, 1)

        keep = int(host.config.data.get("log_keep_days", 30))
        self.lbl_note = QLabel(
            f"The app asks the Jebao cloud every {netwatch.POLL_S} s which pumps are online, so a drop "
            f"shorter than that can be missed. The history is kept on this computer for {keep} days "
            "(the connection-….jsonl file in the log folder), and every drop and reconnection is also in "
            "the System Log."
        )
        self.lbl_note.setWordWrap(True)
        self.lbl_note.setStyleSheet("color: grey; font-size: 11px;")
        lay.addWidget(self.lbl_note)
        tips = QLabel(TIPS)
        tips.setWordWrap(True)
        tips.setTextFormat(Qt.TextFormat.RichText)
        tips.setStyleSheet("font-size: 12px;")
        lay.addWidget(tips)

        btns = QHBoxLayout()
        self.btn_check = QPushButton("Check now")
        self.btn_check.clicked.connect(self.check_now)
        btn_copy = QPushButton("Copy report")
        btn_copy.setToolTip("Copy the table as text, e.g. to paste into an email or a lab notebook.")
        btn_copy.clicked.connect(self.copy_report)
        btn_logs = QPushButton("Open Log Folder")
        btn_logs.clicked.connect(host.open_log_folder)
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.accept)
        btns.addWidget(self.btn_check)
        btns.addWidget(btn_copy)
        btns.addWidget(btn_logs)
        btns.addStretch(1)
        btns.addWidget(btn_close)
        lay.addLayout(btns)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(2000)
        self.refresh()

    def check_now(self):
        self.host.check_pumps_now()
        self.refresh()

    def rows(self):
        """The report as data: one dict per pump, in rack order."""
        log = self.host.netlog
        out = []
        for d in self.host.cloud_devices:
            did = d.get("did")
            s = log.summary(did)
            s["name"] = self.host.get_device_name(did)
            s["mac"] = s["mac"] or d.get("mac", "")
            if s["online"] is None:
                s["online"] = bool(d.get("is_online", False))
            out.append(s)
        return out

    def refresh(self):
        now = time.time()
        log = self.host.netlog
        c = log.cloud_summary()
        if c["ok"] is None:
            cloud = "<span style='color:grey'>Not checked yet (log in first).</span>"
        elif c["ok"]:
            speed = ""
            if c["median_s"] is not None:
                speed = f" · usual reply {c['median_s']:.1f} s, slowest {c['slowest_s']:.1f} s"
            failed = (
                f" · <span style='color:{AMBER}'>{c['failed']} failed checks today</span>"
                if c["failed"]
                else ""
            )
            cloud = (
                f"<span style='color:{GREEN}'>●</span> This computer reaches the Jebao cloud"
                f" · checked {_ago(c['last'], now)}{speed}{failed}"
            )
        else:
            cloud = (
                f"<span style='color:{RED}'>●</span> <b>This computer can't reach the Jebao cloud</b> "
                f"({c['problem']}). The pumps' status below is from the last good check. Check this "
                "computer's internet first."
            )
        rows = self.rows()
        n_on = sum(1 for r in rows if r["online"])
        self.lbl_cloud.setText(f"{cloud}<br><b>{n_on} of {len(rows)} pumps online.</b>")
        t = self.table
        t.setRowCount(len(rows))
        center = Qt.AlignmentFlag.AlignCenter
        left = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        for i, r in enumerate(rows):
            on = r["online"]
            cells = [
                (r["name"], left, None),
                ("Online" if on else "Offline", center, GREEN if on else RED),
                (_clock(r["since"]), center, None),
                (str(r["drops_day"]), center, AMBER if r["drops_day"] else None),
                (str(r["drops_week"]), center, AMBER if r["drops_week"] else None),
                (fmt_duration(r["offline_day_s"]) if r["offline_day_s"] else "—", center, None),
                (fmt_duration(r["longest_week_s"]) if r["longest_week_s"] else "—", center, None),
                ("now" if on else _ago(r["last_online"], now), center, None),
                (r["mac"] or "—", left, GREY),
            ]
            for col, (val, align, fg) in enumerate(cells):
                item = t.item(i, col)
                if item is None:
                    item = QTableWidgetItem()
                    t.setItem(i, col, item)
                item.setText(val)
                item.setTextAlignment(align)
                item.setForeground(QColor(fg) if fg else t.palette().text().color())

    def report_text(self):
        now = time.time()
        c = self.host.netlog.cloud_summary()
        lines = [f"Jebao pump connections, {datetime.now():%Y-%m-%d %H:%M}"]
        if c["ok"] is False:
            lines.append(f"This computer can't reach the Jebao cloud: {c['problem']}")
        elif c["median_s"] is not None:
            lines.append(
                f"Cloud checks today: {c['checks']} ({c['failed']} failed), usual reply {c['median_s']:.1f} s"
            )
        lines.append("")
        lines.append(
            "Pump\tNow\tSince\tDrops 24 h\tDrops 7 d\tOffline 24 h\tLongest outage 7 d\tLast seen\tMAC"
        )
        for r in self.rows():
            lines.append(
                "\t".join(
                    [
                        r["name"],
                        "online" if r["online"] else "OFFLINE",
                        _clock(r["since"]),
                        str(r["drops_day"]),
                        str(r["drops_week"]),
                        fmt_duration(r["offline_day_s"]),
                        fmt_duration(r["longest_week_s"]),
                        "now" if r["online"] else _ago(r["last_online"], now),
                        r["mac"] or "",
                    ]
                )
            )
        return "\n".join(lines)

    def copy_report(self):
        QGuiApplication.clipboard().setText(self.report_text())
