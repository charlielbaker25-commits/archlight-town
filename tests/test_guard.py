"""The Guard: hard rules in plain code block, Haiku only adds flags, blocked actions can't be approved, and the
Guard runs again at execution. A fake Haiku client: pytest never reaches the API. Email is only ever dry-run."""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import launcher
from engine.executor import ExecutorError
from engine.mayor import MayorError
from engine.settings import load_settings
from engine.town import open_town
from packs.websites import guard, pack
from packs.websites.guard import OPT_OUT_LINE, WebsiteGuard

ADDRESS = "P.O. Box 4521, St. Louis, MO 63105"
FROM = "Example Web Studio <town@example.com>"


class FakeHaiku:
    """The soft check's model. `reply` is what it says; `fail` makes the call raise."""

    def __init__(self, reply='{"flags": []}', fail=None):
        self.reply, self.fail, self.calls = reply, fail, 0
        self.messages = self

    def create(self, **request):
        self.calls += 1
        if self.fail:
            raise self.fail
        usage = SimpleNamespace(input_tokens=900, output_tokens=40, cache_creation_input_tokens=0,
                                cache_read_input_tokens=0)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self.reply)], usage=usage)


@pytest.fixture
def town(instance_file, limits_file):
    haiku = FakeHaiku()
    t = open_town(load_settings(instance_file), pack.PACK_SCHEMAS, pack.handlers(), limits_file, pack.MODELS_FILE,
                  api_client=haiku, api_key_loader=lambda: "sk-test")
    t.killswitch.resume(t.conn)
    t.mayor.guard = t.executor.guard = WebsiteGuard(t.limits, t.budget, t.llm, ADDRESS, real=False)
    t.haiku = haiku
    yield t
    t.close()


def add_lead(town, name="Butterbell Bakery", email="butterbellbakery@example.com", phone="+13145550103"):
    return town.conn.execute(
        "INSERT INTO leads (business_name, business_type, area, address, phone, website, public_email, contact_route,"
        " source, source_id, source_release, created_at) VALUES (?, 'bakery', 'Kirkwood', '1 Main St', ?, '', ?,"
        " 'email', 'overture', ?, 'r', '2026-10-26T00:00:00Z')", (name, phone, email, f"src-{name}")).lastrowid


def email(lead_id, **change):
    content = {"kind": "first", "lead_id": lead_id, "from": FROM, "to": "butterbellbakery@example.com",
               "subject": "A free website preview for Butterbell Bakery",
               "body": f"Hi,\n\nWe built you a preview.\n\nThe Example Web Studio team\n\n{OPT_OUT_LINE}\n{ADDRESS}",
               "build_id": None}
    return {**content, **change}


def propose(town, content, action_type="send_email"):
    return town.mayor.propose("messenger", action_type, "Butterbell Bakery", content, "first email")


def verdict(town, action_id):
    return json.loads(town.mayor.get(action_id)["guard_result"])


