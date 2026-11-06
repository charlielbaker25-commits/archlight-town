"""Reaper HQ's page, end to end in a real browser (Playwright's Chromium) against a real local server.

A throwaway Town (the Reaper test set's frozen fake Town), a scripted model, no network: every request the page
makes must go to 127.0.0.1. These check what Charlie relies on: the token, the buildings and their states, a real
message walking sender -> Town Hall -> receiver, cards that only act on Confirm, STAND DOWN while a chat turn is
still thinking, and the live stream reconnecting by itself after the server restarts.
"""
import json
import threading
import time
import urllib.request

import pytest

from engine.events import log_event
from tests.hq_live import LiveHQ

# The slow 3D page tests (a real browser): `pytest -m "not hq_browser"` skips them for a quick laptop run.
pytestmark = pytest.mark.hq_browser

playwright_api = pytest.importorskip("playwright.sync_api")
# The 3D village loads its models and draws its first frame before the labels appear: a few seconds on a laptop
# with a graphics chip, much longer where the browser draws in software (a machine without a GPU).
LOAD_MS = 150_000


@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as p:
        try:
            chromium = p.chromium.launch()
        except Exception as err:   # Chromium not installed on this laptop: `python -m playwright install chromium`
            pytest.skip(f"Chromium not available: {err}")
        yield chromium
        chromium.close()


@pytest.fixture
def live(tmp_path):
    folder = tmp_path / "town"
    folder.mkdir()
    server = LiveHQ(folder).start()
    yield server
    server.stop()


@pytest.fixture
def page(browser, live):
    context = browser.new_context(viewport={"width": 1366, "height": 768})
    tab = context.new_page()
    tab.outside = []
    tab.errors = []
    local = f"http://127.0.0.1:{live.port}/"
    tab.on("request", lambda r: None if r.url.startswith((local, "data:", "blob:")) else tab.outside.append(r.url))
    tab.on("pageerror", lambda e: tab.errors.append(str(e)))
    tab.goto(live.lite_url)
    tab.wait_for_selector(".plate[data-agent='mayor']", timeout=LOAD_MS)
    yield tab
    assert tab.outside == [], f"the page reached outside 127.0.0.1: {tab.outside}"
    assert tab.errors == [], f"page errors: {tab.errors}"
    context.close()


