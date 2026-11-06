"""The test set's outreach slots: the Messenger (`--agents messenger`, on demand: each draft uses Charlie's plan)
and the Guard (`--agents guard`, plain code, $0).

Messenger: every case worth pursuing (01-08; 09-11 never reach the Messenger) gets a draft from the real
Messenger prompt and checks, written from the case's facts, its answer key's problems (as if the Inspector
measured them) and a fixed fake preview link. Nothing is proposed and nothing can be sent. Plain code scores it:
the right route (email or call script), then for an email the business name in the subject, the word cap, the
preview link, the biggest measured problem named, the opt-out line and address, no dollar amounts and nothing
the facts don't support; for a call script the link offered by text or email (never read aloud), the prices in order (Charlie,
2026-11-02; emails never carry a dollar amount), the problem, the care plan, 3 objections, a voicemail and
the opener's length instead of the subject and word cap. Plus the outreach wording decision (2026-10-26): an
email opens with "Hi there," and has the price clause; every draft says what the preview is (the preview
clause, 2026-11-02); a call opener says why Charlie is calling; nothing says
a person reviews every site or calls us a WashU business. Scored by the rules in messenger.py, never by the
answer keys' good_draft text.

Guard: seeded proposals in testset/guard/cases.yaml, each run in a throwaway database (never the instance's
own), hard rules only. A point when the verdict matches and a blocked case's reason names the rule.
"""
import hashlib
import json
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from engine.db import connect, init_db
from engine.town import Town
from engine.yamlio import load_yaml
from packs.websites.guard import OPT_OUT_LINE, WebsiteGuard, dnc_add, plain
from packs.websites.offer import load_offer
from packs.websites.messenger import (CALL, EMAIL, FROM, DraftInput, MessengerLimits, assemble_email,
                                      biggest_problems, call_text, design_line_problems, draft_route, greeting_ok, has_price_clause, names_problem,
                                      link_problems, opener_problems, preview_clause_missing,
                                      price_problems, unsupported, words, wording_problems,
                                      write_draft)
from packs.websites.previews import preview_url
from packs.websites.schema import PACK_SCHEMAS
from packs.websites.testset_runner import AgentScore, Case, _plain

GUARD_CASES = Path(__file__).resolve().parent / "testset" / "guard" / "cases.yaml"
TESTSET_ADDRESS = "P.O. Box 0000, Testville, MO 00000 (TEST SET ADDRESS)"
# What the Inspector would have measured, in plain words (the test set's fixtures are frozen, so these hold).
MEASURED = {
    "no_site": "No website was found for the business: only a map listing with a phone number.",
    "social_or_builder_only": "The business's only web page is a social profile or a free-builder page.",
    "not_mobile_friendly": "The site has no mobile layout: on a phone the text is tiny and the page scrolls sideways.",
    "no_https": "The site loads over plain http, so browsers mark it \"Not secure\".",
    "outdated": "The site's newest copyright and dates are from 2014.",
    "slow": "The main image takes over 7 seconds to appear on a phone.",
    "broken_links": "Some links and images on the homepage point at pages or files that don't exist.",
}


def testset_preview(case: Case) -> str:
    """A fixed, fake private link per case (never published; the test set sends nothing)."""
    return preview_url(hashlib.sha256(f"testset-{case.name}".encode()).hexdigest())


def asked_for_call(case: Case) -> bool:
    return str(case.answer.get("call_script_when_asked") or "").lower() == "yes"


def messenger_input(case: Case) -> DraftInput | None:
    """None for a case the Messenger never sees (not worth pursuing)."""
    if case.answer.get("worth_pursuing") != "yes" or case.answer.get("contact_route") not in ("email", "held"):
        return None
    problems = case.answer["site_problems"]
    lead = {k: case.info.get(k) for k in ("business_name", "business_type", "area", "address", "phone",
                                          "public_email", "website")}
    # Email-first (2026-11-03): an email lead gets an email; a held case marked call_script_when_asked gets a call
    # script, as if its owner had replied asking for a call (so the script writer is still scored).
    kind = draft_route(lead, asked_for_call(case))
    if kind is None:
        return None
    return DraftInput(kind, lead, list(problems), " ".join(MEASURED[p] for p in problems if p in MEASURED),
                      testset_preview(case), label=f"testset-{case.name}", testset_case=case.name,
                      design_summary=str(case.answer.get("design_summary") or "") if kind == EMAIL else "")


