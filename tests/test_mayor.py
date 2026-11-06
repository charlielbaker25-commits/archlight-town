import json

import pytest

from engine.mayor import Mayor, MayorError


def _event_count(conn):
    return conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]


def _propose(mayor, **overrides):
    args = dict(agent="scout", action_type="add_lead", target="Hair Studio X",
                content={"business_name": "Hair Studio X"}, reason="No website")
    args.update(overrides)
    return mayor.propose(**args)


def test_proposal_waits_in_the_queue(conn):
    mayor = Mayor(conn, town_stopped=lambda: False)
    action_id = _propose(mayor)
    pending = mayor.list_pending()
    assert [row["id"] for row in pending] == [action_id]
    assert pending[0]["status"] == "proposed"
    assert pending[0]["guard_result"] is None


def test_rejecting_without_a_reason_fails(conn):
    mayor = Mayor(conn, town_stopped=lambda: False)
    action_id = _propose(mayor)
    for empty in ("", "   ", None):
        with pytest.raises(MayorError):
            mayor.reject(action_id, empty)
    assert mayor.get(action_id)["status"] == "proposed"


def test_reject_saves_the_reason(conn):
    mayor = Mayor(conn, town_stopped=lambda: False)
    action_id = _propose(mayor)
    mayor.reject(action_id, "Chain business, not a fit")
    row = mayor.get(action_id)
    assert row["status"] == "rejected"
    assert row["reject_reason"] == "Chain business, not a fit"
    assert row["decided_at"]


def test_editing_saves_both_the_original_and_the_edited_content(conn):
    mayor = Mayor(conn, town_stopped=lambda: False)
    action_id = _propose(mayor, content={"business_name": "Hair Studio X", "phone": "555"})
    mayor.edit_and_approve(action_id, {"business_name": "Hair Studio X", "phone": "314-555-0100"})
    row = mayor.get(action_id)
    assert row["status"] == "approved"
    assert json.loads(row["original_content"])["phone"] == "555"
    assert json.loads(row["edited_content"])["phone"] == "314-555-0100"
    assert json.loads(row["content"])["phone"] == "314-555-0100"


def test_decisions_only_move_forward(conn):
    mayor = Mayor(conn, town_stopped=lambda: False)
    rejected = _propose(mayor)
    mayor.reject(rejected, "no")
    with pytest.raises(MayorError):
        mayor.approve(rejected)
    with pytest.raises(MayorError):
        mayor.edit_and_approve(rejected, {"x": 1})
    approved = _propose(mayor)
    mayor.approve(approved)
    with pytest.raises(MayorError):
        mayor.reject(approved, "changed my mind")
    with pytest.raises(MayorError):
        mayor.approve(999)


def test_every_mayor_call_writes_an_event(conn):
    mayor = Mayor(conn, town_stopped=lambda: False)
    calls = [
        lambda: _propose(mayor),
        lambda: mayor.list_pending(),
        lambda: mayor.approve(1),
        lambda: _propose(mayor),
        lambda: mayor.reject(2, "too far away"),
        lambda: _propose(mayor),
        lambda: mayor.edit_and_approve(3, {"business_name": "Edited"}),
        # refused calls are logged too
        lambda: mayor.reject(1, ""),
        lambda: mayor.approve(42),
        lambda: _propose(mayor, reason=""),
    ]
    for call in calls:
        before = _event_count(conn)
        try:
            call()
        except MayorError:
            pass
        assert _event_count(conn) == before + 1
