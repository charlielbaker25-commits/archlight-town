"""Go-live week: the send switch and every gate in front of a real send, shadow days, and who may flip it.

Every test uses FakeGmail (it records a "sent" email in sent.json and never leaves this machine). The switch is
set by writing it the way `town golive` does (golive.flip_on), because the command itself refuses to run inside
pytest, on the dev instance and for Claude Code: tests prove that too."""
import json

import pytest
from typer.testing import CliRunner

import launcher
from engine.events import log_event
from engine.executor import ExecutorError
from engine.limits import Limits
from engine.mayor import MayorError
from engine.settings import load_settings
from packs.websites import golive
from packs.websites.gmail import FakeGmail, GmailError
from packs.websites.guard import OPT_OUT_LINE, dnc_add, outreach_pause
from packs.websites.outreach import SendRefused, content_hash
from tests.test_gmail import email_content
from tests.test_messenger import ADDRESS, add_lead

runner = CliRunner()


@pytest.fixture
def town(instance_file, limits_file, monkeypatch):
    """The launcher's own wiring (Outbox, Guard, executor's live check) on a throwaway instance with an address."""
    with instance_file.open("a", encoding="utf-8") as f:
        f.write(f"mailing_address: {ADDRESS}\n")
    monkeypatch.setattr(launcher, "LIMITS_FILE", limits_file)
    t, _ = launcher._open(load_settings(instance_file), "charlie", instance_file)
    t.killswitch.resume(t.conn)
    t.fake = FakeGmail(t.settings.state_folder / "gmail-fake")
    yield t
    t.close()


def shadow(town, days=3):
    log_event(town.conn, "mayor", "shadow_started", {"by": "charlie"})
    for n in range(days):
        log_event(town.conn, "mayor", "shadow_day", {"day": f"2026-11-0{n + 1}"})


def switch_on(town):
    golive.flip_on(town.conn, town.settings, "charlie", {"dad_ok": True})


def approved_email(town, **changes):
    lead_id = add_lead(town)
    action_id = town.mayor.propose("messenger", "send_email", "Butterbell Bakery",
                                   {**email_content(lead_id), **changes}, "first email")
    town.mayor.approve(action_id)
    return lead_id, action_id


def live(town):
    shadow(town)
    switch_on(town)
    assert golive.live_problems(town.conn, town.settings, town.limits) == []


# ---------------- off by default: nothing is ever sent ----------------

def test_a_new_town_has_sending_off_and_an_approved_email_only_becomes_a_draft(town):
    assert isinstance(outbox(town).client, FakeGmail)              # tests always get the fake
    assert golive.read_switch(town.settings)["sending"] is False
    _, action_id = approved_email(town)
    outcome = town.executor.execute(action_id)
    assert outcome.dry_run and outcome.result.startswith("dry-run: not sent; saved as Gmail draft")
    assert len(town.fake.recorded()) == 1 and town.fake.sent() == []


@pytest.mark.parametrize("missing", ["switch", "address", "shadow days", "unreadable switch"])
def test_without_every_condition_nothing_is_sent(town, missing):
    shadow(town, 2 if missing == "shadow days" else 3)
    if missing != "switch":
        switch_on(town)
    if missing == "unreadable switch":
        golive.switch_path(town.settings).write_text("{not json", encoding="utf-8")
    if missing == "address":
        object.__setattr__(town.settings, "mailing_address", "")
    problems = golive.live_problems(town.conn, town.settings, town.limits)
    assert problems and golive.live_types(town.conn, town.settings, town.limits) == frozenset()
    if missing != "address":       # with no address the Guard blocks the email before anything else
        _, action_id = approved_email(town)
        assert town.executor.execute(action_id).dry_run
    assert town.fake.sent() == []


def test_nothing_sends_without_approval_even_when_live(town):
    live(town)
    lead_id = add_lead(town)
    action_id = town.mayor.propose("messenger", "send_email", "Butterbell Bakery", email_content(lead_id), "first")
    with pytest.raises(ExecutorError, match="not approved"):
        town.executor.execute(action_id)
    town.mayor.reject(action_id, "not this one")
    with pytest.raises(ExecutorError, match="not approved"):
        town.executor.execute(action_id)
    assert town.fake.sent() == [] and town.fake.recorded() == []


# ---------------- live: the approved draft is sent once ----------------

