"""Autopilot (Charlie, 2026-11-04): the Mayor passes approved work to the next agent by itself, and never decides.

The agents' heavy work (a build, a draft, a Scout run) is replaced by recorders where a test only needs to know
it was asked for; the gates and caps are the real ones (the orders' checks). Fake Gmail and Cloudflare; a counting
fake model, so a test can prove a quiet cycle makes no model call."""
import json
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import launcher
from engine.claude_cli import LIMIT_EVENT, SubscriptionLimitReached
from engine.db import utc_now
from engine.events import log_event
from engine.killswitch import TownStopped
from engine.settings import load_settings
from packs.websites import autopilot, golive, orders

runner = CliRunner()


class CountingModel:
    """Counts every model call (the Guard's soft check included) and answers with no flags."""

    def __init__(self):
        self.calls = 0
        self.messages = self

    def create(self, **request):
        self.calls += 1
        usage = SimpleNamespace(input_tokens=10, output_tokens=5, cache_creation_input_tokens=0, cache_read_input_tokens=0)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text='{"flags": []}')], usage=usage)


@pytest.fixture
def model(monkeypatch):
    fake = CountingModel()
    monkeypatch.setattr(launcher, "API_CLIENT", fake)
    monkeypatch.setattr(launcher, "_api_key", lambda: "sk-test")
    return fake


@pytest.fixture
def town(instance_file, limits_file, monkeypatch, model):
    monkeypatch.setattr(launcher, "LIMITS_FILE", limits_file)
    t, publisher = launcher._open(load_settings(instance_file), "charlie", instance_file)
    t.killswitch.resume(t.conn)
    t.publisher = publisher
    t.gmail = t.executor.handlers["send_email"].run.__self__.client
    yield t
    t.close()


@pytest.fixture
def recorded(monkeypatch):
    """The orders' run() replaced by a recorder; a 'build' also writes a builds row, like a real build would."""
    calls = []

    def fake_run(town, order):
        calls.append((order.name, dict(order.params)))
        if order.name == "build":
            town.conn.execute("INSERT INTO builds (lead_id, workspace, status, created_at) VALUES (?, 'w', 'built', ?)",
                              (order.params["lead_id"], utc_now()))
        return f"{order.name} done"
    monkeypatch.setattr(orders.ORDERS, "run", fake_run)
    monkeypatch.setattr(orders, "template_ready", lambda: True)
    return calls


def add_lead(town, name, email="owner@example.com", worthy=True):
    lead_id = town.conn.execute(
        "INSERT INTO leads (business_name, business_type, area, address, phone, website, public_email, contact_route,"
        " source, source_id, source_release, created_at) VALUES (?, 'bakery', 'Clayton', '1 Main St', '+13145550100',"
        " '', ?, ?, 'overture', ?, 'r', ?)", (name, email, "email" if email else "call", f"src-{name}",
                                               utc_now())).lastrowid
    if worthy is not None:
        town.conn.execute("INSERT INTO inspections (lead_id, status, problems, need_score, build_worthy, created_at)"
                          " VALUES (?, 'done', '[\"no_site\"]', ?, ?, ?)", (lead_id, 100 if worthy else 10,
                                                                           1 if worthy else 0, utc_now()))
    return lead_id


def waiting_lead_proposal(town):
    return town.mayor.propose("scout", "add_lead", "Waiting Bakery", {"business_name": "Waiting Bakery",
                              "source_id": "x1", "public_email": "w@example.com"}, "a lead for Charlie")


def flat(text):
    """Terminal output with Rich's line wrapping undone."""
    return " ".join(text.split())


def cycle(town):
    return autopilot.run_cycle(town, town.gmail, town.publisher)


# ---------------- it never decides ----------------

def test_autopilot_never_approves_rejects_or_edits_anything(town, recorded):
    for n in range(4):
        add_lead(town, f"Bakery {n}")
    proposed = [waiting_lead_proposal(town)]
    result = cycle(town)
    assert [c[0] for c in recorded].count("build") == 3
    assert all(town.mayor.get(i)["status"] == "proposed" for i in proposed)
    decided = town.conn.execute("SELECT COUNT(*) FROM events WHERE event_type IN ('action_approved', 'action_rejected',"
                                " 'action_edited_and_approved', 'execution_started')").fetchone()[0]
    assert decided == 0 and result.waiting == 1 and "Waiting for you: 1" in result.line
    source = (launcher.ROOT / "packs/websites/autopilot.py").read_text(encoding="utf-8")
    for decision in ("mayor.approve(", "mayor.reject(", "edit_and_approve(", "executor", ".execute(action"):
        assert decision not in source, decision


