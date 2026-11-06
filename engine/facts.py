"""Town facts: everything Reaper and the village map need, read by plain code from the database and the log.

Never from a model. Reaper's words are built on these numbers, the map draws them, and the sit-rep counts
them, so a number Reaper says can always be traced back to rows in the database.

The engine knows the shape (the queue, spend against the caps, agent states, counts); the pack supplies a
TownView that says what its events and tables mean (which agents exist, what "a lead was built" is, which
event means an agent started or finished work). The engine never imports the pack.
"""
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from engine.db import utc_now
from engine.flows import Flow, event_dict

STATES = ("idle", "working", "waiting_for_approval", "done", "error")
# A "done" agent goes back to idle this long after it finished, so the map shows what's happening now.
DONE_SHOWS_MINUTES = 15
# How many recent events are read to work out each agent's state.
STATE_SCAN_EVENTS = 3000


@dataclass(frozen=True)
class AgentInfo:
    name: str           # the code name used in the log, e.g. "scout"
    title: str          # e.g. "Scout"
    building: str       # e.g. "Lookout Tower"


@dataclass(frozen=True)
class Activity:
    """What one event says about one agent: it started work, finished, or hit an error."""
    agent: str
    state: str          # "working", "done" or "error" (waiting and idle are worked out by the engine)
    task: str


class TownView(Protocol):
    """What a pack tells the engine about its own Town."""
    agents: tuple[AgentInfo, ...]
    # A "working" agent with no finish event after this long counts as an error (the job died without one).
    stale_working_minutes: int

    def activity(self, conn: sqlite3.Connection, event: dict[str, Any]) -> Activity | None: ...

    def flows(self, conn: sqlite3.Connection, event: dict[str, Any]) -> list[Flow]: ...

    def counts(self, conn: sqlite3.Connection, since: str) -> dict[str, int]: ...

    def lead(self, conn: sqlite3.Connection, lead_id: int) -> dict[str, Any] | None: ...

    def build(self, conn: sqlite3.Connection, build_id: int) -> dict[str, Any] | None: ...

    def related_events(self, conn: sqlite3.Connection, action: dict[str, Any]) -> list[dict[str, Any]]: ...

    def todays_plan(self, town: Any) -> list[str]: ...

    def day_start_utc(self) -> str: ...

    def scoreboard(self, conn: sqlite3.Connection) -> dict[str, Any]: ...

    def town_iq(self, conn: sqlite3.Connection) -> dict[str, Any]: ...

    def playbook(self, agent: str) -> str | None: ...

    def analysis_data(self, conn: sqlite3.Connection) -> dict[str, Any]: ...


def _parse(ts: str) -> datetime | None:
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hours_ago(hours: float, now: datetime | None = None) -> str:
    return _iso((now or datetime.now(timezone.utc)) - timedelta(hours=hours))


# ---------------- the queue ----------------

def queue(town: Any) -> dict[str, Any]:
    """Every pending proposal with its Guard result as of now (the hard rules re-run, read-only, plus the soft
    flags it was proposed with), grouped by action type and agent so they can be handled in batches.
    Doesn't log `queue_viewed`: reading facts for the map isn't Charlie opening the queue."""
    rows = town.conn.execute("SELECT * FROM actions WHERE status = 'proposed' ORDER BY id").fetchall()
    items = []
    for row in rows:
        try:
            verdict = town.mayor.guard_recheck(row)
            guard = ({"verdict": verdict.verdict, "blocks": list(verdict.blocks), "flags": list(verdict.flags)}
                     if verdict else {"verdict": "not checked", "blocks": [], "flags": []})
        except Exception as err:   # a rule that can't run counts as a block (fail closed)
            guard = {"verdict": "blocked", "blocks": [f"the Guard couldn't check it ({type(err).__name__})"],
                     "flags": []}
        items.append({"id": row["id"], "agent": row["agent"], "action_type": row["action_type"],
                      "target": row["target"], "reason": row["reason"], "created_at": row["created_at"],
                      "cost_estimate_usd": row["cost_estimate_usd"], "guard": guard,
                      "blocked": guard["verdict"] == "blocked"})
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for item in items:
        key = (item["action_type"], item["agent"])
        group = groups.setdefault(key, {"action_type": key[0], "agent": key[1], "ids": [], "blocked_ids": []})
        group["ids"].append(item["id"])
        if item["blocked"]:
            group["blocked_ids"].append(item["id"])
    return {"pending": len(items), "blocked": sum(1 for i in items if i["blocked"]), "items": items,
            "groups": list(groups.values())}


# ---------------- money ----------------

