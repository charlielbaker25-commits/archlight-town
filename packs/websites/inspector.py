"""The Inspector: grades each saved lead's website and writes a "why they need help" note.

How one inspection works:
1. Plain code decides what kind of web presence the lead has: none, a social or directory
   profile, a free-builder page, or a site of its own.
2. For a site of its own, plain code measures it (inspector_measure.py: Playwright and
   local Lighthouse). Social profiles are never loaded; free-builder pages are loaded only
   for the "before" screenshots.
3. Plain code turns the measured facts into the test set's problem categories and a need
   score from 0 to 100. Build-worthy means the score reaches the threshold in limits.yaml.
4. One small Haiku call writes one plain sentence per measured problem. Code throws away
   anything about a problem that wasn't measured, so the note can't invent one.
5. The inspection is saved (with the screenshots' paths) and logged. Nothing is proposed:
   the Inspector only feeds Builder.

It checks the kill switch before every lead and every fetch, checks the budget before every
lead, and stops for the day at its daily cap (limits.yaml).
"""
import json
import re
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from engine.db import utc_now
from engine.events import log_event
from engine.killswitch import TownStopped
from engine.limits import Limits
from engine.town import Town
from packs.websites.inspector_measure import (DomainNotFound, LiveAccess, MeasureSettings, SiteMeasurer,
                                              UnsafeURL)
from packs.websites.instructions import agent_instructions
from packs.websites.scout import NO_SITE, OWN_SITE, local_day_start_utc, web_presence

AGENT = "inspector"
# The categories, in the order notes list them. They match packs/websites/testset/README.md.
CATEGORIES = ("no_site", "social_or_builder_only", "not_mobile_friendly", "no_https", "slow",
              "outdated", "broken_links")
# How much each problem adds to the need score (capped at 100). A scoring rubric, not a limit.
# no_https is 40 so a "Not secure" site is build-worthy on its own (Charlie, 2026-10-20).
NEED_WEIGHTS = {"no_site": 100, "social_or_builder_only": 85, "not_mobile_friendly": 40, "no_https": 40,
                "slow": 25, "outdated": 30, "broken_links": 25}
# Social and directory profiles are never loaded (their terms, and it tells us nothing new).
# Other "weak" hosts in Scout's list are free-builder pages, loaded once for screenshots.
NO_FETCH_HOSTS = ("facebook.com", "instagram.com", "yelp.com", "google.com", "mapquest.com", "linktr.ee")
KIND_NONE, KIND_SOCIAL, KIND_BUILDER, KIND_OWN = "none", "social", "builder", "own"
LABELS = {"no_site": "No website", "social_or_builder_only": "Only a social or free-builder page",
          "not_mobile_friendly": "Hard to use on a phone", "no_https": "Not secure",
          "slow": "Slow to load", "outdated": "Looks out of date", "broken_links": "Broken links"}
UNREADABLE = "code (Haiku's reply couldn't be read)"

JOB = """You are the Inspector for Example Web Studio, a small St. Louis web-design business. Write the
"why they need help" note for one local business, from facts that plain code measured on its website.

For each problem listed, write one short, plain sentence a busy owner would understand, using only
that problem's facts. Never mention a problem that isn't listed. Never add numbers, names or details
that aren't in the facts, and don't say one fact causes another. Be honest and calm: no hype, no
scare words, no selling. Never claim more than was measured: for "no_site" we only know the listing has no
website, so say "We couldn't find a website listed for <business>.", never "anywhere", "anywhere online" or
"not online at all".

Reply with JSON only, no other text:
{"why": {"<problem>": "<one sentence>"}}"""


class InspectorError(RuntimeError):
    pass


@dataclass(frozen=True)
class InspectorLimits:
    per_day: int
    build_worthy: int
    max_pages: int
    page_timeout_s: float
    lighthouse_timeout_s: float
    slow_below: float
    slow_lcp_s: float
    heavy_kb: float
    outdated_years: int

    @classmethod
    def read(cls, limits: Limits) -> "InspectorLimits":
        get = lambda key: limits.get(f"inspector.{key}")
        return cls(int(get("inspections_per_day")), int(get("build_worthy_score")), int(get("max_pages_per_site")),
                   float(get("page_timeout_seconds")), float(get("lighthouse_timeout_seconds")),
                   float(get("lighthouse_slow_below")), float(get("lighthouse_slow_lcp_seconds")),
                   float(get("heavy_page_kb")), int(get("outdated_after_years")))

    def measure_settings(self, run_lighthouse: bool = True) -> MeasureSettings:
        return MeasureSettings(self.max_pages, self.page_timeout_s, self.lighthouse_timeout_s, run_lighthouse,
                               reads_slow=lambda run: lighthouse_reads_slow(run, self))


