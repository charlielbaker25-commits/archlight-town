"""The website Town's orders through Reaper: the jobs Charlie can start by talking (engine/orders.py).

Each order's check() applies the SAME rules and daily caps as its `town` command, by calling the same gate
code where it exists (the Builder's lead checks, the Messenger's draft gates, the Critic's publish gate), and
changes nothing. run() does exactly what the command does. Publishing, takedowns, resuming, limit and
do-not-contact changes are not here and the engine refuses them by name anyway.
"""
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from engine.orders import Order, OrderRefused
from engine.town import Town
from packs.websites import previews
from packs.websites.builder import BuilderError, BuilderLimits, _subjects, builds_today, run_builder, template_ready
from packs.websites.critic import can_propose_publish, critique_with_send_backs, review_build
from packs.websites.inspector import (InspectorError, InspectorLimits, _leads_to_inspect, inspections_today,
                                      run_inspector)
from packs.websites.messenger import MessengerError, prepare, run_messenger
from packs.websites.package import PackageError, propose_package
from packs.websites.scout import ScoutError, leads_proposed_today, run_scout
from engine.claude_cli import SubscriptionLimitReached


def _int(params: dict[str, Any], key: str) -> int:
    try:
        return int(params[key])
    except (KeyError, TypeError, ValueError):
        raise OrderRefused(f"needs a whole-number {key}") from None


def _text(params: dict[str, Any], key: str, default: str | None = None) -> str:
    value = " ".join(str(params.get(key) or default or "").split())[:60]
    if not value:
        raise OrderRefused(f"needs {key}")
    return value


AFTER_PASS = ("package", "publish", "none")   # what a build order does after a Critic pass (package by default)


