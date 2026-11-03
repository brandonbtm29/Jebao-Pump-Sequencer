"""Discord notifications: tell a Discord channel when the app needs you or a run ends.

The app posts to a channel's webhook (in Discord: Server Settings → Integrations →
Webhooks → New Webhook → Copy Webhook URL). Nothing is installed in Discord, and nothing
in Discord can control the pumps: a webhook can only post.

- The webhook URL holds a token. It is kept in ``secrets.json``, never logged, and blanked
  from error messages (``scrub``).
- Messages go out one at a time from a background thread, so a slow or unreachable Discord
  never holds up the pumps. Notes with the same ``group`` that come within ``GROUP_WAIT_S``
  of each other go out as one message (CH1 and CH5 reaching the same Wait for me step).
- Mentions: only the person or role set in Settings is ever pinged
  (``allowed_mentions``), so an "@everyone" in a step's message can't ping anyone.
- What is sent for each event, and whether it pings, is set in ``config['discord']``
  (``settings()`` fills in the defaults).

No Qt here, so it can be tested on its own.
"""

import collections
import logging
import queue
import re
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

import requests

log = logging.getLogger("JebaoGUI")

# event: (label in Settings, sent by default, pings by default)
EVENTS = {
    "review": ("Wait for me: a channel is waiting for you", True, True),
    "failed": ("A run failed", True, True),
    "guard": ("A head can't be confirmed OFF", True, True),
    "offline": ("A pump went offline while in use", True, True),
    "stopped": ("A run was stopped", True, False),
    "done": ("A run finished", True, False),
    "rest": ("A head is resting (on-time limit)", True, False),
}
COLORS = {
    "review": 0xE67E22,  # the orange of the approval bar
    "failed": 0xDC3545,
    "guard": 0xDC3545,
    "offline": 0xDC3545,
    "stopped": 0x95A5A6,
    "done": 0x28A745,
    "rest": 0x3498DB,
    "test": 0x5865F2,
}
BOT_NAME = "Jebao Pump Sequencer"
TIMEOUT_S = 10
GROUP_WAIT_S = 2.0
RETRIES = 3  # after the first try
BACKOFF_S = (2, 5, 15)
MAX_WAIT_S = 60  # longest a "slow down" (HTTP 429) is waited out
MAX_QUEUED = 100

# Discord's limits
MAX_CONTENT = 2000
MAX_DESCRIPTION = 4096
MAX_FIELDS = 25
MAX_FIELD_NAME = 256
MAX_FIELD_VALUE = 1024
MAX_FOOTER = 2048

_WEBHOOK = re.compile(
    r"^https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api/(?:v\d+/)?webhooks/(\d{5,25})/([\w-]{20,200})/?$"
)
_WEBHOOK_IN_TEXT = re.compile(r"(?i)(?:https?://[^\s'\"<>]*)?/api/(?:v\d+/)?webhooks/[^\s'\"<>)]+")
_ID = re.compile(r"^\d{15,22}$")
_MARKDOWN = re.compile(r"([\\*_~`|>#\[\]()-])")


def check_url(url):
    """Empty if ``url`` looks like a Discord webhook, else what's wrong with it."""
    url = (url or "").strip()
    if not url:
        return "No webhook URL."
    if not _WEBHOOK.match(url):
        return (
            "That isn't a Discord webhook URL. It looks like "
            "https://discord.com/api/webhooks/<numbers>/<letters and numbers>."
        )
    return ""


def webhook_token(url):
    """The secret part of a webhook URL ("" if it isn't one), for blanking from logs."""
    m = _WEBHOOK.match((url or "").strip())
    return m.group(2) if m else ""


def scrub(text):
    """Blank any webhook URL (or its path) from ``text``."""
    return _WEBHOOK_IN_TEXT.sub("/api/webhooks/***", str(text))


def check_mention(mention_id):
    """Empty if ``mention_id`` is empty or looks like a Discord user/role ID, else the problem."""
    mention_id = (mention_id or "").strip()
    if mention_id and not _ID.match(mention_id):
        return "A Discord ID is 15 to 22 digits (right-click your name → Copy User ID)."
    return ""


def escape(text):
    """Show ``text`` as typed in Discord, not as formatting."""
    return _MARKDOWN.sub(r"\\\1", str(text))


