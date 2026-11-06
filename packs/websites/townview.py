"""The website Town as Reaper and the village map see it: its agents, what each event means, and the counts.

The engine's facts and flows (engine/facts.py, engine/flows.py) know only shapes. This file says, for this
business, which agent an event belongs to, whether it means "started work", "finished" or "error", and which
message it stands for on the map. Everything comes from a real event or row; an event this file doesn't
recognise means nothing (no state change, no flow), never a guess.

Messages go through the Town Hall (the Mayor), as in BLUEPRINT 3.3, with the hand-offs Charlie named for the
map: Builder -> Critic "build ready", Critic -> Builder on a send-back, and Charlie <-> Jarvis at the Orb Tower.
"""
import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from engine.facts import Activity, AgentInfo
from engine.flows import CHARLIE_NODE, Flow, event_dict
from packs.websites import clerk, golive, instructions, librarian, village
from packs.websites.instructions import load_playbook
from packs.websites.leads import check_url
from packs.websites.scout import local_day_start_utc

PLAYBOOKS_DIR = Path(__file__).resolve().parent / "playbooks"
AGENTS = (
    AgentInfo("jarvis", "Jarvis (Reaper)", "Orb Tower"),
    AgentInfo("mayor", "Mayor", "Town Hall"),
    AgentInfo("guard", "Guard", "Gatehouse"),
    AgentInfo("librarian", "Librarian", "Library"),
    AgentInfo("treasurer", "Treasurer", "Bank"),
    AgentInfo("scout", "Scout", "Lookout Tower"),
    AgentInfo("inspector", "Inspector", "Workshop"),
    AgentInfo("builder", "Builder", "Builder's Yard"),
    AgentInfo("critic", "Critic", "Gallery"),
    AgentInfo("messenger", "Messenger", "Post Office"),
    AgentInfo("clerk", "Clerk", "Mailroom"),
)
AGENT_NAMES = frozenset(a.name for a in AGENTS)
MAYOR = "mayor"
JARVIS = "jarvis"

# Plain words for each action type, for labels like "proposed lead #12".
ACTION_NOUNS = {
    "add_lead": "lead", "call_script": "call script", "send_email": "email", "publish_preview": "preview publish",
    "publish_homepage": "homepage publish", "change_limit": "limit change", "raise_cap": "cap raise",
    "resume_paid_work": "paid-work resume", "propose_lesson": "lesson", "retire_lesson": "lesson retirement",
    "playbook_change": "playbook change", "profile_change": "briefing preference",
    "reply_needs_charlie": "reply", "outreach_package": "outreach package",
}
# Orders through Reaper (engine/orders.py): which agent does each one's work, for the map.
ORDER_AGENTS = {"scout": "scout", "inspect": "inspector", "build": "builder", "critique": "critic",
                "draft": "messenger", "propose_publish": "builder", "package": "messenger", "autopilot": "mayor"}
ORDER_EVENTS = frozenset({"order_confirmed", "order_started", "order_finished", "order_failed", "order_cancelled"})
ERROR_EVENTS = {"api_error": "API error", "claude_cli_error": "Claude run failed",
                "claude_cli_timeout": "Claude run timed out", "subscription_limit_hit": "Claude plan usage limit",
                "api_spend_limit_hit": "Console spend limit"}


# How Reaper names each action type when talking to Charlie (plain English, never the code name).
PLAIN_ACTIONS = {
    "add_lead": "new lead", "call_script": "call script", "send_email": "email draft",
    "publish_preview": "preview to publish", "publish_homepage": "homepage to publish", "change_limit": "limit change",
    "raise_cap": "budget cap raise", "resume_paid_work": "paid-work restart",
    "reply_needs_charlie": "reply that needs you", "outreach_package": "outreach package",
}


def plain_action(action_type: str | None) -> str:
    return PLAIN_ACTIONS.get(action_type or "", (action_type or "item").replace("_", " "))


def _noun(action_type: str | None) -> str:
    return ACTION_NOUNS.get(action_type or "", (action_type or "action").replace("_", " "))


def _action(conn: sqlite3.Connection, action_id: int | None) -> sqlite3.Row | None:
    if not action_id:
        return None
    return conn.execute("SELECT id, agent, action_type, content FROM actions WHERE id = ?", (action_id,)).fetchone()


def _int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _subject(d: dict[str, Any]) -> str:
    """What a build or review was for: a lead, Example Web Studio's homepage, or a test-set case."""
    if d.get("lead_id"):
        return f"lead #{d['lead_id']}"
    if d.get("testset_case"):
        return f"test case {d['testset_case']}"
    return "the homepage"


# (agent, event type) -> (state, task). The task's {placeholders} are filled from the event's details.
STATE_EVENTS: dict[tuple[str, str], tuple[str, str]] = {
    ("scout", "scout_started"): ("working", "searching {business_type} in {area}"),
    ("scout", "scout_run"): ("done", "saved {n_saved} lead(s)"),
    ("scout", "scout_daily_cap_reached"): ("done", "daily lead cap reached"),
    ("scout", "scout_skipped"): ("done", "nothing new to propose"),
    ("scout", "scout_failed"): ("error", "run failed: {error}"),
    ("scout", "scout_refused"): ("done", "refused: {why}"),
    ("scout", "scout_finished"): ("done", "{message}"),
    ("inspector", "inspector_started"): ("working", "inspecting leads"),
    ("inspector", "inspector_finished"): ("done", "{message}"),
    ("inspector", "inspection_done"): ("working", "inspected lead #{lead_id}"),
    ("inspector", "inspection_failed"): ("working", "couldn't inspect lead #{lead_id}"),
    ("inspector", "inspector_run"): ("done", "inspected {n_inspected} lead(s)"),
    ("inspector", "inspector_daily_cap_reached"): ("done", "daily inspection cap reached"),
    ("inspector", "inspector_failed"): ("error", "run failed: {error}"),
    ("inspector", "inspector_refused"): ("done", "refused: {why}"),
    ("builder", "build_started"): ("working", "building {subject} (build #{build})"),
    ("builder", "send_back_started"): ("working", "rebuilding after the Critic (build #{build})"),
    ("builder", "build_done"): ("done", "build #{build} built for {subject}"),
    ("builder", "build_failed"): ("error", "build #{build} failed: {failure}"),
    ("builder", "builder_daily_cap_reached"): ("done", "daily build cap reached"),
    ("builder", "preview_published"): ("done", "{kind} published (#{action_id})"),
    ("critic", "review_started"): ("working", "reviewing build #{build}"),
    ("critic", "review_done"): ("done", "build #{build}: {VERDICT}"),
    ("messenger", "draft_written"): ("done", "{draft_kind} for {subject}"),
    ("messenger", "no_draft"): ("done", "no draft for {subject} (rules not met)"),
    ("messenger", "gmail_draft_created"): ("done", "Gmail draft saved, not sent (#{action_id})"),
    ("messenger", "email_sent"): ("done", "email SENT (#{action_id})"),
    ("messenger", "email_send_failed"): ("error", "a send failed, never retried (#{action_id})"),
    ("mayor", "autopilot_cycle_started"): ("working", "autopilot: passing work to the next agent"),
    ("mayor", "autopilot_cycle_finished"): ("done", "{line}"),
    ("clerk", "clerk_started"): ("working", "reading replies on the Town's threads"),
    ("clerk", "clerk_finished"): ("done", "{message}"),
    ("clerk", "clerk_failed"): ("error", "inbox check failed: {error}"),
    ("clerk", "reply_needs_charlie"): ("working", "a {kind} reply needs Charlie (#{action_id})"),
    ("clerk", "reply_opt_out"): ("working", "opt-out honored (lead #{lead_id})"),
    ("clerk", "bounce_recorded"): ("working", "a bounce recorded (lead #{lead_id})"),
    ("guard", "guard_checked"): ("done", "#{action_id}: {verdict}"),
    ("guard", "guard_blocked"): ("done", "BLOCKED #{action_id}"),
    ("treasurer", "budget_warning"): ("done", "80% warning on the {cap} cap"),
    ("treasurer", "paid_work_paused"): ("error", "paid work paused (budget)"),
    ("treasurer", "paid_work_resumed"): ("done", "paid work resumed"),
    ("treasurer", "cost_logged"): ("done", "logged a cost"),
    ("librarian", "backup_created"): ("done", "daily backup saved"),
    ("librarian", "backup_failed"): ("error", "daily backup failed"),
    ("jarvis", "jarvis_turn_started"): ("working", "answering Charlie"),
    ("jarvis", "jarvis_turn"): ("done", "answered Charlie"),
    ("jarvis", "jarvis_turn_failed"): ("error", "couldn't reach the model; answered from plain facts"),
    ("jarvis", "sitrep_delivered"): ("done", "sit-rep delivered"),
    ("jarvis", "reaper_stand_down"): ("done", "stood the Town down on Charlie's order"),
    ("jarvis", "analysis_started"): ("working", "running a deep analysis"),
    ("jarvis", "analysis_done"): ("done", "deep analysis delivered"),
    ("jarvis", "analysis_failed"): ("error", "deep analysis failed"),
    ("mayor", "action_proposed"): ("done", "received #{action_id}"),
    ("mayor", "action_approved"): ("done", "approved #{action_id}"),
    ("mayor", "action_rejected"): ("done", "rejected #{action_id}"),
    ("mayor", "action_edited_and_approved"): ("done", "edited and approved #{action_id}"),
}


