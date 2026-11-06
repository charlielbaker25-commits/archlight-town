"""Reaper HQ under load, in a real browser: Demo mode with a real flow every few seconds, a graphics reset, and an
error on the page. The page must never go blank, and the graphics memory it uses must stay flat.

By default the stress run is short (about 90 seconds) so `pytest` stays quick. For the full 10-minute run:
    HQ_STRESS_SECONDS=600 pytest tests/test_hq_stress.py -s
The counts at the start and the end are printed (with -s) and written to dev/hq-stress.json if dev/ exists.
"""
import json
import os
import time
from pathlib import Path

import pytest

from engine.events import log_event
from tests.hq_live import LiveHQ
from tests.test_hq_page import LOAD_MS, browser, wait_until  # noqa: F401  (the shared browser fixture)

# The slow 3D page tests (a real browser): `pytest -m "not hq_browser"` skips them for a quick laptop run.
pytestmark = pytest.mark.hq_browser

playwright_api = pytest.importorskip("playwright.sync_api")

STRESS_SECONDS = int(os.environ.get("HQ_STRESS_SECONDS", "90"))
FLOW_EVERY_SECONDS = float(os.environ.get("HQ_STRESS_FLOW_EVERY", "3"))
# A mix of real events, so messages take many different routes (Charlie's fly to the sky; the rest walk roads).
EVENTS = [
    ("builder", "build_done", {"build": 1, "lead_id": 1}),
    ("critic", "review_done", {"build": 1, "lead_id": 1, "verdict": "pass"}),
    ("inspector", "inspection_done", {"lead_id": 1, "need_score": 80, "build_worthy": True}),
    ("jarvis", "jarvis_turn", {"mode": "plain"}),
    ("messenger", "draft_written", {"lead_id": 1, "kind": "call_script", "draft": 1}),
    ("guard", "guard_blocked", {"blocks": ["daily cap reached"]}),
    ("scout", "scout_refused", {"why": "no Overture data"}),
]


@pytest.fixture
def live(tmp_path):
    folder = tmp_path / "town"
    folder.mkdir()
    server = LiveHQ(folder).start()
    yield server
    server.stop()


def open_page(browser, live, **context):
    ctx = browser.new_context(viewport={"width": 1366, "height": 768}, **context)
    tab = ctx.new_page()
    tab.errors = []
    tab.on("pageerror", lambda e: tab.errors.append(str(e)))
    tab.goto(live.url)
    tab.wait_for_selector(".plate[data-agent='mayor']", timeout=LOAD_MS)
    return ctx, tab


def log(live, agent, kind, details):
    town = live.open_town()
    try:
        log_event(town.conn, agent, kind, details)
    finally:
        town.close()


def page_alive(tab):
    """Not blank: the map's canvas is there with a size, the labels show, and no error or reset box is up."""
    return tab.evaluate("""(() => {
        const c = document.querySelector('#map canvas');
        return Boolean(c && c.width > 0 && c.height > 0 && document.querySelectorAll('.plate').length >= 10
                       && document.getElementById('crash').hidden && document.getElementById('graphics-reset').hidden);
    })()""")


def pools_full(tab):
    """Every courier the map can ever need has been made (they each carry a skeleton texture)."""
    pools = tab.evaluate("window.HQ.map.debug().pools")
    return pools["couriersMade"] == pools["maxWalking"]


def memory(tab):
    return tab.evaluate("window.HQ.map.debug().memory")


