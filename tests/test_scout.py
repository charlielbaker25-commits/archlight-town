"""Scout v0 against a small fake places file and a fake Haiku. No network, no real API."""
import json
import re
from types import SimpleNamespace

import duckdb
import pytest
from typer.testing import CliRunner

import launcher
from engine.db import utc_now
from engine.killswitch import TownStopped
from engine.settings import load_settings
from engine.town import open_town
from packs.websites import pack
from packs.websites.instructions import agent_instructions
from packs.websites.leads import contact_route
from packs.websites.scout import (captive_networks, has_street_address, lead_flags, normalize_area,
                                  phone_area_code, run_scout, screen_candidates, skip_reason, web_presence)

# (id, name, basic_category, taxonomy_primary, website, phone, email, locality, confidence)
PLACES = [
    ("p1", "Shear Bliss", "personal_or_beauty_service", "hair_salon", None, "+13145550101", "hi@shearbliss.example", "Clayton", 0.95),
    ("p2", "Nail Nook", "personal_or_beauty_service", "nail_salon", None, "+13145550102", None, "Clayton", 0.92),
    ("p3", "Glow Beauty", "personal_or_beauty_service", "beauty_salon", "https://glow.square.site", "+13145550103", None, "Clayton", 0.90),
    ("p4", "Cuts by Dee", "personal_or_beauty_service", "hair_salon", "https://facebook.com/cutsbydee", "+13145550104", "dee@cuts.example", "Clayton", 0.88),
    ("p5", "Modern Mane", "personal_or_beauty_service", "hair_salon", "https://modernmane.example", "+13145550105", None, "Clayton", 0.91),
    ("p6", "Salon Six", "personal_or_beauty_service", "hair_salon", None, "+13145550106", None, "Clayton", 0.85),
    ("p7", "Faded Barbers", "personal_or_beauty_service", "barber", None, "+13145550107", None, "Clayton", 0.93),
    ("p8", "Low Confidence Salon", "personal_or_beauty_service", "hair_salon", None, "+13145550108", None, "Clayton", 0.40),
    ("p9", "Unreachable Salon", "personal_or_beauty_service", "hair_salon", None, None, None, "Clayton", 0.90),
    ("p10", "Downtown Salon", "personal_or_beauty_service", "hair_salon", None, "+13145550110", None, "St. Louis", 0.90),
    ("p11", "Joe's Diner", "restaurant", "diner", None, "+13145550111", None, "Clayton", 0.95),
    ("p12", "Ladue Locks", "personal_or_beauty_service", "hair_salon", None, "+16465550112", None, "Ladue", 0.90),
    ("p13", "Jake Doe - State Farm Insurance Agent", "insurance", "insurance_agency", None, "+13145550113", None, "Clayton", 0.95),
    ("p14", "Clayton Insurance Group", "insurance", "insurance_agency", None, "+13145550114", None, "Clayton", 0.90),
]


def _write_places(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("""CREATE TABLE p (source_id VARCHAR, name VARCHAR, basic_category VARCHAR, taxonomy_primary VARCHAR,
                   website VARCHAR, phone VARCHAR, email VARCHAR, locality VARCHAR, confidence DOUBLE)""")
    con.executemany("INSERT INTO p VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", PLACES)
    con.execute(f"""COPY (SELECT source_id, name, basic_category, taxonomy_primary,
                    'services > ' || basic_category || ' > ' || taxonomy_primary AS taxonomy_hierarchy,
                    confidence, website, phone, email, '1 Main St' AS address, locality, 'MO' AS region,
                    '63105' AS postcode, 38.65 AS lat, -90.32 AS lon, 1.0 AS distance_miles,
                    '2026-09-23.1' AS release FROM p) TO '{path.as_posix()}' (FORMAT PARQUET)""")


class FakeHaiku:
    """Picks every candidate it's shown, in order, up to the number the prompt asks for."""

    def __init__(self, reply=None):
        self.calls = 0
        self.reply = reply
        self.messages = self

    def create(self, **request):
        self.calls += 1
        self.last_request = request
        n = int(re.search(r"pick the (\d+) best", request["system"]).group(1))
        shortlist = json.loads(request["messages"][0]["content"].split("Candidates:\n", 1)[1])
        text = self.reply if self.reply is not None else json.dumps(
            {"picks": [{"source_id": c["source_id"], "why": f"{c['name']}: web presence {c['web_presence']}."}
                       for c in shortlist[:n]]})
        usage = SimpleNamespace(input_tokens=1500, output_tokens=300,
                                cache_creation_input_tokens=0, cache_read_input_tokens=0)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], usage=usage)


