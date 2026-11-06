"""The Builder and the `claude -p` runner, with a fake `claude` process. pytest never starts the real
CLI, never uses Charlie's plan, never runs Astro or a browser, and never reaches the network."""
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from engine import claude_cli
from engine.claude_cli import CliTimeout, SubscriptionLimitReached, build_command, clean_env, parse_reset
from engine.db import utc_now
from engine.killswitch import TownStopped
from engine.settings import load_settings
from engine.town import open_town
from packs.websites import builder, pack
from packs.websites.builder import BuilderError, BuilderLimits, run_builder
from packs.websites.builder_check import facts_check, file_hashes, scan_sources
from packs.websites.builder_content import homepage_content, phone_display
from packs.websites.testset_runner import load_cases, resolve_case_url

LEAD = {"business_name": "Marigold & Shears Hair Studio", "business_type": "hair_salon", "area": "Maplewood",
        "address": "7340 Manchester Rd, Maplewood, MO 63143", "phone": "+13145550101", "website": "",
        "public_email": "hello@marigold.example.com"}
GOOD_PAGE = """<!doctype html><html lang="en"><head><meta name="viewport" content="width=device-width">
<meta name="robots" content="noindex, nofollow"><title>Marigold &amp; Shears</title></head><body>
<h1>Marigold &amp; Shears Hair Studio</h1><p>Haircuts, color and styling in Maplewood.</p>
<address>7340 Manchester Rd,<br>Maplewood, MO 63143</address>
<a href="tel:+13145550101">(314) 555-0101</a> <a href="mailto:hello@marigold.example.com">hello@marigold.example.com</a>
<img src="a.svg" alt="Scissors drawing"><p class="preview-note">Preview made by Example Web Studio for Marigold &amp;
Shears Hair Studio. Details to be confirmed by the owner.</p></body></html>"""


# ---------------- a fake `claude` process ----------------

def result_line(text="Done: a bold botanical design.", is_error=False, status=None, session="s-1"):
    return json.dumps({"type": "result", "subtype": "success", "is_error": is_error, "api_error_status": status,
                       "result": text, "session_id": session, "duration_ms": 61000, "duration_api_ms": 58000,
                       "num_turns": 12, "total_cost_usd": 1.25, "permission_denials": [],
                       "usage": {"input_tokens": 20, "output_tokens": 9000, "cache_read_input_tokens": 300000,
                                 "cache_creation_input_tokens": 40000}}) + "\n"


def tool_line(name, tool_input):
    return json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": name, "input": tool_input}]}}) + "\n"


class FakeProcess:
    pid = 4242

    def __init__(self, lines, stderr="", never_ends=False):
        self.stdout, self.stderr = iter(lines), iter([stderr] if stderr else [])
        self.never_ends = never_ends
        self.stdin = self
        self.prompt = ""

    def write(self, text):
        self.prompt += text

    def close(self):
        pass

    def poll(self):
        return None if self.never_ends else 0

    def wait(self, timeout=None):
        return 0


class FakeClaude:
    """Stands in for subprocess.Popen. Each call plays the next script: (output lines, files it writes)."""

    def __init__(self, *scripts, never_ends=False):
        self.scripts = list(scripts)
        self.calls = []
        self.never_ends = never_ends

    def __call__(self, command, cwd, env, **kwargs):
        lines, files = self.scripts.pop(0) if self.scripts else ([result_line()], {})
        self.calls.append({"command": command, "cwd": cwd, "env": env})
        for name, text in files.items():
            path = Path(cwd) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        return FakeProcess(lines, never_ends=self.never_ends)


SITE_FILES = {"src/pages/index.astro": "---\nimport Base from '../layouts/Base.astro';\n---\n<Base />\n"}
DESIGN = ([tool_line("Skill", {"skill": "archlight-builder:frontend-design"}),
           tool_line("Write", {"file_path": "src/pages/index.astro"}), result_line()], SITE_FILES)