def test_autopilot_never_runs_while_the_town_is_stopped(town, recorded, model):
    add_lead(town, "Bakery")
    town.killswitch.stop(town.conn)
    with pytest.raises(TownStopped):
        cycle(town)
    assert recorded == [] and model.calls == 0
    assert not town.conn.execute("SELECT 1 FROM events WHERE event_type = 'autopilot_cycle_started'").fetchone()


def test_a_stop_mid_cycle_ends_it_before_the_next_step(town, recorded, monkeypatch):
    add_lead(town, "Bakery")
    real_inbox = autopilot._inbox
    monkeypatch.setattr(autopilot, "_inbox", lambda t, c: (t.killswitch.stop(t.conn), real_inbox)[0])
    with pytest.raises(TownStopped):
        cycle(town)
    assert recorded == []                                   # no build after the stop


# ---------------- caps ----------------

def test_builds_stop_at_the_lower_of_autopilot_and_building_caps_oldest_lead_first(town, recorded, limits_file):
    leads = [add_lead(town, f"Bakery {n}") for n in range(5)]
    cycle(town)
    assert [p["lead_id"] for name, p in recorded if name == "build"] == leads[:3]      # autopilot.builds_per_day 3
    text = limits_file.read_text(encoding="utf-8").replace("sites_per_day: 5", "sites_per_day: 4")
    limits_file.write_text(text.replace("builds_per_day: 3", "builds_per_day: 9"), encoding="utf-8")
    recorded.clear()
    town.limits = type(town.limits)(limits_file)
    result = cycle(town)
    assert [p["lead_id"] for name, p in recorded if name == "build"] == [leads[3]]     # 4 a day: one more
    assert any("builds today" in s for s in result.skipped)


def test_held_and_unworthy_leads_are_never_built(town, recorded):
    add_lead(town, "No Email", email="")
    add_lead(town, "Not Worth It", worthy=False)
    cycle(town)
    assert [c for c in recorded if c[0] == "build"] == []


def test_the_inspector_and_scout_caps_come_from_their_own_checks(town, recorded, limits_file, monkeypatch):
    add_lead(town, "Fresh", worthy=None)                    # not inspected yet
    text = limits_file.read_text(encoding="utf-8").replace("inspections_per_day: 25", "inspections_per_day: 0")
    limits_file.write_text(text.replace("new_leads_per_day: 50", "new_leads_per_day: 0"), encoding="utf-8")
    town.limits = type(town.limits)(limits_file)
    town.settings.overture.places_file.parent.mkdir(parents=True, exist_ok=True)
    town.settings.overture.places_file.write_bytes(b"")     # Scout's data "exists"; the cap still says no
    monkeypatch.setattr(autopilot.explore, "auto_pick", lambda t: SimpleNamespace(business_type="bakery",
                                                                                   area="any", mode="explore"))
    result = cycle(town)
    assert recorded == []
    assert any("inspections today; Fresh waits for tomorrow" in s for s in result.skipped)
    assert any("lead cap" in s for s in result.skipped)


def test_scout_runs_only_when_leads_run_low_and_none_are_waiting_for_charlie(town, recorded, monkeypatch):
    monkeypatch.setattr(autopilot.explore, "auto_pick", lambda t: SimpleNamespace(business_type="bakery",
                                                                                   area="any", mode="explore"))
    town.settings.overture.places_file.parent.mkdir(parents=True, exist_ok=True)
    town.settings.overture.places_file.write_bytes(b"")
    cycle(town)
    assert [c[0] for c in recorded] == ["scout"]
    recorded.clear()
    waiting_lead_proposal(town)                             # one waiting for Charlie: no more Scout
    cycle(town)
    assert recorded == []


# ---------------- a plan usage limit stops the plan steps, nothing else ----------------

def test_a_usage_limit_wait_skips_builds_and_drafts_and_the_rest_carries_on(town, recorded):
    add_lead(town, "Bakery")
    until = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    log_event(town.conn, "builder", LIMIT_EVENT, {"wait_until": until})
    result = cycle(town)
    assert recorded == [] and "usage limit" in result.plan_paused
    assert town.conn.execute("SELECT 1 FROM events WHERE event_type = 'clerk_finished'").fetchone()   # inbox still ran


