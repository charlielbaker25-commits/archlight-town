"""The outreach package (Charlie, 2026-11-05): one approval per prospect, for the site and the first email together.

The launcher's own wiring on a throwaway instance: FakeCloudflare (records deployments, answers the live check from
what it recorded), FakeGmail (records drafts and "sent" emails), a fake `claude` for the Messenger and a fake Haiku
for the Guard's soft check. Nothing here reaches Cloudflare, Google or the API."""
import json
import time

import pytest
from typer.testing import CliRunner

import launcher
from engine import claude_cli
from engine.db import utc_now
from engine.executor import ExecutorError
from engine.killswitch import TownStopped
from engine.mayor import MayorError
from engine.settings import load_settings
from packs.websites import golive, package, previews
from packs.websites.critic_measure import dist_hash
from packs.websites.gmail import GmailError
from packs.websites.guard import dnc_add, lead_history
from packs.websites.messenger import MessengerError, prepare
from packs.websites.package import PackageError, propose_package
from tests.test_autopilot import CountingModel
from tests.test_golive import shadow, switch_on
from tests.test_messenger import ADDRESS, FakeSonnet

runner = CliRunner()
PAGE = ('<!doctype html><html lang="en"><head><title>{name}</title><link rel="stylesheet" href="/_astro/site.A1.css">'
        '</head><body><h1>{name}</h1></body></html>')


@pytest.fixture
def town(instance_file, limits_file, monkeypatch):
    with instance_file.open("a", encoding="utf-8") as f:
        f.write(f"mailing_address: {ADDRESS}\n")
    text = limits_file.read_text(encoding="utf-8").replace("live_check_wait_seconds: 10", "live_check_wait_seconds: 0")
    limits_file.write_text(text.replace("live_check_tries: 6", "live_check_tries: 2"), encoding="utf-8")
    monkeypatch.setattr(launcher, "LIMITS_FILE", limits_file)
    monkeypatch.setattr(launcher, "API_CLIENT", CountingModel())
    monkeypatch.setattr(launcher, "_api_key", lambda: "sk-test")
    monkeypatch.setattr(claude_cli, "POLL_SECONDS", 0.01)
    t, publisher = launcher._open(load_settings(instance_file), "charlie", instance_file)
    t.killswitch.resume(t.conn)
    t.publisher, t.cf = publisher, publisher.client
    t.outbox = t.executor.handlers["send_email"].run.__self__
    t.gmail = t.outbox.client
    t.cli._launch = FakeSonnet()
    yield t
    t.close()


def make_build(town, tmp_path, lead_id=None, kind="preview", name="Butterbell Bakery", verdict="pass"):
    ws = tmp_path / f"ws-{time.time_ns()}"
    (ws / "dist" / "_astro").mkdir(parents=True)
    (ws / "dist" / "index.html").write_text(PAGE.format(name=name), encoding="utf-8")
    (ws / "dist" / "_astro" / "site.A1.css").write_text("body{}", encoding="utf-8")
    digest = dist_hash(ws / "dist")
    build_id = town.conn.execute("INSERT INTO builds (lead_id, workspace, status, kind, dist_hash, critic, created_at)"
                                 " VALUES (?, ?, 'built', ?, ?, ?, ?)", (lead_id, str(ws), kind, digest,
                                                                         "passed" if verdict == "pass" else None,
                                                                         utc_now())).lastrowid
    if verdict:
        town.conn.execute("INSERT INTO reviews (build_id, workspace, dist_hash, verdict, decided_by, summary,"
                          " created_at) VALUES (?, ?, ?, ?, 'critic', 'Warm, honest and clear.', ?)",
                          (build_id, str(ws), digest, verdict, utc_now()))
    return build_id


def homepage_live(town, tmp_path):
    action_id = previews.propose_publish(town, make_build(town, tmp_path, kind="homepage", name="Example Web Studio"))
    town.mayor.approve(action_id)
    assert town.executor.execute(action_id).status == "executed"


