"""Autopilot (Charlie, 2026-11-04): the Mayor passes approved work to the next agent by itself (BLUEPRINT 3.3).

`town autopilot` runs one cycle every `autopilot.cycle_minutes` (limits.yaml) until Ctrl+C or the kill switch;
`town autopilot --once` runs a single cycle. One cycle, in this order:
  a. preview upkeep (expired previews come down) and the Clerk's inbox check;
  b. inspect every saved lead not yet inspected (the Inspector's daily cap);
  c. build every build-worthy lead not yet built, oldest first, up to the lower of `autopilot.builds_per_day` and
     `building.sites_per_day` (every build today counts, manual ones too). The Critic and its send-backs run as
     with `town build`;
  d. the outreach package (Charlie, 2026-11-05): for each Critic-passed build that never had one, the Messenger
     writes the first email against the build's reserved preview link and the site and the email go to Charlie as
     ONE proposal (packs/websites/package.py). Autopilot no longer proposes a separate publish or a separate first
     email (both still exist by hand: `town propose-publish`, `town draft`);
  e. a follow-up for each lead the Guard's follow-up rule allows (a proposal);
  f. Scout (`town scout --auto`, once) only if fewer than `autopilot.leads_waiting_min` saved leads are waiting to
     be built AND no new-lead proposal is waiting for Charlie (the daily lead cap applies). Scout saves the
     picks that pass every plain-code check itself (2026-11-05); nothing it does leaves the Town.

The whole chain: Scout saves leads -> inspect -> build + Critic -> package -> waits for Charlie -> (approved) live +
email -> the inbox. `town autopilot` runs it in a terminal; "go" in HQ or Reaper starts the same loop in the
background once Charlie confirms (start_background). Only one loop runs at a time.

Every step goes through the SAME checks and code as its `town` command: the orders' check() and run()
(packs/websites/orders.py), which call the commands' own gates and caps. Autopilot NEVER approves, rejects or edits
anything: everything that reaches the outside world (a new lead, a publish, an email, a reply that needs Charlie)
waits in `town pending` for him. Call scripts are never drafted here: only `town draft <lead id>` writes one.

Work moves right after a decision (Charlie, 2026-11-04): while `town autopilot` runs, it watches the log for
Charlie's approvals (terminal, Reaper or HQ's Needs you panel) and, within `autopilot.react_seconds`, runs a
REACT cycle: only steps b, c and d (inspect, build with the Critic, first emails), so an approved lead goes
straight to the Inspector and, if build-worthy and under the cap, the Builder, and an approved publish goes
straight to its email draft. The same gates and caps as a full cycle; cycles run one at a time in the one loop
(`serve`); the full cycle every `autopilot.cycle_minutes` keeps everything else (inbox, follow-ups, Scout). When a
cap stops the chain, it says so in plain words and logs `autopilot_held` ("Builder: 3 of 3 builds today; Coco
Nails Spa builds tomorrow"): the activity line shows it and its courier stops at the Town Hall.

A plan usage-limit error stops the plan steps (builds and drafts) until the reset (the existing rule:
claude_cli.check_available); the rest of the cycle carries on. The kill switch is checked before every step; a
stopped Town ends the cycle at once and nothing runs. A cycle with nothing to do makes no model call.
"""
import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from engine.claude_cli import SubscriptionLimitReached
from engine.events import log_event
from engine.killswitch import TownStopped
from engine.orders import Order, OrderRefused
from engine.town import Town
from packs.websites import clerk, explore, golive, previews
from packs.websites.builder import _has_good_build, _latest_inspection, _subjects, builds_today
from packs.websites.inspector import InspectorLimits, _leads_to_inspect, inspections_today
from packs.websites.leads import email_problem, is_held
from packs.websites.messenger import EMAIL, FOLLOW_UP_KIND, MessengerError, prepare
from packs.websites.orders import ORDERS
from packs.websites.package import PackageError, propose_package, ready_builds