def test_a_usage_limit_hit_during_a_build_stops_the_other_builds(town, recorded, monkeypatch):
    for n in range(3):
        add_lead(town, f"Bakery {n}")
    calls = []

    def hit(t, order):
        calls.append(order.name)
        raise SubscriptionLimitReached("plan limit", datetime.now(timezone.utc) + timedelta(hours=1))
    monkeypatch.setattr(orders.ORDERS, "run", hit)
    result = cycle(town)
    assert calls == ["build"] and result.plan_paused == "plan limit"


# ---------------- quiet cycles, rejections, and who may run it ----------------

def test_a_cycle_with_nothing_to_do_makes_no_model_call(town, model, monkeypatch):
    waiting_lead_proposal(town)                             # leads are waiting for Charlie: no Scout
    before = model.calls
    result = cycle(town)
    assert model.calls == before and "nothing new to do" in result.line
    assert town.conn.execute("SELECT COUNT(*) FROM costs").fetchone()[0] == 0


def test_an_email_charlie_rejected_isnt_redrafted_by_the_next_cycle(town):
    lead_id = add_lead(town, "Bakery")
    rejected = town.mayor.propose("messenger", "send_email", "Bakery", {"kind": "first", "lead_id": lead_id}, "x")
    town.conn.execute("UPDATE actions SET status = 'rejected', reject_reason = 'tone' WHERE id = ?", (rejected,))
    assert autopilot.rejected_email_leads(town, follow_up=False) == {lead_id}
    assert autopilot.rejected_email_leads(town, follow_up=True) == set()


def test_town_autopilot_is_refused_to_claude_code_on_the_real_town_and_runs_once_on_dev(dev_instance, monkeypatch, model):
    monkeypatch.setenv("CLAUDECODE", "1")
    result = runner.invoke(launcher.app, ["autopilot", "--once"])
    assert result.exit_code == 1 and "Charlie's call" in result.stdout
    town = lambda *args: runner.invoke(launcher.app, ["--instance", "dev", *args])
    assert "never runs while it's stopped" in flat(town("autopilot", "--once").stdout)   # a new Town starts stopped
    town("resume")
    t, _ = launcher._open(load_settings(dev_instance), "charlie", dev_instance)
    waiting_lead_proposal(t)
    t.close()
    result = town("autopilot", "--once")
    assert result.exit_code == 0 and "Autopilot cycle: nothing new to do. Waiting for you: 1" in flat(result.stdout)
    t, _ = launcher._open(load_settings(dev_instance), "charlie", dev_instance)
    try:
        status = autopilot.status(t)
        assert status["on"] is False and status["waiting_for_you"] == 1 and status["last_cycle"]
        assert autopilot.status_line(t).startswith("Autopilot: off, last cycle")
    finally:
        t.close()


def test_a_test_or_other_instance_file_is_refused(instance_file):
    result = runner.invoke(launcher.app, ["autopilot", "--once"])          # charlie, but not instance.yaml itself
    assert result.exit_code == 1 and "real Town (run by Charlie) or the dev instance" in flat(result.stdout)


def test_the_state_file_reads_off_when_the_loop_is_gone(town):
    autopilot.write_state(town.settings, True, "charlie", datetime.now(timezone.utc) + timedelta(minutes=30))
    assert autopilot.status(town)["on"] is True
    autopilot.write_state(town.settings, True, "charlie", datetime.now(timezone.utc) - timedelta(hours=2))
    assert autopilot.status(town)["on"] is False                            # crashed: stale = off


# ---------------- shadow days ----------------

def test_a_cycle_counts_toward_a_shadow_day_like_the_commands(town, recorded):
    golive.start_shadow(town.conn, "charlie")
    cycle(town)
    assert golive.today_steps(town.conn, golive.shadow_started(town.conn))["inbox"] is True    # the Clerk's own event
    for kind in ("scout_run", "build_done", "gmail_draft_created"):
        log_event(town.conn, "x", kind, {})
    assert autopilot.shadow_steps_after(town)                                 # the same rule logs the day


# ---------------- one click (Charlie, 2026-11-05): packages, not separate publishes and first emails ----------------

