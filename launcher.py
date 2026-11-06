"""The `town` command. The only file that connects the engine to the website pack.

Every command opens one instance's Town (database, kill switch, Mayor, executor).
The real one (instance.yaml) is the default. `--instance dev` or TOWN_INSTANCE=dev
picks the dev copy (instance.dev.yaml); TOWN_INSTANCE can also be a file path (tests).

Claude Code may only read on the real Town: any other command it runs there is
refused before anything happens (see READ_ONLY_COMMANDS).
"""
import json
import os
import re
import sqlite3
import threading
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Any

import anthropic
import typer
from dotenv import dotenv_values
from playwright.sync_api import Error as PlaywrightError

from engine import decisions, hq, profile, reaper, sitrep
from engine import orders as order_rules
from engine.jarvis import Reaper
from engine.reaper_chat import run_chat
from engine.actor import CHARLIE, CLAUDE_CODE, current_actor
from engine.backup import backup_database, ensure_daily_backup
from engine.budget import PaidWorkPaused, month_start_utc
from engine.claude_cli import SubscriptionLimitReached
from engine.events import log_event
from engine.executor import ExecutorError
from engine.killswitch import KillSwitch, TownStopped
from engine.limits import LimitNotFound, Limits
from engine.llm import MissingAPIKey
from engine.mayor import MayorError
from engine.settings import Settings, load_settings
from engine.signature import CATCHPHRASE
from engine.standdown import STAND_DOWN_REPLY
from engine.town import Town, open_town
from engine.voice import WARM_DELAY_SECONDS, Voice
from packs.websites import overture, pack, previews, testset_runner
from packs.websites.builder import BuilderError, latest_built_workspace, run_builder
from packs.websites.builder_check import serve_folder
from packs.websites.cloudflare import CloudflareClient, CloudflareError, FakeCloudflare, RealCloudflare
from packs.websites import gmail, photo_library
from packs.websites.gmail import FakeGmail, GmailClient, RealGmail
from packs.websites.leads import check_url
from packs.websites.outreach import Outbox
from packs.websites import guard as guard_rules
from packs.websites.critic import CriticError, critique_with_send_backs, latest_review, recent_reviews, review_build
from packs.websites.inspector import InspectorError, run_inspector
from packs.websites.messenger import MessengerError, call_scripts, run_messenger
from packs.websites.scout import ScoutError, run_scout
from packs.websites import autopilot, clerk, explore, golive, librarian, package, playbook_gate
from packs.websites.orders import ORDERS
from packs.websites.townview import VIEW

ROOT = Path(__file__).resolve().parent
# The Gmail sign-in token: the one secret outside .env (it changes on every refresh). secrets/ is gitignored.
GMAIL_TOKEN_FILE = ROOT / "secrets" / "gmail_token.json"
LIMITS_FILE = pack.LIMITS_FILE
MODELS_FILE = pack.MODELS_FILE
# None means the real Anthropic SDK client. Tests swap in a fake so they never call the real API.
API_CLIENT = None

