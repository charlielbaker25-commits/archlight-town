"""The executor: the only code that carries out an action, and only an approved one.

Rules, each enforced here in plain code:
- It refuses any action whose status isn't 'approved' (proposed, rejected, executed, failed).
- It runs each action at most once. It "claims" the action by flipping it from
  approved to executed in one locked database step, before doing any work. If two
  processes try at once, only one claim succeeds.
- It refuses everything while the Town is stopped (the kill switch).
- External actions (anything that touches the outside world) are dry-run unless
  their type has been deliberately switched on: in `live_external_types` (fixed when the
  Town opens), or by the pack's `live_now` check, asked at the moment each action runs (the
  website pack's go-live switch uses it, so turning sending off takes effect at once).
  Internal actions (they only change the Town's own records, like saving an
  approved lead) really run, because that's what approving them means.
  A dry run can also "rehearse" if its handler says how (the website pack saves
  an approved email as a Gmail draft instead of sending it).
- If the pack gave it a guard, the guard's hard rules run once more right before
  the action is claimed. Blocked = refused (it stays approved, so a block that
  clears later, like tomorrow's send cap, can run with `town run`).
"""
import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Callable

from engine.db import transaction, utc_now
from engine.events import log_event
from engine.guard import Guard, stored_flags

EXECUTOR = "executor"


class ExecutorError(RuntimeError):
    """The executor refused to run an action."""


@dataclass(frozen=True)
class Handler:
    """How to carry out one action type. Packs register these."""
    action_type: str
    external: bool
    # Does the work and returns a short description of what happened.
    run: Callable[[sqlite3.Connection, int, dict[str, Any]], str]
    # Describes what it would do, for dry-run output.
    describe: Callable[[dict[str, Any]], str]
    # Optional: the safe thing a DRY RUN does instead of only describing (for example, saving the email as a
    # draft for review instead of sending it). Returns a short description. If it raises, the action is
    # marked failed, never executed. None = a dry run just describes.
    rehearse: Callable[[sqlite3.Connection, int, dict[str, Any]], str] | None = None


@dataclass(frozen=True)
class Outcome:
    action_id: int
    status: str        # 'executed' or 'failed'
    dry_run: bool
    result: str


class Executor:
    def __init__(
        self,
        conn: sqlite3.Connection,
        handlers: dict[str, Handler],
        town_stopped: Callable[[], bool],
        live_external_types: frozenset[str] = frozenset(),
    ) -> None:
        self.conn = conn
        self.handlers = handlers
        self.town_stopped = town_stopped
        # External action types switched on for real. Empty until shadow week passes.
        self.live_external_types = live_external_types
        # The pack's guard (set by the launcher, like the Mayor's). None = no guard (engine tests).
        self.guard: Guard | None = None
        # Optional: external types that are live RIGHT NOW (asked at each execution, after the guard). None = only
        # live_external_types. Fail closed: if it raises, nothing extra is live.
        self.live_now: Callable[[], frozenset[str]] | None = None

    def execute(self, action_id: int) -> Outcome:
        if self.town_stopped():
            self._refuse(action_id, "the Town is stopped (kill switch)")
        row = self.conn.execute("SELECT * FROM actions WHERE id = ?", (action_id,)).fetchone()
        if row is None:
            self._refuse(None, f"no action #{action_id}")
        if row["status"] != "approved":
            self._refuse(action_id, f"action #{action_id} is {row['status']}, not approved")
        handler = self.handlers.get(row["action_type"])
        if handler is None:
            self._refuse(action_id, f"no handler for action type '{row['action_type']}'")

        content = json.loads(row["content"])  # read before claiming, so a bad row can't be left half-run
        self._guard_check(row, content)
        self._claim(action_id)

        dry_run = handler.external and handler.action_type not in self.live_external_types | self._live_now()
        try:
            if dry_run and handler.rehearse:
                result = handler.rehearse(self.conn, action_id, content)
            elif dry_run:
                result = "DRY RUN, nothing sent: would " + handler.describe(content)
            else:
                result = handler.run(self.conn, action_id, content)
        except Exception as err:  # the action failed; record it, never retry automatically
            return self._finish(action_id, "failed", dry_run, f"{type(err).__name__}: {err}")
        return self._finish(action_id, "executed", dry_run, result)

    def execute_approved(self) -> list[Outcome]:
        """Run every approved action that hasn't run yet, oldest first."""
        ids = [r["id"] for r in self.conn.execute(
            "SELECT id FROM actions WHERE status = 'approved' ORDER BY id")]
        return [self.execute(i) for i in ids]

    # ---- internals ----

    def _live_now(self) -> frozenset[str]:
        if self.live_now is None:
            return frozenset()
        try:
            return frozenset(self.live_now())
        except Exception:
            return frozenset()

    def _guard_check(self, row: sqlite3.Row, content: dict[str, Any]) -> None:
        """The guard's hard rules, once more, at the last moment before anything runs."""
        if not self.guard:
            return
        verdict = self.guard.recheck(self.conn, row["action_type"], row["target"], content,
                                     float(row["cost_estimate_usd"] or 0), row["id"])
        if verdict.blocked:
            verdict = verdict.with_flags(stored_flags(row["guard_result"]))
            self.conn.execute("UPDATE actions SET guard_result = ? WHERE id = ?", (verdict.to_json(), row["id"]))
            log_event(self.conn, "guard", "guard_blocked", {"at": "execution", "blocks": list(verdict.blocks)},
                      row["id"])
            self._refuse(row["id"], "the Guard blocked it at execution: " + "; ".join(verdict.blocks))

    def _claim(self, action_id: int) -> None:
        with transaction(self.conn):
            cur = self.conn.execute(
                "UPDATE actions SET status = 'executed', executed_at = ? WHERE id = ? AND status = 'approved'",
                (utc_now(), action_id),
            )
            if cur.rowcount != 1:
                # Someone else claimed it between our read and this update.
                raise ExecutorError(f"action #{action_id} was already claimed")
            log_event(self.conn, EXECUTOR, "execution_started", {}, action_id)

    def _finish(self, action_id: int, status: str, dry_run: bool, result: str) -> Outcome:
        with transaction(self.conn):
            self.conn.execute("UPDATE actions SET status = ?, result = ? WHERE id = ?",
                              (status, result, action_id))
            log_event(self.conn, EXECUTOR, f"execution_{'finished' if status == 'executed' else 'failed'}",
                      {"dry_run": dry_run, "result": result}, action_id)
        return Outcome(action_id, status, dry_run, result)

    def _refuse(self, action_id: int | None, why: str) -> None:
        log_event(self.conn, EXECUTOR, "execution_refused", {"why": why}, action_id)
        raise ExecutorError(why)
