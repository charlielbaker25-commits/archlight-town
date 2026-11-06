"""Reaper HQ (`town hq`): the local server's safety rules, its endpoints, and decisions through Confirm only.

The app runs in-process with Starlette's test client against a throwaway Town (the Reaper test set's frozen
state and a scripted model). Nothing listens on a real port and nothing reaches the real API.
"""
import base64
import hashlib
import json
import re

import pytest
from starlette.testclient import TestClient

from engine import hq
from engine.events import log_event
from engine.flows import flows_since
from engine.scripted_llm import ScriptedClient
from engine.standdown import STAND_DOWN_REPLY
from packs.websites import testset_reaper as frozen
from packs.websites.townview import VIEW

PORT = 8765
TOKEN = "fictional-hq-session"
ORIGIN = f"http://127.0.0.1:{PORT}"


@pytest.fixture
def hq_app(tmp_path):
    tmp_path = tmp_path / "hq"   # conftest already put an instance.yaml in tmp_path itself
    tmp_path.mkdir()
    client = ScriptedClient()
    frozen.open_fake_town(tmp_path, client=client).close()   # seed once
    opened = []

    def open_town():
        town = frozen.open_fake_town(tmp_path, client=client)
        opened.append(town)
        return town

    app = hq.create_app(open_town, VIEW, TOKEN, PORT, can_decide=True)
    web = TestClient(app, base_url=ORIGIN)
    web.model = client
    web.open_town = open_town
    return web


def get(web, path, token=TOKEN, **headers):
    return web.get(path, headers={**({hq.TOKEN_HEADER: token} if token else {}), **headers})


def post(web, path, body=None, token=TOKEN, origin=ORIGIN, **headers):
    sent = {**({hq.TOKEN_HEADER: token} if token else {}), **({"Origin": origin} if origin else {}), **headers}
    return web.post(path, json=body or {}, headers=sent)


def statuses(web):
    town = web.open_town()
    try:
        return frozen.statuses(town)
    finally:
        town.close()


# ---------------- safety ----------------

def test_the_server_binds_127_0_0_1_only():
    seen = {}
    hq.serve(object(), 9999, run=lambda app, **kw: seen.update(kw))
    assert seen["host"] == "127.0.0.1" and seen["access_log"] is False
    assert hq.HOST == "127.0.0.1"


@pytest.mark.parametrize("token", [None, "", "wrong-token"])
def test_a_missing_or_wrong_token_is_refused(hq_app, token):
    assert get(hq_app, "/api/facts", token=token).status_code == 401
    assert post(hq_app, "/api/stand-down", token=token).status_code == 401
    assert get(hq_app, "/", token=token).status_code == 401


def test_the_token_can_ride_in_the_link_for_the_page_and_the_stream(hq_app):
    assert hq_app.get(f"/?token={TOKEN}").status_code == 200
    assert hq_app.get(f"/?token=nope").status_code == 401


@pytest.mark.parametrize("origin", ["https://evil.example", "http://127.0.0.1:1", "null"])
def test_a_foreign_origin_is_refused(hq_app, origin):
    assert get(hq_app, "/api/facts", Origin=origin).status_code == 403
    assert post(hq_app, "/api/chat", {"message": "status"}, origin=origin).status_code == 403


def test_a_change_without_this_pages_origin_is_refused(hq_app):
    assert post(hq_app, "/api/stand-down", origin=None).status_code == 403


def test_a_cross_site_request_is_refused(hq_app):
    assert get(hq_app, "/api/facts", **{"Sec-Fetch-Site": "cross-site"}).status_code == 403


def test_a_foreign_host_is_refused(hq_app):
    # DNS rebinding: another site's name pointed at 127.0.0.1 still says its own name in Host.
    assert get(hq_app, "/api/facts", Host="evil.example").status_code == 403


def test_there_is_no_resume_route_and_no_cors(hq_app):
    paths = {getattr(r, "path", "") for r in hq_app.app.routes}
    assert not any("resume" in p for p in paths)
    assert post(hq_app, "/api/resume").status_code in (404, 405)
    response = get(hq_app, "/api/facts", Origin=ORIGIN)
    assert "access-control-allow-origin" not in {k.lower() for k in response.headers}
    assert response.headers["Referrer-Policy"] == "no-referrer"


def test_no_api_docs_are_served(hq_app):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert get(hq_app, path).status_code == 404


# ---------------- the data ----------------

def test_facts_states_and_flows(hq_app):
    data = get(hq_app, "/api/facts").json()
    assert data["facts"]["queue"]["pending"] == 5
    assert {a["agent"] for a in data["facts"]["agents"]} >= {"mayor", "builder", "jarvis"}
    assert {b["building"] for b in data["buildings"]} >= {"Town Hall", "Orb Tower"}
    assert any(f["from_node"] == "mayor" and f["to_node"] == "builder" for f in data["flows"])


def test_the_stream_sends_new_flows_and_the_facts(hq_app):
    start = get(hq_app, "/api/facts").json()["last_event_id"]
    town = hq_app.open_town()
    try:
        town.conn.execute("INSERT INTO events (ts, agent, event_type, details) VALUES (?, 'critic', 'review_done',"
                          " ?)", ("2026-10-27T01:00:00Z", json.dumps({"build": 3, "verdict": "pass"})))
    finally:
        town.close()
    body = hq_app.get(f"/api/stream?token={TOKEN}&after={start}&once=true").text
    assert "event: flow" in body and "PASS build #3" in body
    assert "event: facts" in body


def test_the_stream_works_with_a_bare_read_only_reader_like_the_launchers(tmp_path):
    # `town hq` polls with a plain read-only sqlite3 connection (no row factory). R1's tests never passed
    # one, and the first new event crashed the stream on the real dev Town.
    import sqlite3
    folder = tmp_path / "hq"
    folder.mkdir()
    client = ScriptedClient()
    frozen.open_fake_town(folder, client=client).close()
    database = (folder / "town.db").resolve()
    reader = lambda: sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
    app = hq.create_app(lambda: frozen.open_fake_town(folder, client=client), VIEW, TOKEN, PORT, can_decide=True,
                        open_reader=reader)
    web = TestClient(app, base_url=ORIGIN)
    start = get(web, "/api/facts").json()["last_event_id"]
    town = frozen.open_fake_town(folder, client=client)
    try:
        town.conn.execute("INSERT INTO events (ts, agent, event_type, details) VALUES (?, 'critic', 'review_done',"
                          " ?)", ("2026-10-27T01:00:00Z", json.dumps({"build": 3, "verdict": "pass"})))
        town.conn.commit()
    finally:
        town.close()
    body = web.get(f"/api/stream?token={TOKEN}&after={start}&once=true").text
    assert "PASS build #3" in body


def _town_opener(folder):
    """A with_town for hq._events, on the frozen Town in `folder`."""
    client = ScriptedClient()
    frozen.open_fake_town(folder, client=client).close()

    def with_town(work):
        town = frozen.open_fake_town(folder, client=client)
        try:
            return work(town)
        finally:
            town.close()
    return with_town


