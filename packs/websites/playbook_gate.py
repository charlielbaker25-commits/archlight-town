"""Playbook changes, gated by the test set (BLUEPRINT 5.3 step 5 and 5.7).

An approved lesson becomes a PROPOSED change to its agent's playbook (packs/websites/playbooks/<agent>.md): one line
added (or, for a retired lesson, taken out). Before the proposal goes in the queue, plain code runs that agent's
test set twice, with the playbook as it is now and with the change (instructions.playbook_override: in memory, the
file isn't touched), and saves both scores in the proposal.

The Guard then refuses it, as a hard rule, when (gate_problems):
- the score with the change is lower than without it (BLOCKED, with both scores),
- the agent has no test set to check it against, or the gate didn't run,
- the playbook file changed since the gate ran, or the proposed text isn't exactly the text that was tested (an
  edit means a new gate: reject it and run `town librarian playbook <lesson> --text "..."`),
- the lesson isn't approved (or, for a removal, isn't retired), or the playbook would grow past
  librarian.playbook_max_lines.
Only the approved `playbook_change` action writes a playbook file (CLAUDE.md rule), through the handler below, and
the executor runs the Guard once more right before it.
"""
import difflib
import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from engine.db import utc_now
from engine.events import log_event
from engine.executor import Handler
from packs.websites import instructions
from packs.websites.librarian import AGENT, LibrarianError

PLAYBOOK_CHANGE = "playbook_change"
# Which test set checks which agent's playbook ("jarvis" is Reaper). The Mayor has none: its playbook can't change
# through a lesson until it does.
TESTSET_FOR = {"scout": "scout", "inspector": "inspector", "builder": "builder", "critic": "critic",
               "messenger": "messenger", "guard": "guard", "jarvis": "reaper"}
Scorer = Callable[[Any, str, int], tuple[float, int]]


def sha(text: str) -> str:
    return hashlib.sha256((text or "").strip().encode("utf-8")).hexdigest()


def playbook_path(agent: str) -> Path:
    return Path(instructions.PLAYBOOKS_DIR) / f"{agent}.md"


def current_text(agent: str) -> str:
    path = playbook_path(agent)
    return path.read_text(encoding="utf-8").strip() if path.exists() else ""


def lesson_line(lesson: dict[str, Any]) -> str:
    ev = lesson["evidence"] if isinstance(lesson["evidence"], dict) else json.loads(lesson["evidence"])
    return f"- {lesson['text']} (lesson #{lesson['id']}: {ev.get('count')} of {ev.get('out_of')})"


def changed_text(before: str, lesson: dict[str, Any], text: str | None = None, remove: bool = False) -> str:
    lines = [l for l in before.splitlines()]
    if remove:
        tag = f"(lesson #{lesson['id']}:"
        kept = [l for l in lines if tag not in l]
        if len(kept) == len(lines):
            raise LibrarianError(f"lesson #{lesson['id']} isn't in the {lesson['agent']} playbook")
        return "\n".join(kept).strip()
    line = f"- {text.strip()} (lesson #{lesson['id']})" if text else lesson_line(lesson)
    if any(f"(lesson #{lesson['id']}" in l for l in lines):
        raise LibrarianError(f"lesson #{lesson['id']} is already in the {lesson['agent']} playbook")
    return "\n".join([*lines, line]).strip()


def testset_score(town: Any, testset_agent: str, runs: int) -> tuple[float, int]:
    """The real gate: that agent's test set (runs averaged), saved like any `town testset` run."""
    from packs.websites.testset_runner import run_testset   # heavy; only when a gate really runs
    run = run_testset(town, runs=runs, agents=[testset_agent])
    return run.earned, run.possible


def run_gate(town: Any, agent: str, before: str, after: str, runs: int, scorer: Scorer | None = None) -> dict[str, Any]:
    testset = TESTSET_FOR.get(agent)
    gate: dict[str, Any] = {"agent": agent, "testset": testset, "runs": runs, "before_sha": sha(before),
                            "after_sha": sha(after), "ran_at": utc_now()}
    if not testset:
        gate["error"] = f"the {agent} has no test set to check a playbook change against"
        return gate
    score = scorer or testset_score
    town.killswitch.check()
    with instructions.playbook_override(agent, before):
        earned, possible = score(town, testset, runs)
    gate["before"] = {"earned": earned, "possible": possible}
    town.killswitch.check()
    with instructions.playbook_override(agent, after):
        earned, possible = score(town, testset, runs)
    gate["after"] = {"earned": earned, "possible": possible}
    log_event(town.conn, AGENT, "playbook_gate_run", gate)
    return gate


def _pct(score: dict[str, Any]) -> float:
    return score["earned"] / score["possible"] if score.get("possible") else 0.0