def test_autopilot_builds_ask_for_no_proposal_and_its_package_step_makes_the_one_proposal(town, recorded, monkeypatch):
    lead_id = add_lead(town, "Bakery")
    made = []
    monkeypatch.setattr(autopilot, "ready_builds", lambda t: [41] if not made else [])
    monkeypatch.setattr(autopilot, "propose_package", lambda t, build_id: made.append(build_id) or 77)
    result = cycle(town)
    assert ("build", {"lead_id": lead_id, "after_pass": "none"}) in recorded      # no publish proposal from a build
    assert made == [41] and any("packages proposed (site + email): build #41 as #77" in r for r in result.ran)
    assert not [c for c in recorded if c[0] in ("draft", "propose_publish")]    # never a separate first email


def test_a_build_that_can_t_be_packaged_is_skipped_with_the_reason(town, recorded, monkeypatch):
    from packs.websites.package import PackageError
    monkeypatch.setattr(autopilot, "ready_builds", lambda t: [5])

    def refuse(t, build_id):
        raise PackageError("Example Web Studio's homepage isn't live yet.")
    monkeypatch.setattr(autopilot, "propose_package", refuse)
    result = cycle(town)
    assert any("package for build #5: Example Web Studio's homepage isn't live yet." in s for s in result.skipped)


def test_autopilot_never_drafts_a_separate_first_email(town, recorded, monkeypatch):
    lead_id = add_lead(town, "Bakery")
    ready = lambda t, lead, follow_up=False: SimpleNamespace(kind=autopilot.FOLLOW_UP_KIND if follow_up else autopilot.EMAIL)
    monkeypatch.setattr(autopilot, "prepare", ready)
    monkeypatch.setattr(orders, "prepare", ready)
    cycle(town)
    drafts = [p for name, p in recorded if name == "draft"]
    assert drafts == [{"lead_id": lead_id, "follow_up": True}]                 # follow-ups only, as before


def test_a_rejected_package_counts_as_a_rejected_first_email(town):
    lead_id = add_lead(town, "Bakery")
    rejected = town.conn.execute(
        "INSERT INTO actions (agent, action_type, target, content, reason, status, reject_reason, created_at) VALUES"
        " ('messenger', 'outreach_package', 't', ?, 'r', 'rejected', 'email: tone', ?)",
        (json.dumps({"lead_id": lead_id, "email": {"kind": "first", "lead_id": lead_id}}), utc_now())).lastrowid
    assert rejected and autopilot.rejected_email_leads(town, follow_up=False) == {lead_id}


# ---------------- "go" and Stop ----------------

def test_the_loop_ends_on_a_stop_request_and_the_state_file_says_off(town, recorded, monkeypatch):
    monkeypatch.setattr(autopilot, "WAIT_STEP_SECONDS", 0.01)
    waiting_lead_proposal(town)
    cycles = []

    def stop_after_first(c):
        cycles.append(c)
        autopilot.request_stop(town.settings, "charlie")        # HQ's Stop, between cycles
    why = autopilot.run_loop(town, town.gmail, town.publisher, "charlie", on_cycle=stop_after_first)
    assert why == "stopped from HQ (Stop button)" and len(cycles) == 1
    assert autopilot.status(town)["on"] is False and not autopilot.stop_requested(town.settings)
    kinds = [r[0] for r in town.conn.execute("SELECT event_type FROM events WHERE event_type LIKE 'autopilot_o%'")]
    assert kinds == ["autopilot_on", "autopilot_off"]


def test_the_loop_never_runs_while_stopped_and_ends_on_the_kill_switch(town, recorded, monkeypatch):
    monkeypatch.setattr(autopilot, "WAIT_STEP_SECONDS", 0.01)
    town.killswitch.stop(town.conn)
    assert autopilot.run_loop(town, town.gmail, town.publisher, "charlie") == "the Town is stopped"
    assert not town.conn.execute("SELECT 1 FROM events WHERE event_type = 'autopilot_cycle_started'").fetchone()


def test_go_starts_one_background_loop_only_and_stop_ends_it(town, recorded, instance_file, monkeypatch):
    import threading
    monkeypatch.setattr(autopilot, "WAIT_STEP_SECONDS", 0.01)
    waiting_lead_proposal(town)
    settings = load_settings(instance_file)

    def tools():
        t, publisher = launcher._open(settings, "charlie", instance_file)
        return t, t.executor.handlers["send_email"].run.__self__.client, publisher
    said = autopilot.start_background(tools, "charlie")
    assert "Autopilot started" in said
    try:
        deadline = time.monotonic() + 10
        while not autopilot.status(town)["on"] and time.monotonic() < deadline:
            time.sleep(0.02)
        assert autopilot.status(town)["on"] is True
        with pytest.raises(autopilot.OrderRefused, match="already running"):
            autopilot.start_background(tools, "charlie")              # only one loop at a time
    finally:
        autopilot.request_stop(town.settings, "charlie")
        autopilot._background.join(10)
    assert not autopilot.background_running() and autopilot.status(town)["on"] is False
    ended = town.conn.execute("SELECT details FROM events WHERE event_type = 'autopilot_loop_ended'").fetchone()
    assert "Stop button" in json.loads(ended["details"])["why"]