@pytest.fixture
def places(instance_file):
    settings = load_settings(instance_file)
    _write_places(settings.overture.places_file)
    return settings


@pytest.fixture
def town(places, limits_file):
    fake = FakeHaiku()
    t = open_town(places, pack.PACK_SCHEMAS, pack.handlers(), limits_file, pack.MODELS_FILE, api_client=fake)
    t.killswitch.resume(t.conn)
    t.fake = fake
    yield t
    t.close()


def _leads(town):
    return [dict(r) for r in town.conn.execute("SELECT * FROM leads ORDER BY id")]


def _not_saved(town):
    rows = town.conn.execute("SELECT details FROM events WHERE event_type = 'scout_not_saved' ORDER BY id")
    return [p for r in rows for p in json.loads(r["details"])["picks"]]


def _proposals(town):
    return [dict(r) | {"content": json.loads(r["content"])} for r in
            town.conn.execute("SELECT * FROM actions WHERE action_type = 'add_lead' ORDER BY id")]


def test_a_lead_with_no_public_email_gets_the_call_route():
    assert contact_route(None) == "call"
    assert contact_route("   ") == "call"
    assert contact_route("hi@shop.example") == "email"


def test_scout_saves_the_picks_that_pass_every_check_and_nothing_else(town):
    """Leads save automatically (Charlie, 2026-11-05): a public email, no flags, a local phone, no website or only
    a social/free-builder page, not on the do-not-contact list, never seen before. Anything else is never saved."""
    run = run_scout(town, "salon", "Clayton")
    leads = _leads(town)
    assert [l["business_name"] for l in leads] == ["Shear Bliss", "Cuts by Dee"]   # no site / Facebook only
    assert run.saved == [l["id"] for l in leads]
    assert all(l["saved_by"] == "scout" and l["action_id"] is None and l["status"] == "new" for l in leads)
    assert all(l["contact_route"] == "email" and l["source"] == "overture" and l["why"] for l in leads)
    assert _proposals(town) == []                                  # nothing waits for an approval
    fates = {p["business_name"]: p["fate"] for p in _not_saved(town)}
    assert fates == {"Nail Nook": "held", "Glow Beauty": "held", "Salon Six": "held"}   # no public email: held
    assert "2 lead(s) saved" in run.message and "3 held" in run.message
    saved_events = town.conn.execute("SELECT COUNT(*) FROM events WHERE event_type = 'lead_saved' AND agent = 'scout'")
    assert saved_events.fetchone()[0] == 2
    assert town.fake.calls == 1 and town.fake.last_request["model"] == "claude-haiku-5-5"


def test_a_pick_with_its_own_website_or_an_out_of_area_phone_is_dropped_not_saved(town):
    from packs.websites import scout as scout_mod
    place = {"source_id": "x", "name": "Modern Mane", "phone": "+13145550105", "email": "hi@mane.example",
             "website": "https://modernmane.example", "locality": "Clayton", "release": "r"}
    content = scout_mod.lead_content(place)
    assert scout_mod.auto_save_problems(town, place, content) == ["it lists a website of its own"]
    far = {**place, "website": None, "phone": "+16465550112"}
    problems = scout_mod.auto_save_problems(town, far, scout_mod.lead_content(far))
    assert "flagged: out_of_area_phone" in problems and "no local St. Louis phone number" in problems
    no_phone = {**place, "website": None, "phone": None}
    assert scout_mod.auto_save_problems(town, no_phone, scout_mod.lead_content(no_phone)) == [
        "no local St. Louis phone number"]


