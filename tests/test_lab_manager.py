"""The Lab Manager bot: buttons, keeping the channel in step (Reconciler), what it shows
(lab_cards) and what its buttons do in the app. Nothing here talks to Discord."""

import asyncio
import json
import threading

import pytest

import discord_notify as dn
import lab_cards
import lab_manager as lm
import sequence as sq

ME = "234567890123456789"
CHANNEL = "345678901234567890"


# --- buttons -----------------------------------------------------------------------------------


def test_only_our_buttons_from_allowed_people_do_anything():
    cid = lm.custom_id("abcd1234", "go", "run1|0|1")
    assert lm.parse_custom_id(cid) == ("abcd1234", "go", "run1|0|1")
    assert lm.parse_custom_id("other:thing") is None
    assert lm.decide(cid, ME, "abcd1234", [ME]) == ("do", ("go", "run1|0|1"))
    assert lm.decide(cid, ME, "ffff0000", [ME]) == ("ignore", "")  # another computer's, or an old one
    verdict, why = lm.decide(cid, "999999999999999999", "abcd1234", [ME])
    assert verdict == "refuse" and "list" in why
    assert lm.decide("lm:abcd1234:go", ME, "abcd1234", [ME]) == ("ignore", "")


# --- keeping the channel in step -----------------------------------------------------------------


class FakeTransport:
    def __init__(self):
        self.messages = {}
        self.log = []
        self.n = 0

    async def post(self, spec):
        self.n += 1
        msg_id = str(self.n)
        self.messages[msg_id] = spec
        self.log.append(("post", spec.key))
        return msg_id

    async def edit(self, msg_id, spec):
        self.log.append(("edit", spec.key))
        if msg_id not in self.messages:
            return False
        self.messages[msg_id] = spec
        return True

    async def delete(self, msg_id):
        self.log.append(("delete", msg_id))
        self.messages.pop(msg_id, None)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def card(text, sig="running", final=False):
    return lm.Spec("card:r1", embed={"title": text}, sig=sig, final=final)


def apply(rec, wanted, force=False):
    asyncio.run(rec.apply({s.key: s for s in wanted}, force=force))


def test_messages_are_posted_edited_and_deleted_to_match():
    t, clock = FakeTransport(), Clock()
    rec = lm.Reconciler(t, clock=clock)
    ping = lm.Spec("wait:r1:0", content="Waiting for you: CH1", sig="[1]")
    apply(rec, [card("Running · 10 min left"), ping])
    assert [k for k, _ in [(e[1], 0) for e in t.log]] == ["card:r1", "wait:r1:0"]
    t.log.clear()
    clock.t += 3
    apply(rec, [card("Running · 9 min left"), ping])  # progress only: waits
    assert t.log == []
    clock.t += lm.EDIT_EVERY_S
    apply(rec, [card("Running · 8 min left"), ping])
    assert t.log == [("edit", "card:r1")]
    t.log.clear()
    apply(rec, [card("Paused", sig="paused")])  # something that matters: at once; the ping goes
    assert ("edit", "card:r1") in t.log and ("delete", "2") in t.log
    assert list(t.messages) == ["1"] and t.messages["1"].embed["title"] == "Paused"


def test_a_finished_card_is_kept_then_tidied_after_some_days():
    t, clock = FakeTransport(), Clock()
    rec = lm.Reconciler(t, clock=clock, keep_days=7)
    apply(rec, [card("Running")])
    apply(rec, [card("Finished", sig="done", final=True)])
    assert "card:r1" in rec.kept and "card:r1" not in rec.posted
    apply(rec, [])  # the next run: the old card stays in the channel
    assert t.messages["1"].embed["title"] == "Finished"
    clock.t += 8 * 86400
    apply(rec, [])
    assert t.messages == {} and rec.kept == {}


def test_a_message_someone_deleted_is_posted_again():
    t, clock = FakeTransport(), Clock()
    rec = lm.Reconciler(t, clock=clock)
    apply(rec, [card("Running")])
    t.messages.clear()
    apply(rec, [card("Waiting", sig="waiting")])
    assert list(t.messages.values())[0].embed["title"] == "Waiting"