@pytest.fixture
def town(instance_file, limits_file, monkeypatch):
    t = open_town(load_settings(instance_file), pack.PACK_SCHEMAS, pack.handlers(), limits_file, pack.MODELS_FILE)
    t.killswitch.resume(t.conn)
    monkeypatch.setattr(claude_cli, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(claude_cli, "kill_tree", lambda process: setattr(process, "killed", True))
    yield t
    t.close()


@pytest.fixture
def fake_site(monkeypatch):
    """Astro and the browser, faked: the 'build' writes the page HTML; measuring returns good numbers."""
    built = {"html": GOOD_PAGE, "builds": 0, "pages": {}}   # pages: any other page, {"privacy/index.html": html}

    def fake_build(workspace, timeout_s):
        built["builds"] += 1
        (workspace / "dist").mkdir(exist_ok=True)
        (workspace / "dist" / "index.html").write_text(built["html"], encoding="utf-8")
        for name, html in built["pages"].items():
            (workspace / "dist" / name).parent.mkdir(parents=True, exist_ok=True)
            (workspace / "dist" / name).write_text(html, encoding="utf-8")
        return True, "built"

    monkeypatch.setattr(builder, "template_ready", lambda: True)
    monkeypatch.setattr(builder, "_link_packages", lambda workspace: None)
    monkeypatch.setattr(builder, "run_astro_build", fake_build)
    monkeypatch.setattr(builder, "measure_site", lambda dist, timeout: {
        "overflows_at_375": False, "width_at_375": 375, "outside_requests": [],
        "lighthouse": {"performance": 99, "accessibility": 100}, "reduced_motion": {"moving": []}})
    return built


def add_lead(town, build_worthy=1, facts=None, final_url=None, lead=LEAD):
    lead_id = town.conn.execute(
        "INSERT INTO leads (business_name, business_type, area, address, phone, website, public_email,"
        " contact_route, source, source_id, source_release, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?,"
        " 'email', 'overture', ?, 'r1', 'new', ?)",
        (lead["business_name"], lead["business_type"], lead["area"], lead["address"], lead["phone"],
         lead["website"], lead["public_email"], f"src-{time.time_ns()}", utc_now())).lastrowid
    town.conn.execute(
        "INSERT INTO inspections (lead_id, final_url, status, problems, need_score, build_worthy, facts, created_at)"
        " VALUES (?, ?, 'done', '[\"no_site\"]', ?, ?, ?, ?)",
        (lead_id, final_url, 100 if build_worthy else 25, build_worthy, json.dumps(facts or {"site_kind": "none"}),
         utc_now()))
    return lead_id


# ---------------- the `claude -p` runner ----------------

def test_the_command_is_locked_down_and_never_skips_permissions():
    command = build_command("claude", "claude-opus-5-5", ["Read", "Write"], ["Edit(./package.json)"],
                            [Path("plugin")], "job", resume="s-1")
    assert command[:3] == ["claude", "-p", "--restricted"] and "--strict-mcp-config" in command
    assert command[command.index("--permission-mode") + 1] == "dontAsk"
    assert command[command.index("--tools") + 1] == "Read,Write"
    assert "--resume" in command and "--plugin-dir" in command and "--disallowedTools" in command
    assert not any("dangerously" in part for part in command)


def test_the_api_key_and_claude_code_markers_never_reach_claude():
    env = clean_env({"PATH": "x", "ANTHROPIC_API_KEY": "sk-secret", "ANTHROPIC_BASE_URL": "http://evil",
                     "CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli", "HOME": "h"})
    assert env == {"PATH": "x", "HOME": "h"}


def test_a_run_records_time_tokens_tools_and_a_zero_dollar_cost(town, tmp_path):
    fake = FakeClaude(DESIGN)
    town.cli._launch = fake
    seen = []
    result = town.cli.run("builder", "Build it", tmp_path, ["Read", "Write"], 60, 60,
                          on_tool=lambda name, tool_input: seen.append(name))
    assert result.model == "claude-opus-5-5" and result.num_turns == 12 and result.usage["output_tokens"] == 9000
    assert result.tool_uses == {"Skill": 1, "Write": 1} and result.skills_used == ["archlight-builder:frontend-design"]
    assert seen == ["Skill", "Write"] and result.api_equivalent_usd == 1.25
    assert "ANTHROPIC_API_KEY" not in fake.calls[0]["env"] and fake.calls[0]["cwd"] == str(tmp_path)
    cost = town.conn.execute("SELECT * FROM costs WHERE agent = 'builder'").fetchone()
    assert cost["service"] == "claude_subscription" and cost["dollar_cost"] == 0
    assert json.loads(cost["units"])["output_tokens"] == 9000


def test_only_agents_on_the_cli_route_can_use_it(town, tmp_path):
    town.cli._launch = FakeClaude()
    with pytest.raises(ValueError, match="not `claude -p`"):
        town.cli.run("scout", "x", tmp_path, ["Read"], 60, 60)


def test_a_usage_limit_is_never_retried_and_blocks_later_jobs(town, tmp_path):
    reset = int((datetime.now(timezone.utc) + timedelta(hours=2)).timestamp())
    fake = FakeClaude(([result_line(f"Claude AI usage limit reached|{reset}", is_error=True)], {}))
    town.cli._launch = fake
    with pytest.raises(SubscriptionLimitReached) as err:
        town.cli.run("builder", "x", tmp_path, ["Read"], 60, 60)
    assert int(err.value.wait_until.timestamp()) == reset and len(fake.calls) == 1   # one call, no retry
    event = town.conn.execute("SELECT details FROM events WHERE event_type = 'subscription_limit_hit'").fetchone()
    assert json.loads(event[0])["retried"] is False
    with pytest.raises(SubscriptionLimitReached):
        town.cli.run("builder", "x", tmp_path, ["Read"], 60, 60)
    assert len(fake.calls) == 1   # refused before launching anything


def test_usage_limit_detection_and_the_fallback_wait():
    now = datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc)
    assert parse_reset("rate limited", now, 60) == now + timedelta(minutes=60)
    assert claude_cli.is_usage_limit({"is_error": True, "api_error_status": 429, "result": "x"}, "")
    assert claude_cli.is_usage_limit(None, "You've hit your limit · resets 3pm")
    assert not claude_cli.is_usage_limit({"is_error": False, "result": "I noted the usage limit"}, "")