def score_messenger(town: Town, cases: list[Case],
                    on_progress: Callable[[str], None] = lambda message: None) -> AgentScore:
    lim = MessengerLimits.read(town.limits)
    score = AgentScore("messenger", model=town.routing.for_agent("messenger").model)
    used = []
    for case in cases:
        inp = messenger_input(case)
        if inp is None:
            continue
        town.killswitch.check()
        on_progress(f"Messenger test: {case.name} ({inp.kind.replace('_', ' ')}) ...")
        draft = write_draft(town, inp, lim, on_progress)
        expected = EMAIL if case.answer["contact_route"] == "email" else CALL
        score.add("route", inp.kind == expected, f"{case.name}: route {inp.kind}, the key says {expected}")
        if case.answer["contact_route"] == "held":
            # Plain code, free: with no reply asking for a call, a held lead gets no draft at all.
            unasked = draft_route(inp.lead, False)
            score.add("held: nothing unless asked", unasked is None,
                      f"{case.name}: without a call request the route is {unasked or 'none (held)'}")
        _score_draft(score, case, inp, draft, lim)
        used.append(f"{case.name} {draft['wall_seconds']:.0f}s, {draft['tokens']['output_tokens']:,} out")
    score.note = "drafts " + "; ".join(used) + " (Claude plan usage, $0 billed)"
    return score


def _score_draft(score: AgentScore, case: Case, inp: DraftInput, draft: dict[str, Any],
                 lim: MessengerLimits) -> None:
    answer = draft["answer"]
    why = f"{case.name}: no draft ({draft['error'] or '; '.join(draft['problems'][:3])}) (draft #{draft['id']})"
    if inp.kind == EMAIL:
        body = assemble_email(answer["body"], TESTSET_ADDRESS) if answer else ""
        checks = {
            "name in subject": lambda: inp.lead["business_name"].lower() in answer["subject"].lower(),
            f"under {lim.max_email_words + 1} words": lambda: words(answer["body"]) <= lim.max_email_words,
            "preview link": lambda: inp.preview_url in answer["body"],
            "measured problem named": lambda: names_problem(answer["body"], inp.problems),
            "opt-out and address": lambda: plain(OPT_OUT_LINE) in plain(body) and plain(TESTSET_ADDRESS) in plain(body),
            "no dollar amounts": lambda: not [u for u in unsupported(body, inp.lead) if "price" in u],
            "nothing unsupported": lambda: not unsupported(f"{answer['subject']}\n{answer['body']}", inp.lead),
            # The outreach wording decision (2026-10-26): extra plain-code points.
            "opens with Hi there": lambda: greeting_ok(answer["body"]),
            "price clause": lambda: has_price_clause(answer["body"]),
            # Builder v2 (2026-11-02): says what the preview is, confidently.
            "preview clause": lambda: not preview_clause_missing(answer["body"]),
            "outreach wording": lambda: not wording_problems(f"{answer['subject']}\n{answer['body']}"),
            # One personal sentence about THEIR preview's design, only from the Critic's summary (2026-11-04).
            "personal design line": lambda: bool(answer.get("design_line"))
            and not design_line_problems(answer, inp.design_summary),
        }
        text = lambda: f"{answer['subject']} | {answer['body']}"
    else:
        checks = {
            "offers to text or email the link": lambda: not link_problems(answer),
            "measured problem named": lambda: names_problem(answer["problem"], inp.problems),
            "care plan": lambda: "care plan" in answer["care_plan"].lower(),
            "3 objections": lambda: len(answer["objections"]) == 3 and all(o["answer"] for o in answer["objections"]),
            "voicemail": lambda: 0 < words(answer["voicemail"]) <= lim.max_voicemail_words,
            "20-second opener": lambda: 0 < words(answer["opener"]) <= lim.max_opener_words,
            # Charlie's prices (2026-11-02): objection 2 answers "What does it cost?" in order; only our prices.
            "prices in order": lambda: not price_problems(answer),
            "only our prices": lambda: not [u for u in unsupported(call_text(answer), inp.lead,
                                                                   load_offer().amounts()) if "dollar" in u],
            "nothing unsupported": lambda: not unsupported(call_text(answer), inp.lead, load_offer().amounts()),
            "opener says why": lambda: not opener_problems(answer["opener"]),
            "preview clause": lambda: not preview_clause_missing(answer["preview"]),
            "outreach wording": lambda: not wording_problems(call_text(answer)),
        }
        text = lambda: call_text(answer)
    for part, check in checks.items():
        if not answer:
            score.add(part, False, why)
            continue
        ok = check()
        amounts = load_offer().amounts() if inp.kind != EMAIL else frozenset()
        detail = "; ".join(unsupported(text(), inp.lead, amounts)[:2]) if part == "nothing unsupported" else \
            "; ".join(price_problems(answer)[:2]) if part == "prices in order" else \
            "; ".join(wording_problems(text())[:2]) if part == "outreach wording" else \
            ", ".join(biggest_problems(inp.problems)) if part == "measured problem named" else ""
        score.add(part, ok, f"{case.name}: {part} failed{f' ({detail})' if detail else ''} (draft #{draft['id']})")


# ---------------- the Guard (plain code, a throwaway database) ----------------

