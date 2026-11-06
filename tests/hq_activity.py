"""Real Town activity for Reaper HQ's screenshots and browser checks, offline.

This runs the Town's OWN code (Scout, the Guard, the Mayor and executor, Inspector, Builder, Critic, publishing
and Messenger) on a throwaway Town, so every message on the map comes from real events in its log. Only the
outside world is replaced, exactly as the tests do: the Overture places file is the Scout tests' small fixture,
the model is scripted (no API), the `claude` process is the tests' fake (it writes the site files and answers
like the Builder, Critic and Messenger would), Astro's build and the browser measurements are faked, and the
Town's own fake Cloudflare and Gmail clients are used (nothing is published or sent anywhere).
"""
import html
import json
from pathlib import Path
from typing import Any, Callable

from engine import claude_cli
from engine.decisions import _run as run_approved
from packs.websites import builder, critic, previews
from packs.websites.builder import run_builder
from packs.websites.critic import critique_with_send_backs
from packs.websites.inspector import run_inspector
from packs.websites.messenger import run_messenger
from packs.websites.scout import run_scout
from tests.test_builder import DESIGN, FakeClaude
from tests.test_critic import CLEAN, PASS, critic_line
from tests.test_messenger import answer_line, good_answer
from tests.test_scout import _write_places


def scout_picks(request: dict[str, Any]) -> str:
    """The scripted model's answer to the Scout: every candidate it was shown, in order."""
    import re
    n = int(re.search(r"pick the (\d+) best", request["system"]).group(1))
    shortlist = json.loads(request["messages"][0]["content"].split("Candidates:\n", 1)[1])
    return json.dumps({"picks": [{"source_id": c["source_id"], "why": f"{c['name']}: no website found."}
                                 for c in shortlist[:n]]})


class MessengerClaude(FakeClaude):
    """The fake `claude` for the Messenger: it reads the facts folder it was given and answers like Sonnet."""

    def __call__(self, command, cwd, env, **kwargs):
        self.scripts = [([answer_line(good_answer(cwd))], {})]
        return super().__call__(command, cwd, env, **kwargs)


def fake_site_tools() -> list[tuple[Any, str, Any]]:
    """Astro and the browser, faked like the Builder and Critic tests do. Returns what to put back."""
    def fake_build(workspace, timeout_s):
        # A plain page for the lead in the brief (the facts check still checks it like any build).
        biz = json.loads((workspace / "brief.json").read_text(encoding="utf-8"))["business"]
        name = html.escape(biz["name"])
        email = f'<a href="mailto:{biz["public_email"]}">{biz["public_email"]}</a>' if biz["public_email"] else ""
        page = (f'<!doctype html><html lang="en"><head><meta name="viewport" content="width=device-width">'
                f'<meta name="robots" content="noindex, nofollow"><title>{name}</title></head><body><h1>{name}</h1>'
                f'<address>{html.escape(biz["address_exactly"])}</address><a href="{biz["phone_tel_link"]}">'
                f'{biz["phone_display"]}</a> {email}<p class="preview-note">Preview made by Example Web Studio for {name}. '
                f'Details to be confirmed by the owner.</p></body></html>')
        (workspace / "dist").mkdir(exist_ok=True)
        (workspace / "dist" / "index.html").write_text(page, encoding="utf-8")
        return True, "built"
    def fake_measure(dist, shots, *args, **kwargs):
        shots.mkdir(parents=True, exist_ok=True)
        (shots / "375px-01.png").write_bytes(b"png")
        return json.loads(json.dumps(CLEAN))
    swaps = [(builder, "template_ready", lambda: True), (builder, "_link_packages", lambda workspace: None),
             (builder, "run_astro_build", fake_build),
             (builder, "measure_site", lambda dist, timeout: {
                 "overflows_at_375": False, "width_at_375": 375, "outside_requests": [],
                 "lighthouse": {"performance": 99, "accessibility": 100}, "reduced_motion": {"moving": []}}),
             (critic, "measure_for_review", fake_measure),
             (claude_cli, "POLL_SECONDS", 0.01)]
    saved = [(mod, name, getattr(mod, name)) for mod, name, _ in swaps]
    for mod, name, value in swaps:
        setattr(mod, name, value)
    return saved