def test_after_a_crash_pings_are_deleted_and_cards_marked_closed(tmp_path):
    store = tmp_path / "lab.json"
    t = FakeTransport()
    rec = lm.Reconciler(t, str(store), instance="old")
    apply(rec, [card("Running"), lm.Spec("wait:r1:0", content="Waiting")])
    assert json.loads(store.read_text())["posted"] == {"card:r1": "1", "wait:r1:0": "2"}
    # a new session (another instance) finds them
    rec2 = lm.Reconciler(t, str(store), instance="new")
    assert rec2.leftover == {"card:r1": "1", "wait:r1:0": "2"}
    asyncio.run(rec2.tidy_leftovers(lambda key: lab_cards.closed_card(key)))
    assert "2" not in t.messages and "closed" in t.messages["1"].embed["title"]
    assert "card:r1" in rec2.kept and rec2.leftover == {}
    # the bot restarted in the same session (new settings): it carries on with its messages
    rec3 = lm.Reconciler(t, str(store), instance="new")
    apply(rec3, [card("Running", sig="x")])
    assert rec3.leftover == {}


def test_publish_without_a_running_bot_is_harmless():
    bot = lm.LabManager(on_action=lambda *a: None)
    bot.publish({"x": lm.Spec("x")})
    bot.stop()
    assert not bot.running() and bot.status == "off"


# --- settings ------------------------------------------------------------------------------------


def test_bot_settings_defaults_and_odd_values():
    s = dn.settings(
        {"mode": "bot", "channel_id": f" {CHANNEL} ", "allowed_ids": [ME, "nope", 5], "keep_days": 900}
    )
    assert s["mode"] == "bot" and s["channel_id"] == CHANNEL
    assert s["allowed_ids"] == [ME] and s["keep_days"] == 365
    assert dn.settings({})["mode"] == "webhook" and dn.settings({})["keep_days"] == 7
    assert dn.check_channel(CHANNEL) == "" and dn.check_channel("general")


# --- in the app ------------------------------------------------------------------------------------


@pytest.fixture
def lab(window, monkeypatch):
    """The window with the Lab Manager switched on (but never connecting), two pumps."""
    monkeypatch.setitem(
        window.config.data,
        "discord",
        {"enabled": True, "mode": "bot", "channel_id": CHANNEL, "mention_id": ME},
    )
    monkeypatch.setitem(window.secrets.data, "discord_bot_token", "x" * 60)
    published = []
    monkeypatch.setattr(window.lab, "running", lambda: True)
    monkeypatch.setattr(window.lab, "publish", lambda wanted: published.append(wanted))
    window.cloud_devices = [
        {"did": "A", "dev_alias": "JP 1", "is_online": True},
        {"did": "B", "dev_alias": "JP 2", "is_online": True},
    ]
    for did in ("A", "B"):
        pump = window.config.get_pump(did)
        pump["calibration"] = [60.0] * 4
        pump["display_name"] = ""
        pump["head_names"] = ["", "", "", ""]
    sent = []

    async def fake_send(did, attrs):
        sent.append((did, dict(attrs)))
        return True

    monkeypatch.setattr(window, "send_cloud_command", fake_send)
    return window, published


def settings(window):
    return dn.settings(window.config.data.get("discord"))


def run(loop, window, steps, during=None, name="SPE A"):
    async def go():
        window.seq_run = None
        task = asyncio.ensure_future(window._async_run_sequence(name, sq.to_saved_all(steps)))
        window.sequence_task = task
        if during is not None:
            while window.seq_run is None:
                await asyncio.sleep(0.01)
            await during(task)
        try:
            return await task
        except asyncio.CancelledError:
            return "cancelled"

    return loop.run_until_complete(go())