class _WritesMidRead:
    """The pack's view, except that while the stream is turning `trigger` into flows, another part of the Town
    logs `late` (an order's second event). That is the moment the live stream used to skip an event."""

    def __init__(self, view, with_town, trigger, late):
        self.view, self.with_town, self.trigger, self.late, self.done = view, with_town, trigger, late, False

    def __getattr__(self, name):
        return getattr(self.view, name)

    def flows(self, conn, event):
        if event["event_type"] == self.trigger and not self.done:
            self.done = True
            self.with_town(lambda town: log_event(town.conn, *self.late))
        return self.view.flows(conn, event)


def test_the_stream_never_skips_an_event_that_lands_while_it_reads(tmp_path, monkeypatch):
    # Reaper's order lands as two events, one straight after the other. If the second landed while the stream was
    # still reading the first, the stream moved past both and the first one's flows were never sent (the page's
    # "order: inspect 5" from Reaper to the Town Hall went missing; HQ page test test_a_hand_off_reads_in_order).
    for name in ("STREAM_POLL_SECONDS", "STREAM_FACTS_EVERY_SECONDS", "STREAM_PING_SECONDS"):
        monkeypatch.setattr(hq, name, 0.01)
    folder = tmp_path / "hq"
    folder.mkdir()
    with_town = _town_opener(folder)
    view = _WritesMidRead(VIEW, with_town, "order_confirmed",
                          ("jarvis", "order_started", {"order": "inspect", "params": {"lead_id": 5}}))
    stream = hq._events(with_town, view, -1, False)
    next(stream)   # the stream has taken "from now" (and sent the facts) before the order lands
    with_town(lambda town: log_event(town.conn, "jarvis", "order_confirmed",
                                     {"order": "inspect", "params": {"lead_id": 5}}))
    labels = []
    for _ in range(40):   # each poll yields the facts at least; both events show up within a few polls
        chunk = next(stream)
        if chunk.startswith("event: flow"):
            flow = json.loads(chunk.split("data: ", 1)[1])
            labels.append((flow["label"], flow["from_node"], flow["to_node"]))
        if ("inspect 5", "mayor", "inspector") in labels:
            break
    stream.close()
    assert labels == [("order: inspect 5", "charlie", "jarvis"), ("order: inspect 5", "jarvis", "mayor"),
                      ("inspect 5", "mayor", "inspector")]


def test_flows_after_reads_up_to_one_snapshot_of_the_newest_event(tmp_path):
    from engine.flows import flows_after
    folder = tmp_path / "hq"
    folder.mkdir()
    with_town = _town_opener(folder)

    def check(town):
        start = town.conn.execute("SELECT MAX(id) FROM events").fetchone()[0]
        for build in (1, 2, 3):
            log_event(town.conn, "builder", "build_done", {"build": build, "lead_id": 1})
        flows, newest = flows_after(town.conn, VIEW, start)
        assert newest == start + 3 and {f.build_id for f in flows} == {1, 2, 3}
        # A page of `limit` events: `newest` is the last event read, so the next read carries on from there.
        some, upto = flows_after(town.conn, VIEW, start, limit=2)
        assert upto == start + 2 and {f.build_id for f in some} == {1, 2}
        assert flows_after(town.conn, VIEW, newest) == ([], newest)
        # `up_to`: an id the caller read first; nothing after it is read, and that id is where the next read starts.
        capped, upto = flows_after(town.conn, VIEW, start, up_to=start + 1)
        assert upto == start + 1 and {f.build_id for f in capped} == {1}
    with_town(check)


def test_the_timing_log_says_where_a_request_spent_its_time_and_never_the_token(tmp_path):
    folder = tmp_path / "hq"
    folder.mkdir()
    client = ScriptedClient(["All quiet."])
    frozen.open_fake_town(folder, client=client).close()
    lines = []
    app = hq.create_app(lambda: frozen.open_fake_town(folder, client=client), VIEW, TOKEN, PORT, can_decide=True,
                        timing=lines.append)
    web = TestClient(app, base_url=ORIGIN)
    assert post(web, "/api/chat", {"message": "secret plans for Oak Lane"}).status_code == 200
    web.get(f"/hq/{TOKEN}/web/main.js")   # the page's own files carry the token in their path: never timed
    steps = [line.split(":")[0] for line in lines]
    assert steps[:3] == ["chat", "open the Town", "chat"]   # the lock wait, opening the Town, Reaper's turn
    assert "chat: wait for the chat lock" in lines[0] and "chat: Reaper's turn" in lines[2]
    assert lines[-1].startswith("POST /api/chat: ")
    assert all(re.search(r": \d+\.\d\ds \(working \d+\.\d\ds\)", line) for line in lines)
    assert not any(TOKEN in line or "Oak Lane" in line or "All quiet" in line or "/hq/" in line for line in lines)


def test_no_timing_log_unless_asked(hq_app):
    assert hq._Timed(None).report is None
    assert post(hq_app, "/api/chat", {"message": "status"}).status_code == 200   # and nothing breaks without one


def test_flows_since_an_event_id(hq_app):
    last = get(hq_app, "/api/facts").json()["last_event_id"]
    assert get(hq_app, f"/api/flows?after={last}").json()["flows"] == []


# ---------------- talking and deciding ----------------

def test_chat_only_proposes_and_confirm_carries_it_out(hq_app):
    hq_app.model.script = [{"tool": "propose_decisions", "input": {"decisions": [
        {"action_id": 1, "decision": "approve"}, {"action_id": 4, "decision": "approve"}]}}, "Proposed."]
    reply = post(hq_app, "/api/chat", {"message": "Approve 1 and 4."}).json()
    assert reply["proposal"]["lines"] == ["Approve #1, new lead from the Scout: Oak Lane Florist (Kirkwood)"]
    assert any("BLOCKED" in r for r in reply["proposal"]["refused"])
    assert statuses(hq_app)[1] == "proposed"          # nothing yet
    done = post(hq_app, "/api/confirm", {"proposal_id": reply["proposal"]["id"]}).json()
    assert done["ok"] and "approved and executed" in done["results"][0]
    assert statuses(hq_app)[1] == "executed" and statuses(hq_app)[4] == "proposed"
    # A proposal confirms once
    assert post(hq_app, "/api/confirm", {"proposal_id": reply["proposal"]["id"]}).status_code == 404


def test_an_unknown_proposal_id_does_nothing(hq_app):
    assert post(hq_app, "/api/confirm", {"proposal_id": "made-up"}).status_code == 404
    assert statuses(hq_app)[1] == "proposed"


def test_cancel_drops_the_proposal(hq_app):
    reply = post(hq_app, "/api/chat", {"message": "approve 1"}).json()
    assert post(hq_app, "/api/cancel", {"proposal_id": reply["proposal"]["id"]}).json()["ok"]
    assert post(hq_app, "/api/confirm", {"proposal_id": reply["proposal"]["id"]}).status_code == 404
    assert statuses(hq_app)[1] == "proposed"