class _Blank(dict):
    """Formatting helper: a missing detail shows as '?' instead of raising."""

    def __missing__(self, key: str) -> str:
        return "?"


def _fill(template: str, event: dict[str, Any]) -> str:
    d = event["details"]
    values = _Blank({k: v for k, v in d.items() if isinstance(v, (str, int, float)) and v is not None})
    values.update(action_id=event["action_id"] or "?", subject=_subject(d),
                  VERDICT=str(d.get("verdict") or "?").upper(),
                  draft_kind=str(d.get("kind") or "draft").replace("_", " "),
                  n_proposed=len(d.get("proposed") or []), n_inspected=len(d.get("inspected") or []),
                  n_saved=len(d.get("saved") or d.get("proposed") or []))
    values.setdefault("failure", "")
    return template.format_map(values)


def _business(conn: sqlite3.Connection | None, lead_id: Any, build_id: Any = None) -> str | None:
    """The business a message is about, by its lead (or its build's lead), for the speech bubbles."""
    if conn is None:
        return None
    try:
        if not lead_id and build_id:
            row = conn.execute("SELECT lead_id FROM builds WHERE id = ?", (int(build_id),)).fetchone()
            lead_id = row["lead_id"] if row else None
        if not lead_id:
            return None
        row = conn.execute("SELECT business_name FROM leads WHERE id = ?", (int(lead_id),)).fetchone()
    except (sqlite3.Error, TypeError, ValueError):
        return None
    name = str(row["business_name"] or "").strip() if row else ""
    return name[:40] or None


def _says(conn: sqlite3.Connection | None, event: dict[str, Any], f: Flow) -> str:
    """One short plain-English line for the speech bubble over the sender: what this message says.
    Built only from the event and the database; when there's nothing better, it's the flow's own label."""
    kind, d, aid = event["event_type"], event["details"], event["action_id"]
    name = _business(conn, f.lead_id, f.build_id)
    site = f"the site for {name}" if name else ("the homepage" if not f.lead_id and not d.get("testset_case")
                                                else f"build #{f.build_id or d.get('build') or '?'}")
    if kind == "action_proposed":
        action_type = d.get("action_type")
        if f.to_node == CHARLIE_NODE:
            return f"{_noun(action_type)} #{aid} needs your OK"
        if action_type == "add_lead":   # not saved yet: its name is in the proposal itself
            content = d.get("content") if isinstance(d.get("content"), dict) else {}
            found = str(content.get("business_name") or "").strip()[:40] or name
            return f"found a new lead: {found}" if found else f"found a new lead (#{aid})"
        if action_type == "outreach_package" and name:
            return f"{name}'s site and email are ready for you"
        if action_type in ("call_script", "send_email") and name:
            return f"{_noun(action_type)} for {name} is ready for you"
        if action_type in ("publish_preview", "publish_homepage"):
            return f"{site} is ready to publish"
        return f"sent {_noun(action_type)} #{aid} for your OK"
    if kind == "build_started":
        return f"build {site}"
    if kind == "build_done":
        return f"{site} is ready"
    if kind == "build_failed":
        return f"{site} didn't build"
    if kind == "send_back_started":
        return f"fix {site} and try again"
    if kind == "review_done":
        verdict = str(d.get("verdict") or "").lower()
        return f"passed {site}" if verdict == "pass" else f"{site} needs more work" if verdict else f.label
    if kind == "inspection_done":
        who = name or f"lead #{d.get('lead_id')}"
        score = d.get("need_score")
        return (f"{who} needs a new website (score {score})" if d.get("build_worthy")
                else f"{who} scored {score}, not worth building")
    if kind == "draft_written" and name:
        return f"{'call script' if d.get('kind') == 'call_script' else 'email draft'} for {name} is ready"
    if kind == "preview_published":
        return f"the preview of {site.removeprefix('the ')} is live" if name else f.label
    if kind in ("action_approved", "action_rejected", "action_edited_and_approved"):
        verb = {"action_approved": "approved", "action_rejected": "rejected",
                "action_edited_and_approved": "edited and approved"}[kind]
        return f"Charlie {verb} #{aid}" if f.from_node == JARVIS else f"{verb} #{aid}"
    if kind == "guard_checked" and f.kind == "check":
        return f"checked #{aid}: {'all clear' if d.get('verdict') == 'clear' else d.get('verdict') or 'checked'}"
    if kind in ("guard_checked", "guard_blocked") and f.kind == "block":
        blocks = d.get("blocks") or []
        return f"blocked #{aid}: {str(blocks[0])[:60] if blocks else 'a hard rule'}"
    if kind == "town_stopped":
        return "stand down: everything has stopped"
    if kind == "jarvis_turn":
        return "a question for Reaper" if f.from_node == CHARLIE_NODE else "answered"
    return f.label