def add_lead(town, name="Butterbell Bakery", email="butterbellbakery@example.com"):
    lead_id = town.conn.execute(
        "INSERT INTO leads (business_name, business_type, area, address, phone, website, public_email, contact_route,"
        " source, source_id, source_release, created_at, saved_by, check_url, why) VALUES (?, 'bakery', 'Clayton',"
        " '1 Main St, Clayton, MO 63105', '+13145550103', '', ?, 'email', 'overture', ?, 'r', ?, 'scout', ?,"
        " 'No website listed.')", (name, email, f"src-{name}", utc_now(),
                                  f"https://www.google.com/search?q={name.replace(' ', '+')}+Clayton")).lastrowid
    town.conn.execute("INSERT INTO inspections (lead_id, status, problems, note, need_score, build_worthy, created_at)"
                      " VALUES (?, 'done', '[\"no_site\"]', 'No website was found.', 100, 1, ?)", (lead_id, utc_now()))
    return lead_id


@pytest.fixture
def ready(town, tmp_path):
    """The homepage is live and a lead has a Critic-passed build: (lead id, build id)."""
    homepage_live(town, tmp_path)
    lead_id = add_lead(town)
    return lead_id, make_build(town, tmp_path, lead_id)


def proposed(town, build_id):
    action_id = propose_package(town, build_id)
    return action_id, json.loads(town.mayor.get(action_id)["content"])


def events(town, *kinds):
    marks = ",".join("?" * len(kinds))
    return [r["event_type"] for r in town.conn.execute(f"SELECT event_type FROM events WHERE event_type IN ({marks})"
                                                       " ORDER BY id", kinds)]


# ---------------- making one ----------------

def test_one_proposal_carries_the_site_and_the_email(town, ready):
    lead_id, build_id = ready
    action_id, content = proposed(town, build_id)
    row = town.mayor.get(action_id)
    assert row["action_type"] == "outreach_package" and row["status"] == "proposed"
    token = content["token"]
    assert previews.TOKEN.match(token) and content["url"] == previews.preview_url(token)
    issued = town.conn.execute("SELECT * FROM preview_tokens WHERE token = ?", (token,)).fetchone()
    assert issued["build_id"] == build_id and issued["action_id"] == action_id
    email = content["email"]
    assert email["to"] == "butterbellbakery@example.com" and email["kind"] == "first"
    assert content["url"] in email["body"] and ADDRESS in email["body"]       # written against the reserved link
    workspace = previews._build_row(town.conn, build_id)["workspace"]
    assert content["dist_hash"] == dist_hash(__import__("pathlib").Path(workspace) / "dist")
    assert content["critic_summary"] == "Warm, honest and clear." and content["lead"]["check_url"].startswith(
        "https://www.google.com/search?q=")
    verdict = town.mayor.guard_recheck(row)
    assert not verdict.blocked, verdict.blocks
    # Nothing separate: no publish proposal, no send_email, nothing deployed or drafted yet.
    types = {r[0] for r in town.conn.execute("SELECT action_type FROM actions WHERE status = 'proposed'")}
    assert types == {"outreach_package"} and town.gmail.recorded() == [] and len(town.cf.recorded()) == 1


def test_no_package_without_a_critic_pass_the_homepage_or_an_email(town, tmp_path):
    lead_id = add_lead(town)
    with pytest.raises(PackageError, match="homepage"):
        propose_package(town, make_build(town, tmp_path, lead_id))
    homepage_live(town, tmp_path)
    with pytest.raises(PackageError, match="Critic"):
        propose_package(town, make_build(town, tmp_path, lead_id, verdict="fail"))
    held = add_lead(town, "No Email Cafe", email="")
    with pytest.raises(PackageError, match="held"):
        propose_package(town, make_build(town, tmp_path, held, name="No Email Cafe"))
    assert town.conn.execute("SELECT COUNT(*) FROM actions WHERE action_type = 'outreach_package'").fetchone()[0] == 0


