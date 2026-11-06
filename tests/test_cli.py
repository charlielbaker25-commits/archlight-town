"""The `town` commands, run the way Charlie runs them (against a throwaway instance)."""
import json

from typer.testing import CliRunner

import launcher
from engine.executor import Handler
from engine.settings import load_settings
from engine.town import open_town
from packs.websites import pack

runner = CliRunner()


def town(*args):
    return runner.invoke(launcher.app, list(args))


def _open(instance_file):
    return open_town(load_settings(instance_file), pack.PACK_SCHEMAS, {}, pack.LIMITS_FILE, pack.MODELS_FILE)


def _seed(instance_file, n=1):
    t = _open(instance_file)
    t.killswitch.resume(t.conn)
    ids = [t.mayor.propose("scout", "test_action", f"Business {i}", {"business_name": f"Business {i}",
                                                                      "phone": "555"}, "No website")
           for i in range(n)]
    t.close()
    return ids


def _status(instance_file, action_id):
    t = _open(instance_file)
    row = t.mayor.get(action_id)
    t.close()
    return row


def test_pending_shows_the_queue(instance_file):
    _seed(instance_file, 2)
    result = town("pending")
    assert result.exit_code == 0
    assert "2 proposal(s)" in result.stdout and "Business 1" in result.stdout


def test_reject_needs_a_reason(instance_file):
    [action_id] = _seed(instance_file)
    assert town("reject", str(action_id)).exit_code == 1
    result = town("reject", str(action_id), "--reason", "Chain store")
    assert result.exit_code == 0
    assert _status(instance_file, action_id)["reject_reason"] == "Chain store"


def test_edit_with_set_saves_both_versions(instance_file, monkeypatch):
    ran = []
    monkeypatch.setattr(pack, "handlers", lambda outbox=None: {
        "test_action": Handler("test_action", False, lambda c, i, content: ran.append(content) or "saved", str)})
    [action_id] = _seed(instance_file)
    result = town("edit", str(action_id), "--set", "phone=314-555-0100")
    assert result.exit_code == 0, result.stdout
    row = _status(instance_file, action_id)
    assert json.loads(row["original_content"])["phone"] == "555"
    assert json.loads(row["edited_content"])["phone"] == "314-555-0100"
    assert row["status"] == "executed"
    assert ran == [{"business_name": "Business 0", "phone": "314-555-0100"}]


def test_stop_holds_approvals_and_resume_plus_run_releases_them(instance_file, monkeypatch):
    ran = []
    monkeypatch.setattr(pack, "handlers", lambda outbox=None: {
        "test_action": Handler("test_action", False, lambda c, i, content: ran.append(i) or "saved", str)})
    [action_id] = _seed(instance_file)

    assert "Standing down" in town("stop").stdout
    result = town("approve", str(action_id))
    assert result.exit_code == 0 and "held" in result.stdout
    assert town("run").exit_code == 1
    assert ran == []
    assert "STOPPED" in town("status").stdout

    assert "held during the stop" in town("resume").stdout
    assert town("run").exit_code == 0
    assert ran == [action_id]


def test_log_times_show_in_chicago_time_across_daylight_saving():
    assert launcher.chicago_time("2026-10-19T15:44:50Z") == "2026-10-19 10:44:50 AM CDT"
    assert launcher.chicago_time("2026-12-01T18:00:00Z") == "2026-12-01 12:00:00 PM CST"
    assert launcher.chicago_time("2026-10-20T04:30:00Z") == "2026-10-19 11:30:00 PM CDT"  # earlier local date


def test_log_shows_chicago_time_but_the_database_keeps_utc(instance_file):
    [action_id] = _seed(instance_file)
    t = _open(instance_file)
    t.conn.execute("UPDATE events SET ts = '2026-10-19T15:44:50Z' WHERE action_id = ?", (action_id,))
    t.close()
    assert "2026-10-19 10:44:50 AM CDT" in town("log").stdout
    t = _open(instance_file)
    stored = t.conn.execute("SELECT ts FROM events WHERE action_id = ?", (action_id,)).fetchone()[0]
    t.close()
    assert stored == "2026-10-19T15:44:50Z"


def test_log_shows_decisions_and_feedback(instance_file):
    [action_id] = _seed(instance_file)
    town("reject", str(action_id), "--reason", "Too far away")
    result = town("log", "--full")
    assert "action_rejected" in result.stdout and "Too far away" in result.stdout
