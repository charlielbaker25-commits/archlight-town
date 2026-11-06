"""The test set: the cases follow the rules, scoring is right, runs are saved, and a run
never reaches the internet. A fake Haiku stands in for the API: no network, no real cost."""
import json
import re
import shutil
import socket
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import launcher
from engine.killswitch import TownStopped
from engine.settings import load_settings
from engine.town import open_town
from packs.websites import pack, testset_runner
from packs.websites.testset_runner import (CASES_DIR, OfflineViolation, TestsetError, load_cases,
                                           resolve_case_url, run_testset)

runner = CliRunner()
# The answer key's picks: Scout should pick 01-05 and pass on (reject) 10 and 11.
KEY_PICKS = {f"testset-0{i}" for i in range(1, 6)}
# A Scout that only looks at "no website": right on 01-03, wrong on 04-05 (not picked) and 10-11 (picked).
NO_SITE_IDS = {"testset-01", "testset-02", "testset-03", "testset-10", "testset-11"}


class TakeTurnsHaiku:
    """A different FakeHaiku for each call, in turn: Haiku answering differently run to run."""

    def __init__(self, *fakes):
        self.fakes = fakes
        self.calls = 0
        self.messages = self

    def create(self, **request):
        fake = self.fakes[self.calls % len(self.fakes)]
        self.calls += 1
        return fake.create(**request)


class FakeHaiku:
    """Picks the candidates whose source_id is in `picks` (or every candidate if picks is None)."""

    def __init__(self, picks=None, reply=None):
        self.picks = picks
        self.reply = reply
        self.calls = 0
        self.messages = self

    def create(self, **request):
        self.calls += 1
        self.last_request = request
        shortlist = json.loads(request["messages"][0]["content"].split("Candidates:\n", 1)[1])
        chosen = [c for c in shortlist if self.picks is None or c["source_id"] in self.picks]
        text = self.reply if self.reply is not None else json.dumps(
            {"picks": [{"source_id": c["source_id"], "why": f"{c['name']} looks like a fit."} for c in chosen]})
        usage = SimpleNamespace(input_tokens=2000, output_tokens=400,
                                cache_creation_input_tokens=0, cache_read_input_tokens=0)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], usage=usage)


CLEAN_SITE = {"fetched": True, "status": 200, "viewport_tag": "width=device-width, initial-scale=1",
              "overflows_at_375": False, "http_form_actions": [], "copyright_year": 2026, "dated_markers": [],
              "broken_resources": [], "broken_links": [], "page_kb": 5.0,
              "lighthouse": {"performance": 100, "lcp_seconds": 0.9},
              "lighthouse_runs": [{"performance": 100, "lcp_seconds": 0.9}]}
# What the real browser measures on the four frozen sites of their own (06-09), so pytest never opens one.
MEASURED = {
    "06-auto-repair-outdated": {**CLEAN_SITE, "final_url": "http://www.oakhollowauto.example.com/",
                                "viewport_tag": None, "overflows_at_375": True, "copyright_year": 2009,
                                "dated_markers": ["marquee", "font", "best_viewed_in"]},
    "07-dentist-outdated": {**CLEAN_SITE, "final_url": "http://www.brightwaterdental.example.com/",
                            "viewport_tag": None, "overflows_at_375": True, "copyright_year": 2014,
                            "http_form_actions": ["http://www.brightwaterdental.example.com/cgi-bin/formmail.pl"]},
    "08-restaurant-broken": {**CLEAN_SITE, "final_url": "https://www.casatovarelli.example.com/",
                             "copyright_year": 2022, "broken_links": ["menu.html"], "page_kb": 1203.2,
                             "broken_resources": ["images/lasagna.jpg"],
                             "lighthouse": {"performance": 69, "lcp_seconds": 7.2},
                             "lighthouse_runs": [{"performance": 68, "lcp_seconds": 7.3},
                                                 {"performance": 69, "lcp_seconds": 7.2}]},
    "09-plumber-modern": {**CLEAN_SITE, "final_url": "https://www.clearlineplumbing.example.com/"},
}


class FakeSiteMeasurer:
    """Stands in for the browser: answers each case from MEASURED, and checks the case's own URL
    goes through resolve_case_url() (the offline rule) like the real one."""

    def __init__(self, measured):
        self.measured = measured
        self.seen = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def measure(self, url, access, shots, identity=None):
        access.check(url)
        self.seen.append(access.case.name)
        return dict(self.measured[access.case.name])


@pytest.fixture(autouse=True)
def fake_browser(monkeypatch):
    fake = FakeSiteMeasurer(MEASURED)
    monkeypatch.setattr(testset_runner, "open_measurer", lambda town, lim: fake)
    return fake


def by_agent(result):
    return {s.agent: s for s in result.scores}


