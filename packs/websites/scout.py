"""Scout v0: finds local businesses in the Overture file and proposes the best few as leads.

How one run works:
1. Plain code (free and exact) pulls matching businesses from the local Overture file:
   right business type and area, confident listing, reachable by phone or email,
   and not already a lead, already proposed, or already rejected by Charlie.
   Businesses with no website come first, then ones whose "website" is only a
   free-builder or social page, then everyone else.
2. One cheap Haiku call picks the best few from that shortlist and writes a
   one-line reason for each.
3. Leads save automatically (Charlie, 2026-11-05): a pick that passes EVERY plain-code check in
   auto_save_problems() (a public email, no flags, a local phone, no website listed or only a social or
   free-builder page, not on the do-not-contact list, never proposed, saved or contacted before) is saved
   as a lead with no approval, and logged as saved by Scout. Anything else is never saved: a pick with no
   public email is logged as held, any other miss as dropped, each with its reasons. Saving a lead touches
   nothing outside the Town; Charlie's one approval comes later, on the outreach package (site + email).
   The `add_lead` approval path stays for leads Charlie adds by hand.

Email-first (Charlie, 2026-11-03): candidates with a public email come first in the shortlist and Haiku is told
to prefer them.

Scout checks the kill switch before every step and stops for the day at the
daily lead cap. Both numbers come from limits.yaml.
"""
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb

from engine.events import log_event
from engine.killswitch import TownStopped
from engine.llm import Reply
from engine.town import Town
from engine.yamlio import load_yaml
from packs.websites.instructions import agent_instructions
from packs.websites.leads import check_url, contact_route, email_problem, insert_lead

AGENT = "scout"
# The code shortlist is this many times the number of leads wanted, so Haiku has a real choice.
SHORTLIST_PER_PICK = 4
# How many matching rows code looks at before sorting them by web presence.
SCAN_ROWS = 400
# Hosts that usually mean "no real website": free builders, directory pages and social profiles.
WEAK_WEB_HOSTS = ("facebook.com", "instagram.com", "square.site", "wixsite.com", "godaddysites.com",
                  "business.site", "hub.biz", "yelp.com", "linktr.ee", "weebly.com", "blogspot.com",
                  "google.com", "mapquest.com")
NO_SITE = "none"
WEAK_SITE = "free builder, directory or social page only"
OWN_SITE = "has its own site"
UNREADABLE_PREFIX = "Ranked by code"
# St. Louis-area phone area codes (314 city and inner county, 636 outer county, 618 Metro East).
# Any other area code gets the out_of_area_phone flag: a hint for Haiku and Charlie, never an automatic reject.
LOCAL_AREA_CODES = ("314", "636", "618")
OUT_OF_AREA_PHONE = "out_of_area_phone"
# A chain or franchise location: its Overture brand differs from its own name, or the brand is on 2+
# places in the region, or its name carries a store number ("QuickTurn Oil & Lube #214", "Store 52").
# Such places usually use the corporate website and can't buy their own. Overture often lists a lone
# business's own name as its brand, so that alone isn't a chain. Like the phone flag, it's a hint for
# Haiku and Charlie, never an automatic reject.
CHAIN_LOCATION = "chain_location"
STORE_NUMBER = re.compile(r"#\s*\d+|\bstore\s*(?:no\.?|#)?\s*\d+", re.IGNORECASE)
# Leads plain code drops before Haiku sees them (logged as skipped), because they can't buy a site:
# - a captive agent (State Farm, Edward Jones...): the corporate office controls their website.
# - the lead-gen listing pattern: an out-of-area phone AND no street address. One of the two alone
#   still goes to Haiku and Charlie, since some real local owners have an out-of-state cell.
CAPTIVE_NETWORKS_FILE = Path(__file__).resolve().parent / "captive_networks.yaml"
SKIP_CAPTIVE = "captive_agent"
SKIP_LEAD_GEN = "lead_gen_listing"
# A street address has a house number followed by a word ("7340 Manchester Rd"). P.O. boxes don't count.
STREET_NUMBER = re.compile(r"\b\d+[a-z]?\s+[a-z]", re.IGNORECASE)
PO_BOX = re.compile(r"\bp\.?\s*o\.?\s*box\b", re.IGNORECASE)

