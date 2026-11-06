"""Claude Code never makes Charlie's decisions on the real Town (CLAUDE.md rule, enforced here).

Every `town` command run from Claude Code is logged as `claude-code`. On the real
instance it may only read; every other command is refused before anything changes.
"""
import json

import pytest
from typer.testing import CliRunner

import launcher
from engine.actor import CHARLIE, CLAUDE_CODE, current_actor
from engine.settings import load_settings
from engine.town import open_town
from packs.websites import pack

runner = CliRunner()


def town(*args):
    return runner.invoke(launcher.app, list(args))


def _open(instance_file):
    return open_town(load_settings(instance_file), pack.PACK_SCHEMAS, {}, pack.LIMITS_FILE, pack.MODELS_FILE)


def _seed(instance_file):
    """A running Town with one proposal waiting."""
    t = _open(instance_file)
    t.killswitch.resume(t.conn)
    action_id = t.mayor.propose("scout", "test_action", "Business 0", {"business_name": "Business 0"}, "No website")
    t.close()
    return action_id


def _events(instance_file, event_type):
    t = _open(instance_file)
    rows = [dict(r) for r in t.conn.execute("SELECT * FROM events WHERE event_type = ? ORDER BY id", (event_type,))]
    t.close()
    return rows


@pytest.fixture
def claude_code(monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")


# ---------------- working out the actor ----------------

def test_claudecode_variable_means_claude_code():
    assert current_actor({"CLAUDECODE": "1"}) == CLAUDE_CODE
    assert current_actor({"CLAUDECODE": ""}) == CLAUDE_CODE  # present at all counts


def test_claude_code_entrypoint_alone_also_means_claude_code():
    assert current_actor({"CLAUDE_CODE_ENTRYPOINT": "cli"}) == CLAUDE_CODE


def test_no_markers_means_charlie():
    assert current_actor({"PATH": "C:\\Windows"}) == CHARLIE


def test_unreadable_environment_fails_closed_to_claude_code():
    class Broken(dict):
        def __contains__(self, key):
            raise OSError("environment unavailable")

    assert current_actor(Broken()) == CLAUDE_CODE


# ---------------- the real instance: decisions refused ----------------

DECISION_COMMANDS = [
    ["stop"],
    ["resume"],
    ["approve", "{id}"],
    ["reject", "{id}", "--reason", "Chain store"],
    ["edit", "{id}", "--set", "phone=314-555-0100"],
    ["run"],
    ["limit", "leads.new_leads_per_day", "10", "--reason", "test"],
    ["scout", "--type", "salon", "--area", "Clayton"],
    ["api-check"],
    ["backup"],
    ["overture-download"],
]


@pytest.mark.parametrize("args", DECISION_COMMANDS, ids=lambda a: a[0])
def test_each_decision_command_refuses_claude_code_on_the_real_instance(instance_file, claude_code, args):
    action_id = _seed(instance_file)
    result = town(*[a.format(id=action_id) for a in args])

    assert result.exit_code == 1
    message = " ".join(result.stdout.split())  # the terminal wraps long lines
    assert "Negative" in message and "--instance dev" in message
    # Nothing changed: the proposal still waits, the Town still runs, no new proposals, no backup.
    t = _open(instance_file)
    assert t.mayor.get(action_id)["status"] == "proposed"
    assert not t.killswitch.is_stopped()
    assert t.conn.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 1
    t.close()
    assert not (instance_file.parent / "backups").exists()
    [refused] = _events(instance_file, "command_refused")
    assert refused["agent"] == CLAUDE_CODE
    assert json.loads(refused["details"])["command"] == args[0]


def test_claude_code_cannot_resume_a_stopped_real_town(instance_file, monkeypatch):
    _seed(instance_file)
    assert town("stop").exit_code == 0  # Charlie stops it
    monkeypatch.setenv("CLAUDECODE", "1")
    assert town("resume").exit_code == 1
    t = _open(instance_file)
    assert t.killswitch.is_stopped()
    t.close()


@pytest.mark.parametrize("command", ["pending", "log", "costs", "status"])
def test_read_only_commands_still_work_for_claude_code_on_the_real_instance(instance_file, claude_code, command):
    _seed(instance_file)
    result = town(command)
    assert result.exit_code == 0, result.stdout
    [logged] = _events(instance_file, "claude_code_command")
    assert logged["agent"] == CLAUDE_CODE
    assert json.loads(logged["details"]) == {"command": command, "instance": "real"}
    # Read-only means no automatic backup either.
    assert not (instance_file.parent / "backups").exists()


# ---------------- the actor in the log ----------------

def test_charlie_is_logged_as_charlie(instance_file):
    action_id = _seed(instance_file)
    assert town("reject", str(action_id), "--reason", "Chain store").exit_code == 0
    [rejected] = _events(instance_file, "action_rejected")
    assert json.loads(rejected["details"])["decided_by"] == CHARLIE
    assert _events(instance_file, "claude_code_command") == []


def test_claude_code_is_logged_as_claude_code_on_the_dev_instance(dev_instance, claude_code):
    action_id = _seed(dev_instance)
    assert town("--instance", "dev", "reject", str(action_id), "--reason", "Testing").exit_code == 0
    assert town("--instance", "dev", "stop").exit_code == 0

    [rejected] = _events(dev_instance, "action_rejected")
    assert json.loads(rejected["details"])["decided_by"] == CLAUDE_CODE
    [stopped] = _events(dev_instance, "town_stopped")
    assert stopped["agent"] == CLAUDE_CODE
    state = json.loads(load_settings(dev_instance).killswitch_file.read_text(encoding="utf-8"))
    assert state["by"] == CLAUDE_CODE
    assert [e["agent"] for e in _events(dev_instance, "claude_code_command")] == [CLAUDE_CODE, CLAUDE_CODE]