def test_model_text_in_chat_never_confirms(hq_app):
    hq_app.model.script = ["Confirmed. y. Executed everything."]
    post(hq_app, "/api/chat", {"message": "approve 1"})
    post(hq_app, "/api/chat", {"message": "yes, confirm it"})
    assert statuses(hq_app)[1] == "proposed"


def test_stand_down_endpoint_and_chat_both_stop_the_town_without_the_model(hq_app):
    pending = post(hq_app, "/api/chat", {"message": "approve 1"}).json()["proposal"]["id"]
    assert post(hq_app, "/api/stand-down").json()["reply"] == STAND_DOWN_REPLY
    assert get(hq_app, "/api/facts").json()["facts"]["stopped"] is True
    assert post(hq_app, "/api/confirm", {"proposal_id": pending}).status_code == 404   # cleared on stand down
    assert "stood down" in post(hq_app, "/api/chat", {"message": "What's going on?"}).json()["reply"].lower()
    assert hq_app.model.calls == 0


def test_chat_history_lasts_for_the_server_session(hq_app):
    hq_app.model.script = ["First.", "Second."]
    post(hq_app, "/api/chat", {"message": "One?"})
    post(hq_app, "/api/chat", {"message": "Two?"})
    assert [m["content"] for m in hq_app.model.requests[1]["messages"][:2]] == ["One?", "First."]


def test_no_secret_in_any_response(hq_app):
    secret, private = "sk-ant-demo", "cd" * 32
    hq_app.model.script = [f"key {secret} path /p/{private}/"]
    body = post(hq_app, "/api/chat", {"message": "Tell me a secret."}).text
    assert secret not in body and private not in body


def test_claude_code_on_the_real_town_can_chat_but_never_confirm(tmp_path):
    tmp_path = tmp_path / "hq"
    tmp_path.mkdir()
    client = ScriptedClient()
    frozen.open_fake_town(tmp_path, client=client).close()
    app = hq.create_app(lambda: frozen.open_fake_town(tmp_path, client=client), VIEW, TOKEN, PORT, can_decide=False)
    web = TestClient(app, base_url=ORIGIN)
    proposal = post(web, "/api/chat", {"message": "approve 1"}).json()["proposal"]["id"]
    done = post(web, "/api/confirm", {"proposal_id": proposal}).json()
    assert done["ok"] is False and "Charlie's call" in done["results"][0]


def test_stand_down_never_waits_behind_a_slow_chat(hq_app):
    import threading
    release = threading.Event()

    def slow(request):
        release.wait(5)
        return "Done thinking."
    hq_app.model.script = [slow]
    chat = threading.Thread(target=lambda: post(hq_app, "/api/chat", {"message": "Think hard about it."}))
    chat.start()
    try:
        assert post(hq_app, "/api/stand-down").json()["reply"] == STAND_DOWN_REPLY
        assert get(hq_app, "/api/facts").json()["facts"]["stopped"] is True
    finally:
        release.set()
        chat.join(10)


def test_too_many_live_streams_are_refused(hq_app, monkeypatch):
    monkeypatch.setattr(hq, "MAX_STREAMS", 0)
    assert hq_app.get(f"/api/stream?token={TOKEN}&once=true").status_code == 429


def test_village_facts_match_the_database(hq_app):
    town = hq_app.open_town()
    try:
        town.conn.execute("INSERT INTO inspections (lead_id, status, created_at) VALUES (1, 'done', '2026-10-26T00:00:00Z')")
        town.mayor.reject(2, "spam listing")
        leads = town.conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
    finally:
        town.close()
    data = get(hq_app, "/api/village").json()
    board = data["scoreboard"]
    assert board["leads"] == leads == 2 and board["inspected"] == 1 and board["built"] == 0
    assert board["contacted"] == board["clients"] == board["revenue_usd"] == 0
    assert "revenue_usd" in board["not_tracked_yet"]
    assert board["cost_this_month_usd"] == pytest.approx(0.0123)
    assert data["bank"]["api_cap_usd"] == 9.0 and data["bank"]["mrr_usd"] == 0
    # Frozen town: #6 executed unchanged, #2 rejected -> 1 of 2 first-try
    assert data["library"] == {"first_try_approval_rate": 0.5, "first_try": 1, "edited": 0, "rejected": 1,
                               "decided": 2, "lessons": []}
    assert data["gatehouse"]["red_flag"] is True
    assert [b["id"] for b in data["gatehouse"]["blocked_now"]] == [4, 5]


def test_the_scoreboard_adds_up_one_business_at_a_time(hq_app):
    """Each stage counts businesses, so no stage exceeds the one before: Archlight's own homepage build and a
    send-back rebuild of the same lead don't inflate 'built' or 'Critic-passed'."""
    town = hq_app.open_town()
    try:
        ts = "2026-10-26T00:00:00Z"
        town.conn.execute("INSERT INTO inspections (lead_id, status, created_at) VALUES (1, 'done', ?)", (ts,))
        for lead in (1, 1, None):   # lead #1 twice (a rebuild) and the homepage (no lead)
            build = town.conn.execute("INSERT INTO builds (lead_id, workspace, status, created_at) VALUES"
                                      " (?, 'w', 'built', ?)", (lead, ts)).lastrowid
            town.conn.execute("INSERT INTO reviews (build_id, workspace, verdict, decided_by, created_at) VALUES"
                              " (?, 'w', 'pass', 'critic', ?)", (build, ts))
        town.conn.commit()
    finally:
        town.close()
    board = get(hq_app, "/api/village").json()["scoreboard"]
    funnel = [board[k] for k in ("leads", "inspected", "built", "critic_passed", "previews_live", "contacted")]
    assert funnel == [2, 1, 1, 1, 0, 0]
    assert funnel == sorted(funnel, reverse=True)


def test_agent_detail_has_state_events_and_playbook(hq_app):
    scout = get(hq_app, "/api/agent/scout").json()
    assert scout["state"] == "waiting_for_approval" and len(scout["recent_events"]) == 2
    assert scout["playbook"] is not None   # packs/websites/playbooks/scout.md exists (empty for now)
    assert get(hq_app, "/api/agent/nobody").status_code == 404


# ---------------- R2: the page's files, cards, revise, stand down first ----------------

def test_the_page_and_every_file_it_loads_need_the_token(hq_app):
    page = hq_app.get(f"/?token={TOKEN}")
    assert page.status_code == 200 and f"/hq/{TOKEN}/web/main.js" in page.text
    assert "__HQ_BASE__" not in page.text
    script = hq_app.get(f"/hq/{TOKEN}/web/main.js")
    assert script.status_code == 200 and script.headers["content-type"].startswith("text/javascript")
    assert hq_app.get(f"/hq/{TOKEN}/node_modules/three/build/three.module.js").status_code == 200
    model = hq_app.get(f"/hq/{TOKEN}/assets/kenney3d/hexagon-kit/GLB%20format/grass.glb")
    assert model.status_code == 200 and model.headers["content-type"] == "model/gltf-binary"
    for wrong in ("wrong-token", "x"):
        assert hq_app.get(f"/hq/{wrong}/web/main.js").status_code == 401
    assert hq_app.get("/hq/web/main.js").status_code == 401


