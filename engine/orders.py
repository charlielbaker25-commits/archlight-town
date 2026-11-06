"""Orders through Reaper: jobs Charlie asks for by talking ("scout salons in Clayton", "build lead 12").

The same path as decisions (engine/decisions.py): Reaper's `propose_orders` tool only proposes; plain code
checks each order against the same rules and daily caps the `town` command uses; only Charlie's confirmation
starts it, logged as his order via Reaper.

Which orders exist is the pack's call (its OrderBook). Some things are NEVER orders, whatever a pack says:
publishing, takedowns, resuming, limit changes and do-not-contact changes stay terminal commands.

Jobs run in the background, one at a time (a build can take minutes), each with its own Town (its own
database connection), so the chat and the map keep working. Every job checks the kill switch before it
starts, and the jobs themselves stop on it the way the commands do.
"""
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from engine.events import log_event

JARVIS = "jarvis"
# Never through Reaper, whatever a pack lists (Charlie, 2026-10-27). Matched on the order's name with
# separators removed, so "propose-publish" (only a proposal) is allowed but "publish" is not.
FORBIDDEN = frozenset({"publish", "publishpreview", "publishhomepage", "takedown", "resume", "limit",
                       "changelimit", "setlimit", "dnc", "dncadd", "dncremove", "donotcontact", "stop", "send",
                       "sendemail", "approve", "reject", "edit"})


# "go", "start autopilot", "run the town" (Charlie, 2026-11-05): plain code turns these words into a proposal for
# the pack's order with this name (the Town's own loop), which only Charlie's Confirm starts. A pack without it
# simply has no "go".
GO_ORDER = "autopilot"
GO_WORDS = re.compile(r"^\s*(?:go|start (?:the )?autopilot|run the town|start the town|"
                      r"run autopilot)\s*[.!]*\s*$", re.IGNORECASE)


class OrderRefused(ValueError):
    """An order that can't run (a rule, a cap, or not an order at all). The message says why."""


@dataclass(frozen=True)
class Order:
    name: str
    params: dict[str, Any] = field(default_factory=dict)

    def key(self) -> str:
        return f"{self.name}({', '.join(f'{k}={v}' for k, v in sorted(self.params.items()))})"


class OrderBook(Protocol):
    """A pack's orders. check() never changes anything; run() does the job like the matching command."""
    names: frozenset[str]

    def check(self, town: Any, order: Order) -> str: ...      # the plain line of what will run; or OrderRefused

    def run(self, town: Any, order: Order) -> str: ...        # what happened, in a line


def normalized(name: str) -> str:
    return "".join(ch for ch in str(name).lower() if ch.isalnum())


def parse(raw: Any) -> tuple[list[Order], list[str]]:
    items = raw.get("orders") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return [], ["the order list wasn't a list"]
    orders, refused = [], []
    for item in items[:10]:
        if not isinstance(item, dict) or not str(item.get("order") or "").strip():
            refused.append(f"not an order: {str(item)[:60]}")
            continue
        params = item.get("params") if isinstance(item.get("params"), dict) else {}
        orders.append(Order(str(item["order"]).strip().lower().replace("-", "_"), dict(params)))
    if len(items) > 10:
        refused.append("more than 10 orders at once; ask for fewer")
    return orders, refused


def check(town: Any, book: OrderBook | None, order: Order) -> str:
    """The line Charlie will see, or OrderRefused. Forbidden names are refused before the pack is asked."""
    if normalized(order.name) in FORBIDDEN:
        raise OrderRefused(f"'{order.name}' is never an order through Reaper; it stays a terminal command")
    if book is None or order.name not in book.names:
        raise OrderRefused(f"'{order.name}' isn't an order Reaper can give"
                           + (f" (orders: {', '.join(sorted(book.names))})" if book else ""))
    return book.check(town, order)


class Runner:
    """Runs confirmed orders in the background, one at a time, each with its own Town."""

    def __init__(self, open_town: Callable[[], Any], book: OrderBook, ordered_by: str) -> None:
        self.open_town = open_town
        self.book = book
        self.ordered_by = ordered_by
        self._one_at_a_time = threading.Lock()
        self.threads: list[threading.Thread] = []

    def launch(self, order: Order) -> str:
        thread = threading.Thread(target=self._run, args=(order,), name=f"order-{order.name}")
        self.threads.append(thread)
        thread.start()
        return f"Order started in the background: {order.key()}. Watch it on the map or with `town log`."

    def wait(self, timeout: float | None = None) -> None:
        for thread in self.threads:
            thread.join(timeout)

    def _run(self, order: Order) -> None:
        with self._one_at_a_time:
            run_now(self.open_town, self.book, order, self.ordered_by)


def run_now(open_town: Callable[[], Any], book: OrderBook, order: Order, ordered_by: str) -> str:
    """One order, start to finish, logged. Never raises: a failure is logged as order_failed."""
    town = open_town()
    try:
        details = {"order": order.name, "params": order.params, "ordered_by": ordered_by, "via": "reaper"}
        if town.killswitch.is_stopped():
            log_event(town.conn, JARVIS, "order_cancelled", {**details, "why": "the Town is stood down"})
            return f"{order.key()}: not started, the Town is stood down."
        try:
            book.check(town, order)   # again, right before it runs: a cap may have filled since Charlie confirmed
        except OrderRefused as err:
            log_event(town.conn, JARVIS, "order_cancelled", {**details, "why": str(err)})
            return f"{order.key()}: not started: {err}"
        log_event(town.conn, JARVIS, "order_started", details)
        try:
            result = book.run(town, order)
        except OrderRefused as err:   # the agent's own rule said no (e.g. already done): a refusal, not an error
            log_event(town.conn, JARVIS, "order_cancelled", {**details, "why": str(err)})
            return f"{order.key()}: refused: {err}"
        except Exception as err:   # the job failed; recorded, never retried
            log_event(town.conn, JARVIS, "order_failed", {**details, "error": f"{type(err).__name__}: {err}"[:300]})
            return f"{order.key()}: FAILED: {err}"
        log_event(town.conn, JARVIS, "order_finished", {**details, "result": str(result)[:500]})
        return f"{order.key()}: {result}"
    finally:
        town.close()
