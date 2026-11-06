"""The Treasurer's budget check: counts this month's spend and pauses paid work at either cap.

Two caps, both read from limits.yaml every time (never copied into code):
- API spend (the Town's own count of Claude API costs): $9/month
- Total paid spend (everything paid): $25/month

At either cap, or when the Claude Console says the spend limit or credits ran out,
paid work pauses and ONE request goes into Charlie's approval queue. Nothing paid
runs again until Charlie approves it (a cap raise, or "resume" once he has fixed
the Console). If he rejects it, paid work stays paused, and the next paid job
files a fresh request, so the decision is always waiting in `town pending`.
These are plain number comparisons, never a model's judgment.

Fail closed: if this month's spend can't be read, paid work counts as paused.
"""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from engine.db import utc_now
from engine.events import log_event
from engine.limits import Limits
from engine.mayor import Mayor, MayorError

API_SERVICE = "claude_api"
API_CAP = "money.api_monthly_cap_usd"
TOTAL_CAP = "money.total_monthly_cap_usd"
REQUEST_TYPES = ("raise_cap", "resume_paid_work")
TREASURER = "treasurer"


class PaidWorkPaused(RuntimeError):
    """Paid calls are paused. Charlie decides what happens next."""


def month_start_utc(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return f"{now:%Y-%m}-01T00:00:00Z"


def _spend_since(conn: sqlite3.Connection, start: str) -> tuple[float, float]:
    """(API spend, total spend) in one database's costs table since `start`."""
    api = conn.execute(
        "SELECT COALESCE(SUM(dollar_cost), 0) FROM costs WHERE ts >= ? AND service = ?",
        (start, API_SERVICE)).fetchone()[0]
    total = conn.execute(
        "SELECT COALESCE(SUM(dollar_cost), 0) FROM costs WHERE ts >= ?", (start,)).fetchone()[0]
    return float(api), float(total)


class Budget:
    def __init__(self, conn: sqlite3.Connection, limits: Limits, mayor: Mayor,
                 shared_databases: tuple[Path, ...] = ()) -> None:
        self.conn = conn
        self.limits = limits
        self.mayor = mayor
        # Other instances (the dev Town) bill the same API key and budget, so their spend counts here too.
        self.shared_databases = shared_databases

    # ---- reading ----

    def month_spend(self) -> tuple[float, float]:
        """(API spend, total spend) so far this UTC calendar month, including the other instances'."""
        api, total = _spend_since(self.conn, month_start_utc())
        shared_api, shared_total = self.shared_spend()
        return api + shared_api, total + shared_total

    def shared_spend(self) -> tuple[float, float]:
        """This month's spend recorded by the other instances. A database that doesn't exist
        yet counts as $0; one that exists but can't be read raises, so the check fails closed."""
        api = total = 0.0
        for path in self.shared_databases:
            if not Path(path).exists():
                continue
            # Read-only, so this instance can never change the other one's records.
            other = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)
            try:
                a, t = _spend_since(other, month_start_utc())
            finally:
                other.close()
            api, total = api + a, total + t
        return api, total

    def paused_reason(self) -> str | None:
        """Why paid work is paused, or None if it isn't. The latest pause/resume event decides."""
        row = self.conn.execute(
            "SELECT event_type, details FROM events WHERE event_type IN ('paid_work_paused', 'paid_work_resumed')"
            " ORDER BY id DESC LIMIT 1").fetchone()
        if row is None or row["event_type"] == "paid_work_resumed":
            return None
        return json.loads(row["details"]).get("reason", "paused")

    def paused_kind(self) -> str | None:
        """'cap' or 'console' while paused (None if running), so each pause is lifted the right way."""
        row = self.conn.execute(
            "SELECT event_type, details FROM events WHERE event_type IN ('paid_work_paused', 'paid_work_resumed')"
            " ORDER BY id DESC LIMIT 1").fetchone()
        if row is None or row["event_type"] == "paid_work_resumed":
            return None
        return json.loads(row["details"]).get("kind", "cap")

    def caps(self) -> tuple[float, float]:
        return float(self.limits.get(API_CAP)), float(self.limits.get(TOTAL_CAP))

    # ---- the check every paid call makes first ----

    def check_paid_allowed(self) -> None:
        try:
            paused = self.paused_reason()
            api, total = self.month_spend()
            api_cap, total_cap = self.caps()
        except Exception as err:
            self._log_safely("budget_check_failed", {"error": f"{type(err).__name__}: {err}"})
            raise PaidWorkPaused(f"Couldn't read this month's spend ({err}). Paid work paused until it can.") from err
        self._enforce_caps(api, api_cap, total, total_cap)
        if paused:
            # Not over a cap, but still paused (a Console error, or a rejected request).
            # Make sure Charlie has a decision waiting.
            self._file_request(self.resume_request(paused))
            raise PaidWorkPaused(f"Paid work paused: {paused}. Your decision is waiting in `town pending`.")

    def after_spend(self) -> None:
        """After a cost is logged: pause right away if it crossed a cap, so the request reaches Charlie now."""
        try:
            api, total = self.month_spend()
            api_cap, total_cap = self.caps()
            self._enforce_caps(api, api_cap, total, total_cap)
        except PaidWorkPaused:
            pass  # recorded; the next paid call will be refused
        except Exception as err:
            self._log_safely("budget_check_failed", {"error": f"{type(err).__name__}: {err}"})
            # the next check_paid_allowed fails closed

    # ---- recording ----

    def record_cost(self, agent: str, model: str, service: str, units: dict, dollars: float) -> None:
        self.conn.execute(
            "INSERT INTO costs (ts, agent, model, service, units, dollar_cost) VALUES (?, ?, ?, ?, ?, ?)",
            (utc_now(), agent, model, service, json.dumps(units, sort_keys=True), dollars))
        log_event(self.conn, TREASURER, "cost_logged",
                  {"agent": agent, "model": model, "service": service, "units": units, "dollars": round(dollars, 6)})

    # ---- pausing and resuming ----

    def pause(self, reason: str, request: dict, kind: str = "cap") -> None:
        """Pause paid work and put one request in Charlie's queue (never a second while one waits).
        kind is 'cap' (a budget cap) or 'console' (the Console refused: spend limit or credits)."""
        if self.paused_reason() is None:
            log_event(self.conn, TREASURER, "paid_work_paused", {"reason": reason, "kind": kind})
        self._file_request(request)

    def resume(self, why: str) -> None:
        log_event(self.conn, TREASURER, "paid_work_resumed", {"why": why})

    def console_request(self, model: str, cause: str, error_message: str) -> dict:
        return {
            "action_type": "resume_paid_work",
            "target": "Charlie: Claude Console",
            "content": {"cause": cause, "error": error_message,
                        "what_to_do": "Raise the workspace spend limit or add credits at console.anthropic.com "
                                      "(only you can), then approve this to resume paid calls."},
            "reason": f"The Console refused a {model} call ({cause}). Paid work is paused and the call was not "
                      "retried. Fix it in the Console, then approve; or reject to stay paused.",
        }

    def resume_request(self, paused_reason: str) -> dict:
        return {
            "action_type": "resume_paid_work",
            "target": "Charlie: paid work",
            "content": {"cause": paused_reason,
                        "what_to_do": "Approve to resume paid calls, or reject to stay paused."},
            "reason": f"Paid work is still paused ({paused_reason}) and a paid job is waiting.",
        }

    # ---- internals ----

    def _enforce_caps(self, api: float, api_cap: float, total: float, total_cap: float) -> None:
        if api >= api_cap:
            self.pause(f"API spend ${api:.2f} reached the ${api_cap:.2f} monthly cap",
                       self._raise_request(API_CAP, "monthly API cap", api_cap, api))
            raise PaidWorkPaused(f"API cap reached (${api:.2f} of ${api_cap:.2f}). Cap-raise request in `town pending`.")
        if total >= total_cap:
            self.pause(f"Total spend ${total:.2f} reached the ${total_cap:.2f} monthly cap",
                       self._raise_request(TOTAL_CAP, "monthly total budget", total_cap, total))
            raise PaidWorkPaused(f"Budget cap reached (${total:.2f} of ${total_cap:.2f}). Cap-raise request in `town pending`.")
        self._warn_once(api, api_cap, "api")
        self._warn_once(total, total_cap, "total")

    def _raise_request(self, limit_key: str, label: str, cap: float, spent: float) -> dict:
        step = float(self.limits.get("money.cap_raise_step_usd"))
        new_value = round(max(cap, spent) + step, 2)
        calls, avg = self._month_api_calls()
        more = f"about {int(step / avg)}" if avg > 0 else "many"
        expected = (f"Keeps paid jobs running. This month: {calls} paid calls averaging ${avg:.4f} each, "
                    f"so +${step:.2f} covers {more} more.")
        console_note = ""
        if limit_key == API_CAP and new_value > float(self.limits.get("money.console_api_limit_usd")):
            console_note = " The Console's own limit must also be raised (only you can, at console.anthropic.com)."
        return {
            "action_type": "raise_cap",
            "target": f"Charlie: {label}",
            "content": {"limit": limit_key, "current_value": cap, "new_value": new_value,
                        "spent_this_month": round(spent, 4), "expected_return": expected},
            "reason": (f"Paid work paused: ${spent:.2f} spent of the ${cap:.2f} {label}. Approve to raise it to "
                       f"${new_value:.2f}, edit new_value to choose another amount, or reject to stay paused."
                       + console_note),
        }

    def _file_request(self, request: dict) -> None:
        if self._open_request() is not None:
            return
        try:
            self.mayor.propose(TREASURER, request["action_type"], request["target"], request["content"],
                               request["reason"])
        except MayorError as err:  # e.g. the Town was stopped mid-call; the pause still stands
            log_event(self.conn, TREASURER, "cap_request_not_filed", {"why": str(err)})

    def _month_api_calls(self) -> tuple[int, float]:
        row = self.conn.execute(
            "SELECT COUNT(*), COALESCE(AVG(dollar_cost), 0) FROM costs WHERE ts >= ? AND service = ?",
            (month_start_utc(), API_SERVICE)).fetchone()
        return int(row[0]), float(row[1])

    def _open_request(self) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT id FROM actions WHERE status IN ('proposed', 'approved') AND action_type IN (?, ?)",
            REQUEST_TYPES).fetchone()

    def _warn_once(self, spent: float, cap: float, which: str) -> None:
        """Log one early-warning event per cap per month at 80% (BLUEPRINT 4.5)."""
        if spent < cap * float(self.limits.get("money.early_warning_fraction")):
            return
        already = self.conn.execute(
            "SELECT 1 FROM events WHERE event_type = 'budget_warning' AND ts >= ? AND details LIKE ?",
            (month_start_utc(), f'%"cap": "{which}"%')).fetchone()
        if not already:
            log_event(self.conn, TREASURER, "budget_warning", {"cap": which, "spent": round(spent, 4), "limit": cap})

    def _log_safely(self, event_type: str, details: dict) -> None:
        try:
            log_event(self.conn, TREASURER, event_type, details)
        except Exception:
            pass  # the database itself may be the problem; the refusal still happens