def test_go_in_reaper_only_proposes_and_is_refused_where_charlie_may_not_start_it(town):
    from engine import decisions as dec
    from engine.jarvis import Reaper
    from packs.websites.orders import ORDERS
    from packs.websites.townview import VIEW
    rules = dec.Rules(order_book=ORDERS.with_autopilot(None), launch=lambda order: "started")
    result = Reaper(town, VIEW, rules=rules).turn("go")
    assert result.mode == "plain" and result.proposal.empty
    assert "autopilot starts from here only for Charlie" in result.proposal.refused[0]
    started = []
    rules = dec.Rules(order_book=ORDERS.with_autopilot(lambda: started.append(1) or "Autopilot started."),
                      launch=lambda order: rules.order_book.run(town, order))
    for words in ("go", "Start autopilot.", "run the town!"):
        proposal = Reaper(town, VIEW, rules=rules).turn(words).proposal
        assert [o.name for o in proposal.orders] == ["autopilot"] and "Start autopilot" in proposal.lines[0]
    assert started == []                                                    # nothing starts until Confirm
    assert dec.confirm(town, proposal, True, rules) == ["Autopilot started."] and started == [1]
    town.killswitch.stop(town.conn)
    assert Reaper(town, VIEW, rules=rules).turn("go").mode == "stopped"      # stood down: no proposal at all


def test_go_is_refused_while_autopilot_already_runs(town):
    from engine import decisions as dec
    from engine.orders import Order
    from packs.websites.orders import ORDERS
    autopilot.write_state(town.settings, True, "charlie", datetime.now(timezone.utc) + timedelta(minutes=30))
    proposal = dec.check(town, [], orders=[Order("autopilot")], rules=dec.Rules(order_book=ORDERS.with_autopilot(
        lambda: "x")))
    assert proposal.empty and "already running" in proposal.refused[0]


def test_the_launcher_gives_go_only_to_charlie_on_his_real_town_or_the_dev_instance(dev_instance, instance_file):
    real, dev = load_settings(instance_file), load_settings(dev_instance)
    assert launcher._autopilot_starter(dev, "claude-code", dev_instance) is not None        # dev: fakes
    assert launcher._autopilot_starter(real, "claude-code", instance_file) is None          # Claude Code, real Town
    # (instance_file here is the throwaway "real" instance; the true instance.yaml rule is the command's own test)


# ---------------- work moves right after a decision (Charlie, 2026-11-04) ----------------

class Clock:
    """A fake clock: sleeping moves time on, and `at` runs a function once time passes a moment."""

    def __init__(self):
        self.now, self.events = 0.0, []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        for when, work in list(self.events):
            if self.now >= when:
                self.events.remove((when, work))
                work()


def approve_a_lead(town):
    action = town.mayor.propose("scout", "add_lead", "New Bakery", {"business_name": "New Bakery",
                                "business_type": "bakery", "area": "Clayton", "source_id": "nb-1",
                                "public_email": "new@example.com", "source": "overture", "source_release": "r"}, "x")
    town.mayor.approve(action)
    town.executor.execute(action)


def test_an_approval_starts_a_react_cycle_within_react_seconds(town, recorded):
    clock, cycles = Clock(), []
    react_s = float(town.limits.get("autopilot.react_seconds"))
    clock.events.append((100.0, lambda: (approve_a_lead(town), cycles.append(("approved at", clock.now)))))
    autopilot.serve(town, town.gmail, town.publisher, lambda c: cycles.append((c.line, clock.now)),
                    sleep=clock.sleep, clock=clock, max_cycles=2)
    full, (marker, approved_at), react = cycles
    assert full[0].startswith("Autopilot cycle:") and react[0].startswith("Autopilot right after your decision:")
    assert react[1] - approved_at <= react_s
    # The react cycle is only inspect, build and first emails: the inbox and Scout wait for the full cycle.
    rows = [json.loads(r["details"]) for r in town.conn.execute(
        "SELECT details FROM events WHERE event_type = 'autopilot_cycle_started' ORDER BY id")]
    assert [r["react"] for r in rows] == [False, True]
    assert town.conn.execute("SELECT COUNT(*) FROM events WHERE event_type = 'clerk_finished'").fetchone()[0] == 1


