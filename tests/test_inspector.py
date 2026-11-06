"""The Inspector: grading rules, the need score, the note, saved records, caps and safety.
A fake measurer and a fake Haiku stand in for the browser and the API: no network, no real cost."""
import json
import os
import socket
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import launcher
from engine.budget import PaidWorkPaused
from engine.db import utc_now
from engine.killswitch import TownStopped
from engine.limits import Limits
from engine.settings import load_settings
from engine.town import open_town
from packs.websites import pack
from packs.websites.inspector import (InspectorError, InspectorLimits, grade, need_score, parse_note,
                                      run_inspector, site_kind)
from packs.websites.inspector_measure import DomainNotFound, UnsafeURL, check_public_url

runner = CliRunner()
YEAR = 2026
OLD_SITE = "http://www.oldshop.example.com"
GOOD_SITE = "https://www.goodshop.example.com"
BROKEN_SITE = "https://www.brokenshop.example.com"
DOWN_SITE = "https://www.downshop.example.com"
GONE_SITE = "https://www.gone-shop.example.com"

# Facts shaped like what inspector_measure.SiteMeasurer.measure() returns for each kind of site.
CLEAN = {"fetched": True, "status": 200, "viewport_tag": "width=device-width, initial-scale=1",
         "overflows_at_375": False, "width_at_375": 375, "http_form_actions": [], "copyright_year": 2026,
         "dated_markers": [], "broken_resources": [], "broken_links": [], "page_kb": 40.0,
         "lighthouse": {"performance": 97, "lcp_seconds": 1.1},
         "lighthouse_runs": [{"performance": 97, "lcp_seconds": 1.1}]}
FACTS = {
    GOOD_SITE: {**CLEAN, "final_url": GOOD_SITE + "/"},
    OLD_SITE: {**CLEAN, "final_url": OLD_SITE + "/", "viewport_tag": None, "overflows_at_375": True,
               "width_at_375": 800, "copyright_year": 2009, "dated_markers": ["marquee", "font"]},
    BROKEN_SITE: {**CLEAN, "final_url": BROKEN_SITE + "/", "broken_links": [BROKEN_SITE + "/menu.html"],
                  "broken_resources": [BROKEN_SITE + "/images/a.jpg"], "page_kb": 1203.0,
                  "lighthouse": {"performance": 69, "lcp_seconds": 7.2},
                  "lighthouse_runs": [{"performance": 66, "lcp_seconds": 7.5}, {"performance": 69, "lcp_seconds": 7.2}]},
    DOWN_SITE: {"fetched": False, "error": "net::ERR_CONNECTION_TIMED_OUT"},
}


@pytest.fixture
def lim(limits_file):
    return InspectorLimits.read(Limits(limits_file))


class FakeMeasurer:
    """Returns canned facts per URL. Raises DomainNotFound for GONE_SITE, like a real dead domain."""

    def __init__(self):
        self.measured, self.screenshot_only, self.opened = [], [], 0

    def __enter__(self):
        self.opened += 1
        return self

    def __exit__(self, *exc):
        return False

    def measure(self, url, access, shots, identity=None):
        self.measured.append(url)
        if url == GONE_SITE:
            raise DomainNotFound("www.gone-shop.example.com doesn't exist")
        facts = dict(FACTS[url])
        if "error" not in facts:
            facts["screenshots"] = {k: str(v) for k, v in shots.items()}
        return facts

    def screenshots_only(self, url, access, shots):
        self.screenshot_only.append(url)
        return {"fetched": True, "screenshots": {k: str(v) for k, v in shots.items()}}


class FakeHaiku:
    """Writes one sentence per problem it's shown, plus one about a problem nobody measured."""

    def __init__(self, reply=None):
        self.calls, self.reply, self.messages = 0, reply, self

    def create(self, **request):
        self.calls += 1
        self.last_request = request
        problems = json.loads(request["messages"][0]["content"])["problems"]
        why = {p: f"Haiku on {p}." for p in problems}
        why.setdefault("slow", "INVENTED: the site is slow.")
        text = self.reply if self.reply is not None else json.dumps({"why": why})
        usage = SimpleNamespace(input_tokens=600, output_tokens=150, cache_creation_input_tokens=0,
                                cache_read_input_tokens=0)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], usage=usage)


