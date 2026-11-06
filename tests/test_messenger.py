"""The Messenger, with a fake `claude` that reads the facts it was given and answers like Sonnet would. pytest
never starts the real CLI, never uses Charlie's plan, never reaches the network, and nothing is ever sent."""
import json
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from engine import claude_cli
from engine.db import utc_now
from engine.killswitch import TownStopped
from engine.settings import load_settings
from engine.town import open_town
from packs.websites import messenger, pack
from packs.websites.guard import OPT_OUT_LINE, WebsiteGuard, dnc_add
from packs.websites.messenger import (ADDRESS_MISSING, FROM, SIGN_OFF, MessengerError, assemble_email,
                                      draft_for_lead, prepare, run_messenger, unsupported, words)
from packs.websites.previews import preview_url
from packs.websites.testset_outreach import messenger_input, score_messenger
from packs.websites.testset_runner import load_cases
from tests.test_builder import FakeClaude, FakeProcess, result_line
from tests.test_guard import FakeHaiku
from tests.test_testset import no_network  # noqa: F401

ADDRESS = "P.O. Box 4521, St. Louis, MO 63105"
TOKEN = "ab" * 32
EMAIL_LEAD = ("Butterbell Bakery", "bakery", "butterbellbakery@example.com", "+13145550103")
CALL_LEAD = ("Fictional Pediatric Dentistry", "pediatric_dentistry", "", "+13145550107")
CLAUSE = "It's a quick preview built from your public listing; your finished site adds your own photos, online booking, hours and reviews, and we keep it updated every month."
from packs.websites.offer import load_offer
COST = load_offer().cost_answer()
# How a good draft names each measured problem (the words messenger.PROBLEM_WORDS looks for).
SAYS = {"no_site": "We couldn't find a website for your business.",
        "social_or_builder_only": "Right now your only page online is a Facebook page.",
        "not_mobile_friendly": "Your site is hard to read on a phone.",
        "no_https": "Browsers mark your site as not secure.",
        "outdated": "Your site looks outdated.",
        "slow": "Your site is slow to load on a phone.",
        "broken_links": "A few links on your site are broken."}


# ---------------- a fake Sonnet that reads its facts/ folder ----------------

def design_line_from(summary):
    """The one personal sentence, built only from the Critic's summary's first sentence ("" without a summary)."""
    first = (summary or "").split(". ")[0].strip().rstrip(".")
    return f"We went with {first[:1].lower()}{first[1:]}." if first else ""


def good_answer(cwd, follow_up=False, command=()):
    facts = Path(cwd) / "facts"
    lead = json.loads((facts / "lead.json").read_text(encoding="utf-8"))["business"]
    problems = json.loads((facts / "inspector.json").read_text(encoding="utf-8"))["measured_problems_biggest_first"]
    link = json.loads((facts / "preview.json").read_text(encoding="utf-8"))["preview_link"]
    name, problem = lead["business_name"], SAYS[problems[0]] if problems else ""
    # A call script is asked for with its own JSON schema (an "opener"): email-first, an email lead that asked for a call
    if lead["public_email"] and "opener" not in " ".join(map(str, command)):
        if follow_up:
            return {"subject": f"Re: A free website preview for {name}",
                    "body": f"Hi there, just checking you saw the preview we built:\n{link}\n{CLAUSE} It costs far less than "
                            f"a traditional web design firm usually charges. No worries if not."}
        line = design_line_from(json.loads((facts / "preview.json").read_text(encoding="utf-8")).get("design_summary"))
        return {"subject": f"A free website preview for {name}",
                "body": f"Hi there,\n\n{problem} We built you a free preview:\n{link}\n{CLAUSE}{' ' + line if line else ''}"
                        f"\n\nWe can host it and keep it "
                        f"secure with a monthly care plan, for far less than a traditional web design firm usually "
                        f"charges. Would you like to talk, or should we take it down?", "design_line": line}
    return {"opener": f"Hi, this is Charlie from Example Web Studio, a small St. Louis web-design studio. I'm calling "
                      f"because we built a free preview of a new website for {name}. Do you have a minute?",
            "problem": problem,
            "preview": f"We built you a free preview. {CLAUSE} I can text or email you the link.",
            "care_plan": "We'd host it and keep it secure with a monthly care plan, $4/month.",
            "objections": [{"objection": "We're busy.", "answer": "No problem, thanks for your time."},
                           {"objection": "What does it cost?", "answer": COST},
                           {"objection": "We use Facebook.", "answer": "That's fine; a site works alongside it."}],
            "voicemail": "Hi, this is Charlie from Example Web Studio. I'll try again, or email town@example.com."}


def answer_line(answer, session="m-1"):
    line = json.loads(result_line(json.dumps(answer), session=session))
    line["structured_output"] = answer
    return json.dumps(line) + "\n"