def spend(town: Any) -> dict[str, Any]:
    """This month's spend against both caps, with the 80% early warnings. Fails closed like the budget: if
    spend can't be read, it says so and counts paid work as paused."""
    try:
        api, total = town.budget.month_spend()
        api_cap, total_cap = town.budget.caps()
        warn_at = float(town.limits.get("money.early_warning_fraction"))
        paused = town.budget.paused_reason()
    except Exception as err:
        return {"readable": False, "error": f"{type(err).__name__}", "paused": "spend unreadable (fail closed)"}
    return {"readable": True, "api_usd": round(api, 4), "api_cap_usd": api_cap, "total_usd": round(total, 4),
            "total_cap_usd": total_cap, "api_warning": api >= api_cap * warn_at,
            "total_warning": total >= total_cap * warn_at, "warning_fraction": warn_at, "paused": paused}


# ---------------- agent states ----------------

def agent_states(conn: sqlite3.Connection, view: TownView, pending: list[dict[str, Any]],
                 now: datetime | None = None) -> list[dict[str, Any]]:
    """Each agent's state, worked out from its last start / finish / error event in the log:
    - working: its last event says it started (and it isn't stale),
    - error: its last event was an error, or it "started" longer ago than the stale limit with no finish,
    - waiting_for_approval: not working, and something it proposed waits in the queue,
    - done: it finished within the last DONE_SHOWS_MINUTES,
    - idle: otherwise.
    The Mayor waits for approval whenever the queue isn't empty (it holds the queue)."""
    now = now or datetime.now(timezone.utc)
    last: dict[str, tuple[Activity, str, int]] = {}
    rows = conn.execute("SELECT * FROM (SELECT * FROM events ORDER BY id DESC LIMIT ?) ORDER BY id",
                        (STATE_SCAN_EVENTS,)).fetchall()
    for row in rows:
        try:
            act = view.activity(conn, event_dict(row))
        except Exception:
            act = None
        if act is not None and act.state in ("working", "done", "error"):
            last[act.agent] = (act, row["ts"], row["id"])
    waiting: dict[str, int] = {}
    for item in pending:
        waiting[item["agent"]] = waiting.get(item["agent"], 0) + 1
    states = []
    for info in view.agents:
        act, ts, event_id = last.get(info.name, (None, None, None))
        state, task = ("idle", "") if act is None else (act.state, act.task)
        when = _parse(ts) if ts else None
        if state == "working" and when and now - when > timedelta(minutes=view.stale_working_minutes):
            state, task = "error", f"no finish recorded for: {task}"
        if state != "working":
            count = len(pending) if info.name == "mayor" else waiting.get(info.name, 0)
            if count:
                state, task = "waiting_for_approval", f"{count} awaiting approval"
            elif state == "done" and when and now - when > timedelta(minutes=DONE_SHOWS_MINUTES):
                state = "idle"
        states.append({"agent": info.name, "title": info.title, "building": info.building, "state": state,
                       "task": task, "since": ts, "event_id": event_id})
    return states


# ---------------- events ----------------

def recent_events(conn: sqlite3.Connection, limit: int = 15, agent: str | None = None,
                  event_type: str | None = None, action_id: int | None = None) -> list[dict[str, Any]]:
    query, params = "SELECT * FROM events WHERE 1=1", []
    for column, value in (("agent", agent), ("event_type", event_type), ("action_id", action_id)):
        if value is not None:
            query += f" AND {column} = ?"   # column names are fixed above, never from input
            params.append(value)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, min(int(limit), 100)))
    return [event_dict(r) for r in reversed(conn.execute(query, params).fetchall())]


def last_event_ts(conn: sqlite3.Connection, event_type: str) -> str | None:
    row = conn.execute("SELECT ts FROM events WHERE event_type = ? ORDER BY id DESC LIMIT 1",
                       (event_type,)).fetchone()
    return row["ts"] if row else None


# ---------------- everything together ----------------

def town_facts(town: Any, view: TownView, since: str | None = None, events: int = 15) -> dict[str, Any]:
    """The one function: every fact Reaper, the map and the sit-rep use, as plain data.
    `since` (UTC) is where the counts start; by default the last 24 hours."""
    since = since or hours_ago(24)
    q = queue(town)
    noun = getattr(view, "noun", None)   # the pack's everyday name for each action type, if it has one
    for entry in (*q["items"], *q["groups"]):
        entry["what"] = noun(entry["action_type"]) if callable(noun) else entry["action_type"].replace("_", " ")
    return {
        "as_of": utc_now(),
        "instance": town.settings.instance,
        "stopped": town.killswitch.is_stopped(),
        "queue": q,
        "approved_waiting_to_run": town.conn.execute(
            "SELECT COUNT(*) FROM actions WHERE status = 'approved'").fetchone()[0],
        "since": since,
        "counts": view.counts(town.conn, since),
        "spend": spend(town),
        "agents": agent_states(town.conn, view, q["items"]),
        "recent_events": recent_events(town.conn, events),
    }


