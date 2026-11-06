"""The operator profile (BLUEPRINT 5.2): how Charlie likes to be briefed. Business-neutral.

It holds three settings, and only these:
- length:    "short", "normal" (the default) or "detailed",
- lead_with: what a briefing opens with: "results" (the default: what happened), "waiting" (the queue) or
             "problems" (failures and blocks),
- skip:      sections Charlie said to leave out: any of "costs", "plan".

It is built ONLY from Charlie's explicit feedback: words he says to Reaper ("shorter", "skip the costs", "lead
with what's waiting"), or a thumbs-down in Reaper HQ with one of those same choices. A thumbs up or down on its
own is recorded (the Librarian can count it) but changes nothing, because it doesn't say WHAT to change.
Every change is a `profile_change` proposal in the queue; only Charlie's approval writes it (the handler below).
Reaper's chat and the morning sit-rep read it on every run.
"""
import json
import re
import sqlite3
from typing import Any

from engine.db import utc_now
from engine.events import log_event
from engine.executor import Handler

AGENT = "jarvis"
PROFILE_CHANGE = "profile_change"
DEFAULTS: dict[str, Any] = {"length": "normal", "lead_with": "results", "skip": []}
CHOICES = {"length": ("short", "normal", "detailed"), "lead_with": ("results", "waiting", "problems"),
           "skip": ("costs", "plan")}

# The table (operator_profile) is in engine/db.py's schema.

# Charlie's explicit words -> one change. Matched on the whole message (short feedback, not a sentence that
# merely mentions costs): "shorter", "too long", "skip the costs", "lead with what's waiting".
_FEEDBACK = [
    (re.compile(r"^(?:(?:please |reaper,? )?(?:be )?(?:shorter|briefer|more brief|keep it (?:short|brief)|"
                r"too long|less detail|tl;?dr)[.!]?)$", re.IGNORECASE), ("length", "short")),
    (re.compile(r"^(?:(?:please |reaper,? )?(?:longer|more detail|more detailed|too short|give me more)[.!]?)$",
                re.IGNORECASE), ("length", "detailed")),
    (re.compile(r"^(?:(?:please |reaper,? )?(?:skip|drop|leave out|no more|don'?t (?:tell me|mention|bother me with))"
                r" (?:about )?(?:the )?(?:costs?|spend(?:ing)?|money)(?: part| line| section)?[.!]?)$",
                re.IGNORECASE), ("skip", "costs")),
    (re.compile(r"^(?:(?:please |reaper,? )?(?:skip|drop|leave out) (?:the |today'?s )?plan[.!]?)$", re.IGNORECASE),
     ("skip", "plan")),
    (re.compile(r"^(?:(?:please |reaper,? )?(?:lead|start|open) with (?:what'?s waiting|the queue|my queue|"
                r"what needs me|decisions)[.!]?)$", re.IGNORECASE), ("lead_with", "waiting")),
    (re.compile(r"^(?:(?:please |reaper,? )?(?:lead|start|open) with (?:problems|what'?s broken|failures|"
                r"blocks|bad news)[.!]?)$", re.IGNORECASE), ("lead_with", "problems")),
    (re.compile(r"^(?:(?:please |reaper,? )?(?:lead|start|open) with (?:results|the numbers|what happened)[.!]?)$",
                re.IGNORECASE), ("lead_with", "results")),
]
# The choices HQ's thumbs-down offers (the same changes, as buttons).
HQ_CHANGES = {"shorter": ("length", "short"), "more_detail": ("length", "detailed"), "skip_costs": ("skip", "costs"),
              "lead_waiting": ("lead_with", "waiting")}


def load(conn: sqlite3.Connection) -> dict[str, Any]:
    """The current profile: the defaults, with every approved change on top."""
    profile = {k: (list(v) if isinstance(v, list) else v) for k, v in DEFAULTS.items()}
    try:
        for row in conn.execute("SELECT key, value FROM operator_profile"):
            if row["key"] in DEFAULTS:
                profile[row["key"]] = json.loads(row["value"])
    except sqlite3.OperationalError:   # an old database before the table existed
        pass
    return profile


def feedback_change(said: str) -> tuple[str, str] | None:
    """The change Charlie's words ask for, or None. Plain code, no model."""
    text = " ".join((said or "").split())
    for pattern, change in _FEEDBACK:
        if pattern.match(text):
            return change
    return None