@dataclass
class InspectorRun:
    inspected: list[dict[str, Any]] = field(default_factory=list)
    message: str = ""
    cost_usd: float = 0.0


# ---------------- grading (plain code) ----------------

def normalize_url(website: str | None) -> str:
    website = (website or "").strip()
    return website if not website or "://" in website else f"http://{website}"


def site_kind(website: str | None) -> str:
    presence = web_presence(website)
    if presence == NO_SITE:
        return KIND_NONE
    if presence == OWN_SITE:
        return KIND_OWN
    return KIND_SOCIAL if any(host in website.lower() for host in NO_FETCH_HOSTS) else KIND_BUILDER


def lighthouse_reads_slow(run: dict[str, Any], lim: InspectorLimits) -> bool:
    """One Lighthouse run's verdict: performance below the cutoff, or main content later than the cutoff."""
    if run.get("performance") is None:
        return False
    lcp = run.get("lcp_seconds")
    return run["performance"] < lim.slow_below or (lcp is not None and lcp > lim.slow_lcp_s)


def _slow(facts: dict[str, Any], lim: InspectorLimits) -> bool:
    """"Slow" is a claim to a prospect, and real sites vary a lot run to run, so it needs TWO Lighthouse
    runs that both read slow. One slow run alone (say the second run failed) isn't enough."""
    runs = [r for r in facts.get("lighthouse_runs") or [] if r.get("performance") is not None]
    if runs:
        return len(runs) >= 2 and all(lighthouse_reads_slow(r, lim) for r in runs)
    return (facts.get("page_kb") or 0) > lim.heavy_kb   # only when Lighthouse couldn't run at all


def grade(kind: str, facts: dict[str, Any], lim: InspectorLimits, year: int) -> list[str]:
    """The measured facts -> problem categories. No judgment calls: every rule is a measurement."""
    if kind == KIND_NONE or facts.get("domain_not_found") or facts.get("mentions_business") is False:
        return ["no_site"]
    if kind in (KIND_SOCIAL, KIND_BUILDER):
        return ["social_or_builder_only"]
    viewport = facts.get("viewport_tag")
    found = {
        "not_mobile_friendly": viewport is None or "width=device-width" not in viewport.lower()
                               or bool(facts.get("overflows_at_375")),
        "no_https": str(facts.get("final_url", "")).startswith("http://") or bool(facts.get("http_form_actions"))
                    or bool(facts.get("certificate_error")),
        "slow": _slow(facts, lim),
        "outdated": (facts.get("copyright_year") is not None and year - facts["copyright_year"] >= lim.outdated_years)
                    or bool(facts.get("dated_markers")),
        "broken_links": bool(facts.get("broken_links")) or bool(facts.get("broken_resources"))
                        or facts.get("status") in (404, 410),
    }
    return [c for c in CATEGORIES if found.get(c)]


def need_score(problems: list[str]) -> int:
    return min(100, sum(NEED_WEIGHTS[p] for p in problems))


