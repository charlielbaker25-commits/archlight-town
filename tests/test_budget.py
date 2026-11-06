"""Cost logging and budget pauses, using a fake API client (never the real API)."""
import json
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from engine.budget import PaidWorkPaused
from engine.db import utc_now
from engine.killswitch import TownStopped
from engine.limits import Limits
from engine.llm import is_spend_limit_error
from engine.settings import load_settings
from engine.town import open_town
from packs.websites import pack


class FakeClient:
    """Stands in for anthropic.Anthropic(). Counts calls and can raise an error instead of answering."""

    def __init__(self, error=None):
        self.calls = 0
        self.error = error
        self.messages = self

    def create(self, **request):
        self.calls += 1
        self.last_request = request
        if self.error:
            raise self.error
        usage = SimpleNamespace(input_tokens=1000, output_tokens=200,
                                cache_creation_input_tokens=0, cache_read_input_tokens=0)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="OK")], usage=usage)


def _api_error(status, message, error_type="invalid_request_error"):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    body = {"type": "error", "error": {"type": error_type, "message": message}}
    cls = anthropic.BadRequestError if status == 400 else anthropic.APIStatusError
    return cls(message, response=httpx2.Response(status, request=request), body=body)


@pytest.fixture
def town(instance_file, limits_file):
    client = FakeClient()
    t = open_town(load_settings(instance_file), pack.PACK_SCHEMAS, {}, limits_file, pack.MODELS_FILE,
                  api_client=client)
    t.killswitch.resume(t.conn)
    t.fake = client
    yield t
    t.close()


def _spend(town, dollars, service="claude_api"):
    town.conn.execute(
        "INSERT INTO costs (ts, agent, model, service, units, dollar_cost) VALUES (?, 'scout', 'claude-haiku-5-5', ?, '{}', ?)",
        (utc_now(), service, dollars))


def _ask(town):
    return town.llm.ask("scout", "system", "prompt")


def _requests(town, action_type):
    return town.conn.execute("SELECT * FROM actions WHERE action_type = ? AND status = 'proposed'",
                             (action_type,)).fetchall()


def _approve_and_run(town, action_id):
    town.mayor.approve(action_id)
    return town.executor.execute(action_id)


# ---- cost logging ----

def test_every_api_call_logs_its_cost_and_model(town):
    reply = _ask(town)
    row = town.conn.execute("SELECT * FROM costs").fetchone()
    assert row["model"] == "claude-haiku-5-5" == reply.model
    assert row["service"] == "claude_api"
    # 1000 input tokens at $0.10/M + 200 output tokens at $0.50/M
    assert row["dollar_cost"] == pytest.approx(0.0002)
    assert json.loads(row["units"])["output_tokens"] == 200
    event = town.conn.execute("SELECT details FROM events WHERE event_type = 'cost_logged'").fetchone()
    assert json.loads(event["details"])["model"] == "claude-haiku-5-5"
    assert town.fake.last_request["model"] == "claude-haiku-5-5"


# ---- the two caps ----

def test_api_cap_pauses_paid_calls_and_requests_a_raise(town):
    _spend(town, 9.00)
    with pytest.raises(PaidWorkPaused):
        _ask(town)
    with pytest.raises(PaidWorkPaused):
        _ask(town)
    assert town.fake.calls == 0
    [request] = _requests(town, "raise_cap")  # one request, not one per attempt
    content = json.loads(request["content"])
    assert content["limit"] == "money.api_monthly_cap_usd"
    assert content["current_value"] == 9.00 and content["new_value"] == 10.00
    assert content["expected_return"]

    _approve_and_run(town, request["id"])

    assert Limits(town.limits.path).get("money.api_monthly_cap_usd") == 10.00
    _ask(town)
    assert town.fake.calls == 1


def test_total_cap_pauses_paid_calls_and_requests_a_raise(town):
    _spend(town, 25.00, service="phone")
    with pytest.raises(PaidWorkPaused):
        _ask(town)
    assert town.fake.calls == 0
    [request] = _requests(town, "raise_cap")
    assert json.loads(request["content"])["limit"] == "money.total_monthly_cap_usd"


def test_nothing_paid_runs_until_charlie_approves_a_new_amount(town):
    _spend(town, 9.00)
    with pytest.raises(PaidWorkPaused):
        _ask(town)
    [request] = _requests(town, "raise_cap")
    town.mayor.reject(request["id"], "Not this month")
    with pytest.raises(PaidWorkPaused):
        _ask(town)
    assert town.fake.calls == 0
    # The decision is back in the queue, so Charlie can still change his mind later.
    assert len(_requests(town, "raise_cap")) == 1


def test_crossing_a_cap_files_the_request_right_away(town):
    _spend(town, 8.9999)
    _ask(town)  # this call's cost pushes spend past $9
    assert len(_requests(town, "raise_cap")) == 1
    with pytest.raises(PaidWorkPaused):
        _ask(town)
    assert town.fake.calls == 1


