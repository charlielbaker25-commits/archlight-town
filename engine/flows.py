"""Message flows: the messages the village map shows travelling between buildings (BLUEPRINT 9.2).

A flow is one message from one place to another, read from ONE real event in the log: "Scout -> Mayor:
proposed lead #12". Nothing here invents traffic. The engine only knows the shape (from, to, kind, label and
the ids); what each event means is the pack's call (its TownView), and an event the pack doesn't recognise
maps to no flow at all, never a guess.

Agents never talk to each other directly (BLUEPRINT 3.3), so a pack's flows go through the Town Hall (the
Mayor). The one exception is Charlie <-> Jarvis at the Orb Tower.
"""
import json
import sqlite3
from dataclasses import asdict, dataclass
from typing import Any, Protocol

# The person, not an agent: decisions come from Charlie and approvals wait for him.
CHARLIE_NODE = "charlie"


@dataclass(frozen=True)
class Flow:
    event_id: int
    ts: str
    from_node: str      # an agent's name, or "charlie"
    to_node: str
    kind: str           # e.g. "proposal", "decision", "work", "result", "block", "alert"
    label: str          # short, plain words: "proposed lead #12"
    action_id: int | None = None
    lead_id: int | None = None
    build_id: int | None = None
    says: str = ""      # the speech bubble over the sender, in plain words: "the site for Oak Lane Florist is ready"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class FlowMapper(Protocol):
    def flows(self, conn: sqlite3.Connection, event: dict[str, Any]) -> list[Flow]:
        """The flows one event stands for (often one, sometimes two, usually none)."""
        ...


def event_dict(row: sqlite3.Row) -> dict[str, Any]:
    """An events row as a plain dict, with its details parsed (garbled JSON becomes {})."""
    try:
        details = json.loads(row["details"] or "{}")
    except (TypeError, ValueError):
        details = {}
    return {"id": row["id"], "ts": row["ts"], "agent": row["agent"], "event_type": row["event_type"],
            "action_id": row["action_id"], "details": details if isinstance(details, dict) else {}}


def flows_since(conn: sqlite3.Connection, mapper: FlowMapper, after_id: int = 0, since_ts: str | None = None,
                limit: int = 500) -> list[Flow]:
    """Every flow from events after event id `after_id` (and at or after `since_ts`, if given), oldest first.
    The live stream calls this with the last id it sent, so each flow goes out once."""
    query = "SELECT * FROM events WHERE id > ?"
    params: list[Any] = [after_id]
    if since_ts:
        query += " AND ts >= ?"
        params.append(since_ts)
    query += " ORDER BY id LIMIT ?"
    params.append(limit)
    flows: list[Flow] = []
    for row in conn.execute(query, params).fetchall():
        flows.extend(mapper.flows(conn, event_dict(row)))
    return flows


def flows_after(conn: sqlite3.Connection, mapper: FlowMapper, after_id: int, since_ts: str | None = None,
                limit: int = 500, up_to: int | None = None) -> tuple[list[Flow], int]:
    """The flows from events after `after_id`, plus the id to carry on from next time.

    The newest id is read FIRST and only events up to it are read. An event that lands meanwhile (an order's
    second event, a write from another agent) has a higher id, so the next read picks it up. Reading the flows
    first and the newest id second (as HQ's stream once did) could step past an event that landed in between,
    and its flows were never sent. With `limit` events read, the id returned is the last one read, so nothing past
    the page is skipped either. `up_to` is a newest id the caller already read (HQ's page load reads the recent
    past up to it, and the live stream carries on from that id, not from the end of the page)."""
    newest = last_event_id(conn) if up_to is None else up_to
    query = "SELECT * FROM events WHERE id > ? AND id <= ?"
    params: list[Any] = [after_id, newest]
    if since_ts:
        query += " AND ts >= ?"
        params.append(since_ts)
    query += " ORDER BY id LIMIT ?"
    params.append(limit)
    rows = conn.execute(query, params).fetchall()
    flows: list[Flow] = []
    for row in rows:
        flows.extend(mapper.flows(conn, event_dict(row)))
    carry_on = int(rows[-1]["id"]) if len(rows) == limit else max(newest, after_id)
    return flows, carry_on


def last_event_id(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0])
