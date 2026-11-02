"""Settings → Discord: the webhook, who gets pinged, and which events are sent.

``DiscordDialog(host)`` reads and saves ``host.secrets.data['discord_webhook']`` (the URL
holds a token, so it lives in secrets.json) and ``host.config.data['discord']`` (see
``discord_notify.settings``). "Send a test message" uses what's in the boxes, saved or not.
"""

from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

import discord_notify as dn

HOW_TO_WEBHOOK = (
    "In Discord, open the server's <b>Server Settings → Integrations → Webhooks → New Webhook</b>, "
    "pick the channel, then <b>Copy Webhook URL</b> and paste it here. Anyone with the URL can "
    "post in that channel, so it is kept in secrets.json with your Jebao login."
)
HOW_TO_ID = (
    "To ping you, paste your Discord user ID: <b>User Settings → Advanced → Developer Mode</b> "
    "on, then right-click your name → <b>Copy User ID</b>. Or a role's ID, to ping everyone "
    "with that role. Leave it empty for no pings."
)


class _Result(QObject):
    """Carries the test message's result from the sending thread to the dialog."""

    done = pyqtSignal(bool, str)


class DiscordDialog(QDialog):
    def __init__(self, host, parent=None):
        super().__init__(parent)
        self.host = host
        self.setWindowTitle("Discord notifications")
        self.setMinimumWidth(560)
        saved = dn.settings(host.config.data.get("discord"))

        layout = QVBoxLayout(self)
        intro = QLabel(
            "Posts to a Discord channel when a channel waits for you, a run ends, or a pump "
            "needs you. Discord can't control the pumps."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.chk_enabled = QCheckBox("Send notifications to Discord")
        self.chk_enabled.setChecked(saved["enabled"])
        layout.addWidget(self.chk_enabled)

        self.url_input = QLineEdit(str(host.secrets.data.get("discord_webhook") or ""))
        self.url_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.url_input.setPlaceholderText("https://discord.com/api/webhooks/…")
        self.btn_show = QPushButton("Show")
        self.btn_show.setCheckable(True)
        self.btn_show.toggled.connect(self._show_url)
        url_row = QHBoxLayout()
        url_row.addWidget(self._caption("Webhook URL:"))
        url_row.addWidget(self.url_input, 1)
        url_row.addWidget(self.btn_show)
        layout.addLayout(url_row)
        layout.addWidget(self._note(HOW_TO_WEBHOOK))

        self.mention_input = QLineEdit(saved["mention_id"])
        self.mention_input.setPlaceholderText("e.g. 123456789012345678")
        self.mention_kind = QComboBox()
        self.mention_kind.addItem("Person", "user")
        self.mention_kind.addItem("Role", "role")
        self.mention_kind.setCurrentIndex(1 if saved["mention_kind"] == "role" else 0)
        mention_row = QHBoxLayout()
        mention_row.addWidget(self._caption("Ping:"))
        mention_row.addWidget(self.mention_input, 1)
        mention_row.addWidget(self.mention_kind)
        layout.addLayout(mention_row)
        layout.addWidget(self._note(HOW_TO_ID))

        grid = QGridLayout()
        grid.addWidget(QLabel("<b>Tell Discord when…</b>"), 0, 0)
        grid.addWidget(QLabel("<b>Send</b>"), 0, 1, Qt.AlignmentFlag.AlignHCenter)
        grid.addWidget(QLabel("<b>Ping</b>"), 0, 2, Qt.AlignmentFlag.AlignHCenter)
        self.chk_send, self.chk_ping = {}, {}
        for row, (key, (label, _send, _ping)) in enumerate(dn.EVENTS.items(), start=1):
            send, ping = QCheckBox(), QCheckBox()
            send.setChecked(saved["events"][key])
            ping.setChecked(saved["pings"][key])
            ping.setEnabled(send.isChecked())
            send.toggled.connect(ping.setEnabled)
            grid.addWidget(QLabel(label), row, 0)
            grid.addWidget(send, row, 1, Qt.AlignmentFlag.AlignHCenter)
            grid.addWidget(ping, row, 2, Qt.AlignmentFlag.AlignHCenter)
            self.chk_send[key], self.chk_ping[key] = send, ping
        grid.setColumnStretch(0, 1)
        layout.addLayout(grid)

        test_row = QHBoxLayout()
        self.btn_test = QPushButton("Send a test message")
        self.btn_test.clicked.connect(self.send_test)
        self.lbl_status = QLabel("")
        self.lbl_status.setWordWrap(True)
        self.lbl_status.setTextFormat(Qt.TextFormat.PlainText)
        test_row.addWidget(self.btn_test)
        test_row.addWidget(self.lbl_status, 1)
        layout.addLayout(test_row)

        self._result = _Result(self)
        self._result.done.connect(self._test_done)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        # Tall enough for the wrapped help, instead of squashing it
        width = 560
        self.resize(width, max(self.sizeHint().height(), layout.totalHeightForWidth(width)))

    @staticmethod
    def _caption(text):
        label = QLabel(text)
        label.setMinimumWidth(100)
        return label

    @staticmethod
    def _note(text):
        label = QLabel(text)
        label.setWordWrap(True)
        label.setStyleSheet("color: grey;")
        return label

    def _show_url(self, shown):
        mode = QLineEdit.EchoMode.Normal if shown else QLineEdit.EchoMode.Password
        self.url_input.setEchoMode(mode)
        self.btn_show.setText("Hide" if shown else "Show")

    def _say(self, text, error=False):
        self.lbl_status.setStyleSheet("color: #DC3545;" if error else "")
        self.lbl_status.setText(text)

    def values(self):
        """(webhook URL, config['discord']) as in the boxes."""
        return self.url_input.text().strip(), {
            "enabled": self.chk_enabled.isChecked(),
            "events": {k: box.isChecked() for k, box in self.chk_send.items()},
            "pings": {k: box.isChecked() for k, box in self.chk_ping.items()},
            "mention_id": self.mention_input.text().strip(),
            "mention_kind": self.mention_kind.currentData(),
        }

    def _problem(self, url, values, need_url):
        if need_url or url:
            problem = dn.check_url(url)
            if problem:
                return problem
        return dn.check_mention(values["mention_id"])

    def send_test(self):
        url, values = self.values()
        problem = self._problem(url, values, need_url=True)
        if problem:
            self._say(problem, error=True)
            return
        self.btn_test.setEnabled(False)
        self._say("Sending…")
        self.host.discord_test(url, values, on_done=self._result.done.emit)

    def _test_done(self, ok, problem):
        self.btn_test.setEnabled(True)
        if ok:
            self._say("Sent. Check the channel" + (" (and that you were pinged)." if self._pinged() else "."))
        else:
            self._say(f"Not sent: {problem}", error=True)

    def _pinged(self):
        return bool(self.mention_input.text().strip())

    def accept(self):
        url, values = self.values()
        problem = self._problem(url, values, need_url=values["enabled"])
        if problem:
            self._say(problem, error=True)
            return
        if url:
            self.host.secrets.data["discord_webhook"] = url
        else:
            self.host.secrets.data.pop("discord_webhook", None)
        self.host.config.data["discord"] = values
        self.host.secrets.save()
        self.host.config.save()
        super().accept()
