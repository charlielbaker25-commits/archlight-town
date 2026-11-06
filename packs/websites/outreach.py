"""The two outreach action types the Messenger proposes, and what approving them does.

- send_email (EXTERNAL): an email to a lead from town@example.com. It is never in
  pack.LIVE_EXTERNAL_TYPES. While go-live is off (always, until Charlie switches it on: packs/websites/golive.py)
  the executor DRY-RUNS it: the approved email is saved as a Gmail draft in the Town's own Gmail (Outbox.save_draft),
  exactly as the prospect would receive it, and nothing is sent. Only while every go-live condition holds does the
  executor run it for real: Outbox.send, which checks every gate again in plain code and then sends that same
  approved draft with drafts.send, once, never retried. Without an Outbox, run() refuses (nothing to send with).
- call_script (INTERNAL): a script for Charlie to read when HE calls a lead that has no public email.
  Approving it only marks it ready (`town calls` lists it). The Town never calls anyone.
"""
import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Callable

from engine.db import utc_now
from engine.events import log_event
from engine.executor import Handler
from engine.killswitch import KillSwitch
from engine.limits import Limits
from packs.websites.gmail import GmailClient, GmailError, build_message, single_address
from packs.websites.guard import OPT_OUT_LINE, dnc_matches, norm_email, outreach_pause, plain
from packs.websites.scout import local_day_start_utc

SEND_EMAIL, CALL_SCRIPT = "send_email", "call_script"
# One approval per prospect (Charlie, 2026-11-05): the site and the first email in ONE proposal (packs/websites/
# package.py). Its email part goes through this same Outbox (save_draft, or send while go-live is live).
OUTREACH_PACKAGE = "outreach_package"
EMAIL_ACTIONS = (SEND_EMAIL, OUTREACH_PACKAGE)
AGENT = "messenger"


def email_part(action_type: str, content: dict[str, Any]) -> dict[str, Any]:
    """The email an action carries: a send_email's whole content, or an outreach package's `email` part."""
    if action_type == OUTREACH_PACKAGE:
        email = content.get("email")
        return email if isinstance(email, dict) else {}
    return content


class OutboxRefused(GmailError):
    """A plain-code check at execution stopped the draft before Gmail was called."""


def _never_send(conn: sqlite3.Connection, action_id: int, content: dict[str, Any]) -> str:
    raise RuntimeError("Email sending isn't set up here (no Outbox: no Gmail client). Nothing was sent.")