async def until(check, timeout=5.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not check():
        assert loop.time() < end, "timed out"
        await asyncio.sleep(0.01)


def review(lanes, remote=True, message="Swap the cartridge."):
    return {
        **sq.new_step("review", lanes=lanes),
        "label": "Swap",
        "message": message,
        "sound": False,
        "remote": remote,
    }


def test_the_webhook_is_quiet_while_the_bot_is_on(lab, monkeypatch):
    window, _ = lab
    monkeypatch.setitem(
        window.secrets.data, "discord_webhook", "https://discord.com/api/webhooks/1/" + "a" * 30
    )
    assert not window.notify("failed", "x")
    assert window.lab_settings().allowed == (ME,)  # the person pinged may press buttons
    assert "x" * 60 in window.private_values()


def test_a_run_card_and_a_wait_ping_with_continue_buttons(lab, qt_loop):
    window, published = lab
    _, loop = qt_loop
    seen = {}

    async def during(task):
        await until(lambda: window._ch_review_events.get(1) and window._ch_review_events.get(2))
        wanted = lab_cards.specs(window, settings(window), "inst")
        seen["wanted"] = wanted
        ping = next(s for k, s in wanted.items() if k.startswith("wait:"))
        assert ping.ping_user == ME and ping.content.startswith(f"<@{ME}> ")
        assert "Waiting for you: CH1–2" in ping.content
        assert ping.embed["description"] == "Swap the cartridge."
        labels = [b.label for b in ping.buttons]
        assert labels == ["Continue CH1", "Continue CH2", "Continue all"]
        go = lm.parse_custom_id(ping.buttons[0].custom_id)
        assert window.lab_do(go[1], go[2], "brandon") == "Continued CH1."
        assert not window._ch_review_events.get(1) and window._ch_review_events.get(2)
        assert window.lab_do(go[1], go[2], "brandon") == "Nothing is waiting there any more."
        everyone = lm.parse_custom_id(ping.buttons[-1].custom_id)
        assert window.lab_do(everyone[1], everyone[2], "brandon") == "Continued CH2."

    assert run(loop, window, [review([1, 2])], during) == "done"
    card = seen["wanted"][f"card:{window._run_id}"]
    assert "waiting for you" in card.embed["title"] and [b.label for b in card.buttons] == [
        "Pause all",
        "Stop run",
    ]
    assert any(f["name"] == "CH1" and "Waiting for you" in f["value"] for f in card.embed["fields"])
    assert published  # the bot was told along the way
    final = lab_cards.specs(window, settings(window), "inst")
    done = final[f"card:{window._run_id}"]
    assert done.final and done.embed["title"].startswith("✓ Finished") and done.buttons == ()
    assert not any(k.startswith("wait:") for k in final)


def test_a_step_without_remote_continue_has_no_buttons(lab, qt_loop):
    window, _ = lab
    _, loop = qt_loop

    async def during(task):
        await until(lambda: window._ch_review_events.get(1))
        wanted = lab_cards.specs(window, settings(window), "inst")
        ping = next(s for k, s in wanted.items() if k.startswith("wait:"))
        assert ping.buttons == () and "Continue in the app" in ping.embed["footer"]["text"]
        path = "-".join(str(p) for p in window._review_at[1])
        assert "doesn't allow" in window.lab_do("go", f"{window._run_id}|{path}|1", "brandon")
        assert window._ch_review_events.get(1)  # still waiting
        window.approve_channel(1)

    assert run(loop, window, [review([1], remote=False)], during) == "done"


def test_pause_resume_and_stop_from_discord(lab, qt_loop, caplog):
    window, _ = lab
    _, loop = qt_loop
    steps = [{**sq.new_step("dose", heads=[["A", 1]]), "volume": 30}]

    async def during(task):
        await until(lambda: ("A", 1) in window.head_runs)
        rid = window._run_id
        assert window.lab_do("pause", rid, "brandon") == "Paused every channel."
        assert window._ch_paused[1]
        card = lab_cards.specs(window, settings(window), "inst")[f"card:{rid}"]
        assert [b.label for b in card.buttons] == ["Resume all", "Stop run"]
        assert window.lab_do("resume", rid, "brandon") == "Resumed."
        assert window.lab_do("stopyes", "someotherrun", "brandon").startswith("That run has ended")
        assert window.lab_do("stopyes", rid, "brandon").startswith("Stopping")

    with caplog.at_level("INFO", logger="JebaoGUI"):
        assert run(loop, window, steps, during) == "cancelled"
    assert "[Discord] brandon paused" in caplog.text and "[Discord] brandon pressed Stop run" in caplog.text
    assert "brandon pressed Stop in Discord" in window.seq_run.why
    window.queue_paused = False


def test_a_failed_run_gets_a_ping_until_dismissed(lab, qt_loop, monkeypatch):
    window, _ = lab
    _, loop = qt_loop

    async def refuse(did, attrs):
        return not any(attrs.values())

    monkeypatch.setattr(window, "send_cloud_command", refuse)
    real_sleep = asyncio.sleep

    async def quick(seconds, *a, **k):
        await real_sleep(min(seconds, 0.01))

    monkeypatch.setattr(asyncio, "sleep", quick)
    assert run(loop, window, [{**sq.new_step("dose", heads=[["A", 1]]), "volume": 1}]) == "failed"
    wanted = lab_cards.specs(window, settings(window), "inst")
    alert = wanted[f"alert:{window._run_id}"]
    assert alert.ping_user == ME and "failed" in alert.content and alert.buttons[0].label == "Got it"
    assert wanted[f"card:{window._run_id}"].embed["title"].startswith("✕")
    _inst, action, arg = lm.parse_custom_id(alert.buttons[0].custom_id)
    assert window.lab_do(action, arg, "brandon") == "Cleared."
    assert f"alert:{window._run_id}" not in lab_cards.specs(window, settings(window), "inst")


def test_heads_not_confirmed_off_and_offline_pumps_are_pinged(lab):
    window, _ = lab
    window.guard.must_off[("A", 2)] = "OFF didn't go through"
    window._notify_guard(window.guard.pending(), now=0.0)
    window._notify_guard(window.guard.pending(), now=100.0)
    wanted = lab_cards.specs(window, settings(window), "inst")
    assert "JP 1 · CH2" in wanted["alert:guard"].embed["description"]
    window.guard.must_off.clear()
    window._guard_told = set()
    window._offline_told = {"B"}
    window.cloud_devices[1]["is_online"] = False
    wanted = lab_cards.specs(window, settings(window), "inst")
    assert "alert:guard" not in wanted and "JP 2" in wanted["alert:offline"].content
    window._offline_told = set()


def test_a_press_from_the_bots_thread_is_done_on_the_main_thread(lab, qt_loop):
    window, _ = lab
    app, _loop = qt_loop
    window._lab_outcome = {"run_id": "r9", "event": "failed", "title": "x", "body": "y"}
    box = {}
    worker = threading.Thread(
        target=lambda: box.setdefault("f", window._lab_action_from_bot("dismiss", "r9", "brandon"))
    )
    worker.start()
    worker.join()
    for _ in range(50):
        app.processEvents()
        if box["f"].done():
            break
    assert box["f"].result(timeout=1) == "Cleared." and window._lab_outcome is None


def test_the_dialog_saves_the_bot_token_in_secrets(window, monkeypatch):
    from discord_dialog import DiscordDialog

    saved, restarted = [], []
    monkeypatch.setattr(window.secrets, "save", lambda: saved.append("secrets"))
    monkeypatch.setattr(window.config, "save", lambda: saved.append("config"))
    monkeypatch.setattr(window, "restart_lab", lambda: restarted.append(True))
    monkeypatch.setitem(window.config.data, "discord", {})
    window.secrets.data.pop("discord_bot_token", None)
    dlg = DiscordDialog(window)
    dlg.chk_enabled.setChecked(True)
    dlg.rb_bot.setChecked(True)
    assert dlg.bot_box.isVisibleTo(dlg) and not dlg.webhook_box.isVisibleTo(dlg)
    dlg.accept()
    assert dlg.result() == 0 and "token" in dlg.lbl_status.text()
    dlg.token_input.setText("t" * 60)
    dlg.channel_input.setText(CHANNEL)
    dlg.allowed_input.setText(f"{ME}, 12")
    dlg.accept()
    assert dlg.result() == 0 and "12" in dlg.lbl_status.text()
    dlg.allowed_input.setText(ME)
    dlg.accept()
    assert dlg.result() == 1 and restarted == [True]
    assert window.secrets.data["discord_bot_token"] == "t" * 60
    cfg = window.config.data["discord"]
    assert cfg["mode"] == "bot" and cfg["channel_id"] == CHANNEL and cfg["allowed_ids"] == [ME]
    assert "t" * 60 not in json.dumps(cfg)
    window.secrets.data.pop("discord_bot_token", None)