def evidence(problems: list[str], kind: str, facts: dict[str, Any], website: str, year: int) -> dict[str, list[str]]:
    """The measured facts behind each problem, in plain words. Haiku sees only these."""
    lighthouse = facts.get("lighthouse") or {}
    out: dict[str, list[str]] = {}
    for problem in problems:
        if problem == "no_site":
            if facts.get("domain_not_found"):
                out[problem] = [f"the listed website ({website}) doesn't exist"]
            elif facts.get("mentions_business") is False:
                out[problem] = [f"the listed website ({website}) ends up at {facts.get('final_url')}, "
                                "a page that doesn't mention the business"]
            else:
                out[problem] = ["no website is listed anywhere for the business"]
        elif problem == "social_or_builder_only":
            kind_words = "a social or directory profile" if kind == KIND_SOCIAL else "a free website-builder page"
            out[problem] = [f"the only web presence is {kind_words} ({website})"]
        elif problem == "not_mobile_friendly":
            items = []
            if not facts.get("viewport_tag") or "width=device-width" not in str(facts.get("viewport_tag")).lower():
                items.append("the page has no mobile viewport tag, so phones show a shrunken desktop page")
            if facts.get("overflows_at_375"):
                items.append(f"on a 375px-wide phone screen the page is {facts.get('width_at_375')}px wide "
                             "and scrolls sideways")
            out[problem] = items
        elif problem == "no_https":
            items = []
            if str(facts.get("final_url", "")).startswith("http://"):
                items.append("the site loads over plain http, so browsers mark it \"Not secure\"")
            if facts.get("http_form_actions"):
                items.append("a form on the homepage sends what visitors type over plain http")
            if facts.get("certificate_error"):
                items.append("the site's security certificate is broken, so browsers show a warning page")
            out[problem] = items
        elif problem == "slow":
            items = []
            if lighthouse.get("performance") is not None:
                items.append(f"Google's Lighthouse gives the homepage {lighthouse['performance']}/100 for speed on a phone")
                if lighthouse.get("lcp_seconds") is not None:
                    items.append(f"its main content takes about {lighthouse['lcp_seconds']} seconds to appear on a phone")
            # Lighthouse's own byte count when it ran; the browser's rougher count only when it didn't.
            weight = lighthouse.get("total_kb") or facts.get("page_kb")
            items.append(f"the homepage and its files weigh {weight} KB")
            out[problem] = items
        elif problem == "outdated":
            items = []
            if facts.get("copyright_year") is not None and year - facts["copyright_year"] > 0:
                items.append(f"the newest copyright year on the page is {facts['copyright_year']}")
            markers = {"marquee": "scrolling marquee text", "blink": "blinking text", "font": "old <font> tags",
                       "frames": "frames", "flash": "Flash content", "best_viewed_in": "a \"best viewed in\" note"}
            if facts.get("dated_markers"):
                items.append("it uses " + ", ".join(markers.get(m, m) for m in facts["dated_markers"])
                             + ", which went out of style years ago")
            out[problem] = items
        elif problem == "broken_links":
            links = facts.get("broken_links") or []
            files = facts.get("broken_resources") or []
            items = [f"{len(links)} of the homepage links checked go to missing pages ({', '.join(links[:3])})"] if links else []
            if files:
                items.append(f"{len(files)} images or files on the homepage are missing")
            if facts.get("status") in (404, 410):
                items.append(f"the listed homepage itself answers \"not found\" ({facts['status']})")
            out[problem] = items
    return out


# ---------------- the note (Haiku, from measured facts only) ----------------

# Claims bigger than what was measured (Charlie, 2026-11-04). The Inspector only knows the lead lists no website;
# it never searched "everywhere", so "anywhere", "anywhere online" or "not online at all" is more than it knows.
# Plain code drops a sentence that says so and uses its own. The Messenger has the same rule (messenger.py).
OVERCLAIM = re.compile(r"\banywhere\b|\bnowhere\b|\b(?:not|isn'?t|aren'?t) (?:online|on the (?:web|internet))\b|"
                       r"\bno (?:online|web|internet) presence(?: at all)?\b|\bnot (?:anywhere )?online\b|"
                       r"\bdoesn'?t exist online\b|\bcan'?t be found online\b|\bno trace\b", re.IGNORECASE)


def overclaims(text: str) -> str | None:
    match = OVERCLAIM.search(text or "")
    return match.group(0) if match else None


def no_site_sentence(business: str) -> str:
    """What the Town really knows when a lead lists no website: only that."""
    return f"We couldn't find a website listed for {business}."


def code_sentence(problem: str, facts_for_problem: list[str], business: str | None = None) -> str:
    if problem == "no_site" and business:
        return no_site_sentence(business)
    return f"{LABELS[problem]}: {'; '.join(facts_for_problem)}."


def parse_note(text: str, problems: list[str]) -> dict[str, str] | None:
    """Haiku's JSON -> {problem: sentence}, keeping only problems that were measured. None if unreadable."""
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not match:
        return None
    try:
        why = json.loads(match.group(0)).get("why")
    except (json.JSONDecodeError, AttributeError):
        return None
    if not isinstance(why, dict):
        return None
    return {p: str(why[p]).strip() for p in problems if isinstance(why.get(p), str) and why[p].strip()}