def test_town_stop_kills_a_running_claude(town, tmp_path):
    town.cli._launch = FakeClaude(never_ends=True)
    town.cli.check_available = lambda: None   # it started; `town stop` comes a moment later
    town.killswitch.engage("test")
    with pytest.raises(TownStopped):
        town.cli.run("builder", "x", tmp_path, ["Read"], 60, 60)
    assert town.conn.execute("SELECT COUNT(*) FROM events WHERE event_type = 'claude_cli_stopped'").fetchone()[0] == 1


def test_the_time_limit_ends_a_run(town, tmp_path):
    town.cli._launch = FakeClaude(never_ends=True)
    with pytest.raises(CliTimeout):
        town.cli.run("builder", "x", tmp_path, ["Read"], 0.05, 60)


# ---------------- the facts check ----------------

def check(html, tmp_path, lead=LEAD, own_words=""):
    (tmp_path / "index.html").write_text(html, encoding="utf-8")
    return facts_check(tmp_path, lead, own_words)


def test_a_page_with_only_the_real_facts_passes(tmp_path):
    result = check(GOOD_PAGE, tmp_path)
    assert result["passed"] and result["problems"] == [] and result["sample_sections"] == []


@pytest.mark.parametrize("invented", [
    "Rated 4.9 by our clients", "★★★★★", "Read our reviews", "What our clients say: testimonials",
    "Award-winning stylists", "Serving Maplewood since 1998", "Est. 2005", "Over 20 years of experience",
    "Maplewood's #1 salon", "Best in St. Louis", "Haircuts from $25", "Licensed and insured",
    "Certified colorists", "Satisfaction guaranteed", "Free consultations", "Trusted by 500 families",
    "Ask for Dr. Patel",
])
def test_every_invented_claim_fails_the_check(tmp_path, invented):
    result = check(GOOD_PAGE.replace("Haircuts, color and styling", invented), tmp_path)
    assert not result["passed"], invented


def test_a_claim_the_business_makes_itself_is_allowed(tmp_path):
    page = GOOD_PAGE.replace("Haircuts, color and styling", "Serving Maplewood since 1998")
    assert check(page, tmp_path, own_words="Family run, serving Maplewood since 1998.")["passed"]