def test_an_approved_lead_goes_straight_to_the_inspector_and_a_build_worthy_one_to_the_builder(town, recorded):
    lead_id = add_lead(town, "Ready Bakery", worthy=None)       # just approved: not inspected yet
    def inspect_then(order_name, params):
        if order_name == "inspect":
            town.conn.execute("INSERT INTO inspections (lead_id, status, problems, need_score, build_worthy, created_at)"
                              " VALUES (?, 'done', '[\"no_site\"]', 100, 1, ?)", (lead_id, utc_now()))
    original = orders.ORDERS.run

    def run(t, order):
        inspect_then(order.name, order.params)
        return original(t, order)
    orders.ORDERS.run = run
    try:
        autopilot.run_cycle(town, town.gmail, town.publisher, react=True)
    finally:
        orders.ORDERS.run = original
    assert [name for name, _ in recorded] == ["inspect", "build"]
    assert recorded[1][1] == {"lead_id": lead_id, "after_pass": "none"}   # the package step proposes


def test_a_react_cycle_never_moves_the_thirty_minute_tick(town, recorded):
    clock, starts = Clock(), []
    for at in (60.0, 300.0):
        clock.events.append((at, lambda: approve_a_lead(town)))
    autopilot.serve(town, town.gmail, town.publisher, lambda c: starts.append((c.line.split(":")[0], clock.now)),
                    sleep=clock.sleep, clock=clock, max_cycles=4)
    kinds = [k for k, _ in starts]
    assert kinds == ["Autopilot cycle", "Autopilot right after your decision", "Autopilot right after your decision",
                     "Autopilot cycle"]
    assert starts[3][1] == pytest.approx(30 * 60, abs=5)       # the full cycle on its own schedule


def test_a_cap_says_so_in_plain_words_and_the_courier_stops_at_the_town_hall(town, recorded, limits_file):
    from packs.websites.townview import VIEW, activity_lines
    text = limits_file.read_text(encoding="utf-8").replace("builds_per_day: 3", "builds_per_day: 1")
    limits_file.write_text(text, encoding="utf-8")
    town.limits = type(town.limits)(limits_file)
    add_lead(town, "First Bakery")
    add_lead(town, "Fictional Example Business")
    result = autopilot.run_cycle(town, town.gmail, town.publisher, react=True)
    line = "Builder: 1 of 1 builds today; Fictional Example Business builds tomorrow"
    assert line in result.skipped
    row = town.conn.execute("SELECT * FROM events WHERE event_type = 'autopilot_held'").fetchone()
    assert json.loads(row["details"])["line"] == line
    assert any(a.startswith(line) for a in activity_lines(town.conn))
    from engine.flows import event_dict
    [flow] = VIEW.flows(town.conn, event_dict(row))
    assert (flow.from_node, flow.to_node) == ("inspector", "mayor") and line in flow.says


def test_the_inspector_cap_holds_too(town, recorded, limits_file):
    limits_file.write_text(limits_file.read_text(encoding="utf-8").replace("inspections_per_day: 25",
                                                                           "inspections_per_day: 0"), encoding="utf-8")
    town.limits = type(town.limits)(limits_file)
    add_lead(town, "Fresh Bakery", worthy=None)
    result = autopilot.run_cycle(town, town.gmail, town.publisher, react=True)
    assert "Inspector: 0 of 0 inspections today; Fresh Bakery waits for tomorrow" in result.skipped


def test_a_stop_while_waiting_ends_the_loop(town, recorded):
    clock = Clock()
    clock.events.append((30.0, lambda: town.killswitch.stop(town.conn)))
    assert autopilot.serve(town, town.gmail, town.publisher, lambda c: None, sleep=clock.sleep, clock=clock) == "stopped"


def test_never_two_autopilots_at_once(dev_instance, model):
    run = lambda *args: runner.invoke(launcher.app, ["--instance", "dev", *args])
    run("resume")
    t, _ = launcher._open(load_settings(dev_instance), "charlie", dev_instance)
    try:
        autopilot.write_state(t.settings, True, "charlie", datetime.now(timezone.utc) + timedelta(minutes=30))
    finally:
        t.close()
    result = run("autopilot")
    assert result.exit_code == 1 and "already running" in flat(result.stdout)