class WebsiteTownView:
    agents = AGENTS
    # "Jobs done today" on the village's side board: these events are a finished piece of work.
    job_done_events = frozenset({"scout_finished", "inspection_done", "build_done", "review_done", "draft_written",
                                 "gmail_draft_created", "preview_published", "guard_checked", "guard_blocked",
                                 "email_sent", "clerk_finished",
                                 "execution_finished", "sitrep_delivered", "analysis_done", "backup_created",
                                 "order_finished"})
    # The longest job is a build: building.build_timeout_minutes (30) plus the Critic. Past this with no
    # finish event, the job died without one.
    stale_working_minutes = 60

    # ---------------- agent states ----------------

    def activity(self, conn: sqlite3.Connection, event: dict[str, Any]) -> Activity | None:
        agent, kind = event["agent"], event["event_type"]
        if kind in ERROR_EVENTS and agent in AGENT_NAMES:
            return Activity(agent, "error", ERROR_EVENTS[kind])
        if (agent, kind) in STATE_EVENTS:
            state, template = STATE_EVENTS[(agent, kind)]
            return Activity(agent, state, _fill(template, event))
        if kind == "town_stopped":
            return Activity(MAYOR, "done", "STAND DOWN: all units halted")
        if agent in AGENT_NAMES and kind == "claude_cli_started":
            return Activity(agent, "working", "running Claude")
        if agent in AGENT_NAMES and kind == "claude_cli_stopped":
            return Activity(agent, "done", "stopped by the kill switch")
        if agent == "executor" and kind in ("execution_finished", "execution_failed"):
            ok = kind == "execution_finished"
            return Activity(MAYOR, "done" if ok else "error",
                            f"ran #{event['action_id']}" if ok else f"#{event['action_id']} failed")
        return None

    # ---------------- message flows ----------------

    def flows(self, conn: sqlite3.Connection, event: dict[str, Any]) -> list[Flow]:
        """The map's messages for one event, each with its speech bubble in plain words (`says`)."""
        found = self._flows(conn, event)
        return [replace(f, says=_says(conn, event, f)) for f in found]

    def _flows(self, conn: sqlite3.Connection, event: dict[str, Any]) -> list[Flow]:
        agent, kind, d = event["agent"], event["event_type"], event["details"]
        aid = event["action_id"]

        def flow(src: str, dst: str, k: str, label: str, lead: Any = None, build: Any = None) -> Flow:
            return Flow(event["id"], event["ts"], src, dst, k, label, aid, _int(lead), _int(build))

        if kind == "action_proposed":
            content = d.get("content") if isinstance(d.get("content"), dict) else {}
            lead, noun = content.get("lead_id"), _noun(d.get("action_type"))
            return [flow(d.get("by") or "?", MAYOR, "proposal", f"proposed {noun} #{aid}", lead,
                         content.get("build_id")),
                    flow(MAYOR, CHARLIE_NODE, "waiting", f"{noun} #{aid} waiting for approval", lead)]
        if kind in ("guard_checked", "guard_blocked"):
            if kind == "guard_blocked" or d.get("verdict") == "blocked":
                blocks = d.get("blocks") or []
                return [flow("guard", MAYOR, "block", f"BLOCKED #{aid}: {blocks[0] if blocks else 'hard rule'}")]
            return [flow("guard", MAYOR, "check", f"#{aid} {d.get('verdict', 'checked')}")]
        if kind in ("action_approved", "action_rejected", "action_edited_and_approved"):
            who = d.get("decided_by") or CHARLIE_NODE
            verb = {"action_approved": "approved", "action_rejected": "rejected",
                    "action_edited_and_approved": "edited and approved"}[kind]
            if d.get("via") == "reaper":
                return [flow(JARVIS, MAYOR, "decision", f"{who} {verb} #{aid} (via Reaper)")]
            return [flow(who, MAYOR, "decision", f"{verb} #{aid}")]
        if kind in ("execution_finished", "execution_failed"):
            row = _action(conn, aid)
            if row is None:
                return []
            noun = _noun(row["action_type"])
            if kind == "execution_failed":
                return [flow(MAYOR, row["agent"], "alert", f"{noun} #{aid} FAILED")]
            return [flow(MAYOR, row["agent"], "result", f"{noun} #{aid} done{' (dry run)' if d.get('dry_run') else ''}")]
        if kind == "build_started":
            return [flow(MAYOR, "builder", "work", f"build {_subject(d)}", d.get("lead_id"), d.get("build"))]
        if kind == "build_done":
            return [flow("builder", "critic", "work", f"build #{d.get('build')} ready", d.get("lead_id"),
                         d.get("build"))]
        if kind == "build_failed":
            return [flow("builder", MAYOR, "alert", f"build #{d.get('build')} failed", d.get("lead_id"),
                         d.get("build"))]
        if kind == "send_back_started":
            return [flow("critic", "builder", "work", f"send back build #{d.get('from_build')}", build=d.get("build"))]
        if kind == "review_done":
            return [flow("critic", MAYOR, "result", f"{str(d.get('verdict') or '?').upper()} build #{d.get('build')}",
                         build=d.get("build"))]
        if kind == "inspection_done":
            worthy = ", build-worthy" if d.get("build_worthy") else ""
            return [flow("inspector", MAYOR, "result", f"lead #{d.get('lead_id')} scored {d.get('need_score')}{worthy}",
                         d.get("lead_id"))]
        if kind == "lead_saved":   # Scout saved a passing pick itself (2026-11-05); the Mayor routes it on
            return [flow("scout", MAYOR, "result", f"saved lead #{d.get('lead_id')}", d.get("lead_id"))]
        if kind == "inspection_failed":
            return [flow("inspector", MAYOR, "alert", f"couldn't inspect lead #{d.get('lead_id')}", d.get("lead_id"))]
        if kind == "draft_written" and not d.get("testset_case"):
            draft_action = self._draft_action(conn, d.get("draft"))
            noun = "call script" if d.get("kind") == "call_script" else "email draft"
            label = f"{noun} #{draft_action}" if draft_action else f"{noun} for lead #{d.get('lead_id')}"
            return [flow("messenger", MAYOR, "result", label, d.get("lead_id"))]
        if kind == "gmail_draft_created":
            return [flow("messenger", MAYOR, "result", f"Gmail draft saved, not sent (#{aid})")]
        if kind == "autopilot_held":
            # A daily cap stopped the chain: the courier walks from the agent that handed over to the Town Hall and
            # stops there, with the plain words as its bubble ("Builder: 3 of 3 builds today; ... tomorrow").
            return [flow(d.get("from") or MAYOR, MAYOR, "alert", str(d.get("line") or "held for tomorrow")[:120],
                         (d.get("lead_ids") or [None])[0])]
        if kind == "email_sent":
            return [flow("messenger", "guard", "work", f"email SENT (#{aid})", d.get("lead_id"))]
        if kind == "reply_opt_out":
            return [flow("clerk", "guard", "work", f"opt-out: do-not-contact (lead #{d.get('lead_id')})", d.get("lead_id"))]
        if kind == "bounce_recorded":
            return [flow("clerk", MAYOR, "alert", f"bounce (lead #{d.get('lead_id')})", d.get("lead_id"))]
        if kind == "preview_published":
            return [flow("builder", MAYOR, "result", f"{d.get('kind', 'preview')} live (#{aid})", build=d.get("build"))]
        if kind in ("preview_expired", "preview_taken_down"):
            return [flow("builder", MAYOR, "result", f"preview {d.get('preview')} taken down")]
        if kind == "town_stopped":
            return [flow(JARVIS if d.get("via") == "reaper" else agent, MAYOR, "alert", "STAND DOWN: all units halted")]
        if kind == "town_resumed":
            return [flow(agent, MAYOR, "decision", "resume operations")]
        if kind in ("budget_warning", "paid_work_paused", "paid_work_resumed"):
            label = {"budget_warning": f"80% of the {d.get('cap', '')} cap", "paid_work_paused": "paid work paused",
                     "paid_work_resumed": "paid work resumed"}[kind]
            return [flow("treasurer", MAYOR, "result" if kind == "paid_work_resumed" else "alert", label)]
        if kind in ERROR_EVENTS and agent in AGENT_NAMES and agent != MAYOR:
            return [flow(agent, MAYOR, "alert", ERROR_EVENTS[kind])]
        if kind == "jarvis_turn":
            return [flow(CHARLIE_NODE, JARVIS, "chat", "asked Reaper"),
                    flow(JARVIS, CHARLIE_NODE, "chat", "Reaper answered")]
        if kind == "reaper_decisions_proposed":
            count = sum(len(d.get(k) or []) for k in ("items", "edits", "orders"))   # decisions, edits and orders
            return [flow(JARVIS, CHARLIE_NODE, "waiting", f"{count} to confirm")]
        if kind == "sitrep_delivered":
            return [flow(JARVIS, CHARLIE_NODE, "chat", "morning sit-rep")]
        if kind in ORDER_EVENTS:
            return self._order_flows(kind, d, flow)
        if kind in ("inspector_refused", "scout_refused"):   # a rule said no: a plain answer, not an alert
            return [flow(agent, MAYOR, "result", f"refused: {str(d.get('why') or '')[:70]}")]
        return []

    @staticmethod
    def _order_flows(kind: str, d: dict[str, Any], flow: Callable[..., Flow]) -> list[Flow]:
        """An order Charlie confirmed through Reaper: Jarvis hands it to the Town Hall, the Town Hall hands it to
        the agent that does the work, and the outcome comes back. The agent's own work (proposals, builds,
        reviews) shows as its usual flows."""
        name = str(d.get("order") or "?")
        worker = ORDER_AGENTS.get(name, MAYOR)
        params = d.get("params") if isinstance(d.get("params"), dict) else {}
        what = " ".join([name.replace("_", " ")] + [str(v) for v in params.values()][:3])
        lead, build = params.get("lead_id"), params.get("build_id")
        if kind == "order_confirmed":
            return [flow(CHARLIE_NODE, JARVIS, "decision", f"order: {what}", lead, build),
                    flow(JARVIS, MAYOR, "decision", f"order: {what}", lead, build)]
        if kind == "order_started":
            return [flow(MAYOR, worker, "work", what, lead, build)]
        if kind == "order_finished":
            return [flow(worker, MAYOR, "result", f"{what}: done", lead, build)]
        if kind == "order_failed":
            return [flow(worker, MAYOR, "alert", f"{what}: FAILED", lead, build)]
        return [flow(MAYOR, JARVIS, "result", f"{what}: refused ({str(d.get('why') or '')[:60]})", lead, build)]

    @staticmethod
    def noun(action_type: str | None) -> str:
        return plain_action(action_type)

    def village_layout(self) -> dict[str, Any]:
        return village.village_layout(self.agents)

    @staticmethod
    def _draft_action(conn: sqlite3.Connection, draft_id: Any) -> int | None:
        if not draft_id:
            return None
        row = conn.execute("SELECT action_id FROM drafts WHERE id = ?", (draft_id,)).fetchone()
        return row["action_id"] if row else None

    # ---------------- counts ----------------

    def counts(self, conn: sqlite3.Connection, since: str) -> dict[str, int]:
        """What happened since `since` (UTC). Test-set work never counts (it isn't real business)."""
        def one(sql: str) -> int:
            return int(conn.execute(sql, (since,)).fetchone()[0] or 0)

        return {
            # Found = proposed by hand plus saved by Scout itself (2026-11-05: passing picks save automatically).
            "leads_found": one("SELECT COUNT(*) FROM actions WHERE action_type = 'add_lead' AND created_at >= ?")
            + one("SELECT COUNT(*) FROM leads WHERE saved_by = 'scout' AND created_at >= ?"),
            "leads_saved": one("SELECT COUNT(*) FROM leads WHERE created_at >= ?"),
            "inspected": one("SELECT COUNT(*) FROM inspections WHERE status = 'done' AND created_at >= ?"),
            "built": one("SELECT COUNT(*) FROM builds WHERE status = 'built' AND testset_case IS NULL"
                         " AND created_at >= ?"),
            "critic_passed": one("SELECT COUNT(*) FROM reviews WHERE build_id IS NOT NULL AND verdict = 'pass'"
                                 " AND created_at >= ?"),
            "critic_failed": one("SELECT COUNT(*) FROM reviews WHERE build_id IS NOT NULL AND verdict = 'fail'"
                                 " AND created_at >= ?"),
            "previews_live": int(conn.execute("SELECT COUNT(*) FROM previews WHERE status = 'live'"
                                              " AND kind = 'preview'").fetchone()[0]),
            "emails_drafted": one("SELECT COUNT(*) FROM drafts WHERE status = 'proposed' AND kind IN"
                                  " ('email', 'follow_up') AND created_at >= ?"),
            "call_scripts": one("SELECT COUNT(*) FROM drafts WHERE status = 'proposed' AND kind = 'call_script'"
                                " AND created_at >= ?"),
            "approvals": one("SELECT COUNT(*) FROM events WHERE event_type IN ('action_approved',"
                             " 'action_edited_and_approved') AND ts >= ?"),
            "rejections": one("SELECT COUNT(*) FROM events WHERE event_type = 'action_rejected' AND ts >= ?"),
            # Failed executions, failed builds and inspections, refused drafts and API errors.
            "failures": one("SELECT COUNT(*) FROM actions WHERE status = 'failed' AND executed_at >= ?")
            + one("SELECT COUNT(*) FROM builds WHERE status = 'failed' AND testset_case IS NULL AND created_at >= ?")
            + one("SELECT COUNT(*) FROM inspections WHERE status = 'failed' AND created_at >= ?")
            + one("SELECT COUNT(*) FROM drafts WHERE status = 'no_draft' AND testset_case IS NULL AND created_at >= ?")
            + one("SELECT COUNT(*) FROM events WHERE event_type = 'api_error' AND ts >= ?"),
        }

    # ---------------- one lead, one build, "why" ----------------

    def lead(self, conn: sqlite3.Connection, lead_id: int) -> dict[str, Any] | None:
        row = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
        if row is None:
            return None
        lead = {k: row[k] for k in ("id", "business_name", "business_type", "area", "address", "phone", "website",
                                    "public_email", "contact_route", "status", "created_at")}
        insp = conn.execute("SELECT need_score, build_worthy, problems, note, status, created_at FROM inspections"
                            " WHERE lead_id = ? ORDER BY id DESC LIMIT 1", (lead_id,)).fetchone()
        lead["inspection"] = dict(insp) if insp else None
        lead["builds"] = [dict(r) for r in conn.execute(
            "SELECT id, status, critic, failure, created_at FROM builds WHERE lead_id = ? ORDER BY id", (lead_id,))]
        # The private preview path is never included (Reaper's answers go to a model and to the screen).
        lead["previews"] = [dict(r) for r in conn.execute(
            "SELECT id, build_id, status, published_at, expires_at FROM previews WHERE lead_id = ? ORDER BY id",
            (lead_id,))]
        lead["drafts"] = [dict(r) for r in conn.execute(
            "SELECT id, kind, status, action_id, created_at FROM drafts WHERE lead_id = ? ORDER BY id", (lead_id,))]
        return lead

    def build(self, conn: sqlite3.Connection, build_id: int) -> dict[str, Any] | None:
        row = conn.execute("SELECT * FROM builds WHERE id = ?", (build_id,)).fetchone()
        if row is None:
            return None
        build = {k: row[k] for k in ("id", "lead_id", "testset_case", "kind", "status", "failure", "critic",
                                     "rounds", "wall_seconds", "send_back_of", "created_at", "finished_at")}
        review = conn.execute("SELECT * FROM reviews WHERE build_id = ? ORDER BY id DESC LIMIT 1",
                              (build_id,)).fetchone()
        build["review"] = None
        if review:
            build["review"] = {"id": review["id"], "verdict": review["verdict"], "decided_by": review["decided_by"],
                               "summary": review["summary"]}
            for key in ("scores", "must_fix", "hard_failures", "nice_to_have"):
                try:
                    build["review"][key] = json.loads(review[key] or "null")
                except ValueError:
                    build["review"][key] = review[key]
        return build

    def related_events(self, conn: sqlite3.Connection, action: dict[str, Any]) -> list[dict[str, Any]]:
        """The events behind an action: its own (proposed, Guard, decided, executed), plus the work on its lead
        and build (inspection, build, review, draft), oldest first, at most 40."""
        content = action.get("content") if isinstance(action.get("content"), dict) else {}
        lead_id, build_id = _int(content.get("lead_id")), _int(content.get("build_id"))
        clauses, params = ["action_id = ?"], [action["id"]]
        if lead_id:
            clauses.append("json_extract(details, '$.lead_id') = ?")
            params.append(lead_id)
        if build_id:
            clauses.append("json_extract(details, '$.build') = ?")
            params.append(build_id)
        rows = conn.execute(f"SELECT * FROM events WHERE ({' OR '.join(clauses)}) AND event_type NOT IN"
                            " ('cost_logged', 'queue_viewed', 'claude_cli_started', 'claude_cli_done')"
                            " ORDER BY id DESC LIMIT 40", params).fetchall()
        return [event_dict(r) for r in reversed(rows)]

    # ---------------- today's plan (for the sit-rep) ----------------

    def todays_plan(self, town: Any) -> list[str]:
        """What each agent has waiting for it today, from the database (the Town works when Charlie runs a
        command, so this is "what's ready", not a schedule)."""
        conn, lim, today = town.conn, town.limits, self.day_start_utc()

        def scalar(sql: str, *params: Any) -> int:
            return int(conn.execute(sql, params).fetchone()[0] or 0)

        found = scalar("SELECT COUNT(*) FROM actions WHERE action_type = 'add_lead' AND created_at >= ?", today) + \
            scalar("SELECT COUNT(*) FROM leads WHERE saved_by = 'scout' AND created_at >= ?", today)
        uninspected = scalar("SELECT COUNT(*) FROM leads l WHERE NOT EXISTS (SELECT 1 FROM inspections i"
                             " WHERE i.lead_id = l.id)")
        to_build = scalar("SELECT COUNT(DISTINCT i.lead_id) FROM inspections i WHERE i.build_worthy = 1 AND"
                          " i.status = 'done' AND NOT EXISTS (SELECT 1 FROM builds b WHERE b.lead_id = i.lead_id)")
        built_today = scalar("SELECT COUNT(*) FROM builds WHERE created_at >= ? AND send_back_of IS NULL", today)
        unreviewed = scalar("SELECT COUNT(*) FROM builds WHERE status = 'built' AND critic IS NULL AND"
                            " testset_case IS NULL")
        from packs.websites.package import ready_builds   # here: package imports the agents
        ready = len(ready_builds(town))
        packages = scalar("SELECT COUNT(*) FROM actions WHERE action_type = 'outreach_package' AND status = 'proposed'")
        replies = clerk.replies_needing_charlie(conn)
        calls = len([r for r in replies if r["kind"] == "call_request"])
        state = golive.status(conn, town.settings, lim)
        from packs.websites import autopilot     # here: autopilot imports the agents, which import this view's helpers
        return [
            autopilot.status_line(town),
            f"Clerk: {len(replies)} repl(ies) waiting for you" + (f", {calls} asking for a call (`town draft <lead"
                                                                 f" id>` writes the script)" if calls else "") + ".",
            "Sending: " + ("ON (approved emails are sent)." if state["sending"] else
                           f"off, dry-run (shadow days {len(state['shadow_days'])} of "
                           f"{state['shadow_days_required']}).") ,
            f"Scout: {found} of {lim.get('leads.new_leads_per_day')} new leads today (passing picks save themselves).",
            f"Inspector: {uninspected} saved lead(s) not yet inspected.",
            f"Builder: {to_build} build-worthy lead(s) without a site; {built_today} of "
            f"{lim.get('building.sites_per_day')} builds used today.",
            f"Critic: {unreviewed} built site(s) waiting for review.",
            f"Messenger: {ready} Critic-passed site(s) ready for an outreach package; {packages} package(s) "
            "(site + email) waiting for you.",
        ]

    def day_start_utc(self) -> str:
        return local_day_start_utc()

    # ---------------- the village's extras (R2 draws them) ----------------

    def scoreboard(self, conn: sqlite3.Connection) -> dict[str, Any]:
        """BLUEPRINT 9.2's scoreboard, all time, from the database. Things the Town doesn't track yet are 0 and
        listed in `not_tracked_yet` (never invented). Cost is added by the engine (this month's spend)."""
        def n(sql: str) -> int:
            return int(conn.execute(sql).fetchone()[0] or 0)

        # Every stage counts businesses (saved leads), so the funnel adds up: no stage can exceed the one before.
        # Example Web Studio's own homepage, test-set builds and the Critic's send-back rebuilds aren't businesses.
        return {
            "leads": n("SELECT COUNT(*) FROM leads"),
            "inspected": n("SELECT COUNT(DISTINCT i.lead_id) FROM inspections i JOIN leads l ON l.id = i.lead_id"
                           " WHERE i.status = 'done'"),
            "built": n("SELECT COUNT(DISTINCT b.lead_id) FROM builds b JOIN leads l ON l.id = b.lead_id"
                       " WHERE b.status = 'built' AND b.testset_case IS NULL"),
            "critic_passed": n("SELECT COUNT(DISTINCT b.lead_id) FROM reviews r JOIN builds b ON b.id = r.build_id"
                               " JOIN leads l ON l.id = b.lead_id WHERE r.verdict = 'pass'"
                               " AND b.status = 'built' AND b.testset_case IS NULL"),
            "previews_live": n("SELECT COUNT(DISTINCT p.lead_id) FROM previews p JOIN builds b ON b.id = p.build_id"
                               " JOIN reviews r ON r.build_id = b.id AND r.verdict = 'pass'"
                               " WHERE p.status = 'live' AND p.kind = 'preview'"),
            # Businesses really emailed (go-live's real sends; 0 while sending is off and the Town only saves drafts).
            "contacted": n("SELECT COUNT(DISTINCT lead_id) FROM gmail_drafts WHERE sent_at IS NOT NULL"),
            # Businesses that wrote back (the Clerk; bounces and automatic replies don't count).
            "replies": n("SELECT COUNT(DISTINCT lead_id) FROM inbox_messages WHERE kind NOT IN ('bounce', 'auto_reply')"),
            "clients": 0, "care_plan_subscribers": 0, "revenue_usd": 0.0, "mrr_usd": 0.0,
            "not_tracked_yet": ["clients", "care_plan_subscribers", "revenue_usd", "mrr_usd"],
            "notes": {"contacted": "emails actually sent; drafts only while go-live is off",
                      "gmail_drafts_saved": n("SELECT COUNT(*) FROM gmail_drafts")},
        }

    def sitrep_notices(self, town: Any, since: str) -> list[str]:
        """Lines added to the sit-rep word for word, after any model wording (so they can't be dropped or
        reworded): every automatic opt-out since the last report, with the sender's own words labelled as theirs."""
        return clerk.opt_out_notices(town.conn, since=since)

    def village_extras(self, town: Any) -> dict[str, Any]:
        """The Post Office's go-live checklist (read-only: flipping stays the terminal's `town golive`) and the
        Mailroom's replies waiting for Charlie. Plain code from the database and the state folder."""
        from packs.websites import autopilot
        return {"golive": golive.status(town.conn, town.settings, town.limits), "autopilot": autopilot.status(town),
                "mailroom": {"waiting": clerk.replies_needing_charlie(town.conn)[:12]}}

    def town_iq(self, conn: sqlite3.Connection) -> dict[str, Any]:
        """The Library's 'town IQ' (BLUEPRINT 5.6): the share of the agents' proposals Charlie approved unchanged.
        Counts every decided proposal from an agent (not his own limit changes or the Treasurer's cap requests)."""
        row = conn.execute(
            "SELECT SUM(CASE WHEN status IN ('approved', 'executed', 'failed') AND edited_content IS NULL THEN 1"
            " ELSE 0 END), SUM(CASE WHEN edited_content IS NOT NULL THEN 1 ELSE 0 END),"
            " SUM(CASE WHEN status = 'rejected' THEN 1 ELSE 0 END) FROM actions WHERE status != 'proposed'"
            " AND agent NOT IN ('charlie', 'claude-code', 'treasurer')").fetchone()
        first, edited, rejected = (int(v or 0) for v in row)
        decided = first + edited + rejected
        return {"first_try_approval_rate": round(first / decided, 3) if decided else None,
                "first_try": first, "edited": edited, "rejected": rejected, "decided": decided}

    def analysis_data(self, conn: sqlite3.Connection) -> dict[str, Any]:
        """Everything a deep analysis may use, by plain code: the funnel by business type and by area, the
        Critic's results, Charlie's decisions by action type with the rejection reasons, and the month's spend
        by agent. Test-set work is left out. Business facts only (no private preview paths)."""
        def rows(sql: str) -> list[dict[str, Any]]:
            return [dict(r) for r in conn.execute(sql)]

        funnel = """SELECT l.{col} AS {col}, COUNT(*) AS leads,
                 SUM(EXISTS (SELECT 1 FROM inspections i WHERE i.lead_id = l.id AND i.status = 'done')) AS inspected,
                 SUM(EXISTS (SELECT 1 FROM inspections i WHERE i.lead_id = l.id AND i.build_worthy = 1)) AS build_worthy,
                 SUM(EXISTS (SELECT 1 FROM builds b WHERE b.lead_id = l.id AND b.status = 'built')) AS built,
                 SUM(EXISTS (SELECT 1 FROM builds b WHERE b.lead_id = l.id AND b.critic = 'passed')) AS critic_passed,
                 SUM(EXISTS (SELECT 1 FROM previews p WHERE p.lead_id = l.id AND p.status = 'live')) AS preview_live,
                 SUM(EXISTS (SELECT 1 FROM drafts d WHERE d.lead_id = l.id AND d.status = 'proposed')) AS drafted,
                 SUM(l.contact_route = 'email') AS has_public_email
                 FROM leads l GROUP BY l.{col} ORDER BY leads DESC"""
        return {
            "note": "From the Town's database by plain code. Replies, clients and revenue aren't tracked yet.",
            "funnel_by_business_type": rows(funnel.format(col="business_type")),
            "funnel_by_area": rows(funnel.format(col="area")),
            "proposed_leads_by_decision": rows(
                "SELECT status, COUNT(*) AS n FROM actions WHERE action_type = 'add_lead' GROUP BY status"),
            "critic_reviews": rows("SELECT verdict, decided_by, COUNT(*) AS n FROM reviews WHERE build_id IS NOT NULL"
                                   " GROUP BY verdict, decided_by"),
            "decisions_by_action_type": rows(
                "SELECT action_type, status, COUNT(*) AS n, SUM(edited_content IS NOT NULL) AS edited FROM actions"
                " GROUP BY action_type, status"),
            "rejection_reasons": rows("SELECT action_type, target, reject_reason FROM actions WHERE status ="
                                      " 'rejected' ORDER BY id DESC LIMIT 40"),
            "inspection_problems": rows("SELECT l.business_type, i.need_score, i.problems FROM inspections i JOIN"
                                        " leads l ON l.id = i.lead_id WHERE i.status = 'done'"),
            "spend_this_month_by_agent": rows(
                "SELECT agent, model, COUNT(*) AS calls, ROUND(SUM(dollar_cost), 4) AS usd FROM costs WHERE ts >="
                " strftime('%Y-%m-01T00:00:00Z', 'now') GROUP BY agent, model"),
            "town_iq": self.town_iq(conn),
        }

    def playbook(self, agent: str) -> str | None:
        """An agent's playbook file (BLUEPRINT 5.2), or None if it has none yet."""
        if agent not in AGENT_NAMES:
            return None
        if not (instructions.PLAYBOOKS_DIR / f"{agent}.md").exists():
            return None
        return load_playbook(agent)   # an empty file is "" (it exists, with no lessons yet)

    def lessons(self, conn: sqlite3.Connection, limit: int = 12) -> list[dict[str, Any]]:
        """The Library's lessons for HQ: proposed and approved first (newest first), each with its evidence."""
        rows = librarian.lessons(conn, ("proposed", "approved"))[:limit]
        return [{"id": r["id"], "text": r["text"], "agent": r["agent"], "status": r["status"],
                 "action_id": r["action_id"], "count": r["evidence"].get("count"), "out_of": r["evidence"].get("out_of"),
                 "evidence_ids": r["evidence"].get("ids", [])[:12], "why": r["evidence"].get("why", "")}
                for r in rows]

    def latest_lesson(self, conn: sqlite3.Connection) -> str | None:
        """For the sit-rep: the newest approved lesson, with its evidence count."""
        rows = librarian.lessons(conn, ("approved",))
        if not rows:
            return None
        r = rows[0]
        return f"#{r['id']} for the {r['agent'].title()}: {r['text']} ({r['evidence'].get('count')} of " \
               f"{r['evidence'].get('out_of')})"