@dataclass
class WebsiteOrders:
    names = frozenset({"scout", "inspect", "build", "critique", "draft", "propose_publish", "package", "autopilot"})
    # "go" (Charlie, 2026-11-05): how this Town starts autopilot in the background once he confirms. The launcher
    # sets it only where Charlie may start it (his real Town, run by him, or the dev instance); None = refused.
    start_autopilot: Callable[[], str] | None = None

    def with_autopilot(self, starter: Callable[[], str] | None) -> "WebsiteOrders":
        return WebsiteOrders(start_autopilot=starter)

    # ---------------- checks (change nothing) ----------------

    def check(self, town: Town, order: Order) -> str:
        p = order.params
        if order.name == "scout":
            kind, area = _text(p, "business_type"), _text(p, "area", "any")
            if not Path(town.settings.overture.places_file).exists():
                raise OrderRefused("no Overture data yet (`town overture-download` first)")
            cap = int(town.limits.get("leads.new_leads_per_day"))
            used = leads_proposed_today(town)
            if used >= cap:
                raise OrderRefused(f"the daily lead cap is reached ({used} of {cap})")
            per_run = min(int(town.limits.get("leads.proposals_per_scout_run")), cap - used)
            return (f"Scout: find up to {per_run} {kind} businesses in {area}; the ones that pass every plain-code "
                    f"check are saved as leads ({used} of {cap} today). Nothing leaves the Town.")
        if order.name == "inspect":
            lim = InspectorLimits.read(town.limits)
            used = inspections_today(town)
            if used >= lim.per_day:
                raise OrderRefused(f"the daily inspection cap is reached ({used} of {lim.per_day})")
            if p.get("lead_id") in (None, "", "all"):
                return f"Inspector: inspect every saved lead not yet inspected ({used} of {lim.per_day} today)."
            lead_id = _int(p, "lead_id")
            if not town.conn.execute("SELECT 1 FROM leads WHERE id = ?", (lead_id,)).fetchone():
                raise OrderRefused(f"no saved lead #{lead_id}")
            try:
                _leads_to_inspect(town, lead_id)   # the Inspector's own rule (e.g. already inspected), read-only
            except InspectorError as err:
                raise OrderRefused(str(err)) from None
            return f"Inspector: inspect lead #{lead_id} ({used} of {lim.per_day} today)."
        if order.name == "build":
            lead_id = _int(p, "lead_id")
            if not template_ready():
                raise OrderRefused("the site template isn't installed")
            try:
                subject = _subjects(town, lead_id, False)[0]   # the Builder's own checks (inspected, build-worthy...)
            except BuilderError as err:
                raise OrderRefused(str(err)) from None
            lim = BuilderLimits.read(town.limits)
            used = builds_today(town)
            if used >= lim.per_day:
                raise OrderRefused(f"the daily build cap is reached ({used} of {lim.per_day})")
            after = _after_pass(p)
            then = {"package": "a pass becomes one outreach package for you (the site and the first email)",
                    "publish": "a pass is proposed for publishing", "none": "nothing is proposed yet"}[after]
            return (f"Builder: build a sample site for lead #{lead_id} {subject.lead['business_name']}, then the "
                    f"Critic reviews it, and {then} (Opus on your plan; {used} of {lim.per_day} builds today). "
                    "Publishes nothing.")
        if order.name == "critique":
            build_id = _int(p, "build_id")
            row = town.conn.execute("SELECT status FROM builds WHERE id = ?", (build_id,)).fetchone()
            if not row or row["status"] != "built":
                raise OrderRefused(f"build #{build_id} isn't a finished build")
            return f"Critic: review build #{build_id} (Opus on your plan, read-only)."
        if order.name == "draft":
            lead_id = _int(p, "lead_id")
            follow_up = str(p.get("follow_up", "")).lower() in ("true", "1", "yes")
            try:
                prepare(town, lead_id, follow_up)   # the Messenger's own gates (preview live, Guard follow-up rule)
            except MessengerError as err:
                raise OrderRefused(str(err)) from None
            return (f"Messenger: draft {'a follow-up' if follow_up else 'outreach'} for lead #{lead_id} (Sonnet on "
                    "your plan). It's proposed for your approval; nothing is sent.")
        if order.name == "propose_publish":
            build_id = _int(p, "build_id")
            if not can_propose_publish(town.conn, build_id):
                raise OrderRefused(f"build #{build_id} isn't Critic-passed with unchanged files")
            return f"Propose publishing build #{build_id} (a proposal for your approval; publishes nothing)."
        if order.name == "package":
            build_id = _int(p, "build_id")
            if not can_propose_publish(town.conn, build_id):
                raise OrderRefused(f"build #{build_id} isn't Critic-passed with unchanged files")
            return (f"Messenger: write the first email for build #{build_id}'s site and propose both as one outreach "
                    "package (Sonnet on your plan). Publishes and sends nothing until you approve it.")
        if order.name == "autopilot":
            from packs.websites import autopilot   # here: autopilot imports these orders
            if self.start_autopilot is None:
                raise OrderRefused("autopilot starts from here only for Charlie on his real Town (or the dev "
                                   "instance); use `town autopilot` in a terminal")
            if town.killswitch.is_stopped():
                raise OrderRefused("the Town is stopped; autopilot never runs while it's stopped")
            if autopilot.background_running() or autopilot.status(town)["on"]:
                raise OrderRefused("autopilot is already running")
            minutes = float(town.limits.get("autopilot.cycle_minutes"))
            return (f"Start autopilot: a cycle now, then every {minutes:g} minutes (Scout saves leads, inspect, build "
                    "and Critic, outreach packages, follow-ups, the inbox). It approves nothing: every package waits "
                    "for you. Stop button or stand down ends it.")
        raise OrderRefused(f"unknown order '{order.name}'")

    # ---------------- running (only after Charlie confirms) ----------------

    def run(self, town: Town, order: Order) -> str:
        """Runs the job. When the agent's own rule says no (it raises its rule error), that's a refusal for the
        order, not an error: Charlie sees "refused" and why, and the agent isn't marked as broken."""
        try:
            return self._run(town, order)
        except (InspectorError, ScoutError, PackageError) as err:
            raise OrderRefused(str(err)) from None

    def _run(self, town: Town, order: Order) -> str:
        p = order.params
        if order.name == "scout":
            return run_scout(town, _text(p, "business_type"), _text(p, "area", "any")).message
        if order.name == "inspect":
            lead_id = None if p.get("lead_id") in (None, "", "all") else _int(p, "lead_id")
            return run_inspector(town, lead_id).message
        if order.name == "build":
            return self._build(town, _int(p, "lead_id"), _after_pass(p))
        if order.name == "critique":
            review = review_build(town, _int(p, "build_id"))
            return f"Critic: {str(review.get('verdict', '?')).upper()} build #{p['build_id']}."
        if order.name == "draft":
            follow_up = str(p.get("follow_up", "")).lower() in ("true", "1", "yes")
            return run_messenger(town, _int(p, "lead_id"), follow_up).message
        if order.name == "propose_publish":
            return f"Proposed publishing as #{previews.propose_publish(town, _int(p, 'build_id'))}."
        if order.name == "package":
            return f"Proposed the outreach package as #{propose_package(town, _int(p, 'build_id'))}."
        if order.name == "autopilot":
            if self.start_autopilot is None:
                raise OrderRefused("autopilot can't start from here")
            return self.start_autopilot()
        raise OrderRefused(f"unknown order '{order.name}'")

    @staticmethod
    def _build(town: Town, lead_id: int, after_pass: str = "package") -> str:
        """Exactly `town build <lead>`: build, the Critic with its send-backs, and on a pass the outreach package
        (or, by hand, a publish proposal; autopilot asks for nothing here and packages in its own step)."""
        result = run_builder(town, lead_id)
        notes = [result.message]
        for b in result.builds:
            if not b["facts_check"]:
                continue
            run = critique_with_send_backs(town, b["id"])
            notes.append(f"Critic: {run.outcome or 'unfinished'} (final build #{run.final_build_id}).")
            if run.outcome == "passed" and after_pass != "none":
                notes.append(after_pass_proposal(town, run.final_build_id, after_pass))
        return " ".join(notes) + " Nothing was published."


def _after_pass(params: dict[str, Any]) -> str:
    after = str(params.get("after_pass") or "package").lower()
    if after not in AFTER_PASS:
        raise OrderRefused(f"after_pass must be one of {', '.join(AFTER_PASS)}")
    return after


def after_pass_proposal(town: Town, build_id: int, after_pass: str = "package") -> str:
    """After a Critic pass: the outreach package for a lead's site (Charlie, 2026-11-05), or a publish proposal
    (the homepage always; a preview when asked). The build stands either way; a refusal is reported, not raised."""
    homepage = town.conn.execute("SELECT kind FROM builds WHERE id = ?", (build_id,)).fetchone()["kind"] == "homepage"
    try:
        if after_pass == "package" and not homepage:
            return f"Proposed the outreach package (site + first email) as #{propose_package(town, build_id)}."
        return f"Proposed publishing as #{previews.propose_publish(town, build_id)}."
    except Exception as err:   # same as the command: the build stands, the proposal didn't happen
        if isinstance(err, SubscriptionLimitReached):
            raise
        what = "the outreach package" if after_pass == "package" and not homepage else "publishing"
        return f"Not proposed ({what}): {err}"


ORDERS = WebsiteOrders()
