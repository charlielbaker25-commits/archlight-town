"""The outreach package (Charlie, 2026-11-05): ONE approval per prospect, for the site and the first email together.

Why: Charlie wants to see the site and the email side by side and decide once. Nothing still goes out without his
click; what changes is that the two parts travel as one proposal.

How a package is made (`propose_package`; autopilot calls it, and `town package <build id>` by hand):
1. Only for a lead's build the Critic passed whose files are still exactly the ones it passed (the same publish
   gate as `town propose-publish`: the homepage is live, the lead has no live preview, the layout is right).
2. A private preview path (token) is issued for that build now: the email is written against the link the site
   WILL have. Like every token, it's never reused, even if the package is rejected.
3. The Messenger writes the first email (Sonnet through `claude -p`, its usual plain-code rules, footer by code),
   with every first-email gate (do-not-contact, not held, no first email sent, waiting or already a draft).
4. One `outreach_package` proposal: the build id, its dist hash, the token and link, the Critic's summary, the
   lead's facts with the Check link, and the email (subject, body, To). The Guard checks both parts (publish gate
   and every email rule) when it's proposed, again at approval and again at execution.

What approving does (`execute_package`), in this order, stopping at the first failure and saying so:
1. the kill switch;
2. publish exactly the approved files with the approved token (every publish check again, the dist hash too);
3. fetch the link (plain HTTP, no browser) and check it answers 200 with the noindex header and the approved page;
4. the email through the existing Outbox: a Gmail draft while go-live is off, a real send (every go-live gate,
   checked inside Outbox.send) when it's on.
If the publish fails, no email. If the email fails after publishing, the preview stays up and the result says
exactly that; the email is never re-sent automatically.

Rejecting needs a reason that says which part: "site: ...", "email: ..." or "both: ..." (plain code checks it).
The email part stays editable (`town edit`, before and after saved as usual); the site part can't be edited:
reject it and rebuild.
"""
import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from engine.events import log_event
from engine.executor import Handler
from engine.killswitch import TownStopped
from engine.town import Town
from packs.websites import previews
from packs.websites.cloudflare import CloudflareError
from packs.websites.critic import can_propose_publish, latest_review
from packs.websites.critic_measure import dist_hash
from packs.websites.leads import check_url, email_problem, is_held
from packs.websites.messenger import (MessengerError, MessengerLimits, email_content, package_input, write_draft)
from packs.websites.outreach import OUTREACH_PACKAGE, Outbox
from packs.websites.previews import PublishError, Publisher, preview_url

AGENT = "messenger"          # the Messenger hands the package over (its email is the last piece made)
PACKAGE = OUTREACH_PACKAGE
PARTS = ("site", "email", "both")
EDITABLE_EMAIL_FIELDS = ("subject", "body")   # the email part; To stays the lead's stored email


class PackageError(RuntimeError):
    """A package can't be made (a gate said no). Nothing was proposed."""


class PackageFailed(RuntimeError):
    """An approved package stopped at a step. The message says which step and what did or didn't happen."""


# ---------------- what exists ----------------

def packages_for_build(conn: sqlite3.Connection, build_id: int, statuses: tuple[str, ...] | None = None) -> list[sqlite3.Row]:
    rows = conn.execute("SELECT * FROM actions WHERE action_type = ? ORDER BY id", (PACKAGE,)).fetchall()
    return [r for r in rows if json.loads(r["content"]).get("build_id") == build_id
            and (statuses is None or r["status"] in statuses)]


def email_attempted(conn: sqlite3.Connection, build_id: int) -> bool:
    """Did the Messenger already try a package email for this build (proposed or refused)? Autopilot then leaves
    it to Charlie (`town package <build id>`), so a draft that keeps failing never costs plan usage every cycle."""
    return conn.execute("SELECT 1 FROM drafts WHERE build_id = ? AND kind = 'email' AND testset_case IS NULL",
                        (build_id,)).fetchone() is not None


