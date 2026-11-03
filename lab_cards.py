"""What the Lab Manager bot shows in Discord, worked out from what the app is doing now.

``specs(host, settings, instance)`` returns ``{key: lab_manager.Spec}``: the run card, a ping
for each Wait for me step channels are waiting at, and pings for heads not confirmed OFF,
pumps offline while in use and how the last run ended. The bot makes the channel match it.
No Qt here; ``host`` is the main window (see the names it uses below).
"""

import time
from datetime import datetime

import discord_notify as dn
import sequence
from lab_manager import Button, Spec, custom_id

ORANGE, GREEN, RED, GREY, BLUE = 0xE67E22, 0x28A745, 0xDC3545, 0x95A5A6, 0x3498DB
MAX_FIELDS = 24


def _clock(t):
    return datetime.fromtimestamp(t).strftime("%H:%M")


def _mention(settings, event):
    """(user id, role id) to ping for ``event``, as set in Settings → Discord."""
    if (
        not settings["pings"].get(event)
        or dn.check_mention(settings["mention_id"])
        or not settings["mention_id"]
    ):
        return "", ""
    if settings["mention_kind"] == "role":
        return "", settings["mention_id"]
    return settings["mention_id"], ""


def _content(text, user="", role=""):
    head = f"<@&{role}> " if role else (f"<@{user}> " if user else "")
    return (head + text)[: dn.MAX_CONTENT]


def _embed(color, title="", body="", fields=(), footer=""):
    embed = {"color": color}
    if title:
        embed["title"] = dn._cut(dn.escape(title), 256)
    if body:
        embed["description"] = dn._cut(dn.escape(body), dn.MAX_DESCRIPTION)
    shown = [
        {"name": dn._cut(dn.escape(n), 256), "value": dn._cut(dn.escape(v), 1024), "inline": "\n" not in v}
        for n, v in fields
        if str(n).strip() and str(v).strip()
    ][:MAX_FIELDS]
    if shown:
        embed["fields"] = shown
    if footer:
        embed["footer"] = {"text": dn._cut(footer, dn.MAX_FOOTER)}
    return embed


def lane_text(host, run, lane):
    """(status, text) for one lane on the run card."""
    ls = run.lanes[lane]
    if host._ch_review_events.get(lane):
        return "waiting", "Waiting for you" + (f": {ls.title}" if ls.title else "")
    status = ls.status
    if run.ended and status not in ("idle", "done"):
        return "stopped", f"Stopped at step {ls.top + 1}" if ls.top is not None else "Stopped"
    if status == "idle":
        return "idle", "Not used"
    if status == "done":
        return "done", "Done"
    if host._ch_paused.get(lane):
        return "paused", f"Paused: {ls.title}"
    resting = []
    for (did, ch), hr in host.head_runs.items():
        if hr.source != "sequence" or (hr.lane if hr.lane is not None else ch) != lane:
            continue
        st = host.run_state(did, ch)
        if st and st[0] == "cooling":
            resting.append(f"{host.head_label(did, ch)} until {_clock(time.time() + st[2])}")
    if resting:
        return "resting", f"{ls.title}\nResting (on-time limit): " + ", ".join(resting)
    text = f"Step {ls.top + 1}: {ls.title}" if ls.top is not None else ls.title
    if ls.detail:
        text += f"\n{ls.detail}"
    if ls.ml > 0 and status == "pumping":
        text += f"\n{sequence.fmt_ml(ls.ml)} so far"
    return status, text


def run_card(host, run, run_id, instance, footer):
    clean = host._discord_text
    name = clean(run.name)
    lanes = sorted(run.lanes)
    shown = [(ln, *lane_text(host, run, ln)) for ln in lanes]
    statuses = [st for _ln, st, _t in shown]
    fields = [(f"CH{ln}", clean(text)) for ln, st, text in shown if st != "idle"]
    took = sequence.fmt_time(run.elapsed())
    if run.ended:
        if run.outcome == "done":
            title, color = f"✓ Finished “{name}”", GREEN
            body = f"Took {took} ({_clock(run.started)}–{_clock(run.ended)})."
        elif run.outcome == "failed":
            title, color = f"✕ “{name}” failed", RED
            body = f"After {took}: {clean(run.why)}. Every head it used was sent OFF."
        else:
            title, color = f"■ “{name}” stopped", GREY
            at = f" at {run.stopped_at}" if run.stopped_at else ""
            body = f"After {took}{at}: {clean(run.why)}."
        given = [(clean(label), ml) for label, ml in run.delivered.items() if ml > 0.005]
        if given:
            lines = [f"{label}: {ml:.2f} mL" for label, ml in given[:12]]
            if len(given) > 12:
                lines.append(f"and {len(given) - 12} more")
            fields.append(("Delivered", "\n".join(lines)))
        buttons = ()
    else:
        if "waiting" in statuses:
            title, color = f"⏳ “{name}” is waiting for you", ORANGE
        elif statuses and all(st in ("paused", "idle", "done") for st in statuses) and "paused" in statuses:
            title, color = f"⏸ “{name}” is paused", ORANGE
        else:
            title, color = f"▶ Running “{name}”", BLUE
        left = run.left_s()
        body = f"Started {_clock(run.started)}"
        if left:
            body += f" · about {sequence.fmt_time(left)} left (≈ {_clock(time.time() + left)})"
        if run.stopping:
            body += " · stopping…"
        paused = any(st == "paused" for st in statuses)
        buttons = (
            Button(
                custom_id(instance, "resume" if paused else "pause", run_id),
                "Resume all" if paused else "Pause all",
            ),
            Button(custom_id(instance, "stop", run_id), "Stop run", "danger"),
        )
        if run.stopping:
            buttons = ()
    queued = len(host.sequence_queue)
    if queued:
        nxt = host.sequence_queue[0].get("name") or "Untitled sequence"
        paused_q = host.queue_paused or (run.ended and run.outcome != "done")
        fields.append(("Queue", f"{'Paused: ' if paused_q else 'Next: '}“{clean(nxt)}” ({queued} waiting)"))
    sig = f"{title}|{statuses}|{[b.label for b in buttons]}|{queued}"
    return Spec(
        f"card:{run_id}",
        embed=_embed(color, title, body, fields, footer),
        buttons=buttons,
        sig=sig,
        final=bool(run.ended),
    )


