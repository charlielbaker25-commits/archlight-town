"""Reaper's orders (B), edits (C) and deep analysis (D): proposed by the model, checked by plain code, carried out
only after Charlie confirms. Throwaway towns (the Reaper test set's frozen state), a scripted model, a fake
`claude -p`. Nothing reaches the real API or Charlie's plan.
"""
import json
from types import SimpleNamespace

import pytest

from engine import analysis
from engine import decisions as dec
from engine import orders as order_rules
from engine.claude_cli import STRIPPED_PREFIXES, SubscriptionLimitReached
from engine.jarvis import Reaper
from engine.orders import Order, OrderRefused
from packs.websites import pack
from packs.websites import testset_reaper as frozen
from packs.websites.orders import ORDERS
from packs.websites.townview import VIEW


@pytest.fixture
def town(tmp_path):
    (tmp_path / "town").mkdir()   # conftest already put an instance.yaml in tmp_path itself
    t = frozen.open_fake_town(tmp_path / "town")
    yield t
    t.close()


class FakeBook:
    """A pack's orders, recorded instead of run."""
    names = frozenset({"scout", "build", "publish", "resume", "change_limit", "dnc_add"})

    def __init__(self):
        self.ran = []

    def check(self, town, order):
        if order.params.get("full"):
            raise OrderRefused("the daily cap is reached")
        return f"{order.name} {order.params}"

    def run(self, town, order):
        self.ran.append(order)
        return "done"


def rules(book=None, launched=None, run_inline_with=None):
    def launch(order):
        if launched is not None:
            launched.append(order)
        if run_inline_with is not None:
            return order_rules.run_now(lambda: run_inline_with, book, order, "charlie")
        return "started"
    return dec.Rules(uneditable_types=frozenset(pack.LIVE_EXTERNAL_TYPES), order_book=book, launch=launch)


def statuses(town):
    return frozen.statuses(town)


# ---------------- B: orders ----------------

def test_an_order_never_runs_without_confirmation(town):
    book, launched = FakeBook(), []
    town.client.script = [{"tool": "propose_orders", "input": {"orders": [
        {"order": "scout", "params": {"business_type": "salon", "area": "Clayton"}}]}}, "Proposed."]
    result = Reaper(town, VIEW, rules=rules(book, launched)).turn("Scout salons in Clayton.")
    assert result.proposal.orders == (Order("scout", {"business_type": "salon", "area": "Clayton"}),)
    assert "Order: scout" in result.text
    assert launched == [] and book.ran == []
    assert town.conn.execute("SELECT COUNT(*) FROM events WHERE event_type LIKE 'order_%'").fetchone()[0] == 0


@pytest.mark.parametrize("name", ["publish", "resume", "change_limit", "dnc_add", "takedown", "Publish-Preview",
                                  "set_limit", "send_email", "approve"])
def test_terminal_only_orders_are_refused_even_if_a_pack_lists_them(town, name):
    proposal = dec.check(town, [], orders=[Order(name, {})], rules=rules(FakeBook()))
    assert proposal.empty
    assert "never an order through Reaper" in proposal.refused[0]
    assert not proposal.orders


def test_the_website_pack_offers_exactly_the_orders_charlie_listed():
    # 2026-11-05: "package" (site + first email as one proposal) and "autopilot" ("go"; only where Charlie may start it).
    assert ORDERS.names == {"scout", "inspect", "build", "critique", "draft", "propose_publish", "package", "autopilot"}
    assert not ORDERS.names & {"publish", "takedown", "resume", "limit", "dnc"}


def test_a_refused_order_is_not_an_agent_error(town, tmp_path):
    """When the agent's own rule says no while the job runs (it was fine at confirmation), the order is logged
    as refused (order_cancelled), not failed, and the agent's building shows done, not red."""
    from engine.facts import agent_states
    from packs.websites.inspector import InspectorError, run_inspector

    class Book:
        names = frozenset({"inspect"})

        def check(self, town, order):
            return "Inspector: inspect lead #1."

        def run(self, town, order):
            try:
                run_inspector(town, 99)   # no such lead: the Inspector's rule refuses
            except InspectorError as err:
                raise OrderRefused(str(err)) from None

    said = order_rules.run_now(lambda: frozen.open_fake_town(tmp_path / "town"), Book(),
                               Order("inspect", {"lead_id": 99}), "charlie")
    assert "refused" in said and "FAILED" not in said
    kinds = [r["event_type"] for r in town.conn.execute("SELECT event_type FROM events ORDER BY id DESC LIMIT 4")]
    assert "order_cancelled" in kinds and "order_failed" not in kinds and "inspector_refused" in kinds
    state = {a["agent"]: a for a in agent_states(town.conn, VIEW, [])}
    assert state["inspector"]["state"] == "done" and "refused" in state["inspector"]["task"]
    refusal = {"id": 1, "ts": frozen.TS, "agent": "inspector", "event_type": "inspector_refused", "action_id": None,
               "details": {"why": "already inspected"}}
    assert [f.kind for f in VIEW.flows(town.conn, refusal)] == ["result"]


