"""The dev instance: Claude Code's test copy. It never shares storage with the real Town,
the claude-code check applies only to the real one, and both count toward the same caps."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

import launcher
from engine.actor import CLAUDE_CODE
from engine.settings import load_settings
from engine.town import open_town
from packs.websites import pack

ROOT = Path(__file__).resolve().parent.parent
runner = CliRunner()


def town(*args):
    return runner.invoke(launcher.app, list(args))


def _open(instance_file):
    return open_town(load_settings(instance_file), pack.PACK_SCHEMAS, {}, pack.LIMITS_FILE, pack.MODELS_FILE)


# ---------------- the real files in the repo ----------------

def test_the_two_instances_never_share_storage(public_repo_settings):
    real = load_settings(public_repo_settings / "instance.yaml")
    dev = load_settings(public_repo_settings / "instance.dev.yaml")
    assert (real.instance, dev.instance) == ("real", "dev")
    for field in ("database", "backup_folder", "state_folder"):
        assert getattr(real, field).resolve() != getattr(dev, field).resolve(), field
    assert real.overture.places_file.resolve() != dev.overture.places_file.resolve()
    assert real.killswitch_file.resolve() != dev.killswitch_file.resolve()
    assert not launcher._is_real(dev)
    assert launcher._is_real(real)


def test_each_instance_counts_the_others_spend(public_repo_settings):
    real = load_settings(public_repo_settings / "instance.yaml")
    dev = load_settings(public_repo_settings / "instance.dev.yaml")
    assert [p.resolve() for p in real.budget_shared_with] == [dev.database.resolve()]
    assert [p.resolve() for p in dev.budget_shared_with] == [real.database.resolve()]


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_dev_database_and_backups_are_ignored_by_git(public_repo_settings):
    dev = load_settings(public_repo_settings / "instance.dev.yaml")
    for path in (dev.database, dev.backup_folder / "town-2026-10-19-000000.db", dev.killswitch_file):
        result = subprocess.run(["git", "check-ignore", "-q", str(path.relative_to(public_repo_settings))], cwd=public_repo_settings)
        assert result.returncode == 0, f"{path} is not ignored by git"


def test_instance_names_pick_the_right_file():
    assert launcher._instance_file(None) == ROOT / "instance.yaml"
    assert launcher._instance_file("real") == ROOT / "instance.yaml"
    assert launcher._instance_file("dev") == ROOT / "instance.dev.yaml"
    assert launcher._instance_file("C:/elsewhere/instance.yaml") == Path("C:/elsewhere/instance.yaml")


# ---------------- choosing and using the dev instance ----------------

def test_instance_flag_and_environment_variable_both_pick_dev(dev_instance, monkeypatch):
    assert "(dev instance)" in town("--instance", "dev", "status").stdout
    monkeypatch.setenv("TOWN_INSTANCE", "dev")
    assert "(dev instance)" in town("status").stdout
    monkeypatch.delenv("TOWN_INSTANCE")
    assert "(real instance)" in town("status").stdout


def test_dev_starts_stopped_like_any_new_town(dev_instance):
    assert "STOPPED" in town("--instance", "dev", "status").stdout


def test_claude_code_check_applies_only_to_the_real_instance(dev_instance, instance_file, monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    assert town("--instance", "dev", "resume").exit_code == 0
    t = _open(dev_instance)
    action_id = t.mayor.propose("scout", "test_action", "Business 0", {"business_name": "Business 0"}, "No website")
    t.close()
    result = town("--instance", "dev", "reject", str(action_id), "--reason", "Testing on dev")
    assert result.exit_code == 0, result.stdout
    # The same command on the real instance is refused.
    assert town("resume").exit_code == 1
    # The real Town was never touched: still stopped, nothing logged there but the refusal.
    t = _open(instance_file)
    assert t.killswitch.is_stopped()
    assert [r["event_type"] for r in t.conn.execute("SELECT event_type FROM events")] == ["command_refused"]
    t.close()


def test_a_dev_file_that_shares_the_real_database_counts_as_real(dev_instance, instance_file, monkeypatch):
    # Claims to be dev, but points at the real database: treated as real (fail closed).
    dev_instance.write_text(dev_instance.read_text(encoding="utf-8").replace("dev/town.db", "town.db"),
                            encoding="utf-8")
    assert launcher._is_real(load_settings(dev_instance))
    monkeypatch.setenv("CLAUDECODE", "1")
    assert town("--instance", "dev", "resume").exit_code == 1


# ---------------- one budget across both instances ----------------

def _record_cost(instance_file, dollars):
    t = _open(instance_file)
    t.budget.record_cost("scout", "claude-haiku-test", "claude_api", {}, dollars)
    t.close()


def test_dev_spend_counts_toward_the_real_caps_and_back(dev_instance, instance_file):
    real_text = instance_file.read_text(encoding="utf-8")
    instance_file.write_text(real_text + "budget_shared_with:\n  - dev/town.db\n", encoding="utf-8")
    dev_instance.write_text(dev_instance.read_text(encoding="utf-8") + "budget_shared_with:\n  - town.db\n",
                            encoding="utf-8")
    _record_cost(dev_instance, 2.00)
    _record_cost(instance_file, 0.50)

    for path in (instance_file, dev_instance):
        t = _open(path)
        assert t.budget.month_spend() == (2.50, 2.50)
        t.close()
    assert "Includes $2.0000 spent by the other instance" in town("costs").stdout


def test_a_missing_shared_database_counts_as_zero(instance_file):
    instance_file.write_text(instance_file.read_text(encoding="utf-8")
                             + "budget_shared_with:\n  - nowhere/town.db\n", encoding="utf-8")
    t = _open(instance_file)
    assert t.budget.month_spend() == (0.0, 0.0)
    t.close()


def test_claude_code_runs_are_logged_on_dev_too(dev_instance, monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    town("--instance", "dev", "status")
    t = _open(dev_instance)
    row = t.conn.execute("SELECT * FROM events WHERE event_type = 'claude_code_command'").fetchone()
    t.close()
    assert row["agent"] == CLAUDE_CODE
    assert json.loads(row["details"]) == {"command": "status", "instance": "dev"}
