import pytest

from engine.executor import Executor, ExecutorError, Handler
from engine.killswitch import KillSwitch, TownStopped
from engine.mayor import Mayor, MayorError


def _running_switch(tmp_path, conn):
    switch = KillSwitch(tmp_path / "state" / "killswitch.json")
    switch.resume(conn)
    return switch


def test_a_new_town_starts_stopped(tmp_path):
    assert KillSwitch(tmp_path / "state" / "killswitch.json").is_stopped()


def test_stop_and_resume(tmp_path, conn):
    switch = _running_switch(tmp_path, conn)
    assert not switch.is_stopped()
    switch.stop(conn, "testing")
    assert switch.is_stopped()
    with pytest.raises(TownStopped):
        switch.check()
    switch.resume(conn)
    switch.check()
    kinds = [r["event_type"] for r in conn.execute("SELECT event_type FROM events ORDER BY id")]
    assert kinds == ["town_resumed", "town_stopped", "town_resumed"]


def test_garbled_killswitch_file_counts_as_stopped(tmp_path, conn):
    switch = _running_switch(tmp_path, conn)
    switch.path.write_text("{not json", encoding="utf-8")
    assert switch.is_stopped()


def test_unreadable_killswitch_file_counts_as_stopped(tmp_path, conn):
    switch = _running_switch(tmp_path, conn)
    switch.path.unlink()
    switch.path.mkdir()  # a folder where the file should be: reading it raises an error
    assert switch.is_stopped()


def test_town_stop_works_even_when_the_database_is_broken(instance_file, tmp_path):
    from typer.testing import CliRunner

    import launcher

    switch = KillSwitch(tmp_path / "state" / "killswitch.json")
    switch._write(False, "")  # running
    (tmp_path / "town.db").write_bytes(b"this is not a database" * 100)
    result = CliRunner().invoke(launcher.app, ["stop"])
    assert result.exit_code == 0 and "Standing down" in result.stdout
    assert switch.is_stopped()


def test_after_town_stop_no_agent_runs_and_no_action_executes(tmp_path, conn):
    switch = _running_switch(tmp_path, conn)
    runs = []
    handler = Handler("test_action", False, lambda c, i, content: runs.append(i) or "ran", str)
    mayor = Mayor(conn, town_stopped=switch.is_stopped)
    executor = Executor(conn, {"test_action": handler}, town_stopped=switch.is_stopped)
    approved = mayor.propose("scout", "test_action", "x", {}, "before the stop")
    mayor.approve(approved)

    switch.stop(conn)

    # An approved action can't execute, one at a time or in bulk
    with pytest.raises(ExecutorError):
        executor.execute(approved)
    with pytest.raises(ExecutorError):
        executor.execute_approved()
    # No agent can start a step or propose anything new
    with pytest.raises(TownStopped):
        switch.check()
    with pytest.raises(MayorError):
        mayor.propose("scout", "test_action", "x", {}, "during the stop")
    assert runs == []
    assert mayor.get(approved)["status"] == "approved"

    # After resume, the held action runs once
    switch.resume(conn)
    executor.execute(approved)
    assert runs == [approved]
