"""Builds each agent's instructions: its job description plus its playbook.

Playbooks (packs/websites/playbooks/<agent>.md) hold approved lessons only.
They are read here and never written by code: a playbook only changes
through an approved Librarian action (playbook_gate.py's playbook_change handler).

playbook_override() lets the playbook gate run an agent's test set with a PROPOSED playbook, in memory, without
touching the file: while it's active, that agent's instructions use the proposed text instead.
"""
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

PLAYBOOKS_DIR = Path(__file__).resolve().parent / "playbooks"
_OVERRIDES: dict[str, str] = {}


def load_playbook(agent: str, folder: Path | None = None) -> str:
    if agent in _OVERRIDES:
        return _OVERRIDES[agent].strip()
    path = Path(folder or PLAYBOOKS_DIR) / f"{agent}.md"
    return path.read_text(encoding="utf-8").strip() if path.exists() else ""


@contextmanager
def playbook_override(agent: str, text: str) -> Iterator[None]:
    """For the test-set gate only: the agent's playbook reads as `text` until the block ends."""
    previous = _OVERRIDES.get(agent)
    _OVERRIDES[agent] = text
    try:
        yield
    finally:
        if previous is None:
            _OVERRIDES.pop(agent, None)
        else:
            _OVERRIDES[agent] = previous


def agent_instructions(agent: str, job: str, folder: Path | None = None) -> str:
    playbook = load_playbook(agent, folder)
    if not playbook:
        return job
    return f"{job}\n\nYour playbook (lessons Charlie approved; follow them):\n{playbook}"