@pytest.fixture
def town(instance_file, limits_file):
    t = open_town(load_settings(instance_file), pack.PACK_SCHEMAS, pack.handlers(), limits_file,
                  pack.MODELS_FILE, api_client=FakeHaiku())
    t.killswitch.resume(t.conn)
    yield t
    t.close()


def add_lead(conn, name, website="", email="owner@shop.example"):
    """A saved lead. It has a public email by default: a held lead (no email) is skipped by a bulk run."""
    return conn.execute(
        "INSERT INTO leads (business_name, business_type, area, website, public_email, contact_route, source,"
        " source_id, source_release, created_at) VALUES (?, 'shop', 'Clayton', ?, ?, ?, 'overture', ?,"
        " '2026-09-23.1', ?)", (name, website, email, "email" if email else "call", f"src-{name}",
                                utc_now())).lastrowid


def inspect(town, lead_id=None, measurer=None):
    measurer = measurer or FakeMeasurer()
    return run_inspector(town, lead_id, open_measurer=lambda lim, t: measurer), measurer


# ---------------- grading (plain code) ----------------

def test_site_kinds():
    assert site_kind("") == "none" and site_kind(None) == "none"
    assert site_kind("https://www.facebook.com/someshop") == "social"
    assert site_kind("https://someshop.wixsite.com/home") == "builder"
    assert site_kind(GOOD_SITE) == "own"


def test_each_category_comes_from_a_measurement(lim):
    assert grade("none", {}, lim, YEAR) == ["no_site"]
    assert grade("own", {"domain_not_found": "x"}, lim, YEAR) == ["no_site"]
    assert grade("social", {}, lim, YEAR) == grade("builder", {}, lim, YEAR) == ["social_or_builder_only"]
    assert grade("own", FACTS[GOOD_SITE], lim, YEAR) == []
    assert grade("own", FACTS[OLD_SITE], lim, YEAR) == ["not_mobile_friendly", "no_https", "outdated"]
    assert grade("own", FACTS[BROKEN_SITE], lim, YEAR) == ["slow", "broken_links"]


def test_grading_details(lim):
    facts = {**CLEAN, "final_url": GOOD_SITE}
    assert grade("own", {**facts, "http_form_actions": ["http://x.example.com/form"]}, lim, YEAR) == ["no_https"]
    assert grade("own", {**facts, "certificate_error": "net::ERR_CERT_DATE_INVALID"}, lim, YEAR) == ["no_https"]
    assert grade("own", {**facts, "viewport_tag": "width=1024"}, lim, YEAR) == ["not_mobile_friendly"]
    # outdated_after_years is 5: a 2022 copyright in 2026 is fine, 2021 is outdated.
    assert grade("own", {**facts, "copyright_year": 2022}, lim, YEAR) == []
    assert grade("own", {**facts, "copyright_year": 2021}, lim, YEAR) == ["outdated"]
    runs = lambda *rs: {**facts, "lighthouse_runs": list(rs)}
    low_score, late_lcp = {"performance": 45, "lcp_seconds": 2.0}, {"performance": 80, "lcp_seconds": 4.5}
    # "Slow" needs two runs that both read slow (for either reason)...
    assert grade("own", runs(low_score, late_lcp), lim, YEAR) == ["slow"]
    # ...one slow run alone isn't enough: not confirmed, or the second run disagrees or failed.
    assert grade("own", runs(low_score), lim, YEAR) == []
    assert grade("own", runs(low_score, {"performance": 75, "lcp_seconds": 3.9}), lim, YEAR) == []
    assert grade("own", runs(low_score, {"error": "Lighthouse took too long"}), lim, YEAR) == []
    # Lighthouse couldn't run at all: the page weight decides instead.
    assert grade("own", {**runs({"error": "x"}), "page_kb": 1500}, lim, YEAR) == ["slow"]
    assert grade("own", {**runs({"error": "x"}), "page_kb": 500}, lim, YEAR) == []
    assert grade("own", {**facts, "status": 404}, lim, YEAR) == ["broken_links"]


def test_need_score_and_the_build_worthy_line(lim):
    assert need_score([]) == 0
    assert need_score(["no_https"]) == 40 >= lim.build_worthy  # a "Not secure" site is build-worthy alone
    assert need_score(["outdated"]) == 30 < lim.build_worthy
    assert need_score(["not_mobile_friendly", "no_https", "outdated"]) == 100  # capped
    assert need_score(["no_site"]) == 100


