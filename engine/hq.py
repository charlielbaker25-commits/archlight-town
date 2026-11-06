"""Reaper HQ: the local server behind the page (`town hq`). The page itself comes in session R2.

Safety, in plain code:
- It listens on 127.0.0.1 only (this laptop; nothing else on the network can reach it).
- A new random token is made at every launch, and every request must carry it (the `X-Town-Token` header, or
  `?token=` for the page itself and the live stream, which browsers can't give headers).
- The Host header must be this server (stops "DNS rebinding", where another website points its own name at
  127.0.0.1), any Origin must be this server, and a POST must carry this server's Origin. There is no CORS, so
  browsers refuse to let other websites read anything from it.
- Decisions use the same plain-code checks as the terminal (engine/decisions.py): the chat only proposes;
  only a separate /api/confirm request (Charlie clicking Confirm) carries a proposal out.
- Stand down has an endpoint. Resume does NOT: resuming is only `town resume` in the terminal.
- Every request opens its own Town (SQLite connections can't be shared across the server's threads), and the
  chat and decisions run one at a time.
- A built site can be looked at under `/site/<token>/<build id>/` (the pack's site_file: static files of a real
  build's dist/ only, read-only, no directory listing). Those pages get their own strict policy: no script runs
  at all (a site made from outside data never runs code on this server's address), no forms, no connections.
- The page's own files (scripts, styles, art, fonts) come from the project's `hq/` folder under
  `/hq/<token>/...`: the token rides in the path, so plain JavaScript modules can import each other and every
  file still needs the token. Only listed folders and file types are served, never anything outside `hq/`.
"""
import base64
import hashlib
import json
import re
import secrets
import socket
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
import anyio
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from engine import catchup
from engine.events import log_event
from engine import decisions as dec
from engine import facts as town_facts
from engine import profile
from engine.flows import flows_after, last_event_id
from engine.jarvis import HQ_CONFIRM_HINT, Reaper, scrub
from engine.signature import after_batch
from engine.standdown import is_stand_down, stand_down

HOST = "127.0.0.1"
TOKEN_HEADER = "x-town-token"
# The page's files: the project's hq/ folder (three.js and the fonts are installed there with `npm ci --prefix hq`).
STATIC_DIR = Path(__file__).resolve().parent.parent / "hq"
STATIC_PREFIX = "/hq/"
SITE_PREFIX = "/site/"
# A built site shown in HQ (Sites panel, a package card's View): its files only, and nothing on it can run code,
# send a form or connect anywhere. Styles, images and fonts from the same server only.
SITE_CSP = ("default-src 'none'; script-src 'none'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "font-src 'self' data:; media-src 'self'; connect-src 'none'; object-src 'none'; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'none'")
STATIC_FOLDERS = ("web/", "assets/", "node_modules/three/build/", "node_modules/three/examples/jsm/loaders/",
                  "node_modules/three/examples/jsm/utils/", "node_modules/three/examples/jsm/controls/",
                  "node_modules/@fontsource/")
STATIC_TYPES = {".js": "text/javascript", ".css": "text/css", ".png": "image/png", ".woff2": "font/woff2",
                ".woff": "font/woff", ".json": "application/json", ".glb": "model/gltf-binary"}
PAGE_FILE = "web/index.html"
# Installed by `npm ci --prefix hq`; without them the page can't draw, so the plain notice says what to run.
NEEDED_FILES = ("node_modules/three/build/three.module.js",
                "node_modules/@fontsource/barlow-condensed/files/barlow-condensed-latin-700-normal.woff2")
BASE_MARK = "__HQ_BASE__"
FLOWS_ON_LOAD_HOURS = 24
STREAM_POLL_SECONDS = 1.0
STREAM_FACTS_EVERY_SECONDS = 5.0
STREAM_PING_SECONDS = 2.0      # often enough that a closed tab frees its stream slot within seconds
MAX_STREAMS = 4            # open live streams (browser tabs); more are refused so they can't tie up the server
MAX_PROPOSALS = 20
STAND_DOWN_THREADS = 2     # stand-down requests get their own worker threads, never shared with anything else         # waiting confirmations kept; the oldest goes first
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data: blob:; font-src 'self'; connect-src 'self'; media-src 'none'; "
       "object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
# The page tells the browser where "three" lives with one small import map (a JSON list of file addresses; it
# runs nothing). Browsers treat it like an inline script, so the page's policy allows exactly that one map by
# its SHA-256 hash, worked out by the server at launch: nothing else inline can run.
IMPORT_MAP = re.compile(r'<script type="importmap">(.*?)</script>', re.S)
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",        # the token is in the page's address; never send it anywhere
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    # No inline scripts. Images may be data: or blob: (three.js unpacks the models' textures that way); the
    # models themselves are fetched from this server (connect-src 'self').
    "Content-Security-Policy": CSP,
    "Permissions-Policy": "microphone=(self), camera=(), geolocation=()",
}


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