AGENT = "mayor"                  # the Mayor routes approved work (BLUEPRINT 3.3)
STATE_FILE = "autopilot.json"
STOP_FILE = "autopilot.stop"     # HQ's Stop button writes it; the loop ends at its next check (any process)
WAIT_STEP_SECONDS = 5.0          # how often a waiting loop checks the kill switch and the stop request


@dataclass
class Cycle:
    ran: list[str] = field(default_factory=list)       # one plain line per step that did something
    skipped: list[str] = field(default_factory=list)   # steps that had nothing to do, or were refused, and why
    plan_paused: str = ""                               # the usage-limit message, if plan work stopped
    waiting: int = 0                                     # proposals waiting for Charlie after the cycle
    line: str = ""


def waiting_for_charlie(town: Town) -> int:
    return int(town.conn.execute("SELECT COUNT(*) FROM actions WHERE status = 'proposed'").fetchone()[0])


def leads_waiting_to_build(town: Town) -> int:
    """Saved leads the Town could still build for: not held (no email), not bounced, no good build yet, and not
    inspected as not build-worthy."""
    count = 0
    for row in town.conn.execute("SELECT * FROM leads ORDER BY id"):
        if is_held(row) or row["status"] == "bounced" or email_problem(row["public_email"]) \
                or _has_good_build(town, row["id"]):
            continue
        inspection = _latest_inspection(town, row["id"])
        if inspection and not inspection["build_worthy"]:
            continue
        count += 1
    return count


def _order(town: Town, name: str, **params: Any) -> str:
    """The order's own check (the command's gates and caps), then its run. OrderRefused when a rule says no."""
    order = Order(name, params)
    ORDERS.check(town, order)
    return ORDERS.run(town, order)


def run_cycle(town: Town, gmail_client: Any, publisher: Any, react: bool = False) -> Cycle:
    """One autopilot cycle (the module docstring has the order). `react`: only inspect, build and first emails,
    right after Charlie approved something. Raises TownStopped if the Town is stopped."""
    town.killswitch.check()
    cycle = Cycle()
    log_event(town.conn, AGENT, "autopilot_cycle_started", {"react": react})

    def step(name: str, work: Callable[[], str | None]) -> None:
        town.killswitch.check()                       # before every step: a stop ends the cycle here
        try:
            said = work()
        except (TownStopped, KeyboardInterrupt):
            raise
        except SubscriptionLimitReached as err:
            cycle.plan_paused = str(err)
            cycle.skipped.append(f"{name}: plan usage limit ({err})")
            return
        except (OrderRefused, MessengerError, PackageError) as err:
            cycle.skipped.append(f"{name}: {err}")
            return
        except Exception as err:                      # one step failing never stops the others; logged
            log_event(town.conn, AGENT, "autopilot_step_failed", {"step": name,
                                                                  "error": f"{type(err).__name__}: {err}"[:300]})
            cycle.skipped.append(f"{name}: FAILED ({type(err).__name__}: {str(err)[:120]})")
            return
        if said:
            cycle.ran.append(said)

    if not react:
        # a. upkeep and the inbox
        step("preview upkeep", lambda: (f"{n} expired preview(s) taken down"
                                        if (n := previews.upkeep(publisher, town.conn)) else None))
        step("inbox", lambda: _inbox(town, gmail_client))
    # b. inspect
    step("inspect", lambda: _inspect(town, cycle))
    # c. build (plan usage)
    step("build", lambda: _builds(town, cycle))
    # d. outreach packages: the site and the first email as ONE proposal (plan usage; 2026-11-05)
    step("packages", lambda: _packages(town, cycle))
    if not react:
        # e. follow-ups (plan usage), f. Scout, only when the pipeline runs dry and Charlie has no new leads waiting
        step("follow-ups", lambda: _drafts(town, cycle, follow_up=True))
        step("scout", lambda: _scout(town))

    cycle.waiting = waiting_for_charlie(town)
    did = "; ".join(cycle.ran) if cycle.ran else "nothing new to do"
    cycle.line = (f"Autopilot {'right after your decision' if react else 'cycle'}: {did}. "
                  f"Waiting for you: {cycle.waiting} (`town pending`)."
                  + (f" Plan work paused: {cycle.plan_paused}" if cycle.plan_paused else ""))
    log_event(town.conn, AGENT, "autopilot_cycle_finished", {"line": cycle.line, "ran": cycle.ran, "react": react,
                                                            "skipped": cycle.skipped, "waiting": cycle.waiting,
                                                            "plan_paused": cycle.plan_paused or None})
    return cycle