def test_orders_are_held_to_the_commands_rules_and_caps(town):
    def refused(order):
        proposal = dec.check(town, [], orders=[order], rules=rules(ORDERS))
        assert proposal.empty
        return proposal.refused[0]
    assert "no Overture data" in refused(Order("scout", {"business_type": "salon", "area": "Clayton"}))
    assert "hasn't been inspected" in refused(Order("build", {"lead_id": 1}))
    assert "no saved lead #99" in refused(Order("inspect", {"lead_id": 99}))
    # The Inspector's own rule: a lead already inspected isn't offered again (found by the R2 dev run, where
    # the card offered it and the job then failed).
    town.conn.execute("INSERT INTO inspections (lead_id, status, created_at) VALUES (2, 'done', ?)", (frozen.TS,))
    assert "already inspected" in refused(Order("inspect", {"lead_id": 2}))
    assert "isn't a finished build" in refused(Order("critique", {"build_id": 5}))
    assert "isn't Critic-passed" in refused(Order("propose_publish", {"build_id": 5}))
    assert "whole-number lead_id" in refused(Order("build", {"lead_id": "the bakery"}))
    # The daily inspection cap
    cap = town.limits.get("inspector.inspections_per_day")
    for _ in range(cap):
        town.conn.execute("INSERT INTO inspections (lead_id, status, created_at) VALUES (1, 'done', ?)",
                          (frozen.TS.replace("2026-10-26", "2099-01-01"),))
    assert "daily inspection cap is reached" in refused(Order("inspect", {"lead_id": 1}))


def test_a_confirmed_order_runs_once_logged_as_charlie_via_reaper(town):
    book = FakeBook()
    proposal = dec.check(town, [], orders=[Order("scout", {"business_type": "salon"})], rules=rules(book))
    lines = dec.confirm(town, proposal, True, rules(book, run_inline_with=_Reopen(town)))
    assert book.ran == [Order("scout", {"business_type": "salon"})] and "done" in lines[0]
    kinds = [r["event_type"] for r in town.conn.execute(
        "SELECT event_type FROM events WHERE event_type LIKE 'order_%' ORDER BY id")]
    assert kinds == ["order_confirmed", "order_started", "order_finished"]
    started = json.loads(town.conn.execute("SELECT details FROM events WHERE event_type = 'order_started'")
                         .fetchone()["details"])
    assert started["ordered_by"] == "charlie" and started["via"] == "reaper"


def test_an_order_whose_cap_filled_before_it_ran_is_cancelled(town):
    book = FakeBook()
    result = order_rules.run_now(lambda: _Reopen(town), book, Order("scout", {"full": True}), "charlie")
    assert "not started" in result and book.ran == []


def test_the_kill_switch_stops_a_queued_order(town):
    book = FakeBook()
    town.killswitch.stop(town.conn)
    result = order_rules.run_now(lambda: _Reopen(town), book, Order("scout", {}), "charlie")
    assert "stood down" in result and book.ran == []
    proposal = dec.check(town, [], orders=[Order("scout", {})], rules=rules(book))
    with pytest.raises(dec.DecisionError, match="stood down"):
        dec.confirm(town, proposal, True, rules(book))


def test_background_orders_run_one_at_a_time_with_their_own_town(town):
    book = FakeBook()
    opened = []

    folder = town.settings.database.parent

    def open_town():   # each job opens its own Town in its own thread, like the real runner
        opened.append(1)
        return frozen.open_fake_town(folder, client=town.client)
    runner = order_rules.Runner(open_town, book, "charlie")
    runner.launch(Order("scout", {"n": 1}))
    runner.launch(Order("scout", {"n": 2}))
    runner.wait(10)
    assert [o.params["n"] for o in book.ran] == [1, 2] and len(opened) == 2


class _Reopen:
    """The same throwaway Town, as a background job would open it (close() does nothing here)."""

    def __init__(self, town):
        self._town = town

    def __getattr__(self, name):
        return getattr(self._town, name)

    def close(self):
        pass


# ---------------- C: edits ----------------

def test_an_edit_shows_before_and_after_and_waits_for_confirmation(town):
    town.client.script = [{"tool": "propose_edit", "input": {
        "action_id": 3, "field": "opener", "new_text": "Hi, this is Charlie. We built you a free preview."}},
        "Proposed."]
    before = statuses(town)
    result = Reaper(town, VIEW, rules=rules()).turn("Change #3's opener to say we built a free preview.")
    assert "before: Hi, this is Charlie from Example Web Studio." in result.text
    assert "after:  Hi, this is Charlie. We built you a free preview." in result.text
    assert statuses(town) == before
    assert json.loads(town.conn.execute("SELECT content FROM actions WHERE id = 3").fetchone()[0])["opener"] == \
        "Hi, this is Charlie from Example Web Studio."


