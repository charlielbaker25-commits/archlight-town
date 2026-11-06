"""Decisions, edits and orders by talking, safely: Reaper proposes, plain code checks, only Charlie acts.

How it travels:
1. Reaper's tools only RETURN proposals: `propose_decisions` (approve/reject queued actions), `propose_edit`
   (change one field of a queued action, then approve it) and `propose_orders` (start a job, like `town
   scout`). None of them can change anything.
2. check() validates every item in plain code:
   - decisions: the action exists and is pending, a BLOCKED action can't be approved (the Guard's hard rules
     re-run now), a rejection has a reason, nothing outside the queue, no duplicates;
   - edits: the action is pending, isn't a type that can't be edited (publishing), the field exists and holds
     text, and the Guard's hard rules pass on the edited content (an edit can fix a block, never skip one);
   - orders: the order exists in the pack, is never one of the terminal-only ones (engine/orders.py), and
     passes the same rules and daily caps as its command.
   What passes becomes a Proposal with the exact confirmation text; what fails is listed with why.
3. confirm() is the only function that acts, and only a person's explicit confirmation calls it: typing `y`
   in `town reaper`, or clicking Confirm on the page (a separate request carrying the proposal's id). No model
   output is ever passed to it, and Reaper has no tool that reaches it.
4. confirm() re-checks everything. If anything changed since Charlie saw the list (an item decided elsewhere,
   newly blocked, its content changed, a cap filled), NOTHING is done and he's asked to look again. Otherwise
   decisions and edits go through the Mayor (logged as decided by the person running it, "via": "reaper") and
   run right away like `town approve`; orders start in the background.
Nothing here runs while the Town is stopped.
"""
import hashlib
import json
import secrets
import sqlite3
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from engine import orders as order_rules
from engine.db import utc_now
from engine.events import log_event
from engine.executor import ExecutorError
from engine.mayor import MayorError

JARVIS = "jarvis"
VIA = "reaper"
APPROVE, REJECT = "approve", "reject"
MAX_BATCH = 50
MAX_EDIT_CHARS = 5000
MIN_OWN_WORDS = 3          # a rejection reason shorter than this is never labelled as Charlie's own words


class DecisionError(ValueError):
    """A confirmation that can't go ahead (stopped, not allowed, or the queue changed)."""


@dataclass(frozen=True)
class Decision:
    action_id: int
    decision: str          # "approve" or "reject"
    reason: str = ""


@dataclass(frozen=True)
class Edit:
    action_id: int
    field: str
    new_text: str


@dataclass(frozen=True)
class Rules:
    """What the launcher tells the decision code about this Town: the action types that can't be edited, the
    pack's orders, and how to start a confirmed order (in the background, normally)."""
    uneditable_types: frozenset[str] = frozenset()
    order_book: Any = None
    launch: Callable[[order_rules.Order], str] | None = None
    noun: Callable[[str], str] | None = None   # the pack's everyday name for an action type ("new lead")


def worded(rules: Rules, view: Any) -> Rules:
    """The rules with the pack's everyday words for action types filled in. Proposing and confirming must use
    the same words: confirm re-checks the exact lines, and nothing is done if they differ."""
    noun = getattr(view, "noun", None)
    if rules.noun is None and callable(noun):
        return replace(rules, noun=noun)
    return rules


@dataclass(frozen=True)
class Proposal:
    id: str
    items: tuple[Decision, ...]
    lines: tuple[str, ...]          # one plain entry per item, exactly what will happen
    refused: tuple[str, ...]        # items that can't be done, and why
    # A fingerprint of each decided or edited action (its exact content, reason and target) when Charlie saw
    # the list. If any of them changes before he confirms, nothing is done.
    fingerprints: tuple[str, ...] = ()
    edits: tuple[Edit, ...] = ()
    orders: tuple[order_rules.Order, ...] = ()
    created_at: str = field(default_factory=utc_now)
    # The words the lines were written with; confirm re-checks with the same ones (not part of what's compared).
    noun: Callable[[str], str] | None = field(default=None, compare=False, repr=False)

    @property
    def empty(self) -> bool:
        return not (self.items or self.edits or self.orders)

    @property
    def count(self) -> int:
        return len(self.items) + len(self.edits) + len(self.orders)

    def confirmation_text(self) -> str:
        parts = []
        if not self.empty:
            parts.append(f"Please confirm {self.count} item{'' if self.count == 1 else 's'}:")
            parts.extend(f"  {line}" for line in self.lines)
        if self.refused:
            parts.append("Not included, and why:")
            parts.extend(f"  {line}" for line in self.refused)
        if self.empty:
            parts.append("Nothing to confirm.")
        return "\n".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "lines": list(self.lines), "refused": list(self.refused),
                "items": [d.__dict__ for d in self.items], "edits": [e.__dict__ for e in self.edits],
                "orders": [{"order": o.name, "params": o.params} for o in self.orders],
                "created_at": self.created_at}