class FakeSonnet(FakeClaude):
    """Answers from the workspace's facts. `answers` overrides the first calls' answers (a dict, or a plain
    string for an unreadable reply); after those it writes a good draft."""

    def __init__(self, *answers, follow_up=False):
        super().__init__()
        self.answers, self.follow_up = list(answers), follow_up

    def __call__(self, command, cwd, env, **kwargs):
        self.calls.append({"command": command, "cwd": cwd, "env": env})
        answer = self.answers.pop(0) if self.answers else good_answer(cwd, self.follow_up, command)
        return FakeProcess([answer_line(answer) if isinstance(answer, dict) else result_line(answer)])


# ---------------- a Town with leads, inspections, builds and previews ----------------

@pytest.fixture
def town(instance_file, limits_file, monkeypatch):
    with instance_file.open("a", encoding="utf-8") as f:
        f.write(f"mailing_address: {ADDRESS}\n")
    t = open_town(load_settings(instance_file), pack.PACK_SCHEMAS, pack.handlers(), limits_file, pack.MODELS_FILE,
                  api_client=FakeHaiku(), api_key_loader=lambda: "sk-test")
    t.killswitch.resume(t.conn)
    t.mayor.guard = t.executor.guard = WebsiteGuard(t.limits, t.budget, t.llm, ADDRESS, real=False)
    monkeypatch.setattr(claude_cli, "POLL_SECONDS", 0.01)
    t.passed = set()   # the builds the Critic passed (files unchanged since)
    monkeypatch.setattr(messenger, "can_propose_publish", lambda conn, build_id: build_id in t.passed)
    yield t
    t.close()


def add_lead(town, lead=EMAIL_LEAD, problems=("no_site",)):
    name, kind, email, phone = lead
    lead_id = town.conn.execute(
        "INSERT INTO leads (business_name, business_type, area, address, phone, website, public_email, contact_route,"
        " source, source_id, source_release, created_at) VALUES (?, ?, 'Clayton', '1 Main St, Clayton, MO 63105', ?,"
        " '', ?, ?, 'overture', ?, 'r', ?)",
        (name, kind, phone, email, "email" if email else "call", f"src-{name}", utc_now())).lastrowid
    town.conn.execute("INSERT INTO inspections (lead_id, status, problems, note, created_at) VALUES (?, 'done', ?,"
                      " 'No website was found.', ?)", (lead_id, json.dumps(list(problems)), utc_now()))
    return lead_id


def ask_for_call(town, lead_id, text="Could you give me a call tomorrow?"):
    """What the Clerk records when a prospect replies asking for a call (email-first: the only way to a script)."""
    town.conn.execute("INSERT INTO inbox_messages (gmail_message_id, thread_id, lead_id, from_address, text, kind,"
                      " classified_by, created_at) VALUES (?, 't', ?, 'owner@example.com', ?, 'call_request', 'code', ?)",
                      (f"m-{lead_id}-{utc_now()}-{text[:5]}", lead_id, text, utc_now()))


def add_build(town, lead_id, passed=True):
    build_id = town.conn.execute("INSERT INTO builds (lead_id, workspace, status, created_at) VALUES (?, 'w',"
                                 " 'built', ?)", (lead_id, utc_now())).lastrowid
    if passed:
        town.passed.add(build_id)
    return build_id


def publish(town, lead_id, build_id, days_left=20.0, token=TOKEN):
    """A published preview, as the publish action records it. A negative days_left is already expired."""
    action_id = town.conn.execute("INSERT INTO actions (agent, action_type, target, content, reason, status,"
                                  " created_at) VALUES ('mayor', 'publish_preview', 't', '{}', 'r', 'executed', ?)",
                                  (utc_now(),)).lastrowid
    expires = (datetime.now(timezone.utc) + timedelta(days=days_left)).strftime("%Y-%m-%dT%H:%M:%SZ")
    town.conn.execute("INSERT INTO previews (kind, build_id, lead_id, token, dist_hash, action_id, status,"
                      " published_at, expires_at) VALUES ('preview', ?, ?, ?, 'h', ?, 'live', ?, ?)",
                      (build_id, lead_id, token, action_id, utc_now(), expires))


def ready_email_lead(town):
    lead_id = add_lead(town)
    publish(town, lead_id, add_build(town, lead_id))
    return lead_id