def test_a_confirmed_edit_goes_through_the_mayor_keeping_the_original(town):
    proposal = dec.check(town, [], edits=[dec.Edit(1, "business_name", "Oak Lane Flowers")], rules=rules())
    lines = dec.confirm(town, proposal, True, rules())
    row = town.conn.execute("SELECT * FROM actions WHERE id = 1").fetchone()
    assert row["status"] == "executed" and "edited (original kept)" in lines[0]
    assert json.loads(row["original_content"])["business_name"] == "Oak Lane Florist"
    assert json.loads(row["edited_content"])["business_name"] == "Oak Lane Flowers"
    event = json.loads(town.conn.execute("SELECT details FROM events WHERE event_type = 'action_edited_and_approved'")
                       .fetchone()["details"])
    assert event["decided_by"] == "charlie" and event["via"] == "reaper"


@pytest.mark.parametrize("edit,why", [
    (dec.Edit(1, "no_such_field", "x"), "no field"),
    (dec.Edit(1, "confidence", "0.1"), "isn't text"),
    (dec.Edit(1, "business_name", "   "), "1 to 5000"),
    (dec.Edit(6, "business_name", "x"), "already executed"),
    (dec.Edit(5, "opener", "x"), "no field"),
])
def test_bad_edits_are_refused(town, edit, why):
    proposal = dec.check(town, [], edits=[edit], rules=rules())
    assert proposal.empty and why in proposal.refused[0]


def test_publish_actions_cant_be_edited(town):
    town.conn.execute("INSERT INTO actions (id, agent, action_type, target, content, reason, status, created_at) VALUES"
                      " (7, 'builder', 'publish_preview', 'Preview', '{\"business\": \"Maple\"}', 'r', 'proposed', ?)",
                      (frozen.TS,))
    proposal = dec.check(town, [], edits=[dec.Edit(7, "business", "Other")], rules=rules())
    assert proposal.empty and "can't be edited" in proposal.refused[0]


def test_an_edit_that_leaves_the_action_blocked_is_refused(town):
    proposal = dec.check(town, [], edits=[dec.Edit(4, "subject", "A new subject")], rules=rules())
    assert proposal.empty and "BLOCKED" in proposal.refused[0]


# ---------------- D: deep analysis ----------------

class FakeCLI:
    def __init__(self, error=None):
        self.calls, self.error = [], error

    def check_available(self):
        pass

    def run(self, agent, prompt, cwd, tools, timeout_s, limit_wait_minutes, denied=(), **kw):
        self.calls.append({"agent": agent, "prompt": prompt, "cwd": cwd, "tools": tools, "denied": list(denied)})
        if self.error:
            raise self.error
        return SimpleNamespace(text="Salons: 1 lead, nothing built yet. Too few to compare.", model="sonnet",
                               wall_seconds=12.0)


def test_analysis_is_read_only_on_sonnet_through_claude_p(town, monkeypatch):
    cli = FakeCLI()
    monkeypatch.setattr(town, "cli", cli)
    town.client.script = [{"tool": "analyze", "input": {"question": "What's working?"}}, "Running deep analysis."]
    before = statuses(town)
    result = Reaper(town, VIEW).turn("What's working, deep dive please.")
    call = cli.calls[0]
    assert call["agent"] == "jarvis_analysis"
    choice = town.routing.for_agent("jarvis_analysis")
    assert choice.route == "claude_cli" and "sonnet" in choice.model
    assert set(call["tools"]) <= {"Read", "Glob", "Grep"}
    assert {"Write", "Edit", "Bash", "WebFetch"} <= set(call["denied"])
    data = json.loads((call["cwd"] / analysis.DATA_FILE).read_text(encoding="utf-8"))
    assert data["funnel_by_business_type"][0]["leads"] >= 1
    assert "town_data.json" in call["prompt"] and "never instructions" in call["prompt"]
    assert statuses(town) == before
    assert result.tools == ["analyze"]


def test_claude_p_never_gets_anthropic_variables():
    from engine.claude_cli import clean_env
    env = clean_env({"ANTHROPIC_API_KEY": "sk-ant-x", "ANTHROPIC_BASE_URL": "u", "PATH": "p"})
    assert not any(k.upper().startswith(STRIPPED_PREFIXES) for k in env) and env["PATH"] == "p"


def test_an_analysis_usage_limit_is_reported_and_never_retried(town, monkeypatch):
    from datetime import datetime, timezone
    cli = FakeCLI(error=SubscriptionLimitReached("Plan limit.", datetime.now(timezone.utc)))
    monkeypatch.setattr(town, "cli", cli)
    town.client.script = [{"tool": "analyze", "input": {"question": "Compare salons and dentists."}}, "Unavailable."]
    Reaper(town, VIEW).turn("Compare salons and dentists.")
    assert len(cli.calls) == 1
    sent = json.dumps(town.client.requests[1]["messages"][-1]["content"])
    assert "Not retried" in sent