VIEW = WebsiteTownView()


# ---------------- HQ's "Needs you" panel (Charlie, 2026-11-04: the page is hard to follow) ----------------

def _local_time(ts: str) -> str:
    """'2026-11-04T20:29:00Z' -> '2:29' on Charlie's clock."""
    from datetime import datetime
    try:
        moment = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone()
    except ValueError:
        return "?"
    return f"{moment.hour % 12 or 12}:{moment.minute:02d}"


def _type_words(business_type: str | None, n: int) -> str:
    words = str(business_type or "business").replace("_", " ").strip()
    if n == 1:
        return words
    return words[:-1] + "ies" if words.endswith("y") else words + ("es" if words.endswith(("s", "sh", "ch")) else "s")


def show_command(conn: sqlite3.Connection, lead_id: Any, build_id: Any) -> str | None:
    """The exact laptop command that opens this site (Charlie, 2026-11-05): `town show <LEAD number>` when this build
    is the lead's newest built site (what that command shows), otherwise `town show --build <build id>`."""
    try:
        if lead_id:
            newest = conn.execute("SELECT id FROM builds WHERE lead_id = ? AND status = 'built' ORDER BY id DESC LIMIT 1",
                                  (int(lead_id),)).fetchone()
            if newest and (not build_id or newest["id"] == int(build_id)):
                return f"town show {int(lead_id)}"
        return f"town show --build {int(build_id)}" if build_id else None
    except (sqlite3.Error, TypeError, ValueError):
        return None