def test_the_note_keeps_only_measured_problems():
    reply = json.dumps({"why": {"no_https": "Browsers say Not secure.", "slow": "INVENTED", "made_up": "x"}})
    assert parse_note(reply, ["no_https"]) == {"no_https": "Browsers say Not secure."}
    assert parse_note("not json", ["no_https"]) is None


# ---------------- a run ----------------

def test_a_run_grades_saves_and_logs_each_lead(town):
    ids = {name: add_lead(town.conn, name, site) for name, site in
           [("No Site Co", ""), ("Old Shop", OLD_SITE), ("Good Shop", GOOD_SITE), ("Broken Shop", BROKEN_SITE)]}
    result, measurer = inspect(town)
    by_lead = {r["lead_id"]: r for r in result.inspected}
    assert by_lead[ids["No Site Co"]]["problems"] == ["no_site"] and by_lead[ids["No Site Co"]]["need_score"] == 100
    old = by_lead[ids["Old Shop"]]
    assert old["problems"] == ["not_mobile_friendly", "no_https", "outdated"] and old["build_worthy"]
    good = by_lead[ids["Good Shop"]]
    assert good["problems"] == [] and good["need_score"] == 0 and not good["build_worthy"] and good["note_by"] == "code"
    assert measurer.measured == [OLD_SITE, GOOD_SITE, BROKEN_SITE] and measurer.opened == 1
    rows = town.conn.execute("SELECT * FROM inspections ORDER BY id").fetchall()
    assert len(rows) == 4 and all(r["status"] == "done" for r in rows)
    old_row = next(r for r in rows if r["lead_id"] == ids["Old Shop"])
    assert old_row["desktop_screenshot"].endswith("-desktop.png") and "lead-" in old_row["mobile_screenshot"]
    assert json.loads(old_row["facts"])["copyright_year"] == 2009
    # Three leads had problems, so three Haiku notes; the clean site needed none. Each cost is logged with its model.
    assert town.llm._client.calls == 3
    costs = town.conn.execute("SELECT model FROM costs WHERE agent = 'inspector'").fetchall()
    assert len(costs) == 3 and {c["model"] for c in costs} == {"claude-haiku-5-5"}
    events = [r["event_type"] for r in town.conn.execute("SELECT event_type FROM events WHERE agent = 'inspector'")]
    assert events.count("inspection_done") == 4 and "inspector_run" in events


def test_the_note_never_carries_an_unmeasured_problem(town):
    add_lead(town.conn, "Old Shop", OLD_SITE)
    result, _ = inspect(town)
    note = result.inspected[0]["note"]
    assert "Haiku on no_https." in note and "Haiku on outdated." in note
    assert "INVENTED" not in note  # Haiku wrote about "slow", which wasn't measured: dropped
    sent = json.loads(town.llm._client.last_request["messages"][0]["content"])
    assert set(sent["problems"]) == {"not_mobile_friendly", "no_https", "outdated"}


def test_an_unreadable_note_falls_back_to_plain_code(town):
    town.llm._client = FakeHaiku(reply="sorry")
    add_lead(town.conn, "Old Shop", OLD_SITE)
    [record] = inspect(town)[0].inspected
    assert record["note_by"].startswith("code") and "Not secure:" in record["note"]


def test_social_pages_are_never_loaded_and_builder_pages_only_for_pictures(town):
    add_lead(town.conn, "Social Shop", "https://www.facebook.com/fictional.socialshop")
    add_lead(town.conn, "Builder Shop", "https://fictional-buildershop.wixsite.com/home")
    result, measurer = inspect(town)
    assert measurer.measured == [] and measurer.screenshot_only == ["https://fictional-buildershop.wixsite.com/home"]
    assert [r["problems"] for r in result.inspected] == [["social_or_builder_only"]] * 2


def test_no_browser_starts_when_no_lead_has_a_site(town):
    add_lead(town.conn, "No Site Co")
    _, measurer = inspect(town)
    assert measurer.opened == 0