@pytest.mark.parametrize("change, problem", [
    (("(314) 555-0101", "(314) 555-0199"), "phone"),
    (("tel:+13145550101", "tel:+13145550199"), "phone"),
    (("hello@marigold.example.com</a>", "info@marigold.example.com</a>"), "email"),
    (("7340 Manchester Rd,", "7340 Manchester Road,"), "address"),
    (('<meta name="robots" content="noindex, nofollow">', ""), "noindex"),
    (("<img", '<form><input name="x"></form><img'), "form"),
    (("Preview made by Example Web Studio", "Made by Example Web Studio"), "preview note"),
])
def test_wrong_facts_forms_and_missing_tags_fail(tmp_path, change, problem):
    result = check(GOOD_PAGE.replace(*change), tmp_path)
    assert not result["passed"] and any(problem in p for p in result["problems"]), result


def test_no_email_listed_means_no_email_on_the_page(tmp_path):
    result = check(GOOD_PAGE, tmp_path, lead={**LEAD, "public_email": ""})
    assert not result["passed"] and any("email" in p for p in result["problems"])


# ---------------- the source scan ----------------

@pytest.fixture
def workspace(tmp_path):
    tmp_path = tmp_path / "ws"
    tmp_path.mkdir()
    for name in ("package.json", "astro.config.mjs"):
        (tmp_path / name).write_bytes((builder.TEMPLATE_DIR / name).read_bytes())
    (tmp_path / "src" / "pages").mkdir(parents=True)
    (tmp_path / "src" / "pages" / "index.astro").write_text("---\nimport Base from '../layouts/Base.astro';\n"
                                                           "import '@fontsource-variable/fraunces';\n---\n<Base/>")
    return tmp_path


