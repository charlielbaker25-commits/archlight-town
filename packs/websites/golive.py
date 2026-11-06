"""Go-live: the send switch, the shadow days and the checklist (Charlie's decisions, 2026-11-03).

Real sending is OFF until Charlie switches it on himself. `send_email` is live (an approved email's Gmail draft is
really sent) only while ALL of these hold, checked in plain code right before every send:
1. the switch is on: `<state folder>/golive.json` says `"sending": true` (written only by `town golive`, which
   only Charlie can run, only on his real instance, never from Claude Code, the dev instance or a test);
2. the instance's `mailing_address` isn't empty (CAN-SPAM: it goes in every email's footer);
3. at least `golive.shadow_days_required` (limits.yaml, 3) shadow days are logged.
Anything unreadable counts as OFF (fail closed). The switch lives in the state folder, next to the kill switch,
not in instance.yaml: instance.yaml is in git, and a merge or a pull must never be able to turn sending on.

Shadow days (`town shadow start`): from then on, each day the Town runs the full loop in dry-run (Scout proposes,
a site is built or reviewed, an approved email lands in Gmail as a draft, the inbox is checked, and nothing is
sent) is logged once as a `shadow_day` event. Checked after every `town` command, so nothing extra has to run.

Even when live, every email still needs Charlie's approval in the queue, the Guard's caps and rules, and the kill
switch (outreach.Outbox.send has the full list).
"""
import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from engine.events import log_event
from engine.limits import Limits
from packs.websites.scout import local_day_start_utc

AGENT = "mayor"                      # flips and shadow days show at the Town Hall
SEND_EMAIL = "send_email"
CONFIRM_PHRASE = "SEND REAL EMAIL"   # what Charlie types to switch sending on
SWITCH_FILE = "golive.json"
# Printed by `town golive` right after the switch flips (the Town can't delete Gmail drafts, on purpose).
SHADOW_DRAFTS_WARNING = ("The shadow-day drafts in Gmail are NOT sent and never will be. Don't press Send on any draft "
                         "in Gmail yourself: that skips the Guard, the do-not-contact list and the Clerk. Delete them "
                         "in Gmail when you're done reviewing; `town draft` proposes fresh emails for those leads.")
# What a shadow day needs, each from a real event that day: (step, event types that count, plain words).
SHADOW_STEPS = (
    ("scout", ("scout_run",), "Scout proposed leads"),
    ("build", ("build_done", "review_done"), "a site was built or reviewed"),
    ("draft", ("gmail_draft_created",), "an approved email was saved as a Gmail draft"),
    ("inbox", ("clerk_finished",), "the inbox was checked (`town inbox`)"),
)


# ---------------- the switch ----------------

def switch_path(settings: Any) -> Path:
    return Path(settings.state_folder) / SWITCH_FILE


def read_switch(settings: Any) -> dict[str, Any]:
    """The switch as saved. Missing or unreadable = off."""
    try:
        data = json.loads(switch_path(settings).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"sending": False}
    return data if isinstance(data, dict) and data.get("sending") is True else {**(data if isinstance(data, dict)
                                                                                    else {}), "sending": False}


