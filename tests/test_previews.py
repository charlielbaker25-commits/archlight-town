"""Preview links on Cloudflare Pages. Everything here is fake: the fake Cloudflare client records what it
would have deployed, and the real client is only ever given a fake Wrangler and a fake HTTP function.
Nothing in this file can reach Cloudflare or the internet."""
import hashlib
import json
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

import launcher
from engine.db import utc_now
from engine.executor import ExecutorError
from engine.killswitch import TownStopped
from engine.limits import Limits
from engine.settings import load_settings
from engine.town import open_town
from packs.websites import cloudflare, pack, previews
from packs.websites.cloudflare import CloudflareError, FakeCloudflare, RealCloudflare
from packs.websites.critic_measure import dist_hash
from packs.websites.previews import PublishError, Publisher
from tests.test_builder import add_lead

runner = CliRunner()
PAGE = '<!doctype html><html lang="en"><head><link rel="stylesheet" href="/_astro/site.A1.css"></head><body>Hi</body></html>'
SECRET_TOKEN = "cf-secret-token-0123456789abcdef"
SECRET_ACCOUNT = "acct0123456789abcdef0123456789ab"


@pytest.fixture
def town(instance_file, limits_file):
    settings = load_settings(instance_file)
    fake = FakeCloudflare(settings.state_folder / "cloudflare-fake")
    publisher = Publisher(fake, Limits(limits_file), None, Path(settings.database).parent / "deploys")  # type: ignore[arg-type]
    t = open_town(settings, pack.PACK_SCHEMAS, {**pack.handlers(), **publisher.handlers()}, limits_file,
                  pack.MODELS_FILE, live_external_types=pack.LIVE_EXTERNAL_TYPES)
    publisher.killswitch = t.killswitch
    t.killswitch.resume(t.conn)
    t.publisher, t.fake = publisher, fake   # handy for the tests
    yield t
    t.close()


def make_build(town, tmp_path, kind="preview", verdict="pass", page=PAGE, assets=None, lead=True, pages=None):
    """A finished build with a Critic review of exactly its files. `pages`: any other page, {path: html}."""
    ws = tmp_path / f"ws-{time.time_ns()}"
    dist = ws / "dist"
    (dist / "_astro").mkdir(parents=True)
    (dist / "index.html").write_text(page, encoding="utf-8")
    for name, html in (pages or {}).items():
        (dist / name).parent.mkdir(parents=True, exist_ok=True)
        (dist / name).write_text(html, encoding="utf-8")
    for name, data in (assets or {"site.A1.css": b"body{}"}).items():
        (dist / "_astro" / name).write_bytes(data)
    lead_id = add_lead(town) if kind == "preview" and lead else None
    digest = dist_hash(dist)
    build_id = town.conn.execute(
        "INSERT INTO builds (lead_id, workspace, status, kind, dist_hash, created_at) VALUES (?, ?, 'built', ?, ?, ?)",
        (lead_id, str(ws), kind, digest, utc_now())).lastrowid
    if verdict:
        town.conn.execute("INSERT INTO reviews (build_id, workspace, dist_hash, verdict, decided_by, summary, created_at)"
                          " VALUES (?, ?, ?, ?, 'critic', 'Honest and distinctive.', ?)",
                          (build_id, str(ws), digest, verdict, utc_now()))
    return build_id


def publish(town, build_id):
    action_id = previews.propose_publish(town, build_id)
    town.mayor.approve(action_id)
    return action_id, town.executor.execute(action_id)


def live_homepage(town, tmp_path):
    build_id = make_build(town, tmp_path, kind="homepage")
    _, outcome = publish(town, build_id)
    assert outcome.status == "executed", outcome.result
    return build_id


def last_files(town):
    return town.fake.recorded()[-1]["files"]


# ---------------- the approval gate ----------------

def test_nothing_is_published_without_approval(town, tmp_path):
    build_id = make_build(town, tmp_path, kind="homepage")
    action_id = previews.propose_publish(town, build_id)
    with pytest.raises(ExecutorError):
        town.executor.execute(action_id)                     # proposed, not approved
    town.mayor.reject(action_id, "not yet")
    with pytest.raises(ExecutorError):
        town.executor.execute(action_id)                     # rejected
    assert town.fake.recorded() == []
    assert town.conn.execute("SELECT COUNT(*) FROM previews").fetchone()[0] == 0


