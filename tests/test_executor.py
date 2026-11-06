import pytest

from engine.db import connect
from engine.executor import Executor, ExecutorError, Handler
from engine.mayor import Mayor


class Recorder:
    """A fake handler that counts how many times it really ran."""

    def __init__(self):
        self.calls = 0

    def run(self, conn, action_id, content):
        self.calls += 1
        return "done"


def _setup(conn, external=False, stopped=False, live=frozenset()):
    rec = Recorder()
    handler = Handler("test_action", external, rec.run, lambda c: f"send {c['msg']}")
    executor = Executor(conn, {"test_action": handler}, town_stopped=lambda: stopped, live_external_types=live)
    return Mayor(conn, town_stopped=lambda: False), executor, rec


def _propose(mayor):
    return mayor.propose("scout", "test_action", "someone", {"msg": "hello"}, "testing")


def test_executor_refuses_an_action_that_isnt_approved(conn):
    mayor, executor, rec = _setup(conn)
    action_id = _propose(mayor)
    with pytest.raises(ExecutorError):
        executor.execute(action_id)
    assert rec.calls == 0
    assert mayor.get(action_id)["status"] == "proposed"


def test_a_rejected_action_never_runs(conn):
    mayor, executor, rec = _setup(conn)
    action_id = _propose(mayor)
    mayor.reject(action_id, "not needed")
    with pytest.raises(ExecutorError):
        executor.execute(action_id)
    assert executor.execute_approved() == []
    assert rec.calls == 0
    assert mayor.get(action_id)["status"] == "rejected"


def test_an_approved_action_runs_exactly_once(conn):
    mayor, executor, rec = _setup(conn)
    action_id = _propose(mayor)
    mayor.approve(action_id)
    outcome = executor.execute(action_id)
    assert outcome.status == "executed"
    with pytest.raises(ExecutorError):
        executor.execute(action_id)
    assert executor.execute_approved() == []
    assert rec.calls == 1
    assert mayor.get(action_id)["result"] == "done"


def test_two_processes_cannot_both_run_the_same_action(conn, tmp_path):
    mayor, executor, rec = _setup(conn)
    action_id = _propose(mayor)
    mayor.approve(action_id)
    # A second connection to the same database file stands in for a second process.
    other = connect(tmp_path / "town.db")
    other_executor = Executor(other, executor.handlers, town_stopped=lambda: False)
    executor.execute(action_id)
    with pytest.raises(ExecutorError):
        other_executor.execute(action_id)
    other.close()
    assert rec.calls == 1


def test_external_actions_are_dry_run_by_default(conn):
    mayor, executor, rec = _setup(conn, external=True)
    action_id = _propose(mayor)
    mayor.approve(action_id)
    outcome = executor.execute(action_id)
    assert outcome.dry_run is True
    assert "DRY RUN" in outcome.result and "send hello" in outcome.result
    assert rec.calls == 0


def test_external_action_runs_only_when_its_type_is_switched_on(conn):
    mayor, executor, rec = _setup(conn, external=True, live=frozenset({"test_action"}))
    action_id = _propose(mayor)
    mayor.approve(action_id)
    assert executor.execute(action_id).dry_run is False
    assert rec.calls == 1


def test_a_failing_handler_marks_the_action_failed_and_does_not_retry(conn):
    mayor, _, _ = _setup(conn)

    def boom(conn, action_id, content):
        raise RuntimeError("network down")

    executor = Executor(conn, {"test_action": Handler("test_action", False, boom, str)}, town_stopped=lambda: False)
    action_id = _propose(mayor)
    mayor.approve(action_id)
    outcome = executor.execute(action_id)
    assert outcome.status == "failed"
    assert "network down" in outcome.result
    with pytest.raises(ExecutorError):
        executor.execute(action_id)
