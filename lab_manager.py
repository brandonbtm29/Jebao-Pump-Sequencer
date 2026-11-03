"""Lab Manager: the app's own Discord bot, for fewer messages and Continue buttons.

Instead of one message per event (what the webhook in ``discord_notify`` does), the bot keeps
a small set of messages in the channel up to date:

- one **run card** per run, edited as the run goes (progress, each channel, rests), with Pause
  and Stop buttons, and kept when the run ends;
- a short **ping** when something needs you (a Wait for me step, a failed run, a head not
  confirmed OFF, a pump offline), which is deleted once it's handled.

How it works:

- The app describes the messages it wants now: ``{key: Spec}`` (``publish``). The bot posts,
  edits and deletes its own messages until the channel matches (``Reconciler``). Edits that
  only move progress along go out at most every ``EDIT_EVERY_S``; anything else at once.
- Message ids are saved per computer (``store_path``), so after a crash the next start tidies
  up what was left. Finished run cards are kept for ``keep_days`` (0: for good).
- Buttons carry ``lm:<instance>:<action>:<arg>``. ``instance`` is new each time the app starts,
  so a button from another computer (same bot) or an older session is ignored. Only people
  in ``allowed`` may press them; ``decide()`` checks that, and the app double-checks the state
  (``on_action``) before doing anything.
- The bot runs in its own thread with its own event loop, so Discord never holds up the pumps.
  It needs the ``discord.py`` package, imported only when the bot starts.

The token is a secret: it lives in secrets.json and is never logged.
"""

import asyncio
import concurrent.futures
import contextlib
import json
import logging
import os
import secrets as _secrets
import threading
import time
from dataclasses import dataclass, field

log = logging.getLogger("JebaoGUI")

EDIT_EVERY_S = 20.0  # progress-only edits of a message at most this often
TICK_S = 2.0  # how often the bot looks at what the app wants
KEEP_DAYS = 7
MAX_BUTTONS = 25
_STYLES = ("primary", "secondary", "success", "danger")


@dataclass(frozen=True)
class Button:
    custom_id: str
    label: str
    style: str = "secondary"
    disabled: bool = False


@dataclass(frozen=True)
class Spec:
    """One message as the app wants it. ``key`` names it ("card:<run>", "wait:<run>:<path>",
    "alert:guard"…). ``sig`` is what matters for an edit: when only the rest changes (progress),
    the edit can wait. A ``final`` message is kept in the channel once posted as it is."""

    key: str
    content: str = ""
    embed: dict = field(default_factory=dict, hash=False, compare=True)
    buttons: tuple = ()
    ping_user: str = ""  # a user ID allowed to be pinged by this message ("" = none)
    ping_role: str = ""
    sig: str = ""
    final: bool = False

    def same(self, other):
        return (
            other is not None
            and self.content == other.content
            and self.embed == other.embed
            and self.buttons == other.buttons
        )


def custom_id(instance, action, arg=""):
    return f"lm:{instance}:{action}:{arg}"


def parse_custom_id(text):
    """(instance, action, arg) from a button's custom_id, or None if it isn't ours."""
    parts = str(text or "").split(":", 3)
    if len(parts) != 4 or parts[0] != "lm":
        return None
    return parts[1], parts[2], parts[3]


def decide(custom, user_id, instance, allowed):
    """What to do with a button press: ("ignore", "") for another session's button (another
    computer answers it), ("refuse", why) for someone not allowed, else ("do", (action, arg))."""
    parsed = parse_custom_id(custom)
    if parsed is None or parsed[0] != instance:
        return ("ignore", "")
    if str(user_id) not in {str(a) for a in allowed}:
        return ("refuse", "You aren't on this lab's list of people who may control the pumps.")
    return ("do", (parsed[1], parsed[2]))


# --- keeping the channel the way the app wants it ---------------------------------------------