def write_note(town: Town, lead: dict[str, Any], problems: list[str],
               facts_by_problem: dict[str, list[str]]) -> tuple[str, str, float]:
    """Returns (note, who wrote it, cost)."""
    if not problems:
        return "No problems found: the site is secure, works on phones, loads quickly and looks current.", "code", 0.0
    town.killswitch.check()
    prompt = json.dumps({"business": lead["business_name"], "type": lead["business_type"], "town": lead["area"],
                         "problems": {p: facts_by_problem[p] for p in problems}}, indent=1)
    reply = town.llm.ask(AGENT, agent_instructions(AGENT, JOB), prompt, max_tokens=600, effort="low")
    sentences = parse_note(reply.text, problems)
    if sentences is None:
        log_event(town.conn, AGENT, "inspector_reply_unreadable", {"lead_id": lead["id"], "reply": reply.text[:500]})
        return (" ".join(code_sentence(p, facts_by_problem[p], lead["business_name"]) for p in problems), UNREADABLE,
                reply.cost_usd)
    # A problem Haiku skipped, or a sentence claiming more than was measured ("anywhere online"), gets the code's
    # own sentence, so every measured problem is in the note and nothing beyond it.
    dropped = {p: s for p, s in sentences.items() if overclaims(s)}
    if dropped:
        log_event(town.conn, AGENT, "inspector_overclaim_dropped", {"lead_id": lead["id"], "sentences": dropped})
    note = " ".join((sentences.get(p) if p not in dropped else None)
                    or code_sentence(p, facts_by_problem[p], lead["business_name"]) for p in problems)
    return note, reply.model, reply.cost_usd


# ---------------- a run ----------------

def inspections_today(town: Town) -> int:
    return town.conn.execute("SELECT COUNT(*) FROM inspections WHERE created_at >= ?",
                             (local_day_start_utc(),)).fetchone()[0]


def _leads_to_inspect(town: Town, lead_id: int | None) -> list[dict[str, Any]]:
    if lead_id is None:
        # Leads never looked at. A failed inspection is retried only by `town inspect <id>`, so a site
        # that blocks us isn't hit again every run.
        # Held leads (no public email: email-first) are never contacted, so a bulk run doesn't spend on them.
        rows = town.conn.execute("SELECT * FROM leads WHERE id NOT IN (SELECT lead_id FROM inspections)"
                                 " AND trim(public_email) != '' ORDER BY id")
        return [dict(r) for r in rows]
    row = town.conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
    if not row:
        raise InspectorError(f"No saved lead #{lead_id}. Leads are saved when you approve Scout's proposals.")
    done = town.conn.execute("SELECT id FROM inspections WHERE lead_id = ? AND status = 'done'", (lead_id,)).fetchone()
    if done:
        raise InspectorError(f"Lead #{lead_id} was already inspected (inspection #{done['id']}).")
    return [dict(row)]


def screenshot_paths(town: Town, lead_id: int) -> dict[str, Path]:
    """In the instance's data folder (never in git): <data>/screenshots/lead-<id>/<time>-desktop.png."""
    folder = Path(town.settings.database).parent / "screenshots" / f"lead-{lead_id}"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return {"desktop": folder / f"{stamp}-desktop.png", "mobile": folder / f"{stamp}-mobile.png"}


def inspect_lead(town: Town, lead: dict[str, Any], lim: InspectorLimits, measurer: Callable[[], Any],
                 year: int) -> dict[str, Any]:
    """Measure, grade, write the note and save one inspection. Returns the saved record."""
    website = normalize_url(lead.get("website"))
    kind = site_kind(website)
    facts: dict[str, Any] = {"site_kind": kind}
    error = None
    shots = screenshot_paths(town, lead["id"])
    if kind == KIND_OWN:
        try:
            identity = {"name": lead["business_name"], "phone": lead.get("phone") or ""}
            facts.update(measurer().measure(website, LiveAccess(), shots, identity))
            error = facts.get("error")
        except DomainNotFound as err:
            facts["domain_not_found"] = str(err)
        except UnsafeURL as err:
            error = str(err)
    elif kind == KIND_BUILDER:
        facts.update(measurer().screenshots_only(website, LiveAccess(), shots))
    record: dict[str, Any] = {"lead_id": lead["id"], "business_name": lead["business_name"], "website": website,
                              "final_url": facts.get("final_url"), "facts": facts, "cost_usd": 0.0, "error": error,
                              "screenshots": facts.get("screenshots") or {}}
    if error:
        record.update(status="failed", problems=[], need_score=None, build_worthy=None, note=None, note_by=None)
    else:
        problems = grade(kind, facts, lim, year)
        score = need_score(problems)
        note, note_by, cost = write_note(town, lead, problems, evidence(problems, kind, facts, website, year))
        record.update(status="done", problems=problems, need_score=score, build_worthy=score >= lim.build_worthy,
                      note=note, note_by=note_by, cost_usd=cost)
    record["id"] = _save(town, record)
    return record