def test_a_business_on_the_do_not_contact_list_or_seen_before_is_never_saved(town):
    from packs.websites.guard import dnc_add
    dnc_add(town.conn, "hi@shearbliss.example", "asked us not to", "charlie")
    town.conn.execute("INSERT INTO leads (business_name, business_type, area, public_email, contact_route, source,"
                      " source_id, source_release, created_at) VALUES ('Cuts by Dee', 'hair_salon', 'Clayton',"
                      " 'other@cuts.example', 'email', 'overture', 'older-id', 'r', ?)", (utc_now(),))
    run = run_scout(town, "salon", "Clayton")
    assert run.saved == []
    why = {p["business_name"]: " ".join(p["why_not"]) for p in _not_saved(town)}
    assert "do-not-contact" in why["Shear Bliss"] and "already proposed, saved or contacted" in why["Cuts by Dee"]


def test_scout_never_picks_the_same_business_twice(town):
    run_scout(town, "salon", "Clayton")
    first = {p["source_id"] for p in _not_saved(town)} | {l["source_id"] for l in _leads(town)}
    run_scout(town, "salon", "Clayton")
    shortlist = json.loads(town.fake.last_request["messages"][0]["content"].split("Candidates:\n", 1)[1])
    assert not first & {c["source_id"] for c in shortlist}         # saved, held and dropped picks never come back
    ids = [l["source_id"] for l in _leads(town)]
    assert len(ids) == len(set(ids))


def test_daily_lead_cap_is_enforced(town):
    for i in range(49):
        town.conn.execute(
            "INSERT INTO actions (agent, action_type, target, content, reason, created_at)"
            " VALUES ('scout', 'add_lead', 'x', ?, 'seed', ?)", (json.dumps({"source_id": f"seed{i}"}), utc_now()))
    assert len(run_scout(town, "salon", "Clayton").saved) == 1      # one left today: one pick, saved
    run = run_scout(town, "salon", "Clayton")
    assert run.saved == [] and "cap reached" in run.message           # the auto-saved lead counts toward the cap
    assert town.fake.calls == 1  # no API call once the cap is reached


def test_scout_does_nothing_while_the_town_is_stopped(town):
    town.killswitch.stop(town.conn)
    with pytest.raises(TownStopped):
        run_scout(town, "salon", "Clayton")
    assert town.fake.calls == 0 and _proposals(town) == [] and _leads(town) == []


def test_unreadable_haiku_reply_falls_back_to_code_ranking(town):
    town.fake.reply = "Sorry, here are some thoughts..."
    run = run_scout(town, "salon", "Clayton")
    assert len(run.saved) + len(run.not_saved) == 5
    assert "Ranked by code" in _leads(town)[0]["why"]


def test_a_lead_charlie_adds_by_hand_still_needs_his_approval_and_an_edit_can_change_its_route(town):
    content = {"business_name": "Nail Nook", "business_type": "nail_salon", "area": "Clayton", "address": "1 Main St",
               "phone": "+13145550102", "website": "", "public_email": "", "source": "overture", "source_id": "p2",
               "source_release": "r", "confidence": 0.9}
    action_id = town.mayor.propose("charlie", "add_lead", "Nail Nook (Clayton)", content, "Charlie adds it by hand")
    town.mayor.edit_and_approve(action_id, {**content, "public_email": "nails@nook.example"})
    town.executor.execute(action_id)
    lead = town.conn.execute("SELECT * FROM leads WHERE source_id = 'p2'").fetchone()
    assert lead["contact_route"] == "email" and lead["action_id"] == action_id and lead["saved_by"] == "charlie"


def test_area_codes_and_the_out_of_area_flag():
    assert phone_area_code("+13145550101") == phone_area_code("(314) 555-0101") == "314"
    assert phone_area_code("646-555-0110") == "646"
    assert phone_area_code("555-0101") is None and phone_area_code(None) is None
    assert lead_flags({"phone": "+16365550100"}) == [] and lead_flags({"phone": "618-555-0100"}) == []
    assert lead_flags({"phone": "+16465550112"}) == ["out_of_area_phone"]
    assert lead_flags({"phone": None}) == []