def test_a_dead_domain_is_no_site_and_a_down_site_is_failed_not_graded(town):
    gone = add_lead(town.conn, "Gone Shop", GONE_SITE)
    down = add_lead(town.conn, "Down Shop", DOWN_SITE)
    result, _ = inspect(town)
    by_lead = {r["lead_id"]: r for r in result.inspected}
    assert by_lead[gone]["problems"] == ["no_site"]
    assert by_lead[down]["status"] == "failed" and by_lead[down]["need_score"] is None
    assert "ERR_CONNECTION_TIMED_OUT" in by_lead[down]["error"]
    # `town inspect` doesn't hit the failed site again; `town inspect <id>` retries it.
    assert inspect(town)[0].inspected == []
    retry, measurer = inspect(town, down)
    assert measurer.measured == [DOWN_SITE] and len(retry.inspected) == 1


def test_a_done_lead_isnt_inspected_twice_and_unknown_leads_are_refused(town):
    lead = add_lead(town.conn, "Good Shop", GOOD_SITE)
    inspect(town)
    with pytest.raises(InspectorError, match="already inspected"):
        inspect(town, lead)
    with pytest.raises(InspectorError, match="No saved lead #999"):
        inspect(town, 999)


def test_the_daily_cap_stops_the_inspector(town, limits_file):
    Limits(limits_file).set("inspector.inspections_per_day", 2)
    for i in range(3):
        add_lead(town.conn, f"Shop {i}")
    first, _ = inspect(town)
    assert len(first.inspected) == 2 and "1 wait for tomorrow" in first.message
    second, _ = inspect(town)
    assert second.inspected == [] and "Daily inspection cap reached (2)" in second.message


def test_nothing_runs_while_the_town_is_stopped(town):
    add_lead(town.conn, "Old Shop", OLD_SITE)
    town.killswitch.stop(town.conn)
    with pytest.raises(TownStopped):
        inspect(town)
    assert town.conn.execute("SELECT COUNT(*) FROM inspections").fetchone()[0] == 0


def test_a_stop_mid_run_stops_before_the_next_step(town):
    add_lead(town.conn, "Old Shop", OLD_SITE)
    add_lead(town.conn, "Good Shop", GOOD_SITE)

    class StopsWhileLoading(FakeMeasurer):
        def measure(self, url, access, shots, identity=None):
            facts = super().measure(url, access, shots, identity)
            town.killswitch.stop(town.conn)   # Charlie runs `town stop` while the first site loads
            return facts

    with pytest.raises(TownStopped):
        inspect(town, measurer=StopsWhileLoading())
    assert town.conn.execute("SELECT COUNT(*) FROM inspections").fetchone()[0] == 0
    assert town.llm._client.calls == 0   # no note gets written after the stop


def test_a_budget_pause_stops_the_run_before_any_record(town, monkeypatch):
    add_lead(town.conn, "Old Shop", OLD_SITE)

    def paused():
        raise PaidWorkPaused("Paid work paused.")

    monkeypatch.setattr(town.budget, "check_paid_allowed", paused)
    with pytest.raises(PaidWorkPaused):
        inspect(town)
    assert town.conn.execute("SELECT COUNT(*) FROM inspections").fetchone()[0] == 0


def test_the_inspector_proposes_nothing(town):
    add_lead(town.conn, "Old Shop", OLD_SITE)
    inspect(town)
    assert town.conn.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 0


# ---------------- safety: only public websites ----------------