@dataclass
class _Budget:
    """The budget, as each Guard case sets it up."""
    paused: bool = False

    def paused_reason(self) -> str | None:
        return "test-set pause" if self.paused else None

    def month_spend(self) -> tuple[float, float]:
        return 0.0, 0.0

    def caps(self) -> tuple[float, float]:
        return 9.0, 25.0


def load_guard_cases(path: Path = GUARD_CASES) -> list[dict[str, Any]]:
    return _plain(load_yaml(path))["cases"]


def _sent(conn: Any, lead_id: int, kind: str, days_ago: float) -> None:
    when = (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    action_id = conn.execute("INSERT INTO actions (agent, action_type, target, content, reason, status, created_at,"
                             " executed_at) VALUES ('messenger', 'send_email', 'testset', ?, 'seeded', 'executed', ?, ?)",
                             (json.dumps({"kind": kind, "lead_id": lead_id}), when, when)).lastrowid
    # Really sent (go-live): only a gmail_drafts row with sent_at counts toward a lead's history.
    conn.execute("INSERT INTO gmail_drafts (action_id, lead_id, draft_id, to_address, subject, fake, created_at, sent_at) VALUES (?, ?, 'seeded', 'seeded@example.com', 'seeded', 1, ?, ?)", (action_id, lead_id if conn.execute("SELECT 1 FROM leads WHERE id = ?", (lead_id,)).fetchone() else None,
                  when, when))   # a filler email for another business (the daily cap) has no saved lead


def run_guard_case(town: Town, case: dict[str, Any], lead_case: Case) -> tuple[str, list[str]]:
    """One seeded proposal in a throwaway database. Returns (verdict, block reasons)."""
    setup, proposal = case.get("setup") or {}, dict(case.get("proposal") or {})
    with tempfile.TemporaryDirectory() as folder:
        conn = connect(Path(folder) / "guard-testset.db")
        try:
            init_db(conn, PACK_SCHEMAS)
            info = lead_case.info
            lead_id = conn.execute(
                "INSERT INTO leads (business_name, business_type, area, address, phone, website, public_email,"
                " contact_route, source, source_id, source_release, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'email',"
                " 'overture', ?, 'testset', '2026-10-26T00:00:00Z')",
                (info["business_name"], info["business_type"], info["area"], info["address"], info["phone"],
                 info["website"], info["public_email"], info["source_id"])).lastrowid
            for value in setup.get("dnc") or []:
                dnc_add(conn, value, "test-set case", "testset")
            for sent in setup.get("sent") or []:
                _sent(conn, lead_id, sent["kind"], float(sent["days_ago"]))
            for _ in range(int(setup.get("sent_today") or 0)):
                _sent(conn, 9999, "first", 0)
            address = setup.get("mailing_address", TESTSET_ADDRESS)
            guard = WebsiteGuard(town.limits, _Budget(bool(setup.get("paused"))), None, address,
                                 real=bool(setup.get("real")))
            action_type = proposal.pop("action_type", "send_email")
            cost = float(proposal.pop("cost_estimate_usd", 0) or 0)
            content = _proposal(action_type, lead_id, info, address, proposal)
            verdict = guard.recheck(conn, action_type, info["business_name"], content, cost)
        finally:
            conn.close()
    return verdict.verdict, list(verdict.blocks)


def _proposal(action_type: str, lead_id: int, info: dict[str, Any], address: str,
              changes: dict[str, Any]) -> dict[str, Any]:
    if action_type == "call_script":
        return {"lead_id": lead_id, "for": "charlie", "business": info["business_name"], "phone": info["phone"]}
    if action_type != "send_email":
        return {"lead_id": lead_id, "phone": info["phone"]}
    body = assemble_email(f"Hi {info['business_name']}, we built you a free preview: "
                          f"https://showcase.example.com/p/{'ab' * 32}/", address)
    content = {"kind": "first", "lead_id": lead_id, "from": FROM, "to": info["public_email"],
               "subject": f"A free website preview for {info['business_name']}", "body": body}
    for key, value in changes.items():
        content[key] = value.format(address=address, opt_out=OPT_OUT_LINE) if isinstance(value, str) else value
    return content


def score_guard(town: Town, cases: list[Case]) -> AgentScore:
    lead_case = next(c for c in cases if c.info.get("public_email") and c.answer.get("contact_route") == "email")
    score = AgentScore("guard", model="plain code (hard rules only)")
    for case in load_guard_cases():
        expected = case["expect"]
        verdict, blocks = run_guard_case(town, case, lead_case)
        mentions = str(expected.get("mentions") or "")
        ok = verdict == expected["verdict"] and (not mentions or any(mentions in b for b in blocks))
        score.add("seeded proposals", ok, f"{case['name']}: expected {expected['verdict']}"
                  f"{f' ({mentions})' if mentions else ''}, got {verdict}: {'; '.join(blocks) or 'no block'}")
    return score
