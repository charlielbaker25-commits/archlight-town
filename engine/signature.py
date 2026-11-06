"""Reaper's signature lines (BLUEPRINT 8.3; decisions log 2026-10-27, "Reaper keeps his signature lines").

Information is always plain English. The signature lines come on top, only at key moments, decided by plain code
here rather than by the model's mood:
- the catchphrase, "Never send an organic to do a machine's work.", when Charlie thanks or praises Reaper
  ("thanks", "great job", "nice work"), when a big task is done (a confirmed batch of several items that all went
  through), or at the end of a good sit-rep; never in an ordinary reply, and never twice within a few replies;
- the stand-down line, every time the Town is stood down;
- the other BLUEPRINT 8.3 lines, sparingly, under the same once-in-a-while rule.
A line is never put in place of information: it always comes after the plain sentences.
"""
import re
from typing import Any

CATCHPHRASE = "Never send an organic to do a machine's work."
STAND_DOWN_LINE = "Standing down. All units halted. Awaiting your order to resume."
OTHER_LINES = ("Superior firepower. Superior defenses. Superior performance.", "Maximum firepower achieved.",
               "Full metal everything.")
ALL_LINES = (CATCHPHRASE, *OTHER_LINES)
# A batch this size or bigger, all carried out, counts as "a big task done".
BIG_BATCH = 3
# A signature line may come back only after this many of Reaper's replies without one.
REST_REPLIES = 6


# Thanks and praise, said to Reaper (Charlie, 2026-10-28: "if I said thanks, great job, then it would reply with
# that"). Matched in plain code.
PRAISE = re.compile(r"\b(thanks?|thank you|thx|cheers|great (job|work)|good (job|work)|nice (job|work|one)|well done|"
                    r"awesome|amazing|excellent|perfect|nailed it|good stuff|legend|you rock|impressive|"
                    r"love (it|that))\b", re.IGNORECASE)
# A message that is ONLY thanks or praise ("thanks", "great job Reaper!") gets a reply from plain code, no model.
PRAISE_ONLY = re.compile(r"^\W*(?:(?:reaper|man|mate|buddy|bro|dude|so much|again|a lot|really|that'?s|that was|"
                         r"very|so|you|guys|all|everyone|team)\W*)*$", re.IGNORECASE)
PRAISE_REPLY = "Acknowledged."


def is_praise(said: str) -> bool:
    """Thanks or praise, not a question ("is that a good job for the Builder?" isn't praise)."""
    text = (said or "").strip()
    return bool(PRAISE.search(text)) and not text.endswith("?")


def praise_only(said: str) -> bool:
    """True when the whole message is thanks or praise (nothing else asked)."""
    text = said or ""
    return is_praise(text) and len(text) <= 80 and bool(PRAISE_ONLY.match(PRAISE.sub(" ", text)))


def praise_reply(history: list[dict[str, Any]]) -> str:
    """Reaper's answer to thanks: the catchphrase, unless he used a signature line in the last few replies."""
    return PRAISE_REPLY if recently_used(history, 2) else f"{PRAISE_REPLY} {CATCHPHRASE}"


def has_signature(text: str) -> bool:
    return any(line.lower().rstrip(".") in text.lower() for line in ALL_LINES)


def recently_used(history: list[dict[str, Any]], replies_back: int = REST_REPLIES) -> bool:
    """True if one of Reaper's last few replies already had a signature line."""
    replies = [m.get("content") for m in history if m.get("role") == "assistant" and isinstance(m.get("content"), str)]
    return any(has_signature(r) for r in replies[-replies_back:])


def strip_signature(text: str) -> str:
    """Removes the signature lines from a reply (the information around them stays)."""
    out = text
    for line in ALL_LINES:
        for variant in (line, line.rstrip(".")):
            out = out.replace(variant, "")
    return "\n".join(part.rstrip() for part in out.splitlines()).strip()


def all_clear(facts: dict[str, Any]) -> bool:  # kept for the sit-rep's own rule; not a chat trigger
    """Nothing waiting for Charlie, nothing blocked, no agent in error, no spending warning, the Town running."""
    s = facts["spend"]
    warned = not s.get("readable") or s.get("api_warning") or s.get("total_warning") or s.get("paused")
    return (not facts["stopped"] and not facts["queue"]["pending"] and not warned
            and not any(a["state"] == "error" for a in facts["agents"]))


def just_confirmed(history: list[dict[str, Any]]) -> bool:
    """True if the last thing that happened was Charlie confirming a list (a job done)."""
    users = [m.get("content") for m in history if m.get("role") == "user" and isinstance(m.get("content"), str)]
    return bool(users) and users[-1].startswith("[Town: Charlie confirmed")


def keep_rare(text: str, history: list[dict[str, Any]], moment: bool) -> str:
    """Plain-code check on a model reply: a signature line stays only at a key moment (`moment`: Charlie thanked
    or praised Reaper, or he just confirmed a list) and only if none of the last few replies had one. Otherwise
    it's taken out and the information stays."""
    if has_signature(text) and (not moment or recently_used(history)):
        return strip_signature(text) or text
    return text


def after_batch(results: list[str], count: int) -> str:
    """The catchphrase after a confirmed batch, when it was big and every item went through; otherwise ''."""
    failed = ("not decided", "not edited", "fail", "refused", "nothing started", "held", "blocked")
    if count >= BIG_BATCH and results and not any(word in r.lower() for r in results for word in failed):
        return CATCHPHRASE
    return ""


def good_sitrep(facts: dict[str, Any]) -> bool:
    """A sit-rep worth the catchphrase: the Town running, real work done since the last report, nothing failed,
    nothing blocked and no spending warning."""
    c, q, s = facts["counts"], facts["queue"], facts["spend"]
    work = sum(int(c.get(k) or 0) for k in ("leads_saved", "inspected", "built", "critic_passed", "emails_drafted",
                                             "call_scripts", "approvals"))
    warned = not s.get("readable") or s.get("api_warning") or s.get("total_warning") or s.get("paused")
    return not facts["stopped"] and work > 0 and not c.get("failures") and not q["blocked"] and not warned
