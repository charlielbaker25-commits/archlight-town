"""Capture public media from a new, fictional test-set Town, never a real instance.

    python tools/showcase_media.py
    python tools/showcase_media.py --chromium /usr/bin/chromium
    python tools/showcase_media.py --refresh-package  # keep the other media unchanged

Needs the dev extras, Playwright Chromium and `npm ci --prefix hq`. Output goes
to tools/showcase/media; make_showcase places verified captures in docs/media.
Only the fictional homepage is approved and fake-published through the real
approval spine. The prospect package stays proposed, with the real Guard clear.
No real email or publication occurs.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


FICTIONAL_ADDRESS = "100 Example St, Example City, MO 00000 (FICTIONAL)"
PACKAGE_MEDIA = {"needs-you.png", "sites.png", "town-hall.gif"}


def open_showcase_town(folder: Path, client):
    """Production wiring with explicit fake adapters, confined to this temporary dev instance."""
    from engine.killswitch import KillSwitch
    from engine.limits import Limits
    from engine.settings import load_settings
    from engine.town import open_town
    from packs.websites import golive, pack, package, previews
    from packs.websites.cloudflare import FakeCloudflare
    from packs.websites.gmail import FakeGmail
    from packs.websites.guard import WebsiteGuard
    from packs.websites.outreach import Outbox
    from tests.test_messenger import FakeSonnet

    settings = load_settings(folder / "instance.yaml")
    if (settings.instance != "dev" or settings.mailing_address != FICTIONAL_ADDRESS
            or settings.database != folder.resolve() / "town.db" or settings.budget_shared_with
            or not settings.state_folder.is_relative_to(folder.resolve())):
        raise RuntimeError("showcase requires its isolated fictional dev instance")
    limits = Limits(pack.LIMITS_FILE)
    stop = KillSwitch(settings.killswitch_file)
    publisher = previews.Publisher(FakeCloudflare(settings.state_folder / "cloudflare-fake"), limits, stop,
                                   folder / "deploys")
    outbox = Outbox(FakeGmail(settings.state_folder / "gmail-fake"), stop, limits, settings.mailing_address,
                    live_problems=lambda conn: golive.live_problems(conn, settings, limits))
    town = open_town(settings, pack.PACK_SCHEMAS,
                     {**pack.handlers(outbox), **pack.publish_handlers(publisher, outbox)},
                     pack.LIMITS_FILE, pack.MODELS_FILE, api_key_loader=lambda: "scripted", api_client=client,
                     actor="charlie", live_external_types=pack.LIVE_EXTERNAL_TYPES)
    town.mayor.guard = town.executor.guard = WebsiteGuard(limits, town.budget, town.llm,
                                                          settings.mailing_address, real=False)
    town.mayor.reject_rule = package.reject_reason_problem
    town.executor.live_now = lambda: golive.live_types(town.conn, settings, limits)
    town.cli._launch = FakeSonnet()  # only the model's response is scripted; all assembly and gates are real
    town.publisher, town.gmail = publisher, outbox.client
    return town


def seed(live, folder: Path) -> tuple[int, int]:
    """Use only frozen fictional fixtures; publish the fake homepage through approval first."""
    from engine.db import utc_now
    from engine.events import log_event
    from engine.yamlio import load_yaml
    from packs.websites import package, previews
    from packs.websites.critic_measure import dist_hash

    # LiveHQ created this dev instance under TemporaryDirectory, never the repository's instance.
    instance = folder / "instance.yaml"
    if folder.resolve() == ROOT.resolve() or load_yaml(instance).get("instance") != "dev":
        raise RuntimeError("refusing to seed anything but the throwaway dev Town")
    instance.write_text("instance: dev\npack: websites\ndatabase: town.db\nbackup_folder: backups\n"
                        "state_folder: state\nmailing_address: " + FICTIONAL_ADDRESS + "\n"
                        "overture:\n  region_name: Example City\n  center_lat: 0\n  center_lon: 0\n"
                        "  radius_miles: 1\n  places_file: places.parquet\n", encoding="utf-8")
    case = load_yaml(ROOT / "packs/websites/testset/cases/07-dentist-outdated/case.yaml")
    ws = folder / "sample"
    shutil.copytree(ROOT / "packs/websites/testset/critic/c1-good-dentist/dist", ws / "dist")
    for path in (ws / "dist").rglob("*"):
        if path.suffix in {".html", ".css", ".js"}:
            value = path.read_text(encoding="utf-8")
            path.write_text(value.replace("Example Web Studio", "Example Web Studio")
                            .replace("showcase.example.com", "showcase.example.com"), encoding="utf-8")
    town = live.open_town()
    try:
        town.killswitch.check()
        for table in ("actions", "leads", "events", "costs"):
            town.conn.execute(f"DELETE FROM {table}")

        def reviewed_build(workspace, lead_id=None, kind="preview"):
            digest = dist_hash(workspace / "dist")
            build_id = town.conn.execute(
                "INSERT INTO builds (lead_id,workspace,status,kind,critic,dist_hash,created_at,finished_at)"
                " VALUES (?,?,'built',?,'passed',?,?,?)",
                (lead_id, str(workspace), kind, digest, utc_now(), utc_now())).lastrowid
            town.conn.execute("INSERT INTO reviews (build_id,workspace,verdict,decided_by,dist_hash,summary,created_at)"
                              " VALUES (?,?,'pass','critic',?,?,?)", (build_id, str(workspace), digest,
                              "A calm layout with clear services and contact details.", utc_now()))
            return build_id

        home = folder / "fictional-homepage"
        (home / "dist").mkdir(parents=True)
        (home / "dist/index.html").write_text(
            '<!doctype html><html lang="en"><head><title>Example Web Studio</title></head>'
            '<body><h1>Example Web Studio</h1><p>Fictional showcase homepage.</p></body></html>', encoding="utf-8")
        homepage = reviewed_build(home, kind="homepage")
        home_action = previews.propose_publish(town, homepage)
        town.mayor.approve(home_action)
        result = town.executor.execute(home_action)
        if result.status != "executed" or town.publisher.client.real or town.gmail.real:
            raise RuntimeError("fictional homepage must execute only through fake adapters")
        assert previews.homepage_live(town.conn)

        town.conn.execute("INSERT INTO leads (id,business_name,business_type,area,address,phone,website,public_email,"
                          "contact_route,source,source_id,source_release,created_at)"
                          " VALUES (1,?,?,?,?,?,?,?,'email','overture','fictional-showcase','fictional',?)",
                          (case["business_name"], case["business_type"], case["area"], case["address"],
                           case["phone"], case["website"], case["public_email"], utc_now()))
        town.conn.execute("INSERT INTO inspections (lead_id,status,problems,note,need_score,build_worthy,created_at)"
                          " VALUES (1,'done','[\"outdated\"]','The fictional listed website looks outdated.',80,1,?)",
                          (utc_now(),))
        build = reviewed_build(ws, lead_id=1)
        action = package.propose_package(town, build)
        row = town.mayor.get(action)
        verdict = town.mayor.guard_recheck(row)
        if row["status"] != "proposed" or verdict.verdict != "clear":
            raise RuntimeError("fictional package must genuinely pass the Guard: " + verdict.summary())
        assert town.gmail.recorded() == []
        log_event(town.conn, "critic", "review_done", {"build": build, "verdict": "pass"})
        town.conn.commit()
        return build, action
    finally:
        town.close()


def capture(out: Path, chromium: str | None = None, only: set[str] | None = None) -> None:
    from PIL import Image
    from playwright.sync_api import sync_playwright
    from engine.events import log_event
    from tests.test_autopilot import CountingModel
    from tests.hq_live import LiveHQ

    wanted = lambda name: only is None or name in only

    def painted(page):
        # On software graphics, a screenshot can otherwise reuse the frame from
        # before a click. Wait for both layout and the next displayed frame.
        page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
        page.wait_for_timeout(500)

    with tempfile.TemporaryDirectory(prefix="town-fictional-showcase-") as temporary:
        folder = Path(temporary)
        client = CountingModel()
        live = LiveHQ(folder, token="fictional-showcase-session")
        live.open_town = lambda: open_showcase_town(folder, client)
        build, action = seed(live, folder)
        live.start()
        try:
            with sync_playwright() as pw:
                launch = {"executable_path": chromium} if chromium else {}
                browser = pw.chromium.launch(**launch)
                context = browser.new_context(viewport={"width": 1440, "height": 1000}, device_scale_factor=1,
                                              reduced_motion="reduce")
                # No site, font, speech service, or other outside request can leave this context.
                def local_only(route):
                    if urlsplit(route.request.url).hostname in {"127.0.0.1", "localhost"}:
                        route.continue_()
                    else:
                        route.abort()
                context.route("**/*", local_only)
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.add_init_script("localStorage.setItem('hq-muted', 'true')")
                page.goto(live.lite_url)
                page.wait_for_function("() => Boolean(window.HQ?.map?.ready)", timeout=180000)
                page.locator(".need[data-kind='outreach_package']").wait_for(timeout=60000)
                # Visible disclosure stays in every frame, even when the UI is cropped or shared.
                page.evaluate("""() => {
                  const label = document.createElement('div');
                  label.textContent = 'FICTIONAL TEST-SET TOWN · scripted models · nothing sent or published';
                  label.style = 'position:fixed;bottom:0;left:0;right:0;padding:8px;text-align:center;background:#142635;color:#cfe9dd;font:13px sans-serif;z-index:99999';
                  document.body.append(label);
                }""")
                page.wait_for_timeout(2000)
                if page.locator("#slow-note").is_visible():
                    page.locator("#slow-close").click()
                painted(page)
                if wanted("village.png"):
                    page.screenshot(path=out / "village.png")
                # This caption reports a backend check; it never changes blocks or permission buttons.
                with_town = live.open_town()
                try:
                    assert with_town.mayor.guard_recheck(with_town.mayor.get(action)).verdict == "clear"
                finally:
                    with_town.close()
                page.evaluate(r"""() => {
                  const decorate = () => {
                    const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                    while (walk.nextNode()) {
                      const n = walk.currentNode;
                      const safe = n.data.replace(/\/p\/[a-f0-9]{64}/gi, '/p/[fictional preview]');
                      if (n.data !== safe) n.data = safe;
                    }
                    for (const card of document.querySelectorAll('.need[data-kind="outreach_package"]')) {
                      if (!card.classList.contains('is-blocked') && card.querySelector('.need-approve')
                          && !card.querySelector('.showcase-guard')) {
                        const status = document.createElement('p'); status.className = 'showcase-guard';
                        status.textContent = 'Guard: clear'; status.style.color = '#91e3bd';
                        card.querySelector('.need-title').after(status);
                      }
                    }
                  };
                  decorate(); new MutationObserver(decorate).observe(document.body,
                    {subtree:true, childList:true, characterData:true});
                }""")
                package = page.locator(".need[data-kind='outreach_package']")
                assert package.locator(".need-approve").is_enabled()
                assert not page.locator(".need.is-blocked").count()
                package.locator(".need-view").click()
                page.locator("#needs-you").evaluate("node => {node.style.maxHeight = '52vh'; node.scrollTop = 0;}")
                package.locator(".need-details").wait_for(state="visible")
                painted(page)
                if wanted("needs-you.png"):
                    page.screenshot(path=out / "needs-you.png")
                page.locator("#sites-open").click()
                page.locator("#sites[open]").wait_for()
                painted(page)
                if wanted("sites.png"):
                    page.screenshot(path=out / "sites.png")
                page.locator("#sites-close").click()
                if only is None:
                    page.fill("#chat-input", "status")
                    page.press("#chat-input", "Enter")
                    page.wait_for_function("() => document.querySelectorAll('#chat-log .msg').length >= 3")
                    painted(page)
                    if wanted("reaper.png"):
                        page.screenshot(path=out / "reaper.png")
                    before = page.locator("#chat-log .msg").count()
                    page.fill("#chat-input", "catch me up")
                    page.press("#chat-input", "Enter")
                    page.wait_for_function("n => document.querySelectorAll('#chat-log .msg').length > n+1", arg=before)
                    painted(page)
                    if wanted("catch-me-up.png"):
                        page.screenshot(path=out / "catch-me-up.png")
                    sample = context.new_page()
                    base = live.url.split("/?", 1)[0]
                    sample.goto(f"{base}/site/{live.token}/{build}/")
                    painted(sample)
                    if wanted("preview.png"):
                        sample.screenshot(path=out / "preview.png", full_page=True)
                    sample.close()
                # Record one staged log handoff using the production event mapper.
                # It is a fixture event, not a claim that a model built this site now.
                page.locator(".board-agent[data-agent='mayor']").dispatch_event("click")
                page.wait_for_timeout(2500)
                page.locator("#panel-close").dispatch_event("click")
                page.evaluate("window.HQ.map.setTimeScale(1)")
                town = live.open_town()
                try:
                    town.killswitch.check()
                    log_event(town.conn, "builder", "build_done", {"build": build, "lead_id": 1, "fictional": True})
                    town.conn.commit()
                finally:
                    town.close()
                page.wait_for_function("() => window.HQ.map.debug().walking.length > 0", timeout=60000)
                frames = []
                for _ in range(24):
                    frames.append(Image.open(io.BytesIO(page.screenshot())).convert("RGB"))
                    page.wait_for_timeout(250)
                frames[0].save(out / "town-hall.gif", save_all=True, append_images=frames[1:],
                               duration=300, loop=0, optimize=False)
                if errors:
                    raise RuntimeError("HQ page errors: " + "; ".join(errors))
                context.close()
                browser.close()
        finally:
            live.stop()
    paths = sorted(out.glob("*.png")) + sorted(out.glob("*.gif"))
    provenance = {"source": "fictional test-set Town; scripted clients; no external requests",
                  "case": "07-dentist-outdated; c1-good-dentist frozen preview",
                  "width": 1440, "fixture_package": "Guard clear; approved fake homepage; real Messenger assembly; prospect package proposed only",
                  "clear_package_media": sorted(PACKAGE_MEDIA),
                  "preview_links": "fictional tokens redacted from displayed text only; backend token binding unchanged",
                  "flow": "staged build_done fixture event through the production Town Hall renderer",
                  # Chunks distinguish integrity metadata from a credential-sized blob.
                  "sha256": {p.name: [hashlib.sha256(p.read_bytes()).hexdigest()[i:i+16]
                                       for i in range(0, 64, 16)] for p in paths}}
    (out / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chromium", help="optional installed Chromium executable")
    parser.add_argument("--isolated-output", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--refresh-package", action="store_true", help="replace only needs-you.png, sites.png and town-hall.gif")
    args = parser.parse_args()
    if args.isolated_output is not None:
        capture(args.isolated_output, args.chromium, PACKAGE_MEDIA if args.refresh_package else None)
        return
    destination = ROOT / "tools/showcase/media"
    if destination.exists() and not args.refresh_package:
        raise SystemExit("Media already exists; move the previous capture aside before regenerating.")
    with tempfile.TemporaryDirectory(prefix="town-media-capture-") as temporary:
        staging = Path(temporary) / "media"
        staging.mkdir()
        if args.refresh_package:
            shutil.copytree(destination, staging, dirs_exist_ok=True)
        # Fictional leads alone are insufficient: a hard-coded sender or error
        # message could still name the private business. Capture a sanitized
        # code snapshot in a fresh process too. No private instance is opened.
        from tools.make_showcase import prepare, PrivateContext
        files, _ = prepare(ROOT, PrivateContext(checks=["fictional media scratch snapshot; no private metadata read"]))
        public = Path(temporary) / "public"
        for name, data in files.items():
            if name.startswith("docs/media/"):
                continue
            path = public / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        shutil.copytree(ROOT / "hq/node_modules", public / "hq/node_modules")
        command = [sys.executable, str(public / "tools/showcase_media.py"), "--isolated-output", str(staging)]
        if args.chromium:
            command.extend(["--chromium", args.chromium])
        if args.refresh_package:
            command.append("--refresh-package")
        subprocess.run(command, cwd=public, check=True)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if args.refresh_package:
            for name in PACKAGE_MEDIA | {"provenance.json"}:
                shutil.copyfile(staging / name, destination / name)
        else:
            shutil.copytree(staging, destination)
    print("Captured fictional showcase media; run make_showcase.py to scan and export it.")


if __name__ == "__main__":
    main()