def _inbox(town: Town, client: Any) -> str | None:
    run = clerk.run_inbox(town, client)
    if not run.new:
        return None
    return f"inbox: {run.message.removesuffix(' Nothing was sent.')}"


def _builds(town: Town, cycle: Cycle) -> str | None:
    if cycle.plan_paused:
        return None
    cap = min(int(town.limits.get("autopilot.builds_per_day")), int(town.limits.get("building.sites_per_day")))
    todo = _subjects(town, None, False)                 # build-worthy, not held, not built; oldest lead first
    # A lead whose email address looks invalid (it would bounce) is never built by autopilot: it can't be contacted.
    todo = [s for s in todo if not email_problem(s.lead["public_email"])]
    if not todo:
        return None
    town.cli.check_available()                          # a usage-limit wait in force: no builds until the reset
    done = []
    ordered = sorted(todo, key=lambda s: s.lead_id)
    for n, subject in enumerate(ordered):
        if builds_today(town) >= cap:
            waiting = [s.lead["business_name"] for s in ordered[n:]]
            held(town, cycle, "builder", "inspector", f"Builder: {builds_today(town)} of {cap} builds today; "
                 f"{names(waiting)} {'builds' if len(waiting) == 1 else 'build'} tomorrow",
                 [s.lead_id for s in ordered[n:]])
            break
        town.killswitch.check()
        # after_pass none: the packages step (d) turns a pass into the one proposal; no separate publish here.
        done.append(f"lead #{subject.lead_id}: {_order(town, 'build', lead_id=subject.lead_id, after_pass='none')}")
    return "built " + " | ".join(done) if done else None


def _packages(town: Town, cycle: Cycle) -> str | None:
    """One outreach package per Critic-passed build that never had one (package.ready_builds). A build whose
    package can't be made (the homepage isn't live yet, a first-email gate) is skipped with the reason."""
    if cycle.plan_paused:
        return None
    todo = ready_builds(town)
    if not todo:
        return None
    town.cli.check_available()
    done = []
    for build_id in todo:
        town.killswitch.check()
        try:
            done.append(f"build #{build_id} as #{propose_package(town, build_id)}")
        except PackageError as err:
            cycle.skipped.append(f"package for build #{build_id}: {err}")
    return "packages proposed (site + email): " + " | ".join(done) if done else None


def names(businesses: list[str], most: int = 3) -> str:
    """"Fictional Example Business", "A and B", "A, B, C and 2 more"."""
    shown = [str(b) for b in businesses[:most]]
    rest = len(businesses) - len(shown)
    if rest:
        return ", ".join(shown) + f" and {rest} more"
    return " and ".join(shown) if len(shown) <= 2 else ", ".join(shown[:-1]) + " and " + shown[-1]


def held(town: Town, cycle: Cycle, agent: str, handed_by: str, line: str, lead_ids: list[int]) -> None:
    """A daily cap stopped the chain: said in plain words (the cycle's line, the activity line) and logged as
    `autopilot_held`, whose courier walks from `handed_by` to the Town Hall and stops there."""
    cycle.skipped.append(line)
    log_event(town.conn, AGENT, "autopilot_held", {"agent": agent, "from": handed_by, "line": line,
                                                   "lead_ids": lead_ids})


