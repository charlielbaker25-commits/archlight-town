"""The only path from Town code to `claude -p` (heavy jobs on Charlie's own subscription login).

Every run, in order:
1. checks the kill switch, and that no subscription usage limit is still waiting to reset,
2. asks models.yaml which model this agent uses (it must be on the claude_cli route),
3. launches the official `claude` CLI, locked down:
   - ANTHROPIC_API_KEY (and every other ANTHROPIC_* variable) removed, so it uses the subscription
     and can never bill the API key,
   - `--restricted`: no command-running tools or web access unless named, user/project settings
     (hooks, plugins, Charlie's own rules) ignored, file tools confined to the working folder,
   - only the tools the agent is given, `--permission-mode dontAsk` (anything else is refused),
   - never `--dangerously-skip-permissions` (a code check refuses to build such a command),
4. watches it: `town stop` or the time limit kills the whole process tree,
5. reads its stream-json output: the tools it used, its time, tokens and turns,
6. logs the usage to the costs table ($0 billed: it's subscription work) and the event log.

A Pro usage-limit error is never retried: it's logged with a wait-until time, and every
subscription job refuses to start until then. The Town has no loop that waits for it.
"""
import json
import os
import re
import sqlite3
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from engine.budget import Budget
from engine.events import log_event
from engine.killswitch import KillSwitch, TownStopped
from engine.models import CLI_ROUTE, ModelRouting

SUBSCRIPTION_SERVICE = "claude_subscription"
LIMIT_EVENT = "subscription_limit_hit"
FORBIDDEN_FLAGS = ("--dangerously-skip-permissions", "--allow-dangerously-skip-permissions")
# Variables a `claude -p` child must never see. ANTHROPIC_* would make it bill the API (or point it
# elsewhere); the Claude Code markers would make it think it's nested inside another session.
STRIPPED_PREFIXES = ("ANTHROPIC_",)
STRIPPED_NAMES = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")
USAGE_LIMIT_WORDS = re.compile(r"usage limit|hit your limit|limit reached|out of extra usage", re.IGNORECASE)
RESET_EPOCH = re.compile(r"\|(\d{10})\b")
RESET_CLOCK = re.compile(r"resets\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)", re.IGNORECASE)
POLL_SECONDS = 1.0


class SubscriptionLimitReached(RuntimeError):
    """Charlie's plan usage limit was hit. Subscription jobs wait until `wait_until` (UTC)."""

    def __init__(self, message: str, wait_until: datetime) -> None:
        super().__init__(message)
        self.wait_until = wait_until


class CliTimeout(RuntimeError):
    pass


class CliError(RuntimeError):
    pass


@dataclass
class CliResult:
    text: str
    session_id: str
    model: str
    wall_seconds: float
    duration_ms: int = 0
    duration_api_ms: int = 0
    num_turns: int = 0
    usage: dict[str, int] = field(default_factory=dict)
    api_equivalent_usd: float = 0.0   # what the same tokens would cost on the API; NOT billed
    tool_uses: dict[str, int] = field(default_factory=dict)
    skills_used: list[str] = field(default_factory=list)
    permission_denials: int = 0
    structured: dict[str, Any] | None = None   # the reply as JSON, when the run asked for --json-schema


def clean_env(environ: dict[str, str] | None = None) -> dict[str, str]:
    environ = dict(os.environ if environ is None else environ)
    return {k: v for k, v in environ.items()
            if not k.upper().startswith(STRIPPED_PREFIXES) and k.upper() not in STRIPPED_NAMES}


def build_command(claude: str, model: str, tools: list[str], denied: list[str] = (),
                  plugin_dirs: list[Path] = (), system_append: str = "", resume: str | None = None,
                  json_schema: str | None = None) -> list[str]:
    command = [claude, "-p", "--restricted", "--strict-mcp-config", "--model", model,
               "--tools", ",".join(tools), "--allowedTools", *tools, "--permission-mode", "dontAsk",
               "--output-format", "stream-json", "--verbose"]
    if denied:
        command += ["--disallowedTools", *denied]
    for folder in plugin_dirs:
        command += ["--plugin-dir", str(folder)]
    if system_append:
        command += ["--append-system-prompt", system_append]
    if resume:
        command += ["--resume", resume]
    if json_schema:
        command += ["--json-schema", json_schema]
    if any(flag in command for flag in FORBIDDEN_FLAGS):
        raise CliError("refused: the Town never skips Claude Code's permission checks")
    return command


