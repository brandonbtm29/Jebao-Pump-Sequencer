"""Discord notifications: the webhook check, what is posted, sending from the background
thread, and what the app tells Discord (Wait for me, runs ending, the guard, pumps going
offline, rests) and never tells it (pump IDs, the webhook URL in the log)."""

import asyncio
import logging
import threading

import pytest

import discord_notify as dn
import sequence as sq

URL = "https://discord.com/api/webhooks/123456789012345678/" + "AbC-dEf_123" * 6
TOKEN = URL.rsplit("/", 1)[1]
ME = "234567890123456789"


class Resp:
    def __init__(self, code, body=None, headers=None):
        self.status_code = code
        self._body = body
        self.headers = headers or {}

    def json(self):
        if self._body is None:
            raise ValueError("no JSON")
        return self._body


class FakePost:
    """Answers with ``answers`` in turn (then 204), recording what was posted."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.posts = []
        self.lock = threading.Lock()

    def __call__(self, url, json=None, timeout=None):
        with self.lock:
            self.posts.append((url, json))
            answer = self.answers.pop(0) if self.answers else Resp(204)
        if isinstance(answer, Exception):
            raise answer
        return answer


def notifier(post, group_wait=0.05):
    return dn.Notifier(post=post, sleep=lambda s: None, group_wait=group_wait)


# --- the webhook URL ---------------------------------------------------------------------


def test_only_discord_webhooks_are_accepted():
    assert dn.check_url(URL) == ""
    assert dn.check_url(URL.replace("discord.com", "discordapp.com")) == ""
    assert dn.check_url(URL.replace("discord.com", "ptb.discord.com")) == ""
    assert dn.check_url("") == "No webhook URL."
    for bad in (
        URL.replace("https", "http"),
        URL.replace("discord.com", "discord.com.evil.example"),
        "https://example.com/api/webhooks/123456789012345678/" + "x" * 40,
        "https://discord.com/channels/123/456",
    ):
        assert dn.check_url(bad), bad
    assert dn.webhook_token(URL) == TOKEN and dn.webhook_token("nope") == ""


def test_the_webhook_is_blanked_from_error_text():
    text = f"HTTPSConnectionPool(host='discord.com'): Max retries exceeded with url: {URL[19:]} (x)"
    assert TOKEN not in dn.scrub(text) and "/api/webhooks/***" in dn.scrub(text)
    assert TOKEN not in dn.scrub(f"posting to {URL} failed")


def test_settings_defaults_and_odd_values():
    s = dn.settings(None)
    assert s["enabled"] is False and s["mention_id"] == "" and s["mention_kind"] == "user"
    assert all(s["events"].values())
    assert s["pings"]["review"] and s["pings"]["failed"] and not s["pings"]["done"]
    s = dn.settings({"enabled": 1, "events": "x", "pings": {"done": True}, "mention_kind": "?"})
    assert s["enabled"] is True and s["events"]["rest"] and s["pings"]["done"]
    assert s["mention_kind"] == "user"
    assert dn.check_mention("") == "" and dn.check_mention(ME) == ""
    assert dn.check_mention("@brandon") and dn.check_mention("123")


# --- what is posted ----------------------------------------------------------------------


def test_payload_pings_only_the_person_set_and_only_when_asked():
    note = dn.Note("review", "Waiting for you: {items}", body="@everyone swap it", items=["CH1"], ping=True)
    body = dn.payload(note, ME, "user", footer="LAB-PC · Jebao Pump Sequencer")
    assert body["content"].startswith(f"<@{ME}> ")
    assert "Waiting for you: CH1" in body["content"]
    assert body["allowed_mentions"] == {"parse": [], "users": [ME]}  # no @everyone, whatever the text
    assert body["embeds"][0]["footer"]["text"] == "LAB-PC · Jebao Pump Sequencer"
    role = dn.payload(note, ME, "role")
    assert role["content"].startswith(f"<@&{ME}> ") and role["allowed_mentions"]["roles"] == [ME]
    quiet = dn.payload(dn.Note("done", "Finished", ping=False), ME)
    assert "<@" not in quiet["content"] and quiet["allowed_mentions"] == {"parse": []}
    assert "<@" not in dn.payload(note, "not-an-id")["content"]


def test_payload_shows_text_as_typed_and_keeps_to_discords_limits():
    note = dn.Note(
        "failed",
        "“SPE *fast*” failed",
        body="x" * 5000,
        fields=[("Step", "2: Dose_A"), ("Empty", ""), ("Delivered", "a\nb")],
    )
    body = dn.payload(note)
    assert r"SPE \*fast\*" in body["content"]
    embed = body["embeds"][0]
    assert len(embed["description"]) <= dn.MAX_DESCRIPTION and embed["description"].endswith("…")
    assert [f["name"] for f in embed["fields"]] == ["Step", "Delivered"]
    assert embed["fields"][0]["value"] == r"2: Dose\_A" and embed["fields"][0]["inline"]
    assert not embed["fields"][1]["inline"]  # several lines: full width
    assert len(dn.payload(dn.Note("done", "y" * 3000))["content"]) <= dn.MAX_CONTENT


# --- sending -------------------------------------------------------------------------------


def test_notes_go_out_in_order_and_report_back():
    post = FakePost()
    n = notifier(post)
    results = []
    assert n.send(URL, dn.Note("done", "one"), on_done=lambda ok, why: results.append(ok))
    assert n.send(URL, dn.Note("done", "two"))
    assert n.wait_idle(5)
    assert [p[1]["content"] for p in post.posts] == ["**one**", "**two**"]
    assert post.posts[0][0] == URL and results == [True] and n.sent == 2
    n.close()


def test_a_bad_url_sends_nothing():
    post = FakePost()
    n = notifier(post)
    results = []
    assert not n.send("https://example.com/x", dn.Note("done", "x"), on_done=lambda *a: results.append(a))
    assert post.posts == [] and results[0][0] is False and "webhook" in results[0][1]


def test_lanes_reaching_the_same_step_together_make_one_message():
    post = FakePost()
    n = notifier(post, group_wait=0.3)
    for ch in ("CH1", "CH5"):
        n.send(
            URL, dn.Note("review", "Waiting for you: {items}", items=[ch], group="review:3", ping=ch == "CH5")
        )
    n.send(URL, dn.Note("done", "other"))
    assert n.wait_idle(5)
    contents = [p[1]["content"] for p in post.posts]
    assert contents == ["**Waiting for you: CH1, CH5**", "**other**"]
    n.close()


def test_slow_down_is_waited_out_and_server_errors_retried():
    waits = []
    post = FakePost(Resp(429, {"retry_after": 1.5}), Resp(502), Resp(204))
    n = dn.Notifier(post=post, sleep=waits.append, group_wait=0)
    n.send(URL, dn.Note("done", "x"))
    assert n.wait_idle(5)
    assert len(post.posts) == 3 and waits == [1.5, dn.BACKOFF_S[1]] and n.sent == 1
    n.close()


def test_a_refused_message_is_not_retried_and_the_log_never_shows_the_url(caplog):
    post = FakePost(Resp(404, {"message": f"Unknown Webhook {URL}"}))
    n = notifier(post)
    results = []
    with caplog.at_level(logging.WARNING, logger="JebaoGUI"):
        n.send(URL, dn.Note("done", "x"), on_done=lambda ok, why: results.append((ok, why)))
        assert n.wait_idle(5)
    assert len(post.posts) == 1 and results[0][0] is False and "HTTP 404" in results[0][1]
    assert "Unknown Webhook" in caplog.text and TOKEN not in caplog.text and TOKEN not in results[0][1]
    assert n.failed == 1
    n.close()


def test_no_connection_is_retried_then_given_up_without_the_url(caplog):
    error = ConnectionError(f"Max retries exceeded with url: {URL[19:]}")
    post = FakePost(*([error] * (dn.RETRIES + 1)))
    n = notifier(post)
    with caplog.at_level(logging.WARNING, logger="JebaoGUI"):
        n.send(URL, dn.Note("done", "x"))
        assert n.wait_idle(5)
    assert len(post.posts) == dn.RETRIES + 1
    assert "couldn't reach Discord" in caplog.text and TOKEN not in caplog.text
    n.close()


def test_closing_sends_what_is_left_without_waiting_to_merge():
    post = FakePost()
    n = notifier(post, group_wait=30)
    n.send(URL, dn.Note("review", "Waiting for you: {items}", items=["CH1"], group="g"))
    n.close(timeout=5)
    assert len(post.posts) == 1
    assert not n.send(URL, dn.Note("done", "after"))  # closed: nothing more goes out


# --- what the app tells Discord -------------------------------------------------------------


class Recorder:
    """Stands in for the app's Notifier: keeps the notes instead of sending them."""

    def __init__(self):
        self.notes = []
        self.sent = self.failed = 0
        self.last_problem = ""

    def send(self, url, note, mention_id="", mention_kind="user", footer="", on_done=None):
        self.notes.append((url, note, mention_id, footer))
        if on_done:
            on_done(True, "")
        return True

    def close(self, timeout=None):
        pass

    def events(self):
        return [n.event for _u, n, _m, _f in self.notes]