def needs_you(town: Any) -> dict[str, Any]:
    """Every item waiting for Charlie, in plain English, newest first, each with what View shows and whether the
    Guard blocks it right now (plain code, read-only). New leads of one type come as one card ("New leads: 5
    salons"); Approve or Reject on it decides each of them, one by one, through the same confirm path."""
    conn = town.conn
    rows = conn.execute("SELECT * FROM actions WHERE status = 'proposed' ORDER BY id DESC").fetchall()
    items: list[dict[str, Any]] = []
    lead_groups: dict[str, dict[str, Any]] = {}
    for row in rows:
        content = json.loads(row["content"] or "{}")
        verdict = town.mayor.guard_recheck(row)
        blocks = list(verdict.blocks) if verdict and verdict.blocked else []
        kind = row["action_type"]
        business = content.get("business") or content.get("business_name") or _business(
            conn, content.get("lead_id"), content.get("build_id")) or row["target"]
        if kind == "add_lead":
            group = lead_groups.setdefault(str(content.get("business_type") or "business"), {
                "key": f"leads-{content.get('business_type')}", "kind": "new_leads", "ids": [], "leads": [],
                "blocks": [], "newest_id": row["id"]})
            group["ids"].append(row["id"])
            group["blocks"] += [f"#{row['id']}: {b}" for b in blocks]
            group["leads"].append({"id": row["id"], "business_name": content.get("business_name"),
                                   "area": content.get("area"), "address": content.get("address"),
                                   "phone": content.get("phone"), "website": content.get("website"),
                                   "public_email": content.get("public_email"),
                                   "check_url": content.get("check_url") or check_url(content.get("business_name"),
                                                                                       content.get("area")),
                                   "why": row["reason"], "held": bool(content.get("held"))})
            continue
        item = {"key": f"action-{row['id']}", "ids": [row["id"]], "kind": kind, "blocks": blocks,
                "newest_id": row["id"], "business": business}
        show = show_command(conn, content.get("lead_id"), content.get("build_id"))
        view_it = f" View it: {show}" if show else ""
        if kind == "outreach_package":
            email = content.get("email") if isinstance(content.get("email"), dict) else {}
            lead = content.get("lead") if isinstance(content.get("lead"), dict) else {}
            item.update(title=f"Website + email ready: {business}",
                        announce=f"Website and first email for {business} are ready.{view_it}",
                        detail={"type": "package", "build_id": content.get("build_id"), "lead_id": content.get("lead_id"),
                                "show": show,
                                "url": content.get("url"), "critic": content.get("critic_summary"),
                                "expires_after_days": content.get("expires_after_days"),
                                "lead": {**lead, "check_url": lead.get("check_url") or check_url(
                                    lead.get("business_name"), lead.get("area"))},
                                "to": email.get("to"), "from": email.get("from"), "subject": email.get("subject"),
                                "body": email.get("body"),
                                "note": "Approve = publish the site at its private link, check it's live, then the "
                                        "email (a Gmail draft while go-live is off). Reject says which part."})
        elif kind == "publish_preview":
            item.update(title=f"Website ready: {business}", announce=f"Website for {business} is ready.{view_it}",
                        detail={"type": "preview", "url": content.get("url"), "lead_id": content.get("lead_id"),
                                "build_id": content.get("build_id"), "show": show,
                                "note": "Goes live at this private link when you approve."})
        elif kind == "publish_homepage":
            item.update(title="Homepage ready: Example Web Studio", announce=f"Example Web Studio's homepage is ready.{view_it}",
                        detail={"type": "preview", "url": content.get("url"), "build_id": content.get("build_id"),
                                "show": show, "note": "Goes live at showcase.example.com when you approve."})
        elif kind == "send_email":
            follow = content.get("kind") == "follow_up"
            item.update(title=f"{'Follow-up' if follow else 'Email'} ready: {business}",
                        announce=f"{'Follow-up' if follow else 'Email'} to {business} is ready for your approval.",
                        detail={"type": "email", "to": content.get("to"), "from": content.get("from"),
                                "subject": content.get("subject"), "body": content.get("body")})
        elif kind == "reply_needs_charlie":
            call = bool(content.get("call_request"))
            item.update(title=f"Reply from {business}" + (" (asks for a call)" if call else ""),
                        announce=f"{business} replied. It needs you.",
                        detail={"type": "reply", "from": content.get("from"), "subject": content.get("subject"),
                                "words": content.get("reply"), "call_request": call, "lead_id": content.get("lead_id"),
                                "note": (f"For a call script: town draft {content.get('lead_id')}. " if call else "")
                                        + "Answer them yourself; the Town never replies."})
        elif kind == "call_script":
            item.update(title=f"Call script ready: {business}", announce=f"A call script for {business} is ready.",
                        detail={"type": "script", "phone": content.get("phone"), "script": content.get("script")})
        else:
            words = plain_action(kind)
            item.update(title=f"{words[:1].upper()}{words[1:]}: {row['target']}",
                        announce=f"A {words} is waiting for your approval.",
                        detail={"type": "other", "reason": row["reason"], "content": content})
        item["reason"] = row["reason"]
        items.append(item)
    for business_type, group in lead_groups.items():
        n = len(group["ids"])
        group.update(title=f"New leads: {n} {_type_words(business_type, n)}",
                     announce=f"{n} new {_type_words(business_type, n)} {'are' if n > 1 else 'is'} ready for your "
                              "approval.", detail={"type": "leads", "leads": group.pop("leads")}, business=None,
                     reason="Scout's picks: approve the ones to pursue.")
        items.append(group)
    items.sort(key=lambda i: i["newest_id"], reverse=True)
    for item in items:
        item["blocked"] = bool(item["blocks"])
    return {"items": items, "activity": activity_lines(conn), "autopilot": autopilot_view(town)}