class Reconciler:
    """Posts, edits and deletes the bot's messages through ``transport`` (async ``post(spec)``
    → id, ``edit(id, spec)`` → False if the message is gone, ``delete(id)``)."""

    def __init__(self, transport, store_path="", keep_days=KEEP_DAYS, clock=time.time, instance=""):
        self.transport = transport
        self.store_path = store_path
        self.keep_days = keep_days
        self.clock = clock
        self.instance = instance
        self.posted = {}  # key -> {"id", "spec", "at"}
        self.kept = {}  # key -> {"id", "at"}: finished run cards, left in the channel
        self.leftover = {}  # key -> id: from an earlier session, not yet tidied up
        self._load()

    # saving the ids
    def _load(self):
        if not self.store_path or not os.path.exists(self.store_path):
            return
        try:
            with open(self.store_path, encoding="utf-8") as f:
                data = json.load(f)
            posted = {str(k): str(v) for k, v in (data.get("posted") or {}).items()}
            if self.instance and data.get("instance") == self.instance:
                # the bot restarted within this session (new settings): carry on with them
                self.posted = {k: {"id": v, "spec": None, "at": 0.0} for k, v in posted.items()}
            else:
                self.leftover = posted
            self.kept = {
                str(k): {"id": str(v["id"]), "at": float(v["at"])}
                for k, v in (data.get("kept") or {}).items()
            }
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as e:
            log.warning(
                f"Lab Manager: couldn't read its list of messages ({type(e).__name__}); starting afresh."
            )

    def _save(self):
        if not self.store_path:
            return
        data = {
            "instance": self.instance,
            "posted": {**self.leftover, **{k: v["id"] for k, v in self.posted.items()}},
            "kept": self.kept,
        }
        tmp = self.store_path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self.store_path) or ".", exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, self.store_path)
        except OSError as e:
            log.debug(f"Lab Manager: couldn't save its list of messages ({e}).")

    async def tidy_leftovers(self, closed_card):
        """Messages from a session that ended without tidying up: pings are deleted, run cards
        get ``closed_card(key)`` (a Spec) and are kept."""
        for key, msg_id in list(self.leftover.items()):
            try:
                if key.startswith("card:"):
                    if await self.transport.edit(msg_id, closed_card(key)):
                        self.kept[key] = {"id": msg_id, "at": self.clock()}
                else:
                    await self.transport.delete(msg_id)
            except Exception as e:  # noqa: BLE001 - tidying is best-effort
                log.debug(f"Lab Manager: couldn't tidy {key} ({type(e).__name__}).")
            self.leftover.pop(key, None)
        self._save()

    async def apply(self, wanted, force=False):
        """Make the channel match ``wanted`` ({key: Spec})."""
        now = self.clock()
        changed = False
        for key in [k for k in self.posted if k not in wanted]:
            entry = self.posted.pop(key)
            changed = True
            with contextlib.suppress(Exception):
                await self.transport.delete(entry["id"])
        for key, spec in wanted.items():
            if key in self.kept:
                continue
            entry = self.posted.get(key)
            if entry is None:
                msg_id = await self.transport.post(spec)
                self.posted[key] = {"id": msg_id, "spec": spec, "at": now}
                changed = True
            elif not spec.same(entry["spec"]):
                old = entry["spec"]
                urgent = force or old is None or spec.sig != old.sig or spec.final
                if urgent or now - entry["at"] >= EDIT_EVERY_S:
                    if not await self.transport.edit(entry["id"], spec):  # deleted by someone
                        entry["id"] = await self.transport.post(spec)
                    entry.update(spec=spec, at=now)
                    changed = True
            entry = self.posted.get(key)
            if spec.final and entry is not None and spec.same(entry["spec"]):
                self.kept[key] = {"id": entry["id"], "at": now}
                del self.posted[key]
                changed = True
        if self.keep_days:
            cutoff = now - self.keep_days * 86400
            for key in [k for k, v in self.kept.items() if v["at"] < cutoff and k not in wanted]:
                entry = self.kept.pop(key)
                changed = True
                with contextlib.suppress(Exception):
                    await self.transport.delete(entry["id"])
        if changed:
            self._save()


# --- the bot ---------------------------------------------------------------------------------


@dataclass
class Settings:
    token: str
    channel_id: str
    allowed: tuple = ()
    keep_days: int = KEEP_DAYS
    store_path: str = ""