def test_early_warning_at_80_percent_is_logged_once(town):
    _spend(town, 7.30)
    _ask(town)
    _ask(town)
    warnings = town.conn.execute("SELECT details FROM events WHERE event_type = 'budget_warning'").fetchall()
    assert [json.loads(w["details"])["cap"] for w in warnings] == ["api"]


# ---- Console errors ----

@pytest.mark.parametrize("error", [
    _api_error(400, "You have reached your specified workspace API usage limits. You will regain access on 2026-11-01."),
    _api_error(400, "Your credit balance is too low to access the Anthropic API."),
    _api_error(402, "Payment required", error_type="billing_error"),
])
def test_console_spend_limit_or_out_of_credits_pauses_and_is_never_retried(town, error):
    town.fake.error = error
    with pytest.raises(PaidWorkPaused):
        _ask(town)
    assert town.fake.calls == 1  # tried once, never retried
    with pytest.raises(PaidWorkPaused):
        _ask(town)
    assert town.fake.calls == 1  # paused: the next call never reaches the API
    [request] = _requests(town, "resume_paid_work")
    assert "console.anthropic.com" in json.loads(request["content"])["what_to_do"]
    hit = town.conn.execute("SELECT details FROM events WHERE event_type = 'api_spend_limit_hit'").fetchone()
    assert json.loads(hit["details"])["retried"] is False

    # Charlie fixes the Console, then approves
    town.fake.error = None
    _approve_and_run(town, request["id"])
    _ask(town)
    assert town.fake.calls == 2


def test_other_api_errors_do_not_pause_paid_work(town):
    town.fake.error = _api_error(400, "messages: field required")
    with pytest.raises(anthropic.BadRequestError):
        _ask(town)
    assert town.budget.paused_reason() is None
    assert not is_spend_limit_error(town.fake.error)


# ---- fail closed and the kill switch ----

def test_unreadable_spend_counts_as_paused(town):
    town.conn.execute("DROP TABLE costs")
    with pytest.raises(PaidWorkPaused):
        _ask(town)
    assert town.fake.calls == 0


def test_no_api_call_while_the_town_is_stopped(town):
    town.killswitch.stop(town.conn)
    with pytest.raises(TownStopped):
        _ask(town)
    assert town.fake.calls == 0


def test_a_limit_change_only_lands_after_approval(town):
    action_id = town.mayor.propose("charlie", "change_limit", "limits.yaml", {
        "limit": "leads.new_leads_per_day", "current_value": 50, "new_value": 40}, "Slow down")
    assert town.limits.get("leads.new_leads_per_day") == 50
    _approve_and_run(town, action_id)
    assert town.limits.get("leads.new_leads_per_day") == 40


def test_town_costs_and_town_limit_commands(town):
    from typer.testing import CliRunner

    import launcher

    _ask(town)
    runner = CliRunner()
    result = runner.invoke(launcher.app, ["costs"])
    assert result.exit_code == 0
    assert "of $9.00" in result.stdout and "claude-haiku-5-5" in result.stdout

    result = runner.invoke(launcher.app, ["limit", "leads.new_leads_per_day", "40", "--reason", "Slow down"])
    assert result.exit_code == 0 and "proposed" in result.stdout
    assert town.limits.get("leads.new_leads_per_day") == 50  # nothing changes until approved


def test_the_real_client_never_retries_on_its_own(instance_file, limits_file):
    t = open_town(load_settings(instance_file), pack.PACK_SCHEMAS, {}, limits_file, pack.MODELS_FILE,
                  api_key_loader=lambda: "sk-test-key")
    try:
        assert t.llm._get_client().max_retries == 0  # builds the client only; no network call
    finally:
        t.close()


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), 0, -1])
def test_a_money_cap_must_be_a_real_positive_number(town, bad):
    action_id = town.mayor.propose("charlie", "change_limit", "limits.yaml", {
        "limit": "money.api_monthly_cap_usd", "current_value": 9.0, "new_value": bad}, "test")
    assert _approve_and_run(town, action_id).status == "failed"
    assert town.limits.get("money.api_monthly_cap_usd") == 9.00


def test_a_money_change_does_not_lift_a_console_pause(town):
    town.fake.error = _api_error(400, "Your credit balance is too low to access the Anthropic API.")
    with pytest.raises(PaidWorkPaused):
        _ask(town)
    action_id = town.mayor.propose("charlie", "change_limit", "limits.yaml", {
        "limit": "money.total_monthly_cap_usd", "current_value": 25.0, "new_value": 30.0}, "unrelated")
    _approve_and_run(town, action_id)
    assert town.budget.paused_kind() == "console"


def test_a_bad_limit_value_fails_and_changes_nothing(town):
    action_id = town.mayor.propose("charlie", "change_limit", "limits.yaml", {
        "limit": "leads.new_leads_per_day", "current_value": 50, "new_value": "lots"}, "typo")
    assert _approve_and_run(town, action_id).status == "failed"
    assert town.limits.get("leads.new_leads_per_day") == 50
