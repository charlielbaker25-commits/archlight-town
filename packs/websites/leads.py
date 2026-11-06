"""Leads: the contact-route rule and the handler that saves an approved lead.

Email-first (Charlie, 2026-11-03): the Town contacts prospects by email only. A lead with no public email is
saved as "held (no email)" (status `held`) and never contacted: no inspection in a bulk run, no build, no draft.
Charlie calls only when a prospect asks for a call by replying to an email (the Clerk flags it). The
`contact_route` column still says `call` for a lead without an email (the column's two values stay as they were),
but `is_held()` is the rule every agent uses.
"""
import re
import sqlite3
from typing import Any
from urllib.parse import unquote

from engine.db import utc_now
from engine.executor import Handler

LEAD_FIELDS = ("business_name", "business_type", "area", "address", "phone", "website", "public_email",
               "source", "source_id", "source_release", "confidence")


def contact_route(public_email: str | None) -> str:
    """Email if the business lists a public email; otherwise a call script for Charlie."""
    return "email" if (public_email or "").strip() else "call"


HELD = "held"
HELD_WORDS = "held (no email)"


def is_held(lead: Any) -> bool:
    """True for a lead with no public email: saved, never contacted (email-first). Works on a row or a dict."""
    try:
        email = lead["public_email"]
    except (KeyError, IndexError, TypeError):
        email = None
    return not str(email or "").strip()


def lead_route(public_email: str | None) -> str:
    """'email', or 'held' (no public email: saved, never contacted). The Scout test set scores this."""
    return "email" if (public_email or "").strip() else HELD


# ---------------- is this email address usable? (one rule for Scout, the Guard and the facts check) ----------------

GMAIL_DOMAINS = ("gmail.com", "googlemail.com")
DOMAIN = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}")
LOCAL_PART = re.compile(r"[A-Za-z0-9!#$%&*+/=?^_{|}~.-]+")
QUOTES = "\"'`\u2018\u2019\u201c\u201d"
# A whole address in running text: the part before the @ may hold characters a real address can't (an apostrophe,
# as in Overture's "lead@example.com"), so the WHOLE token is taken and then judged, never a fragment of it.
ADDRESS_IN_TEXT = re.compile(r"(?<![A-Za-z0-9._%+'\u2019-])[A-Za-z0-9._%+'\u2019-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*"
                             r"\.[A-Za-z]{2,}")


def email_problem(address: Any) -> str | None:
    """Why this address can't receive email, in plain words, or None if it looks valid. Plain code: exactly one @,
    a real domain with a dot, no spaces, quotes or apostrophes; for gmail.com and googlemail.com only letters,
    digits and dots before the @ (Gmail allows nothing else). Lead #12's Overture address "lead@example.com"
    fails: it would bounce, and during the warm-up one bounce pauses all outreach."""
    text = str(address or "").strip()
    if not text:
        return "there's no address"
    if any(ch.isspace() for ch in text) or any(ch in QUOTES for ch in text):
        return "it has a space, quote or apostrophe"
    if text.count("@") != 1:
        return "it needs exactly one @"
    local, domain = text.split("@")
    domain = domain.lower()
    if not DOMAIN.fullmatch(domain):
        return "the part after the @ isn't a real domain with a dot"
    if not local or not LOCAL_PART.fullmatch(local) or local.startswith(".") or local.endswith(".") or ".." in local:
        return "the part before the @ isn't a valid name"
    if domain in GMAIL_DOMAINS and not re.fullmatch(r"[A-Za-z0-9.]+", local):
        return "Gmail addresses use only letters, digits and dots before the @"
    return None


def find_emails(text: str | None) -> list[str]:
    """Every email address in some text, each as a WHOLE address (trailing sentence dots dropped)."""
    return [m.group(0).rstrip(".") for m in ADDRESS_IN_TEXT.finditer(text or "")]


def mailto_address(link: str) -> str:
    """The address in a mailto: link, decoded ("mailto:fictional%27s@x.com?subject=Hi" -> "fictional's@x.com")."""
    return unquote(link[7:].split("?")[0]).strip()


def check_url(business_name: str | None, area: str | None) -> str:
    """A Google search link for the business name and town, built in plain code, so Charlie can confirm in one click
    that it really has no website before approving. Nothing from Google is ever fetched or stored."""
    from urllib.parse import quote_plus
    words = " ".join(str(x or "").strip() for x in (business_name, area) if str(x or "").strip())
    return "https://www.google.com/search?q=" + quote_plus(words)


def insert_lead(conn: sqlite3.Connection, content: dict[str, Any], action_id: int | None, saved_by: str,
                why: str | None = None) -> int:
    """Writes one lead row (both ways in: an approved add_lead, or Scout's auto-save). Returns its id."""
    lead = {field: content.get(field) for field in LEAD_FIELDS}
    lead["public_email"] = (lead["public_email"] or "").strip()
    # Worked out again here, so a lead Charlie edited (say, adding an email) gets the right route.
    lead["contact_route"] = contact_route(lead["public_email"])
    if conn.execute("SELECT 1 FROM leads WHERE source_id = ?", (lead["source_id"],)).fetchone():
        raise ValueError(f"{lead['business_name']} is already a lead")
    return int(conn.execute(
        "INSERT INTO leads (business_name, business_type, area, address, phone, website, public_email,"
        " contact_route, source, source_id, source_release, confidence, status, action_id, created_at, saved_by,"
        " check_url, why) VALUES (:business_name, :business_type, :area, :address, :phone, :website, :public_email,"
        " :contact_route, :source, :source_id, :source_release, :confidence, :status, :action_id, :created_at,"
        " :saved_by, :check_url, :why)",
        {**lead, "status": HELD if is_held(lead) else "new", "action_id": action_id, "created_at": utc_now(),
         "saved_by": saved_by, "check_url": check_url(lead["business_name"], lead["area"]), "why": why},
    ).lastrowid)


def save_lead(conn: sqlite3.Connection, action_id: int, content: dict[str, Any]) -> str:
    """The approved add_lead (a lead Charlie added by hand, or an old Scout proposal): saved as Charlie's."""
    insert_lead(conn, content, action_id, "charlie")
    name = content.get("business_name")
    if is_held(content):
        # (Not the word "held" here: Reaper reads that word in a result as "this didn't go through".)
        return f"Lead saved, no public email: {name} (kept on file, never contacted; email-first)"
    return f"Lead saved: {name} (contact route: {contact_route(content.get('public_email'))})"


def lead_handlers() -> dict[str, Handler]:
    return {"add_lead": Handler("add_lead", False, save_lead, lambda c: f"save lead {c.get('business_name')}")}