def sent_first_email(town, lead_id, days_ago):
    when = (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    content = {"kind": "first", "lead_id": lead_id, "subject": "A free website preview", "body": "Hi"}
    action_id = town.conn.execute("INSERT INTO actions (agent, action_type, target, content, reason, status, created_at,"
                                  " executed_at) VALUES ('messenger', 'send_email', 't', ?, 'r', 'executed', ?, ?)",
                                  (json.dumps(content), when, when)).lastrowid
    # Really sent by go-live (sent_at): a dry-run draft never counts toward a lead's history.
    town.conn.execute("INSERT INTO gmail_drafts (action_id, lead_id, draft_id, to_address, subject, fake, created_at, sent_at) VALUES (?, ?, 'seeded', 'seeded@example.com', 'seeded', 1, ?, ?)", (action_id, lead_id, when, when))


def actions(town, action_type):
    return town.conn.execute("SELECT * FROM actions WHERE action_type = ? AND status = 'proposed'",
                             (action_type,)).fetchall()


def reject_all(town):
    town.conn.execute("UPDATE actions SET status = 'rejected', reject_reason = 'redo' WHERE status = 'proposed'")


# ---------------- the gates: no draft without a live, unexpired, Critic-passed preview ----------------

def test_an_email_waits_for_a_live_unexpired_critic_passed_preview(town):
    town.cli._launch = fake = FakeSonnet()
    lead_id = add_lead(town)
    with pytest.raises(MessengerError, match="isn't published"):
        prepare(town, lead_id)                                    # nothing built or published
    build_id = add_build(town, lead_id)
    with pytest.raises(MessengerError, match="isn't published"):
        prepare(town, lead_id)                                    # Critic-passed, but not published
    publish(town, lead_id, build_id, days_left=-1)
    with pytest.raises(MessengerError, match="expired"):
        prepare(town, lead_id)                                    # published, but expired
    publish(town, lead_id, build_id, token="cd" * 32)
    town.passed.clear()
    with pytest.raises(MessengerError, match="isn't published"):
        prepare(town, lead_id)                                    # live, but its files changed since the Critic
    town.passed.add(build_id)
    assert prepare(town, lead_id).preview_url == preview_url("cd" * 32)
    assert fake.calls == []                                       # no refused gate ever reached `claude`


def test_a_good_email_is_proposed_with_the_exact_footer_and_the_guard_clears_it(town):
    town.cli._launch = FakeSonnet()
    d = draft_for_lead(town, ready_email_lead(town))
    [row] = actions(town, "send_email")
    content = json.loads(row["content"])
    assert d["guard"]["verdict"] == "clear"
    assert content["from"] == FROM == "Example Web Studio <town@example.com>"
    assert content["to"] == "butterbellbakery@example.com" and preview_url(TOKEN) in content["body"]
    assert content["body"].endswith(f"\n\nCharlie, Example Web Studio\ntown@example.com\nhttps://showcase.example.com"
                                    f"\n\n{OPT_OUT_LINE}\n{ADDRESS}")
    saved = town.conn.execute("SELECT * FROM drafts WHERE id = ?", (d["id"],)).fetchone()
    assert saved["status"] == "proposed" and saved["action_id"] == row["id"] and saved["kind"] == "email"


def test_the_sign_off_is_charlies_first_name_only():
    assert SIGN_OFF == "Charlie, Example Web Studio"
    assert unsupported("Thanks, Charlie Baker", {"phone": "+13145550103"})           # his last name is caught
    assert not unsupported("Baker Street Bakery, thanks, Charlie", {"phone": "+13145550103"})


def test_with_no_mailing_address_the_footer_says_so_and_the_guard_blocks_it(town):
    town.cli._launch = FakeSonnet()
    town.settings = replace(town.settings, mailing_address="")
    town.mayor.guard = WebsiteGuard(town.limits, town.budget, town.llm, "", real=False)
    d = draft_for_lead(town, ready_email_lead(town))
    assert ADDRESS_MISSING in d["content"]["body"] and d["guard"]["verdict"] == "blocked"


def test_a_call_script_needs_a_critic_pass_and_gets_the_link_once_published(town):
    town.cli._launch = FakeSonnet()
    lead_id = add_lead(town, CALL_LEAD)
    with pytest.raises(MessengerError, match=r"held \(no email\)"):    # email-first: nothing until they ask
        prepare(town, lead_id)
    ask_for_call(town, lead_id)
    with pytest.raises(MessengerError, match="no Critic-passed build"):
        prepare(town, lead_id)
    add_build(town, lead_id, passed=False)
    with pytest.raises(MessengerError, match="no Critic-passed build"):
        prepare(town, lead_id)
    build_id = add_build(town, lead_id)
    d = draft_for_lead(town, lead_id)                              # not published yet: no link in the script
    assert d["content"]["preview_url"] is None and d["content"]["for"] == "charlie"
    assert d["guard"]["verdict"] != "blocked"
    with pytest.raises(MessengerError, match="already has a call script waiting"):
        prepare(town, lead_id)
    reject_all(town)
    publish(town, lead_id, build_id)
    d = draft_for_lead(town, lead_id)
    # The link goes to Charlie with the script (to text or email), never into what he says.
    assert d["content"]["preview_url"] == preview_url(TOKEN) and actions(town, "send_email") == []
    assert preview_url(TOKEN) not in messenger.call_text(d["content"]["script"])
    reject_all(town)
    with pytest.raises(MessengerError, match=r"held \(no email\)"):     # a follow-up is an email: none for held
        prepare(town, lead_id, follow_up=True)


def test_a_lead_on_the_do_not_contact_list_gets_no_draft(town):
    town.cli._launch = fake = FakeSonnet()
    lead_id = ready_email_lead(town)
    dnc_add(town.conn, "butterbellbakery@example.com", "asked not to be contacted", "charlie")
    with pytest.raises(MessengerError, match="No draft"):
        draft_for_lead(town, lead_id)
    assert fake.calls == []


def test_an_uninspected_lead_gets_no_draft(town):
    lead_id = ready_email_lead(town)
    town.conn.execute("DELETE FROM inspections")
    with pytest.raises(MessengerError, match="hasn't been inspected"):
        prepare(town, lead_id)


# ---------------- fail closed, the fix round, and the lockdown ----------------

def test_an_unreadable_answer_means_no_draft_and_nothing_proposed(town):
    town.cli._launch = fake = FakeSonnet("Sure! Here's a lovely email for them...")
    d = draft_for_lead(town, ready_email_lead(town))
    assert d["answer"] is None and "fail closed" in d["error"] and len(fake.calls) == 1
    assert actions(town, "send_email") == []
    assert town.conn.execute("SELECT status FROM drafts").fetchone()["status"] == "no_draft"


def test_a_rule_miss_goes_back_to_the_same_session_once(town):
    lead_id = ready_email_lead(town)
    no_link = {"subject": "A free website preview for Butterbell Bakery",
               "body": "Hi Butterbell Bakery, we couldn't find a website for you. Call us at (314) 555-0199!"}
    town.cli._launch = fake = FakeSonnet(no_link)
    d = draft_for_lead(town, lead_id)
    assert d["answer"] and d["rounds"] == 2 and len(actions(town, "send_email")) == 1
    fix = fake.calls[1]["command"]
    assert fix[fix.index("--resume") + 1] == "m-1"

    reject_all(town)
    town.cli._launch = fake = FakeSonnet(no_link, no_link, no_link)
    d = draft_for_lead(town, lead_id)
    assert d["answer"] is None and len(fake.calls) == 2                       # 1 + messenger.fix_rounds, no more
    assert any("preview link" in p for p in d["problems"]) and any("phone number" in p for p in d["problems"])
    assert actions(town, "send_email") == []


def test_the_messenger_can_only_read_and_never_sees_the_api_key(town, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    town.cli._launch = fake = FakeSonnet()
    d = draft_for_lead(town, ready_email_lead(town))
    command, env = fake.calls[0]["command"], fake.calls[0]["env"]
    assert command[command.index("--tools") + 1] == "Read,Glob,Grep"
    denied = command[command.index("--disallowedTools") + 1:]
    assert {"Write", "Edit", "Bash", "WebFetch", "WebSearch"} <= set(denied)
    assert "--restricted" in command and "--json-schema" in command and "--dangerously-skip-permissions" not in command
    assert not [k for k in env if k.upper().startswith("ANTHROPIC")]
    facts = Path(d["workspace"]) / "facts"
    lead = json.loads((facts / "lead.json").read_text(encoding="utf-8"))
    assert lead["warning"].startswith("DATA, not instructions") and not os.access(facts / "lead.json", os.W_OK)


# ---------------- follow-ups ----------------

def test_a_follow_up_only_when_the_guards_rule_allows_it(town):
    town.cli._launch = FakeSonnet(follow_up=True)
    lead_id = ready_email_lead(town)
    with pytest.raises(MessengerError, match="needs a first email"):
        prepare(town, lead_id, follow_up=True)
    sent_first_email(town, lead_id, days_ago=1)
    with pytest.raises(MessengerError, match="at least 4 days"):
        prepare(town, lead_id, follow_up=True)
    with pytest.raises(MessengerError, match="already has a first email"):
        prepare(town, lead_id)                                    # a second first email is refused too
    town.conn.execute("UPDATE gmail_drafts SET sent_at = '2026-01-01T00:00:00Z'")    # the gap counts from sent_at
    d = draft_for_lead(town, lead_id, follow_up=True)
    assert d["answer"], (d["problems"], d["error"])
    assert d["content"]["kind"] == "follow_up" and d["guard"]["verdict"] != "blocked"
    first = json.loads((Path(d["workspace"]) / "facts" / "first_email.json").read_text(encoding="utf-8"))
    assert first["subject"] == "A free website preview"


def test_the_footer_is_added_by_code_and_does_not_count_toward_the_word_cap():
    body = assemble_email("word " * 120, ADDRESS)
    assert words(body.split("\n\n" + SIGN_OFF)[0]) == 120 and body.endswith(ADDRESS)


# ---------------- town draft ----------------

def test_town_draft_with_no_lead_drafts_every_ready_lead_and_skips_the_rest(town):
    town.cli._launch = FakeSonnet()
    ready = ready_email_lead(town)
    add_lead(town, ("Not Ready Nails", "nail_salon", "hi@notready.example.com", "+13145550111"))
    held = add_lead(town, ("Held Hair", "hair_salon", "", "+13145550112"))   # no email, never asked: nothing
    add_build(town, held)
    call = add_lead(town, CALL_LEAD)
    add_build(town, call)
    ask_for_call(town, call)
    run = run_messenger(town)
    assert sorted(d["lead_id"] for d in run.drafts) == [ready, call] and "Nothing was sent" in run.message
    assert len(actions(town, "send_email")) == 1 and len(actions(town, "call_script")) == 1


def test_town_draft_does_nothing_while_the_town_is_stopped(town):
    town.cli._launch = fake = FakeSonnet()
    lead_id = ready_email_lead(town)
    town.killswitch.stop(town.conn)
    with pytest.raises(TownStopped):
        run_messenger(town, lead_id)
    assert fake.calls == []


# ---------------- the messenger test set ----------------

def test_the_messenger_test_set_scores_good_drafts_full_marks_and_proposes_nothing(town, no_network):
    town.cli._launch = FakeSonnet()
    cases = load_cases()
    score = score_messenger(town, cases)
    reached = [c.name for c in cases if messenger_input(c)]
    assert len(reached) == 8 and not [n for n in reached if n.startswith(("09", "10", "11"))]
    assert score.earned == score.possible, score.misses
    assert no_network == [] and town.conn.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 0
    assert town.conn.execute("SELECT COUNT(*) FROM drafts WHERE status = 'testset'").fetchone()[0] == 8


def test_the_messenger_test_set_scores_a_bad_draft_down(town):
    bad = {"subject": "Hello!", "body": "Act now: a new site for $499. Our AI builds it."}
    town.cli._launch = FakeSonnet(*[bad] * 30)
    score = score_messenger(town, load_cases())
    assert score.earned < score.possible / 2


@pytest.mark.parametrize("text", ["Right now your only web presence is a social profile.",
                                  "Your business lives on a social media account.",
                                  "Your site is on a free website builder.", "We found your Facebook page."])
def test_the_ways_of_saying_social_or_builder_only_count(text):
    assert messenger.names_problem(text, ["social_or_builder_only"])
    assert not messenger.names_problem("We'd love to help your business.", ["social_or_builder_only"])


def test_the_messenger_sees_the_listed_website_and_a_refused_draft_is_kept_for_charlie(town):
    lead_id = ready_email_lead(town)
    town.conn.execute("UPDATE leads SET website = 'https://facebook.com/butterbell' WHERE id = ?", (lead_id,))
    no_link = {"subject": "A free website preview for Butterbell Bakery", "body": "Hi, we couldn't find a website."}
    town.cli._launch = FakeSonnet(no_link, no_link)
    d = draft_for_lead(town, lead_id)
    facts = json.loads((Path(d["workspace"]) / "facts" / "lead.json").read_text(encoding="utf-8"))
    assert facts["business"]["website"] == "https://facebook.com/butterbell"
    saved = town.conn.execute("SELECT status, content FROM drafts WHERE id = ?", (d["id"],)).fetchone()
    assert saved["status"] == "no_draft" and json.loads(saved["content"]) == {**no_link, "design_line": ""}
    assert actions(town, "send_email") == []


# ---------------- the outreach wording decision (2026-10-26) ----------------

def _email(body, subject="A free website preview for Butterbell Bakery"):
    lead = {"business_name": "Butterbell Bakery", "phone": "+13145550103", "public_email": "b@example.com"}
    link = preview_url(TOKEN)
    return messenger.email_problems(subject, body.format(link=link), lead, ["no_site"], link, 120)


GOOD_BODY = ("Hi there,\n\nWe couldn't find a website for Butterbell Bakery, so we built you a free preview:\n{link}\n"
             "It's a quick preview built from your public listing; your finished site adds your own photos, online "
             "booking, hours and reviews, and we keep it updated every month.\n\n"
             "We'd host it and keep it secure with a monthly care plan, for far less than a traditional web design "
             "firm usually charges. Want to talk?")


def test_a_good_email_passes_every_wording_rule():
    assert _email(GOOD_BODY) == []


def test_an_email_must_open_with_hi_there_not_the_business_name():
    problems = _email(GOOD_BODY.replace("Hi there,", "Hi Butterbell Bakery,"))
    assert any("Hi there," in p for p in problems)


@pytest.mark.parametrize("clause", ["for much less than what a typical web design agency charges",
                                    "far less than a traditional web design firm",
                                    "far less than traditional web design firms usually charge"])
def test_small_rewordings_of_the_price_clause_pass(clause):
    assert messenger.has_price_clause(f"A monthly care plan, {clause}.")


def test_an_email_without_the_price_clause_or_with_a_dollar_amount_is_refused():
    no_clause = GOOD_BODY.replace(", for far less than a traditional web design firm usually charges", "")
    assert any("price clause" in p for p in _email(no_clause))
    dollars = GOOD_BODY.replace("usually charges", "usually charges ($499)")
    assert any("dollar" in p for p in _email(dollars))


@pytest.mark.parametrize("line", ["A person reviews every site before you see it.",
                                  "A real person checks each one.", "Every site is human-reviewed."])
def test_no_draft_says_a_person_reviews_every_site(line):
    assert any("reviews every site" in p for p in _email(GOOD_BODY + " " + line))
    script = {**good_answer_call(), "care_plan": f"We'd host it with a care plan. {line}"}
    assert any("reviews every site" in p for p in call_problems(script))


@pytest.mark.parametrize("line", ["We're a WashU business.", "We're a Washington University team.",
                                  "We're students from WashU."])
def test_nothing_calls_us_a_washu_business_or_says_from_washu(line):
    assert any("WashU" in p for p in _email(GOOD_BODY + " " + line))


def test_started_by_washu_students_is_fine():
    assert _email(GOOD_BODY + " Example Web Studio was started by WashU students.") == []


def good_answer_call():
    return {"opener": "Hi, this is Charlie from Example Web Studio. I'm calling because we built a free preview of a new "
                      "website for Fictional Pediatric Dentistry. Do you have a minute?",
            "problem": "We couldn't find a website for your practice.",
            "preview": f"{CLAUSE} I can text or email you the link.", "care_plan": "A monthly care plan, $4/month.",
            "objections": [{"objection": "Busy.", "answer": "No problem."},
                           {"objection": "What does it cost?", "answer": COST},
                           {"objection": "Not now.", "answer": "Thanks for your time."}],
            "voicemail": "Hi, this is Charlie from Example Web Studio. Email town@example.com."}


def call_problems(script):
    lead = {"business_name": "Fictional Pediatric Dentistry", "phone": "+13145550107", "public_email": ""}
    lim = messenger.MessengerLimits(120, 80, 70, 60, 1, 600, 60)
    return messenger.call_problems(script, lead, ["no_site"], preview_url(TOKEN), lim)


def test_a_call_opener_must_say_why_charlie_is_calling_and_never_use_a_pretext():
    assert call_problems(good_answer_call()) == []
    vague = {**good_answer_call(), "opener": "Hi, this is Charlie from Example Web Studio. Do you have a minute?"}
    assert any("why Charlie is calling" in p for p in call_problems(vague))
    pretext = {**good_answer_call(), "opener": "Hi, this is Charlie. I'm calling because we built a free preview. "
                                               "I had a question about your website."}
    assert any("pretext" in p for p in call_problems(pretext))


def test_the_messenger_is_not_told_the_person_reviews_line():
    facts = messenger.offer()["approved_facts_about_us"]
    assert facts and not [f for f in facts if "reviews every site" in f]


def test_the_guards_soft_check_may_flag_scolding_and_accepts_the_reworded_facts():
    from packs.websites.guard import SOFT_SYSTEM
    system = " ".join(SOFT_SYSTEM.split())
    assert "scolding or shaming tone" in system
    assert "started by WashU students" in system and messenger.PRICE_CLAUSE in system


# ---------------- the preview clause (Builder v2, Charlie 2026-11-02) ----------------

def test_every_email_and_call_script_must_say_what_the_preview_is():
    assert _email(GOOD_BODY) == [] and call_problems(good_answer_call()) == []
    assert any("what the preview is" in p for p in _email(GOOD_BODY.replace(CLAUSE, "")))
    script = {**good_answer_call(), "preview": "I can text or email you the link."}
    assert any("what the preview is" in p for p in call_problems(script))


@pytest.mark.parametrize("dropped", ["your own photos", "online booking", "reviews", "keep it updated every month",
                                     "from your public listing", "quick preview"])
def test_each_part_of_the_preview_clause_is_required(dropped):
    assert messenger.preview_clause_missing(CLAUSE) == []
    assert messenger.preview_clause_missing(CLAUSE.replace(dropped, "something"))


def test_small_rewordings_of_the_preview_clause_pass():
    assert messenger.preview_clause_missing(
        "It's a quick preview, built from your public listing. Your finished site adds your own photos, online "
        "booking, your hours and reviews, and we'll keep it updated every month.") == []


def test_the_messenger_is_told_the_preview_clause_word_for_word():
    lim = messenger.MessengerLimits(120, 80, 70, 60, 1, 600, 60)
    for kind in (messenger.EMAIL, messenger.FOLLOW_UP_KIND, messenger.CALL):
        assert messenger.PREVIEW_CLAUSE in messenger.job_for(kind, lim)


# ---------------- Session I: prices in call scripts (never emails), and the link is never read aloud ----------------

def test_the_prices_come_from_offer_yaml_in_charlies_order():
    from packs.websites.offer import load_offer
    offer = load_offer()
    assert [p for _, p, _ in offer.tiers] == [10, 20, 30] and offer.recommended == "Standard"
    assert (offer.care_monthly, offer.no_upfront_monthly, offer.no_upfront_months) == (4, 5, 2)
    assert offer.cost_answer() == COST
    lim = messenger.MessengerLimits(120, 80, 70, 60, 1, 600, 60)
    assert COST in messenger.job_for(messenger.CALL, lim) and "$4/month" in messenger.job_for(messenger.CALL, lim)
    assert "$10" not in messenger.job_for(messenger.EMAIL, lim)


def test_a_good_call_script_with_our_prices_passes():
    assert call_problems(good_answer_call()) == []


def test_objection_2_must_be_the_cost_question_with_every_price_in_order():
    def with_cost(objection="What does it cost?", answer=COST):
        script = good_answer_call()
        script["objections"][1] = {"objection": objection, "answer": answer}
        return call_problems(script)
    assert any("objection 2" in p for p in with_cost(objection="We already have Facebook."))
    assert any("$5 missing" in p for p in with_cost(answer=COST.split(" Or there's")[0]))
    swapped = COST.replace("Premium is $30", "Premium is $XXX").replace("Sites start at $10", "Premium is $30. "
                                                                         "Sites start at $10")
    assert any("in this order" in p for p in with_cost(answer=swapped))


def test_the_care_plan_line_must_say_29_a_month():
    script = {**good_answer_call(), "care_plan": "We'd host it and keep it secure with a monthly care plan."}
    assert any("$4/month" in p for p in call_problems(script))


def test_any_dollar_amount_that_isnt_ours_is_refused_in_a_call_script():
    script = {**good_answer_call(), "voicemail": "Hi, it's Charlie from Example Web Studio: sites from $149. "
                                                 "Email town@example.com."}
    assert any("isn't one of our prices" in p and "$149" in p for p in call_problems(script))


def test_emails_still_carry_no_dollar_amount_not_even_our_prices():
    problems = _email(GOOD_BODY.replace("Want to talk?", "Sites start at $10. Want to talk?"))
    assert any("dollar" in p for p in problems)


@pytest.mark.parametrize("preview", [
    f"Can I text you the link? {preview_url(TOKEN)}",
    "I can text or email you the link, or you can go to showcase.example.com/p/abc right now.",
    "I can text or email you the link. Let me read it out to you.",
    "I can text or email you the link: it's showcaseexample dot com slash p.",
])
def test_a_call_script_never_has_charlie_read_a_link(preview):
    script = {**good_answer_call(), "preview": f"{CLAUSE} {preview}"}
    assert any("web address" in p or "text or email you the link" in p for p in call_problems(script))


def test_town_calls_shows_charlie_the_link_on_its_own_line(town):
    from typer.testing import CliRunner
    import launcher
    content = {"business": "Fictional Pediatric Dentistry", "phone": "(314) 555-0107", "script": good_answer_call(),
               "preview_url": preview_url(TOKEN), "lead_id": None}
    town.conn.execute("INSERT INTO actions (agent, action_type, target, content, reason, status, created_at) VALUES"
                      " ('messenger', 'call_script', 'Fictional Pediatric Dentistry', ?, 'test', 'proposed', '2026-11-02T00:00:00Z')",
                      (json.dumps(content),))
    town.conn.commit()
    result = CliRunner().invoke(launcher.app, ["calls"])
    assert "Link to text or email them after the call (don't read it out)" in result.stdout


# ---------------- "We recommend Standard", never "most people" (Charlie, 2026-11-03) ----------------

def test_the_cost_answer_recommends_standard():
    from packs.websites.offer import load_offer
    assert "We recommend Standard at $20" in load_offer().cost_answer()
    assert "most" not in load_offer().cost_answer().lower()
    script = good_answer_call()
    script["objections"][1]["answer"] = COST.replace("We recommend Standard", "Standard is")
    assert any("We recommend Standard" in p for p in call_problems(script))


@pytest.mark.parametrize("claim", ["Most people go with Standard.", "Most salons pick Standard.",
                                   "Standard is our most popular.", "Most customers choose Standard."])
def test_claims_about_what_most_customers_choose_are_refused(claim):
    script = good_answer_call()
    script["objections"][1]["answer"] = f"{COST} {claim}"
    assert any("nobody has bought yet" in p for p in call_problems(script))
    assert any("nobody has bought yet" in p for p in _email(GOOD_BODY + " " + claim))


# ---------------- email-first (Charlie, 2026-11-03) ----------------

def test_a_call_script_is_made_only_after_a_reply_asks_and_once_per_request(town):
    town.cli._launch = FakeSonnet()
    lead_id = ready_email_lead(town)
    assert messenger.draft_route({"public_email": "a@b.example"}, False) == messenger.EMAIL
    assert messenger.draft_route({"public_email": ""}, False) is None
    assert messenger.draft_route({"public_email": ""}, True) == messenger.CALL
    assert not messenger.call_requested(town.conn, lead_id)
    ask_for_call(town, lead_id)
    assert messenger.call_requested(town.conn, lead_id)
    d = draft_for_lead(town, lead_id)
    assert town.mayor.get(d["action_id"])["action_type"] == "call_script"
    assert "replied asking for a call" in town.mayor.get(d["action_id"])["reason"]
    assert not messenger.call_requested(town.conn, lead_id)     # one script per request


def test_the_follow_up_context_is_only_ever_a_really_sent_email(town):
    """A dry-run (shadow-day) first email is never the "first email" a follow-up refers to."""
    town.cli._launch = FakeSonnet(follow_up=True)
    lead_id = ready_email_lead(town)
    sent_first_email(town, lead_id, days_ago=10)
    town.conn.execute("UPDATE gmail_drafts SET sent_at = NULL")                       # it was only a draft
    with pytest.raises(MessengerError, match="needs a first email"):
        prepare(town, lead_id, follow_up=True)
    with pytest.raises(MessengerError, match="already a Gmail draft"):                # and no second draft while off
        prepare(town, lead_id)


@pytest.mark.parametrize("claim", ["We couldn't find Butterbell Bakery anywhere online.",
                                   "Butterbell Bakery isn't online at all.", "We searched everywhere and found nowhere."])
def test_the_messenger_never_claims_more_than_was_measured(claim):
    from packs.websites.messenger import wording_problems
    assert any("claims more than was measured" in p for p in wording_problems(claim))
    assert wording_problems("We couldn't find a website listed for Butterbell Bakery.") == []


def test_town_draft_and_autopilot_skip_a_lead_whose_first_email_is_already_a_shadow_draft(town):
    """Charlie, 2026-11-04: leads 1, 7 and 8 were drafted twice on the real Town."""
    from packs.websites import autopilot
    town.cli._launch = FakeSonnet()
    lead_id = ready_email_lead(town)
    sent_first_email(town, lead_id, days_ago=1)
    town.conn.execute("UPDATE gmail_drafts SET sent_at = NULL")                        # only a Gmail draft
    with pytest.raises(MessengerError, match="already a Gmail draft"):
        prepare(town, lead_id)
    assert lead_id not in messenger.eligible_leads(town)
    cycle = autopilot.Cycle()
    assert autopilot._drafts(town, cycle, follow_up=False) is None                     # autopilot step d: nothing


# ---------------- one personal line per email (Charlie, 2026-11-04) ----------------

SUMMARY = "Pomade green and brass with barber-pole stripes and a bold serif heading. Every sample is tagged."


def add_passed_review(town, build_id, summary=SUMMARY):
    town.conn.execute("INSERT INTO reviews (build_id, workspace, verdict, decided_by, summary, created_at)"
                      " VALUES (?, 'w', 'pass', 'critic', ?, ?)", (build_id, summary, utc_now()))


def test_the_design_line_comes_only_from_the_critics_passed_review_of_that_build(town):
    lead_id = ready_email_lead(town)
    build_id = town.conn.execute("SELECT id FROM builds WHERE lead_id = ?", (lead_id,)).fetchone()[0]
    add_passed_review(town, build_id)
    town.cli._launch = FakeSonnet()
    d = draft_for_lead(town, lead_id)
    line = "We went with pomade green and brass with barber-pole stripes and a bold serif heading."
    assert d["answer"]["design_line"] == line and line in d["content"]["body"]
    preview = json.loads((Path(d["workspace"]) / "facts" / "preview.json").read_text(encoding="utf-8"))
    assert preview["design_summary"] == SUMMARY


@pytest.mark.parametrize("line, ok", [
    ("We went with pomade green and brass, with barber-pole stripes.", True),
    ("We went with pomade green and brass; your customers will love it.", False),       # a claim, not design
    ("We went with sunflower yellow.", False),                                         # not in the summary
    ("We went with pomade green. And brass too.", False),                              # two sentences
])
def test_the_design_line_check_is_plain_code(line, ok):
    answer = {"body": f"Hi there,\n{line}\nMore.", "design_line": line}
    assert (messenger.design_line_problems(answer, SUMMARY) == []) == ok
    assert messenger.design_line_problems({"body": "Hi", "design_line": ""}, "") == []
    assert messenger.design_line_problems({"body": "Hi", "design_line": ""}, SUMMARY)        # one is expected


def test_a_bad_design_line_goes_back_once_then_comes_out_and_the_email_still_drafts(town):
    lead_id = ready_email_lead(town)
    build_id = town.conn.execute("SELECT id FROM builds WHERE lead_id = ?", (lead_id,)).fetchone()[0]
    add_passed_review(town, build_id)
    town.cli._launch = good = FakeSonnet()
    base = good_answer_for(town, lead_id)
    bad_line = "We went with sunflower yellow, which your regulars will love."
    bad = {**base, "body": base["body"].replace("We went with pomade", bad_line + " Pomade"), "design_line": bad_line}
    town.cli._launch = fake = FakeSonnet(bad, bad)
    d = draft_for_lead(town, lead_id)
    assert len(fake.calls) == 2                                                  # back once
    assert d["answer"] and d["answer"]["design_line"] == "" and bad_line not in d["content"]["body"]
    assert d["design_line_dropped"] is True and d["guard"]["verdict"] != "blocked"


def good_answer_for(town, lead_id):
    """The fake's good first email for this lead, as it would write it from the facts."""
    town.cli._launch = probe = FakeSonnet()
    d = draft_for_lead(town, lead_id)
    reject_all(town)
    return {"subject": d["answer"]["subject"], "body": d["answer"]["body"], "design_line": d["answer"]["design_line"]}