def test_when_live_the_approved_draft_is_sent_once_with_the_same_content(town):
    live(town)
    lead_id, action_id = approved_email(town)
    outcome = town.executor.execute(action_id)
    assert outcome.status == "executed" and not outcome.dry_run and outcome.result.startswith("SENT to")
    [sent] = town.fake.sent()
    [draft] = town.fake.recorded()
    assert sent["draft_id"] == draft["draft_id"] and sent["body"] == draft["body"]
    row = town.conn.execute("SELECT * FROM gmail_drafts WHERE action_id = ?", (action_id,)).fetchone()
    content = json.loads(town.mayor.get(action_id)["content"])
    assert row["sent_at"] and row["content_hash"] == content_hash(content)
    assert town.conn.execute("SELECT COUNT(*) FROM events WHERE event_type = 'email_sent'").fetchone()[0] == 1
    with pytest.raises(ExecutorError):                     # once: it's executed now
        town.executor.execute(action_id)
    assert len(town.fake.sent()) == 1


def test_switching_off_takes_effect_at_the_next_execution(town):
    live(town)
    _, first = approved_email(town)
    golive.flip_off(town.conn, town.settings, "charlie", "pause")
    assert town.executor.execute(first).dry_run and town.fake.sent() == []


def test_the_kill_switch_stops_a_live_send(town):
    live(town)
    _, action_id = approved_email(town)
    town.killswitch.stop(town.conn)
    with pytest.raises(ExecutorError, match="kill switch"):
        town.executor.execute(action_id)
    assert town.fake.sent() == []


def test_a_do_not_contact_entry_after_approval_stops_the_send(town):
    live(town)
    _, action_id = approved_email(town)
    dnc_add(town.conn, "butterbellbakery@example.com", "asked us to stop", "charlie")
    with pytest.raises(ExecutorError, match="do-not-contact"):
        town.executor.execute(action_id)
    assert town.fake.sent() == []


class FailingSend(FakeGmail):
    def send_draft(self, draft_id):
        self.send_tries = getattr(self, "send_tries", 0) + 1
        raise GmailError("HTTP 500 backend error")


def test_a_send_error_fails_the_action_once_and_is_never_retried(town):
    live(town)
    failing = FailingSend(town.settings.state_folder / "gmail-fake")
    town.executor.handlers["send_email"].run.__self__.client = failing
    _, action_id = approved_email(town)
    outcome = town.executor.execute(action_id)
    assert outcome.status == "failed" and "never retried" in outcome.result and failing.send_tries == 1
    assert town.mayor.get(action_id)["status"] == "failed"
    assert town.executor.execute_approved() == []                     # nothing left to run again


# ---------------- each gate in Outbox.send, in plain code ----------------

def claimed(town, **changes):
    """An approved email claimed by the executor (status executed), as Outbox.send sees it."""
    _, action_id = approved_email(town)
    town.conn.execute("UPDATE actions SET status = 'executed' WHERE id = ?", (action_id,))
    content = {**json.loads(town.mayor.get(action_id)["content"]), **changes}
    return action_id, content


def outbox(town):
    return town.executor.handlers["send_email"].run.__self__


@pytest.mark.parametrize("case, match", [
    ("to", "isn't the lead's stored public email"),
    ("opt-out", "opt-out line is missing"),
    ("address", "mailing address is missing"),
    ("changed", "changed after it was approved"),
    ("cap", "already went out today"),
    ("bounced lead", "bounced before"),
    ("not claimed", "isn't an approved email claimed"),
])
def test_every_send_gate_refuses_before_gmail_is_called(town, limits_file, case, match):
    live(town)
    box = outbox(town)
    if case == "to":
        action_id, content = claimed(town)
        content["to"] = "someone@else.example"
        town.conn.execute("UPDATE actions SET content = ? WHERE id = ?", (json.dumps(content), action_id))
    elif case == "opt-out":
        action_id, content = claimed(town)
        content["body"] = content["body"].replace(OPT_OUT_LINE, "")
        town.conn.execute("UPDATE actions SET content = ? WHERE id = ?", (json.dumps(content), action_id))
    elif case == "address":
        action_id, content = claimed(town)
        content["body"] = content["body"].replace(ADDRESS, "")
        town.conn.execute("UPDATE actions SET content = ? WHERE id = ?", (json.dumps(content), action_id))
    elif case == "changed":
        action_id, content = claimed(town, subject="A different subject than approved")
    elif case == "cap":
        action_id, content = claimed(town)
        real = Limits(limits_file)
        box.limits = type("CapZero", (), {"get": staticmethod(lambda key: 0 if key == "email.sends_per_day"
                                                              else real.get(key))})()
    elif case == "bounced lead":
        action_id, content = claimed(town)
        town.conn.execute("UPDATE leads SET status = 'bounced' WHERE id = ?", (content["lead_id"],))
    else:
        _, action_id = approved_email(town)
        content = json.loads(town.mayor.get(action_id)["content"])
    with pytest.raises(SendRefused, match=match):
        box.send(town.conn, action_id, content)
    assert town.fake.sent() == [] and town.fake.recorded() == []


