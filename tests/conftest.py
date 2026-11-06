"""Shared test setup. Every test gets its own throwaway database and state folder."""
import shutil
from pathlib import Path

import pytest

import launcher
from engine.actor import CLAUDE_CODE_MARKERS
from engine.db import connect, init_db
from packs.websites.schema import PACK_SCHEMAS

ROOT = Path(__file__).resolve().parent.parent
PACK = ROOT / "packs" / "websites"


@pytest.fixture(autouse=True)
def instance_file(tmp_path, monkeypatch):
    """Point every test (and every `town` command a test runs) at a throwaway
    instance, so tests never touch the real database, state or backups."""
    path = tmp_path / "instance.yaml"
    path.write_text(
        "pack: websites\n"
        "database: town.db\n"
        "backup_folder: backups\n"
        "state_folder: state\n"
        "overture:\n"
        "  region_name: Test Town\n"
        "  center_lat: 38.6488\n"
        "  center_lon: -90.3108\n"
        "  radius_miles: 15\n"
        "  places_file: places.parquet\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TOWN_INSTANCE", str(path))
    return path


@pytest.fixture
def dev_instance(tmp_path, instance_file, monkeypatch):
    """A throwaway dev instance next to the throwaway real one. `--instance dev` and
    TOWN_INSTANCE=dev pick it, and the real instance is the throwaway instance_file."""
    path = tmp_path / "instance.dev.yaml"
    text = instance_file.read_text(encoding="utf-8")
    for name in ("town.db", "backups", "state", "places.parquet"):
        text = text.replace(f": {name}\n", f": dev/{name}\n")
    path.write_text("instance: dev\n" + text, encoding="utf-8")
    monkeypatch.setattr(launcher, "INSTANCE_FILES", {launcher.REAL: instance_file, launcher.DEV: path})
    return path


@pytest.fixture(autouse=True)
def as_charlie(monkeypatch):
    """Tests run as Charlie by default, even when pytest itself runs inside Claude Code.
    Tests about Claude Code set CLAUDECODE themselves."""
    for name in CLAUDE_CODE_MARKERS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def conn(tmp_path):
    connection = connect(tmp_path / "town.db")
    init_db(connection, PACK_SCHEMAS)
    yield connection
    connection.close()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """When an HQ page or stress test fails, its server's timing log goes in the failure report (its own section),
    so a slow laptop shows which step waited: opening the Town, the chat lock, Reaper's turn (tests/hq_live.py)."""
    outcome = yield
    report = outcome.get_result()
    live = getattr(item, "funcargs", {}).get("live")
    if report.failed and report.when in ("setup", "call") and hasattr(live, "timing_report"):
        report.sections.append(("HQ timing log", live.timing_report()))


@pytest.fixture
def limits_file(tmp_path):
    """A private copy of limits.yaml, so tests can change limits without touching the real file."""
    copy = tmp_path / "limits.yaml"
    shutil.copy(PACK / "limits.yaml", copy)
    return copy

# Public tests use generated configurations, never a private instance file.
@pytest.fixture
def public_repo_settings(tmp_path, monkeypatch):
    import subprocess
    folder = tmp_path / "repo-settings"
    folder.mkdir()
    common = ("pack: websites\noverture:\n  region_name: Example Town\n  center_lat: 0\n"
              "  center_lon: 0\n  radius_miles: 15\n  places_file: {places}\n")
    (folder / "instance.yaml").write_text(
        "instance: real\ndatabase: data/town.db\nbackup_folder: backups\nstate_folder: state\n"
        "budget_shared_with: [dev/town.db]\n" + common.format(places="data/places.parquet"), encoding="utf-8")
    (folder / "instance.dev.yaml").write_text(
        "instance: dev\ndatabase: dev/town.db\nbackup_folder: dev/backups\nstate_folder: dev/state\n"
        "budget_shared_with: [data/town.db]\n" + common.format(places="dev/places.parquet"), encoding="utf-8")
    # Check ignore behavior in a throwaway git repo even before the user git-inits
    # the public snapshot. This creates no history and never contacts a remote.
    shutil.copy(ROOT / ".gitignore", folder / ".gitignore")
    if shutil.which("git"):
        subprocess.run(["git", "init", "-q", str(folder)], check=True)
    monkeypatch.setattr(launcher, "INSTANCE_FILES", {launcher.REAL: folder / "instance.yaml",
                                                   launcher.DEV: folder / "instance.dev.yaml"})
    return folder