def village(town: Any, view: TownView) -> dict[str, Any]:
    """The village's buildings beyond the agents (BLUEPRINT 9.2), all plain code: the scoreboard, the Bank,
    the Library's town IQ and the Gatehouse's blocks."""
    money = spend(town)
    board = dict(view.scoreboard(town.conn))
    board["cost_this_month_usd"] = money.get("total_usd")
    q = queue(town)
    blocks_24h = town.conn.execute("SELECT COUNT(*) FROM events WHERE event_type = 'guard_blocked' AND ts >= ?",
                                   (hours_ago(24),)).fetchone()[0]
    # The pack's own extra panels (plain data, read-only), if it has any.
    extras = getattr(view, "village_extras", None)
    return {
        "extras": extras(town) if callable(extras) else {},
        "done_today": done_today(town.conn, view),
        "scoreboard": board,
        "bank": {**money, "revenue_usd": board.get("revenue_usd", 0.0), "mrr_usd": board.get("mrr_usd", 0.0)},
        # The Library: the town IQ, plus the lessons (proposed and approved, with evidence) if the pack keeps any.
        "library": {**view.town_iq(town.conn), "lessons": (getattr(view, "lessons", None) or (lambda c: []))(town.conn)},
        "gatehouse": {"red_flag": q["blocked"] > 0, "blocked_now": [
            {"id": i["id"], "action_type": i["action_type"], "target": i["target"], "blocks": i["guard"]["blocks"]}
            for i in q["items"] if i["blocked"]], "blocks_last_24h": int(blocks_24h)},
    }


def done_today(conn: sqlite3.Connection, view: TownView) -> int:
    """How many jobs the agents finished today (the pack's day). The pack says which events are finished jobs
    (`job_done_events`); without that list, every event that means "done" for an agent counts."""
    kinds = getattr(view, "job_done_events", None)
    if kinds:
        marks = ",".join("?" * len(kinds))
        return int(conn.execute(f"SELECT COUNT(*) FROM events WHERE ts >= ? AND event_type IN ({marks})",
                                (view.day_start_utc(), *sorted(kinds))).fetchone()[0])
    count = 0
    for row in conn.execute("SELECT * FROM events WHERE ts >= ? ORDER BY id", (view.day_start_utc(),)):
        try:
            act = view.activity(conn, event_dict(row))
        except Exception:
            act = None
        count += act is not None and act.state == "done"
    return count


def agent_detail(town: Any, view: TownView, agent: str) -> dict[str, Any] | None:
    """One building's panel: the agent's state and current task, its last 10 events, and its playbook."""
    states = {a["agent"]: a for a in agent_states(town.conn, view, queue(town)["items"])}
    if agent not in states:
        return None
    return {**states[agent], "recent_events": recent_events(town.conn, 10, agent=agent),
            "playbook": view.playbook(agent)}


def compact(facts: dict[str, Any]) -> str:
    """A short text version of the facts for Reaper's prompt (a few hundred tokens, no event details)."""
    q, s = facts["queue"], facts["spend"]
    lines = [f"as_of {facts['as_of']} UTC; instance {facts['instance']}; "
             f"town {'STOPPED (stood down)' if facts['stopped'] else 'RUNNING'}",
             f"queue: {q['pending']} pending, {q['blocked']} BLOCKED; approved not yet run: "
             f"{facts['approved_waiting_to_run']}"]
    for g in q["groups"]:
        blocked = f" (BLOCKED: {', '.join('#' + str(i) for i in g['blocked_ids'])})" if g["blocked_ids"] else ""
        lines.append(f"  {g['action_type']} from {g['agent']}: {', '.join('#' + str(i) for i in g['ids'])}{blocked}")
    lines.append(f"since {facts['since']} UTC: " + ", ".join(f"{k} {v}" for k, v in facts["counts"].items()))
    if s.get("readable"):
        lines.append(f"spend this month: API ${s['api_usd']:.4f} of ${s['api_cap_usd']:.2f}"
                     f"{' (80% WARNING)' if s['api_warning'] else ''}; total ${s['total_usd']:.4f} of "
                     f"${s['total_cap_usd']:.2f}{' (80% WARNING)' if s['total_warning'] else ''}"
                     f"{'; PAID WORK PAUSED: ' + str(s['paused']) if s['paused'] else ''}")
    else:
        lines.append("spend this month: unreadable, so paid work counts as paused")
    lines.append("agents: " + "; ".join(f"{a['title']} {a['state']}" + (f" ({a['task']})" if a["task"] else "")
                                        for a in facts["agents"]))
    return "\n".join(lines)


def to_json(facts: dict[str, Any]) -> str:
    return json.dumps(facts, sort_keys=True, default=str)