class LabManager:
    """The bot, in its own thread. ``on_action(action, arg, user_name)`` is called from the
    bot's thread and returns a concurrent Future (the app answers on its own thread) that
    resolves to the text the person sees."""

    def __init__(self, on_action, on_status=None, closed_card=None):
        self.on_action = on_action
        self.on_status = on_status or (lambda text: None)
        self.closed_card = closed_card or (lambda key: Spec(key, content="The app closed."))
        self.instance = _secrets.token_hex(4)
        self.status = "off"
        self._settings = None
        self._thread = None
        self._loop = None
        self._client = None
        self._wanted = {}
        self._wanted_lock = threading.Lock()
        self._wake = None
        self._stopping = threading.Event()
        self._flushed = threading.Event()

    # --- from the app -----------------------------------------------------------------------

    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self, settings):
        """Connect with ``settings``. Returns False (with the reason in ``status``) if it can't."""
        self.stop()
        try:
            import discord  # noqa: F401  (only needed once the bot is used)
        except ImportError:
            self._set_status("needs the discord.py package: run Setup again (or pip install -e .)")
            return False
        self._settings = settings
        self._stopping.clear()
        self._flushed.clear()
        self._thread = threading.Thread(target=self._run, name="lab-manager", daemon=True)
        self._thread.start()
        return True

    def publish(self, wanted):
        """What the channel should show now ({key: Spec}). Cheap: the bot picks it up."""
        with self._wanted_lock:
            self._wanted = dict(wanted)
        loop, wake = self._loop, self._wake
        if loop is not None and wake is not None:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(wake.set)

    def stop(self, timeout=3.0):
        """Bring the channel up to date one last time (for up to ``timeout`` s) and disconnect."""
        thread = self._thread
        if thread is None:
            return
        self._stopping.set()
        loop, wake = self._loop, self._wake
        if loop is not None and wake is not None:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(wake.set)
        self._flushed.wait(timeout)
        client = self._client
        if loop is not None and client is not None:
            with contextlib.suppress(RuntimeError):
                asyncio.run_coroutine_threadsafe(client.close(), loop)
        thread.join(timeout)
        self._thread = None
        self._set_status("off")

    def _set_status(self, text):
        self.status = text
        with contextlib.suppress(Exception):
            self.on_status(text)

    # --- the bot's thread ---------------------------------------------------------------------

    def _run(self):
        loop = asyncio.new_event_loop()
        self._loop = loop
        try:
            loop.run_until_complete(self._main())
        except Exception as e:  # noqa: BLE001 - the bot must never take the app down
            self._set_status(f"stopped ({type(e).__name__})")
            log.warning(f"Lab Manager stopped: {type(e).__name__}.")
        finally:
            self._flushed.set()
            with contextlib.suppress(Exception):
                loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()
            self._loop = None

    async def _main(self):
        import discord

        logging.getLogger("discord").setLevel(logging.WARNING)
        logging.getLogger("discord.client").setLevel(logging.ERROR)  # (no voice: not needed)
        s = self._settings
        self._wake = asyncio.Event()
        intents = discord.Intents.none()
        intents.guilds = True  # (not privileged; discord.py keeps its channel list with it)
        proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or None
        client = discord.Client(intents=intents, proxy=proxy)
        self._client = client
        transport = _DiscordTransport(client, s.channel_id)
        rec = Reconciler(transport, s.store_path, s.keep_days, instance=self.instance)
        ready = asyncio.Event()

        @client.event
        async def on_ready():
            ready.set()

        @client.event
        async def on_interaction(interaction):
            await self._pressed(interaction)

        self._set_status("connecting…")
        login = asyncio.ensure_future(client.start(s.token))
        waiter = asyncio.ensure_future(ready.wait())
        await asyncio.wait({login, waiter}, return_when=asyncio.FIRST_COMPLETED)
        if login.done():
            waiter.cancel()
            exc = login.exception() if not login.cancelled() else None
            if isinstance(exc, discord.LoginFailure):
                self._set_status("the bot token was refused: copy a new one into Settings → Discord")
            else:
                self._set_status(f"couldn't connect ({type(exc).__name__ if exc else 'closed'})")
            with contextlib.suppress(Exception):
                await client.close()
            return
        try:
            await transport.open()
        except Exception as e:  # noqa: BLE001
            self._set_status(
                "can't post in that channel: check its ID and that Lab Manager may see it "
                f"({type(e).__name__})"
            )
            await client.close()
            return
        self._set_status(f"online as {client.user}")
        await rec.tidy_leftovers(self.closed_card)
        try:
            while True:
                stopping = self._stopping.is_set()
                with self._wanted_lock:
                    wanted = dict(self._wanted)
                try:
                    await rec.apply(wanted, force=stopping)
                except Exception as e:  # noqa: BLE001 - try again next time
                    log.debug(f"Lab Manager: couldn't update the channel ({type(e).__name__}).")
                if stopping:
                    break
                self._wake.clear()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), TICK_S)
                if login.done():
                    self._set_status("disconnected")
                    return
        finally:
            self._flushed.set()
            if not client.is_closed():
                await client.close()

    async def _pressed(self, interaction):
        import discord

        if interaction.type != discord.InteractionType.component:
            return
        s = self._settings
        verdict, what = decide(
            interaction.data.get("custom_id"), interaction.user.id, self.instance, s.allowed
        )
        if verdict == "ignore":
            return
        if verdict == "refuse":
            await interaction.response.send_message(what, ephemeral=True)
            log.warning(
                f"[Discord] {interaction.user.name} pressed a button but isn't on the list; nothing done."
            )
            return
        action, arg = what
        if action == "stop":  # ask first, in a message only they see
            view = _view(
                [
                    Button(custom_id(self.instance, "stopyes", arg), "Yes, stop the run", "danger"),
                ]
            )
            await interaction.response.send_message(
                "Stop the run? Every head it uses is turned off and the queue is paused.",
                view=view,
                ephemeral=True,
            )
            view.stop()
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            future = self.on_action(action, arg, interaction.user.name)
            reply = await asyncio.wait_for(asyncio.wrap_future(future), 10)
        except Exception as e:  # noqa: BLE001
            reply = f"That didn't work ({type(e).__name__})."
        await interaction.followup.send(str(reply or "Done."), ephemeral=True)
        if self._wake is not None:
            self._wake.set()