def _inspect(town: Town, cycle: Cycle) -> str | None:
    waiting = _leads_to_inspect(town, None)
    if not waiting:
        return None
    lim = InspectorLimits.read(town.limits)
    used = inspections_today(town)
    if used >= lim.per_day:
        held(town, cycle, "inspector", "scout", f"Inspector: {used} of {lim.per_day} inspections today; "
             f"{names([w['business_name'] for w in waiting])} {'waits' if len(waiting) == 1 else 'wait'} for tomorrow",
             [w["id"] for w in waiting])
        return None
    return _order(town, "inspect", lead_id="all")


# ---------------- the loop: a full cycle every cycle_minutes, a react cycle right after a decision ----------------

DECIDED = ("action_approved", "action_edited_and_approved")


def last_event_id(conn: Any) -> int:
    return int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0])


def approved_since(conn: Any, after_id: int) -> int:
    """How many approvals Charlie made after event `after_id` (from any window: terminal, Reaper or HQ)."""
    marks = ",".join("?" * len(DECIDED))
    return int(conn.execute(f"SELECT COUNT(*) FROM events WHERE id > ? AND event_type IN ({marks})",
                            (after_id, *DECIDED)).fetchone()[0])


def serve(town: Town, gmail_client: Any, publisher: Any, on_cycle: Callable[[Cycle], None], once: bool = False,
          sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
          max_cycles: int | None = None, should_stop: Callable[[], bool] = lambda: False) -> str:
    """The autopilot loop. Cycles run one at a time, here, in order: never two at once. A full cycle runs now and
    then every autopilot.cycle_minutes (a react cycle never moves that schedule). In between it checks the log
    every few seconds; an approval Charlie made since the last check starts a react cycle within
    autopilot.react_seconds. Returns why it ended ("once", "stopped", or "max cycles" in tests). The kill switch
    ends it at once."""
    minutes = float(town.limits.get("autopilot.cycle_minutes"))
    react_s = float(town.limits.get("autopilot.react_seconds"))
    poll = max(0.5, min(5.0, react_s / 2))          # checks often enough to start well within react_seconds
    cycles, react, next_full = 0, False, clock()
    seen = last_event_id(town.conn)
    while True:
        if town.killswitch.is_stopped():
            return "stopped"
        if should_stop():                       # HQ's Stop button (2026-11-05): the loop ends; nothing is decided
            return "stop requested"
        full_now = clock() >= next_full
        if full_now or react:
            if full_now:
                next_full = clock() + minutes * 60
            write_state(town.settings, True, by=town.actor,
                        next_at=datetime.now(timezone.utc) + timedelta(seconds=max(0.0, next_full - clock())))
            before = last_event_id(town.conn)
            cycle = run_cycle(town, gmail_client, publisher, react=not full_now)
            on_cycle(cycle)
            cycles += 1
            # The cycle's own events aren't decisions; an approval made while it ran still counts next check.
            seen = before if approved_since(town.conn, before) else last_event_id(town.conn)
            react = False
            if once:
                return "once"
            if max_cycles is not None and cycles >= max_cycles:
                return "max cycles"
            continue
        if approved_since(town.conn, seen):
            react = True
            continue
        sleep(poll)


def _drafts(town: Town, cycle: Cycle, follow_up: bool) -> str | None:
    """An email (or follow-up) for every lead the Messenger's own gates allow. Never a call script."""
    if cycle.plan_paused or not follow_up:   # first emails travel in the outreach package now
        return None
    want = FOLLOW_UP_KIND if follow_up else EMAIL
    ready = []
    rejected = rejected_email_leads(town, follow_up)
    for row in town.conn.execute("SELECT id, public_email FROM leads ORDER BY id").fetchall():
        if email_problem(row["public_email"]):   # it would bounce: autopilot never drafts for it
            continue
        if row["id"] in rejected:      # Charlie said no to this one: autopilot doesn't redraft it every cycle
            continue
        try:
            if prepare(town, row["id"], follow_up).kind == want:
                ready.append(row["id"])
        except MessengerError:
            continue
    if not ready:
        return None
    town.cli.check_available()
    done = []
    for lead_id in ready:
        town.killswitch.check()
        done.append(f"lead #{lead_id}: {_order(town, 'draft', lead_id=lead_id, follow_up=follow_up)}")
    return ("follow-ups drafted " if follow_up else "emails drafted ") + " | ".join(done)