def _write(settings: Any, data: dict[str, Any]) -> None:
    path = switch_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def flip_on(conn: sqlite3.Connection, settings: Any, by: str, answers: dict[str, Any]) -> None:
    """Switch sending on. Only `town golive` calls this, after the checklist passed and Charlie typed the phrase
    (the launcher refuses anyone but Charlie on his real instance). Logged as `golive_on` with his answers."""
    _write(settings, {"sending": True, "by": by, "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                      **answers})
    log_event(conn, by, "golive_on", {"by": by, **answers})


def flip_off(conn: sqlite3.Connection, settings: Any, by: str, reason: str) -> None:
    """Switch sending off (the safe direction). Logged as `golive_off`."""
    _write(settings, {"sending": False, "by": by, "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                      "reason": reason})
    log_event(conn, by, "golive_off", {"by": by, "reason": reason})


# ---------------- shadow days ----------------

def shadow_started(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT ts FROM events WHERE event_type = 'shadow_started' ORDER BY id DESC LIMIT 1").fetchone()
    return row["ts"] if row else None


def start_shadow(conn: sqlite3.Connection, by: str) -> bool:
    """`town shadow start`. False if shadow mode had already started (nothing changes)."""
    if shadow_started(conn):
        return False
    log_event(conn, AGENT, "shadow_started", {"by": by})
    return True


def shadow_days(conn: sqlite3.Connection) -> list[str]:
    """The days (Charlie's calendar, YYYY-MM-DD) logged as shadow days since shadow mode started."""
    started = shadow_started(conn)
    if not started:
        return []
    rows = conn.execute("SELECT details FROM events WHERE event_type = 'shadow_day' AND ts >= ? ORDER BY id",
                        (started,)).fetchall()
    return sorted({str(json.loads(r["details"]).get("day")) for r in rows})


def today_steps(conn: sqlite3.Connection, since: str | None = None) -> dict[str, bool]:
    """Which parts of the full loop ran today (since Charlie's local midnight, or since shadow mode started)."""
    since = max(local_day_start_utc(), since or "")
    done = {}
    for step, kinds, _ in SHADOW_STEPS:
        marks = ",".join("?" * len(kinds))
        done[step] = bool(conn.execute(f"SELECT 1 FROM events WHERE ts >= ? AND event_type IN ({marks}) LIMIT 1",
                                       (since, *kinds)).fetchone())
    return done


def sent_today(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM events WHERE event_type = 'email_sent' AND ts >= ?",
                            (local_day_start_utc(),)).fetchone()[0])


def record_shadow_day(conn: sqlite3.Connection, settings: Any) -> str | None:
    """Logs today as a shadow day once every step ran in dry-run (sending off, nothing sent today). Returns the day
    if it logged one now. Plain code; safe to call after every command."""
    started = shadow_started(conn)
    if not started:
        return None
    day = datetime.now().astimezone().date().isoformat()
    if day in shadow_days(conn) or read_switch(settings)["sending"] or sent_today(conn):
        return None
    if not all(today_steps(conn, started).values()):
        return None
    log_event(conn, AGENT, "shadow_day", {"day": day, "steps": [s for s, _, _ in SHADOW_STEPS]})
    return day


# ---------------- is sending live right now? ----------------

def live_problems(conn: sqlite3.Connection, settings: Any, limits: Limits) -> list[str]:
    """Why real sending is NOT live right now, in plain words. Empty = live. Fails closed."""
    try:
        problems = []
        if not read_switch(settings)["sending"]:
            problems.append("the go-live switch is off (only Charlie turns it on, with `town golive`)")
        if not str(getattr(settings, "mailing_address", "") or "").strip():
            problems.append("no mailing address is set (mailing_address in the instance file)")
        need, have = int(limits.get("golive.shadow_days_required")), len(shadow_days(conn))
        if have < need:
            problems.append(f"{have} of {need} shadow days logged (golive.shadow_days_required)")
        return problems
    except Exception as err:     # anything that can't be checked means not live
        return [f"go-live couldn't be checked ({type(err).__name__}: {err})"]


def live_types(conn: sqlite3.Connection, settings: Any, limits: Limits) -> frozenset[str]:
    """For the executor: {"send_email"} only while every go-live condition holds, else nothing."""
    return frozenset({SEND_EMAIL}) if not live_problems(conn, settings, limits) else frozenset()


# ---------------- the checklist (`town golive` and HQ, read-only there) ----------------

def checklist(conn: sqlite3.Connection, settings: Any, limits: Limits, scopes: tuple[bool, str] | None = None,
              dad_ok: bool | None = None) -> list[tuple[bool, str]]:
    """Each line: (passes, plain words). `scopes` is the Gmail scope check's result (None = not checked here: the
    last `town gmail-check` result from the log is shown). `dad_ok` is Charlie's own answer (None = not asked)."""
    from packs.websites.guard import outreach_pause   # here, to keep the import order simple
    lines: list[tuple[bool, str]] = []
    address = str(getattr(settings, "mailing_address", "") or "").strip()
    lines.append((bool(address), f"Mailing address: {address}" if address else
                  "Mailing address: NOT SET (mailing_address in instance.yaml)"))
    need, days = int(limits.get("golive.shadow_days_required")), shadow_days(conn)
    started = shadow_started(conn)
    lines.append((len(days) >= need, f"Shadow days: {len(days)} of {need}" + (f" ({', '.join(days)})" if days else "")
                  + ("" if started else " (shadow mode not started: `town shadow start`)")))
    if scopes is None:
        row = conn.execute("SELECT details FROM events WHERE event_type = 'gmail_scopes_checked' ORDER BY id DESC"
                           " LIMIT 1").fetchone()
        last = json.loads(row["details"]) if row else None
        scopes = (bool(last and last.get("ok")), "Gmail scopes: " + (
            f"{last.get('line')} (last `town gmail-check`)" if last else "not checked yet (`town gmail-check`)"))
    lines.append(scopes)
    cap, warmup = int(limits.get("email.sends_per_day")), int(limits.get("golive.warmup_sends_per_day"))
    lines.append((cap <= warmup, f"Today's send cap: {cap} a day, {sent_today(conn)} sent today" + (
        "" if cap <= warmup else f" (start at {warmup}: `town limit email.sends_per_day {warmup} --reason "
                                 "\"deliverability warm-up\"`, then approve it)")))
    paused = outreach_pause(conn, limits)
    lines.append((paused is None, "Bounces and complaints: none pausing outreach" if paused is None else
                  f"Outreach paused: {paused}"))
    if dad_ok is not None:
        lines.append((dad_ok, "Permission to use the mailing address: " + ("yes (Charlie)" if dad_ok else "NO")))
    on = read_switch(settings)["sending"]
    lines.append((True, f"Switch: {'ON (real sending)' if on else 'off (dry-run: approved emails become Gmail drafts)'}"))
    return lines


def status(conn: sqlite3.Connection, settings: Any, limits: Limits) -> dict[str, Any]:
    """The checklist as data, for HQ (read-only there; flipping stays a terminal command)."""
    started = shadow_started(conn)
    return {"sending": read_switch(settings)["sending"],
            "live_problems": live_problems(conn, settings, limits),
            "checklist": [{"ok": ok, "line": line} for ok, line in checklist(conn, settings, limits)],
            "shadow_started": started, "shadow_days": shadow_days(conn),
            "shadow_days_required": int(limits.get("golive.shadow_days_required")),
            "today": today_steps(conn, started) if started else {}}


def shadow_day_words(steps: dict[str, bool]) -> list[str]:
    return [f"{'done' if steps.get(step) else 'not yet'}: {words}" for step, _, words in SHADOW_STEPS]