def test_the_send_refuses_itself_when_go_live_is_off_even_if_called_directly(town):
    action_id, content = claimed(town)
    with pytest.raises(SendRefused, match="go-live isn't on"):
        outbox(town).send(town.conn, action_id, content)
    assert town.fake.sent() == []


# ---------------- the bounce and complaint pause (BLUEPRINT 4.5) ----------------

def inbox_row(town, kind, n=1):
    for i in range(n):
        town.conn.execute("INSERT INTO inbox_messages (gmail_message_id, kind, classified_by, created_at)"
                          " VALUES (?, ?, 'code', strftime('%Y-%m-%dT%H:%M:%SZ','now'))", (f"{kind}-{i}-x", kind))


def test_a_complaint_or_too_many_bounces_pause_all_outreach_until_go_live_is_switched_on_again(town):
    live(town)
    assert outreach_pause(town.conn, town.limits) is None
    inbox_row(town, "complaint")
    assert "spam complaint" in outreach_pause(town.conn, town.limits)
    lead_id = add_lead(town)
    blocked = town.mayor.propose("messenger", "send_email", "Butterbell Bakery", email_content(lead_id), "first")
    with pytest.raises(MayorError, match="outreach is paused"):
        town.mayor.approve(blocked)
    import time
    time.sleep(1.1)                                    # timestamps are to the second
    switch_on(town)                                    # Charlie reviewed it and ran `town golive` again
    assert outreach_pause(town.conn, town.limits) is None
    inbox_row(town, "bounce")                          # 1 bounce, 0 sent since: over 5%
    assert "bounce" in outreach_pause(town.conn, town.limits)


# ---------------- who may flip it ----------------

def test_town_golive_is_refused_in_tests_on_dev_and_to_claude_code(dev_instance, monkeypatch):
    result = runner.invoke(launcher.app, ["golive"], input="yes\nyes\nSEND REAL EMAIL\n")
    assert result.exit_code == 1 and "only for Charlie's real Town" in result.stdout    # real file, but a test
    result = runner.invoke(launcher.app, ["--instance", "dev", "golive"], input="yes\nyes\nSEND REAL EMAIL\n")
    assert result.exit_code == 1 and "only for Charlie's real Town" in result.stdout
    monkeypatch.setenv("CLAUDECODE", "1")
    result = runner.invoke(launcher.app, ["golive"], input="yes\nyes\nSEND REAL EMAIL\n")
    assert result.exit_code == 1 and "Charlie's call" in result.stdout
    for settings in (load_settings(dev_instance), load_settings(launcher.INSTANCE_FILES[launcher.REAL])):
        assert golive.read_switch(settings)["sending"] is False
    monkeypatch.delenv("CLAUDECODE")
    town, _ = launcher._open(load_settings(dev_instance), "charlie", dev_instance)
    try:
        refused = [json.loads(r["details"]) for r in town.conn.execute(
            "SELECT details FROM events WHERE event_type = 'golive_refused'")]
        assert refused and all(r["why"] == "not Charlie on his real instance" for r in refused)
    finally:
        town.close()


def test_golive_off_works_on_dev_and_is_logged(dev_instance):
    result = runner.invoke(launcher.app, ["--instance", "dev", "golive", "--off", "--reason", "testing"])
    assert result.exit_code == 0 and "Go-live is OFF" in result.stdout
    town, _ = launcher._open(load_settings(dev_instance), "charlie", dev_instance)
    try:
        assert town.conn.execute("SELECT COUNT(*) FROM events WHERE event_type = 'golive_off'").fetchone()[0] == 1
        assert golive.read_switch(town.settings) == {**golive.read_switch(town.settings), "sending": False}
    finally:
        town.close()


def test_the_switch_lives_in_the_state_folder_never_in_git(town, public_repo_settings):
    assert golive.switch_path(town.settings).parent == town.settings.state_folder
    import subprocess
    root = public_repo_settings
    assert subprocess.run(["git", "check-ignore", "-q", "state/golive.json"], cwd=root).returncode == 0
    assert subprocess.run(["git", "check-ignore", "-q", "dev/state/golive.json"], cwd=root).returncode == 0


# ---------------- shadow days ----------------