class SpeakIn(BaseModel):
    text: str = Field(min_length=1, max_length=600)


class ProposalIn(BaseModel):
    proposal_id: str = Field(min_length=1, max_length=64)


class FeedbackIn(BaseModel):
    """A thumbs up or down on one of Reaper's replies, optionally with an explicit change ("shorter")."""
    about: str = Field(default="reaper reply", max_length=40)
    rating: int = Field(ge=-1, le=1)
    excerpt: str = Field(default="", max_length=300)
    change: str | None = Field(default=None, max_length=20)


class CatchUpIn(BaseModel):
    auto: bool = True


class DecideIn(BaseModel):
    """One click on a "Needs you" card: approve or reject these items (one card can hold a batch of new leads)."""
    action_ids: list[int] = Field(min_length=1, max_length=50)
    decision: str = Field(pattern="^(approve|reject)$")
    reason: str = Field(default="", max_length=500)


class ReviseIn(BaseModel):
    proposal_id: str = Field(min_length=1, max_length=64)
    action_id: int
    reason: str = Field(min_length=1, max_length=500)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((HOST, 0))
        return int(sock.getsockname()[1])


def new_token() -> str:
    return secrets.token_urlsafe(32)


class HQState:
    """What lives as long as the server: the one Reaper conversation and the proposals waiting for Confirm."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.history: list[dict[str, str]] = []
        self.proposals: dict[str, dec.Proposal] = {}
        # Per waiting proposal: whose words each rejection reason is ("charlie" or "reaper"), for the card.
        self.sources: dict[str, dict[int, str]] = {}
        self.streams = 0

    def keep(self, proposal: dec.Proposal, sources: dict[int, str]) -> None:
        with self.lock:
            self.proposals[proposal.id] = proposal
            self.sources[proposal.id] = sources
            while len(self.proposals) > MAX_PROPOSALS:
                oldest = next(iter(self.proposals))
                self.proposals.pop(oldest)
                self.sources.pop(oldest, None)

    def drop(self, proposal_id: str) -> dec.Proposal | None:
        with self.lock:
            self.sources.pop(proposal_id, None)
            return self.proposals.pop(proposal_id, None)


def create_app(open_town: Callable[[], Any], view: Any, token: str, port: int, can_decide: bool,
               page_html: str | None = None, open_reader: Callable[[], Any] | None = None,
               rules: dec.Rules = dec.Rules(), static_dir: Path | None = STATIC_DIR, voice: Any = None,
               timing: Callable[[str], None] | None = None) -> FastAPI:
    """`open_town()` opens this instance's Town (with the launcher's real-vs-fake client rule already applied).
    `open_reader()` opens a READ-ONLY connection to its database, for the live stream's polling (by default
    the stream opens a Town). `voice` makes Reaper's spoken lines (engine/voice.py); without it the page uses the
    browser's own voice. `static_dir` holds the page (hq/web/index.html) and its files; without it, or
    if the page file is missing, the plain placeholder page is served. `timing` (off unless given; `town hq
    --timing`, and the page tests) gets one plain line per step: each /api request, opening the Town, waiting for
    the chat lock, Reaper's turn, a stand-down, a voice line. Lines carry no token, message or reply."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)   # no public API docs either
    rules = dec.worded(rules, view)   # the same words for proposing, revising and confirming
    state = HQState()
    origins = {f"http://{HOST}:{port}", f"http://localhost:{port}"}
    hosts = {f"{HOST}:{port}", f"localhost:{port}"}
    timed = _Timed(timing)

    @app.middleware("http")
    async def guard(request: Request, call_next: Callable) -> Any:
        # Only /api paths are timed: the page's own files carry the token in their path.
        with timed(f"{request.method} {request.url.path}", on=request.url.path.startswith("/api/")):
            return await checked(request, call_next)

    async def checked(request: Request, call_next: Callable) -> Any:
        if request.headers.get("host", "") not in hosts:
            return _refuse(403, "wrong host")
        path = request.url.path
        in_path = (path.split("/")[2] if path.startswith((STATIC_PREFIX, SITE_PREFIX)) and path.count("/") >= 3
                   else "")
        given = request.headers.get(TOKEN_HEADER) or request.query_params.get("token") or in_path
        if not secrets.compare_digest(given.encode(), token.encode()):
            return _refuse(401, "missing or wrong token")
        origin = request.headers.get("origin")
        if origin is not None and origin not in origins:
            return _refuse(403, "foreign origin")
        if request.method != "GET" and origin is None:
            return _refuse(403, "a change needs this page's origin")
        if request.headers.get("sec-fetch-site") not in (None, "same-origin", "none"):
            return _refuse(403, "cross-site request")
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            if name == "Content-Security-Policy" and name in response.headers:
                continue   # the page's own policy (the same, plus its import map's hash)
            response.headers[name] = value
        return response

    def with_town(work: Callable[[Any], Any]) -> Any:
        with timed("open the Town"):
            town = open_town()
        try:
            return work(town)
        finally:
            town.close()

    @app.get("/", response_class=HTMLResponse)
    def page() -> HTMLResponse:
        if page_html:
            return HTMLResponse(page_html)
        page_file = static_dir / PAGE_FILE if static_dir else None
        if page_file is None or not all((static_dir / f).is_file() for f in (PAGE_FILE, *NEEDED_FILES)):
            return HTMLResponse(PLACEHOLDER_PAGE)
        # The page's files live under /hq/<token>/, so each one needs the token too.
        html = page_file.read_text(encoding="utf-8").replace(BASE_MARK, f"{STATIC_PREFIX}{token}/")
        return HTMLResponse(html, headers={"Content-Security-Policy": page_csp(html)})

    @app.get(STATIC_PREFIX + "{given}/{path:path}")
    def static_file(given: str, path: str) -> FileResponse:
        target = _static_path(static_dir, path)
        if target is None:
            raise HTTPException(404, "No such file.")
        return FileResponse(target, media_type=STATIC_TYPES[target.suffix.lower()])

    @app.get("/api/facts")
    def facts() -> JSONResponse:
        def work(town: Any) -> dict[str, Any]:
            since = town_facts.hours_ago(FLOWS_ON_LOAD_HOURS)
            # The newest id first, then the recent past up to it (engine/flows.py flows_after): an event that lands
            # meanwhile has a higher id, so it reaches the page through the stream, which starts after `newest`.
            newest = last_event_id(town.conn)
            recent, _ = flows_after(town.conn, view, 0, since_ts=since, up_to=newest)
            return {"facts": town_facts.town_facts(town, view),
                    "flows": [f.to_dict() for f in recent][-200:],
                    "last_event_id": newest,
                    "buildings": [a.__dict__ for a in view.agents],
                    "can_decide": can_decide}
        return _json(with_town(work))

    @app.get("/api/layout")
    def layout() -> JSONResponse:
        """Where the pack puts each building on the village map (plain data, no database)."""
        laid_out = getattr(view, "village_layout", None)
        return _json(laid_out() if callable(laid_out) else {"buildings": [], "roads": []})

    @app.get("/api/village")
    def village() -> JSONResponse:
        return _json(with_town(lambda town: town_facts.village(town, view)))

    @app.get("/api/agent/{name}")
    def agent(name: str) -> JSONResponse:
        detail = with_town(lambda town: town_facts.agent_detail(town, view, name[:40]))
        if detail is None:
            raise HTTPException(404, "No such agent.")
        return _json(detail)

    @app.get("/api/flows")
    def flows(after: int = 0, since: str | None = None) -> JSONResponse:
        def work(town: Any) -> dict[str, Any]:
            found, newest = flows_after(town.conn, view, after, since_ts=since)
            return {"flows": [f.to_dict() for f in found], "last_event_id": newest}
        return _json(with_town(work))

    @app.get("/api/stream")
    def stream(after: int = -1, once: bool = False) -> Any:
        with state.lock:
            if state.streams >= MAX_STREAMS:
                return _refuse(429, "too many live streams open; close a tab")

        def counted() -> Iterator[str]:
            # Counted inside the stream, so a browser that drops before it starts never holds a slot.
            with state.lock:
                state.streams += 1
            try:
                yield from _events(with_town, view, after, once, open_reader)
            finally:
                with state.lock:
                    state.streams -= 1
        return StreamingResponse(counted(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})

    # The chat and decisions run one at a time (chat_lock). Stand down never waits for them.
    stand_down_limiter = anyio.CapacityLimiter(STAND_DOWN_THREADS)
    chat_lock = threading.Lock()

    def card(town: Any, proposal: dec.Proposal) -> dict[str, Any]:
        """What the page shows for a waiting proposal: R1's validated proposal plus its cards (plain code)."""
        with state.lock:
            sources = dict(state.sources.get(proposal.id, {}))
        return {**proposal.to_dict(), "cards": dec.cards(town, proposal, sources)}

    def halt(said: str) -> dict[str, Any]:
        # No chat_lock: a stand-down never waits behind a model call or a publish. stand_down() writes the
        # kill-switch file first; a running confirm checks it before each execution.
        with timed("stand down"):
            reply = with_town(lambda town: stand_down(town, said))
        with state.lock:
            state.proposals.clear()   # nothing waits for a Confirm on a stood-down Town
            state.sources.clear()
        return {"reply": reply, "mode": "stand_down", "proposal": None, "cost_usd": 0.0, "seconds": 0.0,
                "model": None}

    @app.post("/api/chat")
    async def chat(body: ChatIn) -> JSONResponse:
        # "Reaper, stand down" (typed or spoken) is matched by plain code BEFORE waiting for another turn, and
        # runs right here rather than in the shared worker threads, so a pile of waiting requests can't delay it.
        if is_stand_down(body.message):
            return _json(await stand_down_thread(body.message[:200]))
        return await run_in_threadpool(talk, body)

    def talk(body: ChatIn) -> JSONResponse:

        def work(town: Any) -> dict[str, Any]:
            reaper = Reaper(town, view, rules=rules, confirm_hint=HQ_CONFIRM_HINT)
            reaper.history = state.history
            with timed("chat: Reaper's turn"):
                result = reaper.turn(body.message)
            state.history = reaper.history
            proposal = result.proposal if result.proposal is not None and not result.proposal.empty else None
            if proposal:
                state.keep(proposal, {d.action_id: dec.reason_source(d.reason, body.message)
                                      for d in proposal.items if d.decision == dec.REJECT})
            return {"reply": result.text, "mode": result.mode, "proposal": card(town, proposal) if proposal else None,
                    "cost_usd": round(result.cost_usd, 6), "seconds": result.seconds, "model": result.model}
        with timed.holding(chat_lock, "chat"):
            return _json(with_town(work))

    @app.post("/api/feedback")
    def feedback(body: FeedbackIn) -> JSONResponse:
        """Charlie's thumbs on a reply. Logged; an explicit change ("shorter", "skip the costs") becomes a
        profile_change PROPOSAL in the queue. Nothing about how Reaper briefs him changes until he approves it."""
        if body.rating == 0:
            raise HTTPException(status_code=422, detail="rating must be 1 or -1")
        try:
            return _json(with_town(lambda town: profile.record_rating(town, body.about, body.rating, body.excerpt,
                                                                      body.change)))
        except ValueError as err:
            raise HTTPException(status_code=422, detail=str(err)) from err

    @app.post("/api/revise")
    def revise(body: ReviseIn) -> JSONResponse:
        """Charlie replaces a rejection reason on a waiting card with his own words. This only builds a NEW
        proposal through the same plain-code check (nothing is decided); the old one is dropped. Confirm is
        still a separate request."""
        with timed.holding(chat_lock, "revise"):
            with state.lock:
                proposal = state.proposals.get(body.proposal_id)
                sources = dict(state.sources.get(body.proposal_id, {}))
            if proposal is None:
                raise HTTPException(404, "No such proposal waiting (already confirmed, cancelled or expired).")
            if not any(d.action_id == body.action_id and d.decision == dec.REJECT for d in proposal.items):
                raise HTTPException(400, "That card has no rejection of that action.")
            items = [dec.Decision(d.action_id, d.decision, body.reason.strip()) if d.action_id == body.action_id
                     else d for d in proposal.items]

            def work(town: Any) -> dict[str, Any]:
                new = dec.check(town, items, edits=list(proposal.edits), orders=list(proposal.orders), rules=rules)
                dec.cancel(town, proposal, "replaced: Charlie changed a rejection reason on the page")
                if town.killswitch.is_stopped():   # stood down meanwhile: nothing waits for a Confirm
                    return {"proposal": None, "refused": ["The Town is stood down. Nothing waits for a Confirm."]}
                if new.empty:
                    return {"proposal": None, "refused": list(new.refused)}
                dec.log_proposal(town, new)
                state.drop(proposal.id)
                state.keep(new, {**sources, body.action_id: "charlie"})
                return {"proposal": card(town, new), "refused": list(new.refused)}
            result = with_town(work)
            if result["proposal"] is None:
                state.drop(proposal.id)
            return _json(result)

    @app.post("/api/confirm")
    def confirm(body: ProposalIn) -> JSONResponse:
        with timed.holding(chat_lock, "confirm"):
            with state.lock:
                proposal = state.proposals.get(body.proposal_id)
            if proposal is None:
                raise HTTPException(404, "No such proposal waiting (already confirmed, cancelled or expired).")

            def work(town: Any) -> dict[str, Any]:
                try:
                    return {"ok": True, "results": dec.confirm(town, proposal, can_decide, rules)}
                except dec.DecisionError as err:
                    return {"ok": False, "results": [str(err)]}
            result = with_town(work)   # an unexpected error leaves the proposal waiting, to try again
            # A big batch that all went through earns Reaper's signature line (plain code decides).
            result["signature"] = after_batch(result["results"], proposal.count) if result["ok"] else ""
            outcome = ("Charlie confirmed. Results: " if result["ok"] else "The confirmation didn't go ahead: ")
            state.drop(body.proposal_id)
            with state.lock:
                state.history += [{"role": "user", "content": f"[Town: {outcome}{scrub(' '.join(result['results']))[:1000]}]"},
                                  {"role": "assistant", "content": "Noted."}]
            return _json(result)

    # ---------------- Sites: every build and a read-only View of its files ----------------

    @app.get("/api/sites")
    def sites() -> JSONResponse:
        listed = getattr(view, "sites", None)
        return _json(with_town(listed) if callable(listed) else {"sites": []})

    @app.get(SITE_PREFIX + "{given}/{build_id}")
    def site_root(given: str, build_id: int) -> Response:
        # The page's own files are linked relative to the build's folder, so it needs the trailing slash.
        return Response(status_code=307, headers={"Location": f"{SITE_PREFIX}{given}/{build_id}/"})

    @app.get(SITE_PREFIX + "{given}/{build_id}/{path:path}")
    def site_file(given: str, build_id: int, path: str) -> Response:
        serve = getattr(view, "site_file", None)
        found = with_town(lambda town: serve(town, build_id, path)) if callable(serve) else None
        if found is None:
            raise HTTPException(404, "No such file.")
        data, media_type = found
        return Response(data, media_type=media_type, headers={"Content-Security-Policy": SITE_CSP,
                                                              "X-Robots-Tag": "noindex"})

    @app.post("/api/autopilot-stop")
    def autopilot_stop() -> JSONResponse:
        """Stop autopilot: the safe direction (it only ends the loop; nothing is decided), so no Confirm."""
        stop = getattr(view, "stop_autopilot", None)
        if not callable(stop):
            raise HTTPException(404, "No autopilot here.")
        return _json(with_town(lambda town: stop(town, town.actor)))

    # ---------------- "Catch me up": the page asks once when it opens ----------------

    @app.post("/api/catch-up")
    def catch_up(body: CatchUpIn) -> JSONResponse:
        """The page calls this once when it opens (auto). After reaper.catch_up_after_hours or more away (since his
        last visit), it returns the briefing; otherwise, or on a first visit ever, nothing. Either way this visit is
        logged (`hq_visit`), so the next one counts from now. auto=false briefs him whatever the gap."""
        with timed.holding(chat_lock, "catch up"):
            def work(town: Any) -> dict[str, Any]:
                since = catchup.last_visit(town.conn)
                away = catchup.hours_since(since)
                log_event(town.conn, "jarvis", "hq_visit", {"away_hours": round(away, 2) if away is not None else None})
                threshold = float(town.limits.get("reaper.catch_up_after_hours"))
                if body.auto and (away is None or away < threshold):
                    return {"briefing": None, "away_hours": away}
                b = catchup.briefing(town, view, since=since)
                return {"briefing": b.text, "by": b.by, "away_hours": away, "todo": b.todo}
            return _json(with_town(work))

    # ---------------- "Needs you": every item waiting for Charlie, and one click to decide one ----------------

    @app.get("/api/needs-you")
    def needs_you() -> JSONResponse:
        """The pack's list of waiting items in plain words (read-only), plus an activity line from the log."""
        listed = getattr(view, "needs_you", None)
        return _json(with_town(listed) if callable(listed) else {"items": [], "activity": []})

    @app.post("/api/decide")
    def decide(body: DecideIn) -> JSONResponse:
        """Charlie's click on Approve or Reject IS the decision, through the same path as Reaper's cards: plain code
        checks it (pending, a reason for a rejection, the Guard's re-check: a BLOCKED item can't be approved), then
        the same confirm runs it. If any item is refused, nothing is done."""
        with timed.holding(chat_lock, "decide"):
            def work(town: Any) -> dict[str, Any]:
                reason = " ".join(body.reason.split())
                proposal = dec.check(town, [dec.Decision(i, body.decision, reason) for i in body.action_ids],
                                     rules=rules)
                if proposal.refused or proposal.empty:
                    return {"ok": False, "results": list(proposal.refused) or ["Nothing to decide."]}
                try:
                    return {"ok": True, "results": dec.confirm(town, proposal, can_decide, rules)}
                except dec.DecisionError as err:
                    return {"ok": False, "results": [str(err)]}
            return _json(with_town(work))

    @app.post("/api/cancel")
    def cancel(body: ProposalIn) -> JSONResponse:
        with timed.holding(chat_lock, "cancel"):   # never in the middle of a Confirm of the same card
            proposal = state.drop(body.proposal_id)
        if proposal is not None:
            with_town(lambda town: dec.cancel(town, proposal, "cancelled on the page"))
        return _json({"ok": True})

    # Reaper's voice: the page sends the line it's about to say and plays the audio that comes back. It's made on
    # this laptop (Kokoro + the robot chain); nothing goes online. Only Reaper's own words reach it (the page
    # never sends anything else), it never touches the Town, and it doesn't wait behind the chat lock.
    speak_limiter = anyio.CapacityLimiter(1)

    @app.get("/api/voice")
    def voice_status() -> JSONResponse:
        reason = voice.available() if voice is not None else "Reaper's voice isn't set up on this laptop"
        return _json({"available": reason is None, "reason": reason})

    @app.post("/api/speak")
    async def speak(body: SpeakIn) -> Response:
        if voice is None or voice.available() is not None:
            raise HTTPException(503, "Reaper's voice isn't available here; the browser's voice is used.")
        try:
            with timed("voice line"):
                audio = await anyio.to_thread.run_sync(voice.speak, scrub(body.text), limiter=speak_limiter)
        except Exception as err:   # a voice problem never breaks the page: it falls back to the browser's voice
            raise HTTPException(503, f"Reaper's voice failed ({type(err).__name__}).") from None
        return Response(audio, media_type="audio/wav")

    @app.post("/api/stand-down")
    async def stand_down_now() -> JSONResponse:
        return _json({"reply": (await stand_down_thread("stand down (HQ button)"))["reply"]})

    async def stand_down_thread(said: str) -> dict[str, Any]:
        # Its own small pool of worker threads, so a stand-down never queues behind chats, confirms or streams
        # in the shared pool (and never blocks the server's event loop either).
        return await anyio.to_thread.run_sync(halt, said, limiter=stand_down_limiter)

    return app