def parse_reset(text: str, now: datetime, fallback_minutes: float) -> datetime:
    """When the usage limit resets, if Claude said; otherwise now + the fallback wait (limits.yaml)."""
    epoch = RESET_EPOCH.search(text or "")
    if epoch:
        return datetime.fromtimestamp(int(epoch.group(1)), timezone.utc)
    clock = RESET_CLOCK.search(text or "")
    if clock:
        hour = int(clock.group(1)) % 12 + (12 if clock.group(3).lower() == "pm" else 0)
        local_now = now.astimezone()
        reset = local_now.replace(hour=hour, minute=int(clock.group(2) or 0), second=0, microsecond=0)
        if reset <= local_now:
            reset += timedelta(days=1)
        return reset.astimezone(timezone.utc)
    return now + timedelta(minutes=fallback_minutes)


def is_usage_limit(result: dict[str, Any] | None, stderr: str) -> bool:
    if result and result.get("is_error") and result.get("api_error_status") == 429:
        return True
    text = f"{(result or {}).get('result') or ''} {stderr}"
    return bool((result is None or result.get("is_error")) and USAGE_LIMIT_WORDS.search(text))


def subscription_wait_until(conn: sqlite3.Connection, now: datetime | None = None) -> datetime | None:
    """The latest usage-limit wait, if it hasn't passed yet."""
    row = conn.execute("SELECT details FROM events WHERE event_type = ? ORDER BY id DESC LIMIT 1",
                       (LIMIT_EVENT,)).fetchone()
    if not row:
        return None
    until = datetime.fromisoformat(json.loads(row[0])["wait_until"])
    return until if until > (now or datetime.now(timezone.utc)) else None


def kill_tree(process: subprocess.Popen) -> None:
    """`claude` starts child processes; on Windows only taskkill /T reaches all of them."""
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False)
    else:
        process.kill()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()


Launcher = Callable[..., subprocess.Popen]