app = typer.Typer(
    help="The Town: an AI-run website business. Nothing leaves without Charlie's approval.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)


REAL = "real"
DEV = "dev"
INSTANCE_FILES = {REAL: ROOT / "instance.yaml", DEV: ROOT / "instance.dev.yaml"}
# The only commands Claude Code may run on the real Town. This is an allowlist, so
# any command added later is refused there until someone adds it here on purpose.
READ_ONLY_COMMANDS = frozenset({"pending", "log", "costs", "status", "version"})
# The database stores UTC; `town log` shows Charlie's time (handles CDT/CST on its own).
DISPLAY_TIMEZONE = ZoneInfo("America/Chicago")


def _instance_file(choice: str | None) -> Path:
    """'real' (or nothing) is instance.yaml, 'dev' is instance.dev.yaml, anything else is a file path."""
    return INSTANCE_FILES.get(choice or REAL, Path(choice or ""))


def _is_real(settings: Settings) -> bool:
    """Fail closed: an instance only counts as dev if it says `instance: dev` AND shares no
    database, state or backup folder with the real one. Anything else is treated as real."""
    if settings.instance != DEV:
        return True
    try:
        real = load_settings(INSTANCE_FILES[REAL])
    except Exception:
        return True
    places = lambda s: {Path(p).resolve() for p in (s.database, s.state_folder, s.backup_folder)}
    return bool(places(settings) & places(real))


def _api_key() -> str | None:
    """Read the key from .env only when a paid call needs it. It's never put in the
    environment, so a `claude -p` subprocess can't pick it up."""
    return dotenv_values(ROOT / ".env").get("ANTHROPIC_API_KEY") or None


def _cloudflare(settings: Settings, instance_file: Path, actor: str) -> CloudflareClient:
    """HARD RULE: only Charlie's real Town, run by Charlie in his own terminal, gets the real Cloudflare
    client. The dev instance, any other instance file (tests use those) and Claude Code all get the fake,
    which records what it would have deployed and never goes online. This fails toward the fake: an
    instance has to be exactly instance.yaml AND say it's real AND be run by Charlie."""
    if (settings.instance == REAL and actor == CHARLIE
            and Path(instance_file).resolve() == INSTANCE_FILES[REAL].resolve()):
        return RealCloudflare(ROOT / ".env", previews.PROJECT)
    return FakeCloudflare(settings.state_folder / "cloudflare-fake")


def _is_real_file(settings: Settings, instance_file: Path) -> bool:
    """Charlie's real instance exactly: instance.yaml, and it says `instance: real`."""
    return settings.instance == REAL and Path(instance_file).resolve() == INSTANCE_FILES[REAL].resolve()


def _gmail(settings: Settings, instance_file: Path, actor: str) -> GmailClient:
    """HARD RULE (the same rule as Cloudflare's): only Charlie's real Town, run by Charlie in his own terminal,
    gets the real Gmail client. The dev instance, any other instance file (tests use those) and Claude Code all
    get the fake, which records each draft in <state>/gmail-fake/ and never reaches Google."""
    if actor == CHARLIE and _is_real_file(settings, instance_file):
        return RealGmail(ROOT / ".env", GMAIL_TOKEN_FILE)
    return FakeGmail(settings.state_folder / "gmail-fake")


def _open(settings: Settings, actor: str, instance_file: Path) -> tuple[Town, previews.Publisher]:
    publisher = previews.Publisher(_cloudflare(settings, instance_file, actor), Limits(LIMITS_FILE),
                                   KillSwitch(settings.killswitch_file), Path(settings.database).parent / "deploys")
    limits = Limits(LIMITS_FILE)
    # The Outbox saves an approved email as a Gmail draft; it SENDS only while every go-live condition holds
    # (packs/websites/golive.py: Charlie's switch, the mailing address, the shadow days), checked at each send.
    outbox = Outbox(_gmail(settings, instance_file, actor), KillSwitch(settings.killswitch_file), limits,
                    settings.mailing_address, live_problems=lambda conn: golive.live_problems(conn, settings, limits))
    town = open_town(settings, pack.PACK_SCHEMAS, {**pack.handlers(outbox), **pack.publish_handlers(publisher, outbox)}, LIMITS_FILE, MODELS_FILE,
                     api_key_loader=_api_key, api_client=API_CLIENT, actor=actor,
                     live_external_types=pack.LIVE_EXTERNAL_TYPES)
    pack.upgrade(town.conn)  # adds any column a newer version of the pack expects
    # The Guard checks every proposal (and again at approval and execution). Unknown instance = real (fail closed).
    town.mayor.guard = town.executor.guard = guard_rules.WebsiteGuard(
        town.limits, town.budget, town.llm, settings.mailing_address, real=_is_real(settings))
    # A package's rejection must say which part (site, email or both): plain code, every path (terminal, Reaper, HQ).
    town.mayor.reject_rule = package.reject_reason_problem
    # send_email is live only while go-live says so, asked at the moment each action runs (off = a Gmail draft).
    town.executor.live_now = lambda: golive.live_types(town.conn, settings, town.limits)
    return town, publisher


def _refuse_claude_code(settings: Settings, command: str | None, instance_file: Path) -> None:
    """Claude Code tried a decision on the real Town. Log it, refuse it, change nothing."""
    try:
        town, _ = _open(settings, CLAUDE_CODE, instance_file)
        try:
            log_event(town.conn, CLAUDE_CODE, "command_refused",
                      {"command": command, "instance": settings.instance,
                       "why": "Claude Code may only run read-only commands on the real Town"})
        finally:
            town.close()
    except Exception as err:  # the refusal stands even if the database can't record it
        reaper.warn(f"Refusal not logged (database unavailable: {err}).")
    _fail(f"Negative. `town {command}` is Charlie's call on the real Town. Claude Code may only read here "
          f"({', '.join(sorted(READ_ONLY_COMMANDS - {'version'}))}). Test it with `--instance dev`.")


@app.callback()
def main(
    ctx: typer.Context,
    instance: str = typer.Option(None, "--instance", envvar="TOWN_INSTANCE",
                                 help="Which Town: 'dev' for testing. The real one is the default."),
) -> None:
    """Runs before every `town` command: works out who's asking, then opens the chosen instance's Town."""
    command = ctx.invoked_subcommand
    actor = current_actor()
    instance_file = _instance_file(instance)
    settings = load_settings(instance_file)
    real = _is_real(settings)
    if actor == CLAUDE_CODE and real and command not in READ_ONLY_COMMANDS:
        _refuse_claude_code(settings, command, instance_file)
    if command == "stop":
        # The emergency stop lands first, before anything that could fail on the database.
        KillSwitch(settings.killswitch_file).engage("town stop", by=actor)
    try:
        town, publisher = _open(settings, actor, instance_file)
    except Exception as err:
        if command == "stop":
            reaper.alert("Standing down. All units halted. (Database unavailable, so the stop is recorded "
                         f"in the kill-switch file only: {err})")
            raise typer.Exit(code=0)
        raise
    ctx.obj = town
    ctx.meta["publisher"] = publisher
    ctx.meta["instance_file"] = instance_file
    ctx.call_on_close(town.close)
    if actor == CLAUDE_CODE:
        # Every command Claude Code runs shows up in the log under its own name.
        log_event(town.conn, CLAUDE_CODE, "claude_code_command", {"command": command, "instance": settings.instance})
        if real:
            return  # read-only means read-only: no backup or takedown either; Charlie's next command does them
    _daily_backup(town)
    if command not in ("stop", "version"):
        _preview_upkeep(town, publisher)
        ctx.call_on_close(lambda: _shadow_upkeep(town))   # runs before the Town closes (last in, first out)


def _shadow_upkeep(town: Town) -> None:
    """After each command, while shadow mode is on: log today as a shadow day once the full loop ran in dry-run.
    Plain code, nothing outside; never while the Town is stopped. A failure never blocks Charlie's command."""
    if town.killswitch.is_stopped():
        return
    try:
        day = golive.record_shadow_day(town.conn, town.settings)
    except Exception as err:
        log_event(town.conn, "mayor", "shadow_upkeep_failed", {"error": f"{type(err).__name__}: {err}"[:300]})
        return
    if day:
        reaper.report(f"Shadow day logged: {day} ({len(golive.shadow_days(town.conn))} of "
                      f"{town.limits.get('golive.shadow_days_required')}). Review today's drafts in Gmail.")


def _preview_upkeep(town: Town, publisher: previews.Publisher) -> None:
    """Whenever the Town starts or runs: expired previews come down (never while stopped). A failure
    never blocks Charlie's command; it's logged and tried again with the next one."""
    try:
        previews.upkeep(publisher, town.conn, on_note=reaper.report)
    except Exception as err:
        log_event(town.conn, previews.AGENT, "preview_upkeep_failed", {"error": f"{type(err).__name__}: {err}"[:300]})
        reaper.warn(f"Preview upkeep FAILED: {err}. It runs again with the next command.")


def _publisher(ctx: typer.Context) -> previews.Publisher:
    return ctx.meta["publisher"]


PRIVATE_TOKEN = re.compile(r"\b[0-9a-f]{64}\b")


def _shown(town: Town, text: str) -> str:
    """Preview paths are private. When Claude Code reads the queue or the log, they're hidden."""
    return PRIVATE_TOKEN.sub("<private>", text) if town.actor == CLAUDE_CODE else text


def _daily_backup(town: Town) -> None:
    """The first `town` command each day backs up the database. Skipped while
    the Town is stopped (nothing automatic runs then); `town backup` still works."""
    if town.killswitch.is_stopped():
        return
    try:
        made = ensure_daily_backup(town.conn, town.settings.backup_folder, int(town.limits.get("backups.keep_last")))
    except Exception as err:  # a failed backup must be loud, but it never blocks Charlie's command
        log_event(town.conn, "librarian", "backup_failed", {"error": f"{type(err).__name__}: {err}"})
        reaper.warn(f"Daily backup FAILED: {err}. Run `town backup` to retry.")
        return
    if made:
        reaper.report(f"Daily backup secured: {made.name}")


def _town(ctx: typer.Context) -> Town:
    return ctx.obj


def _fail(message: str) -> None:
    reaper.alert(message)
    raise typer.Exit(code=1)


def _run_now(town: Town, action_id: int) -> None:
    """After Charlie approves, run the action right away (unless the Town is stopped)."""
    if town.killswitch.is_stopped():
        reaper.warn(f"Town stopped. #{action_id} approved and held. It runs after `town resume`, then `town run`.")
        return
    try:
        outcome = town.executor.execute(action_id)
    except ExecutorError as err:
        _fail(f"#{action_id} not executed: {err}")
        return
    if outcome.status == "executed":
        reaper.report(f"#{action_id} executed. {outcome.result}")
    else:
        reaper.alert(f"#{action_id} failed. {outcome.result}")


# ---------------- the approval queue ----------------

@app.command()
def pending(ctx: typer.Context) -> None:
    """Show every proposal waiting for your decision."""
    town = _town(ctx)
    rows = town.mayor.list_pending()
    if not rows:
        reaper.report("Queue empty. No proposals awaiting your order.")
        return
    reaper.report(f"{len(rows)} proposal(s) awaiting your order.")
    for row in rows:
        reaper.plain()
        reaper.plain(f"#{row['id']}  {row['agent']} -> {row['action_type']}  |  {row['target']}")
        reaper.plain(f"    Why:     {row['reason']}")
        if row["action_type"] == "add_lead":   # confirm it really has no website before approving, in one click
            lead = json.loads(row["content"])
            reaper.plain(f"    Check:   {lead.get('check_url') or check_url(lead.get('business_name'), lead.get('area'))}")
        if row["action_type"] in ("outreach_package", "publish_preview", "publish_homepage"):
            from packs.websites.townview import show_command
            content = json.loads(row["content"])
            show = show_command(town.conn, content.get("lead_id"), content.get("build_id"))
            if show:
                reaper.plain(f"    View:    {show}   (or the View button in `town hq`)")
        verdict = town.mayor.guard_recheck(row)   # the hard rules as of now (read-only), plus its saved flags
        line = f"    Cost:    ${row['cost_estimate_usd']:.4f}    Guard: {verdict.summary() if verdict else 'not checked'}"
        (reaper.warn if verdict and verdict.blocked else reaper.plain)(
            line + ("  (can't be approved: fix it with `town edit`, or reject it)" if verdict and verdict.blocked else ""))
        reaper.plain(_shown(town, f"    Content: {json.dumps(json.loads(row['content']), indent=None)}"))
    reaper.plain()
    reaper.plain("Decide with: town approve <id> | town reject <id> --reason \"...\" | town edit <id>")


@app.command()
def approve(ctx: typer.Context, action_id: int) -> None:
    """Approve a proposal. It runs once, right away (internal actions for real, outside ones as a dry run)."""
    town = _town(ctx)
    try:
        town.mayor.approve(action_id)
    except MayorError as err:
        _fail(str(err))
    reaper.report(f"#{action_id} approved.")
    _run_now(town, action_id)


@app.command()
def reject(
    ctx: typer.Context,
    action_id: int,
    reason: str = typer.Option("", "--reason", "-r", help="Why. Required: it becomes feedback."),
    part: str = typer.Option("", "--part", help="For an outreach package: which part you reject (site, email or "
                                               "both). Required for a package."),
) -> None:
    """Reject a proposal. A reason is required (and, for an outreach package, which part: --part site|email|both)."""
    if part:
        try:
            reason = package.with_part(part, reason) if reason.strip() else ""
        except package.PackageError as err:
            _fail(str(err))
    try:
        _town(ctx).mayor.reject(action_id, reason)
    except MayorError as err:
        _fail(str(err))
    reaper.report(f"#{action_id} rejected. Reason logged as feedback.")


def _parse_value(text: str) -> Any:
    """'5' becomes 5, 'true' becomes True, anything that isn't JSON stays text."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


@app.command()
def edit(
    ctx: typer.Context,
    action_id: int,
    set_fields: list[str] = typer.Option(
        None, "--set", help="Change one field: --set phone=314-555-0100 (repeatable). "
                            "Without --set, the content opens in your text editor."),
) -> None:
    """Edit a proposal's content, then approve it. The original and your edit are both saved."""
    town = _town(ctx)
    try:
        row = town.mayor.get(action_id)
    except MayorError as err:
        _fail(str(err))
    is_package = row["action_type"] == package.PACKAGE
    if row["action_type"] in pack.LIVE_EXTERNAL_TYPES and not is_package:
        _fail(f"#{action_id} is a publish: its path, build and files are fixed by the Critic's pass, so it can't "
              f"be edited. Reject it with a reason and propose it again.")
    original = json.loads(row["content"])
    if is_package:
        # Only the email part (subject, body) can change; the site part is fixed by the Critic's pass.
        edited = original
        try:
            if set_fields:
                for item in set_fields:
                    if "=" not in item:
                        _fail(f"--set needs field=value, got '{item}'")
                    key, value = item.split("=", 1)
                    edited = package.edit_email(edited, key.strip(), value)
            else:
                part = {k: original["email"][k] for k in package.EDITABLE_EMAIL_FIELDS}
                text = typer.edit(json.dumps(part, indent=2), extension=".json")
                if text is None:
                    reaper.report(f"No changes saved. #{action_id} still awaiting your order.")
                    return
                for key, value in json.loads(text).items():
                    edited = package.edit_email(edited, key, value)
        except (package.PackageError, json.JSONDecodeError, AttributeError) as err:
            _fail(f"{err}. Nothing changed.")
    elif set_fields:
        edited = dict(original)
        for item in set_fields:
            if "=" not in item:
                _fail(f"--set needs field=value, got '{item}'")
            key, value = item.split("=", 1)
            edited[key.strip()] = _parse_value(value)
    else:
        text = typer.edit(json.dumps(original, indent=2), extension=".json")
        if text is None:
            reaper.report(f"No changes saved. #{action_id} still awaiting your order.")
            return
        try:
            edited = json.loads(text)
        except json.JSONDecodeError as err:
            _fail(f"That isn't valid JSON ({err}). Nothing changed.")
    if edited == original:
        reaper.report(f"No changes made. Use `town approve {action_id}` to approve it as is.")
        return
    try:
        town.mayor.edit_and_approve(action_id, edited)
    except MayorError as err:
        _fail(str(err))
    reaper.report(f"#{action_id} edited and approved. Original kept as feedback.")
    _run_now(town, action_id)


@app.command()
def run(ctx: typer.Context) -> None:
    """Run approved actions that are still waiting (for example, ones approved while stopped)."""
    town = _town(ctx)
    if town.killswitch.is_stopped():
        _fail("Town stopped. Nothing executes until `town resume`.")
    ids = [r["id"] for r in town.conn.execute("SELECT id FROM actions WHERE status = 'approved' ORDER BY id")]
    if not ids:
        reaper.report("Nothing approved is waiting to run.")
    for action_id in ids:
        _run_now(town, action_id)


# ---------------- the do-not-contact list (the Guard checks it before every draft and send) ----------------

dnc_app = typer.Typer(help="The do-not-contact list: nobody on it is emailed, scripted, previewed or added as a lead.",
                      no_args_is_help=True)
app.add_typer(dnc_app, name="dnc")


@dnc_app.command("add")
def dnc_add(
    ctx: typer.Context,
    value: str = typer.Argument(..., help="An email, a phone number, or a business name."),
    reason: str = typer.Option(..., "--reason", "-r", help="Why (required; kept with the entry)."),
) -> None:
    """Add someone to the do-not-contact list. An email or phone also blocks the businesses that use it."""
    town = _town(ctx)
    try:
        added = guard_rules.dnc_add(town.conn, value, reason, by=town.actor)
    except guard_rules.GuardError as err:
        _fail(str(err))
        return
    if not added:
        reaper.report(f"{value} was already on the do-not-contact list.")
    for entry in added:
        reaper.report(f"Do not contact: {entry['shown']} ({entry['kind']}).")


@dnc_app.command("list")
def dnc_list(ctx: typer.Context) -> None:
    """Everyone on the do-not-contact list."""
    entries = guard_rules.dnc_list(_town(ctx).conn)
    if not entries:
        reaper.report("The do-not-contact list is empty.")
    for e in entries:
        linked = f" (linked to entry #{e['linked_from']})" if e["linked_from"] else ""
        reaper.plain(f"#{e['id']:<4} {e['kind']:<9} {e['shown']}  |  {e['reason']}  |  added by {e['added_by']} "
                     f"{chicago_time(e['created_at'])}{linked}")


@dnc_app.command("remove")
def dnc_remove(
    ctx: typer.Context,
    value: str = typer.Argument(..., help="The email, phone or business name to take off the list."),
    reason: str = typer.Option(..., "--reason", "-r", help="Why (required; an opt-out must always be honored)."),
) -> None:
    """Take someone off the do-not-contact list (your decision; never for someone who opted out)."""
    town = _town(ctx)
    try:
        removed = guard_rules.dnc_remove(town.conn, value, reason, by=town.actor)
    except guard_rules.GuardError as err:
        _fail(str(err))
        return
    reaper.report(f"{removed} entr{'y' if removed == 1 else 'ies'} taken off the do-not-contact list. Logged.")


# ---------------- the log ----------------

@app.command()
def log(
    ctx: typer.Context,
    limit: int = typer.Option(30, "--limit", "-n", help="How many events to show."),
    full: bool = typer.Option(False, "--full", help="Show full event details."),
) -> None:
    """Show the most recent events: proposals, decisions, feedback, executions, costs."""
    town = _town(ctx)
    rows = town.conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    if not rows:
        reaper.report("Log empty.")
        return
    for row in reversed(rows):
        details = _shown(town, row["details"])
        if not full and len(details) > 110:
            details = details[:107] + "..."
        action = f" #{row['action_id']}" if row["action_id"] else ""
        reaper.plain(f"{chicago_time(row['ts'])}  {row['agent']:<9} {row['event_type']}{action}  {details}")


def chicago_time(utc_ts: str) -> str:
    """'2026-10-19T15:44:50Z' (how the database stores it) -> '2026-10-19 10:44:50 AM CDT'.
    Only the display changes; anything that isn't a timestamp is shown as it is."""
    try:
        moment = datetime.strptime(utc_ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return str(utc_ts)
    return moment.astimezone(DISPLAY_TIMEZONE).strftime("%Y-%m-%d %I:%M:%S %p %Z")


# ---------------- the kill switch ----------------

@app.command()
def stop(
    ctx: typer.Context,
    reason: str = typer.Option("", "--reason", "-r", help="Optional note for the log."),
) -> None:
    """EMERGENCY STOP. Halts every agent; nothing new starts and nothing executes."""
    town = _town(ctx)
    town.killswitch.stop(town.conn, reason, by=town.actor)
    reaper.alert("Standing down. All units halted. Awaiting your order to resume.")


@app.command()
def resume(ctx: typer.Context) -> None:
    """Restart the Town after `town stop`."""
    town = _town(ctx)
    town.killswitch.resume(town.conn, by=town.actor)
    waiting = town.conn.execute("SELECT COUNT(*) FROM actions WHERE status = 'approved'").fetchone()[0]
    reaper.report("Systems online. Resuming operations.")
    _preview_upkeep(town, _publisher(ctx))   # anything that expired while stopped comes down now
    if waiting:
        reaper.report(f"{waiting} approved action(s) held during the stop. Run them with `town run`.")


@app.command()
def status(ctx: typer.Context) -> None:
    """One-line status: running or stopped, and what's waiting."""
    town = _town(ctx)
    count = lambda s: town.conn.execute("SELECT COUNT(*) FROM actions WHERE status = ?", (s,)).fetchone()[0]
    state = "STOPPED" if town.killswitch.is_stopped() else "RUNNING"
    reaper.report(f"Town {state} ({town.settings.instance} instance). Pending: {count('proposed')}. "
                  f"Approved, not yet run: {count('approved')}.")
    live = previews.live_previews(town.conn)
    home = [p for p in live if p["kind"] == "homepage"]
    shown = [p for p in live if p["kind"] == "preview"]
    next_expiry = f" (next expiry {chicago_time(min(p['expires_at'] for p in shown))})" if shown else ""
    reaper.plain(f"Homepage: {'live (build #' + str(home[0]['build_id']) + ')' if home else 'not live yet'}. "
                 f"Previews live: {len(shown)}{next_expiry}. Links: `town previews`.")
    reviews = recent_reviews(town.conn, 3)
    if reviews:
        reaper.plain("Latest Critic reviews:")
        for r in reviews:
            _review_lines(r, indent="  ", reasons=2)


# ---------------- money and limits ----------------

@app.command()
def costs(ctx: typer.Context) -> None:
    """This month's spend against both caps, by agent and model."""
    town = _town(ctx)
    try:
        api, total = town.budget.month_spend()
        _, shared_total = town.budget.shared_spend()
        api_cap, total_cap = town.budget.caps()
        paused = town.budget.paused_reason()
    except Exception as err:
        _fail(f"Couldn't read this month's spend ({err}). Paid work counts as paused until it can.")
        return
    reaper.report(f"This month (UTC): API ${api:.4f} of ${api_cap:.2f} ({api / api_cap:.0%}). "
                  f"Total ${total:.4f} of ${total_cap:.2f} ({total / total_cap:.0%}).")
    if shared_total:
        reaper.plain(f"  Includes ${shared_total:.4f} spent by the other instance (same API key, same caps).")
    if paused:
        reaper.warn(f"Paid work PAUSED: {paused}. Decision waiting in `town pending`.")
    rows = town.conn.execute(
        "SELECT agent, model, service, COUNT(*) AS calls, SUM(dollar_cost) AS dollars FROM costs"
        " WHERE ts >= ? GROUP BY agent, model, service ORDER BY dollars DESC", (month_start_utc(),)).fetchall()
    for row in rows:
        reaper.plain(f"  {row['agent']:<10} {row['model']:<20} {row['service']:<11} "
                     f"{row['calls']:>4} calls  ${row['dollars']:.4f}")
    if not rows:
        reaper.plain("  No paid calls yet this month.")


@app.command("limit")
def limit_change(
    ctx: typer.Context,
    key: str = typer.Argument(..., help="The limit, e.g. leads.new_leads_per_day"),
    value: str = typer.Argument(..., help="The new value"),
    reason: str = typer.Option(..., "--reason", "-r", help="Why the change."),
) -> None:
    """Propose a change to limits.yaml. It only takes effect after you approve it."""
    town = _town(ctx)
    try:
        old = town.limits.get(key)
        action_id = town.mayor.propose(town.actor, "change_limit", f"limits.yaml: {key}",
                                       {"limit": key, "current_value": old, "new_value": _parse_value(value)},
                                       reason)
    except LimitNotFound:
        _fail(f"No limit called '{key}' in limits.yaml.")
        return
    except MayorError as err:
        _fail(str(err))
        return
    reaper.report(f"Limit change proposed as #{action_id} ({key}: {old} -> {value}). "
                  f"Approve with `town approve {action_id}`.")


@app.command("api-check")
def api_check(ctx: typer.Context) -> None:
    """Make ONE tiny real Haiku call to confirm the API key works (a fraction of a cent, logged)."""
    town = _town(ctx)
    try:
        reply = town.llm.ask("mayor", "You are a status check. Reply with exactly: OK", "Status check.",
                             max_tokens=256, effort="low")
    except (TownStopped, PaidWorkPaused, MissingAPIKey) as err:
        _fail(str(err))
        return
    except anthropic.APIError as err:
        _fail(f"API call failed: {err}")
        return
    reaper.report(f"API online. {reply.model} replied '{reply.text.strip()}'. Cost ${reply.cost_usd:.6f}, logged.")


# ---------------- agents ----------------

@app.command()
def scout(
    ctx: typer.Context,
    business_type: str = typer.Option(None, "--type", "-t", help="e.g. salon, restaurant, auto, plumber"),
    area: str = typer.Option("any", "--area", "-a", help="A town (Clayton), a ZIP (63105), or 'any'"),
    auto: bool = typer.Option(False, "--auto", help="Let the Town pick the type and area: about 70% proven winners, "
                              "30% new ground (explore.* in limits.yaml); the reason is logged."),
) -> None:
    """Scout finds about 5 local businesses and saves the ones that pass every plain-code check as leads (one small
    Haiku call). The rest are never saved (held: no public email; or dropped), each with its reasons in the log."""
    town = _town(ctx)
    if auto == bool(business_type):
        _fail("Give either --type (and --area), or --auto.")
        return
    if auto:
        try:
            choice = explore.auto_pick(town)
        except explore.NoUsableCell as err:
            _fail(f"Scout stands down: {err}.")
            return
        reaper.report(f"Auto pick: {choice.business_type} in {choice.area} ({choice.mode}). Why: {choice.why}")
        business_type, area = choice.business_type, choice.area
    try:
        result = run_scout(town, business_type, area)
    except (TownStopped, PaidWorkPaused, MissingAPIKey, ScoutError, MayorError) as err:
        _fail(str(err))
        return
    except anthropic.APIError as err:
        _fail(f"Scout's API call failed: {err}")
        return
    reaper.report(result.message)
    for pick in result.not_saved:
        reaper.plain(f"    not saved ({pick['fate']}): {pick['business_name']}: {'; '.join(pick['why_not'])}",
                     soft_wrap=True)
    if result.model:
        reaper.report(f"Cost ${result.cost_usd:.5f} on {result.model}. Saved leads move on by themselves "
                      "(inspect, build, then one package for you).")


@app.command()
def inspect(
    ctx: typer.Context,
    lead_id: int = typer.Argument(None, help="One saved lead's id. Leave out to inspect every lead not yet inspected."),
) -> None:
    """Inspector grades saved leads' websites (free measuring, one small Haiku note per lead, logged)."""
    town = _town(ctx)
    try:
        result = run_inspector(town, lead_id)
    except (TownStopped, PaidWorkPaused, MissingAPIKey, InspectorError) as err:
        _fail(str(err))
        return
    except anthropic.APIError as err:
        _fail(f"Inspector's API call failed: {err}")
        return
    except PlaywrightError as err:
        _fail(f"The browser failed ({str(err).splitlines()[0]}). If it's missing, run "
              "`python -m playwright install chromium`.")
        return
    for r in result.inspected:
        reaper.plain()
        if r["status"] == "failed":
            reaper.warn(f"Lead #{r['lead_id']} {r['business_name']}: couldn't inspect {r['website']} ({r['error']}). "
                        f"Retry with `town inspect {r['lead_id']}`.")
            continue
        worthy = "BUILD-WORTHY" if r["build_worthy"] else "not build-worthy"
        reaper.plain(f"Lead #{r['lead_id']} {r['business_name']}  |  {r['website'] or 'no website'}")
        reaper.plain(f"    Need:     {r['need_score']}/100 ({worthy})   Problems: {', '.join(r['problems']) or 'none'}")
        reaper.plain(f"    Why:      {r['note']}")
        if r["screenshots"]:
            reaper.plain(f"    Pictures: {r['screenshots'].get('desktop')} (+ mobile)")
    reaper.plain()
    reaper.report(result.message + (f" Cost ${result.cost_usd:.5f}, logged." if result.inspected else ""))


@app.command()
def build(
    ctx: typer.Context,
    lead_id: int = typer.Argument(None, help="One build-worthy lead's id. Leave out to build every one not yet built."),
    again: bool = typer.Option(False, "--again", help="Build a new site even if this lead already has one."),
    homepage: bool = typer.Option(False, "--homepage", help="Build Example Web Studio's own homepage "
                                  "(from packs/websites/archlight_home.yaml)."),
) -> None:
    """Builder makes a sample site in a private folder (Opus via `claude -p` on your plan; publishes nothing)."""
    town = _town(ctx)
    if homepage and lead_id is not None:
        _fail("Use either a lead id or --homepage, not both.")
    try:
        result = run_builder(town, lead_id, again, homepage=homepage, on_progress=reaper.plain)
    except SubscriptionLimitReached as err:
        _fail(f"{err} Nothing was retried.")
        return
    except (TownStopped, BuilderError) as err:
        _fail(str(err))
        return
    for b in result.builds:
        _report_build(b)
        if not b["facts_check"]:
            continue   # the site never compiled: nothing for the Critic to look at
        try:
            run = critique_with_send_backs(town, b["id"], on_progress=reaper.plain)
        except SubscriptionLimitReached as err:
            _fail(f"{err} The Critic review was saved as unfinished; nothing was retried.")
            return
        except (TownStopped, BuilderError, CriticError) as err:
            _fail(str(err))
            return
        _report_critic_run(run)
        if run.outcome == "passed":
            _auto_propose(town, run.final_build_id)
    reaper.report(result.message + " Nothing was published (publishing waits for your approval).")


def _auto_propose(town: Town, build_id: int) -> None:
    """After the Critic passes a build: a lead's site becomes ONE outreach package (the site and the first email,
    Charlie 2026-11-05); the homepage is proposed for publishing as before."""
    from packs.websites.orders import after_pass_proposal
    try:
        line = after_pass_proposal(town, build_id, "package")
    except SubscriptionLimitReached as err:
        reaper.warn(f"Not proposed: {err} Nothing was retried; `town package {build_id}` after the reset.")
        return
    (reaper.warn if line.startswith("Not proposed") else reaper.report)(line + " Decide with `town pending`.")


@app.command("package")
def package_command(ctx: typer.Context, build_id: int = typer.Argument(..., help="A lead's Critic-passed build.")) -> None:
    """The outreach package by hand: the Messenger writes the first email against the build's private link, and the
    site and the email go to you as ONE proposal (Sonnet on your plan). Publishes and sends nothing."""
    town = _town(ctx)
    try:
        action_id = package.propose_package(town, build_id, on_progress=reaper.plain)
    except SubscriptionLimitReached as err:
        _fail(f"{err} Nothing was retried.")
        return
    except (package.PackageError, previews.PublishError, MayorError, TownStopped) as err:
        _fail(str(err))
        return
    row = town.mayor.get(action_id)
    verdict = town.mayor.guard_recheck(row)
    reaper.report(f"Outreach package #{action_id} proposed for build #{build_id} (site + first email). "
                  f"Guard: {verdict.summary() if verdict else 'not checked'}. Decide with `town pending`.")


def _report_critic_run(run: Any) -> None:
    for review in run.reviews:
        _review_lines(review)
    for rebuilt in run.send_backs:
        _report_build(rebuilt)
    if run.outcome == "passed":
        reaper.report(f"Critic PASSED build #{run.final_build_id} after {len(run.send_backs)} send-back(s).")
    elif run.outcome == "critic_failed":
        reaper.warn(f"Build #{run.final_build_id} is CRITIC_FAILED after {len(run.send_backs)} send-back(s). "
                    f"It will not be proposed.")
    else:
        reaper.warn(f"A Critic review of build #{run.final_build_id} didn't finish. Run `town critique "
                    f"{run.final_build_id}` again.")


def _review_lines(r: dict[str, Any], indent: str = "    ", reasons: int = 12) -> None:
    """One review, for `town critique`, `town build`, `town show` and `town status`. Works on a saved
    row (JSON text) or a fresh record (lists and dicts)."""
    load = lambda v: json.loads(v) if isinstance(v, str) else (v or [])
    scores = load(r.get("scores")) or {}
    tokens = r.get("tokens") or {"output_tokens": r.get("output_tokens") or 0,
                                 "cache_read_input_tokens": r.get("cache_read_tokens") or 0}
    who = f"build #{r['build_id']}" if r.get("build_id") else f"test fixture {r.get('testset_case')}"
    line = (f"{indent}Review #{r.get('id')} of {who}: {r['verdict'].upper()} (decided by {r['decided_by']})"
            + (f"  scores: " + ", ".join(f"{k} {v['score']}" for k, v in scores.items()) if scores else ""))
    (reaper.plain if r["verdict"] == "pass" else reaper.warn)(line)
    if r.get("error"):
        reaper.plain(f"{indent}  Didn't finish: {r['error'][:200]}")
    for item in load(r.get("must_fix"))[:reasons]:
        reaper.plain(f"{indent}  - {item}")
    if r.get("summary"):
        reaper.plain(f"{indent}  Summary: {r['summary']}")
    if r.get("model"):
        reaper.plain(f"{indent}  {r['wall_seconds']:.0f}s total, {r['claude_seconds']:.0f}s in `claude -p`, "
                     f"{r['turns']} turns, {tokens['output_tokens']:,} output tokens, "
                     f"{tokens['cache_read_input_tokens']:,} cache read, API-price equivalent "
                     f"${r['api_equivalent_usd']:.2f} (not billed)")


@app.command()
def critique(
    ctx: typer.Context,
    build_id: int = typer.Argument(..., help="The build to review."),
    opus_anyway: bool = typer.Option(False, "--opus-anyway", help="Dev only: ask Opus even if a hard check "
                                     "failed, to compare its judgment. The verdict stays FAIL."),
) -> None:
    """The Critic reviews one build (Opus via `claude -p` on your plan, read-only). No send-backs."""
    town = _town(ctx)
    if opus_anyway and town.settings.instance != "dev":
        _fail("--opus-anyway is for the dev instance only.")
    try:
        review = review_build(town, build_id, opus_anyway=opus_anyway, on_progress=reaper.plain)
    except SubscriptionLimitReached as err:
        _fail(f"{err} Nothing was retried.")
        return
    except (TownStopped, CriticError) as err:
        _fail(str(err))
        return
    _review_lines(review, indent="")
    reaper.report(f"Critic {review['verdict'].upper()} for build #{build_id}. Nothing was published.")


@app.command()
def photos(ctx: typer.Context) -> None:
    """Checks the sample-photo library (packs/websites/photo_library/): which photos are usable, and why not.
    Reads files only; changes nothing."""
    town = _town(ctx)
    max_kb = float(town.limits.get("building.max_sample_photo_kb"))
    check = photo_library.library_check(max_kb=max_kb)
    for folder, usable in check["usable"].items():
        reaper.plain(f"  {folder:12} {len(usable)} usable")
    for problem in check["problems"]:
        reaper.warn(f"  {problem}")
    if check["waiting"]:
        reaper.plain(f"  {len(check['waiting'])} listed in LICENSES.md but not downloaded yet "
                     f"(see packs/websites/photo_library/README.md).")
    total = sum(len(v) for v in check["usable"].values())
    reaper.report(f"{total} licensed sample photo(s) ready. A photo without a complete LICENSES.md row is never used.")


# ---------------- the Librarian (lessons, playbooks, the explore split) ----------------

librarian_app = typer.Typer(help="The Librarian: lessons from your decisions, playbook changes gated by the test set.",
                            no_args_is_help=True)
app.add_typer(librarian_app, name="librarian")


@librarian_app.command("review")
def librarian_review(ctx: typer.Context) -> None:
    """Reads the log and your feedback, and PROPOSES lessons with their evidence (the librarian model via
    `claude -p` on your plan, read-only). Each lesson waits in `town pending` for your OK."""
    town = _town(ctx)
    try:
        run = librarian.run_review(town, on_progress=reaper.plain)
    except SubscriptionLimitReached as err:
        _fail(f"{err} Nothing was retried.")
        return
    except (TownStopped, librarian.LibrarianError, MayorError) as err:
        _fail(str(err))
        return
    if run.error:
        _fail(f"The review didn't finish ({run.error}). Nothing was proposed.")
        return
    for lesson_id in run.proposed:
        row = town.conn.execute("SELECT * FROM lessons WHERE id = ?", (lesson_id,)).fetchone()
        ev = json.loads(row["evidence"])
        reaper.plain(f"  Lesson #{lesson_id} ({row['agent']}): {row['text']}  [{ev['count']} of {ev['out_of']}: "
                     f"{', '.join(ev['ids'][:8])}] -> #{row['action_id']} in the queue", soft_wrap=True)
    for refused in run.refused:
        reaper.warn(f"  Refused by plain code: {refused.get('text') or refused.get('retire')}: "
                    f"{'; '.join(refused['why'])}")
    if run.summary:
        reaper.plain(f"  Summary: {run.summary}", soft_wrap=True)
    reaper.report(f"{len(run.proposed)} lesson(s) and {len(run.retirements)} retirement(s) proposed. "
                  f"Decide with `town pending`.")


@librarian_app.command("lessons")
def librarian_lessons(ctx: typer.Context) -> None:
    """Every lesson: proposed, approved, rejected (with your reason) and retired, each with its evidence."""
    town = _town(ctx)
    rows = librarian.lessons(town.conn)
    if not rows:
        reaper.report("No lessons yet. `town librarian review` proposes them from your decisions.")
        return
    for r in rows:
        ev = r["evidence"]
        extra = f" (your reason: {r['reject_reason']})" if r["status"] == "rejected" and r["reject_reason"] else ""
        reaper.plain(f"#{r['id']} {r['status'].upper():9} {r['agent']:10} {r['text']}  [{ev.get('count')} of "
                     f"{ev.get('out_of')}: {', '.join(ev.get('ids', [])[:8])}]{extra}", soft_wrap=True)


@librarian_app.command("playbook")
def librarian_playbook(
    ctx: typer.Context,
    lesson_id: int = typer.Argument(..., help="An approved lesson (to add) or a retired one (to take out)."),
    text: str = typer.Option(None, "--text", help="Your own wording for the playbook line."),
) -> None:
    """Proposes the playbook change for a lesson, after running that agent's test set with and without it. A change
    that lowers the score is blocked by the Guard. Uses the test set's own costs (API or plan)."""
    town = _town(ctx)
    try:
        action_id = playbook_gate.propose_playbook_change(town, lesson_id, text, on_progress=reaper.plain)
    except SubscriptionLimitReached as err:
        _fail(f"{err} Nothing was retried.")
        return
    except (TownStopped, librarian.LibrarianError, MayorError, testset_runner.TestsetError) as err:
        _fail(str(err))
        return
    row = town.mayor.get(action_id)
    content, guard = json.loads(row["content"]), json.loads(row["guard_result"] or "{}")
    reaper.plain(content["diff"] or "(no change)", soft_wrap=True)
    if guard.get("blocks"):
        reaper.warn(f"Proposed as #{action_id}, BLOCKED: {'; '.join(guard['blocks'])}")
    else:
        reaper.report(f"Proposed as #{action_id}. Decide with `town pending`.")


@librarian_app.command("stats")
def librarian_stats(ctx: typer.Context) -> None:
    """Results per business type and area (what `town scout --auto` picks from)."""
    town = _town(ctx)
    cells = explore.cell_stats(town.conn)
    if not cells:
        reaper.report("No leads yet, so `town scout --auto` explores.")
        return
    for c in cells:
        reaper.plain(f"{c['type']:14} {c['area']:18} found {c['found']:3}  build-worthy {c['build_worthy']:3}  "
                     f"Critic-passed {c['critic_passed']:3}  previews approved {c['preview_approved']:3}  "
                     f"replies {'not tracked' if c['replies'] is None else c['replies']}")


# ---------------- outreach (the Messenger drafts; nothing is ever sent from here) ----------------

@app.command()
def draft(
    ctx: typer.Context,
    lead_id: int = typer.Argument(None, help="One lead. Leave out to draft for every lead that's ready."),
    follow_up: bool = typer.Option(False, "--follow-up", help="A follow-up email (only when the Guard's "
                                   "follow-up rule allows one)."),
) -> None:
    """Messenger drafts outreach (Sonnet via `claude -p` on your plan): an email once the lead's preview is live,
    or a call script for you when the prospect replied asking for a call (email-first: held leads get nothing).
    Proposes it; the Guard checks it; nothing is sent from here."""
    town = _town(ctx)
    try:
        run = run_messenger(town, lead_id, follow_up, on_progress=reaper.plain)
    except SubscriptionLimitReached as err:
        _fail(f"{err} Nothing was retried.")
        return
    except (TownStopped, MessengerError, MayorError) as err:
        _fail(str(err))
        return
    for d in run.drafts:
        _report_draft(town, d)
    reaper.report(run.message)


def _guard_note(guard: dict[str, Any]) -> str:
    if guard.get("verdict") == "blocked":
        return "BLOCKED by the Guard: " + "; ".join(guard.get("blocks") or [])
    flags = f" ({'; '.join(guard['flags'])})" if guard.get("flags") else ""
    return f"Guard: {guard.get('verdict', 'not checked')}{flags}"


def _report_draft(town: Town, d: dict[str, Any]) -> None:
    reaper.plain()
    tokens = d["tokens"]
    if not d["answer"]:
        reaper.warn(f"Lead #{d['lead_id']} {d['business_name']}: NO DRAFT ({d['kind']}). "
                    + (d["error"] or "; ".join(d["problems"][:4])))
    else:
        guard = d.get("guard") or {}
        (reaper.warn if guard.get("verdict") == "blocked" else reaper.report)(
            f"Lead #{d['lead_id']} {d['business_name']}: {d['kind'].replace('_', ' ')} proposed as #{d['action_id']}. "
            f"{_guard_note(guard)}")
        content = d["content"]
        if d["kind"] == "call_script":
            _print_script(town, content)
        else:
            reaper.plain(_shown(town, f"    Subject: {content['subject']}"))
            for line in content["body"].splitlines():
                reaper.plain(_shown(town, f"    | {line}"), soft_wrap=True)
    reaper.plain(f"    {d['wall_seconds']:.0f}s total, {d['claude_seconds']:.0f}s in `claude -p` ({d['rounds']} round(s), "
                 f"{d['turns']} turns) on {d['model'] or 'no model'}; {tokens['output_tokens']:,} output tokens, "
                 f"{tokens['cache_read_input_tokens']:,} cache read (API-price equivalent "
                 f"${d['api_equivalent_usd']:.2f}, not billed)")


def _print_script(town: Town, content: dict[str, Any]) -> None:
    script = content.get("script") or {}
    reaper.plain(f"    Call {content.get('business')} at {content.get('phone')}  (you call; the Town never does)")
    for key, label in (("opener", "Opener (20 s)"), ("problem", "The problem"), ("preview", "The preview"),
                       ("care_plan", "Care plan")):
        reaper.plain(_shown(town, f"    {label}: {script.get(key, '')}"), soft_wrap=True)
    # The link is never in what Charlie says: he offers to text or email it, and sends it from here after the call.
    link = content.get("preview_url")
    reaper.plain(_shown(town, f"    Link to text or email them after the call (don't read it out): "
                              f"{link or 'not published yet (publish the preview first)'}"), soft_wrap=True)
    for n, o in enumerate(script.get("objections") or [], 1):
        reaper.plain(f"    Objection {n}: {o.get('objection')}  ->  {o.get('answer')}", soft_wrap=True)
    reaper.plain(f"    Voicemail: {script.get('voicemail', '')}", soft_wrap=True)


@app.command()
def calls(ctx: typer.Context) -> None:
    """Your call scripts (prospects who replied asking for a call): waiting for approval, ready, or done."""
    town = _town(ctx)
    scripts = call_scripts(town)
    if not scripts:
        reaper.report("No call scripts yet. They come from `town draft <lead id>` after a prospect replies asking for "
                      "a call (`town inbox` flags it).")
        return
    for row in scripts:
        state = {"proposed": "waiting for your OK in `town pending`", "approved": "approved, not yet marked ready",
                 "executed": "READY to call", "failed": "failed"}.get(row["status"], row["status"])
        reaper.plain()
        reaper.report(f"#{row['id']}  {row['target']}  |  {state}")
        _print_script(town, row["content"])


# ---------------- publishing (Cloudflare Pages) ----------------

@app.command("propose-publish")
def propose_publish(ctx: typer.Context, build_id: int = typer.Argument(..., help="A Critic-passed build.")) -> None:
    """Propose publishing a Critic-passed build (a lead's preview, or the homepage). Publishes nothing."""
    town = _town(ctx)
    try:
        action_id = previews.propose_publish(town, build_id)
    except (previews.PublishError, MayorError, TownStopped) as err:
        _fail(str(err))
        return
    reaper.report(f"Proposed as #{action_id}. Decide with `town pending`.")


@app.command()
def takedown(
    ctx: typer.Context,
    build_id: int = typer.Argument(..., help="The build whose live preview comes down."),
    reason: str = typer.Option(..., "--reason", help="Why (required; it's kept as feedback)."),
) -> None:
    """Take a live preview down now (your decision). Redeploys without it and deletes old deployments."""
    town = _town(ctx)
    try:
        deployment = previews.takedown(_publisher(ctx), town.conn, build_id, reason, by=town.actor)
    except (previews.PublishError, CloudflareError, TownStopped) as err:
        _fail(str(err))
        return
    note = "" if _publisher(ctx).client.real else " (FAKE Cloudflare on this instance: nothing was online)"
    reaper.report(f"Build #{build_id}'s preview is down (deployment {deployment.id}){note}.")


@app.command("previews")
def list_previews(ctx: typer.Context) -> None:
    """The live homepage and previews, with their private links (yours to share with that business only)."""
    town = _town(ctx)
    live = previews.live_previews(town.conn)
    if not live:
        reaper.report("Nothing is live.")
        return
    for p in live:
        if p["kind"] == "homepage":
            reaper.plain(f"Homepage (build #{p['build_id']}): {previews.SITE_URL}/")
        else:
            reaper.plain(f"{p['business_name']} (build #{p['build_id']}): {previews.preview_url(p['token'])}  "
                         f"expires {chicago_time(p['expires_at'])}", soft_wrap=True)


@app.command("cloudflare-check")
def cloudflare_check(ctx: typer.Context) -> None:
    """Read-only: does the Cloudflare token work, is it Pages-only, and is the project there? Deploys nothing."""
    client = _publisher(ctx).client
    if not isinstance(client, RealCloudflare):
        recorded = client.recorded() if isinstance(client, FakeCloudflare) else []
        reaper.report(f"This instance uses the FAKE Cloudflare client ({_town(ctx).settings.instance}): nothing "
                      f"here can reach Cloudflare. Recorded deployments: {len(recorded)}.")
        return
    try:
        lines = client.check()
    except CloudflareError as err:
        _fail(str(err))
        return
    _report_checks(lines)


def _report_checks(lines: list[tuple[bool, str]]) -> None:
    for passed, line in lines:
        (reaper.report if passed else reaper.alert)(("OK    " if passed else "FAIL  ") + line)
    if not all(passed for passed, _ in lines):
        raise typer.Exit(code=1)


# ---------------- Gmail (drafts; a real send only once Charlie switches go-live on) ----------------

@app.command("gmail-connect")
def gmail_connect(ctx: typer.Context) -> None:
    """Charlie only, real Town only: sign in to town@example.com in your browser (gmail.compose +
    gmail.readonly)."""
    town = _town(ctx)
    if town.actor != CHARLIE or not _is_real_file(town.settings, ctx.meta["instance_file"]):
        log_event(town.conn, "gmail", "gmail_connect_refused",
                  {"instance": town.settings.instance, "by": town.actor})
        _fail("Negative. `town gmail-connect` is only for the real Town, run by Charlie in his own terminal. "
              "The dev instance always uses the fake Gmail.")
        return
    if town.killswitch.is_stopped():
        _fail("The Town is stopped (kill switch). Run `town resume` first, then `town gmail-connect`.")
        return
    reaper.report(f"Opening Google's sign-in in your browser. Sign in as {guard_rules.SENDING_ACCOUNT} yourself. "
                  "The Town asks for gmail.compose (drafts, and sending an approved one once you switch go-live on) "
                  "and gmail.readonly (the Clerk reads replies). Tick both boxes. It never sees your password or code.")
    lines = gmail.connect(ROOT / ".env", GMAIL_TOKEN_FILE)
    connected = all(passed for passed, _ in lines)
    log_event(town.conn, "gmail", "gmail_connected" if connected else "gmail_connect_failed",
              {"account": guard_rules.SENDING_ACCOUNT, "checks": [line for _, line in lines], "by": town.actor})
    _report_checks(lines)


@app.command("gmail-check")
def gmail_check(
    ctx: typer.Context,
    test_draft: bool = typer.Option(False, "--test-draft",
                                    help="Also save one test draft addressed to town@example.com itself."),
) -> None:
    """Read-only: the Gmail token works, it's town@example.com with exactly gmail.compose + gmail.readonly,
    and the draft count."""
    town = _town(ctx)
    client = _gmail(town.settings, ctx.meta["instance_file"], town.actor)
    if isinstance(client, FakeGmail):
        reaper.report(f"This instance uses the FAKE Gmail client ({town.settings.instance}): nothing here can reach "
                      f"Google. Recorded drafts: {client.count_drafts()} (in {client.folder}).")
        if not test_draft:
            return
    if test_draft and town.killswitch.is_stopped():
        _fail("The Town is stopped (kill switch): no test draft. Run `town resume` first.")
        return
    lines = gmail.check(client, test_draft)
    log_event(town.conn, "gmail", "gmail_checked", {"test_draft": test_draft, "fake": not client.real,
                                                    "checks": [f"{'OK' if p else 'FAIL'} {line}" for p, line in lines]})
    scope = next(((p, line) for p, line in lines if "scope" in line), None)
    if scope:   # the go-live checklist (and HQ) shows the last result
        log_event(town.conn, "gmail", "gmail_scopes_checked", {"ok": scope[0], "line": scope[1]})
    _report_checks(lines)


# ---------------- go-live: shadow days and the send switch (Charlie only) ----------------

shadow_app = typer.Typer(help="Shadow days: the whole Town runs the full loop in dry-run; you review every draft in "
                              "Gmail.", no_args_is_help=True)
app.add_typer(shadow_app, name="shadow")


@shadow_app.command("start")
def shadow_start(ctx: typer.Context) -> None:
    """Start shadow mode: from now on, each day the full loop runs in dry-run is logged as a shadow day."""
    town = _town(ctx)
    if town.killswitch.is_stopped():
        _fail("The Town is stopped (kill switch). Run `town resume` first.")
        return
    started = golive.start_shadow(town.conn, town.actor)
    reaper.report("Shadow mode started." if started else "Shadow mode was already on.")
    _shadow_report(town)


@shadow_app.command("status")
def shadow_status(ctx: typer.Context) -> None:
    """Shadow days so far, and what today still needs."""
    _shadow_report(_town(ctx))


def _shadow_report(town: Town) -> None:
    days, need = golive.shadow_days(town.conn), int(town.limits.get("golive.shadow_days_required"))
    started = golive.shadow_started(town.conn)
    if not started:
        reaper.report("Shadow mode isn't on. `town shadow start` begins it.")
        return
    reaper.report(f"Shadow days: {len(days)} of {need}" + (f" ({', '.join(days)})" if days else "") + ".")
    reaper.plain("Today (each from a real event; logged as a shadow day once all are done, with nothing sent):")
    for line in golive.shadow_day_words(golive.today_steps(town.conn, started)):
        reaper.plain(f"    {line}")


@app.command("golive")
def golive_command(
    ctx: typer.Context,
    off: bool = typer.Option(False, "--off", help="Switch real sending OFF (approved emails go back to drafts)."),
    reason: str = typer.Option("", "--reason", help="Why (with --off)."),
) -> None:
    """Charlie only, real Town only: the go-live checklist, then switch real sending on (after typing the phrase).
    `--off` switches it back off. Every email still needs your approval; the Guard and the kill switch still apply."""
    town = _town(ctx)
    settings = town.settings
    if off:
        golive.flip_off(town.conn, settings, town.actor, reason.strip() or "switched off with town golive --off")
        reaper.report("Go-live is OFF. Approved emails are saved as Gmail drafts again; nothing is sent.")
        return
    if (town.actor != CHARLIE or not _is_real_file(settings, ctx.meta["instance_file"])
            or "PYTEST_CURRENT_TEST" in os.environ):
        log_event(town.conn, town.actor, "golive_refused", {"why": "not Charlie on his real instance",
                                                             "instance": settings.instance})
        _fail("Negative. `town golive` is only for Charlie's real Town, run by Charlie in his own terminal "
              "(never Claude Code, the dev instance or a test). Sending stays off.")
        return
    if town.killswitch.is_stopped():
        _fail("The Town is stopped (kill switch). Run `town resume` first. Sending stays off.")
        return
    client = _gmail(settings, ctx.meta["instance_file"], town.actor)
    try:
        granted = client.granted_scopes() if isinstance(client, RealGmail) else set()
        scopes = (granted == gmail.REQUIRED_SCOPES, "Gmail scopes: " + (
            "exactly gmail.compose + gmail.readonly" if granted == gmail.REQUIRED_SCOPES else
            f"{', '.join(sorted(granted)) or 'none'} (run `town gmail-connect` again)"))
    except gmail.GmailError as err:
        scopes = (False, f"Gmail scopes: couldn't check ({err})")
    log_event(town.conn, "gmail", "gmail_scopes_checked", {"ok": scopes[0], "line": scopes[1]})
    dad = typer.prompt("Has your dad said OK to using his office address on Example Web Studio's emails? (yes/no)")
    shadow_ok = typer.prompt("Would you have sent the shadow-day drafts in Gmail as they were? (yes/no)")
    lines = golive.checklist(town.conn, settings, town.limits, scopes, dad_ok=dad.strip().lower() in ("yes", "y"))
    lines.append((shadow_ok.strip().lower() in ("yes", "y"), "You'd have sent the shadow drafts: "
                  + ("yes" if shadow_ok.strip().lower() in ("yes", "y") else "NO (fix the drafts, run more shadow days)")))
    for ok, line in lines:
        (reaper.report if ok else reaper.alert)(("OK    " if ok else "NO    ") + line)
    if not all(ok for ok, _ in lines):
        log_event(town.conn, CHARLIE, "golive_refused", {"why": [line for ok, line in lines if not ok]})
        _fail("Not every item passes, so sending stays OFF.")
        return
    phrase = typer.prompt(f"Type {golive.CONFIRM_PHRASE} to switch real sending on (anything else cancels)")
    if phrase.strip() != golive.CONFIRM_PHRASE:
        log_event(town.conn, CHARLIE, "golive_refused", {"why": "confirmation phrase not typed"})
        _fail("Cancelled. Sending stays OFF.")
        return
    golive.flip_on(town.conn, settings, CHARLIE, {"dad_ok": True, "shadow_drafts_ok": True,
                                                  "shadow_days": golive.shadow_days(town.conn),
                                                  "sends_per_day": int(town.limits.get("email.sends_per_day"))})
    reaper.report("Go-live is ON. From now on an approved email is SENT (once, never retried). Every one still "
                  "needs your approval in `town pending`; `town golive --off` or `town stop` stops it.")
    reaper.warn(golive.SHADOW_DRAFTS_WARNING)


# ---------------- autopilot: the Mayor routes approved work to the next agent ----------------

@app.command("autopilot")
def autopilot_command(
    ctx: typer.Context,
    once: bool = typer.Option(False, "--once", help="Run a single cycle, then stop."),
) -> None:
    """Runs the loop by itself every autopilot.cycle_minutes: inbox, inspect, build (+ Critic), email drafts,
    follow-ups, Scout when leads run low. It approves NOTHING: leads, publishes, emails and replies wait for you in
    `town pending`. Ctrl+C or `town stop` ends it. Charlie on the real Town; the dev instance runs it with the fakes."""
    town = _town(ctx)
    instance_file = ctx.meta["instance_file"]
    dev = not _is_real(town.settings)
    if not dev and not (town.actor == CHARLIE and _is_real_file(town.settings, instance_file)):
        log_event(town.conn, town.actor, "autopilot_refused", {"why": "not Charlie on his real Town or the dev instance"})
        _fail("Negative. `town autopilot` runs on Charlie's real Town (run by Charlie) or the dev instance only.")
        return
    if town.killswitch.is_stopped():
        _fail("The Town is stopped (kill switch). Autopilot never runs while it's stopped: `town resume` first.")
        return
    if not once and autopilot.status(town)["on"]:
        _fail("Autopilot is already running (in another window, or started from HQ with \"go\"; one at a time, so "
              "cycles never overlap). Ctrl+C there, or HQ's Stop button, to stop it.")
        return
    client = _gmail(town.settings, instance_file, town.actor)
    minutes = float(town.limits.get("autopilot.cycle_minutes"))
    react_s = float(town.limits.get("autopilot.react_seconds"))
    reaper.report("Autopilot on. " + ("One cycle." if once else f"A cycle every {minutes:g} minutes, and within "
                  f"{react_s:g} seconds of each approval; Ctrl+C or `town stop` ends it.")
                  + " It approves nothing: decisions wait for you in `town pending`.")

    def report(cycle: autopilot.Cycle) -> None:
        reaper.report(cycle.line)
        for line in cycle.skipped:
            reaper.plain(f"    held or skipped: {line}", soft_wrap=True)

    try:
        why = autopilot.run_loop(town, client, _publisher(ctx), town.actor, once=once, on_cycle=report,
                                 after_cycle=lambda: _shadow_upkeep(town))
    except KeyboardInterrupt:
        reaper.report("Autopilot off (Ctrl+C).")
        return
    if why != "one cycle done":
        reaper.warn(f"Autopilot ends: {why}. Nothing more runs.")


def _autopilot_starter(settings: Settings, actor: str, instance_file: Path) -> Any:
    """How "go" (HQ or Reaper, after Charlie's Confirm) starts autopilot here, or None where it may not: the same
    rule as `town autopilot` (Charlie on his real Town, or the dev instance; never Claude Code on the real Town,
    never another instance file). The loop gets its own Town and the same real-vs-fake clients as this command."""
    dev = not _is_real(settings)
    if not dev and not (actor == CHARLIE and _is_real_file(settings, instance_file)):
        return None

    def tools() -> tuple[Town, Any, Any]:
        town, publisher = _open(settings, actor, instance_file)
        return town, _gmail(settings, instance_file, actor), publisher
    return lambda: autopilot.start_background(tools, actor)


# ---------------- the Clerk: replies on the Town's own threads ----------------

@app.command("inbox")
def inbox_command(
    ctx: typer.Context,
    watch: bool = typer.Option(False, "--watch", help="Check every clerk.check_every_minutes while the Town runs "
                                                     "(Ctrl+C or `town stop` ends it)."),
) -> None:
    """The Clerk reads replies to the Town's emails (plain code first, Haiku only for unclear ones): opt-outs go on
    the do-not-contact list, replies that need you go in `town pending`. It never replies to anyone."""
    town = _town(ctx)
    client = _gmail(town.settings, ctx.meta["instance_file"], town.actor)
    while True:
        try:
            run = clerk.run_inbox(town, client)
        except TownStopped as err:
            _fail(str(err))
            return
        except (gmail.GmailError, PaidWorkPaused, MayorError) as err:
            _fail(f"Inbox check failed: {err}. Nothing was retried.")
            return
        reaper.report(run.message)
        for line in clerk.opt_out_notices(town.conn, inbox_ids=run.opted_out):
            reaper.warn(line)
        for action_id in run.needs_charlie:
            row = town.mayor.get(action_id)
            reaper.plain(f"    #{action_id} {row['target']}: {row['reason']}")
        if not watch:
            return
        _shadow_upkeep(town)
        minutes = float(town.limits.get("clerk.check_every_minutes"))
        reaper.plain(f"Next inbox check in {minutes:g} minutes (Ctrl+C to end).")
        deadline = time.monotonic() + minutes * 60
        while time.monotonic() < deadline:
            if town.killswitch.is_stopped():
                reaper.warn("The Town was stopped: the inbox watch ends.")
                return
            time.sleep(5)


def _report_build(b: dict[str, Any]) -> None:
    reaper.plain()
    who = (f"lead #{b['lead_id']} {b['business_name']}" if b["lead_id"] else "Example Web Studio's homepage"
           if b.get("kind") == "homepage" else f"test case {b['testset_case']} {b['business_name']}")
    tokens = b["tokens"]
    if b["status"] == "built":
        reaper.report(f"Build #{b['id']} for {who}: BUILT. Facts check passed.")
    else:
        reaper.warn(f"Build #{b['id']} for {who}: FAILED. {b['failure']}")
    reaper.plain(f"    Time:   {b['wall_seconds']:.0f}s total, {b['claude_seconds']:.0f}s in `claude -p` "
                 f"({b['rounds']} round(s), {b['turns']} turns) on {b['model'] or 'no model'}")
    reaper.plain(f"    Tokens: {tokens['input_tokens']:,} in, {tokens['output_tokens']:,} out, "
                 f"{tokens['cache_read_input_tokens']:,} cache read, {tokens['cache_creation_input_tokens']:,} "
                 f"cache write (API-price equivalent ${b['api_equivalent_usd']:.2f}, not billed)")
    reaper.plain(f"    Design skill used: {'yes' if b['design_skill_used'] else 'NO'}")
    quality = b.get("quality") or {}
    lighthouse = quality.get("lighthouse") or {}
    if quality:
        reaper.plain(f"    Phone (375px): {'scrolls sideways' if quality.get('overflows_at_375') else 'fits'};"
                     f" Lighthouse performance {lighthouse.get('performance')}, accessibility "
                     f"{lighthouse.get('accessibility')}")
    reaper.plain(f"    Folder: {b['workspace']}")


@app.command()
def show(
    ctx: typer.Context,
    lead_id: int = typer.Argument(None, help="Show this lead's newest built site."),
    build_id: int = typer.Option(None, "--build", help="Show one build by its id (test-set builds too)."),
) -> None:
    """Open a built site in your browser, served from this laptop only (127.0.0.1). Ctrl+C to stop."""
    town = _town(ctx)
    if lead_id is None and build_id is None:
        _fail("Say which site: `town show <lead id>` or `town show --build <build id>`.")
    try:
        dist = latest_built_workspace(town, lead_id, build_id)
    except BuilderError as err:
        _fail(str(err))
        return
    shown = town.conn.execute("SELECT id FROM builds WHERE workspace = ? ORDER BY id DESC LIMIT 1",
                              (str(dist.parent),)).fetchone()
    review = latest_review(town.conn, build_id or shown["id"]) if (build_id or shown) else None
    if review:
        _review_lines(review, indent="")
    else:
        reaper.plain("Not reviewed by the Critic yet.")
    with serve_folder(dist) as url:
        reaper.report(f"Serving {dist} at {url} (only this laptop can see it). Ctrl+C to stop.")
        webbrowser.open(url)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            reaper.report("Stopped serving.")


# ---------------- Reaper (Jarvis): chat, sit-rep, the local HQ server ----------------

def _can_decide(town: Town) -> bool:
    """Decisions through Reaper are Charlie's: Claude Code may only make them on the dev instance."""
    return not (town.actor == CLAUDE_CODE and _is_real(town.settings))


def _reaper_rules(ctx: typer.Context, town: Town) -> tuple[decisions.Rules, order_rules.Runner]:
    """What Reaper may propose here: edits (never of a publish), and the pack's orders, which start in the
    background with their own Town (the same real-vs-fake client rule as this command's) once confirmed."""
    settings, actor, instance_file = town.settings, town.actor, ctx.meta["instance_file"]
    book = ORDERS.with_autopilot(_autopilot_starter(settings, actor, instance_file))
    runner = order_rules.Runner(lambda: _open(settings, actor, instance_file)[0], book, actor)
    return decisions.Rules(uneditable_types=frozenset(pack.LIVE_EXTERNAL_TYPES), order_book=book, noun=VIEW.noun,
                           launch=runner.launch), runner


@app.command("reaper")
def reaper_command(ctx: typer.Context) -> None:
    """Talk to Reaper in the terminal. Decisions only happen when you type y to confirm."""
    town = _town(ctx)
    rules, runner = _reaper_rules(ctx, town)
    run_chat(Reaper(town, VIEW, rules=rules), _can_decide(town), reaper.console)
    if any(t.is_alive() for t in runner.threads):
        reaper.report("Background orders are still running; this window stays open until they finish "
                      "(`town stop` in another terminal halts them).")
        runner.wait()


def _print_timing(line: str) -> None:
    """One line of `town hq --timing` (engine/hq.py): the time, the step and how long it took. No token, no message."""
    print(f"[timing {time.strftime('%H:%M:%S')}] {line}", flush=True)


@app.command("hq")
def hq_command(
    ctx: typer.Context,
    port: int = typer.Option(0, "--port", help="Port on 127.0.0.1 (0 = pick a free one)."),
    no_browser: bool = typer.Option(False, "--no-browser", help="Don't open the browser; just print the link."),
    timing: bool = typer.Option(False, "--timing",
                                help="Print how long each request takes, step by step (for a slow or stuck page)."),
) -> None:
    """Reaper HQ: a local page (127.0.0.1 only) to talk to Reaper and watch the Town. Ctrl+C stops it."""
    town = _town(ctx)
    settings, actor, instance_file = town.settings, town.actor, ctx.meta["instance_file"]
    port = port or hq.free_port()
    token = hq.new_token()   # new every launch; only this link opens the page
    reader = lambda: sqlite3.connect(f"{Path(settings.database).resolve().as_uri()}?mode=ro", uri=True)
    rules, _ = _reaper_rules(ctx, town)
    voice = Voice()
    if voice.available() is None:
        # Load Kokoro and make the lines Reaper says most while the page opens, so his first words come quickly.
        threading.Thread(target=voice.warm, args=(STAND_DOWN_REPLY, CATCHPHRASE),
                         kwargs={"delay": WARM_DELAY_SECONDS}, daemon=True).start()
    app_ = hq.create_app(lambda: _open(settings, actor, instance_file)[0], VIEW, token, port, _can_decide(town),
                         open_reader=reader, rules=rules, voice=voice,
                         timing=_print_timing if timing else None)
    url = f"http://{hq.HOST}:{port}/?token={token}"
    log_event(town.conn, "jarvis", "hq_started", {"port": port, "instance": settings.instance, "by": actor})
    reaper.report(f"Reaper HQ online at http://{hq.HOST}:{port} ({settings.instance} instance). Ctrl+C to stop.")
    reaper.plain(f"Private link (opens the page): {url}", soft_wrap=True)
    reason = voice.available()
    reaper.plain("Reaper's voice: ready (made on this laptop)." if reason is None
                 else f"Reaper's voice: the browser's own voice, because {reason}.")
    if not no_browser:
        webbrowser.open(url)
    hq.serve(app_, port)
    reaper.report("Reaper HQ offline.")


@app.command("sitrep")
def sitrep_command(
    ctx: typer.Context,
    show: bool = typer.Option(False, "--show", help="Open the saved sit-rep file when it's done."),
    plain: bool = typer.Option(False, "--plain", help="Plain-code version only (no model call, free)."),
    schedule: str = typer.Option(None, "--schedule", metavar="HH:MM",
                                 help="Print the Task Scheduler command for a daily sit-rep (you run it yourself)."),
    rate: str = typer.Option(None, "--rate", help="Rate the last sit-rep: up or down (logged; changes nothing)."),
    change: str = typer.Option(None, "--change", help="With --rate: what to change next time: shorter, "
                               "more_detail, skip_costs or lead_waiting (proposed for your approval)."),
) -> None:
    """The morning sit-rep: since the last one, what's waiting (grouped for batches), today's plan, a lesson."""
    if rate is not None:
        if rate not in ("up", "down"):
            _fail("--rate is up or down.")
            return
        town = _town(ctx)
        last = town.conn.execute("SELECT details FROM events WHERE event_type = 'sitrep_delivered' ORDER BY id DESC"
                                 " LIMIT 1").fetchone()
        try:
            out = profile.record_rating(town, "sit-rep", 1 if rate == "up" else -1,
                                        json.loads(last["details"]).get("text", "")[:300] if last else "", change)
        except (ValueError, MayorError, TownStopped) as err:
            _fail(str(err))
            return
        reaper.report(out["message"])
        return
    if schedule is not None:
        try:
            command = sitrep.schedule_command(schedule, ROOT / ".venv" / "Scripts" / "town.exe", ROOT)
        except ValueError as err:
            _fail(str(err))
            return
        reaper.report("Nothing was scheduled. To get a sit-rep every day, paste this into PowerShell yourself:")
        reaper.plain(command, soft_wrap=True)
        reaper.plain("Check it: Task Scheduler > Task Scheduler Library > 'Town morning sit-rep'. "
                     "Remove it: Unregister-ScheduledTask -TaskName \"Town morning sit-rep\"", soft_wrap=True)
        return
    town = _town(ctx)
    report = sitrep.run_sitrep(town, VIEW, use_model=not plain)
    reaper.report("Sit-rep follows.")
    reaper.plain(report.text)
    reaper.plain()
    how = f"worded by {town.routing.for_agent('jarvis').model}, ${report.cost_usd:.5f}" if report.by == "model" \
        else f"plain code ({report.note})"
    reaper.plain(f"({how}. Saved: {report.path})", soft_wrap=True)
    if show:
        os.startfile(report.path)   # opens the file in Charlie's default app for .md files (Windows only)


# ---------------- the test set ----------------

def _pct(earned: float, possible: float) -> str:
    return f"{earned / possible:.0%}" if possible else "n/a"


@app.command()
def testset(
    ctx: typer.Context,
    show_history: bool = typer.Option(False, "--history", help="List past runs instead of running."),
    runs: int = typer.Option(None, "--runs", min=1,
                             help="Run the set this many times; scores are averaged. Default 3 (1 with builder or critic)."),
    agents: str = typer.Option(None, "--agents",
                               help="Comma list: scout, inspector, builder, critic, messenger, guard, reaper. Default "
                                    "scout,inspector. builder, critic and messenger use your Claude plan (2 builds, "
                                    "4 reviews, 8 drafts); guard and reaper are free (plain code; reaper uses a "
                                    "scripted model and a throwaway town)."),
    cases: str = typer.Option(None, "--cases", help="Builder only: comma list of case folders to build."),
) -> None:
    """Score the agents on the fictional test businesses (one small Haiku call per run for Scout, logged)."""
    town = _town(ctx)
    if show_history:
        _testset_history(town)
        return
    chosen = [a.strip() for a in agents.split(",") if a.strip()] if agents else None
    builder_cases = tuple(c.strip() for c in cases.split(",") if c.strip()) if cases else None
    runs = runs or (1 if chosen and {"builder", "critic", "messenger", "guard", "reaper"} & set(chosen) else 3)
    try:
        result = testset_runner.run_testset(town, runs, agents=chosen, builder_cases=builder_cases,
                                            on_progress=reaper.plain)
    except SubscriptionLimitReached as err:
        _fail(f"{err} Nothing was retried.")
        return
    except (TownStopped, PaidWorkPaused, MissingAPIKey, testset_runner.TestsetError) as err:
        _fail(str(err))
        return
    except anthropic.APIError as err:
        _fail(f"Test set's API call failed: {err}")
        return
    reaper.report(f"Test set run #{result.run_id}: {result.cases} cases x {result.runs} run(s), "
                  f"commit {result.git_commit} ({town.settings.instance} instance).")
    for score in result.scores:
        parts = ", ".join(f"{name} {e:g}/{p}" for name, (e, p) in score.parts.items())
        reaper.plain(f"  {score.agent:<15} avg {score.earned:g}/{score.possible}  "
                     f"{_pct(score.earned, score.possible):>4}  lowest {score.lowest}/{score.possible}"
                     f"   ({parts})   ${score.cost_usd:.5f} on {score.model or 'no model'}")
        if score.note:
            reaper.warn(f"  {score.agent}: {score.note}")
    for agent, session in testset_runner.NOT_BUILT.items():
        if testset_runner.AGENT_SLOTS.get(agent) is None and not chosen:
            reaper.plain(f"  {agent:<15} not built yet ({session})")
    reaper.plain(f"  {'TOTAL':<15} avg {result.earned:g}/{result.possible}  "
                 f"{_pct(result.earned, result.possible):>4}   cost ${result.cost_usd:.5f} (logged)")
    previous = result.previous
    if previous:
        before, now = previous["earned"] / previous["possible"], result.earned / result.possible
        verdict = "Holds or improves." if now >= before else "WORSE than the last run: don't ship this change."
        reaper.report(f"Last run #{previous['id']} ({previous['git_commit']}, {previous['runs']} run(s)): "
                      f"average {before:.0%}. Now {now:.0%}. {verdict}")
    misses = [m for s in result.scores for m in s.misses]
    if misses:
        reaper.plain("Misses:")
        for miss in misses:
            reaper.plain(f"  - {miss}")


def _testset_history(town: Town) -> None:
    rows = testset_runner.history(town)
    if not rows:
        reaper.report("No test-set runs yet. Run `town testset`.")
        return
    for row in reversed(rows):
        agents = json.loads(row["scores"]).get("agents", {})
        by_agent = "  ".join(f"{name} {a['earned']:g}/{a['possible']}" for name, a in agents.items())
        reaper.plain(f"#{row['id']:<4} {chicago_time(row['ts'])}  {row['git_commit']:<15} x{row['runs']}  "
                     f"{row['earned']:g}/{row['possible']:g} {_pct(row['earned'], row['possible']):>4}  "
                     f"${row['cost_usd']:.5f}  {by_agent}")


# ---------------- lead data ----------------

@app.command("overture-download")
def overture_download(
    ctx: typer.Context,
    release: str = typer.Option(None, "--release", help="An Overture release like 2026-09-23.1 (default: newest)."),
) -> None:
    """Download Overture Maps places for this instance's region into a local file (free, no key)."""
    town = _town(ctx)
    if town.killswitch.is_stopped():
        _fail("Town stopped. Run `town resume` first.")
    region = town.settings.overture
    reaper.report(f"Pulling Overture places for {region.name}. This can take a few minutes.")
    try:
        meta = overture.download(region, release)
    except Exception as err:
        log_event(town.conn, "scout", "overture_download_failed", {"error": f"{type(err).__name__}: {err}"})
        _fail(f"Overture download failed: {err}")
        return
    log_event(town.conn, "scout", "overture_downloaded", meta)
    reaper.report(f"{meta['count']} places stored from release {meta['release']}.")


@app.command()
def backup(ctx: typer.Context) -> None:
    """Back up the database now (safe while the Town runs). Keeps the newest ones, per limits.yaml."""
    town = _town(ctx)
    try:
        path = backup_database(town.conn, town.settings.backup_folder, int(town.limits.get("backups.keep_last")))
    except Exception as err:
        log_event(town.conn, "librarian", "backup_failed", {"error": f"{type(err).__name__}: {err}"})
        _fail(f"Backup FAILED: {err}")
        return
    reaper.report(f"Backup secured: {path}")


@app.command()
def version() -> None:
    """Show the Town's version."""
    reaper.plain("The Town 0.1.0")