@pytest.fixture
def make_town(instance_file, limits_file):
    opened = []

    def make(fake):
        t = open_town(load_settings(instance_file), pack.PACK_SCHEMAS, pack.handlers(), limits_file,
                      pack.MODELS_FILE, api_client=fake)
        t.killswitch.resume(t.conn)
        opened.append(t)
        return t

    yield make
    for t in opened:
        t.close()


@pytest.fixture
def no_network(monkeypatch):
    """Any attempt to open a network connection is recorded and refused."""
    attempts = []

    def refuse(*args, **kwargs):
        attempts.append(args[1:] if args else kwargs)
        raise OSError("a test-set run tried to reach the network")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    return attempts


# ---------------- the cases themselves ----------------

def test_cases_are_fictional_and_cover_every_situation():
    cases = load_cases()
    assert len(cases) == 11
    for case in cases:
        info = case.info
        assert re.fullmatch(r"\d{3}-555-01\d\d", info["phone"]), case.name
        assert info["public_email"] == "" or info["public_email"].endswith("@example.com"), case.name
        website = info["website"]
        assert website == "" or ".example.com" in website or "fictional" in website, case.name
        assert info["source"] == "testset" and info["source_id"].startswith("testset-")
    answers = [c.answer for c in cases]
    assert sum(a["worth_pursuing"] == "yes" for a in answers) == 8
    assert sum(a["scout"] == "reject" for a in answers) == 2
    assert {c.name for c in cases if c.answer["site_problems"] == "not_inspected"} == {
        "10-garage-door-bad-lead", "11-oil-change-franchise"}
    assert any(c.answer["site_problems"] == [] for c in cases)  # the modern site


def test_the_franchise_case_keeps_its_store_number():
    # Unquoted, YAML reads ` #214` as a comment and the giveaway silently disappears.
    franchise = next(c for c in load_cases() if c.name == "11-oil-change-franchise")
    assert franchise.info["business_name"] == "QuickTurn Oil & Lube #214"


def test_fixture_sites_load_nothing_from_outside():
    linked = re.compile(r"""(?:src|href|action)\s*=\s*["']([^"']+)["']|url\(\s*["']?([^"')]+)""", re.I)
    for path in CASES_DIR.rglob("site/**/*"):
        if path.suffix not in (".html", ".css", ".svg"):
            continue
        for match in linked.finditer(path.read_text(encoding="utf-8")):
            target = match.group(1) or match.group(2)
            if re.match(r"(https?:)?//", target):
                assert ".example.com" in target, f"{path}: outside link {target}"


def test_answer_key_typos_are_refused(tmp_path):
    shutil.copytree(CASES_DIR / "01-salon-no-site", tmp_path / "01-case")
    answer = tmp_path / "01-case" / "answer.yaml"
    answer.write_text(answer.read_text(encoding="utf-8").replace("scout: pick", "scout: maybe"), encoding="utf-8")
    with pytest.raises(TestsetError, match="scout must be one of"):
        load_cases(tmp_path)


# ---------------- the offline rule ----------------

def test_every_case_url_resolves_to_its_local_fixture():
    for case in load_cases():
        website = case.info["website"]
        if not website:
            with pytest.raises(OfflineViolation):
                resolve_case_url(case, "https://anything.example.com")
            continue
        page = resolve_case_url(case, website)
        assert page == (case.folder / case.info["fixture"]).resolve() and page.is_file()


def test_outside_urls_are_refused_and_links_stay_local():
    cases = {c.name: c for c in load_cases()}
    restaurant = cases["08-restaurant-broken"]
    menu = resolve_case_url(restaurant, "menu.html")
    assert menu.parent == (restaurant.folder / "site").resolve() and not menu.exists()  # a broken link
    assert resolve_case_url(restaurant, "https://www.casatovarelli.example.com/style.css").is_file()
    for url in ("https://www.facebook.com/someone-real", "http://example.org/", "//cdn.example.net/x.js",
                "../../../../limits.yaml"):
        with pytest.raises(OfflineViolation):
            resolve_case_url(restaurant, url)
    barber = cases["04-barber-social-page"]
    assert resolve_case_url(barber, barber.info["website"]).is_file()  # facebook.com URL -> local copy


def test_a_testset_run_makes_no_outside_network_request(dev_instance, no_network, monkeypatch):
    fake = FakeHaiku(picks=KEY_PICKS)
    monkeypatch.setattr(launcher, "API_CLIENT", fake)
    assert runner.invoke(launcher.app, ["--instance", "dev", "resume"]).exit_code == 0
    result = runner.invoke(launcher.app, ["--instance", "dev", "testset"])
    assert result.exit_code == 0, result.stdout
    assert fake.calls == 3 and no_network == []  # --runs defaults to 3


# ---------------- scoring ----------------