@pytest.mark.parametrize("path", ["../engine/hq.py", "web/../../.env", "web/%2e%2e/%2e%2e/.env", "web/index.html",
                                  "node_modules/three/package.json", "assets/kenney3d/hexagon-kit/License.txt",
                                  "node_modules/three/examples/jsm/libs/draco/draco_decoder.js",
                                  "web", "web/", "web/nothing.js", "web%5Cmain.js", "%2Fetc%2Fpasswd"])
def test_static_files_never_leave_hq_or_its_listed_folders(hq_app, path):
    # Refused one way or another (a client may collapse "..", which moves the token segment: then 401).
    response = hq_app.get(f"/hq/{TOKEN}/{path}")
    assert response.status_code in (400, 401, 404)
    assert "ANTHROPIC" not in response.text and "def create_app" not in response.text


@pytest.mark.parametrize("path, served", [
    ("web/main.js", True), ("assets/kenney3d/hexagon-kit/GLB format/grass.glb", True), ("node_modules/three/build/three.module.js", True),
    ("node_modules/three/examples/jsm/loaders/GLTFLoader.js", True), ("../engine/hq.py", False), ("web/../../.env", False),
    ("web/..\\..\\.env", False), ("/etc/passwd", False), ("web/index.html", False), ("web", False),
    ("node_modules/three/package.json", False), ("assets/kenney3d/hexagon-kit/License.txt", False),
    ("node_modules/three/src/Three.js", False)])
def test_the_static_path_guard_on_raw_paths(path, served):
    assert (hq._static_path(hq.STATIC_DIR, path) is not None) is served


OUTSIDE_URL = re.compile(r"https?://(?!127\.0\.0\.1[:/]|localhost[:/])[^\s'\"`)]+")


def test_the_page_loads_nothing_from_outside():
    """Our own page files mention no outside address at all; the vendored three.js files only mention addresses
    in comments and error messages (its docs and the WebGL spec), never something they load."""
    checked = 0
    for path in (hq.STATIC_DIR / "web").rglob("*"):
        if path.suffix in (".html", ".js", ".css"):
            checked += 1
            assert not OUTSIDE_URL.findall(path.read_text(encoding="utf-8")), path.name
    assert checked >= 8
    for name in ("build/three.module.js", "build/three.core.js", "examples/jsm/loaders/GLTFLoader.js"):
        code = (hq.STATIC_DIR / "node_modules/three" / name).read_text(encoding="utf-8")
        for line in code.splitlines():
            for url in OUTSIDE_URL.findall(line):
                # Only inside a comment or a warning's text, never in fetch/import/src code.
                assert not re.search(r"(fetch|import|load|src)\s*\(\s*['\"`]https?:", line), (name, url)


def test_no_route_can_resume_publish_change_limits_or_do_not_contact(hq_app):
    routes = {(getattr(r, "path", ""), tuple(sorted(getattr(r, "methods", None) or ()))) for r in hq_app.app.routes}
    assert routes == {
        ("/", ("GET",)), ("/hq/{given}/{path:path}", ("GET",)), ("/api/facts", ("GET",)), ("/api/layout", ("GET",)),
        ("/api/village", ("GET",)), ("/api/agent/{name}", ("GET",)), ("/api/flows", ("GET",)),
        ("/api/stream", ("GET",)), ("/api/chat", ("POST",)), ("/api/revise", ("POST",)), ("/api/confirm", ("POST",)),
        ("/api/cancel", ("POST",)), ("/api/stand-down", ("POST",)), ("/api/voice", ("GET",)), ("/api/speak", ("POST",)),
        # Phase 4: a thumb on a reply. It only logs it, or files a profile_change PROPOSAL; it decides nothing.
        ("/api/feedback", ("POST",)),
        # The "Needs you" panel: a read-only list, and one click deciding one card through the confirm path.
        ("/api/needs-you", ("GET",)), ("/api/decide", ("POST",)),
        # One click (2026-11-05): the Sites list, a build's files (read-only, no script runs on them), and Stop for
        # autopilot (the safe direction). Starting autopilot is NOT a route: "go" proposes, Confirm starts it.
        ("/api/sites", ("GET",)), ("/site/{given}/{build_id}", ("GET",)),
        ("/site/{given}/{build_id}/{path:path}", ("GET",)), ("/api/autopilot-stop", ("POST",)),
        # "Catch me up" when the page opens: read-only apart from logging the visit.
        ("/api/catch-up", ("POST",))}
    for path, _ in routes:
        assert not re.search(r"resume|publish|limit|dnc|contact|takedown|approve|reject", path)


def test_the_page_allows_no_inline_or_evaluated_script(hq_app):
    response = get(hq_app, "/")
    csp = response.headers["content-security-policy"]
    script_rule = csp.split("script-src")[1].split(";")[0].split()
    # Only the page's own files, plus its one import map, allowed by that map's exact hash.
    assert script_rule[0] == "'self'" and len(script_rule) <= 2
    if len(script_rule) == 2:
        maps = hq.IMPORT_MAP.findall(response.text)
        digest = base64.b64encode(hashlib.sha256(maps[0].encode()).digest()).decode()
        assert len(maps) == 1 and script_rule[1] == f"'sha256-{digest}'"
        assert set(json.loads(maps[0])) == {"imports"}   # a plain list of addresses, nothing that runs
    assert "unsafe-eval" not in csp and "unsafe-inline" not in csp.split("style-src")[0]
    # Every other answer keeps the plain policy.
    assert get(hq_app, "/api/facts").headers["content-security-policy"] == hq.CSP


def test_a_spoken_or_typed_stand_down_never_waits_behind_a_slow_turn(hq_app):
    import threading
    import time
    release = threading.Event()

    def slow(request):
        release.wait(5)
        return "Done thinking."
    hq_app.model.script = [slow]
    chat = threading.Thread(target=lambda: post(hq_app, "/api/chat", {"message": "Think hard about it."}))
    chat.start()
    try:
        time.sleep(0.3)   # the slow turn holds the chat lock now
        started = time.monotonic()
        reply = post(hq_app, "/api/chat", {"message": "Reaper, stand down."}).json()
        assert time.monotonic() - started < 3
        assert reply["mode"] == "stand_down" and reply["reply"] == STAND_DOWN_REPLY
        assert get(hq_app, "/api/facts").json()["facts"]["stopped"] is True
    finally:
        release.set()
        chat.join(10)


