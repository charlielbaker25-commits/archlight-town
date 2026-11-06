"""The Librarian test set (`town testset --agents librarian`; on demand: the review uses Charlie's plan).

A throwaway Town gets the frozen fake history in testset/librarian/history.yaml (fifteen fictional decisions with
three real patterns and one lone rejection). Then:
- the real review runs on it (the librarian model through `claude -p`, read-only, its prompt and its plain-code
  checks), and plain code scores what it PROPOSED against answers.yaml: a point for each expected lesson found
  (right agent, the right words), and a point if nothing it proposed is unsupported (no lesson on the lone
  rejection, nothing that matches no key);
- six plain-code points that need no model: a lesson with no evidence, with evidence that isn't in the pack, with
  evidence about another agent, or below the minimum count is refused; a playbook change that lowers its test-set
  score is BLOCKED by the Guard, and one that keeps the score is not.
Nothing touches the instance's own database or the real playbooks (the gate checks use a temporary folder).
"""
import json
import tempfile
from pathlib import Path
from typing import Any

from engine.db import utc_now
from engine.settings import load_settings
from engine.town import Town, open_town
from engine.yamlio import load_yaml
from packs.websites import instructions, pack
from packs.websites.guard import WebsiteGuard
from packs.websites.librarian import LibrarianLimits, gather_evidence, lesson_problems, run_review
from packs.websites.playbook_gate import propose_playbook_change
from packs.websites.testset_runner import AgentScore, Case, _plain

FOLDER = Path(__file__).resolve().parent / "testset" / "librarian"


def load_history(folder: Path = FOLDER) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    return _plain(load_yaml(folder / "history.yaml"))["actions"], _plain(load_yaml(folder / "answers.yaml"))


def seed_history(town: Town, actions: list[dict[str, Any]]) -> None:
    """Writes the frozen decisions straight into the throwaway database, exactly as listed."""
    for a in actions:
        original, edited = a.get("original"), a.get("edited")
        content = edited or original or {"target": a["target"]}
        town.conn.execute(
            "INSERT INTO actions (id, agent, action_type, target, content, reason, status, reject_reason,"
            " original_content, edited_content, created_at, decided_at) VALUES (?, ?, ?, ?, ?, 'frozen test history',"
            " ?, ?, ?, ?, '2026-10-20T15:00:00Z', '2026-10-21T15:00:00Z')",
            (a["id"], a["agent"], a["type"], a["target"], json.dumps(content), a["status"], a.get("reason"),
             json.dumps(original) if original else None, json.dumps(edited) if edited else None))


def open_history_town(folder: Path, parent: Town | None = None) -> Town:
    instance = folder / "instance.yaml"
    instance.write_text("instance: dev\npack: websites\ndatabase: town.db\nbackup_folder: backups\nstate_folder: "
                        "state\noverture:\n  region_name: Test\n  center_lat: 38.6\n  center_lon: -90.3\n"
                        "  radius_miles: 15\n  places_file: places.parquet\n", encoding="utf-8")
    town = open_town(load_settings(instance), pack.PACK_SCHEMAS, pack.handlers(), pack.LIMITS_FILE, pack.MODELS_FILE,
                     actor="charlie")
    town.mayor.guard = town.executor.guard = WebsiteGuard(town.limits, town.budget, town.llm, "", real=False)
    town.killswitch.resume(town.conn)
    if parent is not None:   # the same `claude -p` launcher as the Town running the set (a fake one in pytest)
        town.cli._launch = parent.cli._launch
        town.cli.claude_path = parent.cli.claude_path
    return town


def _matches(text: str, words: list[str]) -> bool:
    low = (text or "").lower()
    return any(w.lower() in low for w in words)


def score_review(score: AgentScore, proposed: list[dict[str, Any]], answers: dict[str, Any]) -> None:
    for key in answers["expected"]:
        found = any(p["agent"] == key["agent"] and _matches(p["text"], key["mentions"]) for p in proposed)
        score.add("lessons found", found, f"no {key['agent']} lesson like: {key['lesson']}")
    stray = [p for p in proposed if _matches(p["text"], answers["forbidden"]["mentions"])
             or not any(p["agent"] == k["agent"] and _matches(p["text"], k["mentions"]) for k in answers["expected"])]
    score.add("no unsupported lessons", not stray,
              "unsupported: " + "; ".join(f"{p['agent']}: {p['text']}" for p in stray[:3]))