def test_a_second_package_for_the_same_build_or_lead_is_refused(town, ready):
    _, build_id = ready
    proposed(town, build_id)
    with pytest.raises(PackageError, match="already waiting"):
        propose_package(town, build_id)


def test_a_draft_that_breaks_the_rules_twice_proposes_nothing(town, ready):
    _, build_id = ready
    bad = {"subject": "Hello", "body": "Hi there, no link here."}
    town.cli._launch = FakeSonnet(bad, bad, bad)
    with pytest.raises(PackageError, match="couldn't write an email"):
        propose_package(town, build_id)
    assert town.conn.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 1   # only the homepage publish


# ---------------- approving: publish, check live, then the email ----------------

def test_approving_publishes_checks_it_is_live_then_saves_a_gmail_draft_in_that_order(town, ready):
    lead_id, build_id = ready
    action_id, content = proposed(town, build_id)
    town.mayor.approve(action_id)
    outcome = town.executor.execute(action_id)
    assert outcome.status == "executed", outcome.result
    assert "Site published at" in outcome.result and "checked live" in outcome.result
    assert "dry-run: not sent; saved as Gmail draft" in outcome.result
    live = town.conn.execute("SELECT * FROM previews WHERE token = ?", (content["token"],)).fetchone()
    assert live["status"] == "live" and live["build_id"] == build_id and live["action_id"] == action_id
    assert town.cf.fetches == [content["url"]]                                 # the live check read the link
    [draft] = town.gmail.recorded()
    assert town.gmail.sent() == [] and not events(town, "email_sent")         # nothing sent: go-live is off
    assert events(town, "preview_published", "gmail_draft_created", "package_done") == [
        "preview_published", "preview_published", "gmail_draft_created", "package_done"]   # homepage, site, email
    with pytest.raises(ExecutorError):
        town.executor.execute(action_id)                                       # runs once, ever


def test_a_publish_failure_means_no_email(town, ready):
    _, build_id = ready
    action_id, _ = proposed(town, build_id)
    town.mayor.approve(action_id)
    town.cf.fail_deploy = True
    outcome = town.executor.execute(action_id)
    assert outcome.status == "failed" and "Stopped at step 2 (publish)" in outcome.result
    assert "Nothing went online and no email was sent" in outcome.result
    assert town.gmail.recorded() == [] and not town.conn.execute(
        "SELECT 1 FROM previews WHERE kind = 'preview'").fetchone()


def test_a_link_that_doesnt_answer_means_no_email_and_says_the_site_is_up(town, ready):
    _, build_id = ready
    action_id, content = proposed(town, build_id)
    town.mayor.approve(action_id)
    town.cf.fetch_status = 404
    outcome = town.executor.execute(action_id)
    assert outcome.status == "failed" and "Stopped at step 3 (live check)" in outcome.result
    assert "WAS published" in outcome.result and "no email was sent" in outcome.result
    assert len(town.cf.fetches) == 2 * len(previews.LIVE_ROUTES)              # each try, every route
    assert town.gmail.recorded() == []
    assert town.conn.execute("SELECT status FROM previews WHERE token = ?", (content["token"],)).fetchone()[0] == "live"


def test_an_email_failure_after_publishing_leaves_the_site_up_and_is_never_retried(town, ready):
    lead_id, build_id = ready
    action_id, content = proposed(town, build_id)
    town.mayor.approve(action_id)
    town.gmail.fail = GmailError("Gmail said no (fake)")
    outcome = town.executor.execute(action_id)
    assert outcome.status == "failed" and "Step 4 (email) failed" in outcome.result
    assert f"IS live at {content['url']}" in outcome.result and "never re-sent automatically" in outcome.result
    assert town.conn.execute("SELECT status FROM previews WHERE token = ?", (content["token"],)).fetchone()[0] == "live"
    with pytest.raises(ExecutorError):
        town.executor.execute(action_id)                                       # failed is final
    assert package.ready_builds(town) == []                                    # autopilot never tries it again
    assert lead_history(town.conn, lead_id)["first_drafted"] is None           # no email counts as made


