"""Settings → Discord: webhook or Lab Manager bot, who gets pinged, which events are sent.

``DiscordDialog(host)`` reads and saves ``host.secrets.data['discord_webhook']`` and
``['discord_bot_token']`` (both are secrets, so they live in secrets.json) and
``host.config.data['discord']`` (see ``discord_notify.settings``). "Send a test message" uses
what's in the boxes, saved or not. OK calls ``host.restart_lab()``.
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
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

import discord_notify as dn

HOW_TO_WEBHOOK = (
    "In Discord, open the server's <b>Server Settings → Integrations → Webhooks → New Webhook</b>, "
    "pick the channel, then <b>Copy Webhook URL</b> and paste it here. Anyone with the URL can "
    "post in that channel, so it is kept in secrets.json with your Jebao login."
)
HOW_TO_BOT = (
    "Lab Manager keeps one live card per run in the channel, pings you only when it needs you "
    "and tidies up after. Its token is in the <b>Discord Developer Portal → Lab Manager → Bot → "
    "Reset Token</b> (then Copy). It is kept in secrets.json. Channel ID: right-click the channel "
    "→ <b>Copy Channel ID</b> (with Developer Mode on)."
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
        self.setMinimumWidth(600)
        saved = dn.settings(host.config.data.get("discord"))

        layout = QVBoxLayout(self)
        intro = QLabel(
            "Posts to a Discord channel when a channel waits for you, a run ends, or a pump "
            "needs you. With the Lab Manager bot, the people you list can also Continue, Pause "
            "or Stop a run from Discord; with a webhook, Discord can't control anything."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.chk_enabled = QCheckBox("Send notifications to Discord")
        self.chk_enabled.setChecked(saved["enabled"])
        layout.addWidget(self.chk_enabled)

        mode_row = QHBoxLayout()
        mode_row.addWidget(self._caption("Post with:"), 0, Qt.AlignmentFlag.AlignTop)
        modes = QVBoxLayout()
        self.rb_webhook = QRadioButton("A webhook: one message per event")
        self.rb_bot = QRadioButton("The Lab Manager bot: a live card per run, Continue buttons")
        (self.rb_bot if saved["mode"] == "bot" else self.rb_webhook).setChecked(True)
        modes.addWidget(self.rb_webhook)
        modes.addWidget(self.rb_bot)
        mode_row.addLayout(modes, 1)
        layout.addLayout(mode_row)

        self.webhook_box = QWidget()
        webhook_lay = QVBoxLayout(self.webhook_box)
        webhook_lay.setContentsMargins(0, 0, 0, 0)
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
        webhook_lay.addLayout(url_row)
        webhook_lay.addWidget(self._note(HOW_TO_WEBHOOK))
        layout.addWidget(self.webhook_box)

        self.bot_box = QWidget()
        bot_lay = QVBoxLayout(self.bot_box)
        bot_lay.setContentsMargins(0, 0, 0, 0)
        self.token_input = QLineEdit(str(host.secrets.data.get("discord_bot_token") or ""))
        self.token_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.token_input.setPlaceholderText("Lab Manager's bot token")
        self.btn_show_token = QPushButton("Show")
        self.btn_show_token.setCheckable(True)
        self.btn_show_token.toggled.connect(self._show_token)
        token_row = QHBoxLayout()
        token_row.addWidget(self._caption("Bot token:"))
        token_row.addWidget(self.token_input, 1)
        token_row.addWidget(self.btn_show_token)
        bot_lay.addLayout(token_row)
        self.channel_input = QLineEdit(saved["channel_id"])
        self.channel_input.setPlaceholderText("e.g. 123456789012345678")
        channel_row = QHBoxLayout()
        channel_row.addWidget(self._caption("Channel ID:"))
        channel_row.addWidget(self.channel_input, 1)
        bot_lay.addLayout(channel_row)
        self.allowed_input = QLineEdit(", ".join(saved["allowed_ids"]))
        self.allowed_input.setPlaceholderText("User IDs, comma-separated (default: who is pinged)")
        self.allowed_input.setToolTip(
            "Only these people can press Continue, Pause or Stop in Discord. Anyone else gets a "
            "message saying they can't."
        )
        allowed_row = QHBoxLayout()
        allowed_row.addWidget(self._caption("May press buttons:"))
        allowed_row.addWidget(self.allowed_input, 1)
        bot_lay.addLayout(allowed_row)
        keep_row = QHBoxLayout()
        keep_row.addWidget(self._caption("Run cards:"))
        keep_row.addWidget(QLabel("delete finished ones after"))
        self.spin_keep = QSpinBox()
        self.spin_keep.setRange(0, 365)
        self.spin_keep.setSuffix(" days")
        self.spin_keep.setSpecialValueText("never")
        self.spin_keep.setValue(saved["keep_days"])
        keep_row.addWidget(self.spin_keep)
        keep_row.addStretch(1)
        bot_lay.addLayout(keep_row)
        status = getattr(getattr(host, "lab", None), "status", "off")
        self.lbl_bot = QLabel(f"Lab Manager: {status}")
        self.lbl_bot.setTextFormat(Qt.TextFormat.PlainText)
        bot_lay.addWidget(self.lbl_bot)
        bot_lay.addWidget(self._note(HOW_TO_BOT))
        layout.addWidget(self.bot_box)
        self.rb_bot.toggled.connect(self._mode_changed)
        self._mode_changed()

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
        self._fit()

    def _fit(self):
        """Tall enough for the wrapped help, instead of squashing it."""
        width = max(self.width(), 600)
        lay = self.layout()
        lay.activate()
        self.resize(width, max(self.sizeHint().height(), lay.totalHeightForWidth(width)))

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

    def _mode_changed(self, *_args):
        bot = self.rb_bot.isChecked()
        self.bot_box.setVisible(bot)
        self.webhook_box.setVisible(not bot)
        if self.layout() is not None and hasattr(self, "btn_test"):
            self._fit()

    def _show_token(self, shown):
        mode = QLineEdit.EchoMode.Normal if shown else QLineEdit.EchoMode.Password
        self.token_input.setEchoMode(mode)
        self.btn_show_token.setText("Hide" if shown else "Show")

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
            "mode": "bot" if self.rb_bot.isChecked() else "webhook",
            "channel_id": self.channel_input.text().strip(),
            "allowed_ids": [
                i.strip() for i in self.allowed_input.text().replace(";", ",").split(",") if i.strip()
            ],
            "keep_days": self.spin_keep.value(),
        }

    def _problem(self, url, values, need_url):
        if values["mode"] == "bot":
            if need_url:
                if not self.token_input.text().strip():
                    return "Paste Lab Manager's bot token (Developer Portal → Bot → Reset Token)."
                problem = dn.check_channel(values["channel_id"])
                if problem:
                    return problem
            bad = [i for i in values["allowed_ids"] if dn.check_mention(i)]
            if bad:
                return f"“{bad[0]}” isn't a Discord user ID (15 to 22 digits)."
        elif need_url or url:
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
        if values["mode"] == "bot":
            self.host.discord_test_bot(
                self.token_input.text().strip(), values["channel_id"], values, on_done=self._result.done.emit
            )
        else:
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
        token = self.token_input.text().strip()
        if token:
            self.host.secrets.data["discord_bot_token"] = token
        else:
            self.host.secrets.data.pop("discord_bot_token", None)
        self.host.config.data["discord"] = values
        self.host.secrets.save()
        self.host.config.save()
        super().accept()
        restart = getattr(self.host, "restart_lab", None)
        if restart is not None:
            restart()