# ---------------- reading tool input (never guessing) ----------------

def parse(raw: Any) -> tuple[list[Decision], list[str]]:
    """Turns a tool call's input ({"decisions": [...]}) into Decisions. Anything malformed is refused with a
    plain reason; nothing is guessed (a missing id or an unknown verb is never "fixed")."""
    items = raw.get("decisions") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return [], ["the decision list wasn't a list"]
    decisions, refused = [], []
    for item in items[: MAX_BATCH + 1]:
        if not isinstance(item, dict):
            refused.append(f"not a decision: {str(item)[:60]}")
            continue
        verb = str(item.get("decision", "")).strip().lower()
        try:
            action_id = int(item.get("action_id"))
        except (TypeError, ValueError):
            refused.append(f"no valid action id in {str(item)[:60]}")
            continue
        if verb not in (APPROVE, REJECT):
            refused.append(f"#{action_id}: '{verb}' isn't approve or reject")
            continue
        decisions.append(Decision(action_id, verb, str(item.get("reason") or "").strip()[:500]))
    if len(items) > MAX_BATCH:
        refused.append(f"more than {MAX_BATCH} decisions at once; ask for a smaller batch")
    return decisions[:MAX_BATCH], refused


def parse_edit(raw: Any) -> tuple[list[Edit], list[str]]:
    try:
        action_id = int(raw["action_id"])
        name, text = str(raw["field"]).strip(), str(raw["new_text"])
    except (KeyError, TypeError, ValueError):
        return [], [f"not a valid edit: {str(raw)[:80]}"]
    return [Edit(action_id, name, text)], []


# ---------------- checking ----------------

def _line(row: sqlite3.Row, d: Decision, rules: "Rules | None" = None) -> str:
    noun = rules.noun if rules is not None and rules.noun else (lambda t: t.replace("_", " "))
    what = f"#{row['id']}, {noun(row['action_type'])} from the {row['agent'].title()}: {row['target']}"
    if d.decision == APPROVE:
        return f"Approve {what}"
    return f"Reject {what}. Reason: {d.reason}"


def fingerprint(row: sqlite3.Row) -> str:
    return hashlib.sha256("\x00".join(str(row[k]) for k in ("content", "reason", "target")).encode()).hexdigest()[:16]


def _pending(town: Any, action_id: int, refused: list[str]) -> sqlite3.Row | None:
    row = town.conn.execute("SELECT * FROM actions WHERE id = ?", (action_id,)).fetchone()
    if row is None:
        refused.append(f"#{action_id}: no such action")
        return None
    if row["status"] != "proposed":
        refused.append(f"#{action_id}: already {row['status']}, not in the queue")
        return None
    return row


def _blocked(town: Any, row: sqlite3.Row, content: dict[str, Any] | None, refused: list[str]) -> bool:
    try:
        verdict = town.mayor.guard_recheck(row, content)
    except Exception as err:   # a rule that can't run blocks (fail closed)
        refused.append(f"#{row['id']}: the Guard couldn't check it ({type(err).__name__}), so it can't be approved")
        return True
    if verdict and verdict.blocked:
        what = "the edited version is" if content is not None else "it's"
        refused.append(f"#{row['id']}: BLOCKED by the Guard ({what} blocked), can't be approved: "
                       + "; ".join(verdict.blocks))
        return True
    return False


def edited_content(row: sqlite3.Row, edit: Edit, rules: Rules) -> tuple[dict[str, Any] | None, str]:
    """The action's content with one field changed, or None and why not."""
    if row["action_type"] in rules.uneditable_types:
        return None, f"#{row['id']}: a {row['action_type']} can't be edited (reject it and propose it again)"
    try:
        content = json.loads(row["content"])
    except ValueError:
        content = None
    if not isinstance(content, dict) or edit.field not in content:
        return None, f"#{row['id']}: it has no field '{edit.field}'"
    if not isinstance(content[edit.field], str):
        return None, f"#{row['id']}: '{edit.field}' isn't text; use `town edit {row['id']}`"
    if not edit.new_text.strip() or len(edit.new_text) > MAX_EDIT_CHARS:
        return None, f"#{row['id']}: the new text must be 1 to {MAX_EDIT_CHARS} characters"
    if edit.new_text == content[edit.field]:
        return None, f"#{row['id']}: the new text is the same as the old"
    return {**content, edit.field: edit.new_text}, ""