def test_the_kill_switch_is_step_one(town, ready):
    _, build_id = ready
    action_id, content = proposed(town, build_id)
    town.mayor.approve(action_id)
    town.killswitch.stop(town.conn)
    with pytest.raises(ExecutorError):
        town.executor.execute(action_id)                                       # the executor refuses first
    with pytest.raises(package.PackageFailed, match="step 1 \\(kill switch\\)"):
        package.execute_package(town.publisher, town.outbox, town.conn, action_id, content)
    assert len(town.cf.recorded()) == 1 and town.gmail.recorded() == []


def test_with_go_live_on_the_package_email_is_really_sent_through_the_outbox(town, ready):
    lead_id, build_id = ready
    shadow(town)
    switch_on(town)
    action_id, _ = proposed(town, build_id)
    town.mayor.approve(action_id)
    outcome = town.executor.execute(action_id)
    assert outcome.status == "executed" and "SENT to butterbellbakery@example.com" in outcome.result
    [sent] = town.gmail.sent()
    assert lead_history(town.conn, lead_id)["first_sent"]["id"] == action_id   # it counts as the lead's first email
    assert golive.live_problems(town.conn, town.settings, town.limits) == []


# ---------------- the Guard checks both parts, at proposal, approval and execution ----------------

def test_files_changed_after_the_package_was_made_block_it(town, ready):
    _, build_id = ready
    action_id, _ = proposed(town, build_id)
    page = previews._build_row(town.conn, build_id)["workspace"] + "/dist/index.html"
    with open(page, "a", encoding="utf-8") as f:
        f.write("<!-- changed -->")
    with pytest.raises(MayorError, match="site: "):
        town.mayor.approve(action_id)


def test_the_email_rules_apply_to_the_package_email(town, ready):
    lead_id, build_id = ready
    action_id, content = proposed(town, build_id)
    dnc_add(town.conn, "butterbellbakery@example.com", "asked us not to", "charlie")
    with pytest.raises(MayorError, match="do-not-contact"):
        town.mayor.approve(action_id)


def test_a_separate_first_email_is_blocked_while_a_package_waits_or_after_it_drafted(town, ready):
    lead_id, build_id = ready
    action_id, _ = proposed(town, build_id)
    assert lead_history(town.conn, lead_id)["waiting"][0]["id"] == action_id
    with pytest.raises(MessengerError):
        prepare(town, lead_id)
    town.mayor.approve(action_id)
    town.executor.execute(action_id)
    assert lead_history(town.conn, lead_id)["first_drafted"]["id"] == action_id   # a shadow-day draft, like any other


def test_the_clerk_cancels_a_waiting_package_on_an_opt_out(town, ready):
    from packs.websites.clerk import cancel_waiting_emails
    lead_id, build_id = ready
    action_id, _ = proposed(town, build_id)
    assert cancel_waiting_emails(town.conn, lead_id, "opted out") == [action_id]
    assert town.mayor.get(action_id)["status"] == "rejected"


# ---------------- deciding: which part, and only the email is editable ----------------

def test_a_rejection_must_say_which_part(town, ready):
    _, build_id = ready
    action_id, _ = proposed(town, build_id)
    with pytest.raises(MayorError, match="which part"):
        town.mayor.reject(action_id, "too plain")
    town.mayor.reject(action_id, "site: too plain for a bakery")
    assert town.mayor.get(action_id)["reject_reason"] == "site: too plain for a bakery"
    assert package.reject_part("email: wrong tone") == "email" and package.reject_part("nope") is None