def test_cards_come_from_the_validated_proposal_not_the_model_text(hq_app):
    hq_app.model.script = [{"tool": "propose_decisions", "input": {"decisions": [
        {"action_id": 1, "decision": "approve"}, {"action_id": 2, "decision": "reject", "reason": "it's a fake listing"},
        {"action_id": 4, "decision": "approve"}]}},
        "Approved #1, #2 and #4 and sent the emails."]
    said = "Approve 1 and 4, reject 2: it's a fake listing."
    proposal = post(hq_app, "/api/chat", {"message": said}).json()["proposal"]
    cards = proposal["cards"]
    assert [(c["kind"], c["decision"], c["action_id"]) for c in cards] == [("decision", "approve", 1),
                                                                         ("decision", "reject", 2)]
    assert cards[0]["target"] == "Oak Lane Florist (Kirkwood)" and cards[0]["agent"] == "scout"
    assert cards[1]["reason"] == "it's a fake listing" and cards[1]["reason_source"] == "charlie"
    assert any("#4" in r and "BLOCKED" in r for r in proposal["refused"])   # never one of the card's items
    assert statuses(hq_app)[1] == "proposed"


def test_an_edit_card_shows_before_and_after(hq_app):
    town = hq_app.open_town()
    try:
        before = json.loads(town.conn.execute("SELECT content FROM actions WHERE id = 3").fetchone()[0])["opener"]
    finally:
        town.close()
    hq_app.model.script = [{"tool": "propose_edit", "input": {"action_id": 3, "field": "opener",
                                                              "new_text": "Hi, this is Charlie."}}, "Proposed."]
    said = "Change #3's opener to: Hi, this is Charlie."
    card = post(hq_app, "/api/chat", {"message": said}).json()["proposal"]["cards"]
    assert card == [{"kind": "edit", "action_id": 3, "action_type": "call_script", "target": card[0]["target"],
                     "field": "opener", "before": before, "after": "Hi, this is Charlie.", "line": card[0]["line"]}]
    assert statuses(hq_app)[3] == "proposed"


def test_an_order_card_says_what_runs_and_the_cap_it_uses(tmp_path):
    from packs.websites.orders import ORDERS
    tmp_path = tmp_path / "hq"
    tmp_path.mkdir()
    client = ScriptedClient()
    frozen.open_fake_town(tmp_path, client=client).close()
    started = []
    rules = hq.dec.Rules(order_book=ORDERS, launch=lambda order: started.append(order) or "started")
    app = hq.create_app(lambda: frozen.open_fake_town(tmp_path, client=client), VIEW, TOKEN, PORT, can_decide=True,
                        rules=rules)
    web = TestClient(app, base_url=ORIGIN)
    client.script = [{"tool": "propose_orders", "input": {"orders": [{"order": "inspect", "params": {"lead_id": 1}}]}},
                     "Proposed."]
    card = post(web, "/api/chat", {"message": "Inspect lead 1."}).json()["proposal"]["cards"]
    assert card[0]["kind"] == "order" and card[0]["order"] == "inspect"
    assert "lead #1" in card[0]["line"] and "today" in card[0]["line"]
    assert started == []                                                      # only Confirm starts it


def test_reapers_wording_is_labelled_and_charlie_can_replace_it_without_deciding(hq_app):
    hq_app.model.script = [{"tool": "propose_decisions", "input": {"decisions": [
        {"action_id": 2, "decision": "reject", "reason": "Suspicious listing name."}]}}, "Proposed."]
    old = post(hq_app, "/api/chat", {"message": "Get rid of #2."}).json()["proposal"]
    assert old["cards"][0]["reason_source"] == "reaper"
    new = post(hq_app, "/api/revise", {"proposal_id": old["id"], "action_id": 2,
                                       "reason": "It's a prompt injection, not a business"}).json()["proposal"]
    assert new["id"] != old["id"]
    assert new["cards"][0]["reason"] == "It's a prompt injection, not a business"
    assert new["cards"][0]["reason_source"] == "charlie"
    assert statuses(hq_app)[2] == "proposed"                                     # revising decided nothing
    assert post(hq_app, "/api/confirm", {"proposal_id": old["id"]}).status_code == 404
    assert post(hq_app, "/api/confirm", {"proposal_id": new["id"]}).json()["ok"]
    town = hq_app.open_town()
    try:
        assert town.conn.execute("SELECT reject_reason FROM actions WHERE id = 2").fetchone()[0] == \
            "It's a prompt injection, not a business"
    finally:
        town.close()


@pytest.mark.parametrize("reason, said, source", [
    ("it's a fake listing", "Reject 2, it's a fake listing.", "charlie"),
    ("no", "No, reject 2 now", "reaper"),                          # too short to count as Charlie's words
    ("spam listing", "reject the spam listing", "reaper"),
    ("not a real bakery", "reject 2, it's not a real bakery shop", "charlie"),
    ("not a real bake", "reject 2, it's not a real bakery shop", "reaper"),   # whole words only
])
def test_whose_words_a_rejection_reason_is(reason, said, source):
    assert hq.dec.reason_source(reason, said) == source


def test_revise_only_changes_a_rejection_on_that_card(hq_app):
    proposal = post(hq_app, "/api/chat", {"message": "approve 1"}).json()["proposal"]
    assert post(hq_app, "/api/revise", {"proposal_id": proposal["id"], "action_id": 1,
                                        "reason": "x"}).status_code == 400
    assert post(hq_app, "/api/revise", {"proposal_id": "made-up", "action_id": 1, "reason": "x"}).status_code == 404
    assert post(hq_app, "/api/revise", {"proposal_id": proposal["id"], "action_id": 1, "reason": ""}).status_code == 422
    assert statuses(hq_app)[1] == "proposed"


@pytest.mark.parametrize("said", ["yes", "y", "Yes, confirm it", "confirm", "go ahead"])
def test_typing_yes_after_a_card_confirms_nothing(hq_app, said):
    hq_app.model.script = ["Understood."]
    assert post(hq_app, "/api/chat", {"message": "approve 1"}).json()["proposal"]
    post(hq_app, "/api/chat", {"message": said})
    assert statuses(hq_app)[1] == "proposed"


def test_the_layout_connects_every_building_to_the_town_hall(hq_app):
    from packs.websites.village import district_tiles
    layout = get(hq_app, "/api/layout").json()
    assert layout["grid"] == "hex" and layout["hub"] == "mayor"
    by_agent = {b["agent"]: b for b in layout["buildings"]}
    # A road runs from the Town Hall to every other building's district, so every message can go through it.
    linked = {r["to"] for r in layout["roads"] if r["from"] == "mayor"}
    assert linked == set(by_agent) - {"mayor"}
    # Every building has its own district, and no two districts share a tile.
    seen: dict[tuple[int, int], str] = {}
    for b in layout["buildings"]:
        for tile in district_tiles(tuple(b["district"]), layout["district_radius"]):
            assert tile not in seen, (b["building"], seen.get(tile))
            seen[tile] = b["agent"]
        assert re.fullmatch(r"#[0-9a-f]{6}", b["color"])
    assert len({b["color"] for b in layout["buildings"]}) == len(layout["buildings"])
    shown = {b["building"] for b in layout["buildings"] if not b["hidden_until_agent"]}
    assert shown == {"Orb Tower", "Town Hall", "Gatehouse", "Library", "Bank", "Lookout Tower", "Workshop",
                     "Builder's Yard", "Gallery", "Post Office"}
    assert {b["building"] for b in layout["buildings"] if b["hidden_until_agent"]} == \
        {"Mailroom", "Maintenance Shed", "Bulletin Board"}


