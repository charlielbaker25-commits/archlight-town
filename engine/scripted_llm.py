"""A scripted stand-in for the Anthropic client, for tests and the offline Reaper test set. Never real work.

It answers `messages.create(**request)` like the SDK does (content blocks, a stop reason, token usage), from a
script instead of a model, so the Town's own code (tools, checks, confirmations, logging) can be tested with
no network and no cost. Each step of the script is one model reply:
- "some text"                                  -> a text answer
- {"tool": "get_queue", "input": {...}}        -> one tool call
- [{"tool": ...}, {"tool": ...}]               -> several tool calls in one reply
- a function(request) -> any of the above       -> decided from what the model was sent
- an Exception instance                        -> the call raises it (an API outage, a spend-limit error)
When the script runs out, it answers "Acknowledged." Every request is kept in `.requests`.
"""
from types import SimpleNamespace
from typing import Any

USAGE = {"input_tokens": 1200, "output_tokens": 120, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}


class ScriptedClient:
    def __init__(self, script: list[Any] | None = None) -> None:
        self.script = list(script or [])
        self.requests: list[dict[str, Any]] = []
        self.messages = self   # the SDK's shape: client.messages.create(...)

    @property
    def calls(self) -> int:
        return len(self.requests)

    def create(self, **request: Any) -> SimpleNamespace:
        self.requests.append(request)
        step = self.script.pop(0) if self.script else "Acknowledged."
        if callable(step):
            step = step(request)
        if isinstance(step, Exception):
            raise step
        calls = step if isinstance(step, list) else [step] if isinstance(step, dict) else []
        if request.get("tool_choice", {}).get("type") == "none":
            calls = []    # the model was told to answer now; a real one can't call tools here either
        if calls:
            content = [SimpleNamespace(type="tool_use", id=f"tool-{len(self.requests)}-{i}", name=c["tool"],
                                       input=c.get("input", {})) for i, c in enumerate(calls)]
            return SimpleNamespace(content=content, stop_reason="tool_use", usage=SimpleNamespace(**USAGE))
        text = step if isinstance(step, str) else "Acknowledged."
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason="end_turn",
                               usage=SimpleNamespace(**USAGE))
