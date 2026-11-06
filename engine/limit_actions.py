"""Executor handlers for limit changes. They only run after Charlie approves them.

- change_limit:     change any value in limits.yaml (a money cap change also resumes paid work)
- raise_cap:        the Treasurer's cap-raise request: change a money cap, then resume paid work
- resume_paid_work: resume paid calls after Charlie fixed the Console (spend limit or credits)

All three are internal actions: they change the Town's own settings and nothing outside it.
If the new cap is still below this month's spend, the next paid call simply pauses again
and files a new request.
"""
import math
import sqlite3
from typing import Any

from engine.budget import Budget
from engine.events import log_event
from engine.executor import Handler
from engine.limits import Limits


def _valid_new_value(key: str, old: Any, new: Any) -> None:
    if isinstance(old, (int, float)) and not isinstance(old, bool):
        # isfinite blocks NaN and Infinity, which would silently switch a cap check off.
        if isinstance(new, bool) or not isinstance(new, (int, float)) or not math.isfinite(new) or new < 0:
            raise ValueError(f"new_value must be a number of 0 or more, got {new!r}")
        if key.startswith("money.") and new <= 0:
            raise ValueError(f"A money limit must be more than 0, got {new!r}")
    elif type(new) is not type(old):
        raise ValueError(f"new_value must be the same kind of value as {old!r}, got {new!r}")


def limit_handlers(limits: Limits, budget: Budget) -> dict[str, Handler]:
    def change_limit(conn: sqlite3.Connection, action_id: int, content: dict[str, Any]) -> str:
        key, new = content["limit"], content["new_value"]
        _valid_new_value(key, limits.get(key), new)
        old = limits.set(key, new)
        log_event(conn, "mayor", "limit_changed", {"limit": key, "old": old, "new": new}, action_id)
        result = f"{key} changed from {old} to {new} in limits.yaml"
        # Only a cap pause ends with a money change. A Console pause needs its own approval.
        if key.startswith("money.") and budget.paused_kind() == "cap":
            budget.resume(f"money limit changed by approved action #{action_id}")
            result += ". Paid work resumed."
        return result

    def raise_cap(conn: sqlite3.Connection, action_id: int, content: dict[str, Any]) -> str:
        if not str(content.get("limit", "")).startswith("money."):
            raise ValueError("raise_cap only changes money caps")
        return change_limit(conn, action_id, content)

    def resume_paid_work(conn: sqlite3.Connection, action_id: int, content: dict[str, Any]) -> str:
        budget.resume(f"Charlie approved action #{action_id}")
        return "Paid work resumed."

    def describe(content: dict[str, Any]) -> str:
        return f"set {content.get('limit')} to {content.get('new_value')}"

    return {
        "change_limit": Handler("change_limit", False, change_limit, describe),
        "raise_cap": Handler("raise_cap", False, raise_cap, describe),
        "resume_paid_work": Handler("resume_paid_work", False, resume_paid_work, lambda c: "resume paid work"),
    }