class _Timed:
    """HQ's optional timing log (see create_app's `timing`). `with timed("open the Town"):` reports how long the
    step took: the wall clock, and how much of that this thread spent working. A step that takes seconds while its
    thread works for a few milliseconds was WAITING (for a lock, the disk, or another thread), not busy. With no
    `timing` given, it does nothing."""

    def __init__(self, report: Callable[[str], None] | None) -> None:
        self.report = report

    @contextmanager
    def __call__(self, what: str, on: bool = True) -> Iterator[None]:
        if self.report is None or not on:
            yield
            return
        wall, cpu = time.monotonic(), time.thread_time()
        try:
            yield
        finally:
            self.report(f"{what}: {time.monotonic() - wall:.2f}s (working {time.thread_time() - cpu:.2f}s) "
                        f"[{threading.current_thread().name}]")

    @contextmanager
    def holding(self, lock: Any, who: str) -> Iterator[None]:
        """`with lock:`, with the wait for it timed."""
        with self(f"{who}: wait for the chat lock"):
            lock.acquire()
        try:
            yield
        finally:
            lock.release()


def page_csp(html: str) -> str:
    """The page's policy: the usual one, plus the hash of its one import map (if it has one)."""
    maps = IMPORT_MAP.findall(html)
    if len(maps) != 1:
        return CSP
    digest = base64.b64encode(hashlib.sha256(maps[0].encode("utf-8")).digest()).decode()
    return CSP.replace("script-src 'self'", f"script-src 'self' 'sha256-{digest}'", 1)


