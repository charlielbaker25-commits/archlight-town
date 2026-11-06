import json
import sqlite3

import pytest

from engine.db import utc_now
from engine.events import log_event, recent_events


def _columns(conn, table):
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def test_actions_table_has_every_plan_column(conn):
    expected = {
        "id", "agent", "action_type", "target", "content", "reason", "cost_estimate_usd",
        "guard_result", "status", "reject_reason", "original_content", "edited_content",
        "created_at", "decided_at", "executed_at",
    }
    assert expected <= _columns(conn, "actions")


def test_events_costs_and_leads_tables_exist(conn):
    assert {"ts", "agent", "event_type", "details"} <= _columns(conn, "events")
    assert {"ts", "agent", "model", "service", "units", "dollar_cost"} <= _columns(conn, "costs")
    assert {
        "business_name", "business_type", "area", "address", "phone", "website", "public_email",
        "contact_route", "source", "source_id", "source_release", "confidence", "status",
    } <= _columns(conn, "leads")


def test_database_refuses_a_rejection_without_a_reason(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO actions (agent, action_type, target, content, reason, status, created_at)"
            " VALUES ('scout', 'add_lead', 'x', '{}', 'r', 'rejected', ?)",
            (utc_now(),),
        )


def test_leads_only_accept_overture_and_known_contact_routes(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO leads (business_name, business_type, area, contact_route, source,"
            " source_id, source_release, created_at)"
            " VALUES ('A', 'salon', 'STL', 'email', 'google_places', 'g1', 'r', ?)",
            (utc_now(),),
        )


def test_log_event_stores_details_as_json(conn):
    log_event(conn, "mayor", "test_event", {"n": 1})
    row = recent_events(conn)[0]
    assert row["event_type"] == "test_event"
    assert json.loads(row["details"]) == {"n": 1}
    assert row["ts"].endswith("Z")