def ready_builds(town: Town) -> list[int]:
    """Builds autopilot may package now: each lead's newest Critic-passed preview build that never had a package
    (any status: a rejection or a failure is Charlie's to follow up), whose lead isn't held and has no live
    preview, and whose email wasn't tried before. Oldest lead first. Plain code, read-only."""
    conn = town.conn
    found = []
    for lead in conn.execute("SELECT * FROM leads ORDER BY id").fetchall():
        if is_held(lead) or lead["status"] == "bounced" or email_problem(lead["public_email"]):
            continue
        if conn.execute("SELECT 1 FROM previews WHERE lead_id = ? AND status = 'live'", (lead["id"],)).fetchone():
            continue
        newest = conn.execute("SELECT id FROM builds WHERE lead_id = ? AND status = 'built' AND kind = 'preview'"
                              " ORDER BY id DESC LIMIT 1", (lead["id"],)).fetchone()
        if not newest or not can_propose_publish(conn, newest["id"]):
            continue
        if packages_for_build(conn, newest["id"]) or email_attempted(conn, newest["id"]):
            continue
        found.append(newest["id"])
    return found


# ---------------- the publish part's checks (plain code; the Guard runs these) ----------------

def publish_problems(conn: sqlite3.Connection, content: dict[str, Any], action_id: int | None) -> list[str]:
    """Why the site part can't go live as approved (empty = it can). At proposal (action_id None) the token must
    be issued for this build and not yet tied to another action; at approval and execution, to THIS action."""
    build_id = content.get("build_id")
    build = conn.execute("SELECT b.*, l.business_name FROM builds b LEFT JOIN leads l ON l.id = b.lead_id"
                         " WHERE b.id = ?", (build_id,)).fetchone() if isinstance(build_id, int) else None
    if not build:
        return [f"there's no build #{build_id}"]
    problems = []
    if build["kind"] != "preview" or not build["lead_id"]:
        problems.append(f"build #{build_id} isn't a lead's preview")
    if build["lead_id"] != content.get("lead_id"):
        problems.append(f"build #{build_id} belongs to lead #{build['lead_id']}, not lead #{content.get('lead_id')}")
    try:
        previews._gate(conn, build)   # finished, Critic-passed files, layout, homepage live, no live preview yet
    except PublishError as err:
        problems.append(str(err))
    review = latest_review(conn, build_id) or {}
    dist = Path(build["workspace"]) / "dist"
    current = dist_hash(dist) if (dist / "index.html").is_file() else None
    if not content.get("dist_hash") or content.get("dist_hash") != review.get("dist_hash") or current != content.get("dist_hash"):
        problems.append(f"build #{build_id}'s files aren't the ones the Critic passed for this package")
    token = content.get("token")
    issued = conn.execute("SELECT * FROM preview_tokens WHERE token = ?", (str(token),)).fetchone()
    if not (isinstance(token, str) and previews.TOKEN.match(token)) or not issued or issued["build_id"] != build_id:
        problems.append("its private link isn't the one issued for this build")
    elif issued["action_id"] != action_id:
        problems.append("its private link was issued with another proposal")
    elif conn.execute("SELECT 1 FROM previews WHERE token = ?", (token,)).fetchone():
        problems.append("its private link was already used")
    url = preview_url(str(token))
    email = content.get("email") if isinstance(content.get("email"), dict) else {}
    if content.get("url") != url or email.get("preview_url") != url:
        problems.append("the link in the package isn't its own private link")
    elif url not in str(email.get("body") or ""):
        problems.append("the email doesn't contain the site's private link")
    if email.get("lead_id") != content.get("lead_id") or email.get("build_id") != build_id:
        problems.append("the email and the site are for different leads or builds")
    if email.get("kind") != "first":
        problems.append("a package carries a first email only (follow-ups stay separate)")
    return problems


# ---------------- making one (agent work: proposes, publishes and sends nothing) ----------------