def autopilot_view(town: Any) -> dict[str, Any]:
    """Autopilot on or off for HQ's panel (read-only; the Go and Stop buttons do their own thing)."""
    from packs.websites import autopilot     # here: autopilot imports the agents, which import this view's helpers
    try:
        s = autopilot.status(town)
    except Exception:   # never breaks the panel
        return {"on": False, "line": "Autopilot: unknown"}
    return {"on": bool(s["on"] or autopilot.background_running()), "line": autopilot.status_line(town),
            "last": (s["last_cycle"] or {}).get("line")}


def stop_autopilot(town: Any, by: str) -> dict[str, Any]:
    """HQ's Stop button: ends the running loop at its next check. The safe direction (nothing is decided, the
    Town keeps running), so no Confirm. Logged."""
    from packs.websites import autopilot
    from engine.events import log_event
    was_on = autopilot.status(town)["on"] or autopilot.background_running()
    autopilot.request_stop(town.settings, by)
    log_event(town.conn, by, "autopilot_stop_requested", {"via": "hq", "was_on": bool(was_on)})
    return {"ok": True, "reply": "Autopilot stops at its next check (within seconds; a step already running finishes "
                                 "first). Nothing waiting was decided." if was_on else "Autopilot wasn't running."}


# ---------------- HQ's Sites panel: every build, its status in plain words, and a View ----------------

