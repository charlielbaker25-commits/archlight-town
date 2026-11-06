"""Who is running this `town` command: Charlie, or Claude Code.

Claude Code sets the CLAUDECODE environment variable (and CLAUDE_CODE_ENTRYPOINT)
in every shell it starts, and child processes like `town` inherit them. If either
is present, the command came from Claude Code.

Fail closed: if the environment can't be read, the actor counts as Claude Code.
There is deliberately no override switch, because Claude Code could set it.
This check stops accidents; the CLAUDE.md rule covers the rest.
"""
import os
from collections.abc import Mapping

CHARLIE = "charlie"
CLAUDE_CODE = "claude-code"
CLAUDE_CODE_MARKERS = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")


def current_actor(environ: Mapping[str, str] | None = None) -> str:
    try:
        env = os.environ if environ is None else environ
        if any(name in env for name in CLAUDE_CODE_MARKERS):
            return CLAUDE_CODE
        return CHARLIE
    except Exception:
        return CLAUDE_CODE
