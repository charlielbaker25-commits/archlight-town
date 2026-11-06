"""Phase 4: the Librarian's lessons, playbook changes gated by the test set, the 70/30 explore split and the operator
profile. Fake `claude -p` and scripted models only; throwaway Towns; never the network or the real playbooks."""
import json
import os
import random
from pathlib import Path

import pytest
from typer.testing import CliRunner

import launcher
from engine import profile, sitrep
from engine.jarvis import Reaper
from engine.settings import load_settings
from engine.town import open_town
from packs.websites import explore, instructions, librarian, pack, playbook_gate
from packs.websites.guard import WebsiteGuard
from packs.websites.librarian import LibrarianLimits, gather_evidence, lesson_problems, run_review
from packs.websites.testset_librarian import load_history, score_librarian, seed_history
from packs.websites.testset_runner import AgentScore
from packs.websites.townview import VIEW
from tests.test_builder import FakeClaude, result_line

LIM = LibrarianLimits(3, 0.6, 5, 400, 600, 60)
GOOD = [{"text": "Previews that look thin get rejected: build every sample section.", "agent": "builder",
         "evidence_ids": ["A1", "A2", "A3"], "count": 3, "out_of": 4, "why": "3 of 4 preview decisions"},
        {"text": "Put the business's neighborhood in the email subject.", "agent": "messenger",
         "evidence_ids": ["A5", "A6", "A7"], "count": 3, "out_of": 4, "why": "he edited 3 subjects that way"},
        {"text": "Skip chain and franchise locations.", "agent": "scout", "evidence_ids": ["A9", "A10", "A11"],
         "count": 3, "out_of": 3, "why": "every chain lead was rejected"}]
BAD = [{"text": "Double-check phone numbers in call scripts.", "agent": "messenger", "evidence_ids": ["A15"],
        "count": 1, "out_of": 1, "why": "one wrong number"},
       {"text": "Charlie loves long emails.", "agent": "messenger", "evidence_ids": [], "count": 3, "out_of": 3,
        "why": "made up"}]


def answer(lessons, retire=(), summary="Three patterns."):
    body = {"lessons": list(lessons), "retire": list(retire), "summary": summary}
    line = json.loads(result_line(json.dumps(body), session="lib-1"))
    line["structured_output"] = body
    return ([json.dumps(line) + "\n"], {})


@pytest.fixture
def town(instance_file, limits_file, tmp_path, monkeypatch):
    t = open_town(load_settings(instance_file), pack.PACK_SCHEMAS, pack.handlers(), limits_file, pack.MODELS_FILE)
    t.mayor.guard = t.executor.guard = WebsiteGuard(t.limits, t.budget, t.llm, "", real=False)
    t.killswitch.resume(t.conn)
    monkeypatch.setattr(instructions, "PLAYBOOKS_DIR", tmp_path / "playbooks")
    (tmp_path / "playbooks").mkdir()
    yield t
    t.close()


@pytest.fixture
def history(town):
    seed_history(town, load_history()[0])
    return town


def decide(town, action_id, approve=True, reason="not convincing"):
    if approve:
        town.mayor.approve(action_id)
        town.executor.execute(action_id)
    else:
        town.mayor.reject(action_id, reason)


# ---------------- 1. evidence and lessons ----------------

def test_the_evidence_pack_has_his_reasons_his_edits_and_ids(history):
    evidence = gather_evidence(history.conn)
    by_id = {i["id"]: i for i in evidence["items"]}
    assert by_id["A1"]["charlies_reason"].startswith("Looks thin") and by_id["A1"]["about"] == ["builder"]
    assert by_id["A5"]["decision"] == "edited"
    assert by_id["A5"]["what_he_changed"]["subject"]["after"].endswith("in Kirkwood")
    assert by_id["A9"]["about"] == ["scout"] and by_id["A15"]["about"] == ["messenger"]