def test_reaper_speaks_numbers_ids_and_amounts_plainly():
    """voice.js's `spoken()` (what the browser's voice reads out) and the narration lines, run in Node."""
    import shutil
    import subprocess
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node isn't installed")
    module = (hq.STATIC_DIR / "web" / "voice.js").as_uri()
    script = (f"import('{module}').then(v => console.log(JSON.stringify(["
              "v.spoken('Approve #12. Spend: API $0.04 of $9.00, total $1.25 of $25.00. Cost $0.0012. 80% used.'),"
              "v.narrationLine({from_node: 'critic', to_node: 'mayor', kind: 'result', label: 'PASS build #7'}),"
              "v.narrationLine({from_node: 'scout', to_node: 'mayor', kind: 'proposal', label: 'proposed lead #3'}),"
              "v.narrationLine({from_node: 'charlie', to_node: 'jarvis', kind: 'chat', label: 'asked Reaper'})])))")
    out = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=30)
    said, critic, scout, chat = json.loads(out.stdout.strip().splitlines()[-1])
    assert said == ("Approve number 12. Spend: API 4 cents of 9 dollars, total 1 dollar and 25 cents of 25 dollars. "
                    "Cost less than one cent. 80 percent used.")
    assert (critic, scout, chat) == ("Critic: pass.", "Scout reports a new lead.", None)


def test_orders_and_deep_analysis_show_on_the_map():
    def event(kind, **details):
        return {"id": 1, "ts": "2026-10-27T01:00:00Z", "agent": "jarvis", "event_type": kind, "action_id": None,
                "details": details}
    started = VIEW.flows(None, event("order_started", order="build", params={"lead_id": 4}))
    assert [(f.from_node, f.to_node, f.kind, f.lead_id) for f in started] == [("mayor", "builder", "work", 4)]
    confirmed = VIEW.flows(None, event("order_confirmed", order="scout", params={"business_type": "salon"}))
    assert [(f.from_node, f.to_node) for f in confirmed] == [("charlie", "jarvis"), ("jarvis", "mayor")]
    failed = VIEW.flows(None, event("order_failed", order="inspect", params={}))
    assert failed[0].kind == "alert" and failed[0].from_node == "inspector"
    activity = VIEW.activity(None, event("analysis_started", question="what works?"))
    assert activity.state == "working" and "deep analysis" in activity.task
    # A waiting proposal counts every item on its card, orders and edits included.
    waiting = VIEW.flows(None, event("reaper_decisions_proposed", items=[], edits=[],
                                     orders=[{"order": "inspect", "params": {"lead_id": 5}}]))
    assert waiting[0].label == "1 to confirm"


# ---------------- R2b: speech bubbles and the side board ----------------

def test_flows_say_what_happened_in_plain_words(hq_app):
    town = hq_app.open_town()
    try:
        log_event(town.conn, "builder", "build_done", {"build": 1, "lead_id": 1})
        log_event(town.conn, "critic", "review_done", {"build": 1, "lead_id": 1, "verdict": "pass"})
        found = {f.label: f.says for f in flows_since(town.conn, VIEW, after_id=0)}
    finally:
        town.close()
    assert found["build #1 ready"] == "the site for Maple Street Bakery is ready"
    assert found["PASS build #1"].startswith("passed ")
    assert not any("_" in said for said in found.values())   # no code names in a bubble


def test_jobs_done_today_counts_real_finished_work(hq_app):
    before = get(hq_app, "/api/village").json()["done_today"]
    town = hq_app.open_town()
    try:
        log_event(town.conn, "builder", "build_done", {"build": 1, "lead_id": 1})
        log_event(town.conn, "treasurer", "cost_logged", {"dollars": 0.01})   # bookkeeping, not a job
    finally:
        town.close()
    assert get(hq_app, "/api/village").json()["done_today"] == before + 1



# ---------------- Reaper's own voice ----------------

class FakeVoice:
    def __init__(self, reason=None):
        self.reason, self.said = reason, []

    def available(self):
        return self.reason

    def speak(self, text):
        self.said.append(text)
        return b"RIFF....WAVEfake"


def voice_app(tmp_path, voice):
    folder = tmp_path / "v"
    folder.mkdir()
    frozen.open_fake_town(folder).close()
    app = hq.create_app(lambda: frozen.open_fake_town(folder), VIEW, TOKEN, PORT, can_decide=True, voice=voice)
    return TestClient(app, base_url=f"http://127.0.0.1:{PORT}")


def test_reapers_voice_is_made_by_the_server_and_scrubbed(tmp_path):
    voice = FakeVoice()
    web = voice_app(tmp_path, voice)
    headers = {"x-town-token": TOKEN, "origin": f"http://127.0.0.1:{PORT}"}
    assert web.get("/api/voice", headers=headers).json() == {"available": True, "reason": None}
    said = web.post("/api/speak", json={"text": "Standing down. Key sk-ant-demo"}, headers=headers)
    assert said.status_code == 200 and said.headers["content-type"] == "audio/wav"
    assert "SECRET" not in voice.said[0]   # the same scrub as every answer
    assert web.post("/api/speak", json={"text": "x" * 601}, headers=headers).status_code == 422


def test_without_the_voice_the_page_is_told_to_use_the_browsers(tmp_path):
    web = voice_app(tmp_path, FakeVoice("the voice packages aren't installed"))
    headers = {"x-town-token": TOKEN, "origin": f"http://127.0.0.1:{PORT}"}
    assert web.get("/api/voice", headers=headers).json()["available"] is False
    assert web.post("/api/speak", json={"text": "hello"}, headers=headers).status_code == 503
    assert web.post("/api/speak", json={"text": "hello"}, headers={"x-town-token": TOKEN}).status_code == 403


# ---------------- Phase 4: thumbs on Reaper's replies (the operator profile) ----------------

def test_a_thumb_is_recorded_and_a_chosen_change_becomes_a_proposal(hq_app):
    alone = post(hq_app, "/api/feedback", {"rating": 1, "excerpt": "Status: all clear."})
    assert alone.status_code == 200 and alone.json()["action_id"] is None
    chosen = post(hq_app, "/api/feedback", {"rating": -1, "excerpt": "A long answer.", "change": "shorter"})
    body = chosen.json()
    assert chosen.status_code == 200 and body["action_id"] and "Proposed #" in body["message"]
    assert post(hq_app, "/api/feedback", {"rating": -1, "change": "be_rude"}).status_code == 422
    assert post(hq_app, "/api/feedback", {"rating": 0}).status_code == 422
    assert post(hq_app, "/api/feedback", {"rating": 1}, token="wrong").status_code in (401, 403)