def propose_package(town: Town, build_id: int, on_progress: Callable[[str], None] = lambda m: None) -> int:
    """Builds the package for one Critic-passed build and proposes it. Returns the action id. Raises PackageError
    (nothing proposed) when a gate says no or the Messenger couldn't write an email that passes its rules."""
    town.killswitch.check()
    conn = town.conn
    build = previews._build_row(conn, build_id)
    if build["kind"] != "preview" or not build["lead_id"]:
        raise PackageError(f"Build #{build_id} isn't a lead's preview; only those become outreach packages.")
    try:
        previews._gate(conn, build)
    except PublishError as err:
        raise PackageError(str(err)) from None
    if packages_for_build(conn, build_id, ("proposed", "approved")):
        raise PackageError(f"A package for build #{build_id} is already waiting in `town pending`.")
    if previews._waiting_publish(conn, build_id, False):
        raise PackageError(f"A separate publish for build #{build_id} is already waiting; decide that one first.")
    lead_id = int(build["lead_id"])
    try:
        inp = package_input(town, lead_id, build_id, "")   # every first-email gate, before a token is issued
    except MessengerError as err:
        raise PackageError(str(err)) from None
    review = latest_review(conn, build_id) or {}
    token = previews.new_token(conn, build_id)               # reserved now; never reused, even if rejected
    url = preview_url(token)
    inp = replace(inp, preview_url=url)
    draft = write_draft(town, inp, MessengerLimits.read(town.limits), on_progress)
    if not draft["answer"]:
        why = draft["error"] or "; ".join(draft["problems"]) or "no draft"
        raise PackageError(f"The Messenger couldn't write an email for lead #{lead_id} that passes its rules "
                           f"(draft #{draft['id']}: {why}). Nothing was proposed; try `town package {build_id}` again.")
    email = email_content(town, inp, draft)
    lead = dict(inp.lead)
    days = previews.PreviewLimits.read(town.limits).expire_days
    content = {
        "build_id": build_id, "lead_id": lead_id, "business": lead["business_name"],
        "dist_hash": review.get("dist_hash"), "review_id": review.get("id"),
        "critic_summary": review.get("summary"), "token": token, "url": url, "expires_after_days": days,
        "lead": {"business_name": lead["business_name"], "business_type": lead["business_type"],
                 "area": lead["area"], "address": lead["address"], "phone": lead["phone"],
                 "website": lead["website"], "public_email": lead["public_email"],
                 "check_url": lead.get("check_url") or check_url(lead["business_name"], lead["area"]),
                 "why": lead.get("why") or "", "problems": inp.problems, "inspector_note": inp.note},
        "email": email,
    }
    reason = (f"The Critic passed build #{build_id} (review #{review.get('id')}) and the Messenger wrote the first "
              f"email. Approve = publish the site at its private link for {days} days, check it's live, then the "
              "email (a Gmail draft while go-live is off). Reject with a reason starting site:, email: or both:.")
    action_id = town.mayor.propose(AGENT, PACKAGE, f"{lead['business_name']} <{lead['public_email']}>", content, reason)
    conn.execute("UPDATE preview_tokens SET action_id = ? WHERE token = ?", (action_id, token))
    conn.execute("UPDATE drafts SET action_id = ? WHERE id = ?", (action_id, draft["id"]))
    log_event(conn, AGENT, "package_proposed", {"lead_id": lead_id, "build": build_id, "draft": draft["id"]},
              action_id)
    return action_id


# ---------------- approving one (only after Charlie's click) ----------------