def check(town: Any, decisions: list[Decision], refused: list[str] | None = None, edits: list[Edit] = (),
          orders: list[order_rules.Order] = (), rules: Rules = Rules()) -> Proposal:
    """Plain-code validation of everything Reaper proposed. Returns what may be confirmed, never acts."""
    refused = list(refused or [])
    ok: list[Decision] = []
    ok_edits: list[Edit] = []
    ok_orders: list[order_rules.Order] = []
    lines: list[str] = []
    prints: list[str] = []
    seen: set[int] = set()
    for d in decisions:
        if d.action_id in seen:
            refused.append(f"#{d.action_id}: listed twice; only the first counts")
            continue
        seen.add(d.action_id)
        row = _pending(town, d.action_id, refused)
        if row is None:
            continue
        if d.decision == REJECT and not d.reason:
            refused.append(f"#{d.action_id}: a rejection needs your reason (it becomes feedback)")
            continue
        rule = getattr(getattr(town, "mayor", None), "reject_rule", None)
        problem = rule(row["action_type"], d.reason) if d.decision == REJECT and callable(rule) else None
        if problem:
            refused.append(f"#{d.action_id}: {problem}")
            continue
        if d.decision == APPROVE and _blocked(town, row, None, refused):
            continue
        ok.append(d)
        lines.append(_line(row, d, rules))
        prints.append(fingerprint(row))
    for e in edits:
        if e.action_id in seen:
            refused.append(f"#{e.action_id}: already in this list; edit it on its own")
            continue
        seen.add(e.action_id)
        row = _pending(town, e.action_id, refused)
        if row is None:
            continue
        content, why = edited_content(row, e, rules)
        if content is None:
            refused.append(why)
            continue
        if _blocked(town, row, content, refused):
            continue
        before = json.loads(row["content"])[e.field]
        ok_edits.append(e)
        lines.append(f"Edit #{row['id']} {row['action_type']} ({row['target']}), field '{e.field}', then approve:\n"
                     f"      before: {before}\n      after:  {e.new_text}")
        prints.append(fingerprint(row))
    for order in orders:
        try:
            lines.append("Order: " + order_rules.check(town, rules.order_book, order))
            ok_orders.append(order)
        except order_rules.OrderRefused as err:
            refused.append(f"order {order.key()}: {err}")
        except Exception as err:   # a check that can't run refuses (fail closed)
            refused.append(f"order {order.key()}: couldn't be checked ({type(err).__name__}: {err})")
    return Proposal(secrets.token_hex(8), tuple(ok), tuple(lines), tuple(refused), tuple(prints),
                    tuple(ok_edits), tuple(ok_orders), noun=rules.noun)


def merge(town: Any, proposals: list[Proposal], rules: Rules = Rules()) -> Proposal:
    """Several proposals in one turn become one confirmation, checked again together."""
    return check(town, [d for p in proposals for d in p.items], [r for p in proposals for r in p.refused],
                 [e for p in proposals for e in p.edits], [o for p in proposals for o in p.orders], rules)


def _words(text: str) -> str:
    return " ".join("".join(ch if ch.isalnum() else " " for ch in (text or "").lower()).split())


def reason_source(reason: str, said: str) -> str:
    """'charlie' when the rejection reason is Charlie's own words (it appears in what he said), otherwise 'reaper'
    (the model wrote or reworded it). The page labels Reaper's wording so Charlie can change it before confirming."""
    # Whole words only, and at least three of them: a short model reason like "no" or "spam listing" would
    # otherwise match almost anything Charlie typed and hide the "(Reaper's wording)" label.
    words = _words(reason)
    if len(words.split()) < MIN_OWN_WORDS:
        return "reaper"
    return "charlie" if f" {words} " in f" {_words(said)} " else "reaper"


def cards(town: Any, proposal: Proposal, sources: dict[int, str] | None = None) -> list[dict[str, Any]]:
    """The confirmation card's items, built by plain code from the validated proposal and the database (never
    from model text). `sources` says, per rejected action id, whose words the reason is. Read-only."""
    sources = sources or {}
    line = iter(proposal.lines)
    out: list[dict[str, Any]] = []

    def row(action_id: int) -> sqlite3.Row | None:
        return town.conn.execute("SELECT id, action_type, agent, target, content FROM actions WHERE id = ?",
                                 (action_id,)).fetchone()

    for d in proposal.items:
        r = row(d.action_id)
        out.append({"kind": "decision", "decision": d.decision, "action_id": d.action_id,
                    "action_type": r["action_type"] if r else None, "agent": r["agent"] if r else None,
                    "target": r["target"] if r else None, "reason": d.reason,
                    "reason_source": sources.get(d.action_id, "reaper") if d.decision == REJECT else None,
                    "line": next(line, "")})
    for e in proposal.edits:
        r = row(e.action_id)
        try:
            before = json.loads(r["content"]).get(e.field) if r else None
        except (ValueError, AttributeError):
            before = None
        out.append({"kind": "edit", "action_id": e.action_id, "action_type": r["action_type"] if r else None,
                    "target": r["target"] if r else None, "field": e.field, "before": before, "after": e.new_text,
                    "line": next(line, "")})
    for o in proposal.orders:
        # Lead and build ids are separate numbers: the card names which is which ("lead #8", "build #12").
        about = ", ".join(f"{key.removesuffix('_id')} #{value}" for key, value in sorted(o.params.items())
                          if key.endswith("_id") and value not in (None, "", "all"))
        out.append({"kind": "order", "order": o.name, "params": o.params, "about": about, "line": next(line, "")})
    return out


