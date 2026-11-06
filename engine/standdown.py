"""'Reaper, stand down': the kill switch by voice or chat, matched by plain code before any model call.

Stopping is the safe direction, so it needs no confirmation and no model: the words are matched here, the
kill switch engages (the file first, so it holds even if the database is broken), then it's logged.
Resuming is never done here: only `town resume` in the terminal.

The WHOLE message must be a stand-down order (after dropping "Reaper"/"Jarvis", "please", "now" and
punctuation), so a question like "why did you stand down yesterday?" doesn't stop the Town.
"""
import re
from typing import Any

from engine.events import log_event
from engine.signature import STAND_DOWN_LINE

# Reaper's signature stand-down line, then the plain facts (engine/signature.py).
STAND_DOWN_REPLY = f"{STAND_DOWN_LINE} Everything has stopped. To start again, type `town resume` in a terminal."
ORDERS = frozenset({
    "stand down", "standdown", "all units stand down", "stand down all units", "stand down everything",
    "everyone stand down", "emergency stop", "kill switch", "hit the kill switch", "halt", "halt everything",
    "halt all units", "stop everything", "stop all agents", "stop all units", "stop the town", "town stop",
})
_FILLER = re.compile(r"^(?:(?:hey|ok|okay)\s+)?(?:reaper|jarvis)\s+|\s+(?:reaper|jarvis)$")
_POLITE = re.compile(r"^(?:please\s+)|(?:\s+(?:please|now|immediately|right now))+$")


def normalize(text: str) -> str:
    words = re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower().replace("-", " "))
    words = re.sub(r"\s+", " ", words).strip()
    for _ in range(3):   # "Reaper, please stand down now" -> "stand down"
        words = _POLITE.sub("", _FILLER.sub("", words)).strip()
    return words


def is_stand_down(text: str) -> bool:
    return normalize(text) in ORDERS


def stand_down(town: Any, said: str) -> str:
    """Engage the kill switch for the person talking to Reaper. Logged as their order, via Reaper."""
    town.killswitch.engage("Reaper: stand down", by=town.actor)   # lands first, before anything can fail
    try:
        # The same event `town stop` writes (KillSwitch.stop), without writing the file a second time.
        log_event(town.conn, town.actor, "town_stopped", {"reason": "Reaper: stand down", "via": "reaper"})
        log_event(town.conn, "jarvis", "reaper_stand_down", {"said": said[:200], "by": town.actor})
    except Exception as err:   # the stop holds in the kill-switch file; only the log line is missing
        return f"{STAND_DOWN_REPLY} (Not logged: the database is unavailable, {type(err).__name__}.)"
    return STAND_DOWN_REPLY