def rejected_email_leads(town: Town, follow_up: bool) -> set[int]:
    """Leads whose email of this kind (first or follow-up) Charlie rejected. Autopilot leaves them alone, so a
    rejection isn't undone by the next cycle; `town draft <lead id>` still drafts one when he asks."""
    found = set()
    for row in town.conn.execute("SELECT action_type, content FROM actions WHERE action_type IN ('send_email',"
                                 " 'outreach_package') AND status = 'rejected'"):
        content = json.loads(row["content"])
        content = content.get("email") or {} if row["action_type"] == "outreach_package" else content
        if (content.get("kind") == "follow_up") == follow_up and content.get("lead_id"):
            found.add(int(content["lead_id"]))
    return found


def _scout(town: Town) -> str | None:
    need = int(town.limits.get("autopilot.leads_waiting_min"))
    waiting = leads_waiting_to_build(town)
    pending_leads = int(town.conn.execute("SELECT COUNT(*) FROM actions WHERE action_type = 'add_lead' AND"
                                          " status = 'proposed'").fetchone()[0])
    if waiting >= need or pending_leads:
        return None
    if not Path(town.settings.overture.places_file).exists():
        raise OrderRefused("no Overture data yet (`town overture-download` first)")
    try:
        choice = explore.auto_pick(town)
    except explore.NoUsableCell as err:
        raise OrderRefused(str(err)) from None
    result = _order(town, "scout", business_type=choice.business_type, area=choice.area)
    return f"Scout ({choice.business_type} in {choice.area}, {choice.mode}): {result}"


# ---------------- on/off, for the sit-rep and HQ (read-only there) ----------------

def state_path(settings: Any) -> Path:
    return Path(settings.state_folder) / STATE_FILE


def write_state(settings: Any, running: bool, by: str, next_at: datetime | None = None) -> None:
    path = state_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"running": running, "by": by, "pid": os.getpid(),
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "next_cycle_at": next_at.isoformat(timespec="seconds") if next_at else None}
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data), encoding="utf-8")
    os.replace(temporary, path)


def status(town: Town) -> dict[str, Any]:
    """Autopilot on or off, its last and next cycle, and what's waiting for Charlie. "On" only while the running
    loop keeps its state file fresh (a crashed loop reads as off)."""
    try:
        data = json.loads(state_path(town.settings).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    minutes = float(town.limits.get("autopilot.cycle_minutes"))
    on, next_at = bool(data.get("running")), data.get("next_cycle_at")
    if on and next_at:
        stale = datetime.fromisoformat(next_at) + timedelta(minutes=minutes) < datetime.now(timezone.utc)
        on = not stale
    row = town.conn.execute("SELECT ts, details FROM events WHERE event_type = 'autopilot_cycle_finished'"
                            " ORDER BY id DESC LIMIT 1").fetchone()
    last = {"ts": row["ts"], "line": json.loads(row["details"]).get("line")} if row else None
    return {"on": on, "next_cycle_at": next_at if on else None, "last_cycle": last,
            "waiting_for_you": waiting_for_charlie(town), "cycle_minutes": minutes}


def status_line(town: Town) -> str:
    s = status(town)
    last = f"last cycle {s['last_cycle']['ts']}" if s["last_cycle"] else "no cycle yet"
    nxt = f", next cycle {s['next_cycle_at']}" if s["next_cycle_at"] else ""
    return f"Autopilot: {'on' if s['on'] else 'off'}, {last}{nxt}, waiting for you: {s['waiting_for_you']}."


def shadow_steps_after(town: Town) -> str | None:
    """A cycle's work is logged by the same code as the commands, so it counts toward a shadow day the same way."""
    return golive.record_shadow_day(town.conn, town.settings)


# ---------------- the loop (the terminal command and "go" from HQ or Reaper run the same one) ----------------

def stop_path(settings: Any) -> Path:
    return Path(settings.state_folder) / STOP_FILE


def request_stop(settings: Any, by: str) -> bool:
    """HQ's Stop button: asks the running loop (in any process) to end at its next check. The safe direction:
    it only ends autopilot; the Town keeps running and nothing waiting is decided. True if a loop was on."""
    path = stop_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"by": by, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}),
                    encoding="utf-8")
    return True