def _static_path(static_dir: Path | None, path: str) -> Path | None:
    """The file to serve for /hq/<token>/<path>, or None: only listed folders and file types inside hq/, never
    a path that climbs out (.., absolute paths, backslashes) or a folder."""
    if static_dir is None or "\\" in path or ".." in path.split("/") or path.startswith("/"):
        return None
    if not any(path.startswith(folder) for folder in STATIC_FOLDERS):
        return None
    root = static_dir.resolve()
    target = (root / path).resolve()
    if not target.is_relative_to(root) or not target.is_file() or target.suffix.lower() not in STATIC_TYPES:
        return None
    return target


def _events(with_town: Callable[[Callable[[Any], Any]], Any], view: Any, after: int, once: bool,
            open_reader: Callable[[], Any] | None = None) -> Iterator[str]:
    """Server-sent events: each new flow as it lands in the log, the facts every few seconds, and a ping.
    after=-1 means "from now" (the page loads the recent past from /api/facts). The every-second poll uses a
    read-only connection when one is given; the facts (which need the whole Town) only every few seconds."""
    def read(work: Callable[[Any], Any]) -> Any:
        if open_reader is None:
            return with_town(lambda town: work(town.conn))
        conn = open_reader()
        conn.row_factory = sqlite3.Row   # rows by column name, like the Town's own connection
        try:
            return work(conn)
        finally:
            conn.close()

    def new_flows(conn: sqlite3.Connection) -> tuple[list[dict[str, Any]], int]:
        flows, newest = flows_after(conn, view, after)
        return [f.to_dict() for f in flows], newest

    if after < 0:
        after = read(last_event_id)
    last_facts = last_ping = 0.0
    while True:
        # One read: the newest id first, then only events up to it (engine/flows.py flows_after). Two separate
        # reads once skipped an event that landed between them, so its message never reached the page.
        found, newest = read(new_flows)
        due = once or time.monotonic() - last_facts >= STREAM_FACTS_EVERY_SECONDS or newest != after
        current = with_town(lambda town: town_facts.town_facts(town, view, events=0)) if due else None
        for flow in found:
            yield f"event: flow\ndata: {scrub(json.dumps(flow, default=str))}\n\n"
        if current is not None:
            last_facts = time.monotonic()
            yield f"event: facts\ndata: {scrub(json.dumps(current, default=str))}\n\n"
        after = newest
        if once:
            return
        if time.monotonic() - last_ping >= STREAM_PING_SECONDS:
            last_ping = time.monotonic()
            yield ": ping\n\n"
        time.sleep(STREAM_POLL_SECONDS)


