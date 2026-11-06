"""The only path from Town code to the Anthropic API (Haiku-only small jobs).

Every call, in order:
1. checks the kill switch,
2. asks models.yaml which model this agent uses (it must be on the API route),
3. checks the budget (either cap reached, or a Console error earlier, means stop),
4. makes the call,
5. logs the cost and the model to the costs table and the event log.

If the Console says the workspace spend limit is reached or credits ran out,
paid work pauses, a request goes to Charlie, and the call is NOT retried.

The API key is read from .env and handed straight to the client. It's never
put into the process environment, so a `claude -p` subprocess launched later
can't accidentally pick it up and bill the API instead of the subscription.
"""
import sqlite3
from dataclasses import dataclass
from typing import Any, Callable

import anthropic

from engine.budget import API_SERVICE, Budget, PaidWorkPaused
from engine.events import log_event
from engine.killswitch import KillSwitch
from engine.models import API_ROUTE, ModelRouting

# Anthropic bills prompt-cache writes at 1.25x and cache reads at 0.1x the input price.
CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.10

SPEND_LIMIT_PHRASES = ("usage limit", "spend limit", "credit balance", "billing")


class MissingAPIKey(RuntimeError):
    pass


@dataclass(frozen=True)
class Reply:
    text: str
    model: str
    cost_usd: float
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class Turn:
    """One model call in a tool-using conversation: the raw content blocks (text and tool_use), why it stopped,
    and what it cost (already logged)."""
    content: list[Any]
    stop_reason: str
    model: str
    cost_usd: float
    units: dict[str, int]


def is_spend_limit_error(err: Exception) -> bool:
    """The Console's workspace spend limit (HTTP 400) or a billing / out-of-credits error (400 or 402)."""
    status = getattr(err, "status_code", None)
    if status == 402:
        return True
    body = getattr(err, "body", None)
    if isinstance(body, dict) and (body.get("error") or {}).get("type") == "billing_error":
        return True
    message = str(getattr(err, "message", err)).lower()
    return status == 400 and any(phrase in message for phrase in SPEND_LIMIT_PHRASES)


class ClaudeAPI:
    def __init__(
        self,
        conn: sqlite3.Connection,
        routing: ModelRouting,
        budget: Budget,
        killswitch: KillSwitch,
        api_key_loader: Callable[[], str | None],
        client: Any = None,
    ) -> None:
        self.conn = conn
        self.routing = routing
        self.budget = budget
        self.killswitch = killswitch
        self.api_key_loader = api_key_loader
        self._client = client  # tests pass a fake; real runs build one on first use

    def ask(self, agent: str, system: str, prompt: str, max_tokens: int = 2048,
            effort: str | None = None) -> Reply:
        request: dict[str, Any] = {
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
        }
        if effort:
            request["output_config"] = {"effort": effort}
        response, choice, units, dollars = self._call(agent, request)
        text = "".join(block.text for block in response.content if getattr(block, "type", "") == "text")
        return Reply(text, choice.model, dollars, units["input_tokens"], units["output_tokens"])

    def converse(self, agent: str, system: list[dict[str, Any]], messages: list[dict[str, Any]],
                 tools: list[dict[str, Any]] | None = None, max_tokens: int = 1024,
                 tool_choice: dict[str, Any] | None = None, effort: str | None = None) -> Turn:
        """One model call in a conversation that can use tools (Jarvis/Reaper). Same path as ask(): kill switch,
        the agent's API model from models.yaml, the budget check, cost and model logged, and a Console
        spend-limit error pauses paid work and is never retried. `system` is a list of blocks so the fixed
        instructions can carry a prompt-cache marker. The caller runs any tools; this only asks the model."""
        request: dict[str, Any] = {"max_tokens": max_tokens, "system": system, "messages": messages}
        if tools:
            request["tools"] = tools
        if tool_choice:
            request["tool_choice"] = tool_choice
        if effort:
            request["output_config"] = {"effort": effort}
        response, choice, units, dollars = self._call(agent, request)
        return Turn(list(response.content), getattr(response, "stop_reason", None) or "", choice.model, dollars, units)

    def _call(self, agent: str, request: dict[str, Any]) -> tuple[Any, Any, dict[str, int], float]:
        """The one place a request reaches the API: checks first, then the call, then the cost is logged."""
        self.killswitch.check()
        choice = self.routing.for_agent(agent)
        if choice.route != API_ROUTE:
            raise ValueError(f"Agent '{agent}' runs on {choice.route}, not the API.")
        self.budget.check_paid_allowed()
        request = {"model": choice.model, **request}

        try:
            response = self._get_client().messages.create(**request)
        except anthropic.APIStatusError as err:
            if is_spend_limit_error(err):
                self._pause_for_console(agent, choice.model, err)
                raise PaidWorkPaused("The Claude Console refused the call (spend limit or credits). "
                                     "Paid work paused; your decision is waiting in `town pending`.") from err
            log_event(self.conn, agent, "api_error", {"model": choice.model, "status": err.status_code,
                                                      "error": str(err.message)})
            raise
        except anthropic.APIConnectionError as err:
            log_event(self.conn, agent, "api_error", {"model": choice.model, "error": f"connection: {err}"})
            raise

        usage = response.usage
        units = {
            "input_tokens": usage.input_tokens or 0,
            "output_tokens": usage.output_tokens or 0,
            "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", 0) or 0,
            "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
        }
        dollars = (
            units["input_tokens"] * choice.input_usd_per_mtok
            + units["cache_creation_input_tokens"] * choice.input_usd_per_mtok * CACHE_WRITE_MULTIPLIER
            + units["cache_read_input_tokens"] * choice.input_usd_per_mtok * CACHE_READ_MULTIPLIER
            + units["output_tokens"] * choice.output_usd_per_mtok
        ) / 1_000_000
        self.budget.record_cost(agent, choice.model, API_SERVICE, units, dollars)
        self.budget.after_spend()
        return response, choice, units, dollars

    def _get_client(self) -> Any:
        if self._client is None:
            key = self.api_key_loader()
            if not key:
                raise MissingAPIKey("No API key. Copy .env.example to .env and paste your Console key there.")
            # max_retries=0: the SDK never retries on its own, so a refused or failed call
            # is never repeated (and never billed twice without being logged).
            self._client = anthropic.Anthropic(api_key=key, max_retries=0)
        return self._client

    def _pause_for_console(self, agent: str, model: str, err: anthropic.APIStatusError) -> None:
        message = str(err.message)
        cause = "out of credits" if "credit" in message.lower() or err.status_code == 402 else "Console spend limit"
        log_event(self.conn, agent, "api_spend_limit_hit", {"model": model, "status": err.status_code,
                                                            "error": message, "retried": False})
        self.budget.pause(f"the Claude Console refused paid calls ({cause})",
                          self.budget.console_request(model, cause, message), kind="console")