def test_only_the_email_part_can_be_edited_and_both_versions_are_kept(town, ready):
    _, build_id = ready
    action_id, content = proposed(town, build_id)
    with pytest.raises(PackageError, match="email's subject or body"):
        package.edit_email(content, "token", "ab" * 32)
    edited = package.edit_email(content, "subject", "A free website preview for Butterbell Bakery (edited)")
    town.mayor.edit_and_approve(action_id, edited)
    row = town.mayor.get(action_id)
    assert json.loads(row["original_content"])["email"]["subject"] != json.loads(row["edited_content"])["email"]["subject"]
    outcome = town.executor.execute(action_id)
    assert outcome.status == "executed"
    assert town.gmail.recorded()[0]["headers"]["Subject"].endswith("(edited)")


def test_the_commands_package_reject_with_a_part_and_edit(town, ready, instance_file, monkeypatch):
    # Each CLI invocation opens another Town; give that instance the scripted
    # process too, so this test never launches a real subscription job.
    original_open = launcher._open

    def scripted_open(*args, **kwargs):
        opened, publisher = original_open(*args, **kwargs)
        opened.cli._launch = FakeSonnet()
        return opened, publisher

    monkeypatch.setattr(launcher, "_open", scripted_open)
    _, build_id = ready
    result = runner.invoke(launcher.app, ["package", str(build_id)])
    assert result.exit_code == 0, result.stdout
    action_id = town.conn.execute("SELECT id FROM actions WHERE action_type = 'outreach_package'").fetchone()[0]
    refused = runner.invoke(launcher.app, ["reject", str(action_id), "--reason", "meh"])
    assert refused.exit_code == 1 and "which part" in " ".join(refused.stdout.split())
    bad_edit = runner.invoke(launcher.app, ["edit", str(action_id), "--set", "token=x"])
    assert bad_edit.exit_code == 1 and town.mayor.get(action_id)["status"] == "proposed"
    done = runner.invoke(launcher.app, ["reject", str(action_id), "--part", "email", "--reason", "too long"])
    assert done.exit_code == 0 and town.mayor.get(action_id)["reject_reason"] == "email: too long"


def test_town_build_by_hand_proposes_the_package_after_a_pass(town, ready, monkeypatch):
    from packs.websites.orders import after_pass_proposal
    _, build_id = ready
    line = after_pass_proposal(town, build_id, "package")
    assert line.startswith("Proposed the outreach package")
    assert {r[0] for r in town.conn.execute("SELECT action_type FROM actions WHERE status = 'proposed'")} == {
        "outreach_package"}


# ---------------- the live check itself (plain HTTP, no browser) ----------------

def test_the_live_check_needs_200_noindex_and_the_approved_page(town, ready, monkeypatch):
    from packs.websites.cloudflare import FetchedPage, RealCloudflare
    import hashlib
    approved = PAGE.format(name="Butterbell Bakery").encode()
    sha = hashlib.sha256(approved).hexdigest()
    answers = []
    monkeypatch.setattr(town.cf, "fetch", lambda url: answers.pop(0))
    town.cf.blocked_routes = {"public_dns", "pages_dev"}       # only the scripted first route answers here
    answers[:] = [FetchedPage(200, {}, sha), FetchedPage(200, {}, sha)]
    with pytest.raises(previews.PublishError, match="without the noindex header"):
        previews.check_live(town.publisher, "https://showcase.example.com/p/x/", approved, sleep=lambda s: None)
    other = b"<html><head><title>Someone else</title></head></html>"
    answers[:] = [FetchedPage(200, {"x-robots-tag": "noindex"}, hashlib.sha256(other).hexdigest(), other)] * 2
    with pytest.raises(previews.PublishError, match="different page"):
        previews.check_live(town.publisher, "https://showcase.example.com/p/x/", approved, sleep=lambda s: None)
    # Cloudflare may rewrite a small part of the HTML (it can hide an email address behind its own script): the
    # same title and the same /_astro/ files still count as the approved page.
    rewritten = approved.replace(b"<h1>", b'<script src="/cdn-cgi/scripts/email-decode.min.js"></script><h1>')
    answers[:] = [FetchedPage(200, {"x-robots-tag": "noindex"}, hashlib.sha256(rewritten).hexdigest(), rewritten)]
    assert "checked live" in previews.check_live(town.publisher, "https://showcase.example.com/p/x/", approved).line
    # The real client's fetch never runs inside pytest.
    with pytest.raises(Exception, match="never used in tests"):
        RealCloudflare(town.settings.state_folder / ".env", "x").fetch("https://showcase.example.com/")