def test_demo_mode_with_a_flow_every_few_seconds_stays_flat_and_never_goes_blank(browser, live):
    ctx, tab = open_page(browser, live)
    try:
        tab.locator("#demo-mode").dispatch_event("click")
        assert tab.locator("#demo-mode").get_attribute("aria-pressed") == "true"
        start_counts, start_pools, samples = None, None, []
        started = time.monotonic()
        # The warm-up: at least this long (the arc shapes' cache fills), AND every pool is full (the couriers are
        # all made at start-up now; this waits for it anyway, so a slow laptop can't sample too early).
        warm_up = min(30.0, STRESS_SECONDS / 3)
        n = 0
        while time.monotonic() - started < STRESS_SECONDS:
            agent, kind, details = EVENTS[n % len(EVENTS)]
            log(live, agent, kind, details)
            n += 1
            time.sleep(FLOW_EVERY_SECONDS)
            assert page_alive(tab), f"the page went blank or showed an error after {n} flows"
            if start_counts is None and time.monotonic() - started >= warm_up and pools_full(tab):
                start_counts = memory(tab)
                start_pools = tab.evaluate("window.HQ.map.debug().pools")
            if start_counts is not None:
                samples.append(memory(tab))
        end_counts = memory(tab)
        debug = tab.evaluate("window.HQ.map.debug()")
        report = {"seconds": STRESS_SECONDS, "flows_logged": n, "start": start_counts, "end": end_counts,
                  "max_geometries": max(s["geometries"] for s in samples),
                  "max_textures": max(s["textures"] for s in samples),
                  "completed": len(tab.evaluate("window.HQ.debug.completed")), "fps": debug["fps"],
                  "pools_start": start_pools, "pools_end": debug["pools"],
                  "pixelRatio": debug["pixelRatio"]}
        print("\nHQ stress:", json.dumps(report))
        dev = Path(__file__).resolve().parent.parent / "dev"
        if dev.is_dir():
            (dev / "hq-stress.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        # Flat: after the warm-up nothing grows (the arc shapes' cache can trade one shape for another).
        assert start_counts is not None, "the pools never filled during the run"
        assert debug["pools"]["couriersMade"] == debug["pools"]["maxWalking"]   # never more couriers than that
        assert report["max_textures"] <= start_counts["textures"]
        assert end_counts["textures"] <= start_counts["textures"]
        assert report["max_geometries"] <= start_counts["geometries"] + 30   # at most the arc-shape cache's size
        assert end_counts["programs"] <= start_counts["programs"]
        assert tab.errors == []
    finally:
        ctx.close()


def test_a_graphics_reset_shows_a_message_rebuilds_and_carries_on(browser, live):
    ctx, tab = open_page(browser, live)
    try:
        assert tab.evaluate("window.HQ.map.debugLoseGraphics()")
        tab.wait_for_selector("#graphics-reset:not([hidden])", timeout=15000)
        assert "Graphics reset" in tab.locator("#graphics-reset").inner_text()
        tab.wait_for_selector("#graphics-reset", state="hidden", timeout=60000)
        log(live, "builder", "build_done", {"build": 1, "lead_id": 1})
        wait_until(lambda: [c for c in tab.evaluate("window.HQ.debug.completed")
                            if c["flow"]["label"] == "build #1 ready"], timeout=60)
        assert page_alive(tab) and tab.errors == []
    finally:
        ctx.close()


def test_any_page_error_shows_a_box_with_copyable_details(browser, live):
    ctx, tab = open_page(browser, live, permissions=["clipboard-read", "clipboard-write"])
    try:
        tab.evaluate("setTimeout(() => { throw new Error('test failure for the error box'); }, 0)")
        tab.wait_for_selector("#crash:not([hidden])", timeout=10000)
        details = tab.locator("#crash-details").inner_text()
        assert "test failure for the error box" in details and "Map:" in details and "Browser:" in details
        tab.click("#crash-copy")
        wait_until(lambda: "test failure" in tab.evaluate("navigator.clipboard.readText()")
                   or tab.locator("#crash-copy").inner_text().startswith("Selected"), timeout=10)
        tab.click("#crash-close")
        assert tab.locator("#crash").is_hidden()
    finally:
        ctx.close()


def test_a_reset_with_messages_on_the_road_rebuilds_every_pool_and_they_walk_again(browser, live):
    """Charlie's laptop: after a graphics reset, a new flow never completed, and the courier pool read 2 of 3. Here a
    reset hits with messages under way AND a courier missing from the pool: afterwards every message (the ones that
    were walking and a new one) completes, and the pool is whole: exactly maxWalking couriers, all free when idle."""
    ctx, tab = open_page(browser, live)
    try:
        done_before = len(tab.evaluate("window.HQ.debug.completed"))
        for agent, kind, details in EVENTS[:3]:
            log(live, agent, kind, details)
        wait_until(lambda: tab.evaluate("window.HQ.map.debug().walking.length") > 0, timeout=60)
        assert tab.evaluate("window.HQ.map.debugLoseCourier()")
        assert tab.evaluate("window.HQ.map.debugLoseGraphics()")
        tab.wait_for_selector("#graphics-reset:not([hidden])", timeout=15000)
        tab.wait_for_selector("#graphics-reset", state="hidden", timeout=60000)
        log(live, "builder", "build_done", {"build": 1, "lead_id": 1})
        wait_until(lambda: len({c["flow"]["event_id"] for c in tab.evaluate("window.HQ.debug.completed")})
                   >= done_before + 4, timeout=120)
        wait_until(lambda: tab.evaluate("window.HQ.map.debug().walking.length === 0"), timeout=60)
        pools = wait_until(lambda: (lambda p: p if p["couriersFree"] == p["maxWalking"] else None)(
            tab.evaluate("window.HQ.map.debug().pools")), timeout=30)
        assert pools["couriersMade"] == pools["maxWalking"] == 3
        assert page_alive(tab) and tab.errors == []
    finally:
        ctx.close()