def test_chain_locations_are_flagged_by_brand_or_store_number():
    for name in ("QuickTurn Oil & Lube #214", "QuickTurn # 9", "Mega Mart Store 52", "Mega Mart Store #52",
                 "Mega Mart Store No. 52"):
        assert lead_flags({"name": name, "phone": "+13145550111"}) == ["chain_location"], name
    assert lead_flags({"name": "Brentwood Oil Change", "brand": "QuickTurn"}) == ["chain_location"]
    # Overture often lists a lone business's own name as its brand: not a chain...
    assert lead_flags({"name": "Midwest Clearance Center", "brand": "Midwest Clearance Center",
                       "brand_locations": 1}) == []
    # ...unless the same brand is on several places in the region.
    assert lead_flags({"name": "Mercy", "brand": "Mercy", "brand_locations": 139}) == ["chain_location"]
    for name in ("Studio 54 Salon", "Restore 2 Wellness", "Number One Nails"):
        assert lead_flags({"name": name, "brand": None}) == [], name
    assert lead_flags({"name": "STL Lube #3", "phone": "+16465550110"}) == ["out_of_area_phone", "chain_location"]


def test_a_chain_location_is_flagged_for_haiku_and_never_saved(places, limits_file):
    # Give one place (Joe's Diner) an Overture brand, the way a franchise listing would have.
    places_file = places.overture.places_file
    branded = places_file.with_name("branded.parquet")
    duckdb.connect().execute(f"""COPY (SELECT *, CASE WHEN source_id = 'p11' THEN 'Diner Chain Co' END AS brand,
                                 CASE WHEN source_id = 'p11' THEN 12 ELSE 0 END AS brand_locations,
                                 CASE WHEN source_id = 'p11' THEN 'joe@diner.example' END AS email2
                                 FROM read_parquet('{places_file.as_posix()}')) TO '{branded.as_posix()}'
                                 (FORMAT PARQUET)""")
    branded.replace(places_file)
    fake = FakeHaiku()
    town = open_town(places, pack.PACK_SCHEMAS, pack.handlers(), limits_file, pack.MODELS_FILE, api_client=fake)
    town.killswitch.resume(town.conn)
    try:
        run = run_scout(town, "diner", "Clayton")
        assert run.saved == [] and _leads(town) == []          # a flag means it's never saved without Charlie
        [pick] = _not_saved(town)
        assert pick["business_name"] == "Joe's Diner" and "flagged: chain_location" in pick["why_not"]
        sent = fake.last_request
        assert '"brand": "Diner Chain Co"' in sent["messages"][0]["content"] and "chain_location" in sent["system"]
    finally:
        town.close()


def test_an_out_of_area_phone_is_flagged_for_haiku_and_never_saved(town):
    run = run_scout(town, "salon", "Ladue")
    assert run.saved == [] and _leads(town) == []
    [pick] = _not_saved(town)
    assert "flagged: out_of_area_phone" in pick["why_not"]
    sent = town.fake.last_request
    assert "+16465550112" in sent["messages"][0]["content"] and "out_of_area_phone" in sent["messages"][0]["content"]
    assert "314, 636, 618" in sent["system"]


def test_the_two_state_farm_listings_reaper_flagged_on_dev_are_skipped_now():
    # Dev #9 and #10 (2026-10-20 05:51 UTC) were proposed ten hours before the captive-agent check landed
    # (cac252a, 15:33 UTC). The same listings, as Overture names them, must be skipped today.
    networks = captive_networks()
    for name, address in (("Anthony Luster - State Farm Insurance Agent", "1045 N Harrison Ave"),
                          ("Greg Metzger - State Farm Insurance Agent", "11046 Manchester Rd")):
        assert skip_reason({"name": name, "address": address}, networks).startswith("captive_agent"), name


