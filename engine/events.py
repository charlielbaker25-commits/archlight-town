"""The event log: every proposal, decision, execution, cost and stop writes here.

It is the Town's record (memory layer 1). Replay mode, the Librarian, the
Treasurer and Jarvis will all read it, so details are stored as full JSON.
"""
import json
import sqlite3
from typing import Any

from engine.db import utc_now


def log_event(
    conn: sqlite3.Connection,
    agent: str,
    event_type: str,
    details: dict[str, Any] | None = None,
    action_id: int | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO events (ts, agent, event_type, action_id, details) VALUES (?, ?, ?, ?, ?)",
        (utc_now(), agent, event_type, action_id, json.dumps(details or {}, sort_keys=True, default=str)),
    )
    return int(cur.lastrowid)


def recent_events(conn: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
