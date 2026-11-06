"""SQLite storage: one database file per business instance.

The engine creates the three business-neutral tables (actions, events, costs).
A pack adds its own tables (the website pack adds `leads`) by passing its
schema to init_db, so the engine never needs to know what a "lead" is.
"""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ACTION_STATUSES = ("proposed", "approved", "rejected", "executed", "failed")

ENGINE_SCHEMA = """
CREATE TABLE IF NOT EXISTS actions (
    id                INTEGER PRIMARY KEY,
    agent             TEXT NOT NULL,
    action_type       TEXT NOT NULL,
    target            TEXT NOT NULL,          -- who or what it's for, in plain words
    content           TEXT NOT NULL,          -- the exact content (JSON), as it will run
    reason            TEXT NOT NULL,          -- why the agent proposes it
    cost_estimate_usd REAL NOT NULL DEFAULT 0,
    guard_result      TEXT,                   -- blank until the Guard arrives in Phase 2
    status            TEXT NOT NULL DEFAULT 'proposed'
                      CHECK (status IN ('proposed', 'approved', 'rejected', 'executed', 'failed')),
    reject_reason     TEXT,
    original_content  TEXT,                   -- set only when Charlie edits
    edited_content    TEXT,                   -- set only when Charlie edits
    result            TEXT,                   -- what happened when it ran (or the dry-run output)
    created_at        TEXT NOT NULL,
    decided_at        TEXT,
    executed_at       TEXT,
    -- A second lock behind the Mayor's own check: the database itself refuses
    -- to store a rejection without a reason.
    CHECK (status != 'rejected' OR length(trim(coalesce(reject_reason, ''))) > 0)
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY,
    ts         TEXT NOT NULL,
    agent      TEXT NOT NULL,
    event_type TEXT NOT NULL,
    action_id  INTEGER REFERENCES actions(id),
    details    TEXT NOT NULL DEFAULT '{}'      -- JSON, with enough detail for replay mode later
);

CREATE TABLE IF NOT EXISTS costs (
    id          INTEGER PRIMARY KEY,
    ts          TEXT NOT NULL,
    agent       TEXT NOT NULL,
    model       TEXT NOT NULL,
    service     TEXT NOT NULL,                 -- 'claude_api' for now
    units       TEXT NOT NULL DEFAULT '{}',    -- JSON, e.g. {"input_tokens": 812, "output_tokens": 140}
    dollar_cost REAL NOT NULL
);

-- How Charlie likes to be briefed (engine/profile.py). Only an approved profile_change writes it.
CREATE TABLE IF NOT EXISTS operator_profile (
    key        TEXT PRIMARY KEY,           -- length, lead_with or skip
    value      TEXT NOT NULL,              -- JSON
    action_id  INTEGER,                    -- the approved profile_change that set it
    set_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_actions_status ON actions(status);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_costs_ts ON costs(ts);
"""


def utc_now() -> str:
    """The current time in UTC, e.g. '2026-10-19T03:12:45Z'. Every timestamp uses this format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path: Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None means Python won't open hidden transactions;
    # code that needs several writes to land together uses `with transaction(conn)`.
    conn = sqlite3.connect(path, isolation_level=None, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL lets readers (town log, the future dashboard) read while an agent writes.
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection, pack_schemas: tuple[str, ...] = ()) -> None:
    conn.executescript(ENGINE_SCHEMA)
    for schema in pack_schemas:
        conn.executescript(schema)


class transaction:
    """`with transaction(conn):` groups writes so they all land, or none do.

    BEGIN IMMEDIATE takes the write lock up front, so two processes can't both
    read "approved" and both run the same action.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def __enter__(self) -> sqlite3.Connection:
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> None:
        self.conn.execute("ROLLBACK" if exc_type else "COMMIT")
