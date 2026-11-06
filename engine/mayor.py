"""The Mayor: the approval queue. Every proposal comes here and waits for Charlie.

An action only moves forward through its statuses:
    proposed -> approved -> executed (or failed)
    proposed -> rejected            (a reason is required, and it becomes feedback)
The Mayor never runs anything itself; the executor runs approved actions.
Every Mayor call writes an event, including refused ones, so the log shows
exactly what was asked and what happened.
"""
import json
import sqlite3
from typing import Any, Callable

from engine.db import transaction, utc_now
from engine.events import log_event
from engine.guard import Guard, GuardVerdict, stored_flags

MAYOR = "mayor"


class MayorError(ValueError):
    """A request the Mayor refused (unknown action, wrong status, missing reason)."""


def _via(via: str | None) -> dict[str, str]:
    return {"via": via} if via else {}


def to_json(content: dict[str, Any]) -> str:
    return json.dumps(content, sort_keys=True, default=str)


class Mayor:
    def __init__(self, conn: sqlite3.Connection, town_stopped: Callable[[], bool], actor: str = "charlie") -> None:
        self.conn = conn
        # Who is deciding through this Mayor: "charlie", or "claude-code" on the dev instance.
        self.actor = actor
        # Agents can't propose while the Town is stopped. Charlie can still
        # approve or reject what's already waiting; nothing runs until resume.
        self.town_stopped = town_stopped
        # The pack's guard (engine/guard.py), set by the launcher. It checks every proposal, and a blocked
        # action can't be approved. None = no guard (engine tests).
        self.guard: Guard | None = None
        # Optional, set by the launcher from the pack: an extra plain-code rule for a rejection reason of some
        # action type (e.g. "say which part you're rejecting"). Returns the problem in words, or None.
        self.reject_rule: Callable[[str, str], str | None] | None = None

    # ---- proposals (from agents) ----

    def propose(
        self,
        agent: str,
        action_type: str,
        target: str,
        content: dict[str, Any],
        reason: str,
        cost_estimate_usd: float = 0.0,
    ) -> int:
        """Saves the proposal for Charlie. With a guard, it's checked first and the result is saved with it:
        a BLOCKED proposal is still saved (so Charlie sees what was stopped and why), but can't be approved."""
        verdict: GuardVerdict | None = None
        if self.guard and not self.town_stopped() and isinstance(content, dict):
            # Outside the transaction: the guard's soft check may make a (logged) model call.
            verdict = self.guard.check(self.conn, action_type, target, content, float(cost_estimate_usd))

        def work() -> int:
            if self.town_stopped():
                raise MayorError("The Town is stopped. No new proposals until `town resume`.")
            if not isinstance(content, dict):
                raise MayorError("content must be a dict")
            for name, value in (("agent", agent), ("action_type", action_type),
                                ("target", target), ("reason", reason)):
                if not str(value or "").strip():
                    raise MayorError(f"{name} is required")
            cur = self.conn.execute(
                "INSERT INTO actions (agent, action_type, target, content, reason, cost_estimate_usd,"
                " guard_result, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'proposed', ?)",
                (agent, action_type, target, to_json(content), reason, float(cost_estimate_usd),
                 verdict.to_json() if verdict else None, utc_now()),
            )
            action_id = int(cur.lastrowid)
            log_event(
                self.conn, MAYOR, "action_proposed",
                {"by": agent, "action_type": action_type, "target": target, "content": content,
                 "reason": reason, "cost_estimate_usd": cost_estimate_usd},
                action_id,
            )
            if verdict:
                log_event(self.conn, "guard", "guard_blocked" if verdict.blocked else "guard_checked",
                          {"verdict": verdict.verdict, "blocks": list(verdict.blocks), "flags": list(verdict.flags)},
                          action_id)
            return action_id

        return self._run("propose_refused", None, work, requested_by=agent)

    def guard_recheck(self, row: sqlite3.Row, content: dict[str, Any] | None = None) -> GuardVerdict | None:
        """The guard's hard rules again for a waiting action (its content, or an edit of it), keeping the soft
        flags it was proposed with. None without a guard."""
        if not self.guard:
            return None
        verdict = self.guard.recheck(self.conn, row["action_type"], row["target"],
                                     json.loads(row["content"]) if content is None else content,
                                     float(row["cost_estimate_usd"] or 0), row["id"])
        return verdict.with_flags(stored_flags(row["guard_result"]))

    # ---- the queue (for Charlie) ----

    def list_pending(self) -> list[sqlite3.Row]:
        rows = self.conn.execute("SELECT * FROM actions WHERE status = 'proposed' ORDER BY id").fetchall()
        log_event(self.conn, MAYOR, "queue_viewed", {"pending": len(rows)})
        return rows

    def get(self, action_id: int) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM actions WHERE id = ?", (action_id,)).fetchone()
        if row is None:
            raise MayorError(f"No action #{action_id}")
        return row

    # `via` says how the decision reached the Mayor when it wasn't a `town` command: "reaper" when Charlie
    # confirmed it in Reaper's chat. The decision is still the actor's own (Charlie's), never Reaper's.
    def approve(self, action_id: int, via: str | None = None) -> None:
        def work() -> None:
            row = self._require_pending(action_id)
            self._require_guard_clear(row)
            self._set_decision(action_id, "approved")
            log_event(self.conn, MAYOR, "action_approved", {"decided_by": self.actor, **_via(via)}, action_id)

        self._run("approve_refused", action_id, work)

    def reject(self, action_id: int, reason: str, via: str | None = None) -> None:
        reason = (reason or "").strip()

        def work() -> None:
            if not reason:
                raise MayorError("A reject reason is required. It becomes feedback for the Town.")
            row = self._require_pending(action_id)
            problem = self.reject_rule(row["action_type"], reason) if self.reject_rule else None
            if problem:
                raise MayorError(f"Action #{action_id}: {problem}")
            self._set_decision(action_id, "rejected", reject_reason=reason)
            log_event(self.conn, MAYOR, "action_rejected",
                      {"decided_by": self.actor, "reject_reason": reason, **_via(via)}, action_id)

        self._run("reject_refused", action_id, work)

    def edit_and_approve(self, action_id: int, new_content: dict[str, Any], via: str | None = None) -> None:
        def work() -> None:
            if not isinstance(new_content, dict):
                raise MayorError("edited content must be a dict")
            row = self._require_pending(action_id)
            self._require_guard_clear(row, new_content)   # an edit can fix a block (say, a missing line), never skip it
            original = row["content"]
            edited = to_json(new_content)
            # Both versions are kept: the original and the edit are Charlie's feedback.
            self.conn.execute(
                "UPDATE actions SET content = ?, original_content = ?, edited_content = ? WHERE id = ?",
                (edited, original, edited, action_id),
            )
            self._set_decision(action_id, "approved")
            log_event(
                self.conn, MAYOR, "action_edited_and_approved",
                {"decided_by": self.actor, "original_content": json.loads(original),
                 "edited_content": new_content, **_via(via)},
                action_id,
            )

        self._run("edit_refused", action_id, work)

    # ---- internals ----

    def _run(self, refused_event: str, action_id: int | None, work: Callable[[], Any],
             requested_by: str | None = None) -> Any:
        """Do the work in one transaction. If the Mayor refuses, everything rolls back
        and the refusal itself is logged, then the error goes back to the caller."""
        requested_by = requested_by or self.actor
        try:
            with transaction(self.conn):
                return work()
        except MayorError as err:
            log_event(self.conn, MAYOR, refused_event,
                      {"why": str(err), "requested_action_id": action_id, "requested_by": requested_by})
            raise

    def _require_pending(self, action_id: int) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM actions WHERE id = ?", (action_id,)).fetchone()
        if row is None:
            raise MayorError(f"No action #{action_id}")
        if row["status"] != "proposed":
            raise MayorError(f"Action #{action_id} is already {row['status']}")
        return row

    def _require_guard_clear(self, row: sqlite3.Row, content: dict[str, Any] | None = None) -> None:
        """The guard's hard rules run again at approval (a do-not-contact entry or today's send count may have
        changed since the proposal). Blocked = refused, with the reasons."""
        verdict = self.guard_recheck(row, content)
        if verdict and verdict.blocked:
            raise MayorError(f"Action #{row['id']} is BLOCKED by the Guard and can't be approved: "
                             + "; ".join(verdict.blocks))

    def _set_decision(self, action_id: int, status: str, reject_reason: str | None = None) -> None:
        self.conn.execute(
            "UPDATE actions SET status = ?, reject_reason = ?, decided_at = ?"
            " WHERE id = ? AND status = 'proposed'",
            (status, reject_reason, utc_now(), action_id),
        )