def run_full_loop_today(town):
    for kind in ("scout_run", "build_done", "gmail_draft_created", "clerk_finished"):
        log_event(town.conn, "x", kind, {})


def test_a_shadow_day_is_logged_once_when_the_full_loop_ran_in_dry_run(town):
    assert golive.record_shadow_day(town.conn, town.settings) is None          # not started
    assert golive.start_shadow(town.conn, "charlie") and not golive.start_shadow(town.conn, "charlie")
    log_event(town.conn, "x", "scout_run", {})
    assert golive.record_shadow_day(town.conn, town.settings) is None          # only part of the loop
    run_full_loop_today(town)
    day = golive.record_shadow_day(town.conn, town.settings)
    assert day and golive.shadow_days(town.conn) == [day]
    assert golive.record_shadow_day(town.conn, town.settings) is None          # once a day
    assert len(golive.shadow_days(town.conn)) == 1


def test_a_day_with_sending_on_or_a_real_send_is_not_a_shadow_day(town):
    golive.start_shadow(town.conn, "charlie")
    run_full_loop_today(town)
    switch_on(town)
    assert golive.record_shadow_day(town.conn, town.settings) is None
    golive.flip_off(town.conn, town.settings, "charlie", "x")
    log_event(town.conn, "messenger", "email_sent", {})
    assert golive.record_shadow_day(town.conn, town.settings) is None


def test_town_shadow_start_and_status_and_the_day_logs_itself_after_a_command(dev_instance):
    town = lambda *args: runner.invoke(launcher.app, ["--instance", "dev", *args])
    town("resume")
    result = town("shadow", "start")
    assert result.exit_code == 0 and "Shadow mode started" in result.stdout and "not yet" in result.stdout
    t, _ = launcher._open(load_settings(dev_instance), "charlie", dev_instance)
    run_full_loop_today(t)
    t.close()
    result = town("shadow", "status")
    assert "Shadow day logged" in result.stdout
    assert "Shadow days: 1 of 3" in town("shadow", "status").stdout


def test_the_checklist_shows_each_item(town):
    lines = dict((line.split(":")[0], ok) for ok, line in golive.checklist(town.conn, town.settings, town.limits,
                                                                          dad_ok=False))
    assert lines["Mailing address"] is True and lines["Shadow days"] is False
    assert lines["Gmail scopes"] is False and lines["Today's send cap"] is False       # 20 > the warm-up 5
    assert lines["Permission to use the mailing address"] is False and lines["Switch"] is True


# ---------------- shadow-day drafts never count as emails a lead was sent (review fix) ----------------

def dry_run_email(town):
    """A shadow-day approval: executed as a dry run, so only a Gmail draft (no sent_at)."""
    lead_id, action_id = approved_email(town)
    assert town.executor.execute(action_id).dry_run
    return lead_id, action_id


def test_a_lead_approved_during_shadow_can_get_its_real_first_email_after_go_live(town):
    from packs.websites.guard import lead_history
    lead_id, shadow_id = dry_run_email(town)
    assert lead_history(town.conn, lead_id)["first_sent"] is None
    live(town)
    real_id = town.mayor.propose("messenger", "send_email", "Butterbell Bakery", email_content(lead_id), "first, for real")
    town.mayor.approve(real_id)                                       # not blocked as "already has a first email"
    outcome = town.executor.execute(real_id)
    assert not outcome.dry_run and outcome.result.startswith("SENT")
    assert [s["draft_id"] for s in town.fake.sent()] == [
        town.conn.execute("SELECT draft_id FROM gmail_drafts WHERE action_id = ?", (real_id,)).fetchone()[0]]
    assert lead_history(town.conn, lead_id)["first_sent"]["id"] == real_id


def test_no_follow_up_for_a_lead_whose_only_first_email_was_a_dry_run(town):
    from packs.websites.guard import follow_up_problems
    lead_id, _ = dry_run_email(town)
    town.conn.execute("UPDATE actions SET executed_at = '2026-01-01T00:00:00Z'")      # long ago: the gap isn't it
    assert "a follow-up needs a first email that already went out" in follow_up_problems(town.conn, lead_id,
                                                                                         town.limits)
    live(town)
    follow = town.mayor.propose("messenger", "send_email", "Butterbell Bakery",
                                {**email_content(lead_id), "kind": "follow_up"}, "follow-up")
    with pytest.raises(MayorError, match="needs a first email"):
        town.mayor.approve(follow)