class ClaudeCLI:
    def __init__(self, conn: sqlite3.Connection, routing: ModelRouting, budget: Budget, killswitch: KillSwitch,
                 claude_path: str = "claude", launch: Launcher = subprocess.Popen) -> None:
        self.conn = conn
        self.routing = routing
        self.budget = budget
        self.killswitch = killswitch
        self.claude_path = claude_path
        self._launch = launch   # tests pass a fake; pytest never starts the real `claude`

    def check_available(self) -> None:
        """Before any subscription job: the Town is running and no usage-limit wait is in force."""
        self.killswitch.check()
        until = subscription_wait_until(self.conn)
        if until:
            raise SubscriptionLimitReached(
                f"Claude plan usage limit reached earlier. Subscription jobs wait until "
                f"{until.astimezone():%a %H:%M} (your time).", until)

    def run(self, agent: str, prompt: str, cwd: Path, tools: list[str], timeout_s: float,
            limit_wait_minutes: float, denied: list[str] = (), plugin_dirs: list[Path] = (),
            system_append: str = "", resume: str | None = None,
            on_tool: Callable[[str, dict[str, Any]], None] | None = None,
            json_schema: str | None = None) -> CliResult:
        self.check_available()
        choice = self.routing.for_agent(agent)
        if choice.route != CLI_ROUTE:
            raise ValueError(f"Agent '{agent}' runs on {choice.route}, not `claude -p`.")
        command = build_command(self.claude_path, choice.model, tools, list(denied), list(plugin_dirs),
                                system_append, resume, json_schema)
        log_event(self.conn, agent, "claude_cli_started", {"model": choice.model, "cwd": str(cwd),
                                                          "tools": tools, "resume": resume})
        started = time.monotonic()
        process = self._launch(command, cwd=str(cwd), env=clean_env(), stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                               errors="replace")
        lines: list[str] = []
        errors: list[str] = []
        readers = [threading.Thread(target=lambda: lines.extend(process.stdout), daemon=True),
                   threading.Thread(target=lambda: errors.extend(process.stderr), daemon=True)]
        for reader in readers:
            reader.start()
        # The prompt goes in on stdin: no Windows command-line length limit, and it never shows in a process list.
        process.stdin.write(prompt)
        process.stdin.close()
        seen = 0
        while process.poll() is None:
            seen = self._report_tools(lines, seen, on_tool)
            if self.killswitch.is_stopped():
                kill_tree(process)
                log_event(self.conn, agent, "claude_cli_stopped", {"why": "town stop", "pid": process.pid})
                raise TownStopped("The Town was stopped mid-job. The `claude -p` run was ended.")
            if time.monotonic() - started > timeout_s:
                kill_tree(process)
                log_event(self.conn, agent, "claude_cli_timeout", {"timeout_s": timeout_s})
                raise CliTimeout(f"`claude -p` took longer than {timeout_s / 60:g} minutes and was ended.")
            time.sleep(POLL_SECONDS)
        for reader in readers:
            reader.join(timeout=10)
        self._report_tools(lines, seen, on_tool)
        return self._finish(agent, choice.model, lines, "".join(errors), time.monotonic() - started,
                            limit_wait_minutes)

    def _report_tools(self, lines: list[str], seen: int, on_tool: Callable | None) -> int:
        end = len(lines)
        if on_tool:
            for line in lines[seen:end]:
                for name, tool_input in _tool_uses(_json(line)):
                    on_tool(name, tool_input)
        return end

    def _finish(self, agent: str, model: str, lines: list[str], stderr: str, wall: float,
                limit_wait_minutes: float) -> CliResult:
        messages = [m for m in (_json(line) for line in lines) if m]
        result = next((m for m in reversed(messages) if m.get("type") == "result"), None)
        if is_usage_limit(result, stderr):
            now = datetime.now(timezone.utc)
            message = ((result or {}).get("result") or stderr).strip()[:300]
            until = parse_reset(message, now, limit_wait_minutes)
            log_event(self.conn, agent, LIMIT_EVENT, {"model": model, "message": message,
                                                     "wait_until": until.isoformat(), "retried": False})
            raise SubscriptionLimitReached(f"Claude plan usage limit reached ({message}). Subscription jobs "
                                           f"wait until {until.astimezone():%a %H:%M} (your time).", until)
        if result is None:
            raise CliError(f"`claude -p` ended without a result: {stderr.strip()[-300:] or 'no output'}")
        tools: dict[str, int] = {}
        skills: list[str] = []
        for message in messages:
            for name, tool_input in _tool_uses(message):
                tools[name] = tools.get(name, 0) + 1
                if name == "Skill" and tool_input.get("skill"):
                    skills.append(str(tool_input["skill"]))
        usage = result.get("usage") or {}
        out = CliResult(
            text=str(result.get("result") or ""), session_id=str(result.get("session_id") or ""), model=model,
            wall_seconds=round(wall, 1), duration_ms=int(result.get("duration_ms") or 0),
            duration_api_ms=int(result.get("duration_api_ms") or 0), num_turns=int(result.get("num_turns") or 0),
            usage={k: int(usage.get(k) or 0) for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens",
                                                        "cache_read_input_tokens")},
            api_equivalent_usd=float(result.get("total_cost_usd") or 0), tool_uses=tools, skills_used=skills,
            permission_denials=len(result.get("permission_denials") or []),
            structured=result.get("structured_output") if isinstance(result.get("structured_output"), dict) else None)
        # Subscription work is $0 billed. The tokens and the API-price equivalent are kept so the
        # Treasurer can see how much of the plan each job uses.
        self.budget.record_cost(agent, model, SUBSCRIPTION_SERVICE,
                                {**out.usage, "turns": out.num_turns, "wall_seconds": out.wall_seconds,
                                 "api_equivalent_usd": round(out.api_equivalent_usd, 4)}, 0.0)
        if result.get("is_error"):
            log_event(self.conn, agent, "claude_cli_error", {"model": model, "result": out.text[:300],
                                                            "status": result.get("api_error_status")})
            raise CliError(f"`claude -p` reported an error: {out.text[:300]}")
        log_event(self.conn, agent, "claude_cli_done", {"model": model, "session_id": out.session_id,
                                                       "wall_seconds": out.wall_seconds, "turns": out.num_turns,
                                                       "usage": out.usage, "tool_uses": tools, "skills": skills})
        return out


def _json(line: str) -> dict[str, Any] | None:
    try:
        value = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _tool_uses(message: dict[str, Any] | None) -> list[tuple[str, dict[str, Any]]]:
    if not message or message.get("type") != "assistant":
        return []
    content = (message.get("message") or {}).get("content") or []
    return [(str(b.get("name")), b.get("input") or {}) for b in content
            if isinstance(b, dict) and b.get("type") == "tool_use"]