def sent(town, lead_id, kind="first", days_ago=0.0, really=True):
    """An email to this lead that already went out, `days_ago` days back. really=True: go-live sent it (its
    gmail_drafts row has sent_at); really=False: a dry run (a shadow-day Gmail draft, never received)."""
    when = (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    action_id = town.conn.execute("INSERT INTO actions (agent, action_type, target, content, reason, status, created_at,"
                                  " executed_at) VALUES ('messenger', 'send_email', 't', ?, 'r', 'executed', ?, ?)",
                                  (json.dumps(email(lead_id, kind=kind)), when, when)).lastrowid
    town.conn.execute("INSERT INTO gmail_drafts (action_id, lead_id, draft_id, to_address, subject, fake, created_at, sent_at) VALUES (?, ?, 'seeded', 'seeded@example.com', 'seeded', 1, ?, ?)", (action_id, lead_id, when, when if really else None))
    return action_id


def test_a_good_email_is_clear_and_haiku_was_asked(town):
    action_id = propose(town, email(add_lead(town)))
    assert verdict(town, action_id)["verdict"] == "clear" and town.haiku.calls == 1
    assert town.conn.execute("SELECT COUNT(*) FROM costs WHERE agent = 'guard'").fetchone()[0] == 1  # cost logged


@pytest.mark.parametrize("change, setup, words", [
    ({"from": "Charlie <operator@example.com>"}, None, "must send from town@example.com"),
    ({"from": "Your Neighbor <town@example.com>"}, None, "sender name must be"),
    ({"to": "someone-else@example.com"}, None, "isn't the lead's public email"),
    ({"body": f"Hi.\n{ADDRESS}"}, None, "opt-out line is missing"),
    ({"body": f"Hi.\n{OPT_OUT_LINE}"}, None, "mailing address"),
    ({}, "dnc_email", "do-not-contact"),
    ({}, "dnc_phone", "do-not-contact"),
    ({}, "dnc_business", "do-not-contact"),
    ({}, "cap", "email.sends_per_day"),
    ({}, "already_first", "already has a first email"),
    ({"kind": "follow_up"}, None, "needs a first email"),
    ({"kind": "follow_up"}, "too_soon", "at least 4 days"),
    ({"kind": "follow_up"}, "two_follow_ups", "the lead is done"),
    ({}, "paused", None),
])
def test_each_hard_rule_blocks(town, change, setup, words):
    lead = add_lead(town)
    cost = 0.0
    if setup == "dnc_email":
        guard.dnc_add(town.conn, "ButterbellBakery@example.com", "asked not to be contacted", "charlie")
    elif setup == "dnc_phone":
        guard.dnc_add(town.conn, "(314) 555-0103", "asked not to be contacted", "charlie")
    elif setup == "dnc_business":
        guard.dnc_add(town.conn, "Butterbell Bakery LLC", "owner said no", "charlie")
    elif setup == "cap":
        for other in range(20):
            sent(town, add_lead(town, name=f"Other {other}", email=f"o{other}@example.com", phone=f"+1314555{other:04d}"))
    elif setup == "already_first":
        sent(town, lead, days_ago=10)
    elif setup == "too_soon":
        sent(town, lead, days_ago=2)
    elif setup == "two_follow_ups":
        sent(town, lead, days_ago=20)
        sent(town, lead, "follow_up", days_ago=12)
        sent(town, lead, "follow_up", days_ago=6)
    elif setup == "paused":
        town.budget.pause("test pause", {"action_type": "change_limit", "target": "t", "content": {}, "reason": "r"})
        cost, words = 0.25, "paid work is paused"
    action_id = town.mayor.propose("messenger", "send_email", "Butterbell Bakery", email(lead, **change), "x", cost)
    result = verdict(town, action_id)
    assert result["verdict"] == "blocked" and any(words in b for b in result["blocks"]), result["blocks"]
    with pytest.raises(MayorError, match="BLOCKED by the Guard"):
        town.mayor.approve(action_id)
    assert town.mayor.get(action_id)["status"] == "proposed"


def test_no_mailing_address_blocks_every_email_and_a_fake_one_is_refused_on_the_real_town(town):
    lead = add_lead(town)
    town.mayor.guard = WebsiteGuard(town.limits, town.budget, town.llm, "", real=True)   # the real Town today
    assert any("no mailing address is set" in b for b in verdict(town, propose(town, email(lead)))["blocks"])
    fake = "P.O. Box 0000, Testville, MO 00000 (FAKE DEV ADDRESS)"
    town.mayor.guard = WebsiteGuard(town.limits, town.budget, town.llm, fake, real=True)
    body = f"Hi.\n{OPT_OUT_LINE}\n{fake}"
    assert any("test address" in b for b in verdict(town, propose(town, email(lead, body=body)))["blocks"])


@pytest.mark.parametrize("action_type", ["place_call", "ai_call", "send_sms", "send_text", "voice_message", "dial"])
def test_the_town_never_calls_or_texts_anyone(town, action_type):
    result = verdict(town, propose(town, {"lead_id": add_lead(town), "phone": "+13145550103"}, action_type))
    assert any("call or text" in b for b in result["blocks"])


def test_a_call_script_for_charlie_is_fine_but_not_for_someone_on_the_list(town):
    lead = add_lead(town)
    script = {"lead_id": lead, "for": "charlie", "business": "Butterbell Bakery", "phone": "(314) 555-0103",
              "script": {"opener": "Hi"}}
    assert verdict(town, propose(town, script, "call_script"))["verdict"] == "clear"
    assert verdict(town, propose(town, {**script, "for": "the town"}, "call_script"))["verdict"] == "blocked"
    guard.dnc_add(town.conn, "+1 314 555 0103", "said no on the phone", "charlie")
    assert verdict(town, propose(town, script, "call_script"))["verdict"] == "blocked"


def test_haiku_can_add_a_flag_but_never_clear_a_block(town):
    town.haiku.reply = json.dumps({"flags": ["The subject sounds like an invoice."], "verdict": "clear",
                                   "clear_blocks": True, "blocks": []})
    lead = add_lead(town)
    good = verdict(town, propose(town, email(lead)))
    assert good["verdict"] == "flagged" and good["blocks"] == []
    assert good["flags"] == ["Haiku: The subject sounds like an invoice."]
    blocked = verdict(town, propose(town, email(lead, to="x@example.com")))
    assert blocked["verdict"] == "blocked" and blocked["flags"]          # the flag is added, the block stays


@pytest.mark.parametrize("reply, fail", [("Looks fine to me!", None), ('{"flags": "none"}', None),
                                         (None, RuntimeError("API down"))])
def test_an_unreadable_or_unreachable_soft_check_is_a_flag_and_the_proposal_still_goes_to_charlie(town, reply, fail):
    town.haiku.reply, town.haiku.fail = reply, fail
    action_id = propose(town, email(add_lead(town)))
    result = verdict(town, action_id)
    assert result["verdict"] == "flagged" and result["flags"][0].startswith("soft check unavailable")
    town.mayor.approve(action_id)                                       # a flag never blocks


def test_the_guard_runs_again_at_execution(town):
    lead = add_lead(town)
    action_id = propose(town, email(lead))
    town.mayor.approve(action_id)
    guard.dnc_add(town.conn, "butterbellbakery@example.com", "replied 'no thanks'", "messenger")
    with pytest.raises(ExecutorError, match="Guard blocked it at execution"):
        town.executor.execute(action_id)
    row = town.mayor.get(action_id)
    assert row["status"] == "approved" and json.loads(row["guard_result"])["verdict"] == "blocked"


def test_an_edit_can_fix_a_block_but_never_skip_one(town):
    lead = add_lead(town)
    action_id = propose(town, email(lead, body=f"Hi.\n{ADDRESS}"))      # no opt-out line
    with pytest.raises(MayorError):
        town.mayor.edit_and_approve(action_id, email(lead, body="Still no opt-out."))
    town.mayor.edit_and_approve(action_id, email(lead))
    assert town.mayor.get(action_id)["status"] == "approved"


def test_email_is_only_ever_a_dry_run(town):
    assert "send_email" not in pack.LIVE_EXTERNAL_TYPES
    action_id = propose(town, email(add_lead(town)))
    town.mayor.approve(action_id)
    outcome = town.executor.execute(action_id)
    assert outcome.dry_run and outcome.result.startswith("DRY RUN, nothing sent")
    with pytest.raises(RuntimeError, match="Nothing was sent"):           # even switched on, there's no sending code
        pack.handlers()["send_email"].run(town.conn, action_id, {})


def test_the_do_not_contact_list_matches_email_phone_and_business(town):
    lead = add_lead(town)
    other = add_lead(town, name="Butterbell Bakery & Cafe", email="other@example.com", phone="+13145550199")
    added = guard.dnc_add(town.conn, "BUTTERBELLBAKERY@example.com", "opted out", "charlie")
    assert {a["kind"] for a in added} == {"email", "business"}          # the lead using that email is blocked too
    assert guard.dnc_matches(town.conn, {"lead_id": lead})
    assert guard.dnc_matches(town.conn, {"business_name": "butterbell bakery, llc"})
    assert not guard.dnc_matches(town.conn, {"lead_id": other})         # a different business isn't
    guard.dnc_add(town.conn, "314.555.0199", "said no", "charlie")
    assert guard.dnc_matches(town.conn, {"phone": "+1 (314) 555-0199"})
    with pytest.raises(guard.GuardError):
        guard.dnc_remove(town.conn, "butterbellbakery@example.com", "", "charlie")
    assert guard.dnc_remove(town.conn, "butterbellbakery@example.com", "added by mistake", "charlie") == 2
    assert not guard.dnc_matches(town.conn, {"lead_id": lead})


def test_a_new_lead_on_the_list_is_blocked(town):
    guard.dnc_add(town.conn, "Butterbell Bakery", "owner said no", "charlie")
    action_id = town.mayor.propose("scout", "add_lead", "Butterbell Bakery (Kirkwood)",
                                   {"business_name": "Butterbell Bakery", "phone": "+13145550103"}, "no website")
    assert verdict(town, action_id)["verdict"] == "blocked"


def test_town_dnc_and_pending_show_blocked(instance_file, monkeypatch):
    runner = CliRunner()
    invoke = lambda *args: runner.invoke(launcher.app, list(args), catch_exceptions=False)
    invoke("resume")
    assert invoke("dnc", "add", "butterbellbakery@example.com", "--reason", "opted out").exit_code == 0
    assert "butterbellbakery@example.com" in invoke("dnc", "list").stdout
    town, _ = launcher._open(load_settings(instance_file), "charlie", instance_file)
    try:
        lead = add_lead(town)
        town.mayor.propose("messenger", "call_script", "Butterbell Bakery",
                           {"lead_id": lead, "for": "charlie", "business": "Butterbell Bakery"}, "no email")
    finally:
        town.close()
    pending = invoke("pending").stdout
    assert "BLOCKED" in pending and "do-not-contact" in pending and "can't be approved" in pending


def test_claude_code_cant_change_the_do_not_contact_list_on_the_real_town(instance_file, monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    result = CliRunner().invoke(launcher.app, ["dnc", "remove", "x@example.com", "--reason", "x"])
    assert result.exit_code == 1 and "Charlie's call" in result.stdout


def test_the_soft_check_sees_the_inspectors_note_and_our_approved_facts(town):
    lead = add_lead(town)
    town.conn.execute("INSERT INTO inspections (lead_id, status, problems, note, created_at) VALUES (?, 'done',"
                      " '[\"no_site\"]', 'The listed website forwards to an unrelated page.', '2026-10-26T00:00:00Z')",
                      (lead,))
    prompt = guard.soft_prompt(email(lead), {}, "", guard.inspector_note(town.conn, email(lead)))
    assert "forwards to an unrelated page" in prompt and "fictional businesses" in prompt
    assert guard.inspector_note(town.conn, {"lead_id": None}) == "(no note)"


# ---------------- Session I: the preview clause and the prices are approved facts ----------------

def test_the_soft_check_is_given_the_preview_clause_and_the_prices_as_approved_facts(town):
    from packs.websites.messenger import PREVIEW_CLAUSE
    prompt = guard.soft_prompt(email(add_lead(town)), {}, "")
    assert PREVIEW_CLAUSE in prompt and "$20" in prompt and "$4/month" in prompt
    assert "a quick preview built from your public listing" in guard.SOFT_SYSTEM


@pytest.mark.parametrize("flag", [
    "Unapproved claim: 'your finished site adds your own photos, online booking, hours and reviews'",
    "The line \"a quick preview built from your public listing\" isn't in the approved facts.",
    "Claims 'we keep it updated every month', which isn't an approved fact.",
    "The preview clause promises online booking.",
])
def test_a_flag_about_the_preview_clause_is_never_shown(town, flag):
    town.haiku.reply = json.dumps({"flags": [flag, "The subject line sounds like an invoice."]})
    action = propose(town, email(add_lead(town)))
    flags = verdict(town, action)["flags"]
    assert flags == ["Haiku: The subject line sounds like an invoice."]   # every other flag is kept


def test_flags_that_only_share_a_word_or_two_with_the_clause_are_kept():
    assert not guard.about_preview_clause("The email promises online booking and reviews they never asked for.")
    assert not guard.about_preview_clause("Pushy tone in the last line.")
    assert guard.about_preview_clause("Says your finished site adds your own photos.")


def test_dropping_soft_flags_never_clears_a_hard_block(town):
    town.haiku.reply = json.dumps({"flags": ["The preview clause is unapproved."]})
    lead = add_lead(town)
    guard.dnc_add(town.conn, "butterbellbakery@example.com", "asked not to be contacted", "charlie")
    result = verdict(town, propose(town, email(lead)))
    assert result["verdict"] == "blocked" and result["flags"] == []


def test_no_obligation_is_an_approved_fact(town):
    prompt = guard.soft_prompt(email(add_lead(town)), {}, "")
    assert "No obligation" in prompt and "no obligation" in guard.SOFT_SYSTEM