def log_proposal(town: Any, proposal: Proposal) -> None:
    log_event(town.conn, JARVIS, "reaper_decisions_proposed",
              {"proposal": proposal.id, "items": [i.__dict__ for i in proposal.items],
               "edits": [e.__dict__ for e in proposal.edits],
               "orders": [{"order": o.name, "params": o.params} for o in proposal.orders],
               "refused": list(proposal.refused)})


def cancel(town: Any, proposal: Proposal, why: str = "not confirmed") -> None:
    log_event(town.conn, JARVIS, "reaper_decisions_cancelled", {"proposal": proposal.id, "why": why})


# ---------------- acting (only on Charlie's confirmation) ----------------

def confirm(town: Any, proposal: Proposal, can_decide: bool, rules: Rules = Rules()) -> list[str]:
    """Carry out a proposal Charlie has explicitly confirmed. Returns one plain line per item.
    `can_decide` is False for Claude Code on the real Town (the launcher works it out); then nothing happens."""
    if not can_decide:
        log_event(town.conn, town.actor, "command_refused",
                  {"command": "reaper confirm", "why": "Claude Code may not decide on the real Town"})
        raise DecisionError("No. Decisions on the real Town are Charlie's call. Nothing was done.")
    if town.killswitch.is_stopped():
        raise DecisionError("The Town is stood down. Nothing done. Decide after `town resume`, or with "
                            "`town approve` / `town reject` in the terminal.")
    if proposal.empty:
        raise DecisionError("Nothing to confirm.")
    if rules.noun is None and proposal.noun is not None:
        rules = replace(rules, noun=proposal.noun)   # re-check in the same words Charlie was shown
    again = check(town, list(proposal.items), edits=list(proposal.edits), orders=list(proposal.orders), rules=rules)
    if again.lines != proposal.lines or again.fingerprints != proposal.fingerprints:
        cancel(town, proposal, "the queue changed before confirmation")
        raise DecisionError("Something changed since you saw this list. Nothing done. Ask again for a fresh "
                            "list:\n" + again.confirmation_text())
    log_event(town.conn, JARVIS, "reaper_decisions_confirmed",
              {"proposal": proposal.id, "confirmed_by": town.actor, "count": proposal.count})
    results = []
    for d in proposal.items:
        try:
            if d.decision == REJECT:
                town.mayor.reject(d.action_id, d.reason, via=VIA)
                results.append(f"#{d.action_id} rejected. Reason logged as feedback.")
                continue
            town.mayor.approve(d.action_id, via=VIA)
        except MayorError as err:
            results.append(f"#{d.action_id} not decided: {err}")
            continue
        results.append(_run(town, d.action_id))
    for e in proposal.edits:
        row = town.mayor.get(e.action_id)
        content, why = edited_content(row, e, rules)
        try:
            if content is None:
                raise MayorError(why)
            town.mayor.edit_and_approve(e.action_id, content, via=VIA)
        except MayorError as err:
            results.append(f"#{e.action_id} not edited: {err}")
            continue
        results.append(f"#{e.action_id} edited (original kept). " + _run(town, e.action_id))
    for order in proposal.orders:
        if rules.launch is None:
            results.append(f"order {order.key()}: no way to start orders here; nothing started")
            continue
        log_event(town.conn, JARVIS, "order_confirmed", {"order": order.name, "params": order.params,
                                                         "ordered_by": town.actor, "via": VIA})
        results.append(rules.launch(order))
    return results


def _run(town: Any, action_id: int) -> str:
    """Right after an approval, run it once, like `town approve` does."""
    if town.killswitch.is_stopped():
        return f"#{action_id} approved and held (the Town stopped). It runs after `town resume`, then `town run`."
    try:
        outcome = town.executor.execute(action_id)
    except ExecutorError as err:
        return f"#{action_id} approved, not executed: {err}"
    if outcome.status == "executed":
        return f"#{action_id} approved and executed. {outcome.result}"
    return f"#{action_id} approved; execution FAILED. {outcome.result}"