def execute_package(pub: Publisher, outbox: Outbox | None, conn: sqlite3.Connection, action_id: int,
                    content: dict[str, Any]) -> str:
    """The four steps in order (module docstring). Any failure raises PackageFailed saying which step, what went
    online and what didn't; the executor marks the action failed with that message (never retried)."""
    url, lead_id, build_id = content.get("url"), content.get("lead_id"), content.get("build_id")
    # 1. the kill switch
    try:
        pub.killswitch.check()
    except TownStopped:
        raise PackageFailed("Stopped at step 1 (kill switch): the Town is stopped. Nothing was published or sent.") from None
    # 2. publish exactly the approved files with the approved token
    problems = publish_problems(conn, content, action_id)
    if problems:
        raise PackageFailed("Stopped at step 2 (publish): " + "; ".join(problems) +
                            ". Nothing went online and no email was sent.")
    build = previews._build_row(conn, int(build_id))
    try:
        _, deployment = previews.publish_site(pub, conn, action_id, build, content.get("token"), content.get("dist_hash"))
    except (PublishError, CloudflareError, TownStopped, OSError) as err:
        raise PackageFailed(f"Stopped at step 2 (publish): {err}. Nothing went online and no email was sent.") from None
    live = conn.execute("SELECT expires_at FROM previews WHERE token = ?", (content.get("token"),)).fetchone()
    until = live["expires_at"] if live else "?"
    fake = "" if pub.client.real else " (FAKE Cloudflare, dev/test: nothing went online)"
    # 3. the link answers with the approved page
    try:
        live_check = previews.check_live(pub, str(url), (Path(build["workspace"]) / "dist" / "index.html").read_bytes())
    except (PublishError, TownStopped, OSError) as err:
        log_event(conn, AGENT, "package_stopped", {"step": "live check", "error": str(err)[:300]}, action_id)
        raise PackageFailed(f"Stopped at step 3 (live check): {err}. The site WAS published at {url}{fake} and stays "
                            f"up until {until} (or `town takedown {build_id} --reason ...`), but no email was "
                            "sent.") from None
    checked = live_check.line
    log_event(conn, AGENT, "package_live_checked", {"route": live_check.route, "url": url}, action_id)   # which way worked
    # 4. the email, through the one Outbox (a draft while go-live is off; a real send, every gate, when it's on)
    email = content.get("email") if isinstance(content.get("email"), dict) else {}
    try:
        if outbox is None:
            raise RuntimeError("no Outbox here (no Gmail client)")
        result = outbox.save_draft(conn, action_id, email) if outbox.live_problems(conn) else \
            outbox.send(conn, action_id, email)
    except Exception as err:
        log_event(conn, AGENT, "package_stopped", {"step": "email", "error": str(err)[:300]}, action_id)
        raise PackageFailed(f"Step 4 (email) failed: {err}. The site IS live at {url}{fake} until {until}. The email "
                            f"was NOT sent and is never re-sent automatically; `town draft {lead_id}` proposes a new "
                            "one when you want it.") from None
    log_event(conn, AGENT, "package_done", {"lead_id": lead_id, "build": build_id, "deployment": deployment,
                                            "email": result[:200]}, action_id)
    return f"Site published at {url}{fake} until {until}; {checked}. Email: {result}"


def describe_package(content: dict[str, Any]) -> str:
    email = content.get("email") if isinstance(content.get("email"), dict) else {}
    return (f"publish build #{content.get('build_id')} ({content.get('business')}) at {content.get('url')}, check "
            f"it's live, then email {email.get('to')}: \"{email.get('subject')}\"")


def package_handlers(pub: Publisher, outbox: Outbox | None) -> dict[str, Handler]:
    run = lambda conn, action_id, content: execute_package(pub, outbox, conn, action_id, content)
    return {PACKAGE: Handler(PACKAGE, True, run, describe_package)}


# ---------------- deciding: which part a rejection is about ----------------

def reject_part(reason: str) -> str | None:
    """'site: too plain' -> 'site'. None if the reason doesn't start with a part."""
    head = str(reason or "").strip().split(":", 1)
    word = head[0].strip().lower() if len(head) == 2 and head[1].strip() else ""
    return word if word in PARTS else None


def reject_reason_problem(action_type: str, reason: str) -> str | None:
    """Plain code: a package's rejection must say which part (and why). Other action types: no extra rule."""
    if action_type != PACKAGE or reject_part(reason):
        return None
    return ("say which part you're rejecting, then why: \"site: ...\", \"email: ...\" or \"both: ...\" "
            "(the site can't be edited: reject it and rebuild; the email can be edited with `town edit`)")


def with_part(part: str, reason: str) -> str:
    part = str(part or "").strip().lower()
    if part not in PARTS:
        raise PackageError(f"the part must be one of {', '.join(PARTS)}")
    return f"{part}: {' '.join(str(reason or '').split())}"


def edit_email(content: dict[str, Any], field: str, value: Any) -> dict[str, Any]:
    """The edited package: only the email part's subject or body may change (the site part is fixed)."""
    if field not in EDITABLE_EMAIL_FIELDS:
        raise PackageError(f"only the email's {' or '.join(EDITABLE_EMAIL_FIELDS)} can be edited in a package "
                           "(the site part can't: reject it with \"site: ...\" and rebuild)")
    edited = json.loads(json.dumps(content))
    edited["email"][field] = str(value)
    return edited