SITE_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css", ".js": "text/javascript",
              ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
              ".avif": "image/avif", ".gif": "image/gif", ".svg": "image/svg+xml", ".ico": "image/x-icon",
              ".woff2": "font/woff2", ".woff": "font/woff", ".txt": "text/plain; charset=utf-8"}
REWRITTEN = (".html", ".css", ".js")
ASTRO_REF = __import__("re").compile(r"(?<![\w/.:-])/_astro/")


def _date(ts: str | None) -> str:
    from datetime import datetime
    try:
        moment = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone()
    except ValueError:
        return str(ts or "?")
    return f"{moment:%b} {moment.day}"


def site_status(conn: sqlite3.Connection, build: sqlite3.Row) -> tuple[str, str, str | None]:
    """(state, plain words, live link) for one build, from the database only."""
    from engine.db import utc_now
    from packs.websites.critic import latest_review
    from packs.websites.previews import SITE_URL, preview_url
    if build["status"] == "running":
        return "building", "building", None
    if build["status"] == "failed":
        return "failed", f"build failed: {(build['failure'] or 'see `town log`')[:160]}", None
    shown = conn.execute("SELECT * FROM previews WHERE build_id = ? ORDER BY id DESC LIMIT 1", (build["id"],)).fetchone()
    if shown and shown["status"] == "live" and (shown["kind"] == "homepage" or (shown["expires_at"] or "") > utc_now()):
        link = SITE_URL + "/" if shown["kind"] == "homepage" else preview_url(shown["token"])
        return "live", ("live at showcase.example.com" if shown["kind"] == "homepage"
                        else f"live until {_date(shown['expires_at'])}"), link
    if shown and shown["status"] in ("live", "expired"):
        return "expired", f"expired {_date(shown['ended_at'] or shown['expires_at'])}", None
    if shown and shown["status"] == "taken_down":
        return "taken_down", f"taken down: {(shown['end_reason'] or '')[:120]}", None
    if shown and shown["status"] == "replaced":
        return "replaced", "replaced by a newer homepage", None
    for row in conn.execute("SELECT action_type, content FROM actions WHERE status = 'proposed' AND action_type IN"
                            " ('outreach_package', 'publish_preview', 'publish_homepage')"):
        if json.loads(row["content"]).get("build_id") == build["id"]:
            return "waiting", "waiting for you" + (" (site + email)" if row["action_type"] == "outreach_package" else ""), None
    review = latest_review(conn, build["id"])
    if build["critic"] == "sent_back":
        newer = conn.execute("SELECT id FROM builds WHERE send_back_of = ? OR send_back_of = (SELECT send_back_of FROM"
                             " builds WHERE id = ?) ORDER BY id DESC LIMIT 1", (build["id"], build["id"])).fetchone()
        return "sent_back", "Critic sent it back" + (f" (fixed in build #{newer['id']})" if newer and newer["id"] != build["id"] else ""), None
    if review and review["verdict"] == "pass":
        return "passed", "Critic passed", None
    if review and review["verdict"] == "fail":
        reasons = json.loads(review["hard_failures"] or "[]") + json.loads(review["must_fix"] or "[]")
        words = "; ".join(str(r.get("problem") if isinstance(r, dict) else r) for r in reasons[:3])
        return "critic_failed", f"Critic failed: {words or review['summary'] or 'see `town show`'}"[:300], None
    return "built", "built, waiting for the Critic", None