@pytest.fixture
def discord(window, monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(window, "discord", rec)
    monkeypatch.setitem(window.secrets.data, "discord_webhook", URL)
    monkeypatch.setitem(window.config.data, "discord", {"enabled": True, "mention_id": ME})
    return window, rec


def test_nothing_is_sent_unless_switched_on(window, monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(window, "discord", rec)
    monkeypatch.setitem(window.secrets.data, "discord_webhook", URL)
    assert not window.notify("done", "x")  # off by default
    monkeypatch.setitem(window.config.data, "discord", {"enabled": True, "events": {"done": False}})
    assert not window.notify("done", "x") and window.notify("failed", "y")
    monkeypatch.setitem(window.secrets.data, "discord_webhook", "")
    assert not window.notify("failed", "y")
    assert len(rec.notes) == 1


def test_pings_follow_the_settings_and_pump_ids_never_go_out(discord):
    window, rec = discord
    window.cloud_devices = [{"did": "dEvIcE123456", "mac": "a1b2c3d4e5f6", "is_online": True}]
    window.notify("failed", "dEvIcE123456 · CH1 failed", body="MAC a1b2c3d4e5f6")
    window.notify("done", "Finished")
    (_u, failed, mention, footer), (_u2, done, _m, _f) = rec.notes
    assert failed.ping and not done.ping and mention == ME
    assert "dEvIcE123456" not in failed.title and failed.title.startswith("a pump")
    assert "a1b2c3d4e5f6" not in failed.body
    assert "Jebao Pump Sequencer" in footer


def test_the_webhook_is_private_and_diagnostics_never_show_it(discord):
    window, _rec = discord
    assert URL in window.private_values() and TOKEN in window.private_values()
    status = window._discord_status()
    assert status.startswith("on (") and TOKEN not in status


@pytest.fixture
def pumps(discord, monkeypatch):
    window, rec = discord
    window.cloud_devices = [{"did": "A", "dev_alias": "JP 1", "is_online": True}]
    window.config.get_pump("A")["calibration"] = [60.0] * 4
    window.config.get_pump("A")["display_name"] = ""
    window.config.get_pump("A")["head_names"] = ["", "", "", ""]
    sent = []

    async def fake_send(did, attrs):
        sent.append((did, dict(attrs)))
        return True

    monkeypatch.setattr(window, "send_cloud_command", fake_send)
    return window, rec


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


def test_wait_for_me_tells_discord_with_its_message_then_the_run_finishing(pumps, qt_loop):
    window, rec = pumps
    _, loop = qt_loop
    review = {
        **sq.new_step("review", lanes=[1]),
        "label": "Swap",
        "message": "Swap the cartridge.",
        "sound": False,
    }
    dose = {**sq.new_step("dose", heads=[["A", 1]]), "volume": 0.2}

    async def during(task):
        await until(lambda: window._ch_review_events.get(1))
        window.approve_channel(1)

    assert run(loop, window, [review, dose], during) == "done"
    assert rec.events() == ["review", "done"]
    note = rec.notes[0][1]
    assert note.headline() == "Waiting for you: CH1" and note.body == "Swap the cartridge."
    assert ("Sequence", "SPE A") in note.fields and note.ping and note.group.startswith("review:")
    done = rec.notes[1][1]
    assert done.title == "Finished “SPE A”" and not done.ping
    assert any(name == "Delivered" and "JP 1 · CH1: 0.20 mL" in value for name, value in done.fields)


def test_a_failed_run_pings(pumps, qt_loop, monkeypatch):
    window, rec = pumps
    _, loop = qt_loop

    async def refuse(did, attrs):
        return not any(attrs.values())  # every ON fails

    monkeypatch.setattr(window, "send_cloud_command", refuse)
    monkeypatch.setattr("jebao_gui.asyncio.sleep", _fast_sleep(asyncio.sleep))
    assert run(loop, window, [{**sq.new_step("dose", heads=[["A", 1]]), "volume": 0.2}]) == "failed"
    (_u, note, _m, _f) = rec.notes[-1]
    assert note.event == "failed" and note.ping and "sent OFF" in note.body


def _fast_sleep(real):
    async def sleep(seconds, *args, **kwargs):
        return await real(min(seconds, 0.01), *args, **kwargs)

    return sleep


def test_stop_run_is_told_without_a_ping(pumps, qt_loop):
    window, rec = pumps
    _, loop = qt_loop

    async def during(task):
        await until(lambda: ("A", 1) in window.head_runs)
        task.cancel()

    run(loop, window, [{**sq.new_step("dose", heads=[["A", 1]]), "volume": 5}], during)
    note = rec.notes[-1][1]
    assert note.event == "stopped" and not note.ping


def test_heads_not_confirmed_off_are_told_after_a_minute_then_all_clear(discord, monkeypatch):
    window, rec = discord
    key = ("A", 2)
    pending = {key: "pump offline"}
    window._notify_guard(pending, now=100.0)
    assert rec.notes == []  # not yet: OFFs often get through after a retry
    window._notify_guard(pending, now=100.0 + 61)
    window._notify_guard(pending, now=100.0 + 120)  # once only
    assert rec.events() == ["guard"]
    note = rec.notes[0][1]
    assert note.ping and "pump offline" in note.headline() and note.group == "guard"
    window._notify_guard({}, now=300.0)
    assert rec.events() == ["guard", "guard"] and not rec.notes[1][1].ping
    window._notify_guard({}, now=400.0)
    assert len(rec.notes) == 2


def test_a_pump_going_offline_is_told_only_while_in_use(discord):
    window, rec = discord
    window.cloud_devices = [{"did": "A", "dev_alias": "JP 1", "is_online": True}]
    window._note_online_changes()
    window.cloud_devices[0]["is_online"] = False
    window._note_online_changes()
    assert rec.notes == []  # nothing was running on it
    window.cloud_devices[0]["is_online"] = True
    window._note_online_changes()
    window.head_runs[("A", 3)] = object()
    try:
        window.cloud_devices[0]["is_online"] = False
        window._note_online_changes()
    finally:
        window.head_runs.pop(("A", 3))
    window.cloud_devices[0]["is_online"] = True
    window._note_online_changes()
    assert rec.events() == ["offline", "offline"]
    gone, back = rec.notes[0][1], rec.notes[1][1]
    assert gone.title.endswith("went offline") and gone.ping
    assert back.title.endswith("is back online") and not back.ping


def test_the_test_message_uses_the_dialog_values(discord):
    window, rec = discord
    results = []
    window.discord_test(URL, {"mention_id": ""}, on_done=lambda ok, why: results.append(ok))
    note = rec.notes[0][1]
    assert note.event == "test" and not note.ping and results == [True]


# --- Settings → Discord ---------------------------------------------------------------------


def test_the_dialog_saves_the_webhook_in_secrets(window, monkeypatch):
    from discord_dialog import DiscordDialog

    saved = []
    monkeypatch.setattr(window.secrets, "save", lambda: saved.append("secrets"))
    monkeypatch.setattr(window.config, "save", lambda: saved.append("config"))
    monkeypatch.setitem(window.config.data, "discord", {})
    window.secrets.data.pop("discord_webhook", None)
    dlg = DiscordDialog(window)
    dlg.chk_enabled.setChecked(True)
    dlg.url_input.setText("https://example.com/hook")
    dlg.accept()
    assert dlg.result() == 0 and "webhook" in dlg.lbl_status.text() and saved == []
    dlg.url_input.setText(URL)
    dlg.mention_input.setText(ME)
    dlg.chk_send["rest"].setChecked(False)
    dlg.accept()
    assert dlg.result() == 1 and sorted(saved) == ["config", "secrets"]
    assert window.secrets.data["discord_webhook"] == URL
    settings = window.config.data["discord"]
    assert settings["enabled"] and settings["mention_id"] == ME and not settings["events"]["rest"]
    assert URL not in str(settings)  # the URL is only in secrets.json
    window.secrets.data.pop("discord_webhook", None)


def test_the_dialog_test_button_reports_back(window, monkeypatch, qt_loop):
    from discord_dialog import DiscordDialog

    asked = []

    def fake_test(url, values, on_done=None):
        asked.append(url)
        on_done(False, "Discord refused it (HTTP 404: Unknown Webhook)")

    monkeypatch.setattr(window, "discord_test", fake_test)
    dlg = DiscordDialog(window)
    dlg.url_input.setText("")
    dlg.send_test()
    assert asked == [] and "No webhook" in dlg.lbl_status.text()
    dlg.url_input.setText(URL)
    dlg.send_test()
    qt_loop[0].processEvents()
    assert asked == [URL] and "HTTP 404" in dlg.lbl_status.text() and dlg.btn_test.isEnabled()


def test_a_broken_notification_never_gets_in_the_way_of_a_run(pumps, qt_loop, monkeypatch, caplog):
    window, _rec = pumps
    _, loop = qt_loop

    def broken(*args, **kwargs):
        raise ValueError("oops")

    monkeypatch.setattr(window, "notify", broken)
    review = {**sq.new_step("review", lanes=[1]), "sound": False}

    async def during(task):
        await until(lambda: window._ch_review_events.get(1))
        window.approve_channel(1)

    with caplog.at_level(logging.WARNING, logger="JebaoGUI"):
        assert (
            run(loop, window, [review, {**sq.new_step("dose", heads=[["A", 1]]), "volume": 0.1}], during)
            == "done"
        )
    assert window.seq_run.outcome == "done"
    assert "Discord notification skipped (ValueError: oops)" in caplog.text
