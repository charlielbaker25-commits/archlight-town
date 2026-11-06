"""The guard hook: plain-code rules every proposal must pass, supplied by the pack (business-neutral here).

The engine doesn't know what the rules are (the website pack's Guard has the do-not-contact list, the
daily send cap and so on). It only knows where they run:
- the Mayor runs the full check on every proposal (the hard rules, plus any soft flags) and saves the
  result with it, so Charlie sees it in `town pending`,
- `approve` and `edit` run the hard rules again and refuse a blocked action,
- the executor runs the hard rules once more right before it carries out an approved action.

A soft check (a model's opinion) can only ADD flags. Only hard rules block, and nothing can clear a block
except the rule itself passing again (say, the daily cap resetting tomorrow).
"""
import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Protocol

from engine.db import utc_now


@dataclass(frozen=True)
class GuardVerdict:
    blocks: tuple[str, ...] = ()          # hard-rule failures, in plain words. Any one blocks the action.
    flags: tuple[str, ...] = ()           # softer concerns for Charlie to weigh. They never block.
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def blocked(self) -> bool:
        return bool(self.blocks)

    @property
    def verdict(self) -> str:
        return "blocked" if self.blocks else "flagged" if self.flags else "clear"

    def to_json(self) -> str:
        return json.dumps({"verdict": self.verdict, "blocks": list(self.blocks), "flags": list(self.flags),
                           "checked_at": utc_now(), **self.details}, sort_keys=True)

    def with_flags(self, flags: list[str] | tuple[str, ...]) -> "GuardVerdict":
        """The same hard-rule result, keeping earlier soft flags (a re-check runs only the hard rules)."""
        return GuardVerdict(self.blocks, tuple(dict.fromkeys([*self.flags, *flags])), self.details)

    def summary(self) -> str:
        if self.blocks:
            return "BLOCKED: " + "; ".join(self.blocks)
        if self.flags:
            return "clear, with flags: " + "; ".join(self.flags)
        return "clear"


def stored_flags(guard_result: str | None) -> list[str]:
    """The soft flags saved with an action when it was proposed."""
    try:
        return list(json.loads(guard_result or "{}").get("flags") or [])
    except (json.JSONDecodeError, AttributeError):
        return []


class Guard(Protocol):
    def check(self, conn: sqlite3.Connection, action_type: str, target: str, content: dict[str, Any],
              cost_estimate_usd: float = 0.0) -> GuardVerdict:
        """Every rule, at proposal time: the hard rules plus any soft flags. Must never raise: a rule that
        can't run blocks (fail closed)."""
        ...

    def recheck(self, conn: sqlite3.Connection, action_type: str, target: str, content: dict[str, Any],
                cost_estimate_usd: float = 0.0, action_id: int | None = None) -> GuardVerdict:
        """The hard rules only (no model call), at approval and execution time. Must never raise."""
        ...