def test_the_village_carries_the_go_live_checklist_and_the_mailroom_read_only(hq_app):
    extras = get(hq_app, "/api/village").json()["extras"]
    golive = extras["golive"]
    assert golive["sending"] is False and golive["live_problems"]            # off by default
    lines = [item["line"] for item in golive["checklist"]]
    for start in ("Mailing address", "Shadow days", "Gmail scopes", "Today's send cap", "Switch"):
        assert any(line.startswith(start) for line in lines), start
    assert extras["mailroom"] == {"waiting": []}
    # Nothing on the page can flip it: no route that writes go-live exists.
    assert not [r.path for r in hq_app.app.routes if "golive" in getattr(r, "path", "")]



# ---------------- "Needs you" (Charlie, 2026-11-04) ----------------

def add_waiting(web, kind, target, content, reason="r"):
    town = web.open_town()
    try:
        action_id = town.conn.execute("INSERT INTO actions (agent, action_type, target, content, reason, status,"
                                      " created_at) VALUES ('x', ?, ?, ?, ?, 'proposed', strftime('%Y-%m-%dT%H:%M:%SZ',"
                                      "'now'))", (kind, target, json.dumps(content), reason)).lastrowid
        town.conn.commit()
        return action_id
    finally:
        town.close()


def test_needs_you_lists_every_waiting_item_in_plain_words_newest_first(hq_app):
    publish = add_waiting(hq_app, "publish_preview", "Maple Street Bakery", {"kind": "preview", "build_id": 1,
                          "lead_id": 1, "business": "Maple Street Bakery", "url": "https://showcase.example.com/p/x/"})
    reply = add_waiting(hq_app, "reply_needs_charlie", "Maple Street Bakery <maple@example.com>",
                        {"lead_id": 1, "business": "Maple Street Bakery", "from": "maple@example.com",
                         "reply": "Love it! Call me?", "call_request": True})
    data = get(hq_app, "/api/needs-you").json()
    by_title = {i["title"]: i for i in data["items"]}
    assert set(by_title) >= {"Reply from Maple Street Bakery (asks for a call)", "Website ready: Maple Street Bakery",
                             "Email ready: Maple Street Bakery", "Call script ready: Maple Street Bakery",
                             "New leads: 2 bakeries"}
    assert [i["newest_id"] for i in data["items"]] == sorted((i["newest_id"] for i in data["items"]), reverse=True)
    assert data["items"][0]["ids"] == [reply] and by_title["Website ready: Maple Street Bakery"]["ids"] == [publish]
    assert by_title["New leads: 2 bakeries"]["ids"] == [2, 1]
    assert by_title["Email ready: Maple Street Bakery"]["blocked"] is True         # no mailing address: BLOCKED
    assert by_title["Email ready: Maple Street Bakery"]["detail"]["body"].startswith("Hi there")
    assert by_title["Reply from Maple Street Bakery (asks for a call)"]["detail"]["words"] == "Love it! Call me?"
    assert data["items"][0]["announce"] == "Maple Street Bakery replied. It needs you."
    assert any(line.startswith("Builder is building Maple Street Bakery's site, started") for line in data["activity"])


def test_decide_approves_one_card_through_the_confirm_path(hq_app):
    result = post(hq_app, "/api/decide", {"action_ids": [1, 2], "decision": "approve"}).json()
    assert result["ok"] is True and statuses(hq_app)[1] == statuses(hq_app)[2] == "executed"


def test_decide_refuses_a_rejection_without_a_reason_and_a_blocked_approval(hq_app):
    result = post(hq_app, "/api/decide", {"action_ids": [3], "decision": "reject", "reason": "  "}).json()
    assert result["ok"] is False and "needs your reason" in result["results"][0] and statuses(hq_app)[3] == "proposed"
    result = post(hq_app, "/api/decide", {"action_ids": [4], "decision": "approve"}).json()
    assert result["ok"] is False and "BLOCKED" in result["results"][0] and statuses(hq_app)[4] == "proposed"
    result = post(hq_app, "/api/decide", {"action_ids": [3, 4], "decision": "approve"}).json()
    assert result["ok"] is False and statuses(hq_app)[3] == "proposed"            # any refused: nothing is done
    result = post(hq_app, "/api/decide", {"action_ids": [3], "decision": "reject", "reason": "wrong number"}).json()
    assert result["ok"] is True and statuses(hq_app)[3] == "rejected"
    assert post(hq_app, "/api/decide", {"action_ids": [3], "decision": "send"}).status_code == 422


def test_decide_does_nothing_when_decisions_arent_charlies(tmp_path):
    folder = tmp_path / "hq2"
    folder.mkdir()
    client = ScriptedClient()
    frozen.open_fake_town(folder, client=client).close()
    app = hq.create_app(lambda: frozen.open_fake_town(folder, client=client), VIEW, TOKEN, PORT, can_decide=False)
    web = TestClient(app, base_url=ORIGIN)
    result = post(web, "/api/decide", {"action_ids": [1], "decision": "approve"}).json()
    assert result["ok"] is False and "Charlie's call" in result["results"][0]
    town = frozen.open_fake_town(folder, client=client)
    try:
        assert frozen.statuses(town)[1] == "proposed"
    finally:
        town.close()


# ---------------- one click (2026-11-05): Sites, a build's files read-only, Stop, the package card ----------------

def add_build(web, tmp_path, lead_id=1, status="built", testset_case=None, pages=None):
    """A real build's dist/ folder (Astro-style: index.html loading /_astro/...)."""
    ws = tmp_path / f"ws-{len(list(tmp_path.glob('ws-*')))}"
    dist = ws / "dist"
    (dist / "_astro").mkdir(parents=True)
    (dist / "index.html").write_text('<!doctype html><html><head><title>Maple</title><link rel=stylesheet '
                                     'href=/_astro/site.A1.css></head><body><img src="/_astro/hero.webp" '
                                     'srcset="/_astro/hero.webp 1x, /_astro/hero2.webp 2x"><script>alert(1)</script>'
                                     '</body></html>', encoding="utf-8")
    (dist / "_astro" / "site.A1.css").write_text("@font-face{src:url(/_astro/font.woff2)}", encoding="utf-8")
    (dist / "_astro" / "hero.webp").write_bytes(b"RIFF....WEBP")
    (dist / "secret.env").write_text("NOPE=1", encoding="utf-8")
    (dist / "empty").mkdir()
    for name, html in (pages or {}).items():
        (dist / name).parent.mkdir(parents=True, exist_ok=True)
        (dist / name).write_text(html, encoding="utf-8")
    town = web.open_town()
    try:
        build_id = town.conn.execute("INSERT INTO builds (lead_id, testset_case, workspace, status, created_at) VALUES"
                                     " (?, ?, ?, ?, '2026-11-05T15:00:00Z')", (lead_id, testset_case, str(ws), status)
                                     ).lastrowid
        town.conn.commit()
        return build_id
    finally:
        town.close()