def propose_playbook_change(town: Any, lesson_id: int, text: str | None = None, scorer: Scorer | None = None,
                            on_progress: Callable[[str], None] = lambda message: None) -> int:
    """`town librarian playbook <lesson id>`: the diff, the gate (that agent's test set before and after), then the
    proposal. A change that lowers the score is still proposed, so Charlie sees it, but the Guard blocks it."""
    from packs.websites.librarian import lessons
    town.killswitch.check()
    found = [l for l in lessons(town.conn) if l["id"] == lesson_id]
    if not found:
        raise LibrarianError(f"No lesson #{lesson_id}.")
    lesson = found[0]
    if lesson["status"] not in ("approved", "retired"):
        raise LibrarianError(f"Lesson #{lesson_id} is {lesson['status']}; only an approved lesson goes into a "
                             "playbook (and a retired one comes out).")
    remove = lesson["status"] == "retired"
    agent = lesson["agent"]
    before = current_text(agent)
    after = changed_text(before, lesson, text, remove)
    runs = int(town.limits.get("librarian.gate_runs"))
    on_progress(f"Running the {TESTSET_FOR.get(agent, agent)} test set {runs}x with the playbook as it is, then "
                f"{runs}x with the change...")
    gate = run_gate(town, agent, before, after, runs, scorer)
    diff = "\n".join(difflib.unified_diff(before.splitlines(), after.splitlines(), f"playbooks/{agent}.md (now)",
                                          f"playbooks/{agent}.md (proposed)", lineterm=""))
    content = {"lesson_id": lesson_id, "agent": agent, "remove": remove, "before_text": before, "after_text": after,
               "diff": diff, "gate": gate}
    score = ("no test-set score" if gate.get("error") else
             f"test set {gate['before']['earned']}/{gate['before']['possible']} -> "
             f"{gate['after']['earned']}/{gate['after']['possible']}")
    return town.mayor.propose(AGENT, PLAYBOOK_CHANGE, f"{agent} playbook ({'remove' if remove else 'add'} lesson "
                                                     f"#{lesson_id})", content,
                              f"{'Take out' if remove else 'Add'} lesson #{lesson_id}: \"{lesson['text']}\". {score}.")


def gate_problems(conn: Any, content: dict[str, Any], max_lines: int) -> list[str]:
    """The Guard's hard rules for a playbook change (plain code; see the module note)."""
    problems = []
    agent = content.get("agent")
    gate = content.get("gate") or {}
    after = str(content.get("after_text") or "")
    if agent not in TESTSET_FOR:
        problems.append(f"the {agent} has no test set to check a playbook change against")
    if gate.get("error") or "before" not in gate or "after" not in gate:
        problems.append(f"the test-set gate didn't run ({gate.get('error') or 'no scores'})")
    elif _pct(gate["after"]) < _pct(gate["before"]):
        problems.append(f"the change lowers the {gate.get('testset')} test-set score from "
                        f"{gate['before']['earned']}/{gate['before']['possible']} to "
                        f"{gate['after']['earned']}/{gate['after']['possible']}")
    if gate.get("after_sha") != sha(after):
        problems.append("the proposed text isn't the text the test set checked (an edit needs a new gate: reject it "
                        "and run `town librarian playbook <lesson> --text \"...\"`)")
    if agent in TESTSET_FOR and gate.get("before_sha") != sha(current_text(agent)):
        problems.append(f"the {agent} playbook changed since the gate ran; run the gate again")
    lesson = conn.execute("SELECT status FROM lessons WHERE id = ?", (content.get("lesson_id"),)).fetchone()
    wanted = "retired" if content.get("remove") else "approved"
    if not lesson or lesson["status"] != wanted:
        problems.append(f"lesson #{content.get('lesson_id')} isn't {wanted}")
    if len([l for l in after.splitlines() if l.strip()]) > max_lines:
        problems.append(f"the playbook would be over {max_lines} lines (librarian.playbook_max_lines); "
                        "short playbooks get followed")
    return problems


def playbook_handlers() -> dict[str, Handler]:
    def write(conn: Any, action_id: int, content: dict[str, Any]) -> str:
        """The ONLY code that writes a playbook, and only for an approved playbook_change (the Guard re-checked
        the gate right before this ran)."""
        path = playbook_path(content["agent"])
        tmp = path.with_suffix(".md.tmp")
        tmp.write_text(content["after_text"].strip() + "\n", encoding="utf-8")
        tmp.replace(path)
        log_event(conn, AGENT, "playbook_changed", {"agent": content["agent"], "lesson": content["lesson_id"],
                                                    "remove": content.get("remove", False), "diff": content["diff"],
                                                    "gate": content["gate"]}, action_id)
        g = content["gate"]
        return (f"The {content['agent']} playbook now {'leaves out' if content.get('remove') else 'includes'} lesson "
                f"#{content['lesson_id']} (test set {g['before']['earned']}/{g['before']['possible']} -> "
                f"{g['after']['earned']}/{g['after']['possible']}). It loads on the agent's next run.")

    return {PLAYBOOK_CHANGE: Handler(PLAYBOOK_CHANGE, False, write,
                                     lambda c: f"write the {c.get('agent')} playbook (lesson #{c.get('lesson_id')})")}