def _json(payload: Any) -> JSONResponse:
    """Every answer goes through the same scrub as Reaper's replies (no key, no private preview path)."""
    return JSONResponse(json.loads(scrub(json.dumps(payload, default=str))))


def _refuse(status: int, why: str) -> JSONResponse:
    return JSONResponse({"error": why}, status_code=status, headers=SECURITY_HEADERS)


def serve(app: FastAPI, port: int, run: Callable[..., Any] | None = None) -> None:
    """Start the server on 127.0.0.1 only. Access logs are off: request lines carry the token."""
    if run is None:
        import uvicorn
        run = uvicorn.run
    run(app, host=HOST, port=port, log_level="warning", access_log=False)


# Shown only if the page's files are missing (hq/web/index.html). No script: the server's rules allow none inline.
PLACEHOLDER_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Reaper HQ</title>
<style>body{font:16px/1.5 system-ui,sans-serif;background:#0e1726;color:#e6ecf5;margin:0;padding:24px;max-width:760px}
code{background:#1a2536;padding:2px 6px;border-radius:4px}</style></head><body>
<h1>Reaper HQ: the page files are missing</h1>
<p>The server is running, but <code>hq/web/index.html</code> isn't there. In the project folder run
<code>npm ci --prefix hq</code>, check that <code>hq/web</code> exists, then start <code>town hq</code> again.</p>
<p>To stop everything now: <code>town stop</code> in a terminal.</p>
</body></html>
"""