def test_captive_agents_are_skipped_by_name_or_brand():
    networks = captive_networks()
    for name in ("Jake Molitor - State Farm Insurance Agent", "ALLSTATE INSURANCE: Pat Lee", "Edward Jones - Ann Ray",
                 "H&R Block", "Liberty Tax Service", "Mutual of Omaha Advisor"):
        assert skip_reason({"name": name, "address": "1 Main St"}, networks).startswith("captive_agent"), name
    assert skip_reason({"name": "Ray Financial", "brand": "Northwestern Mutual", "address": "1 Main St"},
                       networks).startswith("captive_agent")
    # Independent agencies, look-alike words and real estate stay in.
    for name in ("Clayton Insurance Group", "Farmers Market of Kirkwood", "Statefarmer Supply", "Allstates Moving",
                 "Coldwell Banker Gundaker - Sue Ames"):
        assert skip_reason({"name": name, "address": "1 Main St"}, networks) is None, name


def test_lead_gen_pattern_needs_both_signs():
    assert has_street_address("7340 Manchester Rd, Maplewood") and has_street_address("117 N Kirkwood Rd #211")
    assert not has_street_address("Serving St. Louis, MO") and not has_street_address("PO Box 12, St. Louis")
    assert not has_street_address(None)
    out_of_area = {"name": "STL Garage Door Repair 24/7", "phone": "646-555-0110"}
    assert skip_reason(out_of_area | {"address": "Serving St. Louis, MO"}, []).startswith("lead_gen_listing")
    # Only one sign: still goes to Haiku and Charlie.
    assert skip_reason(out_of_area | {"address": "12 Elm St"}, []) is None
    assert skip_reason({"name": "Local Garage", "phone": "314-555-0110", "address": "Serving St. Louis"}, []) is None


def test_screening_keeps_order_and_only_reports_skips_that_would_reach_haiku():
    ranked = [{"source_id": f"s{i}", "name": n, "address": "1 Main St"}
              for i, n in enumerate(["A", "State Farm - B", "C", "D", "Allstate - E"])]
    kept, skipped = screen_candidates(ranked, limit=2, networks=["State Farm", "Allstate"])
    assert [k["name"] for k in kept] == ["A", "C"]
    assert [s["name"] for s in skipped] == ["State Farm - B"]   # Allstate - E ranked past the cut


def test_scout_skips_captive_agents_in_code_and_logs_them(town):
    run_scout(town, "insurance", "Clayton")
    names = [p["business_name"] for p in _not_saved(town)] + [l["business_name"] for l in _leads(town)]
    assert names == ["Clayton Insurance Group"]
    assert "State Farm" not in town.fake.last_request["messages"][0]["content"]   # Haiku never saw it
    event = town.conn.execute("SELECT details FROM events WHERE event_type = 'scout_skipped'").fetchone()
    [skip] = json.loads(event["details"])["skipped"]
    assert skip["source_id"] == "p13" and skip["reason"].startswith("captive_agent: State Farm")


def test_matching_helpers():
    assert normalize_area("Saint Louis") == normalize_area("St. Louis") == normalize_area("st louis")
    assert web_presence(None) == "none"
    assert web_presence("https://x.square.site") != web_presence("https://ownsite.example")


def test_playbook_is_loaded_into_the_agent_instructions(tmp_path):
    (tmp_path / "scout.md").write_text("- Prefer salons with no website.", encoding="utf-8")
    assert "Prefer salons with no website." in agent_instructions("scout", "JOB", tmp_path)
    assert agent_instructions("mayor", "JOB", tmp_path) == "JOB"
    assert (pack.PLAYBOOKS_DIR / "scout.md").exists() and (pack.PLAYBOOKS_DIR / "mayor.md").exists()