def score_plain_code(score: AgentScore, town: Town) -> None:
    """The checks that hold whatever any model says."""
    lim = LibrarianLimits.read(town.limits)
    evidence = gather_evidence(town.conn, lim.max_items)
    good = {"text": "Skip chain and franchise locations.", "agent": "scout", "evidence_ids": ["A9", "A10", "A11"],
            "count": 3, "out_of": 3, "why": "every chain lead was rejected"}
    cases = {
        "no evidence refused": {**good, "evidence_ids": [], "count": 3},
        "evidence not in the pack refused": {**good, "evidence_ids": ["A9", "A10", "A999"]},
        "evidence about another agent refused": {**good, "evidence_ids": ["A1", "A2", "A3"]},
        "below the minimum refused": {**good, "evidence_ids": ["A9", "A10"], "count": 2, "out_of": 2},
    }
    score.add("a good lesson passes the checks", not lesson_problems(good, evidence, lim), "the good lesson was refused")
    for part, lesson in cases.items():
        score.add(part, bool(lesson_problems(lesson, evidence, lim)), f"{part}: it was accepted")


def score_gate(score: AgentScore, town: Town, folder: Path) -> None:
    """A playbook change that lowers its test-set score is BLOCKED; one that keeps it isn't. The scores are scripted
    (the real gate runs the agent's real test set); the Guard's rule is the real one."""
    saved = instructions.PLAYBOOKS_DIR
    instructions.PLAYBOOKS_DIR = folder / "playbooks"
    instructions.PLAYBOOKS_DIR.mkdir()
    try:
        for n, (text, agent, ids) in enumerate((("Skip chain and franchise locations.", "scout", ["A9", "A10", "A11"]),
                                                ("Build every sample section.", "builder", ["A1", "A2", "A3"]))):
            town.conn.execute("INSERT INTO lessons (id, text, agent, evidence, status, created_at) VALUES (?, ?, ?, ?,"
                              " 'approved', ?)", (900 + n, text, agent,
                                                  json.dumps({"ids": ids, "count": 3, "out_of": 3}), utc_now()))
        drop = lambda t, testset, runs: (14.0, 16) if "lesson #900" in instructions.load_playbook("scout") else (15.0, 16)
        same = lambda t, testset, runs: (6.0, 6)
        lowered = json.loads(town.mayor.get(propose_playbook_change(town, 900, scorer=drop))["guard_result"])
        kept = json.loads(town.mayor.get(propose_playbook_change(town, 901, scorer=same))["guard_result"])
        score.add("a change that lowers the score is blocked", any("lowers" in b for b in lowered["blocks"]),
                  f"not blocked: {lowered}")
        score.add("a change that keeps the score isn't blocked", not kept["blocks"], f"blocked: {kept['blocks']}")
    finally:
        instructions.PLAYBOOKS_DIR = saved


def score_librarian(town: Town, cases: list[Case]) -> AgentScore:
    actions, answers = load_history()
    score = AgentScore("librarian", model=town.routing.for_agent("librarian").model)
    with tempfile.TemporaryDirectory(prefix="librarian-testset-") as folder:
        inner = open_history_town(Path(folder), town)
        try:
            seed_history(inner, actions)
            score_plain_code(score, inner)
            run = run_review(inner)
            proposed = [dict(r) for r in inner.conn.execute("SELECT * FROM lessons WHERE status = 'proposed'")]
            if run.error:
                score.note = f"review failed: {run.error}"
            score_review(score, proposed, answers)
            score_gate(score, inner, Path(folder))
            result = run.result
            if result is not None:
                score.note = (f"review {result.wall_seconds:.0f}s, {result.usage.get('output_tokens', 0):,} out, "
                              f"{len(proposed)} proposed, {len(run.refused)} refused by plain code "
                              "(Claude plan usage, $0 billed)")
        finally:
            inner.close()
    return score