def wait_pings(host, settings, run, run_id, instance, footer):
    """One message per Wait for me step that channels are waiting at."""
    out = {}
    if not settings["events"].get("review"):
        return out
    at = {}
    for lane, event in host._ch_review_events.items():
        if event and lane in host._review_at:
            at.setdefault(tuple(host._review_at[lane]), []).append(lane)
    user, role = _mention(settings, "review")
    clean = host._discord_text
    for path, lanes in at.items():
        lanes = sorted(lanes)
        first = lanes[0]
        where = clean(host._ch_review_titles.get(first, ""))
        message = clean(host._ch_review_messages.get(first, ""))
        remote = all(host._review_remote.get(ln) for ln in lanes)
        path_key = "-".join(str(p) for p in path)
        chans = sequence.lanes_text(lanes)
        text = (
            f"**Waiting for you: {dn.escape(chans)}** · {dn.escape(where)} in “{dn.escape(clean(run.name))}”"
        )
        buttons = ()
        if remote:
            buttons = tuple(
                Button(custom_id(instance, "go", f"{run_id}|{path_key}|{ln}"), f"Continue CH{ln}", "primary")
                for ln in lanes
            )
            if len(lanes) > 1:
                buttons += (
                    Button(custom_id(instance, "goall", f"{run_id}|{path_key}"), "Continue all", "success"),
                )
            note = "Check, then press Continue here or in the app."
        else:
            note = (
                "Continue in the app. (Tick “Allow Continue from Discord” in the step to get buttons here.)"
            )
        key = f"wait:{run_id}:{path_key}"
        out[key] = Spec(
            key,
            content=_content(text, user, role),
            embed=_embed(ORANGE, body=message, footer=f"{note}\n{footer}"),
            buttons=buttons,
            ping_user=user,
            ping_role=role,
            sig=str(lanes),
        )
    return out


def alerts(host, settings, instance, footer):
    out = {}
    clean = host._discord_text
    if settings["events"].get("guard") and host._guard_told:
        pending = host.guard.pending()
        heads = [
            f"{clean(host.head_label(*k))} ({pending[k]})"
            for k in sorted(host._guard_told, key=str)
            if k in pending
        ]
        if heads:
            user, role = _mention(settings, "guard")
            out["alert:guard"] = Spec(
                "alert:guard",
                content=_content("**Can't confirm these heads are OFF**", user, role),
                embed=_embed(
                    RED,
                    body="\n".join(heads)
                    + "\n\nThe app keeps trying to turn them off. If a pump is offline, unplug it or "
                    "switch it off by hand. This message goes away once they're off.",
                    footer=footer,
                ),
                ping_user=user,
                ping_role=role,
                sig=str(heads),
            )
    if settings["events"].get("offline") and host._offline_told:
        online = {d.get("did"): d.get("is_online", False) for d in host.cloud_devices}
        gone = [clean(host.get_device_name(did)) for did in sorted(host._offline_told) if not online.get(did)]
        if gone:
            user, role = _mention(settings, "offline")
            out["alert:offline"] = Spec(
                "alert:offline",
                content=_content(f"**Offline while in use: {dn.escape(', '.join(gone))}**", user, role),
                embed=_embed(
                    RED,
                    body="A head that was on may keep pumping until its pump is back online (the app "
                    "then turns it off). Switch it off by hand if you can. This message goes away "
                    "once the pump is back.",
                    footer=footer,
                ),
                ping_user=user,
                ping_role=role,
                sig=str(gone),
            )
    outcome = getattr(host, "_lab_outcome", None)
    if outcome:
        event = outcome["event"]
        user, role = _mention(settings, event)
        key = f"alert:{outcome['run_id']}"
        out[key] = Spec(
            key,
            content=_content(f"**{dn.escape(clean(outcome['title']))}**", user, role),
            embed=_embed(RED if event == "failed" else GREY, body=clean(outcome["body"]), footer=footer),
            buttons=(Button(custom_id(instance, "dismiss", outcome["run_id"]), "Got it"),),
            ping_user=user,
            ping_role=role,
            sig=outcome["title"],
        )
    return out


def specs(host, settings, instance):
    """Everything the channel should show now."""
    footer = host.discord_footer()
    out = {}
    run = host.seq_run
    run_id = getattr(host, "_run_id", "")
    if run is not None and run_id:
        out[f"card:{run_id}"] = run_card(host, run, run_id, instance, footer)
        if not run.ended:
            out.update(wait_pings(host, settings, run, run_id, instance, footer))
    out.update(alerts(host, settings, instance, footer))
    return out


def closed_card(key, footer=""):
    """A run card from a session that ended mid-run (the app closed or crashed)."""
    return Spec(
        key,
        embed=_embed(
            GREY, "■ The app closed before this run ended", "See the run history in the app.", footer=footer
        ),
        final=True,
    )