def wait_until(check, timeout=15.0, step=0.15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(step)
    raise AssertionError("timed out waiting")


def statuses(live):
    town = live.open_town()
    try:
        return {r["id"]: r["status"] for r in town.conn.execute("SELECT id, status FROM actions")}
    finally:
        town.close()


def log(live, agent, kind, details):
    town = live.open_town()
    try:
        log_event(town.conn, agent, kind, details)
    finally:
        town.close()


def send(page, text):
    page.fill("#chat-input", text)
    page.press("#chat-input", "Enter")


def completed(page, label):
    return [c for c in page.evaluate("window.HQ.debug.completed") if c["flow"]["label"] == label]


# ---------------- tests ----------------

def test_the_page_refuses_to_load_without_the_token(live):
    for url in (f"http://127.0.0.1:{live.port}/", f"http://127.0.0.1:{live.port}/?token=wrong"):
        with pytest.raises(urllib.request.HTTPError) as refused:
            urllib.request.urlopen(url, timeout=5)
        assert refused.value.code == 401


def test_buildings_and_states_render(page):
    assert page.locator(".plate").count() == 11
    names = set(page.locator(".plate .plate-name").all_inner_texts())
    assert names == {"Orb Tower", "Town Hall", "Gatehouse", "Library", "Bank", "Lookout Tower", "Workshop",
                     "Builder's Yard", "Gallery", "Post Office", "Mailroom"}   # the Clerk's Mailroom: go-live week
    # The frozen Town: 5 waiting (2 of them blocked), Scout's lead waiting on Charlie.
    assert page.locator(".plate[data-agent='mayor'] .plate-extra").inner_text() == "5 waiting on you"
    assert page.locator(".plate[data-agent='scout']").get_attribute("data-state") == "waiting_for_approval"
    assert "2 blocked" in page.locator(".plate[data-agent='guard']").inner_text()
    assert page.locator("#town-state").inner_text() == "RUNNING"
    assert "not tracked yet" in page.locator("#scoreboard").inner_text()
    assert page.locator("#agent-list li[data-agent='builder']").count() == 1


def test_a_real_flow_walks_from_the_sender_through_the_town_hall_to_the_receiver(page, live):
    log(live, "builder", "build_done", {"build": 1, "lead_id": 1})
    done = wait_until(lambda: completed(page, "build #1 ready"))
    assert done[0]["visited"] == ["builder", "mayor", "critic"]
    assert "build #1 ready" in page.locator("#feed").inner_text()


def test_a_quiet_map_sleeps_and_wakes_for_a_new_message(browser, live):
    log(live, "builder", "build_done", {"build": 1, "lead_id": 1})   # nothing is working any more
    context = browser.new_context(viewport={"width": 1366, "height": 768})
    tab = context.new_page()
    try:
        tab.goto(live.lite_url)
        tab.wait_for_selector(".plate[data-agent='mayor']", timeout=LOAD_MS)
        wait_until(lambda: tab.evaluate("window.HQ.map.debug().sleeping"))   # idle: the map stops drawing
        log(live, "critic", "review_done", {"build": 3, "verdict": "pass"})   # a verdict: nobody left working
        done = wait_until(lambda: completed(tab, "PASS build #3"))
        assert done[0]["visited"] == ["critic", "mayor"]
        wait_until(lambda: tab.evaluate("window.HQ.map.debug().sleeping"))   # and goes back to sleep after
    finally:
        context.close()


def test_a_hand_off_reads_in_order(page, live):
    # An order lands as two events at once; the Town Hall's message to the Inspector waits until Reaper's
    # message has reached the Town Hall.
    log(live, "jarvis", "order_confirmed", {"order": "inspect", "params": {"lead_id": 5}})
    log(live, "jarvis", "order_started", {"order": "inspect", "params": {"lead_id": 5}})
    wait_until(lambda: completed(page, "inspect 5"), timeout=60)   # two walks in a row; slow laptops need room
    labels = [(c["flow"]["label"], c["visited"]) for c in page.evaluate("window.HQ.debug.completed")]
    assert labels.index(("order: inspect 5", ["jarvis", "mayor"])) < labels.index(("inspect 5", ["mayor", "inspector"]))


def test_a_building_panel_shows_its_agent(page):
    page.click(".plate[data-agent='treasurer']")
    page.wait_for_selector(".panel-facts")
    text = page.locator("#panel").inner_text()
    assert "Treasurer" in text and "Revenue" in text and "not tracked yet" in text and "Last 10 events" in text
    page.click("text=Ask Reaper about this")
    assert "Treasurer" in page.locator("#chat-input").input_value()   # dropped into the chat, not sent
    page.click("#panel-close")
    assert page.locator("#panel").is_hidden()


def test_a_proposal_makes_a_card_and_only_confirm_executes_it(page, live):
    send(page, "approve 1")
    card = page.locator(".card").first
    card.wait_for()
    assert "Oak Lane Florist" in card.inner_text() and "Approve" in card.inner_text()
    assert statuses(live)[1] == "proposed"
    card.locator(".btn-confirm").click()
    page.wait_for_selector(".card.card-confirmed")
    assert "approved and executed" in card.inner_text()
    assert statuses(live)[1] == "executed"


def test_cancel_does_nothing(page, live):
    send(page, "reject 2 because it is a fake listing")
    card = page.locator(".card").first
    card.wait_for()
    assert "Reason (your words)" in card.inner_text()
    card.locator(".btn-quiet", has_text="Cancel").click()
    page.wait_for_selector(".card.card-cancelled")
    assert statuses(live)[2] == "proposed"


def test_nothing_confirms_from_chat_text(page, live):
    """With a card open, "yes", "y" and "confirm it" each get an answer straight away (plain code, no model call)
    telling Charlie to use the button. Nothing is confirmed, and no message is left waiting."""
    live.client.script = ["Understood.", "Understood.", "Understood."]
    send(page, "approve 1")
    page.locator(".card").first.wait_for()
    for n, said in enumerate(("yes", "y", "confirm it", "ok"), 1):
        send(page, said)
        wait_until(lambda: page.locator(".msg-pending").count() == 0)
        replies = page.locator(".msg-reaper .msg-text")
        wait_until(lambda: "Use the Confirm button to approve; I can't confirm from chat." in replies.last.inner_text())
    assert statuses(live)[1] == "proposed"
    assert page.locator(".card.card-confirmed").count() == 0
    assert live.client.calls == 0   # no model turn was needed for any of them


def test_stand_down_works_while_a_chat_turn_is_still_waiting(page, live):
    release = threading.Event()
    thinking = threading.Event()

    def slow(request):
        thinking.set()          # the server is now really inside the model call
        release.wait(20)
        return "Done thinking."
    live.client.script = [slow]
    try:
        send(page, "Approve 1 when you're sure.")
        page.wait_for_selector(".msg-pending")
        assert thinking.wait(15)   # click only once the turn is truly thinking (not still on its way in)
        started = time.monotonic()
        # Straight to the button's own click handler: Playwright's click() first waits for a still frame, which
        # takes seconds on a machine that draws the 3D page in software (the orb is animating "thinking").
        page.locator("#stand-down").dispatch_event("click")
        page.wait_for_selector("#banner:not([hidden])")
        assert time.monotonic() - started < 5
        assert page.locator("#town-state").inner_text() == "STOOD DOWN"
        assert page.locator(".msg-pending").count() == 1   # the slow turn is still thinking
        town = live.open_town()
        try:
            assert town.killswitch.is_stopped()
        finally:
            town.close()
    finally:
        release.set()


def test_reaper_stand_down_typed_goes_straight_through_and_withdraws_cards(page, live):
    send(page, "approve 1")
    page.locator(".card").first.wait_for()
    send(page, "Reaper, stand down")
    page.wait_for_selector("#banner:not([hidden])")
    page.wait_for_selector(".card.card-withdrawn")
    assert live.client.calls == 0
    assert page.locator(".btn-confirm").count() == 0   # nothing left to confirm on a stood-down Town
    assert statuses(live)[1] == "proposed"


def test_the_stream_reconnects_after_a_restart_and_catches_up(page, live):
    live.stop()
    page.wait_for_selector("#connection:not([hidden])", timeout=15000)
    log(live, "critic", "review_done", {"build": 7, "verdict": "pass"})   # lands while the server is down
    live.start()
    done = wait_until(lambda: completed(page, "PASS build #7"), timeout=40)
    assert done[0]["visited"] == ["critic", "mayor"]
    page.wait_for_selector("#connection", state="hidden", timeout=15000)


# ---------------- R2b: the 3D village ----------------

def test_every_building_has_its_label_anchored_to_it(page):
    """Each name plate sits on or right beside its own building on screen, and no two plates overlap."""
    plates = {}
    for agent in page.evaluate("[...document.querySelectorAll('.plate')].map(p => p.dataset.agent)"):
        box = page.evaluate(f"window.HQ.map.project('{agent}')")
        plate = page.locator(f".plate[data-agent='{agent}']").bounding_box()
        area = page.locator("#map").bounding_box()
        assert box and plate, agent
        bx, by = area["x"] + box["x"], area["y"] + box["y"]
        # The plate's centre is within half a plate of the building's box (above, beside or below it).
        cx, cy = plate["x"] + plate["width"] / 2, plate["y"] + plate["height"] / 2
        gap_x = max(0, bx - cx, cx - (bx + box["w"]))
        gap_y = max(0, by - cy, cy - (by + box["h"]))
        assert gap_x <= plate["width"] / 2 + 12 and gap_y <= plate["height"] / 2 + 12, (agent, box, plate)
        plates[agent] = plate
    names = list(plates)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            pa, pb = plates[a], plates[b]
            overlap_w = min(pa["x"] + pa["width"], pb["x"] + pb["width"]) - max(pa["x"], pb["x"])
            overlap_h = min(pa["y"] + pa["height"], pb["y"] + pb["height"]) - max(pa["y"], pb["y"])
            assert overlap_w <= 2 or overlap_h <= 2, (a, b)


def test_a_flow_lights_an_arc_runs_a_courier_and_says_it_in_plain_words(page, live):
    log(live, "builder", "build_done", {"build": 1, "lead_id": 1})
    seen = wait_until(lambda: (lambda d: d if d["arcs"] and d["walking"] else None)(page.evaluate("window.HQ.map.debug()")))
    assert seen["walking"][0]["flow"]["from_node"] == "builder"
    bubble = page.locator(".bubble").first
    bubble.wait_for()
    assert bubble.inner_text() == "Builder: the site for Maple Street Bakery is ready"
    done = wait_until(lambda: completed(page, "build #1 ready"), timeout=60)
    assert done[0]["visited"] == ["builder", "mayor", "critic"]
    wait_until(lambda: page.evaluate("window.HQ.map.debug().arcs") == 0)   # the arc fades once it has arrived


def test_clicking_a_building_in_3d_opens_its_panel(page):
    box = page.evaluate("window.HQ.map.project('treasurer')")
    area = page.locator("#map").bounding_box()
    # Click the middle of the Bank's lower half (its plate may sit over the top).
    page.mouse.click(area["x"] + box["x"] + box["w"] / 2, area["y"] + box["y"] + box["h"] * 0.75)
    page.wait_for_selector(".panel-facts")
    assert "Treasurer" in page.locator("#panel").inner_text()


def test_demo_drift_and_whole_town_buttons(page):
    demo = page.locator("#view-demo")
    demo.click()
    assert demo.get_attribute("aria-pressed") == "true"
    assert not page.evaluate("window.HQ.map.debug().sleeping")   # drifting: it keeps drawing
    demo.dispatch_event("click")   # (a plain click waits for a still frame, which a drifting view never gives)
    assert demo.get_attribute("aria-pressed") == "false"
    page.locator("#view-home").dispatch_event("click")
    assert page.evaluate("window.HQ.map.debug().pixelRatio") > 0


def test_the_board_counts_match_the_facts(page):
    assert page.locator("#count-waiting").inner_text() == "5"
    assert page.locator("#count-blocked").inner_text() == "2"
    assert page.locator(".board-agent").count() == 11
    assert page.locator(".board-agent[data-agent='scout']").get_attribute("data-state") == "waiting_for_approval"


def test_thumbs_on_a_reply_record_and_a_chosen_change_waits_for_approval(page, live):
    """Phase 4 (the operator profile): a thumbs-down offers explicit choices; picking one files a proposal, and the
    briefing preference itself doesn't change until Charlie approves it."""
    send(page, "status")
    reply = page.locator(".msg-reaper .msg-rate").last
    reply.wait_for()
    reply.locator("button[aria-label^='Not helpful']").click()
    reply.locator(".msg-chip", has_text="Shorter").click()
    wait_until(lambda: "Proposed #" in reply.locator(".msg-rate-note").inner_text())
    town = live.open_town()
    try:
        from engine import profile
        assert town.conn.execute("SELECT COUNT(*) FROM actions WHERE action_type = 'profile_change' AND status ="
                                 " 'proposed'").fetchone()[0] == 1
        assert profile.load(town.conn)["length"] == "normal"
    finally:
        town.close()


def test_the_library_panel_lists_lessons_and_keeps_the_iq_meter(page):
    page.click(".plate[data-agent='librarian']")
    page.wait_for_selector("#panel .panel-h:has-text('Lessons')")
    text = page.locator("#panel").inner_text()
    assert "Town IQ" in text and "Lessons" in text and "town librarian review" in text


def test_the_post_office_shows_the_go_live_checklist_and_the_mailroom_its_replies(page):
    page.click(".plate[data-agent='messenger']")
    page.wait_for_selector("#panel .panel-h:has-text('Go-live checklist')")
    text = page.locator("#panel").inner_text()
    assert "Shadow days" in text and "Mailing address" in text and "town golive" in text
    assert page.locator("#panel button:has-text('golive')").count() == 0            # read-only: no switch here
    page.click("#panel-close")
    page.click(".plate[data-agent='clerk']")
    page.wait_for_selector("#panel .panel-h:has-text('Replies waiting for you')")
    assert "No replies waiting" in page.locator("#panel").inner_text()


# ---------------- "Needs you" (Charlie, 2026-11-04: the page is hard to follow) ----------------

def waiting(live, kind, target, content):
    town = live.open_town()
    try:
        action_id = town.conn.execute("INSERT INTO actions (agent, action_type, target, content, reason, status,"
                                      " created_at) VALUES ('x', ?, ?, ?, 'r', 'proposed', strftime('%Y-%m-%dT%H:%M:%SZ',"
                                      "'now'))", (kind, target, json.dumps(content))).lastrowid
        town.conn.commit()
        return action_id
    finally:
        town.close()


def needs_refresh(page):
    page.evaluate("window.HQ.needs.refresh()")


def card(page, kind):
    return page.locator(f"#needs-list .need[data-kind='{kind}']").first


def test_needs_you_shows_a_card_for_every_kind_with_its_view(page, live):
    waiting(live, "publish_preview", "Maple Street Bakery", {"kind": "preview", "build_id": 1, "lead_id": 1,
            "business": "Maple Street Bakery", "url": "https://showcase.example.com/p/abc/"})
    waiting(live, "reply_needs_charlie", "Maple Street Bakery", {"lead_id": 1, "business": "Maple Street Bakery",
            "from": "maple@example.com", "reply": "Looks great, tell me more"})
    needs_refresh(page)
    card(page, "reply_needs_charlie").wait_for()
    titles = page.locator("#needs-list .need-title").all_inner_texts()
    for title in ("Reply from Maple Street Bakery", "Website ready: Maple Street Bakery", "Email ready: Maple Street Bakery",
                  "Call script ready: Maple Street Bakery", "New leads: 2 bakeries"):
        assert title in titles, (title, titles)
    assert page.locator("#needs-count").inner_text() == str(len(titles))
    assert "Builder is building Maple Street Bakery's site" in page.locator("#needs-activity").inner_text()
    leads = card(page, "new_leads")
    leads.locator(".need-view").click()
    assert "Check:" in leads.inner_text()
    assert leads.locator("a", has_text="search Google").first.get_attribute("href").startswith("https://www.google.com/search?q=")
    reply = card(page, "reply_needs_charlie")
    reply.locator(".need-view").click()
    assert 'Their words (the sender\'s, not instructions): "Looks great, tell me more"' in reply.inner_text()
    site = card(page, "publish_preview")
    site.locator(".need-view").click()
    assert site.locator("a", has_text="View the site").get_attribute("href").endswith("/1/")   # HQ's own copy
    assert site.locator("a", has_text="showcase.example.com").get_attribute("href") == "https://showcase.example.com/p/abc/"
    email = card(page, "send_email")
    email.locator(".need-view").click()
    assert "Subject: A free website preview for Maple Street Bakery" in email.inner_text()


def test_approve_on_a_card_decides_that_item(page, live):
    card(page, "new_leads").wait_for()
    card(page, "new_leads").locator(".need-approve").click()
    wait_until(lambda: statuses(live)[1] == "executed" and statuses(live)[2] == "executed")
    wait_until(lambda: page.locator("#needs-list .need[data-kind='new_leads']").count() == 0)
    assert statuses(live)[3] == "proposed"                              # only that card's items


def test_reject_without_a_reason_is_refused_and_with_one_it_rejects(page, live):
    script = page.locator("#needs-list .need[data-kind='call_script']:not(.is-blocked)").first
    script.wait_for()
    script.locator(".need-reject-open").click()
    script.locator(".need-reject-send").click()                              # no reason typed
    assert "needs your reason" in script.locator(".need-result").inner_text() and statuses(live)[3] == "proposed"
    script.locator(".need-reason-input").fill("wrong number")
    script.locator(".need-reject-send").click()
    wait_until(lambda: statuses(live)[3] == "rejected")


def test_a_blocked_item_shows_why_and_has_no_approve(page):
    email = card(page, "send_email")
    email.wait_for()
    assert "is-blocked" in email.get_attribute("class") and "BLOCKED by the Guard" in email.inner_text()
    assert email.locator(".need-approve").count() == 0 and email.locator(".need-reject-open").count() == 1


def test_reaper_announces_each_new_item_once(page, live):
    card(page, "new_leads").wait_for()
    waiting(live, "publish_preview", "Oak Lane Florist", {"kind": "preview", "build_id": 2, "business": "Oak Lane Florist",
            "url": "https://showcase.example.com/p/def/"})
    needs_refresh(page)
    line = "Website for Oak Lane Florist is ready."
    wait_until(lambda: line in page.locator("#chat-log").inner_text())
    needs_refresh(page)
    page.wait_for_timeout(500)
    assert page.locator("#chat-log").inner_text().count(line) == 1
    assert "New leads" not in page.locator("#chat-log").inner_text()   # what was waiting at load isn't re-announced


# ---------------- one click (Charlie, 2026-11-05): the package card, Sites, Go and Stop ----------------

def built_site(live, tmp_path, name="Maple Street Bakery", lead_id=1):
    """A real build's dist/ (Astro-style /_astro/ links), so View can open it."""
    ws = tmp_path / f"ws-{name.replace(' ', '-')}"
    (ws / "dist" / "_astro").mkdir(parents=True)
    (ws / "dist" / "index.html").write_text(f'<!doctype html><html><head><title>{name}</title><link rel=stylesheet '
                                            f'href=/_astro/site.css></head><body><h1 class=hero>{name}</h1>'
                                            '<script>document.title = "ran"</script></body></html>', encoding="utf-8")
    (ws / "dist" / "_astro" / "site.css").write_text(".hero{color:rgb(1, 2, 3)}", encoding="utf-8")
    town = live.open_town()
    try:
        build_id = town.conn.execute("INSERT INTO builds (lead_id, workspace, status, created_at) VALUES (?, ?,"
                                     " 'built', strftime('%Y-%m-%dT%H:%M:%SZ','now'))", (lead_id, str(ws))).lastrowid
        town.conn.commit()
        return build_id
    finally:
        town.close()


def test_a_package_card_shows_the_site_the_email_and_the_lead_and_view_opens_the_build(page, live, tmp_path):
    build_id = built_site(live, tmp_path)
    waiting(live, "outreach_package", "Maple Street Bakery <maple@example.com>", {
        "build_id": build_id, "lead_id": 1, "business": "Maple Street Bakery", "url": "https://showcase.example.com/p/abc/",
        "critic_summary": "Warm and honest.", "expires_after_days": 21,
        "lead": {"business_name": "Maple Street Bakery", "area": "Clayton", "why": "No website listed.",
                 "check_url": "https://www.google.com/search?q=Maple+Street+Bakery+Clayton"},
        "email": {"kind": "first", "lead_id": 1, "to": "maple@example.com", "from": "Example Web Studio <a@b>",
                  "subject": "A free website preview for Maple Street Bakery", "body": "Hi there,"}})
    needs_refresh(page)
    package = card(page, "outreach_package")
    package.wait_for()
    assert "Website + email ready: Maple Street Bakery" in package.inner_text()
    package.locator(".need-view").click()
    text = package.inner_text()
    for words in ("Goes live at this private link", "Critic: Warm and honest.", "Subject: A free website preview",
                  "Why Scout picked it: No website listed.", "Check:"):
        assert words in text, words
    view = package.locator("a", has_text="View the site")
    assert view.get_attribute("href").endswith(f"/{build_id}/") and view.get_attribute("rel") == "noopener noreferrer"
    with page.context.expect_page() as opened:
        view.click()
    site = opened.value
    site.wait_for_load_state()
    assert site.locator("h1").inner_text() == "Maple Street Bakery"
    assert site.title() == "Maple Street Bakery"                                 # its script never ran
    assert site.evaluate("getComputedStyle(document.querySelector('h1')).color") == "rgb(1, 2, 3)"   # its CSS did load
    site.close()
    # A rejection says which part: the card has the part picker, and the reason goes as "site: ...".
    package.locator(".need-reject-open").click()
    package.locator(".need-part").select_option("site")
    package.locator(".need-reason-input").fill("too plain for a bakery")
    package.locator(".need-reject-send").click()
    action_id = max(statuses(live))
    wait_until(lambda: statuses(live)[action_id] == "rejected")
    town = live.open_town()
    try:
        assert town.mayor.get(action_id)["reject_reason"] == "site: too plain for a bakery"
    finally:
        town.close()


def test_the_sites_panel_lists_every_build_with_its_status_and_a_view(page, live, tmp_path):
    build_id = built_site(live, tmp_path)
    page.locator("#sites-open").click()
    item = page.locator(f"#sites-list .site[data-build='{build_id}']")
    item.wait_for()
    assert f"build #{build_id}" in item.inner_text() and "waiting for the Critic" in item.inner_text()
    assert "View on this laptop: town show 1" in item.inner_text()              # the LEAD number
    assert item.locator(".site-view").get_attribute("href").endswith(f"/{build_id}/")
    page.locator("#sites-close").click()
    assert not page.locator("#sites").is_visible()


@pytest.fixture
def go_live(tmp_path):
    from engine import decisions as dec
    from packs.websites.orders import ORDERS
    started = []
    book = ORDERS.with_autopilot(lambda: started.append(1) or "Autopilot started (test).")
    folder = tmp_path / "go-town"
    folder.mkdir()
    server = LiveHQ(folder, rules=dec.Rules(order_book=book, launch=lambda order: book.start_autopilot())).start()
    server.started_autopilot = started
    yield server
    server.stop()


def test_go_proposes_starting_autopilot_and_only_confirm_starts_it(browser, go_live):
    context = browser.new_context(viewport={"width": 1366, "height": 768})
    tab = context.new_page()
    try:
        tab.goto(go_live.lite_url)
        tab.wait_for_selector(".plate[data-agent='mayor']", timeout=LOAD_MS)
        tab.evaluate("window.HQ.needs.refresh()")
        wait_until(lambda: "Autopilot: off" in tab.locator("#autopilot-line").inner_text())
        assert tab.locator("#autopilot-go").is_visible() and not tab.locator("#autopilot-stop").is_visible()
        tab.locator("#autopilot-go").click()
        proposal = tab.locator(".card").last
        proposal.wait_for()
        assert "Start autopilot" in proposal.inner_text() and go_live.started_autopilot == []
        proposal.locator(".btn-confirm").click()
        wait_until(lambda: go_live.started_autopilot == [1])
        tab.wait_for_selector(".card.card-confirmed")
    finally:
        context.close()


def test_stop_shows_while_autopilot_is_on_and_asks_it_to_end(page, live):
    from datetime import datetime, timedelta, timezone

    from packs.websites import autopilot
    town = live.open_town()
    try:
        autopilot.write_state(town.settings, True, "charlie", datetime.now(timezone.utc) + timedelta(minutes=30))
    finally:
        town.close()
    needs_refresh(page)
    wait_until(lambda: page.locator("#autopilot-stop").is_visible())
    assert "Autopilot: on" in page.locator("#autopilot-line").inner_text()
    assert not page.locator("#autopilot-go").is_visible()
    page.locator("#autopilot-stop").click()
    wait_until(lambda: "Autopilot stops at its next check" in page.locator("#chat-log").inner_text())
    town = live.open_town()
    try:
        assert autopilot.stop_requested(town.settings)
    finally:
        town.close()


def test_a_website_ready_card_views_hq_s_own_copy_and_names_the_laptop_command(page, live, tmp_path):
    build_id = built_site(live, tmp_path, name="Fictional Example Business")
    waiting(live, "publish_preview", "Fictional Example Business", {"kind": "preview", "build_id": build_id, "lead_id": 1,
            "business": "Fictional Example Business", "url": "https://showcase.example.com/p/zzz/"})
    needs_refresh(page)
    line = "Website for Fictional Example Business is ready. View it: town show 1"
    wait_until(lambda: line in page.locator("#chat-log").inner_text())         # Reaper's announcement says it too
    site = page.locator("#needs-list .need[data-kind='publish_preview']", has_text="Fictional Example Business").first
    site.locator(".need-view").click()
    assert "View on this laptop: town show 1" in site.inner_text()
    view = site.locator("a", has_text="View the site")
    assert view.get_attribute("href").endswith(f"/{build_id}/")               # HQ's own copy, not showcase.example.com
    with page.context.expect_page() as opened:
        view.click()
    tab = opened.value
    tab.wait_for_load_state()
    assert tab.locator("h1").inner_text() == "Fictional Example Business"
    tab.close()