JOB = """You are Scout for a small St. Louis web-design business that builds modern websites for local
businesses and offers a monthly care plan. From the candidate businesses, pick the {n} best prospects.

The business contacts prospects by EMAIL ONLY. Strongly prefer candidates with has_public_email true: a
business without a public email is saved as "held" and never contacted, so pick one only when no reasonable
candidate with an email is left.

Good prospects: independent local businesses (not national chains, franchises, banks, government
offices or big institutions) that have no website, or only a free-builder, directory or social page,
and that can be reached by phone or email. Use only the facts given; don't invent anything.

Each candidate may carry flags from plain code. "out_of_area_phone" means the phone's area code
isn't a St. Louis one ({local_codes}). That's often a call center or lead-generation listing rather
than a local owner, but some real local owners use an out-of-state cell number, so weigh it with
the other facts (name, address, web presence) instead of rejecting on the flag alone.
"chain_location" means Overture lists a chain or franchise brand for the place (shown as "brand"),
or its name has a store number like "#214". Those are usually franchise or chain locations that use the corporate
website and can't buy a site of their own, so they're rarely good prospects. Weigh it with the other
facts too: a local owner can share a name with a brand by coincidence.

Reply with JSON only, no other text:
{{"picks": [{{"source_id": "<id from the list>", "why": "<one short plain sentence>"}}]}}
Pick exactly {n} unless fewer are reasonable prospects."""


class ScoutError(RuntimeError):
    pass


@dataclass
class ScoutRun:
    saved: list[int] = field(default_factory=list)          # lead ids Scout saved itself (every check passed)
    not_saved: list[dict[str, Any]] = field(default_factory=list)   # held or dropped picks, with their reasons
    message: str = ""
    cost_usd: float = 0.0
    model: str = ""


def normalize_area(text: str) -> str:
    """'Saint Louis', 'St. Louis' and 'st louis' all become 'st louis'."""
    text = re.sub(r"\.", "", (text or "").strip().lower())
    return re.sub(r"^saint ", "st ", text)


def web_presence(website: str | None) -> str:
    if not (website or "").strip():
        return NO_SITE
    if any(host in website.lower() for host in WEAK_WEB_HOSTS):
        return WEAK_SITE
    return OWN_SITE


def phone_area_code(phone: str | None) -> str | None:
    """'+1 (646) 555-0110', '+16465550110' and '646-555-0110' all give '646'. None if it isn't a US number."""
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits[:3] if len(digits) == 10 else None