def test_publishing_really_runs_once_approved_and_only_once(town, tmp_path):
    build_id = make_build(town, tmp_path, kind="homepage")
    action_id, outcome = publish(town, build_id)
    assert outcome.status == "executed" and not outcome.dry_run and "FAKE Cloudflare" in outcome.result
    assert len(town.fake.recorded()) == 1
    with pytest.raises(ExecutorError):
        town.executor.execute(action_id)
    assert len(town.fake.recorded()) == 1


@pytest.mark.parametrize("verdict", [None, "fail"])
def test_no_proposal_without_a_critic_pass(town, tmp_path, verdict):
    build_id = make_build(town, tmp_path, kind="homepage", verdict=verdict)
    with pytest.raises(PublishError, match="Critic"):
        previews.propose_publish(town, build_id)
    assert town.conn.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 0


def test_no_preview_until_the_homepage_is_live(town, tmp_path):
    build_id = make_build(town, tmp_path)
    with pytest.raises(PublishError, match="homepage isn't live"):
        previews.propose_publish(town, build_id)
    live_homepage(town, tmp_path)
    assert previews.propose_publish(town, build_id)


def test_a_test_set_build_is_never_proposed(town, tmp_path):
    live_homepage(town, tmp_path)
    build_id = make_build(town, tmp_path, lead=False)
    with pytest.raises(PublishError, match="test-set"):
        previews.propose_publish(town, build_id)


def test_files_changed_after_approval_are_refused(town, tmp_path):
    build_id = make_build(town, tmp_path, kind="homepage")
    action_id = previews.propose_publish(town, build_id)
    town.mayor.approve(action_id)
    workspace = town.conn.execute("SELECT workspace FROM builds WHERE id = ?", (build_id,)).fetchone()[0]
    (Path(workspace) / "dist" / "index.html").write_text(PAGE + "<!-- changed -->", encoding="utf-8")
    outcome = town.executor.execute(action_id)
    assert outcome.status == "failed" and "Critic" in outcome.result
    assert town.fake.recorded() == []


def test_a_publish_cant_be_redirected_to_another_build(town, tmp_path):
    live_homepage(town, tmp_path)
    first, second = make_build(town, tmp_path), make_build(town, tmp_path, page=PAGE + "<p>two</p>")
    action_id = previews.propose_publish(town, first)
    content = json.loads(town.mayor.get(action_id)["content"])
    other = json.loads(town.mayor.get(previews.propose_publish(town, second))["content"])
    # Point this proposal (and its token) at the other build and its hash: refused at execution.
    town.mayor.edit_and_approve(action_id, {**content, "build_id": second, "dist_hash": other["dist_hash"]})
    outcome = town.executor.execute(action_id)
    assert outcome.status == "failed" and "preview path" in outcome.result


def test_the_edit_command_refuses_a_publish(dev_instance, tmp_path):
    town = lambda *args: runner.invoke(launcher.app, ["--instance", "dev", *args])
    town("resume")
    dev, _ = launcher._open(load_settings(dev_instance), "charlie", dev_instance)
    build_id = make_build(dev, tmp_path, kind="homepage")
    action_id = previews.propose_publish(dev, build_id)
    dev.close()
    result = town("edit", str(action_id), "--set", "url=https://example.com/")
    assert result.exit_code == 1 and "can't" in result.stdout


# ---------------- dev can never publish for real ----------------

