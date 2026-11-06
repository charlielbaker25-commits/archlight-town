"""Opens one Town instance: its database, kill switch, limits, model routing,
Mayor, budget, API wrapper, `claude -p` runner and executor, wired together. The launcher hands in
the pack's pieces, so this file stays business-neutral.
"""
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from engine.budget import Budget
from engine.claude_cli import ClaudeCLI
from engine.db import connect, init_db
from engine.executor import Executor, Handler
from engine.killswitch import KillSwitch
from engine.limit_actions import limit_handlers
from engine.profile import profile_handlers
from engine.limits import Limits
from engine.llm import ClaudeAPI
from engine.mayor import Mayor
from engine.models import ModelRouting
from engine.settings import Settings


@dataclass
class Town:
    settings: Settings
    conn: sqlite3.Connection
    killswitch: KillSwitch
    limits: Limits
    routing: ModelRouting
    mayor: Mayor
    budget: Budget
    llm: ClaudeAPI
    executor: Executor
    cli: ClaudeCLI   # `claude -p` on Charlie's subscription (Builder, Critic...)
    actor: str = "charlie"  # who is running this command: "charlie" or "claude-code"

    def close(self) -> None:
        self.conn.close()


def open_town(
    settings: Settings,
    pack_schemas: tuple[str, ...],
    handlers: dict[str, Handler],
    limits_file: Path,
    models_file: Path,
    api_key_loader: Callable[[], str | None] = lambda: None,
    api_client: Any = None,
    actor: str = "charlie",
    live_external_types: frozenset[str] = frozenset(),
) -> Town:
    """`live_external_types`: the external action types the pack has deliberately switched on (they
    really run once approved). Every other external type stays dry-run."""
    conn = connect(settings.database)
    init_db(conn, pack_schemas)
    killswitch = KillSwitch(settings.killswitch_file)
    limits = Limits(limits_file)
    routing = ModelRouting(models_file)
    mayor = Mayor(conn, town_stopped=killswitch.is_stopped, actor=actor)
    budget = Budget(conn, limits, mayor, shared_databases=settings.budget_shared_with)
    llm = ClaudeAPI(conn, routing, budget, killswitch, api_key_loader, client=api_client)
    all_handlers = {**limit_handlers(limits, budget), **profile_handlers(), **handlers}
    return Town(
        settings=settings,
        conn=conn,
        killswitch=killswitch,
        limits=limits,
        routing=routing,
        mayor=mayor,
        budget=budget,
        llm=llm,
        executor=Executor(conn, all_handlers, town_stopped=killswitch.is_stopped,
                          live_external_types=live_external_types),
        cli=ClaudeCLI(conn, routing, budget, killswitch, claude_path=shutil.which("claude") or "claude"),
        actor=actor,
    )