def _view(buttons):
    import discord

    view = discord.ui.View(timeout=None)
    styles = {
        "primary": discord.ButtonStyle.primary,
        "secondary": discord.ButtonStyle.secondary,
        "success": discord.ButtonStyle.success,
        "danger": discord.ButtonStyle.danger,
    }
    for b in list(buttons)[:MAX_BUTTONS]:
        view.add_item(
            discord.ui.Button(
                custom_id=b.custom_id,
                label=b.label[:80],
                style=styles.get(b.style, discord.ButtonStyle.secondary),
                disabled=b.disabled,
            )
        )
    return view


class _DiscordTransport:
    """Posts, edits and deletes the bot's messages in one channel."""

    def __init__(self, client, channel_id):
        self.client = client
        self.channel_id = int(channel_id)
        self.channel = None

    async def open(self):
        self.channel = await self.client.fetch_channel(self.channel_id)

    def _kwargs(self, spec):
        import discord

        users = [discord.Object(int(spec.ping_user))] if spec.ping_user else []
        roles = [discord.Object(int(spec.ping_role))] if spec.ping_role else []
        return {
            "content": spec.content or None,
            "embed": discord.Embed.from_dict(spec.embed) if spec.embed else None,
            "allowed_mentions": discord.AllowedMentions(everyone=False, users=users, roles=roles),
        }

    async def post(self, spec):
        view = _view(spec.buttons) if spec.buttons else None
        kwargs = self._kwargs(spec)
        if view is not None:
            kwargs["view"] = view
        msg = await self.channel.send(**kwargs)
        if view is not None:
            view.stop()  # presses are handled in on_interaction
        return str(msg.id)

    async def edit(self, msg_id, spec):
        import discord

        view = _view(spec.buttons) if spec.buttons else None
        kwargs = self._kwargs(spec)
        kwargs["view"] = view
        try:
            await self.channel.get_partial_message(int(msg_id)).edit(**kwargs)
        except discord.NotFound:
            return False
        finally:
            if view is not None:
                view.stop()
        return True

    async def delete(self, msg_id):
        import discord

        with contextlib.suppress(discord.NotFound):
            await self.channel.get_partial_message(int(msg_id)).delete()


def answer_later():
    """A Future the app sets from its own thread with the reply text."""
    return concurrent.futures.Future()


__all__ = [
    "Button",
    "LabManager",
    "Reconciler",
    "Settings",
    "Spec",
    "answer_later",
    "custom_id",
    "decide",
    "parse_custom_id",
]