@pytest.mark.parametrize("lesson, why", [
    ({**GOOD[2], "evidence_ids": []}, "no evidence cited"),
    ({**GOOD[2], "evidence_ids": ["A9", "A10", "A999"]}, "isn't in the pack"),
    ({**GOOD[2], "evidence_ids": ["A1", "A2", "A3"]}, "about another agent"),
    ({**GOOD[2], "evidence_ids": ["A9", "A10"], "count": 2, "out_of": 2}, "at least 3"),
    ({**GOOD[2], "count": 3, "out_of": 10}, "under 60%"),
    ({**GOOD[2], "evidence_ids": ["A9", "A10"], "count": 3}, "cites only 2"),
    ({**GOOD[2], "agent": "nobody"}, "unknown agent"),
])
def test_never_a_lesson_without_real_evidence(history, lesson, why):
    problems = lesson_problems(lesson, gather_evidence(history.conn), LIM)
    assert any(why in p for p in problems), problems


def test_a_good_lesson_passes_and_a_duplicate_doesnt(history):
    evidence = gather_evidence(history.conn)
    assert lesson_problems(GOOD[2], evidence, LIM) == []
    existing = [{"agent": "scout", "text": "Skip chain and franchise locations!", "status": "approved"}]
    assert any("already exists" in p for p in lesson_problems(GOOD[2], evidence, LIM, existing))