def test_once_a_first_email_really_sends_follow_up_rules_count_from_its_sent_at(town):
    from packs.websites.guard import follow_up_problems
    live(town)
    lead_id, action_id = approved_email(town)
    town.executor.execute(action_id)
    assert any("at least 4 days" in p for p in follow_up_problems(town.conn, lead_id, town.limits))
    town.conn.execute("UPDATE actions SET executed_at = '2026-01-01T00:00:00Z'")      # executed_at isn't the clock
    assert any("at least 4 days" in p for p in follow_up_problems(town.conn, lead_id, town.limits))
    town.conn.execute("UPDATE gmail_drafts SET sent_at = '2026-01-01T00:00:00Z'")     # sent_at is
    assert follow_up_problems(town.conn, lead_id, town.limits) == []


def test_the_daily_cap_still_counts_dry_runs(town):
    from packs.websites.guard import sends_counted_today
    dry_run_email(town)
    assert sends_counted_today(town.conn) == 1                          # shadow days behave like the real thing


def test_town_golive_warns_about_the_shadow_drafts_right_after_the_switch_flips():
    assert golive.SHADOW_DRAFTS_WARNING == (
        "The shadow-day drafts in Gmail are NOT sent and never will be. Don't press Send on any draft in Gmail "
        "yourself: that skips the Guard, the do-not-contact list and the Clerk. Delete them in Gmail when you're "
        "done reviewing; `town draft` proposes fresh emails for those leads.")
    source = (launcher.ROOT / "launcher.py").read_text(encoding="utf-8")
    flip = source.index("golive.flip_on(town.conn, settings, CHARLIE")
    assert flip < source.index("golive.SHADOW_DRAFTS_WARNING", flip) < source.index("@app.command", flip)


def test_the_guard_test_set_scores_full_marks_with_really_sent_seeds(town):
    """The Guard test set's seeded "already sent" emails carry sent_at (only really sent emails count now), including
    the filler email for another business that has no saved lead (the daily-cap case)."""
    from packs.websites.testset_outreach import score_guard
    from packs.websites.testset_runner import load_cases
    score = score_guard(town, load_cases())
    assert score.earned == score.possible == 19, score.misses


# ---------------- a shadow-day draft counts as drafted while sending is off (Charlie, 2026-11-04) ----------------

def test_no_second_shadow_draft_for_the_same_lead(town):
    from packs.websites.guard import lead_history
    lead_id, _ = dry_run_email(town)
    history = lead_history(town.conn, lead_id)
    assert history["first_drafted"] and history["first_sent"] is None          # drafted, never "sent"
    again = town.mayor.propose("messenger", "send_email", "Butterbell Bakery", email_content(lead_id), "a second one")
    blocks = json.loads(town.mayor.get(again)["guard_result"])["blocks"]
    assert any("already a Gmail draft from a shadow day" in b for b in blocks)
    with pytest.raises(MayorError, match="shadow day"):
        town.mayor.approve(again)


def test_after_go_live_exactly_one_fresh_first_email_per_shadow_drafted_lead(town):
    from packs.websites.guard import lead_history
    lead_id, _ = dry_run_email(town)
    live(town)
    assert lead_history(town.conn, lead_id)["first_drafted"] is None           # eligible once go-live is on
    fresh = town.mayor.propose("messenger", "send_email", "Butterbell Bakery", email_content(lead_id), "fresh")
    second = town.mayor.propose("messenger", "send_email", "Butterbell Bakery", email_content(lead_id), "twice?")
    assert any("sent or waiting" in b for b in json.loads(town.mayor.get(second)["guard_result"])["blocks"])
    town.mayor.reject(second, "duplicate")
    town.mayor.approve(fresh)
    assert town.executor.execute(fresh).result.startswith("SENT")
    third = town.mayor.propose("messenger", "send_email", "Butterbell Bakery", email_content(lead_id), "again?")
    assert any("sent or waiting" in b for b in json.loads(town.mayor.get(third)["guard_result"])["blocks"])
    assert len(town.fake.sent()) == 1


def test_switching_off_again_counts_the_shadow_drafts_again(town):
    from packs.websites.guard import lead_history
    lead_id, _ = dry_run_email(town)
    live(town)
    golive.flip_off(town.conn, town.settings, "charlie", "pause")
    assert lead_history(town.conn, lead_id)["first_drafted"]                    # off: drafted, so no re-draft


def test_no_follow_up_from_a_shadow_draft_before_a_real_send(town):
    from packs.websites.guard import follow_up_problems
    lead_id, _ = dry_run_email(town)
    town.conn.execute("UPDATE actions SET executed_at = '2026-01-01T00:00:00Z'")
    assert "a follow-up needs a first email that already went out" in follow_up_problems(town.conn, lead_id, town.limits)