def _plain_name(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def chain_brand(place: dict[str, Any]) -> str | None:
    """The brand, but only when it marks a chain or franchise (not a lone business's own name)."""
    brand = (place.get("brand") or "").strip()
    if not brand:
        return None
    if _plain_name(brand) != _plain_name(place.get("name")) or (place.get("brand_locations") or 0) >= 2:
        return brand
    return None


def is_chain_location(place: dict[str, Any]) -> bool:
    return chain_brand(place) is not None or bool(STORE_NUMBER.search(place.get("name") or ""))


def lead_flags(place: dict[str, Any]) -> list[str]:
    """Plain-code warnings shown to Haiku and on the proposal. They inform; they never reject."""
    flags = []
    area_code = phone_area_code(place.get("phone"))
    if area_code and area_code not in LOCAL_AREA_CODES:
        flags.append(OUT_OF_AREA_PHONE)
    if is_chain_location(place):
        flags.append(CHAIN_LOCATION)
    return flags


def captive_networks(path: Path = CAPTIVE_NETWORKS_FILE) -> list[str]:
    return [str(n).strip() for n in (load_yaml(path).get("networks") or []) if str(n).strip()]


def _contains_words(text: str, phrase: str) -> bool:
    """Whole words, ignoring case: 'State Farm' matches 'Jake Molitor - State Farm Agent', not 'Statefarmer'."""
    return re.search(rf"(?<![a-z0-9]){re.escape(phrase.lower())}(?![a-z0-9])", (text or "").lower()) is not None


def has_street_address(address: str | None) -> bool:
    return bool(STREET_NUMBER.search(address or "")) and not PO_BOX.search(address or "")


def skip_reason(place: dict[str, Any], networks: list[str]) -> str | None:
    """Why plain code drops this lead before Haiku sees it, or None to keep it."""
    for network in networks:
        if _contains_words(place.get("name") or "", network) or _contains_words(place.get("brand") or "", network):
            return f"{SKIP_CAPTIVE}: {network} (the corporate office controls their website)"
    if OUT_OF_AREA_PHONE in lead_flags(place) and not has_street_address(place.get("address")):
        return f"{SKIP_LEAD_GEN}: out-of-area phone and no street address"
    return None


def screen_candidates(ranked: list[dict[str, Any]], limit: int | None = None,
                      networks: list[str] | None = None) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Split ranked candidates into (kept, skipped), stopping once `limit` are kept. Only skips ranked
    among the kept ones are returned: one further down would never have reached Haiku anyway.
    `town testset` uses this too, so the test set scores the same rule."""
    networks = captive_networks() if networks is None else networks
    kept: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for place in ranked:
        if limit is not None and len(kept) == limit:
            break
        reason = skip_reason(place, networks)
        if reason:
            skipped.append({"source_id": place["source_id"], "name": place.get("name") or "", "reason": reason})
        else:
            kept.append(place)
    return kept, skipped


def local_day_start_utc(now: datetime | None = None) -> str:
    """Midnight on Charlie's local calendar, in the UTC format the database uses."""
    now = (now or datetime.now()).astimezone()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def leads_proposed_today(town: Town) -> int:
    """Today's leads against the daily cap: add_lead proposals plus the leads Scout saved itself."""
    since = local_day_start_utc()
    proposed = town.conn.execute("SELECT COUNT(*) FROM actions WHERE action_type = 'add_lead' AND created_at >= ?",
                                 (since,)).fetchone()[0]
    saved = town.conn.execute("SELECT COUNT(*) FROM leads WHERE saved_by = 'scout' AND created_at >= ?",
                              (since,)).fetchone()[0]
    return int(proposed) + int(saved)


def _seen_before(conn: Any, content: dict[str, Any]) -> str | None:
    """Was this business (by email, phone or name) ever proposed, saved or contacted? Plain code."""
    from packs.websites.guard import norm_business, norm_email, norm_phone   # guard imports this module
    email, phone, name = (norm_email(content.get("public_email")), norm_phone(content.get("phone")),
                          norm_business(content.get("business_name")))
    known = [dict(r) for r in conn.execute("SELECT business_name, public_email, phone FROM leads")]
    known += [json.loads(r["content"]) for r in conn.execute("SELECT content FROM actions WHERE action_type IN"
                                                             " ('add_lead', 'send_email', 'outreach_package')")]
    for other in known:
        other = other.get("email") if isinstance(other.get("email"), dict) else other
        if email and email in (norm_email(other.get("public_email")), norm_email(other.get("to"))):
            return f"its email {email} was already proposed, saved or contacted"
        if phone and phone == norm_phone(other.get("phone")):
            return "its phone number was already proposed or saved"
        if name and name in (norm_business(other.get("business_name")), norm_business(other.get("business"))):
            return "a business with this name was already proposed, saved or contacted"
    return None


def auto_save_problems(town: Town, place: dict[str, Any], content: dict[str, Any]) -> list[str]:
    """Why this pick can't be saved without Charlie (empty = it saves itself). Every check is plain code; the
    Guard's do-not-contact check is the same function the Guard uses (dnc_matches)."""
    from packs.websites.guard import dnc_matches      # guard imports this module
    problems = []
    if content["held"]:
        problems.append("no public email (email-first: held, never saved or contacted)")
    elif email_problem(content["public_email"]):   # it would bounce, and one bounce pauses all outreach in warm-up
        problems.append(f"email address looks invalid: {content['public_email']} ({email_problem(content['public_email'])})")
    if content["flags"]:
        problems.append(f"flagged: {', '.join(content['flags'])}")
    if phone_area_code(place.get("phone")) not in LOCAL_AREA_CODES:
        problems.append("no local St. Louis phone number")
    if web_presence(place.get("website")) == OWN_SITE:
        problems.append("it lists a website of its own")
    problems += dnc_matches(town.conn, {k: content.get(k) for k in ("business_name", "phone", "public_email")})
    if place["source_id"] in known_source_ids(town):
        problems.append("it was already proposed or saved")
    else:
        seen = _seen_before(town.conn, content)
        if seen:
            problems.append(seen)
    return problems


def known_source_ids(town: Town) -> list[str]:
    """Everything Scout must skip: saved leads, anything already proposed or rejected, and every pick it already
    looked at and didn't save (held or dropped), so a pick that can't be saved never takes a slot again."""
    saved = [r[0] for r in town.conn.execute("SELECT source_id FROM leads")]
    proposed = [r[0] for r in town.conn.execute(
        "SELECT json_extract(content, '$.source_id') FROM actions WHERE action_type = 'add_lead'")]
    passed_over = [p.get("source_id") for r in town.conn.execute(
        "SELECT details FROM events WHERE event_type = 'scout_not_saved'") for p in json.loads(r[0]).get("picks", [])]
    return sorted({s for s in saved + proposed + passed_over if s})


def find_candidates(places_file: Path, business_type: str, area: str, skip_ids: list[str],
                    min_confidence: float, shortlist: int) -> list[dict[str, Any]]:
    term = business_type.strip().lower()
    if not term:
        raise ScoutError("A business type is required, e.g. --type salon")
    # Values go in as ? parameters, never pasted into the SQL text.
    where = [
        "(lower(taxonomy_hierarchy) LIKE ? OR lower(basic_category) LIKE ? OR lower(taxonomy_hierarchy) LIKE ?)",
        "confidence >= ?",
        "(phone IS NOT NULL OR email IS NOT NULL)",
        "NOT list_contains(?::VARCHAR[], source_id)",
    ]
    params: list[Any] = [f"%{term}%", f"%{term}%", f"%{term.replace(' ', '_')}%", min_confidence, skip_ids]
    area_key = normalize_area(area)
    if area_key not in ("", "any", "all"):
        if area_key.isdigit():
            where.append("postcode LIKE ?")
            params.append(f"{area_key}%")
        else:
            where.append("trim(regexp_replace(replace(lower(locality), '.', ''), '^saint ', 'st ')) = ?")
            params.append(area_key)
    sql = (f"SELECT * FROM read_parquet('{Path(places_file).as_posix()}') WHERE {' AND '.join(where)}"
           f" ORDER BY confidence DESC, name LIMIT {SCAN_ROWS}")
    cursor = duckdb.connect().execute(sql, params)
    columns = [d[0] for d in cursor.description]
    rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
    rank = {NO_SITE: 0, WEAK_SITE: 1, OWN_SITE: 2}
    # Email-first: a public email comes before everything else, then web presence, then confidence.
    rows.sort(key=lambda r: (0 if (r.get("email") or "").strip() else 1, rank[web_presence(r["website"])],
                             -(r["confidence"] or 0)))
    return rows[:shortlist]


def usable_candidates(town: Town, business_type: str, area: str) -> int:
    """How many Overture candidates Scout could really propose for this type and area right now: the same
    filters as a run (confidence, reachable, not already known, the captive and lead-gen screen) plus email-first
    (a public email). Plain code on the local file: free, no model call. explore.auto_pick uses it."""
    ranked = find_candidates(town.settings.overture.places_file, business_type, area, known_source_ids(town),
                             float(town.limits.get("leads.min_confidence")), SCAN_ROWS)
    kept, _ = screen_candidates(ranked)
    return len([c for c in kept if (c.get("email") or "").strip()])


def lead_content(place: dict[str, Any]) -> dict[str, Any]:
    email = (place.get("email") or "").strip()
    region_zip = " ".join(x for x in (place.get("region"), place.get("postcode")) if x)
    address = ", ".join(p for p in (place.get("address"), place.get("locality"), region_zip) if p)
    return {
        "business_name": place["name"],
        "business_type": place.get("taxonomy_primary") or place.get("basic_category") or "unknown",
        "area": place.get("locality") or "",
        "address": address,
        "phone": place.get("phone") or "",
        "website": place.get("website") or "",
        "public_email": email,
        "contact_route": contact_route(email),
        "source": "overture",
        "source_id": place["source_id"],
        "source_release": place["release"],
        "confidence": round(float(place.get("confidence") or 0), 3),
        "flags": lead_flags(place),
        "held": not email,           # email-first: no public email = saved as held, never contacted
        # One click for Charlie: a Google search for the name and town (a link only; nothing is fetched or saved).
        "check_url": check_url(place["name"], place.get("locality")),
    }


def _shortlist_for_prompt(candidates: list[dict[str, Any]]) -> str:
    return json.dumps([{
        "source_id": c["source_id"], "name": c["name"], "brand": chain_brand(c),
        "type": c.get("taxonomy_primary") or c.get("basic_category"),
        "web_presence": web_presence(c.get("website")), "website": c.get("website"),
        "has_public_email": bool(c.get("email")), "phone": c.get("phone"), "flags": lead_flags(c),
        "town": c.get("locality"),
    } for c in candidates], indent=1)


def parse_picks(text: str, candidates: list[dict[str, Any]], n: int) -> list[tuple[dict[str, Any], str]] | None:
    """Turn Haiku's JSON into (place, why) pairs. Returns None if the reply can't be read."""
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not match:
        return None
    try:
        picks = json.loads(match.group(0)).get("picks")
    except (json.JSONDecodeError, AttributeError):
        return None
    if not isinstance(picks, list):
        return None
    by_id = {c["source_id"]: c for c in candidates}
    chosen: list[tuple[dict[str, Any], str]] = []
    for pick in picks:
        if not isinstance(pick, dict):
            continue
        place = by_id.pop(str(pick.get("source_id")), None)  # pop: ignores made-up or repeated ids
        why = str(pick.get("why") or "").strip()
        if place and why:
            chosen.append((place, why))
        if len(chosen) == n:
            break
    return chosen


def pick_leads(town: Town, candidates: list[dict[str, Any]], business_type: str, area: str,
               n: int) -> tuple[list[tuple[dict[str, Any], str]], Reply]:
    """Scout's judgment: one Haiku call picks up to n of the candidates, each with a reason.
    `town testset` calls this too, so the test set scores the exact prompt Scout really uses."""
    town.killswitch.check()
    system = agent_instructions(AGENT, JOB.format(n=n, local_codes=", ".join(LOCAL_AREA_CODES)))
    prompt = f"Business type: {business_type}\nArea: {area}\nCandidates:\n{_shortlist_for_prompt(candidates)}"
    reply = town.llm.ask(AGENT, system, prompt, effort="low")
    picks = parse_picks(reply.text, candidates, n)
    if picks is None:
        # Haiku's reply was unreadable: fall back to the code's own ranking, and say so on each pick.
        log_event(town.conn, AGENT, "scout_reply_unreadable", {"reply": reply.text[:500]})
        picks = [(c, f"{UNREADABLE_PREFIX} (web presence: {web_presence(c.get('website'))}); "
                     "Haiku's reply couldn't be read.") for c in candidates[:n]]
    return picks, reply


def run_scout(town: Town, business_type: str, area: str) -> ScoutRun:
    """`town scout`. Logs a start and exactly one finish (or failure), so the village map can show Scout working."""
    town.killswitch.check()
    log_event(town.conn, AGENT, "scout_started", {"business_type": business_type, "area": area})
    try:
        run = _scout(town, business_type, area)
    except TownStopped:
        log_event(town.conn, AGENT, "scout_finished", {"message": "stopped by the kill switch"})
        raise
    except ScoutError as err:   # a rule said no (no business type, no Overture data): refused, not broken
        log_event(town.conn, AGENT, "scout_refused", {"why": str(err)[:300]})
        raise
    except Exception as err:
        log_event(town.conn, AGENT, "scout_failed", {"error": f"{type(err).__name__}: {err}"[:300]})
        raise
    log_event(town.conn, AGENT, "scout_finished", {"message": run.message})
    return run


def _scout(town: Town, business_type: str, area: str) -> ScoutRun:
    places_file = town.settings.overture.places_file
    if not Path(places_file).exists():
        raise ScoutError("No Overture data yet. Run `town overture-download` first.")

    daily_cap = int(town.limits.get("leads.new_leads_per_day"))
    remaining = daily_cap - leads_proposed_today(town)
    if remaining <= 0:
        log_event(town.conn, AGENT, "scout_daily_cap_reached", {"cap": daily_cap})
        return ScoutRun(message=f"Daily lead cap reached ({daily_cap}). Scout stands down until tomorrow.")
    n = min(int(town.limits.get("leads.proposals_per_scout_run")), remaining)

    # Screen the whole ranked scan, then cut the shortlist, so a skipped lead never takes a slot.
    ranked = find_candidates(places_file, business_type, area, known_source_ids(town),
                             float(town.limits.get("leads.min_confidence")), SCAN_ROWS)
    candidates, skipped = screen_candidates(ranked, n * SHORTLIST_PER_PICK)
    if skipped:
        log_event(town.conn, AGENT, "scout_skipped", {"business_type": business_type, "area": area,
                                                      "skipped": skipped})
    if not candidates:
        return ScoutRun(message=f"No new {business_type} businesses found in {area}.")

    picks, reply = pick_leads(town, candidates, business_type, area, n)
    run = ScoutRun(cost_usd=reply.cost_usd, model=reply.model)
    for place, why in picks:
        town.killswitch.check()  # `town stop` mid-run stops Scout before the next save
        content = lead_content(place)
        problems = auto_save_problems(town, place, content)
        if problems:
            # Never saved. "held" = only the email was missing; anything else is dropped. Logged either way.
            fate = "held" if problems == [p for p in problems if p.startswith("no public email")] else "dropped"
            run.not_saved.append({"business_name": content["business_name"], "source_id": content["source_id"],
                                  "fate": fate, "why_not": problems, "scout_said": why})
            continue
        lead_id = insert_lead(town.conn, content, None, AGENT, why)
        run.saved.append(lead_id)
        log_event(town.conn, AGENT, "lead_saved", {"lead_id": lead_id, "business_name": content["business_name"],
                                                   "area": content["area"], "why": why, "saved_by": AGENT,
                                                   "check_url": content["check_url"]})
    if run.not_saved:
        log_event(town.conn, AGENT, "scout_not_saved", {"business_type": business_type, "area": area,
                                                        "picks": run.not_saved})
    log_event(town.conn, AGENT, "scout_run", {"business_type": business_type, "area": area,
                                              "shortlist": len(candidates), "saved": run.saved,
                                              "not_saved": len(run.not_saved),
                                              "model": reply.model, "cost_usd": round(reply.cost_usd, 6)})
    held = len([p for p in run.not_saved if p["fate"] == "held"])
    dropped = len(run.not_saved) - held
    run.message = (f"{len(run.saved)} lead(s) saved for {business_type} in {area}"
                   + (f"; not saved: {held} held (no public email)" if held else "")
                   + (f"{', ' if held else '; not saved: '}{dropped} dropped (a check failed)" if dropped else "")
                   + ". Nothing left the Town.")
    return run