# ---------------- the live check doesn't depend on Charlie's Wi-Fi (Project chat review, 2026-11-06) ----------------

def approved_and_run(town, build_id):
    action_id, content = proposed(town, build_id)
    town.mayor.approve(action_id)
    return action_id, content, town.executor.execute(action_id)


def route_logged(town, action_id):
    row = town.conn.execute("SELECT details FROM events WHERE event_type = 'package_live_checked' AND action_id = ?",
                            (action_id,)).fetchone()
    return json.loads(row["details"])["route"] if row else None


def test_local_dns_blocked_then_public_dns_over_https_checks_it(town, ready):
    _, build_id = ready
    town.cf.blocked_routes = {"local"}                 # Charlie's home Wi-Fi: showcase.example.com doesn't resolve
    action_id, _, outcome = approved_and_run(town, build_id)
    assert outcome.status == "executed", outcome.result
    assert "checked live through public DNS over HTTPS" in outcome.result and "Gmail draft" in outcome.result
    assert route_logged(town, action_id) == "public DNS over HTTPS (Cloudflare 1.1.1.1)"


def test_public_dns_blocked_too_then_the_pages_dev_address_checks_it(town, ready):
    _, build_id = ready
    town.cf.blocked_routes = {"local", "public_dns"}
    action_id, _, outcome = approved_and_run(town, build_id)
    assert outcome.status == "executed" and "pages.dev address" in outcome.result
    assert route_logged(town, action_id) == "the deployment's own pages.dev address"


def test_every_route_failing_stops_at_step_3_as_before(town, ready):
    _, build_id = ready
    town.cf.blocked_routes = {"local", "public_dns", "pages_dev"}
    action_id, content, outcome = approved_and_run(town, build_id)
    assert outcome.status == "failed" and "Stopped at step 3 (live check)" in outcome.result
    for route in previews.LIVE_ROUTES:
        assert route[0] in outcome.result                                  # what each route saw
    assert town.gmail.recorded() == [] and route_logged(town, action_id) is None


def test_a_route_counts_only_if_the_page_status_and_noindex_check_out(town, ready, monkeypatch):
    from packs.websites.cloudflare import FetchedPage
    _, build_id = ready
    monkeypatch.setattr(town.cf, "fetch", lambda url: FetchedPage(200, {}, "wrong page"))   # answers, but wrong
    action_id, _, outcome = approved_and_run(town, build_id)
    assert outcome.status == "executed" and route_logged(town, action_id).startswith("public DNS")


def test_the_real_client_resolves_by_public_dns_and_keeps_the_hostname_and_finds_pages_dev(tmp_path):
    from packs.websites.cloudflare import CloudflareError, RealCloudflare
    from tests.test_previews import _real_client_with_fakes
    client, _ = _real_client_with_fakes(tmp_path, "ok")
    asked = []
    client.doh = lambda host: asked.append(("doh", host)) or ["192.0.2.10", "192.0.2.11"]

    def pinned(url, ip):
        asked.append(("pinned", url, ip))
        if ip == "192.0.2.10":
            raise CloudflareError("first address down")
        return 200, {"x-robots-tag": "noindex"}, b"page"
    client.pinned_get = pinned
    page = client.fetch_public_dns("https://showcase.example.com/p/abc/")
    assert page.status == 200 and page.body == b"page"
    assert asked == [("doh", "showcase.example.com"), ("pinned", "https://showcase.example.com/p/abc/", "192.0.2.10"),
                     ("pinned", "https://showcase.example.com/p/abc/", "192.0.2.11")]   # the URL (Host, SNI) unchanged
    fetched = []
    client.get = lambda url: fetched.append(url) or (200, {"x-robots-tag": "noindex"}, b"page")
    client.fetch_pages_dev("https://showcase.example.com/p/abc/")
    assert fetched == ["https://abc.showcaseexample.pages.dev/p/abc/"]                 # the newest deployment, same path
    client.allow_in_tests = False
    for route in ("fetch", "fetch_public_dns", "fetch_pages_dev"):
        with pytest.raises(CloudflareError, match="never used in tests"):
            getattr(client, route)("https://showcase.example.com/")