def test_a_review_proposes_checked_lessons_and_refuses_the_rest(history, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    fake = FakeClaude(answer(GOOD + BAD))
    history.cli._launch = fake
    run = run_review(history)
    assert len(run.proposed) == 3 and len(run.refused) == 2
    command, env = fake.calls[0]["command"], fake.calls[0]["env"]
    tools = command[command.index("--tools") + 1].split(",")
    assert tools == ["Read", "Glob", "Grep"] and "--json-schema" in command and "--restricted" in command
    assert not any(k.upper().startswith("ANTHROPIC_") for k in env)
    workspace = Path(fake.calls[0]["cwd"])
    assert (workspace / "evidence.json").is_file() and "librarian" in workspace.parts
    rows = history.conn.execute("SELECT * FROM lessons ORDER BY id").fetchall()
    assert [r["status"] for r in rows] == ["proposed"] * 3
    action = history.mayor.get(rows[0]["action_id"])
    assert action["action_type"] == "propose_lesson" and action["status"] == "proposed"
    assert "3 of 4" in action["reason"] and json.loads(action["guard_result"])["blocks"] == []
    refused = history.conn.execute("SELECT COUNT(*) FROM events WHERE event_type = 'lesson_refused'").fetchone()[0]
    assert refused == 2


def test_a_review_with_too_few_decisions_asks_no_model(town):
    fake = FakeClaude()
    town.cli._launch = fake
    run = run_review(town)
    assert fake.calls == [] and "decide more proposals" in run.summary.lower()


def test_an_unreadable_answer_proposes_nothing(history):
    history.cli._launch = FakeClaude(([result_line("not json")], {}))
    run = run_review(history)
    assert run.error and run.proposed == [] and history.conn.execute("SELECT COUNT(*) FROM lessons").fetchone()[0] == 0


def test_approving_and_rejecting_a_lesson(history):
    history.cli._launch = FakeClaude(answer(GOOD[:2]))
    first, second = run_review(history).proposed
    rows = {r["id"]: r for r in history.conn.execute("SELECT * FROM lessons")}
    decide(history, rows[first]["action_id"])
    decide(history, rows[second]["action_id"], approve=False, reason="I edit subjects for other reasons")
    status = {r["id"]: (r["status"], r["reject_reason"]) for r in librarian.lessons(history.conn)}
    assert status[first] == ("approved", None)
    assert status[second] == ("rejected", "I edit subjects for other reasons")


def test_the_guard_blocks_a_lesson_proposal_without_evidence(history):
    content = {"lesson_id": 1, "text": "x", "agent": "scout", "evidence": {"ids": [], "count": 3, "out_of": 3}}
    action = history.mayor.propose("librarian", "propose_lesson", "lesson", content, "no evidence")
    assert "no evidence" in json.loads(history.mayor.get(action)["guard_result"])["blocks"][0]
    content["evidence"] = {"ids": ["A9", "A10", "A9999"], "count": 3, "out_of": 3}
    action = history.mayor.propose("librarian", "propose_lesson", "lesson", content, "a missing item")
    assert any("A9999" in b for b in json.loads(history.mayor.get(action)["guard_result"])["blocks"])


def test_retiring_needs_an_approved_lesson_and_evidence(history):
    history.cli._launch = FakeClaude(answer(GOOD[2:]))
    [lesson_id] = run_review(history).proposed
    decide(history, history.conn.execute("SELECT action_id FROM lessons").fetchone()[0])
    history.cli._launch = FakeClaude(answer([], retire=[{"lesson_id": lesson_id, "why": "chains now reply",
                                                         "evidence_ids": ["A12", "A13"]}]))
    run = run_review(history)
    assert len(run.retirements) == 1
    decide(history, run.retirements[0])
    assert librarian.lessons(history.conn)[0]["status"] == "retired"


# ---------------- 2. playbook changes, gated by the test set ----------------

def approved_lesson(town, agent="scout", text="Skip chain and franchise locations.", ids=("A9", "A10", "A11")):
    lesson_id = librarian.propose_lesson(town, {"text": text, "agent": agent, "evidence_ids": list(ids), "count": 3,
                                                "out_of": 3, "why": "test"})
    decide(town, town.conn.execute("SELECT action_id FROM lessons WHERE id = ?", (lesson_id,)).fetchone()[0])
    return lesson_id


def guard_of(town, action_id):
    return json.loads(town.mayor.get(action_id)["guard_result"])


def test_a_playbook_change_that_lowers_the_score_is_blocked_with_both_scores(history):
    lesson_id = approved_lesson(history)
    drop = lambda t, testset, runs: (14.0, 16) if f"lesson #{lesson_id}" in instructions.load_playbook("scout") \
        else (15.0, 16)
    action = playbook_gate.propose_playbook_change(history, lesson_id, scorer=drop)
    blocks = guard_of(history, action)["blocks"]
    assert any("lowers the scout test-set score from 15.0/16 to 14.0/16" in b for b in blocks)
    with pytest.raises(Exception):
        history.mayor.approve(action)
    assert not playbook_gate.playbook_path("scout").exists()   # nothing was written


def test_an_approved_change_writes_the_playbook_and_changes_the_agents_next_instructions(history):
    lesson_id = approved_lesson(history)
    seen = []

    def scorer(t, testset, runs):
        seen.append((testset, instructions.load_playbook("scout")))
        return (16.0, 16)
    action = playbook_gate.propose_playbook_change(history, lesson_id, scorer=scorer)
    assert seen[0] == ("scout", "") and "Skip chain and franchise locations." in seen[1][1]   # tested with it
    assert not playbook_gate.playbook_path("scout").exists()                                    # gate wrote nothing
    assert guard_of(history, action)["blocks"] == []
    decide(history, action)
    written = playbook_gate.playbook_path("scout").read_text(encoding="utf-8")
    assert f"- Skip chain and franchise locations. (lesson #{lesson_id}: 3 of 3)" in written
    # The visible change: Scout's very next instructions carry the lesson.
    assert "Skip chain and franchise locations." in instructions.agent_instructions("scout", "Find leads.")


def test_an_edited_playbook_text_needs_a_new_gate(history):
    lesson_id = approved_lesson(history)
    action = playbook_gate.propose_playbook_change(history, lesson_id, scorer=lambda t, s, r: (5.0, 5))
    content = json.loads(history.mayor.get(action)["content"])
    with pytest.raises(Exception, match="isn't the text the test set checked"):
        history.mayor.edit_and_approve(action, {**content, "after_text": content["after_text"] + "\n- Also: be nice."})


def test_a_playbook_that_changed_since_the_gate_is_blocked_at_approval(history):
    lesson_id = approved_lesson(history)
    action = playbook_gate.propose_playbook_change(history, lesson_id, scorer=lambda t, s, r: (5.0, 5))
    playbook_gate.playbook_path("scout").write_text("- something else\n", encoding="utf-8")
    with pytest.raises(Exception, match="changed since the gate ran"):
        history.mayor.approve(action)


def test_no_test_set_no_playbook_change(history):
    content = {"lesson_id": 1, "agent": "mayor", "after_text": "- x", "before_text": "", "diff": "",
               "gate": {"error": "the mayor has no test set"}}
    action = history.mayor.propose("librarian", "playbook_change", "mayor playbook", content, "test")
    assert any("no test set" in b for b in guard_of(history, action)["blocks"])


def test_only_approved_lessons_go_into_a_playbook(history):
    lesson_id = librarian.propose_lesson(history, {**GOOD[2]})
    with pytest.raises(librarian.LibrarianError, match="only an approved lesson"):
        playbook_gate.propose_playbook_change(history, lesson_id, scorer=lambda t, s, r: (5.0, 5))


def test_a_retired_lesson_comes_out_of_the_playbook_through_the_same_gate(history):
    lesson_id = approved_lesson(history)
    decide(history, playbook_gate.propose_playbook_change(history, lesson_id, scorer=lambda t, s, r: (5.0, 5)))
    retire = librarian.propose_retirement(history, lesson_id, "no longer true", ["A12", "A13"],
                                          gather_evidence(history.conn))
    decide(history, retire)
    decide(history, playbook_gate.propose_playbook_change(history, lesson_id, scorer=lambda t, s, r: (5.0, 5)))
    assert "lesson #" not in playbook_gate.playbook_path("scout").read_text(encoding="utf-8")


def test_the_real_gate_runs_the_agents_own_test_set(history, monkeypatch):
    calls = []
    import packs.websites.testset_runner as runner
    monkeypatch.setattr(runner, "run_testset", lambda town, runs, agents: calls.append((runs, agents)) or
                        type("Run", (), {"earned": 19.0, "possible": 19})())
    lesson_id = approved_lesson(history)
    playbook_gate.propose_playbook_change(history, lesson_id)
    assert calls == [(1, ["scout"]), (1, ["scout"])]   # before and after, librarian.gate_runs times each


def test_only_the_playbook_handler_writes_playbooks():
    """CLAUDE.md: never write to a playbook except through an approved Librarian action. Only playbook_gate.py
    knows where a playbook file is (playbook_path) and writes files, and its one write is the handler's."""
    root = Path(pack.__file__).resolve().parent
    texts = {p.name: p.read_text(encoding="utf-8") for p in root.glob("*.py")}
    writers = sorted(n for n, t in texts.items() if "playbook_path(" in t and ".write_text(" in t)
    assert writers == ["playbook_gate.py"]
    assert texts["playbook_gate.py"].count(".write_text(") == 1
    assert "def write(" in texts["playbook_gate.py"]


# ---------------- 3. the 70/30 explore split ----------------

def stats(found=6, approved=2):
    return [{"type": "salon", "area": "Clayton", "found": found, "build_worthy": 4, "critic_passed": 3,
             "preview_approved": approved, "replies": None}]


def test_with_too_little_data_it_explores():
    choice = explore.pick([], 0.7, 5, random.Random(1))
    assert choice.mode == "explore" and "too little data" in choice.why


def test_a_proven_cell_gets_about_70_percent_of_the_runs():
    rng = random.Random(7)
    picks = [explore.pick(stats(), 0.7, 5, rng) for _ in range(1000)]
    exploit = [p for p in picks if p.mode == "exploit"]
    assert 650 <= len(exploit) <= 750
    assert all((p.business_type, p.area) == ("salon", "Clayton") for p in exploit)
    assert all((p.business_type, p.area) != ("salon", "Clayton") for p in picks if p.mode == "explore")


def test_explore_picks_the_least_tried_cells_first():
    choice = explore.pick(stats(found=2, approved=0), 0.7, 5, random.Random(3))
    assert choice.mode == "explore" and "0 lead(s) so far" in choice.why


def test_the_split_comes_from_limits_yaml_and_every_pick_is_logged(town):
    assert float(town.limits.get("explore.exploit_share")) == 0.7
    choice = explore.auto_pick(town, random.Random(2))
    row = town.conn.execute("SELECT details FROM events WHERE event_type = 'scout_pick'").fetchone()
    assert json.loads(row["details"])["why"] == choice.why and json.loads(row["details"])["exploit_share"] == 0.7


def test_cell_stats_count_each_stage(town):
    town.conn.execute("INSERT INTO leads (business_name, business_type, area, address, phone, website, public_email,"
                      " contact_route, source, source_id, source_release, created_at) VALUES ('A', 'hair_salon',"
                      " 'Clayton', '', '', '', '', 'call', 'overture', 's1', 'r', '2026-10-20T00:00:00Z')")
    [cell] = explore.cell_stats(town.conn)
    assert (cell["type"], cell["area"], cell["found"], cell["replies"]) == ("salon", "Clayton", 1, None)


def test_scout_auto_logs_its_pick_before_scouting(dev_instance, monkeypatch):
    result = CliRunner().invoke(launcher.app, ["--instance", "dev", "resume"])
    result = CliRunner().invoke(launcher.app, ["--instance", "dev", "scout", "--auto"])
    assert "Auto pick:" in result.stdout and "Why:" in result.stdout
    both = CliRunner().invoke(launcher.app, ["--instance", "dev", "scout", "--auto", "--type", "salon"])
    assert both.exit_code == 1


# ---------------- 4. the operator profile ----------------

@pytest.mark.parametrize("said, change", [
    ("shorter", ("length", "short")), ("Too long.", ("length", "short")), ("skip the costs", ("skip", "costs")),
    ("don't tell me about the money", ("skip", "costs")), ("lead with what's waiting", ("lead_with", "waiting")),
    ("more detail", ("length", "detailed")), ("what does it cost to make it shorter?", None),
    ("skip the costs and approve 4", None),
])
def test_only_explicit_feedback_counts(said, change):
    assert profile.feedback_change(said) == change


def test_reaper_files_a_proposal_and_nothing_changes_until_charlie_approves(town):
    reply = Reaper(town, VIEW).turn("shorter")
    assert reply.mode == "plain" and "Proposed #" in reply.text
    assert profile.load(town.conn)["length"] == "normal"
    [row] = town.conn.execute("SELECT * FROM actions WHERE action_type = 'profile_change'").fetchall()
    decide(town, row["id"])
    assert profile.load(town.conn)["length"] == "short"
    assert "Already set" in Reaper(town, VIEW).turn("shorter").text


def test_the_sitrep_follows_the_profile(town):
    for key, value in (("skip", "costs"), ("lead_with", "waiting")):
        action_id, _ = profile.propose_change(town, key, value, {"source": "test", "said": value})
        decide(town, action_id)
    text = sitrep.run_sitrep(town, VIEW, use_model=False).text
    assert "Spend this month" not in text
    assert text.index("Waiting for your decision") < text.index("Since the last report")


def test_reapers_instructions_carry_the_profile(town):
    action_id, _ = profile.propose_change(town, "skip", "costs", {"source": "test", "said": "skip the costs"})
    decide(town, action_id)
    assert profile.describe(profile.load(town.conn)) == "Leave out: costs."


def test_a_thumbs_down_with_a_choice_proposes_and_a_thumb_alone_only_records(town):
    alone = profile.record_rating(town, "reaper reply", -1, "a long answer")
    assert alone["action_id"] is None
    chosen = profile.record_rating(town, "reaper reply", -1, "a long answer", "shorter")
    assert chosen["action_id"] and town.mayor.get(chosen["action_id"])["action_type"] == "profile_change"
    events = town.conn.execute("SELECT COUNT(*) FROM events WHERE event_type = 'operator_feedback'").fetchone()[0]
    assert events == 2 and profile.load(town.conn)["length"] == "normal"


def test_town_sitrep_rate(dev_instance):
    runner = CliRunner()
    runner.invoke(launcher.app, ["--instance", "dev", "resume"])
    result = runner.invoke(launcher.app, ["--instance", "dev", "sitrep", "--rate", "down", "--change", "skip_costs"])
    assert result.exit_code == 0 and "Proposed #" in result.stdout


# ---------------- 5. HQ ----------------

def test_the_library_lists_lessons_with_evidence(history):
    history.cli._launch = FakeClaude(answer(GOOD[2:]))
    run_review(history)
    from engine import facts
    library = facts.village(history, VIEW)["library"]
    [lesson] = library["lessons"]
    assert lesson["status"] == "proposed" and lesson["evidence_ids"] == ["A9", "A10", "A11"] and lesson["count"] == 3
    assert "first_try_approval_rate" in library   # the town IQ meter keeps working


# ---------------- 6. the Librarian test set ----------------

def test_the_librarian_test_set_full_marks_for_the_right_lessons(town):
    town.cli._launch = FakeClaude(answer(GOOD))
    score = score_librarian(town, [])
    assert score.earned == score.possible, score.misses
    assert not os.listdir(instructions.PLAYBOOKS_DIR)   # the real (here: temporary) playbooks were never touched


def test_the_librarian_test_set_marks_down_unsupported_or_missing_lessons(town):
    town.cli._launch = FakeClaude(answer([GOOD[0], {**BAD[0], "evidence_ids": ["A15", "A5", "A6"], "count": 3,
                                                   "out_of": 3, "text": "Check the phone number in every script."}]))
    score = score_librarian(town, [])
    assert any("no messenger lesson" in m for m in score.misses) and any("unsupported" in m for m in score.misses)
    assert score.parts["lessons found"][0] == 1


def test_the_librarian_test_set_is_on_demand():
    from packs.websites.testset_runner import DEFAULT_AGENTS, ON_DEMAND
    assert "librarian" in ON_DEMAND and "librarian" not in DEFAULT_AGENTS


def test_town_librarian_commands(dev_instance):
    runner = CliRunner()
    runner.invoke(launcher.app, ["--instance", "dev", "resume"])
    lessons = runner.invoke(launcher.app, ["--instance", "dev", "librarian", "lessons"])
    assert lessons.exit_code == 0 and "No lessons yet" in lessons.stdout
    stats = runner.invoke(launcher.app, ["--instance", "dev", "librarian", "stats"])
    assert stats.exit_code == 0 and "explores" in stats.stdout
    review = runner.invoke(launcher.app, ["--instance", "dev", "librarian", "review"])
    assert "decide more proposals" in review.stdout.lower()


def test_reading_lessons_writes_nothing(history):
    """HQ reads the lessons every second; a write there (even one that changes nothing) takes SQLite's write lock and
    made chat turns wait on Charlie's laptop. Rejected lessons still read as rejected."""
    history.cli._launch = FakeClaude(answer(GOOD[:1]))
    [lesson_id] = run_review(history).proposed
    decide(history, history.conn.execute("SELECT action_id FROM lessons").fetchone()[0], approve=False,
           reason="not a pattern")
    before = history.conn.total_changes
    rows = librarian.lessons(history.conn)
    VIEW.lessons(history.conn), VIEW.latest_lesson(history.conn)
    assert history.conn.total_changes == before
    assert rows[0]["status"] == "rejected" and rows[0]["reject_reason"] == "not a pattern"


# ---------------- never an empty cell (Charlie, 2026-11-04) ----------------

def test_explore_skips_cells_with_too_few_usable_candidates():
    asked = []

    def usable(kind, area):
        asked.append((kind, area))
        return 5 if (kind, area) == ("barber", "Kirkwood") else 0
    choice = explore.pick([], 0.7, 5, random.Random(1), usable=usable, min_candidates=3)
    assert (choice.business_type, choice.area) == ("barber", "Kirkwood")
    assert "5 usable candidate(s)" in choice.why and "skipped for too few candidates" in choice.why
    assert len(asked) < 150                                         # counted as they came up (lazy)


def test_a_proven_cell_that_ran_dry_explores_instead():
    choice = explore.pick(stats(), 0.0 + 1.0, 5, random.Random(7),
                          usable=lambda k, a: 0 if (k, a) == ("salon", "Clayton") else 4, min_candidates=3)
    assert choice.mode == "explore" and (choice.business_type, choice.area) != ("salon", "Clayton")


def test_when_no_cell_has_enough_candidates_scout_is_refused():
    with pytest.raises(explore.NoUsableCell, match="explore.min_candidates"):
        explore.pick([], 0.7, 5, random.Random(1), usable=lambda k, a: 2, min_candidates=3)


def test_usable_candidates_count_scouts_own_filters_from_the_local_file(town):
    """Email-first and the other filters, in plain code on a small Overture file: no model call."""
    from tests.test_scout import _write_places
    from packs.websites.scout import usable_candidates
    _write_places(town.settings.overture.places_file)
    calls = town.conn.execute("SELECT COUNT(*) FROM costs").fetchone()[0]
    # Clayton salons in the scout fixture with a public email, confident and reachable: Shear Bliss, Cuts by Dee.
    assert usable_candidates(town, "salon", "Clayton") == 2
    assert usable_candidates(town, "salon", "Ladue") == 0           # out-of-area phone and no email
    assert town.conn.execute("SELECT COUNT(*) FROM costs").fetchone()[0] == calls
    assert int(town.limits.get("explore.min_candidates")) == 3
    with pytest.raises(explore.NoUsableCell):                        # 2 < 3 everywhere in this tiny file
        explore.auto_pick(town, random.Random(2))