def _save(town: Town, r: dict[str, Any]) -> int:
    inspection_id = town.conn.execute(
        "INSERT INTO inspections (lead_id, website, final_url, status, problems, need_score, build_worthy, note,"
        " note_by, cost_usd, facts, desktop_screenshot, mobile_screenshot, error, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (r["lead_id"], r["website"], r["final_url"], r["status"], json.dumps(r["problems"]), r["need_score"],
         None if r["build_worthy"] is None else int(r["build_worthy"]), r["note"], r["note_by"], r["cost_usd"],
         json.dumps(r["facts"], default=str), r["screenshots"].get("desktop"), r["screenshots"].get("mobile"),
         r["error"], utc_now()),
    ).lastrowid
    if r["status"] == "done":
        log_event(town.conn, AGENT, "inspection_done", {
            "inspection": inspection_id, "lead_id": r["lead_id"], "website": r["website"], "problems": r["problems"],
            "need_score": r["need_score"], "build_worthy": r["build_worthy"], "model": r["note_by"],
            "cost_usd": round(r["cost_usd"], 6)})
    else:
        log_event(town.conn, AGENT, "inspection_failed", {"inspection": inspection_id, "lead_id": r["lead_id"],
                                                          "website": r["website"], "error": r["error"]})
    return int(inspection_id)


def run_inspector(town: Town, lead_id: int | None = None,
                  open_measurer: Callable[[InspectorLimits, Town], Any] | None = None) -> InspectorRun:
    """`town inspect` (every lead not yet looked at, up to the daily cap) or `town inspect <lead id>`.
    Logs a start and exactly one finish (or failure), so the village map can show the Inspector working."""
    town.killswitch.check()
    log_event(town.conn, AGENT, "inspector_started", {"lead_id": lead_id})
    try:
        run = _inspect(town, lead_id, open_measurer)
    except TownStopped:
        log_event(town.conn, AGENT, "inspector_finished", {"message": "stopped by the kill switch"})
        raise
    except InspectorError as err:   # a rule said no (no such lead, already inspected): refused, not broken
        log_event(town.conn, AGENT, "inspector_refused", {"why": str(err)[:300]})
        raise
    except Exception as err:
        log_event(town.conn, AGENT, "inspector_failed", {"error": f"{type(err).__name__}: {err}"[:300]})
        raise
    log_event(town.conn, AGENT, "inspector_finished", {"message": run.message})
    return run


def _inspect(town: Town, lead_id: int | None,
             open_measurer: Callable[[InspectorLimits, Town], Any] | None) -> InspectorRun:
    lim = InspectorLimits.read(town.limits)
    leads = _leads_to_inspect(town, lead_id)
    if not leads:
        return InspectorRun(message="No saved leads are waiting for inspection.")
    remaining = lim.per_day - inspections_today(town)
    if remaining <= 0:
        log_event(town.conn, AGENT, "inspector_daily_cap_reached", {"cap": lim.per_day})
        return InspectorRun(message=f"Daily inspection cap reached ({lim.per_day}). Inspector stands down until tomorrow.")
    open_measurer = open_measurer or (lambda l, t: SiteMeasurer(l.measure_settings(), check=t.killswitch.check))
    run = InspectorRun()
    year = datetime.now().year
    with ExitStack() as stack:
        opened: list[Any] = []

        def measurer() -> Any:
            # The browser only starts if a lead actually has a site to load.
            if not opened:
                opened.append(stack.enter_context(open_measurer(lim, town)))
            return opened[0]

        for lead in leads[:remaining]:
            town.killswitch.check()            # `town stop` mid-run stops before the next lead
            town.budget.check_paid_allowed()   # a budget pause stops the run cleanly, before any half-done record
            record = inspect_lead(town, lead, lim, measurer, year)
            run.inspected.append(record)
            run.cost_usd += record["cost_usd"]
    left = len(leads) - len(run.inspected)
    log_event(town.conn, AGENT, "inspector_run", {"inspected": [r["id"] for r in run.inspected],
                                                  "left_for_tomorrow": left, "cost_usd": round(run.cost_usd, 6)})
    run.message = f"{len(run.inspected)} lead(s) inspected." + (f" {left} wait for tomorrow (daily cap)." if left else "")
    return run