def test_nothing_may_go_in_public(workspace):
    """Every published site shares showcase.example.com's root, so public/ files could clash (previews.py)."""
    (workspace / "public").mkdir()
    (workspace / "public" / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    assert any("public/" in p for p in scan_sources(workspace, file_hashes(builder.TEMPLATE_DIR)))


def test_a_clean_workspace_passes_the_scan(workspace):
    assert scan_sources(workspace, file_hashes(builder.TEMPLATE_DIR)) == []


@pytest.mark.parametrize("path, text, problem", [
    ("src/pages/x.astro", "---\nconst k = process.env.KEY;\n---", "environment"),
    ("src/pages/x.astro", "<script>fetch('https://t.example.com')</script>", "network"),
    ("src/pages/x.astro", "---\nimport fs from 'node:fs';\n---", "imports"),
    ("src/pages/x.astro", "---\nimport x from 'left-pad';\n---", "imports"),
    ("src/pages/x.astro", '<script src="https://cdn.example.com/a.js"></script>', "outside script"),
    ("src/styles/x.css", "@import url('https://fonts.googleapis.com/css');", "outside"),
    ("src/lib/run.js", "console.log(1)", "only .astro"),
    ("evil.mjs", "x", "unexpected file"),
    ("package.json", "{}", "locked"),
])
def test_unsafe_files_are_refused_before_any_build(workspace, path, text, problem):
    (workspace / path).parent.mkdir(parents=True, exist_ok=True)
    (workspace / path).write_text(text)
    problems = scan_sources(workspace, file_hashes(builder.TEMPLATE_DIR))
    assert any(problem in p for p in problems), problems


# ---------------- whole builds ----------------

def test_a_build_succeeds_and_saves_every_measurement(town, fake_site):
    lead_id = add_lead(town)
    fake = FakeClaude(DESIGN)
    town.cli._launch = fake
    [b] = run_builder(town, lead_id).builds
    assert b["status"] == "built" and b["facts_check"]["passed"] and b["design_skill_used"]
    row = dict(town.conn.execute("SELECT * FROM builds").fetchone())
    assert row["status"] == "built" and row["model"] == "claude-opus-5-5" and row["rounds"] == 1
    assert row["output_tokens"] == 9000 and row["turns"] == 12 and row["wall_seconds"] >= 0
    assert row["api_equivalent_usd"] == 1.25 and json.loads(row["tool_uses"]) == {"Skill": 1, "Write": 1}
    brief = json.loads((Path(row["workspace"]) / "brief.json").read_text(encoding="utf-8"))
    assert brief["business"]["address_exactly"] == LEAD["address"]
    assert brief["business"]["phone_tel_link"] == "tel:+13145550101" and brief["their_own_homepage"] is None
    events = [r[0] for r in town.conn.execute("SELECT event_type FROM events WHERE agent = 'builder'")]
    assert "build_started" in events and "build_done" in events
    command = fake.calls[0]["command"]
    assert "--plugin-dir" in command and str(builder.PLUGIN_DIR) in command
    assert town.conn.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 0   # Builder proposes nothing


def test_an_unsafe_site_goes_back_for_a_fix_round_and_is_never_built(town, fake_site):
    lead_id = add_lead(town)
    unsafe = ([result_line()], {"src/pages/index.astro": "<script>fetch('https://x.example.com')</script>"})
    fixed = ([result_line("Fixed.")], SITE_FILES)
    fake = FakeClaude(unsafe, fixed)
    town.cli._launch = fake
    [b] = run_builder(town, lead_id).builds
    assert b["status"] == "built" and b["rounds"] == 2 and fake_site["builds"] == 1
    second = fake.calls[1]["command"]
    assert second[second.index("--resume") + 1] == "s-1"


def test_a_site_that_never_gets_fixed_fails_after_the_fix_rounds(town, fake_site):
    lead_id = add_lead(town)
    unsafe = ([result_line()], {"src/pages/index.astro": "---\nconst k = process.env.X\n---"})
    town.cli._launch = FakeClaude(unsafe, unsafe, unsafe, unsafe)
    [b] = run_builder(town, lead_id).builds
    assert b["status"] == "failed" and b["rounds"] == 3 and "couldn't fix" in b["failure"]
    assert fake_site["builds"] == 0


def test_a_failed_facts_check_is_a_failed_build_with_the_reason(town, fake_site):
    lead_id = add_lead(town)
    fake_site["html"] = GOOD_PAGE.replace("Haircuts, color and styling", "Award-winning stylists")
    town.cli._launch = FakeClaude(DESIGN)
    [b] = run_builder(town, lead_id).builds
    assert b["status"] == "failed" and b["failure"].startswith("facts check failed") and "award" in b["failure"]
    assert town.conn.execute("SELECT status FROM builds").fetchone()[0] == "failed"


def test_only_build_worthy_leads_are_built(town, fake_site):
    lead_id = add_lead(town, build_worthy=0)
    town.cli._launch = FakeClaude()
    with pytest.raises(BuilderError, match="isn't build-worthy"):
        run_builder(town, lead_id)
    assert run_builder(town).message == "No build-worthy leads are waiting for a site."


def test_a_built_lead_is_not_rebuilt_unless_asked(town, fake_site):
    lead_id = add_lead(town)
    town.cli._launch = FakeClaude(DESIGN, DESIGN)
    run_builder(town, lead_id)
    with pytest.raises(BuilderError, match="--again"):
        run_builder(town, lead_id)
    assert run_builder(town, lead_id, again=True).builds[0]["status"] == "built"


def test_the_daily_build_cap_is_enforced(town, fake_site, limits_file):
    limits_file.write_text(limits_file.read_text(encoding="utf-8").replace("sites_per_day: 5", "sites_per_day: 1"),
                           encoding="utf-8")
    add_lead(town), add_lead(town)
    town.cli._launch = FakeClaude(DESIGN, DESIGN)
    run = run_builder(town)
    assert len(run.builds) == 1 and "wait for tomorrow" in run.message
    assert run_builder(town).builds == []


def test_nothing_builds_while_the_town_is_stopped(town, fake_site):
    lead_id = add_lead(town)
    fake = FakeClaude(DESIGN)
    town.cli._launch = fake
    town.killswitch.stop(town.conn)
    with pytest.raises(TownStopped):
        run_builder(town, lead_id)
    assert fake.calls == [] and town.conn.execute("SELECT COUNT(*) FROM builds").fetchone()[0] == 0


def test_a_usage_limit_mid_build_saves_a_failed_build_and_stops_the_run(town, fake_site):
    add_lead(town), add_lead(town)
    fake = FakeClaude(([result_line("You've hit your usage limit", is_error=True)], {}))
    town.cli._launch = fake
    with pytest.raises(SubscriptionLimitReached):
        run_builder(town)
    assert len(fake.calls) == 1   # the second lead was never started
    assert [r[0] for r in town.conn.execute("SELECT status FROM builds")] == ["failed"]


def test_a_parked_site_is_never_used_as_their_homepage():
    lead = {**LEAD, "id": 7}
    parked = {"id": 1, "problems": "[]", "final_url": "http://ads.example.com/",
              "facts": json.dumps({"site_kind": "own", "mentions_business": False})}
    real = {**parked, "facts": json.dumps({"site_kind": "own", "mentions_business": True})}
    assert builder.lead_subject(lead, parked).own_site_url is None
    assert builder.lead_subject(lead, real).own_site_url == "http://ads.example.com/"


def test_their_homepage_text_and_photos_come_from_the_fixture_only(tmp_path):
    [case] = [c for c in load_cases() if c.name == "07-dentist-outdated"]
    fetched = []

    def fetch(url):
        fetched.append(url)
        return resolve_case_url(case, url).read_bytes()

    content = homepage_content(case.info["website"], fetch, tmp_path)
    assert "Brightwater" in content["text"]
    assert all(url.startswith(case.info["website"]) for url in fetched)
    for photo in content["photos"]:
        assert (tmp_path / photo["file"]).is_file()


def test_phone_display():
    assert phone_display("+13145550142") == {"display": "(314) 555-0142", "tel": "+13145550142"}
    assert phone_display("314-555-0101")["tel"] == "+13145550101"


def test_builder_limits_come_from_limits_yaml(town):
    lim = BuilderLimits.read(town.limits)
    assert (lim.per_day, lim.fix_rounds, lim.min_performance, lim.min_accessibility) == (5, 2, 90, 95)
    assert lim.timeout_s == 30 * 60


# ---------------- opening hours (session E, fix (a)) ----------------

HOURS = "<p>Office hours: Mon - Thu 8:00 AM - 5:00 PM, Fri 8:00 AM - 1:00 PM. Please call to confirm hours.</p>"
THEIR_HOURS = "Office Hours\nMon - Thu: 8:00 - 5:00\nFri: 8:00 - 1:00\nSat - Sun: Closed"


def test_hours_from_their_own_homepage_are_allowed(tmp_path):
    page = GOOD_PAGE.replace("<img", HOURS + "<img")
    assert check(page, tmp_path, own_words=THEIR_HOURS)["passed"]
    assert facts_check(tmp_path, LEAD, THEIR_HOURS, stale_source=True)["passed"]


@pytest.mark.parametrize("invented", [
    "<p>Open Mon - Fri 9am - 6pm</p>", "<p>Open Saturdays</p>", "<p>Evening appointments available</p>",
    "<p>Open 7 days a week</p>", "<p>Closed Sundays</p>",
])
def test_invented_hours_still_fail(tmp_path, invented):
    result = check(GOOD_PAGE.replace("<img", invented + "<img"), tmp_path)
    assert not result["passed"] and any("hours" in p or "opening" in p for p in result["problems"]), result


def test_a_time_their_homepage_never_gives_fails_even_when_they_publish_hours(tmp_path):
    page = GOOD_PAGE.replace("<img", "<p>Mon - Thu 8:00 - 7:00</p><img")
    result = check(page, tmp_path, own_words=THEIR_HOURS)
    assert not result["passed"] and any("7:00" in p for p in result["problems"])


def test_hours_from_an_outdated_site_must_say_call_to_confirm(tmp_path):
    page = GOOD_PAGE.replace("<img", "<p>Mon - Thu 8:00 - 5:00</p><img")
    (tmp_path / "index.html").write_text(page, encoding="utf-8")
    assert facts_check(tmp_path, LEAD, THEIR_HOURS, stale_source=False)["passed"]
    result = facts_check(tmp_path, LEAD, THEIR_HOURS, stale_source=True)
    assert not result["passed"] and any("confirm" in p for p in result["problems"])


def test_a_build_that_ignores_reduced_motion_fails_with_the_reason(town, fake_site, monkeypatch):
    monkeypatch.setattr(builder, "measure_site", lambda dist, timeout: {
        "overflows_at_375": False, "outside_requests": [], "lighthouse": {"performance": 99, "accessibility": 100},
        "reduced_motion": {"moving": ['svg.tooth: animation "wiggle"']}})
    town.cli._launch = FakeClaude(DESIGN)
    [b] = run_builder(town, add_lead(town)).builds
    assert b["status"] == "failed" and "reduced-motion" in b["failure"] and "wiggle" in b["failure"]


# ---------------- reduced motion, in a real browser (127.0.0.1 only) ----------------

def _chromium_installed() -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            return Path(p.chromium.executable_path).exists()
    except Exception:
        return False


WIGGLE = """<!doctype html><html><head><style>
@keyframes wiggle { from { transform: rotate(-8deg) } to { transform: rotate(8deg) } }
.tooth { display:inline-block; animation: wiggle 1.6s ease-in-out infinite; }
a.lift { display:inline-block; transition: transform .3s; } a.lift:hover { transform: translateY(-4px); }
a.fade { transition: color .2s; } a.fade:hover { color: red; }
{override}
</style></head><body><span class="tooth">T</span> <a class="lift" href="#x">Call</a>
<a class="fade" href="#x">Email</a><p id="x">x</p></body></html>"""
RESET = "@media (prefers-reduced-motion: reduce) { *, ::before, ::after { animation: none !important; transition: none !important; } }"


def _motion(tmp_path, html):
    from playwright.sync_api import sync_playwright
    from packs.websites.builder_check import reduced_motion_check, serve_folder
    (tmp_path / "index.html").write_text(html, encoding="utf-8")
    with serve_folder(tmp_path) as url, sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            return reduced_motion_check(browser, url)["moving"]
        finally:
            browser.close()


@pytest.mark.skipif(not _chromium_installed(), reason="Playwright's Chromium isn't installed")
def test_the_reduced_motion_check_catches_an_animation_without_an_override(tmp_path):
    moving = _motion(tmp_path, WIGGLE.replace("{override}", ""))
    assert any("wiggle" in m for m in moving) and any("transform" in m for m in moving), moving
    assert not any("color" in m for m in moving)   # a color fade isn't motion


@pytest.mark.skipif(not _chromium_installed(), reason="Playwright's Chromium isn't installed")
def test_an_animation_with_a_reduced_motion_override_passes(tmp_path):
    assert _motion(tmp_path, WIGGLE.replace("{override}", RESET)) == []


def test_a_reduced_motion_check_that_never_ran_counts_as_failed():
    from packs.websites.builder_check import motion_problems
    assert motion_problems({"error": "browser crashed"}) and not motion_problems({"reduced_motion": {"moving": []}})


def test_builder_is_told_to_use_their_published_facts_with_call_to_confirm():
    job = builder.JOB
    assert "their_own_homepage" in job and "call to confirm" in job.lower() and "opening hours" in job


def test_hours_are_read_even_when_the_day_runs_into_the_time():
    from packs.websites.builder_check import page_text, times_of_day
    text = page_text("<dl><dt>Mon - Thu</dt><dd>8:00 - 5:00</dd><dt>Fri</dt><dd>8:00 - 1:00</dd></dl>")[0]
    assert times_of_day(text) == {(8, 0), (5, 0), (1, 0)}
    assert times_of_day("Thu8:00-5:00") == {(8, 0), (5, 0)}
    assert times_of_day("call (314) 555-0107 at 63033") == set()


def test_builder_is_told_every_animation_must_respect_reduced_motion():
    job = builder.JOB.format(fonts="")
    assert "prefers-reduced-motion: reduce" in job and "animations are welcome" in job


def test_the_builder_instructions_format_cleanly():
    text = builder.JOB.format(fonts="@fontsource-variable/fraunces")
    assert "{ *, ::before" in text and "{fonts}" not in text


def test_a_held_lead_never_gets_a_site(town):
    """Email-first (2026-11-03): a lead with no public email is held and never contacted, so no build."""
    held = add_lead(town, lead={**LEAD, "public_email": ""})
    emailed = add_lead(town)
    assert [s.lead_id for s in builder._subjects(town, None, False)] == [emailed]
    with pytest.raises(BuilderError, match=r"held \(no email\)"):
        builder._subjects(town, held, False)
