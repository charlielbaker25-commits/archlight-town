"""The kill switch: `town stop` halts every agent and every execution.

The state lives in a small JSON file (state/killswitch.json) rather than the
database, so every process can check it instantly even if the database is busy.

It fails closed: the Town only counts as running if the file exists, can be
read, and says {"stopped": false}. A missing, unreadable or garbled file means
stopped. So a brand-new Town starts stopped until Charlie runs `town resume`.

Only Charlie stops or resumes it (through `town stop` / `town resume`).
No agent code calls stop() or resume(). Claude Code may only do it on the dev instance.
"""
import json
import sqlite3
import time
from pathlib import Path

from engine.db import utc_now
from engine.events import log_event


SWAP_ATTEMPTS = 40
SWAP_WAIT_SECONDS = 0.05


class TownStopped(RuntimeError):
    """Raised when work tries to start or continue while the Town is stopped."""


class KillSwitch:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def is_stopped(self) -> bool:
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
            return state.get("stopped") is not False
        except Exception:
            # Missing, unreadable or not valid JSON: fail closed.
            return True

    def check(self) -> None:
        """Agents call this before each step. Raises if the Town is stopped."""
        if self.is_stopped():
            raise TownStopped("The Town is stopped. Standing by for `town resume`.")

    # `by` is who gave the order: "charlie", or "claude-code" on the dev instance.
    def engage(self, reason: str = "", by: str = "charlie") -> None:
        """Write the stopped state without touching the database, so `town stop`
        works even if the database is locked or broken."""
        self._write(True, reason, by)

    def stop(self, conn: sqlite3.Connection, reason: str = "", by: str = "charlie", via: str | None = None) -> None:
        """`via` = "reaper" when Charlie said "stand down" to Reaper (the order is still his)."""
        self.engage(reason, by)
        log_event(conn, by, "town_stopped", {"reason": reason, **({"via": via} if via else {})})

    def resume(self, conn: sqlite3.Connection, by: str = "charlie") -> None:
        self._write(False, "", by)
        log_event(conn, by, "town_resumed", {})

    def _write(self, stopped: bool, reason: str, by: str = "charlie") -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"stopped": stopped, "changed_at": utc_now(), "by": by,
                                   "reason": reason}), encoding="utf-8")
        # On Windows, swapping the file in fails while another thread or process is reading it (they check it
        # constantly). That read takes milliseconds, so try again briefly; give up only after about 2 seconds.
        for attempt in range(SWAP_ATTEMPTS):
            try:
                tmp.replace(self.path)
                return
            except PermissionError:
                if attempt == SWAP_ATTEMPTS - 1:
                    raise
                time.sleep(SWAP_WAIT_SECONDS)