def test_scout_agreeing_with_the_key_scores_full_marks(make_town):
    town = make_town(FakeHaiku(picks=KEY_PICKS))
    result = run_testset(town)
    scout = by_agent(result)["scout"]
    assert scout.parts == {"picks": [7, 7], "contact route": [9, 9]}
    assert (scout.earned, scout.possible) == (16, 16) and scout.misses == []
    assert (result.earned, result.possible) == (25, 25)  # scout 16 + inspector 9
    assert scout.model == "claude-haiku-5-5" and result.cost_usd > 0


def test_scout_picking_only_no_site_leads_misses_the_bad_ones(make_town):
    scout = by_agent(run_testset(make_town(FakeHaiku(picks=NO_SITE_IDS))))["scout"]
    assert scout.parts["picks"] == [4, 7]
    # Case 10 (out-of-area phone + no street address) is skipped by plain code before Haiku sees it.
    assert not any("10-garage-door-bad-lead" in m for m in scout.misses)
    assert any("11-oil-change-franchise: expected reject, got pick" in m for m in scout.misses)
    assert any("04-barber-social-page: expected pick, got reject" in m for m in scout.misses)


def test_inspector_decides_cases_never_count_for_scout(make_town):
    picks_everything = by_agent(run_testset(make_town(FakeHaiku(picks=None))))["scout"]
    assert picks_everything.parts["picks"] == [6, 7]  # 01-05 and 10 (code skip) right, 11 wrong; 06-09 not scored


def test_an_unreadable_reply_is_scored_and_flagged(make_town):
    scout = by_agent(run_testset(make_town(FakeHaiku(reply="no idea"))))["scout"]
    assert "couldn't be read" in scout.note


def test_each_run_is_saved_with_commit_cost_and_compared(make_town):
    town = make_town(FakeHaiku(picks=KEY_PICKS))
    first = run_testset(town)
    town.llm._client = FakeHaiku(picks=NO_SITE_IDS)
    second = run_testset(town)
    assert first.previous is None and second.previous["id"] == first.run_id
    row = town.conn.execute("SELECT * FROM testset_runs WHERE id = ?", (second.run_id,)).fetchone()
    assert row["git_commit"] and row["cases"] == 11 and row["cost_usd"] > 0
    assert json.loads(row["scores"])["agents"]["scout"]["parts"]["picks"] == [4, 7]
    events = town.conn.execute("SELECT COUNT(*) FROM events WHERE event_type = 'testset_run'").fetchone()[0]
    assert events == 2
    assert town.conn.execute("SELECT COUNT(*) FROM costs WHERE agent = 'scout'").fetchone()[0] == 6  # 2 x 3 runs


def test_a_testset_run_proposes_nothing(make_town):
    town = make_town(FakeHaiku(picks=KEY_PICKS))
    run_testset(town)
    assert town.conn.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 0
    assert town.conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 0


def test_testset_does_nothing_while_the_town_is_stopped(make_town):
    fake = FakeHaiku(picks=KEY_PICKS)
    town = make_town(fake)
    town.killswitch.stop(town.conn)
    with pytest.raises(TownStopped):
        run_testset(town)
    assert fake.calls == 0
    assert town.conn.execute("SELECT COUNT(*) FROM testset_runs").fetchone()[0] == 0


# ---------------- the command ----------------

def test_command_prints_scores_slots_and_comparison(dev_instance, monkeypatch):
    monkeypatch.setattr(launcher, "API_CLIENT", FakeHaiku(picks=KEY_PICKS))
    town = lambda *args: runner.invoke(launcher.app, ["--instance", "dev", *args])
    town("resume")
    first = town("testset").stdout
    assert "scout" in first and "16/16" in first and "100%" in first
    assert "inspector" in first and "not built yet (Session C)" not in first
    monkeypatch.setattr(launcher, "API_CLIENT", FakeHaiku(picks=NO_SITE_IDS))
    second = " ".join(town("testset").stdout.split())  # the test terminal wraps long lines
    assert "WORSE than the last run" in second and "Misses:" in second and "[in 3/3 runs]" in second
    history = " ".join(town("testset", "--history").stdout.split())  # the test terminal wraps long lines
    assert "#1" in history and "#2" in history and "scout 13/16" in history


def test_claude_code_is_refused_testset_on_the_real_town(dev_instance, monkeypatch):
    fake = FakeHaiku(picks=KEY_PICKS)
    monkeypatch.setattr(launcher, "API_CLIENT", fake)
    monkeypatch.setenv("CLAUDECODE", "1")
    result = runner.invoke(launcher.app, ["testset"])
    assert result.exit_code == 1 and "Negative" in result.stdout
    assert fake.calls == 0
    runner.invoke(launcher.app, ["--instance", "dev", "resume"])
    assert runner.invoke(launcher.app, ["--instance", "dev", "testset", "--runs", "1"]).exit_code == 0
    assert fake.calls == 1