def new_value(profile: dict[str, Any], key: str, value: str) -> Any:
    if key == "skip":
        return sorted(set(profile["skip"]) | {value})
    return value


def describe(profile: dict[str, Any]) -> str:
    """The profile as one plain sentence, for Reaper's instructions and the sit-rep."""
    parts = {"short": "Keep briefings short: the key numbers and what needs him, nothing else.",
             "normal": "", "detailed": "He likes detail: include the numbers behind each point."}[profile["length"]]
    lead = {"results": "", "waiting": "Lead with what's waiting for his decision.",
            "problems": "Lead with problems: failures and anything blocked."}[profile["lead_with"]]
    skip = f"Leave out: {', '.join(profile['skip'])}." if profile["skip"] else ""
    return " ".join(p for p in (parts, lead, skip) if p) or "No preferences set yet."


def propose_change(town: Any, key: str, value: str, source: dict[str, Any]) -> tuple[int | None, str]:
    """Files a profile_change proposal for Charlie (never applies it). Returns (action id or None, message)."""
    if key not in CHOICES or value not in CHOICES[key]:
        raise ValueError(f"unknown profile change {key}={value}")
    current = load(town.conn)
    wanted = new_value(current, key, value)
    if current[key] == wanted:
        return None, f"Already set: {describe(current)}"
    for row in town.conn.execute("SELECT id, content FROM actions WHERE action_type = ? AND status = 'proposed'",
                                 (PROFILE_CHANGE,)):
        content = json.loads(row["content"])
        if content.get("key") == key and content.get("value") == wanted:
            return row["id"], f"Already waiting as #{row['id']}."
    content = {"key": key, "value": wanted, "old": current[key], "evidence": source}
    action_id = town.mayor.propose(AGENT, PROFILE_CHANGE, f"briefing {key}: {json.dumps(wanted)}", content,
                                   f"Charlie's own feedback ({source.get('source')}): \"{source.get('said', '')}\"")
    return action_id, (f"Noted. Proposed #{action_id}: briefing {key.replace('_', ' ')} -> "
                       f"{', '.join(wanted) if isinstance(wanted, list) else wanted}. It changes nothing until you "
                       f"approve it (\"approve {action_id}\").")


def record_rating(town: Any, about: str, rating: int, excerpt: str, change: str | None = None) -> dict[str, Any]:
    """A thumbs up or down from HQ (or `town sitrep --rate`). Logged as operator_feedback. With a change chosen
    (shorter, skip costs...), that explicit choice becomes a profile_change proposal."""
    if rating not in (1, -1):
        raise ValueError("rating must be 1 or -1")
    event_id = log_event(town.conn, AGENT, "operator_feedback", {"about": about[:40], "rating": rating,
                                                                  "change": change, "excerpt": excerpt[:300]})
    out: dict[str, Any] = {"recorded": True, "event_id": event_id, "action_id": None, "message": "Noted."}
    if change:
        if change not in HQ_CHANGES:
            raise ValueError(f"unknown change {change}")
        key, value = HQ_CHANGES[change]
        out["action_id"], out["message"] = propose_change(
            town, key, value, {"source": f"thumbs {'up' if rating > 0 else 'down'} on {about}", "said": change,
                               "event_id": event_id})
    return out


def profile_handlers() -> dict[str, Handler]:
    def apply(conn: sqlite3.Connection, action_id: int, content: dict[str, Any]) -> str:
        key, value = content["key"], content["value"]
        if key not in CHOICES:
            raise ValueError(f"unknown profile key {key}")
        values = value if isinstance(value, list) else [value]
        if not all(v in CHOICES[key] for v in values):
            raise ValueError(f"{value!r} isn't a choice for {key}")
        conn.execute("INSERT INTO operator_profile (key, value, action_id, set_at) VALUES (?, ?, ?, ?)"
                     " ON CONFLICT(key) DO UPDATE SET value = excluded.value, action_id = excluded.action_id,"
                     " set_at = excluded.set_at", (key, json.dumps(value), action_id, utc_now()))
        log_event(conn, AGENT, "profile_changed", {"key": key, "value": value, "old": content.get("old")}, action_id)
        return f"Briefing preference saved: {key} = {value}"

    return {PROFILE_CHANGE: Handler(PROFILE_CHANGE, False, apply,
                                    lambda c: f"set the briefing {c.get('key')} to {c.get('value')}")}