def test_phase_1_done_when_run_through_the_town_commands(places, monkeypatch):
    """Updated 2026-11-05: Scout saves the leads that pass every check itself; Charlie's approval path stays for a
    lead he adds by hand (approve, reject with a reason, edit), and the log shows everything, costs and model too."""
    monkeypatch.setattr(launcher, "API_CLIENT", FakeHaiku())
    runner = CliRunner()
    town = lambda *args: runner.invoke(launcher.app, list(args))

    town("resume")
    result = town("scout", "--type", "salon", "--area", "Clayton")
    assert result.exit_code == 0 and "2 lead(s) saved" in " ".join(result.stdout.split())
    t = open_town(places, pack.PACK_SCHEMAS, {}, pack.LIMITS_FILE, pack.MODELS_FILE)
    for i in range(3):
        t.mayor.propose("charlie", "add_lead", f"Hand {i}", {"business_name": f"Hand {i}", "business_type": "salon",
                        "area": "Clayton", "public_email": f"h{i}@hand.example", "source": "overture",
                        "source_id": f"hand{i}", "source_release": "r"}, "added by hand")
    t.close()
    pending = town("pending").stdout
    ids = [int(i) for i in re.findall(r"^#(\d+)\s", pending, re.MULTILINE)]
    assert len(ids) == 3
    assert town("approve", str(ids[0])).exit_code == 0
    assert town("reject", str(ids[1]), "--reason", "Chain salon, not independent").exit_code == 0
    assert town("edit", str(ids[2]), "--set", "phone=314-555-0199").exit_code == 0

    log = town("log", "--full", "-n", "200").stdout
    for expected in ("lead_saved", "action_rejected", "Chain salon, not independent", "action_edited_and_approved",
                     "314-555-0199", "cost_logged", "claude-haiku-5-5", "execution_finished"):
        assert expected in log
    assert "of $9.00" in town("costs").stdout

    assert "Standing down" in town("stop").stdout
    assert town("scout", "--type", "salon", "--area", "Clayton").exit_code == 1
    assert "Systems online" in town("resume").stdout

    t = open_town(places, pack.PACK_SCHEMAS, {}, pack.LIMITS_FILE, pack.MODELS_FILE)
    saved = t.conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
    t.close()
    assert saved == 4  # two by Scout, one approved + one edited by hand
    assert list(places.backup_folder.glob("town-*.db"))  # today's backup exists


def test_a_scout_run_logs_a_start_and_one_finish_for_the_village_map(town):
    run_scout(town, "salon", "Clayton")
    kinds = [r[0] for r in town.conn.execute(
        "SELECT event_type FROM events WHERE agent = 'scout' AND event_type IN ('scout_started', 'scout_finished',"
        " 'scout_failed') ORDER BY id")]
    assert kinds == ["scout_started", "scout_finished"]


# ---------------- email-first (Charlie, 2026-11-03) ----------------

def test_email_leads_come_first_and_haiku_is_told_email_only(town):
    run_scout(town, "salon", "Clayton")
    shortlist = json.loads(town.fake.last_request["messages"][0]["content"].split("Candidates:\n", 1)[1])
    flags = [c["has_public_email"] for c in shortlist]
    assert flags == sorted(flags, reverse=True)            # every email lead before every no-email lead
    assert "EMAIL ONLY" in town.fake.last_request["system"]


def test_a_no_email_pick_is_held_never_saved_and_never_contacted(town):
    run_scout(town, "salon", "Clayton")
    held = next(p for p in _not_saved(town) if p["business_name"] == "Nail Nook")
    assert held["fate"] == "held" and held["why_not"][0].startswith("no public email")
    assert "Nail Nook" not in {l["business_name"] for l in _leads(town)}


def test_every_saved_lead_keeps_its_google_check_link(town):
    from packs.websites.leads import check_url
    assert check_url("Shear Bliss", "Clayton") == "https://www.google.com/search?q=Shear+Bliss+Clayton"
    run_scout(town, "salon", "Clayton")
    for lead in _leads(town):    # built in plain code from the name and town; nothing from Google is fetched or kept
        assert lead["check_url"] == check_url(lead["business_name"], lead["area"])


def test_a_pick_with_an_invalid_email_is_never_saved_and_says_why(town):
    """Lead #12 Fictional Corner Shop: Overture lists "fictional'splace01@gmail.com". Gmail addresses can't hold an apostrophe,
    so it would bounce; with leads saved automatically, plain code catches it before saving."""
    from packs.websites import scout as scout_mod
    place = {"source_id": "x12", "name": "Fictional Corner Shop", "phone": "+13145550112", "email": "fictional'splace01@gmail.com",
             "website": None, "locality": "Clayton", "release": "r"}
    problems = scout_mod.auto_save_problems(town, place, scout_mod.lead_content(place))
    assert problems == ["email address looks invalid: fictional'splace01@gmail.com (it has a space, quote or apostrophe)"]