def content_hash(content: dict[str, Any]) -> str:
    """SHA-256 of exactly what the prospect receives (To, subject, body). A send must match the approved one."""
    exact = {"to": str(content.get("to") or "").strip().lower(), "subject": str(content.get("subject") or ""),
             "body": str(content.get("body") or "")}
    return hashlib.sha256(json.dumps(exact, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _describe_email(content: dict[str, Any]) -> str:
    return (f"email {content.get('to')} from {content.get('from')}: \"{content.get('subject')}\" "
            f"({len(str(content.get('body') or '').split())} words, {content.get('kind') or 'first'} email)")


class SendRefused(OutboxRefused):
    """A go-live gate stopped a real send. Nothing was sent; the action is marked failed (never retried)."""


@dataclass
class Outbox:
    """Saves an approved email as a Gmail draft (dry run) or, once go-live is on, sends that draft. The Guard has
    already re-checked it (the executor does that before claiming the action); these checks run again here, in
    plain code, right before Gmail is called.

    `live_problems` and `mailing_address` come from the launcher (golive.live_problems for this instance); without
    them nothing can be sent."""
    client: GmailClient
    killswitch: KillSwitch
    limits: Limits | None = None
    mailing_address: str = ""
    live_problems: Callable[[sqlite3.Connection], list[str]] = field(
        default=lambda conn: ["go-live isn't set up for this Outbox"])

    def save_draft(self, conn: sqlite3.Connection, action_id: int, content: dict[str, Any]) -> str:
        saved = self._make_draft(conn, action_id, content)
        return f"dry-run: not sent; saved as Gmail draft {saved.draft_id}"

    def _make_draft(self, conn: sqlite3.Connection, action_id: int, content: dict[str, Any]) -> Any:
        to = single_address(content.get("to"))                  # one plain address, no line break
        lead = conn.execute("SELECT public_email FROM leads WHERE id = ?", (content.get("lead_id"),)).fetchone() \
            if content.get("lead_id") else None
        if not lead:
            raise OutboxRefused("the email isn't tied to a saved lead, so no draft was made")
        if to != norm_email(lead["public_email"]):
            raise OutboxRefused(f"the To address {to} isn't the lead's stored public email "
                                f"({lead['public_email'] or 'none'}), so no draft was made")
        blocked = dnc_matches(conn, content)
        if blocked:
            raise OutboxRefused(f"{blocked[0]}, so no draft was made")
        if conn.execute("SELECT 1 FROM gmail_drafts WHERE action_id = ?", (action_id,)).fetchone():
            raise OutboxRefused(f"action #{action_id} already has a Gmail draft")
        raw = build_message(to, content.get("subject"), content.get("body"))   # exactly the approved content
        self.killswitch.check()                                  # the last moment before Gmail
        saved = self.client.create_draft(raw)
        # Logged first, so the draft's id is on record even if saving the row below fails.
        log_event(conn, AGENT, "gmail_draft_created", {
            "draft_id": saved.draft_id, "message_id": saved.message_id, "thread_id": saved.thread_id, "to": to,
            "subject": content.get("subject"), "fake": not self.client.real, "sent": False}, action_id)
        conn.execute("INSERT INTO gmail_drafts (action_id, lead_id, draft_id, message_id, thread_id, to_address,"
                     " subject, fake, created_at, content_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (action_id, content.get("lead_id"), saved.draft_id, saved.message_id, saved.thread_id, to,
                      content.get("subject"), 0 if self.client.real else 1, utc_now(), content_hash(content)))
        return saved

    # ---------------- go-live: the one real send ----------------

    def send(self, conn: sqlite3.Connection, action_id: int, content: dict[str, Any]) -> str:
        """Sends the approved email for real: the executor calls this only while go-live is live, after the Guard's
        re-check and its claim. Every gate runs again here, in plain code, in this order; any one failing stops it
        before Gmail is called. Then the approved draft is made and sent with drafts.send, once. An error is never
        retried: the action is marked failed and Charlie decides (a draft left in Gmail says so in the log)."""
        problems = self.live_problems(conn)
        if problems:
            raise SendRefused("go-live isn't on (" + "; ".join(problems) + "). Nothing was sent.")
        self.killswitch.check()
        approved = conn.execute("SELECT status, content, action_type FROM actions WHERE id = ?", (action_id,)).fetchone()
        if not approved or approved["status"] != "executed" or approved["action_type"] not in EMAIL_ACTIONS:
            # the executor's claim, so it's this run's own action (a send_email, or an outreach package's email)
            raise SendRefused(f"action #{action_id} isn't an approved email claimed for this run. Nothing was sent.")
        if content_hash(email_part(approved["action_type"], json.loads(approved["content"]))) != content_hash(content):
            raise SendRefused("the email changed after it was approved. Nothing was sent.")
        to = single_address(content.get("to"))
        lead = conn.execute("SELECT public_email, status FROM leads WHERE id = ?", (content.get("lead_id"),)).fetchone() \
            if content.get("lead_id") else None
        if not lead or to != norm_email(lead["public_email"]):
            raise SendRefused(f"the To address {to} isn't the lead's stored public email. Nothing was sent.")
        if lead["status"] == "bounced":
            raise SendRefused("an email to this lead bounced before. Nothing was sent.")
        blocked = dnc_matches(conn, content)
        if blocked:
            raise SendRefused(f"{blocked[0]}. Nothing was sent.")
        if self.limits is None:
            raise SendRefused("no limits given, so the daily cap can't be checked. Nothing was sent.")
        cap = int(self.limits.get("email.sends_per_day"))
        sent_today = int(conn.execute("SELECT COUNT(*) FROM gmail_drafts WHERE sent_at IS NOT NULL AND sent_at >= ?",
                                      (local_day_start_utc(),)).fetchone()[0])
        if sent_today >= cap:
            raise SendRefused(f"{sent_today} emails already went out today (email.sends_per_day is {cap}). "
                              "Nothing was sent.")
        paused = outreach_pause(conn, self.limits)
        if paused:
            raise SendRefused(f"outreach is paused: {paused}. Nothing was sent.")
        body = plain(content.get("body"))
        if plain(OPT_OUT_LINE) not in body:
            raise SendRefused("the opt-out line is missing. Nothing was sent.")
        address = (self.mailing_address or "").strip()
        if not address or plain(address) not in body:
            raise SendRefused("the mailing address is missing from the email. Nothing was sent.")
        saved = self._make_draft(conn, action_id, content)
        row = conn.execute("SELECT content_hash FROM gmail_drafts WHERE action_id = ?", (action_id,)).fetchone()
        if not row or row["content_hash"] != content_hash(content):
            raise SendRefused(f"Gmail draft {saved.draft_id} doesn't match the approved email; it was NOT sent.")
        self.killswitch.check()                                   # the last moment before the one send
        try:
            result = self.client.send_draft(saved.draft_id)
        except GmailError as err:
            log_event(conn, AGENT, "email_send_failed", {"draft_id": saved.draft_id, "to": to,
                                                          "error": str(err)[:300], "retried": False}, action_id)
            raise GmailError(f"{err} (draft {saved.draft_id} is in Gmail, NOT sent; never retried)") from None
        log_event(conn, AGENT, "email_sent", {"draft_id": saved.draft_id, "message_id": result.message_id,
                                              "thread_id": result.thread_id, "to": to, "lead_id": content.get("lead_id"),
                                              "subject": content.get("subject"), "fake": not self.client.real},
                  action_id)
        conn.execute("UPDATE gmail_drafts SET sent_at = ?, sent_message_id = ?, sent_thread_id = ? WHERE action_id = ?",
                     (utc_now(), result.message_id, result.thread_id or saved.thread_id, action_id))
        return f"SENT to {to} (Gmail message {result.message_id}{'; FAKE Gmail: nothing left this laptop' if not self.client.real else ''})"


def _call_script_ready(conn: sqlite3.Connection, action_id: int, content: dict[str, Any]) -> str:
    return (f"Call script ready for Charlie: {content.get('business')} at {content.get('phone')}. "
            "See `town calls`. (The Town never calls anyone.)")


def outreach_handlers(outbox: Outbox | None = None) -> dict[str, Handler]:
    """With no outbox (some engine-level tests), approving an email only describes it, as before session H."""
    return {SEND_EMAIL: Handler(SEND_EMAIL, True, outbox.send if outbox else _never_send, _describe_email,
                                rehearse=outbox.save_draft if outbox else None),
            CALL_SCRIPT: Handler(CALL_SCRIPT, False, _call_script_ready,
                                 lambda c: f"mark the call script for {c.get('business')} ready")}