def sites(town: Any, limit: int = 60) -> dict[str, Any]:
    """Every real build, newest first: business, lead #, build #, status in plain words, and what View opens (the
    live link when it's published, otherwise the build's own files, served read-only by HQ)."""
    conn = town.conn
    rows = conn.execute("SELECT b.*, l.business_name FROM builds b LEFT JOIN leads l ON l.id = b.lead_id WHERE"
                        " b.testset_case IS NULL ORDER BY b.id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for b in rows:
        state, words, link = site_status(conn, b)
        viewable = b["status"] == "built" and (Path(b["workspace"]) / "dist" / "index.html").is_file()
        out.append({"build_id": b["id"], "lead_id": b["lead_id"], "kind": b["kind"],
                    "business": "Example Web Studio (homepage)" if b["kind"] == "homepage" else (b["business_name"] or "?"),
                    "state": state, "status": words, "live_url": link, "viewable": viewable,
                    "send_back_of": b["send_back_of"], "created": _date(b["created_at"]),
                    "show": show_command(conn, b["lead_id"], b["id"]) if viewable else None})
    return {"sites": out}


def site_file(town: Any, build_id: int, path: str) -> tuple[bytes, str] | None:
    """One file of a real build's dist/ folder for HQ's View, read-only: (bytes, media type) or None. Never a
    directory listing (a folder answers with its index.html or nothing), never outside dist/, never a test-set
    build, only plain site file types. Astro links its files at /_astro/...; HQ serves a build under its own path,
    so those links are rewritten to relative ones in what's SENT (the files on disk never change)."""
    row = town.conn.execute("SELECT workspace, status, testset_case FROM builds WHERE id = ?", (build_id,)).fetchone()
    if not row or row["status"] != "built" or row["testset_case"]:
        return None
    path = path or ""
    if "\\" in path or path.startswith("/") or any(part in ("..", "") for part in path.split("/")[:-1]):
        return None
    if path == "" or path.endswith("/"):
        path += "index.html"
    root = (Path(row["workspace"]) / "dist").resolve()
    target = (root / path).resolve()
    if not target.is_relative_to(root) or not target.is_file() or target.suffix.lower() not in SITE_TYPES:
        return None
    data = target.read_bytes()
    suffix = target.suffix.lower()
    if suffix in REWRITTEN:
        depth = len(target.relative_to(root).parts) - 1
        text = ASTRO_REF.sub("../" * depth + "_astro/", data.decode("utf-8", errors="replace"))
        data = text.encode("utf-8")
    return data, SITE_TYPES[suffix]



# Activity lines about one site, which end with the command that opens it on this laptop.
SITE_EVENTS = frozenset({"build_done", "review_done", "preview_published", "package_proposed"})


def _lead_of(conn: sqlite3.Connection, build_id: Any) -> int | None:
    try:
        row = conn.execute("SELECT lead_id FROM builds WHERE id = ?", (int(build_id),)).fetchone()
    except (sqlite3.Error, TypeError, ValueError):
        return None
    return row["lead_id"] if row else None


def activity_lines(conn: sqlite3.Connection, limit: int = 5) -> list[str]:
    """Plain-English lines from the real log, newest first ("Builder is building Fictional Example Business's site, started
    2:29"). Only events this list knows say anything; nothing is guessed."""
    out: list[str] = []
    rows = conn.execute("SELECT * FROM events WHERE event_type IN ('build_started', 'build_done', 'build_failed',"
                        " 'review_done', 'inspection_done', 'draft_written', 'scout_run', 'clerk_finished',"
                        " 'preview_published', 'email_sent', 'autopilot_cycle_finished', 'gmail_draft_created',"
                        " 'lead_saved', 'package_proposed', 'package_stopped', 'autopilot_held')"
                        " ORDER BY id DESC LIMIT 60").fetchall()
    for row in rows:
        d = json.loads(row["details"] or "{}")
        if d.get("testset_case"):
            continue
        kind, at = row["event_type"], _local_time(row["ts"])
        who = _business(conn, d.get("lead_id"), d.get("build")) or ("the homepage" if d.get("build") else "a lead")
        line = {
            "build_started": f"Builder is building {who}'s site, started {at}",
            "build_done": f"Builder finished {who}'s site at {at}; the Critic reviews it next",
            "build_failed": f"Builder couldn't finish {who}'s site ({at})",
            "inspection_done": f"Inspector checked {who}'s web presence at {at}",
            "draft_written": f"Messenger wrote an email for {who} at {at}: waiting for your OK",
            "scout_run": f"Scout saved {len(d.get('saved') or d.get('proposed') or [])} new lead(s) at {at}",
            "clerk_finished": f"Clerk checked the inbox at {at}: {d.get('message', '')}".rstrip(": "),
            "preview_published": f"{who}'s website went live at its private link at {at}",
            "email_sent": f"Email sent to {who} at {at}",
            "gmail_draft_created": f"An approved email for {who} was saved as a Gmail draft at {at} (not sent)",
            "autopilot_cycle_finished": f"{d.get('line', 'Autopilot cycle done')} ({at})",
            "lead_saved": f"Scout saved {d.get('business_name') or 'a lead'} as a lead at {at}",
            "package_proposed": f"{who}'s site and first email are waiting for your OK ({at})",
            "package_stopped": f"{who}'s outreach package stopped at the {d.get('step', '?')} step ({at})",
            "autopilot_held": f"{d.get('line', 'Held for tomorrow')} ({at})",
        }.get(kind)
        if kind == "review_done":
            line = (f"Critic passed {who}'s site at {at}: waiting for your OK to publish" if d.get("verdict") == "pass"
                    else f"Critic sent {who}'s site back at {at}")
        if line and kind in SITE_EVENTS and (d.get("build") or d.get("lead_id")):
            show = show_command(conn, d.get("lead_id") or _lead_of(conn, d.get("build")), d.get("build"))
            line += f". View on this laptop: {show}" if show else ""
        if line and line not in out:
            out.append(line)
        if len(out) >= limit:
            break
    return out


WebsiteTownView.needs_you = staticmethod(needs_you)
WebsiteTownView.sites = staticmethod(sites)
WebsiteTownView.site_file = staticmethod(site_file)
WebsiteTownView.stop_autopilot = staticmethod(stop_autopilot)


def catch_up(town: Any, since: str) -> dict[str, Any]:
    """What the website Town adds to "catch me up" (engine/catchup.py), all plain code: emails really sent, Gmail
    drafts saved, autopilot cycles, every reply with the sender's own words, problems, and the Needs-you list."""
    conn = town.conn

    def count(sql: str, *params: Any) -> int:
        return int(conn.execute(sql, (since, *params)).fetchone()[0] or 0)

    events = lambda kind: count("SELECT COUNT(*) FROM events WHERE ts >= ? AND event_type = ?", kind)
    lines = [f"Emails: {events('email_sent')} really sent, {events('gmail_draft_created')} saved as Gmail drafts "
             "(not sent).",
             f"Autopilot: {events('autopilot_cycle_finished')} cycle(s)."]
    replies = []
    for r in conn.execute("SELECT i.from_address, i.text, i.kind, l.business_name FROM inbox_messages i LEFT JOIN"
                          " leads l ON l.id = i.lead_id WHERE i.created_at >= ? AND i.kind NOT IN ('bounce',"
                          " 'auto_reply') ORDER BY i.id", (since,)):
        words = " ".join(str(r["text"] or "").split())
        replies.append({"business": r["business_name"] or "unknown business", "from": r["from_address"],
                        "words": words[:200] + ("..." if len(words) > 200 else ""), "kind": r["kind"]})
    lines.append(f"Clerk: {len(replies)} repl(ies).")
    problems = []
    bounces = count("SELECT COUNT(*) FROM inbox_messages WHERE created_at >= ? AND kind = 'bounce'")
    if bounces:
        problems.append(f"{bounces} email(s) bounced.")
    opted = count("SELECT COUNT(*) FROM inbox_messages WHERE created_at >= ? AND kind IN ('opt_out', 'complaint')")
    if opted:
        problems.append(f"{opted} automatic opt-out(s) (`town inbox` lists them; `town dnc remove` if one is wrong).")
    from packs.websites.guard import outreach_pause
    paused = outreach_pause(conn, town.limits)
    if paused:
        problems.append(f"Outreach is paused: {paused}.")
    from engine.claude_cli import subscription_wait_until
    wait = subscription_wait_until(conn)
    if wait:
        problems.append(f"Plan work (builds, drafts) waits for the usage limit until {wait.astimezone():%a %H:%M}.")
    todo = [i["title"] + (" (BLOCKED)" if i["blocked"] else "") for i in needs_you(town)["items"]]
    return {"lines": lines, "replies": replies, "problems": problems, "todo": todo}


WebsiteTownView.catch_up = staticmethod(catch_up)