# ---------------- several runs ----------------

def test_several_runs_report_the_average_and_the_lowest(make_town):
    town = make_town(TakeTurnsHaiku(FakeHaiku(picks=KEY_PICKS), FakeHaiku(picks=KEY_PICKS),
                                    FakeHaiku(picks=NO_SITE_IDS)))
    result = run_testset(town, runs=3)
    scout = by_agent(result)["scout"]
    assert result.runs == 3 and scout.runs == 3
    assert scout.earned == round((16 + 16 + 13) / 3, 2) and scout.lowest == 13 and scout.possible == 16
    assert scout.parts["picks"] == [round((7 + 7 + 4) / 3, 2), 7]
    assert any("11-oil-change-franchise: expected reject, got pick" in m and "[in 1/3 runs]" in m
               for m in scout.misses)
    row = town.conn.execute("SELECT * FROM testset_runs WHERE id = ?", (result.run_id,)).fetchone()
    assert row["runs"] == 3 and row["earned"] == round(scout.earned + 9, 2)  # + the inspector's 9
    assert json.loads(row["scores"])["agents"]["scout"]["lowest"] == 13
    event = json.loads(town.conn.execute(
        "SELECT details FROM events WHERE event_type = 'testset_run'").fetchone()[0])
    assert event["runs"] == 3 and event["cost_usd"] > 0


def test_holds_or_improves_compares_averages(dev_instance, monkeypatch):
    town = lambda *args: " ".join(runner.invoke(launcher.app, ["--instance", "dev", *args]).stdout.split())
    town("resume")
    monkeypatch.setattr(launcher, "API_CLIENT", FakeHaiku(picks=KEY_PICKS))
    town("testset", "--runs", "1")
    # One bad run in three still lowers the average, so it's reported as worse.
    monkeypatch.setattr(launcher, "API_CLIENT", TakeTurnsHaiku(FakeHaiku(picks=KEY_PICKS),
                                                               FakeHaiku(picks=KEY_PICKS),
                                                               FakeHaiku(picks=NO_SITE_IDS)))
    out = town("testset", "--runs", "3")
    assert "avg 15/16" in out and "lowest 13/16" in out and "WORSE than the last run" in out
    assert "x1" in town("testset", "--history") and "x3" in town("testset", "--history")


def test_zero_runs_is_refused(make_town):
    with pytest.raises(TestsetError, match="at least 1"):
        run_testset(make_town(FakeHaiku(picks=KEY_PICKS)), runs=0)


def test_an_older_database_gets_the_runs_column(tmp_path):
    import sqlite3

    from packs.websites.schema import upgrade
    conn = sqlite3.connect(tmp_path / "old.db")
    conn.execute("CREATE TABLE testset_runs (id INTEGER PRIMARY KEY, ts TEXT, instance TEXT, git_commit TEXT,"
                 " cases INTEGER, earned REAL, possible REAL, cost_usd REAL, scores TEXT)")
    conn.execute("INSERT INTO testset_runs VALUES (1, 't', 'dev', 'abc', 11, 15, 16, 0.001, '{}')")
    upgrade(conn)
    upgrade(conn)  # safe to run again
    assert conn.execute("SELECT runs FROM testset_runs").fetchone()[0] == 1  # old runs count as one run


# ---------------- the Inspector's slot ----------------

def test_the_inspector_is_scored_on_inspected_cases_only(make_town, fake_browser):
    inspector = by_agent(run_testset(make_town(FakeHaiku(picks=KEY_PICKS)), runs=1))["inspector"]
    assert inspector.parts == {"site problems": [9, 9]} and inspector.misses == []
    # Only the four sites of their own are loaded; no-site, social and builder cases are judged from the URL,
    # and the two not_inspected cases (10, 11) are never looked at.
    assert sorted(fake_browser.seen) == sorted(MEASURED)
    assert inspector.cost_usd == 0 and "plain code" in inspector.model


def test_a_wrong_or_extra_problem_is_a_miss(make_town, fake_browser):
    fake_browser.measured = {**MEASURED, "09-plumber-modern": {**MEASURED["09-plumber-modern"],
                                                               "copyright_year": 2019},
                             "08-restaurant-broken": {**MEASURED["08-restaurant-broken"], "broken_links": [],
                                                      "broken_resources": []}}
    inspector = by_agent(run_testset(make_town(FakeHaiku(picks=KEY_PICKS)), runs=1))["inspector"]
    assert inspector.parts == {"site problems": [7, 9]}
    assert any("09-plumber-modern: expected [], got [outdated] (extra outdated)" in m for m in inspector.misses)
    assert any("08-restaurant-broken" in m and "missed broken_links" in m for m in inspector.misses)


def test_outdated_is_judged_against_the_year_the_fixtures_were_frozen():
    assert testset_runner.TESTSET_YEAR == 2026