def test_only_charlies_real_instance_gets_the_real_client(dev_instance, tmp_path):
    real_file = dev_instance.parent / "instance.yaml"
    real, dev = load_settings(real_file), load_settings(dev_instance)
    assert isinstance(launcher._cloudflare(real, real_file, "charlie"), RealCloudflare)
    assert isinstance(launcher._cloudflare(dev, dev_instance, "charlie"), FakeCloudflare)
    assert isinstance(launcher._cloudflare(real, real_file, "claude-code"), FakeCloudflare)
    other = tmp_path / "copy.yaml"
    other.write_text(real_file.read_text(encoding="utf-8"), encoding="utf-8")
    assert isinstance(launcher._cloudflare(load_settings(other), other, "charlie"), FakeCloudflare)
    # A dev file that shares the real database counts as "real" for Claude Code's permissions, but it
    # still says `instance: dev`, so it can never get the real client.
    shared = tmp_path / "shared.yaml"
    shared.write_text("instance: dev\n" + real_file.read_text(encoding="utf-8"), encoding="utf-8")
    assert isinstance(launcher._cloudflare(load_settings(shared), shared, "charlie"), FakeCloudflare)


def test_the_dev_instance_never_reaches_the_real_client(dev_instance, tmp_path, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("the dev instance touched the real Cloudflare client")
    for name in ("__init__", "deploy", "list_deployments", "delete_deployment", "ensure_project", "check"):
        monkeypatch.setattr(RealCloudflare, name, explode)
    town = lambda *args: runner.invoke(launcher.app, ["--instance", "dev", *args])
    assert town("resume").exit_code == 0
    dev, _ = launcher._open(load_settings(dev_instance), "charlie", dev_instance)
    build_id = make_build(dev, tmp_path, kind="homepage")
    dev.close()
    assert town("propose-publish", str(build_id)).exit_code == 0
    result = town("approve", "1")
    assert result.exit_code == 0 and "FAKE Cloudflare" in result.stdout, result.stdout
    check = town("cloudflare-check")
    assert "FAKE Cloudflare client" in check.stdout and "Recorded deployments: 1" in check.stdout
    recorded = json.loads((dev_instance.parent / "dev" / "state" / "cloudflare-fake" / "deployments.json")
                          .read_text(encoding="utf-8"))
    assert len(recorded["deployments"]) == 1


def test_the_real_client_refuses_to_run_inside_pytest(tmp_path):
    client = RealCloudflare(tmp_path / ".env", "showcaseexample")
    with pytest.raises(CloudflareError, match="never used in tests"):
        client.list_deployments()
    with pytest.raises(CloudflareError, match="never used in tests"):
        client.deploy(tmp_path)


def test_claude_code_on_the_real_town_cant_publish_or_take_down(dev_instance, monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    for command in (["propose-publish", "1"], ["takedown", "1", "--reason", "x"], ["cloudflare-check"],
                    ["previews"]):
        result = runner.invoke(launcher.app, command)
        assert result.exit_code == 1 and "Charlie's call" in result.stdout, command


# ---------------- the token stays secret ----------------

def _real_client_with_fakes(tmp_path, wrangler_output):
    env = tmp_path / ".env"
    env.write_text(f"CLOUDFLARE_API_TOKEN={SECRET_TOKEN}\nCLOUDFLARE_ACCOUNT_ID={SECRET_ACCOUNT}\n"
                   "ANTHROPIC_API_KEY=sk-ant-demo\n", encoding="utf-8")
    seen = {}

    def fake_wrangler(command, **kwargs):
        seen["command"], seen["env"] = command, kwargs["env"]
        return subprocess.CompletedProcess(command, 1 if wrangler_output.startswith("fail") else 0,
                                           stdout=wrangler_output, stderr="")

    def fake_http(method, url, headers, body):
        if method == "GET" and url.endswith("per_page=25"):
            return 200, {"success": True, "result": [{"id": "d2", "url": "https://abc.showcaseexample.pages.dev",
                                                      "created_on": "2026-10-21T10:00:00Z"}],
                         "result_info": {"total_pages": 1}}
        return 403, {"success": False, "errors": [{"message": f"denied for token {SECRET_TOKEN} in {SECRET_ACCOUNT}"}]}

    client = RealCloudflare(env, "showcaseexample", runner=fake_wrangler, http=fake_http, node="node")
    client.allow_in_tests = True   # only ever with the fake Wrangler and fake HTTP above
    return client, seen


def test_wrangler_gets_only_the_cloudflare_secrets(tmp_path, monkeypatch):
    monkeypatch.setattr(cloudflare, "WRANGLER_JS", tmp_path / "wrangler.js")
    (tmp_path / "wrangler.js").write_text("", encoding="utf-8")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-demo")
    client, seen = _real_client_with_fakes(tmp_path, "Deployment complete! https://abc.showcaseexample.pages.dev")
    assert client.deploy(tmp_path).id == "d2"
    env = seen["env"]
    assert env["CLOUDFLARE_API_TOKEN"] == SECRET_TOKEN and env["CLOUDFLARE_ACCOUNT_ID"] == SECRET_ACCOUNT
    assert not [k for k in env if k.startswith(("ANTHROPIC", "CLAUDE"))]
    assert set(env) <= set(cloudflare.SYSTEM_VARS) | {"CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID",
                                                      "WRANGLER_SEND_METRICS", "CI", "NO_COLOR"}
    assert SECRET_TOKEN not in " ".join(seen["command"])


def test_the_token_never_appears_in_errors_logs_or_output(town, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cloudflare, "WRANGLER_JS", tmp_path / "wrangler.js")
    (tmp_path / "wrangler.js").write_text("", encoding="utf-8")
    client, _ = _real_client_with_fakes(tmp_path, f"fail: bad token {SECRET_TOKEN} for account {SECRET_ACCOUNT}")
    with pytest.raises(CloudflareError) as failed:
        client.deploy(tmp_path)
    with pytest.raises(CloudflareError) as denied:
        client.delete_deployment("d1")
    lines = client.check()
    town.publisher.client = client
    _, outcome = publish(town, make_build(town, tmp_path, kind="homepage"))   # fails at Wrangler, via the executor
    assert outcome.status == "failed"
    everything = " ".join([str(failed.value), str(denied.value), json.dumps(lines), outcome.result,
                           capsys.readouterr().out] +
                          [r[0] for r in town.conn.execute("SELECT details FROM events")] +
                          [r[0] or "" for r in town.conn.execute("SELECT result FROM actions")])
    assert "[hidden]" in everything
    assert SECRET_TOKEN not in everything and SECRET_ACCOUNT not in everything


def _checked_client(tmp_path, workers_delete_status, zones=()):
    """A real client whose HTTP is a fake Cloudflare: a Pages-only token, as Charlie's is, except for the
    answer to the Workers delete probe and which DNS zones it can see."""
    env = tmp_path / ".env"
    env.write_text(f"CLOUDFLARE_API_TOKEN={SECRET_TOKEN}\nCLOUDFLARE_ACCOUNT_ID={SECRET_ACCOUNT}\n", encoding="utf-8")
    calls = []

    def fake_http(method, url, headers, body):
        calls.append((method, url))
        if url.endswith("/user/tokens/verify"):
            return 200, {"success": True, "result": {"status": "active"}}
        if "/workers/scripts" in url and method == "GET":
            # Cloudflare merged Workers and Pages: a Pages-only token can READ the Workers list.
            return 200, {"success": True, "result": [{"id": "someone-elses-worker"}]}
        if "/workers/scripts/" in url and method == "DELETE":
            return workers_delete_status, {"success": False, "errors": [{"code": 10000 if workers_delete_status == 403
                                                                         else 10007, "message": "x"}]}
        if "/zones" in url:
            return 200, {"success": True, "result": [{"name": z} for z in zones]}
        if url.endswith("/pages/projects/showcaseexample"):
            return 200, {"success": True, "result": {"domains": ["showcaseexample.pages.dev", "showcase.example.com"]}}
        if "/pages/projects" in url:
            return 200, {"success": True, "result": []}
        return 500, {"success": False}

    client = RealCloudflare(env, "showcaseexample", http=fake_http, node="node")
    client.allow_in_tests = True   # only ever with the fake HTTP above
    return client, calls


def test_cloudflare_check_accepts_a_pages_only_token_that_can_read_workers(tmp_path):
    client, calls = _checked_client(tmp_path, workers_delete_status=403)
    lines = client.check()
    assert all(passed for passed, _ in lines), lines
    assert any("can't change Workers" in line for _, line in lines)
    assert any("showcase.example.com" in line for _, line in lines)
    # Read-only, apart from ONE delete of a Workers script that doesn't exist (a random probe name).
    writes = [(m, u) for m, u in calls if m != "GET"]
    assert len(writes) == 1 and writes[0][0] == "DELETE"
    assert f"/workers/scripts/{cloudflare.WORKERS_PROBE_PREFIX}" in writes[0][1]
    assert "someone-elses-worker" not in writes[0][1]


def test_cloudflare_check_fails_a_token_that_can_write_workers_or_see_dns(tmp_path):
    client, _ = _checked_client(tmp_path, workers_delete_status=404)      # "no such script": it was allowed to try
    assert [line for passed, line in client.check() if not passed][0].startswith("It CAN change Workers")
    client, _ = _checked_client(tmp_path, workers_delete_status=500)      # can't tell: fail closed
    assert any(not passed and "Couldn't tell" in line for passed, line in client.check())
    client, _ = _checked_client(tmp_path, workers_delete_status=403, zones=["showcase.example.com"])
    assert any(not passed and "DNS zones" in line for passed, line in client.check())


# ---------------- what goes live ----------------

def test_the_live_set_is_the_homepage_and_every_unexpired_preview_and_nothing_else(town, tmp_path):
    live_homepage(town, tmp_path)
    a = make_build(town, tmp_path, assets={"site.A1.css": b"body{}", "a.B2.woff2": b"font-a"})
    b = make_build(town, tmp_path, page=PAGE + "<p>b</p>", assets={"b.C3.woff2": b"font-b"})
    never = make_build(town, tmp_path, page=PAGE + "<p>never</p>")          # Critic-passed, never approved
    for build_id in (a, b):
        publish(town, build_id)
    previews.propose_publish(town, never)
    tokens = {r[0]: r[1] for r in town.conn.execute("SELECT build_id, token FROM previews WHERE token IS NOT NULL")}
    assert set(last_files(town)) == {"index.html", "_astro/site.A1.css", "_astro/a.B2.woff2", "_astro/b.C3.woff2",
                                     "_headers", "robots.txt", f"p/{tokens[a]}/index.html", f"p/{tokens[b]}/index.html"}
    # Expire a: the next deploy has the homepage and b only.
    town.conn.execute("UPDATE previews SET expires_at = ? WHERE build_id = ?", ("2000-01-01T00:00:00Z", a))
    assert previews.upkeep(town.publisher, town.conn) == 1
    assert set(last_files(town)) == {"index.html", "_astro/site.A1.css", "_astro/b.C3.woff2", "_headers",
                                     "robots.txt", f"p/{tokens[b]}/index.html"}


def test_files_go_live_byte_for_byte(town, tmp_path):
    live_homepage(town, tmp_path)
    build_id = make_build(town, tmp_path, page=PAGE + "<p>exact</p>")
    publish(town, build_id)
    token = town.conn.execute("SELECT token FROM previews WHERE build_id = ?", (build_id,)).fetchone()[0]
    assert last_files(town)[f"p/{token}/index.html"] == hashlib.sha256((PAGE + "<p>exact</p>").encode()).hexdigest()


def test_noindex_and_robots_txt_are_in_every_deploy(town, tmp_path):
    live_homepage(town, tmp_path)
    folder = tmp_path / "assembled"
    previews.assemble(previews.live_sites(town.conn), folder)
    headers = (folder / "_headers").read_text(encoding="utf-8")
    assert re.search(r"^/p/\*\n\s+X-Robots-Tag: noindex$", headers, re.MULTILINE)
    assert "https://:project.pages.dev/*\n  X-Robots-Tag: noindex" in headers
    assert not re.search(r"^/\*?$", headers, re.MULTILINE)            # no rule makes the homepage noindex
    assert (folder / "robots.txt").read_text(encoding="utf-8") == "User-agent: *\nDisallow: /p/\n"
    assert {"_headers", "robots.txt"} <= set(last_files(town))


def test_a_clash_in_shared_astro_files_is_refused(town, tmp_path):
    live_homepage(town, tmp_path)
    _, outcome = publish(town, make_build(town, tmp_path, assets={"site.A1.css": b"different bytes"}))
    assert outcome.status == "failed" and "different files named _astro/site.A1.css" in outcome.result


def test_a_site_with_other_root_files_cant_be_proposed(town, tmp_path):
    build_id = make_build(town, tmp_path, kind="homepage")
    dist = Path(town.conn.execute("SELECT workspace FROM builds WHERE id = ?", (build_id,)).fetchone()[0]) / "dist"
    (dist / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    town.conn.execute("UPDATE reviews SET dist_hash = ? WHERE build_id = ?", (dist_hash(dist), build_id))
    with pytest.raises(PublishError, match="outside index.html and _astro"):
        previews.propose_publish(town, build_id)


PRIVACY = '<!doctype html><html lang="en"><body><h1>Privacy</h1><a href="/">Home</a></body></html>'


def test_the_homepages_privacy_page_goes_live_at_privacy(town, tmp_path):
    build_id = make_build(town, tmp_path, kind="homepage", pages={"privacy/index.html": PRIVACY})
    _, outcome = publish(town, build_id)
    assert outcome.status == "executed", outcome.result
    files = last_files(town)
    assert files["privacy/index.html"] == hashlib.sha256(PRIVACY.encode()).hexdigest()
    assert files["index.html"] == hashlib.sha256(PAGE.encode()).hexdigest()
    # A preview published later keeps the privacy page live.
    publish(town, make_build(town, tmp_path, page=PAGE + "<p>later</p>"))
    assert "privacy/index.html" in last_files(town)


def test_only_the_homepage_may_have_a_privacy_page(town, tmp_path):
    live_homepage(town, tmp_path)
    preview = make_build(town, tmp_path, pages={"privacy/index.html": PRIVACY})
    with pytest.raises(PublishError, match="outside index.html and _astro"):
        previews.propose_publish(town, preview)
    extra = make_build(town, tmp_path, kind="homepage", pages={"privacy/index.html": PRIVACY,
                                                               "privacy/notes.html": PRIVACY})
    with pytest.raises(PublishError, match="privacy/notes.html"):
        previews.propose_publish(town, extra)


# ---------------- preview paths ----------------

def test_preview_paths_are_random_long_and_never_reused(town, tmp_path):
    some_build = make_build(town, tmp_path, kind="homepage")
    tokens = {previews.new_token(town.conn, some_build) for _ in range(200)}
    assert len(tokens) == 200 and all(re.fullmatch(r"[0-9a-f]{64}", t) for t in tokens)   # 256 bits each
    live_homepage(town, tmp_path)
    build_id = make_build(town, tmp_path)
    first = previews.propose_publish(town, build_id)
    town.mayor.reject(first, "not this week")
    second = previews.propose_publish(town, build_id)
    token = lambda a: json.loads(town.mayor.get(a)["content"])["token"]
    assert token(first) != token(second)
    assert town.conn.execute("SELECT COUNT(*) FROM preview_tokens WHERE token = ?", (token(first),)).fetchone()[0] == 1


def test_claude_code_sees_private_paths_hidden(dev_instance, tmp_path, monkeypatch):
    town = lambda *args: runner.invoke(launcher.app, ["--instance", "dev", *args])
    town("resume")
    dev, _ = launcher._open(load_settings(dev_instance), "charlie", dev_instance)
    live_homepage(dev, tmp_path)
    previews.propose_publish(dev, make_build(dev, tmp_path))
    dev.close()
    unwrapped = lambda result: "".join(result.stdout.splitlines())   # the terminal wraps long lines
    assert re.search(r"/p/[0-9a-f]{64}/", unwrapped(town("pending")))
    monkeypatch.setenv("CLAUDECODE", "1")
    shown = unwrapped(town("pending")) + unwrapped(town("log", "--full", "-n", "200"))
    assert "<private>" in shown and not re.search(r"[0-9a-f]{64}", shown)


# ---------------- coming down ----------------

def test_expiry_takes_a_preview_down_and_deletes_old_deployments(town, tmp_path):
    live_homepage(town, tmp_path)
    build_id = make_build(town, tmp_path)
    publish(town, build_id)
    token, expires = town.conn.execute("SELECT token, expires_at FROM previews WHERE build_id = ?", (build_id,)).fetchone()
    left = datetime.strptime(expires, previews.TIME).replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)
    assert timedelta(days=20, hours=23) < left <= timedelta(days=21)                 # previews.expire_days
    assert previews.upkeep(town.publisher, town.conn) == 0                            # not due yet
    town.conn.execute("UPDATE previews SET expires_at = ? WHERE build_id = ?", ("2000-01-01T00:00:00Z", build_id))
    assert previews.upkeep(town.publisher, town.conn) == 1
    recorded = town.fake.recorded()
    assert all(d["deleted"] for d in recorded[:-1]) and not recorded[-1]["deleted"]   # only the newest is left
    assert any(f"p/{token}/index.html" in d["files"] for d in recorded[:-1])          # ...the old ones that had it are gone
    assert f"p/{token}/index.html" not in recorded[-1]["files"]
    events = [r[0] for r in town.conn.execute("SELECT event_type FROM events WHERE agent = 'builder'")]
    assert "preview_expired" in events and "deployment_deleted" in events
    assert town.conn.execute("SELECT status FROM previews WHERE build_id = ?", (build_id,)).fetchone()[0] == "expired"


def test_takedowns_never_run_while_the_town_is_stopped(town, tmp_path):
    live_homepage(town, tmp_path)
    build_id = make_build(town, tmp_path)
    publish(town, build_id)
    town.conn.execute("UPDATE previews SET expires_at = ? WHERE build_id = ?", ("2000-01-01T00:00:00Z", build_id))
    deploys = len(town.fake.recorded())
    town.killswitch.stop(town.conn)
    assert previews.upkeep(town.publisher, town.conn) == 0
    with pytest.raises(TownStopped):
        previews.takedown(town.publisher, town.conn, build_id, "owner said no", "charlie")
    assert len(town.fake.recorded()) == deploys
    town.killswitch.resume(town.conn)
    assert previews.upkeep(town.publisher, town.conn) == 1          # comes down at the next resume


def test_an_expired_preview_comes_down_at_town_resume(dev_instance, tmp_path):
    town = lambda *args: runner.invoke(launcher.app, ["--instance", "dev", *args])
    town("resume")
    dev, _ = launcher._open(load_settings(dev_instance), "charlie", dev_instance)
    live_homepage(dev, tmp_path)
    build_id = make_build(dev, tmp_path)
    publish(dev, build_id)
    dev.conn.execute("UPDATE previews SET expires_at = ? WHERE build_id = ?", ("2000-01-01T00:00:00Z", build_id))
    dev.close()
    town("stop")
    assert "expired" not in town("status").stdout                      # stopped: nothing comes down
    result = town("resume")
    assert "1 preview(s) expired and came down" in result.stdout, result.stdout


def test_a_takedown_needs_a_reason_and_is_logged(town, tmp_path):
    live_homepage(town, tmp_path)
    build_id = make_build(town, tmp_path)
    publish(town, build_id)
    with pytest.raises(PublishError, match="reason"):
        previews.takedown(town.publisher, town.conn, build_id, " ", "charlie")
    previews.takedown(town.publisher, town.conn, build_id, "owner said no", "charlie")
    row = town.conn.execute("SELECT status, end_reason FROM previews WHERE build_id = ?", (build_id,)).fetchone()
    assert tuple(row) == ("taken_down", "owner said no")
    assert all(d["deleted"] for d in town.fake.recorded()[:-1])


def test_a_failed_delete_is_retried_at_the_next_upkeep(town, tmp_path):
    live_homepage(town, tmp_path)
    town.fake.fail_delete = True
    publish(town, make_build(town, tmp_path))
    assert not town.fake.recorded()[0]["deleted"]
    town.fake.fail_delete = False
    previews.upkeep(town.publisher, town.conn)
    assert all(d["deleted"] for d in town.fake.recorded()[:-1])


def test_the_monthly_deploy_cap_comes_from_limits_yaml(town, tmp_path, limits_file):
    text = limits_file.read_text(encoding="utf-8").replace("deploys_per_month: 450", "deploys_per_month: 1")
    limits_file.write_text(text, encoding="utf-8")
    live_homepage(town, tmp_path)
    _, outcome = publish(town, make_build(town, tmp_path))
    assert outcome.status == "failed" and "deploys this month" in outcome.result