def stop_requested(settings: Any) -> bool:
    return stop_path(settings).exists()


def run_loop(town: Town, client: Any, publisher: Any, by: str, once: bool = False,
             on_cycle: Callable[[Cycle], None] = lambda c: None, after_cycle: Callable[[], None] = lambda: None,
             sleep: Callable[[float], None] = time.sleep) -> str:
    """`serve` (a full cycle every cycle_minutes, a react cycle right after each approval) until `once`, the kill
    switch, a stop request (HQ's Stop) or Ctrl+C. Never runs while stopped. Logs on and off, keeps the state file
    (on while it runs, off when it ends), and returns why it ended, in plain words. `town autopilot` and "go" both
    run this, so they are the same loop."""
    stop_path(town.settings).unlink(missing_ok=True)       # an old request never ends a new loop
    log_event(town.conn, by, "autopilot_on", {"once": once, "cycle_minutes": float(town.limits.get("autopilot.cycle_minutes")),
                                              "react_seconds": float(town.limits.get("autopilot.react_seconds"))})

    def each(cycle: Cycle) -> None:
        on_cycle(cycle)
        after_cycle()
    try:
        ended = serve(town, client, publisher, each, once=once, sleep=sleep,
                      should_stop=lambda: stop_requested(town.settings))
    except TownStopped:
        return "the Town was stopped"
    finally:
        write_state(town.settings, False, by)
        stop_path(town.settings).unlink(missing_ok=True)
        log_event(town.conn, by, "autopilot_off", {})
    return {"once": "one cycle done", "stopped": "the Town is stopped",
            "stop requested": "stopped from HQ (Stop button)"}.get(ended, ended)


_background_lock = threading.Lock()
_background: threading.Thread | None = None


def background_running() -> bool:
    return _background is not None and _background.is_alive()


def start_background(open_tools: Callable[[], tuple[Town, Any, Any]], by: str) -> str:
    """"Go" (HQ or Reaper, after Charlie's Confirm): the same loop as `town autopilot`, in a background thread of
    this process, with its own Town. Only one loop at a time: refused if one runs here or in another window."""
    global _background
    with _background_lock:
        if background_running():
            raise OrderRefused("autopilot is already running")
        town, _, _ = open_tools()
        try:
            if town.killswitch.is_stopped():
                raise OrderRefused("the Town is stopped; autopilot never runs while it's stopped (`town resume`)")
            if status(town)["on"]:
                raise OrderRefused("autopilot is already running (in another window)")
        finally:
            town.close()

        def work() -> None:
            # Its own Town, opened in this thread (a database connection belongs to the thread that made it).
            town, client, publisher = open_tools()
            try:
                why = run_loop(town, client, publisher, by, after_cycle=lambda: shadow_steps_after(town))
                log_event(town.conn, AGENT, "autopilot_loop_ended", {"why": why, "by": by})
            except Exception as err:   # logged; the state file already says off (run_loop's finally)
                log_event(town.conn, AGENT, "autopilot_step_failed", {"step": "loop", "error": f"{type(err).__name__}: {err}"[:300]})
            finally:
                town.close()

        _background = threading.Thread(target=work, name="autopilot", daemon=True)
        _background.start()
    minutes = float(town.limits.get("autopilot.cycle_minutes"))
    return (f"Autopilot started: a cycle now, then every {minutes:g} minutes. It approves nothing; packages and "
            "replies wait for you. Stop it with HQ's Stop button, or stand down to halt everything.")