def settings(saved=None):
    """``config['discord']`` with the defaults filled in."""
    saved = saved if isinstance(saved, dict) else {}
    events = saved.get("events") if isinstance(saved.get("events"), dict) else {}
    pings = saved.get("pings") if isinstance(saved.get("pings"), dict) else {}
    kind = saved.get("mention_kind")
    return {
        "enabled": bool(saved.get("enabled", False)),
        "events": {k: bool(events.get(k, send)) for k, (_l, send, _p) in EVENTS.items()},
        "pings": {k: bool(pings.get(k, ping)) for k, (_l, _s, ping) in EVENTS.items()},
        "mention_id": str(saved.get("mention_id") or "").strip(),
        "mention_kind": kind if kind in ("user", "role") else "user",
        # the Lab Manager bot (lab_manager.py) instead of the webhook
        "mode": "bot" if saved.get("mode") == "bot" else "webhook",
        "channel_id": str(saved.get("channel_id") or "").strip(),
        "allowed_ids": [str(i).strip() for i in (saved.get("allowed_ids") or []) if _ID.match(str(i).strip())]
        if isinstance(saved.get("allowed_ids"), list)
        else [],
        "keep_days": _days(saved.get("keep_days"), KEEP_DAYS_DEFAULT),
    }


KEEP_DAYS_DEFAULT = 7
API = "https://discord.com/api/v10"


def _days(value, default):
    try:
        days = int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default
    return min(max(days, 0), 365)


def check_channel(channel_id):
    """Empty if ``channel_id`` looks like a Discord channel ID, else the problem."""
    if not _ID.match((channel_id or "").strip()):
        return "A channel ID is 15 to 22 digits (right-click the channel → Copy Channel ID)."
    return ""


def bot_target(token, channel_id):
    """(url, headers) to post in ``channel_id`` as the bot."""
    return f"{API}/channels/{channel_id.strip()}/messages", {"Authorization": f"Bot {token.strip()}"}


def _cut(text, limit):
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


@dataclass
class Note:
    """One notification, all plain text (``payload`` escapes it). ``title`` may hold
    ``{items}``, filled with ``items`` (merged across a group); ``body`` and ``fields``
    (name, value) go in the embed."""

    event: str
    title: str
    body: str = ""
    fields: list = field(default_factory=list)
    items: list = field(default_factory=list)
    group: str = ""
    ping: bool = False

    def merged(self, other):
        items = list(self.items) + [i for i in other.items if i not in self.items]
        return replace(self, items=items, ping=self.ping or other.ping)

    def headline(self):
        return self.title.replace("{items}", ", ".join(self.items))


def payload(note, mention_id="", mention_kind="user", footer="", when=None):
    """The JSON Discord expects for ``note``."""
    mention, allowed = "", {"parse": []}
    if note.ping and mention_id and not check_mention(mention_id):
        if mention_kind == "role":
            mention, allowed = f"<@&{mention_id}>", {"parse": [], "roles": [mention_id]}
        else:
            mention, allowed = f"<@{mention_id}>", {"parse": [], "users": [mention_id]}
    content = f"**{escape(note.headline())}**"
    content = _cut(f"{mention} {content}" if mention else content, MAX_CONTENT)
    embed = {"color": COLORS.get(note.event, 0x95A5A6)}
    if str(note.body).strip():
        embed["description"] = _cut(escape(note.body), MAX_DESCRIPTION)
    fields = [
        {
            "name": _cut(escape(name), MAX_FIELD_NAME),
            "value": _cut(escape(value), MAX_FIELD_VALUE),
            "inline": "\n" not in str(value),
        }
        for name, value in note.fields[:MAX_FIELDS]
        if str(name).strip() and str(value).strip()
    ]
    if fields:
        embed["fields"] = fields
    if footer:
        embed["footer"] = {"text": _cut(footer, MAX_FOOTER)}
    embed["timestamp"] = (when or datetime.now(UTC)).isoformat()
    return {"username": BOT_NAME, "content": content, "embeds": [embed], "allowed_mentions": allowed}


@dataclass
class _Item:
    url: str
    note: Note
    mention_id: str = ""
    mention_kind: str = "user"
    footer: str = ""
    on_done: object = None  # on_done(ok, problem), called from the sending thread
    count: int = 1  # notes merged into this one
    headers: dict = None  # the bot's: posts in a channel instead of a webhook


def _backoff(attempt):
    return BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)]


def _retry_after(resp):
    try:
        wait = float(resp.json().get("retry_after", 0))
    except Exception:
        try:
            wait = float(resp.headers.get("Retry-After", 0))
        except (TypeError, ValueError):
            wait = 0.0
    return min(max(wait, 0.5), MAX_WAIT_S)


def _refusal(resp):
    try:
        detail = str(resp.json().get("message") or "")
    except Exception:
        detail = ""
    detail = _cut(scrub(detail), 120)
    return f"Discord refused it (HTTP {resp.status_code}{': ' + detail if detail else ''})"