def test_the_sites_list_shows_every_real_build_newest_first_in_plain_words(hq_app, tmp_path):
    first = add_build(hq_app, tmp_path)
    running = add_build(hq_app, tmp_path, status="running")
    add_build(hq_app, tmp_path, testset_case="01-salon")                         # a test-set build is never listed
    sites = get(hq_app, "/api/sites").json()["sites"]
    ids = [s["build_id"] for s in sites]
    assert ids == sorted(ids, reverse=True) and first in ids and running in ids
    by_id = {s["build_id"]: s for s in sites}
    assert by_id[running]["status"] == "building" and by_id[running]["viewable"] is False
    assert by_id[first]["status"] == "built, waiting for the Critic" and by_id[first]["viewable"] is True
    assert by_id[first]["business"] and by_id[first]["lead_id"] == 1


def test_a_build_s_files_are_served_read_only_with_no_script_and_relative_astro_links(hq_app, tmp_path):
    build_id = add_build(hq_app, tmp_path)
    page = get(hq_app, f"/site/{TOKEN}/{build_id}/", token=None)
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    csp = page.headers["content-security-policy"]
    assert "script-src 'none'" in csp and "connect-src 'none'" in csp and "form-action 'none'" in csp
    assert "href=_astro/site.A1.css" in page.text and 'src="_astro/hero.webp"' in page.text
    assert "_astro/hero2.webp 2x" in page.text and "/_astro/" not in page.text
    css = get(hq_app, f"/site/{TOKEN}/{build_id}/_astro/site.A1.css", token=None)
    assert css.status_code == 200 and "url(../_astro/font.woff2)" in css.text
    assert hq_app.get(f"/site/{TOKEN}/{build_id}", follow_redirects=False).status_code == 307


@pytest.mark.parametrize("path", ["secret.env", "empty/", "../../instance.yaml", "_astro/../../dist/index.html",
                                  "nothing.html"])
def test_nothing_but_a_build_s_own_site_files_is_served(hq_app, tmp_path, path):
    build_id = add_build(hq_app, tmp_path)
    answer = get(hq_app, f"/site/{TOKEN}/{build_id}/{path}", token=None)
    assert answer.status_code in (401, 404, 422) and "NOPE" not in answer.text and "pack:" not in answer.text


def test_a_test_set_or_unfinished_build_and_a_wrong_token_get_nothing(hq_app, tmp_path):
    testset = add_build(hq_app, tmp_path, testset_case="01-salon")
    running = add_build(hq_app, tmp_path, status="running")
    real = add_build(hq_app, tmp_path)
    assert get(hq_app, f"/site/{TOKEN}/{testset}/", token=None).status_code == 404
    assert get(hq_app, f"/site/{TOKEN}/{running}/", token=None).status_code == 404
    assert get(hq_app, f"/site/wrong-token/{real}/", token=None).status_code == 401
    assert get(hq_app, f"/site/{TOKEN}/99999/", token=None).status_code == 404


def test_stop_only_asks_autopilot_to_end_and_decides_nothing(hq_app):
    from packs.websites import autopilot
    before = statuses(hq_app)
    result = post(hq_app, "/api/autopilot-stop").json()
    assert result["ok"] is True and "wasn't running" in result["reply"]
    town = hq_app.open_town()
    try:
        assert autopilot.stop_requested(town.settings)
        assert town.conn.execute("SELECT 1 FROM events WHERE event_type = 'autopilot_stop_requested'").fetchone()
    finally:
        town.close()
    assert statuses(hq_app) == before
    assert post(hq_app, "/api/autopilot-stop", origin=None).status_code == 403          # still this page's origin only


def test_the_needs_you_payload_says_whether_autopilot_is_on(hq_app):
    state = get(hq_app, "/api/needs-you").json()["autopilot"]
    assert state["on"] is False and state["line"].startswith("Autopilot: off")


def test_a_package_is_one_card_with_the_site_the_email_and_the_lead(hq_app):
    content = {"build_id": 7, "lead_id": 1, "business": "Maple Street Bakery", "url": "https://showcase.example.com/p/x/",
               "critic_summary": "Warm and honest.", "expires_after_days": 21,
               "lead": {"business_name": "Maple Street Bakery", "area": "Clayton", "why": "No website listed."},
               "email": {"kind": "first", "lead_id": 1, "to": "maple@example.com", "from": "Example Web Studio <a@b>",
                         "subject": "A free website preview for Maple Street Bakery", "body": "Hi there,"}}
    action_id = add_waiting(hq_app, "outreach_package", "Maple Street Bakery <maple@example.com>", content)
    [card] = [i for i in get(hq_app, "/api/needs-you").json()["items"] if i["ids"] == [action_id]]
    assert card["title"] == "Website + email ready: Maple Street Bakery" and card["kind"] == "outreach_package"
    d = card["detail"]
    assert d["type"] == "package" and d["build_id"] == 7 and d["url"] == content["url"]
    assert d["subject"].startswith("A free website preview") and d["critic"] == "Warm and honest."
    assert d["lead"]["check_url"] == "https://www.google.com/search?q=Maple+Street+Bakery+Clayton"
    assert card["blocked"] is True                       # build #7 doesn't exist here: the Guard blocks the site part
    assert any(b.startswith("site: ") for b in card["blocks"])
    result = post(hq_app, "/api/decide", {"action_ids": [action_id], "decision": "reject", "reason": "too plain"}).json()
    assert result["ok"] is False and "which part" in result["results"][0]          # the page sends "site: ..." etc.
    assert statuses(hq_app)[action_id] == "proposed"
    result = post(hq_app, "/api/decide", {"action_ids": [action_id], "decision": "reject",
                                          "reason": "site: too plain"}).json()
    assert result["ok"] is True and statuses(hq_app)[action_id] == "rejected"


def test_every_site_says_the_laptop_command_that_opens_it(hq_app, tmp_path):
    """Charlie, 2026-11-05: cards, Sites rows, the activity line and Reaper's announcement name `town show <LEAD>`
    (or `town show --build <id>` when it isn't the lead's newest site)."""
    older = add_build(hq_app, tmp_path, lead_id=1)
    newest = add_build(hq_app, tmp_path, lead_id=1)
    rows = {s["build_id"]: s for s in get(hq_app, "/api/sites").json()["sites"]}
    assert rows[newest]["show"] == "town show 1" and rows[older]["show"] == f"town show --build {older}"
    action_id = add_waiting(hq_app, "publish_preview", "Maple Street Bakery", {"kind": "preview", "build_id": newest,
                            "lead_id": 1, "business": "Maple Street Bakery", "url": "https://showcase.example.com/p/x/"})
    [item] = [i for i in get(hq_app, "/api/needs-you").json()["items"] if i["ids"] == [action_id]]
    assert item["announce"] == "Website for Maple Street Bakery is ready. View it: town show 1"
    assert item["detail"]["show"] == "town show 1" and item["detail"]["build_id"] == newest
    town = hq_app.open_town()
    try:
        log_event(town.conn, "builder", "build_done", {"build": newest, "lead_id": 1})
        town.conn.commit()
    finally:
        town.close()
    activity = get(hq_app, "/api/needs-you").json()["activity"]
    assert any(line.startswith("Builder finished") and line.endswith("View on this laptop: town show 1")
               for line in activity), activity