# ---------------- invalid public emails (lead #12 Fictional Corner Shop) ----------------

FICTIONAL = "fictional'splace01@gmail.com"


@pytest.mark.parametrize("address, ok", [
    (FICTIONAL, False), ("fictional.splace01@gmail.com", True), ("fictional_place@gmail.com", False),
    ("fictional+x@googlemail.com", False), ("owner@fictionals-place.com", True), ("o'neil@fictionals.com", False),
    ("two@@x.com", False), ("a@b@c.com", False), ("noat.example.com", False), ("x@localhost", False),
    ("has space@x.com", False), ('"quoted"@x.com', False), ("dots..twice@x.com", False), ("", False),
    ("hi@shearbliss.example", True), ("first.last+tag@sub.domain.co", True)])
def test_one_plain_code_rule_for_email_addresses(address, ok):
    from packs.websites.leads import email_problem
    assert (email_problem(address) is None) == ok, (address, email_problem(address))


def test_whole_addresses_are_found_never_a_fragment():
    from packs.websites.leads import find_emails, mailto_address
    assert find_emails(f"Write to {FICTIONAL}.") == [FICTIONAL]                        # not "splace01@gmail.com"
    assert mailto_address("mailto:fictional%27splace01@gmail.com?subject=Hi") == FICTIONAL


def test_the_facts_check_compares_lead_12_s_address_whole(tmp_path):
    """It used to extract "splace01@gmail.com" from the text, then the URL-encoded form from the mailto: link, and
    fail a correct page twice. Whole addresses only, mailto: decoded."""
    from tests.test_builder import GOOD_PAGE, LEAD, check
    lead = {**LEAD, "public_email": FICTIONAL}
    page = GOOD_PAGE.replace(LEAD["public_email"], FICTIONAL).replace("</body>", f'<p>Email us: {FICTIONAL}</p><a href="mailto:fictional%27splace01@gmail.com">'
                                        'Email</a></body>')
    result = check(page, tmp_path, lead)
    assert not [p for p in result["problems"] if "email" in p.lower()], result["problems"]
    other = check(page.replace(FICTIONAL, "someone@else.com"), tmp_path, lead)
    assert any("email someone@else.com isn't the business's" in p for p in other["problems"])


def test_a_package_for_a_lead_with_an_invalid_email_is_never_made_and_blocked_if_it_exists(town, tmp_path):
    homepage_live(town, tmp_path)
    lead_id = add_lead(town, "Fictional Corner Shop", email=FICTIONAL)
    build_id = make_build(town, tmp_path, lead_id, name="Fictional Corner Shop")
    assert package.ready_builds(town) == []                                    # autopilot skips it
    with pytest.raises(PackageError, match="looks invalid"):
        propose_package(town, build_id)                                        # before any plan usage
    verdict = town.mayor.guard.recheck(town.conn, "outreach_package", "t", {
        "build_id": build_id, "lead_id": lead_id, "email": {"kind": "first", "lead_id": lead_id, "to": FICTIONAL,
                                                            "from": "Example Web Studio <town@example.com>",
                                                            "subject": "s", "body": "b", "build_id": build_id}})
    assert any(f"email address looks invalid: {FICTIONAL}" in b for b in verdict.blocks)