def test_only_public_http_websites_are_loaded(monkeypatch):
    for url in ("file:///C:/Windows/win.ini", "ftp://example.com/", "http://localhost:8080/", "http://printer.local/"):
        with pytest.raises(UnsafeURL):
            check_public_url(url)
    addresses = {"intranet.example.com": "10.0.0.5", "loopback.example.com": "127.0.0.1",
                 "public.example.com": "93.184.215.14"}

    def fake_dns(host, *args, **kwargs):
        if host not in addresses:
            raise socket.gaierror("not found")
        return [(None, None, None, "", (addresses[host], 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_dns)
    for url in ("http://intranet.example.com/", "https://loopback.example.com/"):
        with pytest.raises(UnsafeURL):
            check_public_url(url)
    check_public_url("https://public.example.com/")
    with pytest.raises(DomainNotFound):
        check_public_url("https://nowhere.example.com/")


# ---------------- the command ----------------

def test_inspect_command_on_dev_and_refused_to_claude_code_on_the_real_town(dev_instance, monkeypatch):
    monkeypatch.setattr(launcher, "API_CLIENT", FakeHaiku())
    town = lambda *args: runner.invoke(launcher.app, ["--instance", "dev", *args])
    town("resume")
    dev, _ = launcher._open(load_settings(dev_instance), "charlie", dev_instance)
    add_lead(dev.conn, "No Site Co")
    dev.close()
    result = town("inspect")
    assert result.exit_code == 0, result.stdout
    assert "No Site Co" in result.stdout and "100/100 (BUILD-WORTHY)" in result.stdout
    monkeypatch.setenv("CLAUDECODE", "1")
    refused = runner.invoke(launcher.app, ["inspect"])
    assert refused.exit_code == 1 and "Negative" in refused.stdout


# ---------------- the real browser, on the frozen test-set sites ----------------

def _chromium_installed() -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            return os.path.exists(p.chromium.executable_path)
    except Exception:
        return False


@pytest.mark.skipif(not _chromium_installed(), reason="Playwright's Chromium isn't installed")
def test_the_real_measuring_on_frozen_sites(lim):
    """Playwright for real, but every request is answered from the case's own files (no network)."""
    from packs.websites.inspector_measure import FixtureAccess, SiteMeasurer
    from packs.websites.testset_runner import OfflineViolation, load_cases, resolve_case_url
    cases = {c.name: c for c in load_cases()}
    found = {}
    with SiteMeasurer(lim.measure_settings(run_lighthouse=False)) as measurer:
        for name in ("06-auto-repair-outdated", "08-restaurant-broken", "09-plumber-modern"):
            case = cases[name]
            facts = measurer.measure(case.info["website"], FixtureAccess(case, resolve_case_url, OfflineViolation),
                                     None)
            found[name] = (facts, grade("own", facts, lim, YEAR))
    old_facts, old = found["06-auto-repair-outdated"]
    assert old == ["not_mobile_friendly", "no_https", "outdated"]
    assert old_facts["width_at_375"] == 800 and old_facts["copyright_year"] == 2009
    broken_facts, broken = found["08-restaurant-broken"]
    assert broken == ["slow", "broken_links"]   # slow from page weight here (Lighthouse is off in this test)
    assert len(broken_facts["broken_resources"]) == 3 and broken_facts["broken_links"]
    assert len(broken_facts["links_checked"]) == lim.max_pages - 1   # polite: homepage plus 3 links
    assert found["09-plumber-modern"][1] == []


def test_page_weight_never_waits_on_a_response_body():
    """A tracker that keeps a request open made response.body() wait about 4 minutes on a real site."""
    from packs.websites.inspector_measure import _weight_kb

    class Response:
        def __init__(self, length):
            self.headers = {"content-length": length} if length is not None else {}

        def body(self):
            raise AssertionError("reading a body can hang forever")

    assert _weight_kb([Response("1024"), Response(None), Response("oops")], 0) == 1.0
    assert _weight_kb([Response("1024")], 4096) == 4.0   # the browser's own count, when it's larger


def test_only_the_business_own_broken_files_count():
    """Real sites load ads, analytics and video players that the browser cancels; those aren't broken links."""
    from packs.websites.inspector_measure import _broken_resources
    response = lambda url, status: SimpleNamespace(url=url, status=status)
    page = "https://www.shop.example.com/"
    found = _broken_resources(
        [response(page + "images/missing.jpg", 404), response("https://ads.example.net/pixel", 404),
         response(page + "favicon.ico", 404), response(page + "ok.css", 200)],
        ["https://www.google.com/ccm/collect", page + "font.woff"],          # failed (not cancelled) requests
        ["chrome-extension://abc/img.png", "https://cdn.other.example.org/photo.jpg"], page)
    assert found == [page + "images/missing.jpg", page + "font.woff"]


def test_a_listed_site_that_isnt_about_the_business_counts_as_no_site(lim):
    from packs.websites.inspector_measure import mentions_business
    identity = {"name": "Fictional J Dentist Dds", "phone": "+13145550199"}
    assert mentions_business("Welcome to Fictional J. Dentist, DDS - family dentistry", identity)
    assert mentions_business("Call us: (314) 555-0199", identity)
    assert mentions_business("Dr. Fictional Dentist and team", identity)   # every distinctive word
    assert not mentions_business("This domain may be for sale. Related searches: dentist near me", identity)
    assert mentions_business("anything", None) is None
    parked = {**CLEAN, "final_url": "https://parking.example.net/?clickid=1", "mentions_business": False}
    assert grade("own", parked, lim, YEAR) == ["no_site"]
    from packs.websites.inspector import evidence
    assert "doesn't mention the business" in evidence(["no_site"], "own", parked, GOOD_SITE, YEAR)["no_site"][0]


def test_slow_evidence_quotes_lighthouse_weight_when_it_ran():
    from packs.websites.inspector import evidence
    facts = {**CLEAN, "page_kb": 8447.0, "lighthouse": {"performance": 41, "lcp_seconds": 8.0, "total_kb": 4818.5}}
    assert "weigh 4818.5 KB" in " ".join(evidence(["slow"], "own", facts, GOOD_SITE, YEAR)["slow"])
    facts["lighthouse"] = {"error": "Lighthouse took too long"}
    assert "weigh 8447.0 KB" in " ".join(evidence(["slow"], "own", facts, GOOD_SITE, YEAR)["slow"])


def test_lighthouse_runs_twice_only_when_the_first_run_reads_slow(lim, monkeypatch):
    """Polite and honest: a fast first run settles it; a slow first run gets a second look,
    and the better of the two is what the note quotes."""
    from packs.websites import inspector_measure as im

    def measure_with(results):
        queue = list(results)
        measurer = im.SiteMeasurer(lim.measure_settings())
        monkeypatch.setattr(measurer, "_load_and_read",
                            lambda url, access, shots, facts, ignore_https_errors: facts.update(
                                fetched=True, final_url=url))
        monkeypatch.setattr(measurer, "_lighthouse", lambda access, url: queue.pop(0))
        access = type("Access", (), {"check": lambda self, url: None})()
        return measurer.measure(GOOD_SITE, access, None), queue

    fast = {"performance": 95, "lcp_seconds": 1.0}
    facts, left = measure_with([fast, {"performance": 10}])
    assert facts["lighthouse_runs"] == [fast] and left == [{"performance": 10}]   # no second run
    slow1, slow2 = {"performance": 41, "lcp_seconds": 8.0}, {"performance": 51, "lcp_seconds": 4.2}
    facts, left = measure_with([slow1, slow2])
    assert facts["lighthouse_runs"] == [slow1, slow2] and facts["lighthouse"] == slow2   # the better run
    assert "slow" in grade("own", facts, lim, YEAR)


def test_a_bulk_run_skips_held_leads_but_a_named_one_can_still_be_inspected(town):
    held = add_lead(town.conn, "No Email Co", email="")
    emailed = add_lead(town.conn, "Has Email Co")
    run, _ = inspect(town)
    assert [r["lead_id"] for r in run.inspected] == [emailed]   # email-first: a held lead isn't spent on
    run, _ = inspect(town, held)
    assert [r["lead_id"] for r in run.inspected] == [held]


# ---------------- never more than was measured (Charlie, 2026-11-04) ----------------

@pytest.mark.parametrize("said", ["Fresh Bakes has no website anywhere online.", "We couldn't find them anywhere.",
                                  "The bakery is not online at all.", "They have no online presence."])
def test_a_note_that_claims_more_than_was_measured_is_replaced_by_codes_own_sentence(town, said):
    from packs.websites import inspector
    from packs.websites.inspector import write_note
    town.llm._client = FakeHaiku(reply=json.dumps({"why": {"no_site": said}}))
    lead = {"id": add_lead(town.conn, "Fresh Bakes"), "business_name": "Fresh Bakes", "business_type": "bakery",
            "area": "Clayton"}
    note, by, _ = write_note(town, lead, ["no_site"], {"no_site": ["no website is listed"]})
    assert note == "We couldn't find a website listed for Fresh Bakes." and inspector.overclaims(note) is None
    assert town.conn.execute("SELECT 1 FROM events WHERE event_type = 'inspector_overclaim_dropped'").fetchone()


def test_a_measured_sentence_is_kept():
    from packs.websites.inspector import overclaims
    assert overclaims("We couldn't find a website listed for Fresh Bakes.") is None
    assert overclaims("The site loads over plain http, so browsers mark it Not secure.") is None
