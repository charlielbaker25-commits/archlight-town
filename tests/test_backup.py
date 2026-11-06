import sqlite3
from datetime import datetime, timedelta

from typer.testing import CliRunner

import launcher
from engine.backup import backup_database, ensure_daily_backup
from engine.mayor import Mayor


def _dump(conn):
    return list(conn.iterdump())


def test_town_backup_copy_opens_and_matches_the_original(conn, tmp_path):
    mayor = Mayor(conn, town_stopped=lambda: False)
    mayor.propose("scout", "add_lead", "Hair Studio X", {"business_name": "Hair Studio X"}, "No website")
    path = backup_database(conn, tmp_path / "backups", keep_last=30)

    copy = sqlite3.connect(path)
    try:
        assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        # Compare everything except the backup event itself, which is written after the copy.
        original = [line for line in _dump(conn) if "backup_created" not in line]
        assert _dump(copy) == original
    finally:
        copy.close()


def test_backup_writes_an_event(conn, tmp_path):
    backup_database(conn, tmp_path / "backups", keep_last=30)
    kinds = [r["event_type"] for r in conn.execute("SELECT event_type FROM events")]
    assert "backup_created" in kinds


def test_only_the_newest_backups_are_kept(conn, tmp_path):
    folder = tmp_path / "backups"
    start = datetime(2026, 9, 1, 9, 0, 0)
    for day in range(32):
        backup_database(conn, folder, keep_last=30, now=start + timedelta(days=day))
    names = sorted(p.name for p in folder.glob("town-*.db"))
    assert len(names) == 30
    assert names[0] == "town-2026-09-03-090000.db"


def test_daily_backup_runs_once_per_day(conn, tmp_path):
    folder = tmp_path / "backups"
    morning = datetime(2026, 10, 19, 8, 0, 0)
    assert ensure_daily_backup(conn, folder, 30, morning) is not None
    assert ensure_daily_backup(conn, folder, 30, morning + timedelta(hours=5)) is None
    assert ensure_daily_backup(conn, folder, 30, morning + timedelta(days=1)) is not None


def test_first_town_command_of_the_day_makes_a_backup(instance_file, tmp_path):
    runner = CliRunner()
    runner.invoke(launcher.app, ["resume"])
    result = runner.invoke(launcher.app, ["status"])
    assert "Daily backup secured" in result.stdout
    assert len(list((tmp_path / "backups").glob("town-*.db"))) == 1
    runner.invoke(launcher.app, ["status"])
    assert len(list((tmp_path / "backups").glob("town-*.db"))) == 1


def test_town_backup_command(instance_file, tmp_path):
    result = CliRunner().invoke(launcher.app, ["backup"])
    assert result.exit_code == 0
    assert list((tmp_path / "backups").glob("town-*.db"))