def put_back(saved: list[tuple[Any, str, Any]]) -> None:
    for mod, name, value in saved:
        setattr(mod, name, value)


def drive(open_town: Callable[[], Any], pause: Callable[[str], None] = lambda step: None) -> list[str]:
    """Runs a short, real working day on the Town that `open_town()` opens. `pause(step)` is called between
    steps (the screenshot script waits there so the messages can walk). Returns what happened, step by step."""
    done: list[str] = []
    saved = fake_site_tools()
    try:
        town = open_town()
        try:
            _write_places(Path(town.settings.overture.places_file))
            before = town.conn.execute("SELECT COALESCE(MAX(id), 0) FROM actions").fetchone()[0]
            town.client.script = [scout_picks]
            scouted = run_scout(town, "salon", "Clayton")
            done.append(f"scout: {scouted.message if hasattr(scouted, 'message') else scouted}")
        finally:
            town.close()
        pause("scouted")
        town = open_town()
        try:
            # Two of the Scout's new leads with no website at all (the Inspector needs no browser for those).
            new = [r["id"] for r in town.conn.execute(
                "SELECT id, content FROM actions WHERE action_type = 'add_lead' AND status = 'proposed'"
                " AND agent = 'scout' AND id > ? ORDER BY id", (before,)) if not json.loads(r["content"]).get("website")][:2]
            for action_id in new:
                town.mayor.approve(action_id)
                done.append(run_approved(town, action_id))
        finally:
            town.close()
        pause("approved")
        town = open_town()
        try:
            leads = [r["id"] for r in town.conn.execute(
                "SELECT id FROM leads WHERE id NOT IN (SELECT lead_id FROM inspections) AND COALESCE(website, '') = ''"
                " ORDER BY id DESC LIMIT 2")]
            town.client.script = ["Needs a website: none found."] * 4
            for lead_id in leads:
                done.append(f"inspect {lead_id}: {run_inspector(town, lead_id).message}")
        finally:
            town.close()
        pause("inspected")
        town = open_town()
        try:
            # A build-worthy lead with no public email: after the Critic passes it, the Messenger writes Charlie a
            # call script (an email would also wait for the preview to be live, which needs the homepage live).
            worthy = town.conn.execute(
                "SELECT i.lead_id FROM inspections i JOIN leads l ON l.id = i.lead_id WHERE i.build_worthy = 1"
                " AND COALESCE(l.public_email, '') = '' ORDER BY i.id DESC LIMIT 1").fetchone()
            town.cli._launch = FakeClaude(DESIGN, ([critic_line(PASS)], {}))
            town.cli.check_available = lambda: None
            built = run_builder(town, worthy["lead_id"])
            done.append(f"build: {built.message}")
            build_id = town.conn.execute("SELECT MAX(id) FROM builds WHERE lead_id = ?", (worthy["lead_id"],)).fetchone()[0]
            # Like `town build`: the Critic reviews it, then a passed build is proposed for publishing.
            review = critique_with_send_backs(town, build_id)
            done.append(f"critic: {review.outcome}")
            if review.outcome == "passed":
                try:
                    done.append(f"publish proposed: #{previews.propose_publish(town, review.final_build_id)}")
                except previews.PublishError as err:   # the real rule: no preview before the homepage is live
                    done.append(f"publish not proposed: {err}")
        finally:
            town.close()
        pause("built")
        town = open_town()
        try:
            publish = town.conn.execute("SELECT id FROM actions WHERE action_type = 'publish_preview' AND status ="
                                        " 'proposed' ORDER BY id DESC LIMIT 1").fetchone()
            if publish:
                town.mayor.approve(publish["id"])
                done.append(run_approved(town, publish["id"]))
        finally:
            town.close()
        pause("published")
        town = open_town()
        try:
            town.cli._launch = MessengerClaude()
            town.cli.check_available = lambda: None
            done.append(f"draft: {run_messenger(town, worthy['lead_id'])}")
        finally:
            town.close()
        pause("drafted")
    finally:
        put_back(saved)
    return done