class Notifier:
    """Sends notes to Discord webhooks, one at a time, from its own thread."""

    def __init__(self, post=None, sleep=time.sleep, group_wait=GROUP_WAIT_S):
        self._post = post
        self._sleep = sleep
        self.group_wait = group_wait
        self._queue = queue.Queue(maxsize=MAX_QUEUED)
        self._thread = None
        self._start_lock = threading.Lock()
        self._closing = threading.Event()
        self._waiting = 0  # notes queued or being sent
        self._cond = threading.Condition()
        self.sent = 0
        self.failed = 0
        self.last_problem = ""

    # --- from the app ---------------------------------------------------------------------

    def send(self, url, note, mention_id="", mention_kind="user", footer="", on_done=None, headers=None):
        """Queue ``note`` for ``url`` (a webhook; or, with ``headers`` from ``bot_target``, a
        channel the bot posts in). Returns False (and sends nothing) if it can't go."""
        if headers is None:
            problem = check_url(url)
        else:
            problem = "" if str(url).startswith(f"{API}/channels/") else "Not a Discord channel."
        if problem or self._closing.is_set():
            self._done(on_done, False, problem or "The app is closing.")
            return False
        item = _Item(url.strip(), note, mention_id, mention_kind, footer, on_done, headers=headers)
        self._start()
        with self._cond:
            self._waiting += 1
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            self._finished(1)
            log.warning("Too many Discord notifications are waiting to go out; this one was dropped.")
            self._done(on_done, False, "Too many notifications waiting.")
            return False
        return True

    def wait_idle(self, timeout=None):
        """Wait until everything queued has been sent (or given up on). True if it has."""
        with self._cond:
            return self._cond.wait_for(lambda: self._waiting == 0, timeout)

    def _finished(self, count):
        with self._cond:
            self._waiting -= count
            self._cond.notify_all()

    def close(self, timeout=2.0):
        """Send what's queued (without waiting to merge groups), for up to ``timeout`` s."""
        self._closing.set()
        if self._thread is None:
            return
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        self._thread.join(timeout)

    # --- the sending thread ---------------------------------------------------------------

    def _start(self):
        with self._start_lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._work, name="discord-notify", daemon=True)
                self._thread.start()

    def _work(self):
        backlog = collections.deque()
        stopping = False
        while True:
            if backlog:
                item = backlog.popleft()
            elif stopping:
                return
            else:
                item = self._queue.get()
            if item is None:
                stopping = True
                continue
            if item.note.group:
                item, stopping = self._gather(item, backlog, stopping)
            try:
                self._deliver(item)
            except Exception as e:  # never let the thread die
                log.warning(f"Discord notification not sent: {scrub(type(e).__name__)}.")
            finally:
                self._finished(item.count)

    def _gather(self, item, backlog, stopping):
        """Merge the notes of ``item``'s group that come within ``group_wait``."""
        same = [x for x in backlog if x.note.group == item.note.group and x.url == item.url]
        for x in same:
            backlog.remove(x)
            item = self._merge(item, x)
        end = time.monotonic() + self.group_wait
        while not stopping and not self._closing.is_set():
            left = end - time.monotonic()
            if left <= 0:
                break
            try:
                nxt = self._queue.get(timeout=left)
            except queue.Empty:
                break
            if nxt is None:
                stopping = True
            elif nxt.note.group == item.note.group and nxt.url == item.url:
                item = self._merge(item, nxt)
            else:
                backlog.append(nxt)
        return item, stopping

    @staticmethod
    def _merge(item, other):
        return replace(item, note=item.note.merged(other.note), count=item.count + other.count)

    def _deliver(self, item):
        body = payload(item.note, item.mention_id, item.mention_kind, item.footer)
        extra = {}
        if item.headers:
            body.pop("username", None)  # a bot posts under its own name
            extra["headers"] = item.headers
        post = self._post or requests.post
        problem = ""
        for attempt in range(RETRIES + 1):
            try:
                resp = post(item.url, json=body, timeout=TIMEOUT_S, **extra)
            except Exception as e:
                problem, wait = f"couldn't reach Discord ({scrub(type(e).__name__)})", _backoff(attempt)
            else:
                code = getattr(resp, "status_code", 0)
                if 200 <= code < 300:
                    self.sent += 1
                    self.last_problem = ""
                    self._done(item.on_done, True, "")
                    return
                if code == 429:
                    problem, wait = "Discord asked the app to slow down", _retry_after(resp)
                elif code >= 500:
                    problem, wait = f"Discord had a problem (HTTP {code})", _backoff(attempt)
                else:
                    problem = _refusal(resp)
                    break  # a wrong or deleted webhook: trying again won't help
            if attempt < RETRIES and not self._closing.is_set():
                self._sleep(wait)
            elif self._closing.is_set():
                break
        self.failed += 1
        self.last_problem = problem
        log.warning(f"Discord notification not sent: {problem}. Check the webhook in Settings → Discord.")
        self._done(item.on_done, False, problem)

    @staticmethod
    def _done(on_done, ok, problem):
        if on_done is None:
            return
        try:
            on_done(ok, problem)
        except Exception as e:  # e.g. the dialog that asked was closed meanwhile
            log.debug(f"Discord: on_done failed ({type(e).__name__}).")
