"""Gmail (session H, go-live week): drafts, the Clerk's reads, and exactly one sending call (drafts.send, from
send_draft only). Every test uses FakeGmail or a fake HTTP session; none reaches Google, and the real client
refuses to run inside pytest. The send switch's gates are in tests/test_golive.py."""
import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

import launcher
from engine.executor import ExecutorError
from engine.killswitch import KillSwitch, TownStopped
from engine.mayor import MayorError
from engine.settings import load_settings
from engine.town import open_town
from packs.websites import gmail, pack
from packs.websites.gmail import (COMPOSE_SCOPE, READONLY_SCOPE, SIGN_IN_AGAIN, EndpointRefused, FakeGmail, GmailError,
                                  GmailSignInNeeded, RealGmail, build_message, read_message)
from packs.websites.guard import OPT_OUT_LINE, WebsiteGuard, dnc_add
from packs.websites.messenger import FROM, assemble_email
from packs.websites.outreach import Outbox, OutboxRefused
from tests.test_guard import FakeHaiku
from tests.test_messenger import ADDRESS, add_lead
from tests.test_testset import no_network  # noqa: F401

REPO = Path(__file__).resolve().parents[1]
CLIENT_SECRET = "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en"
ACCESS_TOKEN = "ya29.access-token-must-stay-hidden"
REFRESH_TOKEN = "1//refresh-token-must-stay-hidden"
# Built from the code's own constants, so this file never spells out a sending path itself (the codebase scans below).
SEND = gmail.SEND_ENDPOINT[1].removeprefix("/drafts")          # the send suffix
BOTH = (COMPOSE_SCOPE, READONLY_SCOPE)
DENIED_PATHS = [word for word in gmail.DENIED_ENDPOINTS if word.startswith("/")]     # e.g. the modify and trash paths
BATCH = next(word for word in gmail.DENIED_ENDPOINTS if word.startswith("batch"))
runner = CliRunner()


# ---------------- a Town whose emails go to FakeGmail ----------------

def open_gmail_town(instance_file, limits_file, fake, mailing_address=ADDRESS, real=False):
    settings = load_settings(instance_file)
    outbox = Outbox(fake, KillSwitch(settings.killswitch_file))
    t = open_town(settings, pack.PACK_SCHEMAS, pack.handlers(outbox), limits_file, pack.MODELS_FILE,
                  api_client=FakeHaiku(), api_key_loader=lambda: "sk-test")
    t.killswitch.resume(t.conn)
    t.mayor.guard = t.executor.guard = WebsiteGuard(t.limits, t.budget, t.llm, mailing_address, real=real)
    t.fake, t.outbox = fake, outbox
    return t


@pytest.fixture
def town(instance_file, limits_file, tmp_path):
    t = open_gmail_town(instance_file, limits_file, FakeGmail(tmp_path / "gmail-fake"))
    yield t
    t.close()


def email_content(lead_id, to="butterbellbakery@example.com", subject="A free website preview for Butterbell Bakery"):
    body = assemble_email("Hi there,\n\nWe couldn't find a website for Butterbell Bakery, so we built a free preview:\n"
                          f"https://showcase.example.com/p/{'ab' * 32}/\n\nCafé-friendly, for far less than a traditional "
                          "web design firm usually charges.", ADDRESS)
    return {"kind": "first", "lead_id": lead_id, "from": FROM, "to": to, "subject": subject, "body": body}


def propose_email(town):
    lead_id = add_lead(town)
    return lead_id, town.mayor.propose("messenger", "send_email", "Butterbell Bakery", email_content(lead_id),
                                       "first email")


def status(town, action_id):
    return town.mayor.get(action_id)["status"]


# ---------------- approving an email saves exactly one draft and sends nothing ----------------

def test_approving_an_email_saves_exactly_one_draft_with_the_approved_content(town, no_network):
    _, action_id = propose_email(town)
    approved = json.loads(town.mayor.get(action_id)["content"])
    town.mayor.approve(action_id)
    outcome = town.executor.execute(action_id)
    assert outcome.status == "executed" and outcome.dry_run
    assert outcome.result == "dry-run: not sent; saved as Gmail draft fake-draft-1"
    [draft] = town.fake.recorded()
    assert draft["headers"]["To"] == "butterbellbakery@example.com"
    assert draft["headers"]["From"] == "Example Web Studio <town@example.com>"
    assert draft["headers"]["Subject"] == approved["subject"]
    assert draft["headers"]["List-Unsubscribe"] == "<mailto:town@example.com?subject=unsubscribe>"
    assert draft["body"].rstrip("\n") == approved["body"] and OPT_OUT_LINE in draft["body"]
    row = town.conn.execute("SELECT * FROM gmail_drafts WHERE action_id = ?", (action_id,)).fetchone()
    assert (row["draft_id"], row["message_id"], row["thread_id"], row["fake"]) == \
        ("fake-draft-1", "fake-message-1", "fake-thread-1", 1)
    event = town.conn.execute("SELECT details FROM events WHERE event_type = 'gmail_draft_created'").fetchone()
    assert json.loads(event["details"])["sent"] is False
    assert no_network == []
    with pytest.raises(ExecutorError):                       # it ran once; it never runs again
        town.executor.execute(action_id)
    assert len(town.fake.recorded()) == 1


def test_an_edited_email_is_saved_as_edited(town):
    _, action_id = propose_email(town)
    edited = {**json.loads(town.mayor.get(action_id)["content"]), "subject": "Your free preview, Butterbell Bakery"}
    town.mayor.edit_and_approve(action_id, edited)
    town.executor.execute(action_id)
    [draft] = town.fake.recorded()
    assert draft["headers"]["Subject"] == "Your free preview, Butterbell Bakery"


def test_send_email_is_never_live_and_its_run_never_sends():
    assert "send_email" not in pack.LIVE_EXTERNAL_TYPES
    with pytest.raises(RuntimeError, match="Nothing was sent"):
        pack.handlers()["send_email"].run(None, 1, {})


def test_the_draft_is_plain_text_utf8_with_the_unsubscribe_header():
    raw = build_message("owner@example.com", "A preview for Café Bleu", "Hi there,\nCafé ☕\n")
    assert 'Content-Type: text/plain; charset="utf-8"' in raw.decode("ascii")   # 7-bit safe on the wire
    message = read_message(raw)
    assert message["headers"]["Subject"] == "A preview for Café Bleu" and message["body"] == "Hi there,\nCafé ☕\n"


# ---------------- what stops a draft ----------------

def test_a_blocked_email_creates_no_draft(instance_file, limits_file, tmp_path):
    fake = FakeGmail(tmp_path / "gmail-fake")
    t = open_gmail_town(instance_file, limits_file, fake, mailing_address="", real=True)   # no mailing address yet
    try:
        _, action_id = propose_email(t)
        assert json.loads(t.mayor.get(action_id)["guard_result"])["verdict"] == "blocked"
        with pytest.raises(MayorError):
            t.mayor.approve(action_id)
        with pytest.raises(ExecutorError):
            t.executor.execute(action_id)
        assert fake.recorded() == [] and fake.calls == 0
    finally:
        t.close()


def test_the_do_not_contact_list_stops_the_draft_at_execution(town):
    lead_id, action_id = propose_email(town)
    town.mayor.approve(action_id)
    dnc_add(town.conn, "butterbellbakery@example.com", "asked us to stop", "charlie")
    with pytest.raises(ExecutorError, match="do-not-contact"):
        town.executor.execute(action_id)
    assert town.fake.calls == 0 and status(town, action_id) == "approved"
    # and the Outbox's own check, even without the Guard in front of it
    with pytest.raises(OutboxRefused, match="do-not-contact"):
        town.outbox.save_draft(town.conn, action_id, email_content(lead_id))
    assert town.fake.calls == 0


def test_a_to_address_that_isnt_the_leads_stored_email_is_refused(town):
    lead_id = add_lead(town)
    with pytest.raises(OutboxRefused, match="isn't the lead's stored public email"):
        town.outbox.save_draft(town.conn, 1, email_content(lead_id, to="someone-else@example.com"))
    town.conn.execute("UPDATE leads SET public_email = 'changed@example.com' WHERE id = ?", (lead_id,))
    with pytest.raises(OutboxRefused):                        # the lead's email changed after approval
        town.outbox.save_draft(town.conn, 1, email_content(lead_id))
    assert town.fake.calls == 0


def test_the_kill_switch_stops_a_draft(town):
    lead_id, action_id = propose_email(town)
    town.mayor.approve(action_id)
    town.killswitch.stop(town.conn)
    with pytest.raises(ExecutorError, match="kill switch"):
        town.executor.execute(action_id)
    with pytest.raises(TownStopped):                          # checked again right before the Gmail call
        town.outbox.save_draft(town.conn, action_id, email_content(lead_id))
    assert town.fake.calls == 0 and town.fake.recorded() == []


@pytest.mark.parametrize("to, subject", [
    ("owner@example.com", "A preview\r\nBcc: everyone@example.com"),
    ("owner@example.com", "A preview\nfor you"),
    ("owner@example.com\nBcc: everyone@example.com", "A preview"),
    ("owner@example.com, other@example.com", "A preview"),
    ("Owner <owner@example.com>", "A preview"),
    ("not-an-address", "A preview"),
])
def test_a_line_break_or_anything_but_one_plain_address_is_refused(to, subject):
    with pytest.raises(GmailError):
        build_message(to, subject, "Hi there,")


def test_a_failed_draft_leaves_the_action_failed_never_executed(town):
    _, action_id = propose_email(town)
    edited = {**json.loads(town.mayor.get(action_id)["content"]), "subject": "Hi\r\nBcc: everyone@example.com"}
    town.mayor.edit_and_approve(action_id, edited)
    outcome = town.executor.execute(action_id)
    assert outcome.status == "failed" and "line break" in outcome.result
    assert status(town, action_id) == "failed" and town.fake.recorded() == []
    assert town.conn.execute("SELECT COUNT(*) FROM gmail_drafts").fetchone()[0] == 0


def test_a_sign_in_problem_fails_the_action_once_with_no_retry(town):
    town.fake.fail = GmailSignInNeeded(SIGN_IN_AGAIN)
    _, action_id = propose_email(town)
    town.mayor.approve(action_id)
    outcome = town.executor.execute(action_id)
    assert outcome.status == "failed" and SIGN_IN_AGAIN in outcome.result
    assert town.fake.calls == 1 and status(town, action_id) == "failed"


# ---------------- the real client: one door, an allowlist, no secrets ----------------

class FakeCreds:
    def __init__(self, scopes=(COMPOSE_SCOPE,), refresh_token=REFRESH_TOKEN):
        self.granted_scopes, self.refresh_token = list(scopes), refresh_token
        self.token, self.valid = ACCESS_TOKEN, True

    def to_json(self, strip=None):
        info = {"token": self.token, "refresh_token": self.refresh_token, "client_id": "client-id",
                "client_secret": CLIENT_SECRET, "scopes": [COMPOSE_SCOPE]}
        return json.dumps({k: v for k, v in info.items() if k not in (strip or [])})


class FakeResponse:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.content = json.dumps(body).encode()

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0) if self.responses else FakeResponse(200, {})


def env_file(tmp_path):
    path = tmp_path / ".env"
    path.write_text(f"GMAIL_CLIENT_ID=client-id\nGMAIL_CLIENT_SECRET={CLIENT_SECRET}\n", encoding="utf-8")
    return path


def real_client(tmp_path, session, sleeps=None):
    client = RealGmail(env_file(tmp_path), tmp_path / "secrets" / "gmail_token.json",
                       session_factory=lambda creds: session, sleep=(sleeps if sleeps is not None else []).append,
                       credentials=FakeCreds())
    client.allow_in_tests = True
    return client


@pytest.mark.parametrize("method, path", [
    ("POST", f"/drafts{SEND}"), ("POST", f"/messages{SEND}"), ("POST", "/messages"), *[("POST", w) for w in DENIED_PATHS],
    ("GET", "/threads"), ("DELETE", "/drafts"), ("PUT", "/drafts"), ("GET", "/settings"), ("POST", "/profile"),
    ("GET", "/drafts/123"), *[(m, f"/{kind}/abc{word}") for word in DENIED_PATHS for kind in ("messages", "threads")
                              for m in ("GET", "POST")],
    ("DELETE", "/messages/abc"), ("POST", f"/messages/{BATCH}"), ("GET", "/messages/abc/attachments/x"), ("GET", "/threads/abc/../../settings"), ("POST", "/threads/abc"),
    ("PUT", "/drafts/abc"), ("DELETE", "/drafts/abc"),
])
def test_the_client_refuses_every_endpoint_not_on_the_allowlist(tmp_path, method, path):
    session = FakeSession()
    with pytest.raises(EndpointRefused):
        real_client(tmp_path, session)._request(method, path)
    assert session.calls == []


def test_the_three_allowed_calls_go_to_gmail_only(tmp_path, no_network):
    session = FakeSession(FakeResponse(200, {"emailAddress": "town@example.com"}),
                          FakeResponse(200, {"id": "r-1", "message": {"id": "m-1", "threadId": "t-1"}}),
                          FakeResponse(200, {"drafts": [{"id": "r-1"}], "resultSizeEstimate": 1}))
    client = real_client(tmp_path, session)
    assert client.profile_email() == "town@example.com"
    saved = client.create_draft(build_message("owner@example.com", "Hi", "Hi there,"))
    assert (saved.draft_id, saved.message_id, saved.thread_id) == ("r-1", "m-1", "t-1")
    assert client.count_drafts() == 1
    assert [(m, u) for m, u, _ in session.calls] == [("GET", f"{gmail.API}/profile"), ("POST", f"{gmail.API}/drafts"),
                                                    ("GET", f"{gmail.API}/drafts")]
    assert no_network == []


def test_a_429_or_5xx_gets_one_retry_after_a_short_wait_then_fails(tmp_path):
    sleeps = []
    ok = real_client(tmp_path, FakeSession(FakeResponse(503, {}), FakeResponse(200, {"emailAddress": "a@b.com"})),
                     sleeps)
    assert ok.profile_email() == "a@b.com" and sleeps == [gmail.RETRY_WAIT_S]
    session = FakeSession(FakeResponse(429, {}), FakeResponse(429, {}), FakeResponse(200, {}))
    with pytest.raises(GmailError, match="HTTP 429"):
        real_client(tmp_path, session).profile_email()
    assert len(session.calls) == 2


def test_creating_a_draft_is_retried_only_on_429_never_after_a_5xx(tmp_path):
    session = FakeSession(FakeResponse(500, {}), FakeResponse(200, {"id": "r-2", "message": {}}))
    with pytest.raises(GmailError, match="HTTP 500"):
        real_client(tmp_path, session).create_draft(build_message("owner@example.com", "Hi", "Hi there,"))
    assert len(session.calls) == 1                           # the draft may exist already: no second one
    session = FakeSession(FakeResponse(429, {}), FakeResponse(200, {"id": "r-3", "message": {}}))
    assert real_client(tmp_path, session).create_draft(build_message("o@example.com", "Hi", "Hi")).draft_id == "r-3"


def test_the_real_session_never_refreshes_and_resends_on_its_own(tmp_path):
    client = RealGmail(env_file(tmp_path), tmp_path / "t.json", credentials=FakeCreds())
    session = client._session(FakeCreds())
    assert session._refresh_status_codes == () and session._max_refresh_attempts == 0


def saved_token(tmp_path):
    token_file = tmp_path / "secrets" / "gmail_token.json"
    token_file.parent.mkdir()
    token_file.write_text(json.dumps({"refresh_token": REFRESH_TOKEN, "token_uri": gmail.TOKEN_URI}), encoding="utf-8")
    client = RealGmail(env_file(tmp_path), token_file, session_factory=lambda c: FakeSession())
    client.allow_in_tests = True
    return client, token_file


@pytest.mark.parametrize("granted, ok", [(f"{COMPOSE_SCOPE} {READONLY_SCOPE}", True),
                                         (COMPOSE_SCOPE, False),            # the old token: sign in again
                                         (f"{COMPOSE_SCOPE} {READONLY_SCOPE} https://mail.google.com/", False),
                                         (None, False)])
def test_gmail_check_reads_the_granted_scope_from_a_fresh_refresh_of_the_saved_token(tmp_path, monkeypatch,
                                                                                   granted, ok):
    from google.oauth2.credentials import Credentials

    def refresh(self, request):                             # what Google's refresh answer sets
        self.token, self._granted_scopes = ACCESS_TOKEN, granted.split() if granted else None
    monkeypatch.setattr(Credentials, "refresh", refresh)
    client, token_file = saved_token(tmp_path)
    lines = gmail.check(client)
    assert lines[1] == (ok, lines[1][1]) and "gmail.compose" in lines[1][1]
    assert CLIENT_SECRET not in token_file.read_text(encoding="utf-8")


def test_an_expired_or_revoked_token_fails_cleanly_with_no_retry_and_no_secret(tmp_path, monkeypatch):
    session = FakeSession(FakeResponse(401, {"error": {"message": f"invalid_grant for {ACCESS_TOKEN}"}}))
    with pytest.raises(GmailSignInNeeded) as err:
        real_client(tmp_path, session).profile_email()
    assert str(err.value) == SIGN_IN_AGAIN and len(session.calls) == 1
    # A stored token that can't refresh (Google says invalid_grant): the same message, no retry, no secret.
    from google.auth.exceptions import RefreshError
    from google.oauth2.credentials import Credentials
    refreshes = []

    def refuse(self, request):
        refreshes.append(1)
        raise RefreshError(f"invalid_grant: Token has been expired or revoked ({REFRESH_TOKEN}, {CLIENT_SECRET})")

    monkeypatch.setattr(Credentials, "refresh", refuse)
    token_file = tmp_path / "secrets" / "gmail_token.json"
    token_file.parent.mkdir()
    token_file.write_text(json.dumps({"refresh_token": REFRESH_TOKEN, "token_uri": gmail.TOKEN_URI}), encoding="utf-8")
    client = RealGmail(env_file(tmp_path), token_file, session_factory=lambda c: FakeSession())
    client.allow_in_tests = True
    with pytest.raises(GmailSignInNeeded) as err:
        client.profile_email()
    assert str(err.value) == SIGN_IN_AGAIN and refreshes == [1]
    for secret in (REFRESH_TOKEN, CLIENT_SECRET, ACCESS_TOKEN):
        assert secret not in str(err.value)


def test_no_error_from_gmail_carries_a_secret(tmp_path):
    body = {"error": {"message": f"bad request with {ACCESS_TOKEN} and {CLIENT_SECRET} and {REFRESH_TOKEN}"}}
    with pytest.raises(GmailError) as err:
        real_client(tmp_path, FakeSession(FakeResponse(400, body))).profile_email()
    assert "[hidden]" in str(err.value)
    for secret in (ACCESS_TOKEN, CLIENT_SECRET, REFRESH_TOKEN):
        assert secret not in str(err.value)


def test_the_real_client_refuses_to_do_anything_under_pytest(tmp_path):
    client = RealGmail(env_file(tmp_path), tmp_path / "token.json")
    for call in (client.profile_email, client.count_drafts,
                 lambda: client.create_draft(build_message("a@example.com", "Hi", "Hi"))):
        with pytest.raises(GmailError, match="never used in tests"):
            call()


# ---------------- signing in: the right account and only gmail.compose, or nothing is kept ----------------

def fake_flow(creds):
    def flow(config, scopes):
        assert scopes == list(BOTH) and config["installed"]["client_secret"] == CLIENT_SECRET
        return creds
    flow.allow_in_tests = True
    return flow


def profile(email):
    return lambda creds: FakeSession(FakeResponse(200, {"emailAddress": email}))


def test_signing_in_as_the_sending_account_with_compose_and_readonly_saves_the_token_without_the_secret(tmp_path):
    token_file = tmp_path / "secrets" / "gmail_token.json"
    lines = gmail.connect(env_file(tmp_path), token_file, fake_flow(FakeCreds(BOTH)), profile("town@example.com"))
    assert all(ok for ok, _ in lines), lines
    saved = json.loads(token_file.read_text(encoding="utf-8"))
    assert saved["refresh_token"] == REFRESH_TOKEN and "client_secret" not in saved
    assert CLIENT_SECRET not in token_file.read_text(encoding="utf-8")


def test_the_wrong_account_deletes_the_token_and_refuses(tmp_path):
    token_file = tmp_path / "secrets" / "gmail_token.json"
    token_file.parent.mkdir()
    token_file.write_text("{}", encoding="utf-8")            # an older token is removed too
    lines = gmail.connect(env_file(tmp_path), token_file, fake_flow(FakeCreds(BOTH)), profile("operator@example.com"))
    assert not lines[-1][0] and "signed in as the wrong account" in lines[-1][1].lower()
    assert not token_file.exists()


@pytest.mark.parametrize("scopes", [(COMPOSE_SCOPE,), (READONLY_SCOPE,), (*BOTH, "https://mail.google.com/"),
                                    (*BOTH, "https://www.googleapis.com/auth/gmail.modify"),
                                    ("https://mail.google.com/",), ()])
def test_extra_or_other_scopes_delete_the_token_and_refuse(tmp_path, scopes):
    token_file = tmp_path / "secrets" / "gmail_token.json"
    token_file.parent.mkdir()
    token_file.write_text("{}", encoding="utf-8")
    lines = gmail.connect(env_file(tmp_path), token_file, fake_flow(FakeCreds(scopes)),
                          profile("town@example.com"))
    assert not all(ok for ok, _ in lines) and "gmail.compose" in lines[-1][1]
    assert not token_file.exists()


def test_a_failed_sign_in_never_shows_a_secret(tmp_path):
    def flow(config, scopes):
        raise RuntimeError(f"access_denied (client {CLIENT_SECRET})")
    lines = gmail.connect(env_file(tmp_path), tmp_path / "t.json", flow)
    assert not lines[0][0] and CLIENT_SECRET not in lines[0][1]


# ---------------- real vs fake, and who may run what ----------------

def test_dev_other_instances_and_claude_code_always_get_fake_gmail(dev_instance, instance_file, tmp_path):
    dev, real = load_settings(dev_instance), load_settings(instance_file)
    assert isinstance(launcher._gmail(dev, dev_instance, "charlie"), FakeGmail)
    assert isinstance(launcher._gmail(real, instance_file, "claude-code"), FakeGmail)
    other = tmp_path / "other.yaml"
    other.write_text(instance_file.read_text(encoding="utf-8"), encoding="utf-8")   # says real, isn't instance.yaml
    assert isinstance(launcher._gmail(load_settings(other), other, "charlie"), FakeGmail)
    assert isinstance(launcher._gmail(real, instance_file, "charlie"), RealGmail)    # made, never used here


def test_claude_code_is_refused_gmail_connect_and_gmail_check_on_the_real_town(dev_instance, monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    for command in (["gmail-connect"], ["gmail-check"], ["gmail-check", "--test-draft"]):
        result = runner.invoke(launcher.app, command)
        assert result.exit_code == 1 and "Charlie's call" in result.stdout, command


def test_gmail_connect_is_refused_on_dev_and_gmail_check_reports_the_fake(dev_instance, no_network):
    town = lambda *args: runner.invoke(launcher.app, ["--instance", "dev", *args])
    result = town("gmail-connect")
    assert result.exit_code == 1 and "only for the real Town" in result.stdout
    result = town("gmail-check")
    assert result.exit_code == 0 and "FAKE Gmail" in result.stdout
    result = town("gmail-check", "--test-draft")                 # a new Town starts stopped: no test draft
    assert result.exit_code == 1 and "kill switch" in result.stdout
    town("resume")
    result = town("gmail-check", "--test-draft")
    assert result.exit_code == 0 and "Test draft saved" in result.stdout
    [draft] = FakeGmail(load_settings(dev_instance).state_folder / "gmail-fake").recorded()
    assert draft["headers"]["To"] == "town@example.com"
    assert draft["headers"]["Subject"] == "Example Web Studio: Town connection test"
    assert no_network == []


# ---------------- nothing in the codebase can send, and the token stays out of git ----------------

def test_no_message_send_insert_import_modify_or_trash_call_is_named_anywhere_but_the_deny_list():
    files = json.loads((REPO / "SHOWCASE-REPORT.json").read_text(encoding="utf-8"))["output_files"]
    assert "packs/websites/gmail.py" in files
    found = []
    for name in files:
        path = REPO / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue                                         # images and fonts
        if name == "packs/websites/gmail.py":
            deny_line = next(line for line in text.splitlines() if line.startswith("DENIED_ENDPOINTS = "))
            text = text.replace(deny_line, "")
        found += [f"{name}: {word}" for word in gmail.DENIED_ENDPOINTS if word in text]
    assert found == []


def test_the_token_file_is_gitignored_and_nothing_under_secrets_is_tracked(public_repo_settings):
    assert subprocess.run(["git", "check-ignore", "-q", "secrets/gmail_token.json"], cwd=public_repo_settings).returncode == 0
    tracked = subprocess.run(["git", "ls-files", "secrets"], cwd=public_repo_settings, capture_output=True, text=True, check=True)
    assert tracked.stdout.strip() == ""
    assert launcher.GMAIL_TOKEN_FILE == REPO / "secrets" / "gmail_token.json"


def test_the_drafts_send_path_is_named_only_on_its_own_line_and_called_only_from_outbox_send():
    """The one sending call: its path is spelled once (SEND_ENDPOINT in gmail.py), `send_draft(` is called from exactly
    one function in the Town's code (outreach.Outbox.send), and _request refuses it from anywhere else."""
    import ast
    files = json.loads((REPO / "SHOWCASE-REPORT.json").read_text(encoding="utf-8"))["output_files"]
    path_text, callers = gmail.SEND_ENDPOINT[1], []
    named = []
    for name in files:
        if not name.endswith(".py") or name.startswith("tests/"):
            continue
        text = (REPO / name).read_text(encoding="utf-8")
        named += [f"{name}:{i}" for i, line in enumerate(text.splitlines(), 1) if path_text in line]
        tree = ast.parse(text)
        for func in ast.walk(tree):
            if isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for node in ast.walk(func):
                    if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "send_draft":
                        callers.append(f"{name}:{func.name}")
    [line] = named
    assert line.startswith("packs/websites/gmail.py:") and "SEND_ENDPOINT = " in \
        (REPO / "packs/websites/gmail.py").read_text(encoding="utf-8").splitlines()[int(line.split(":")[1]) - 1]
    assert callers == ["packs/websites/outreach.py:send"]


def test_request_refuses_the_send_call_unless_it_comes_from_send_draft_and_never_retries_it(tmp_path):
    session = FakeSession()
    with pytest.raises(EndpointRefused, match="only send_draft"):
        real_client(tmp_path, session)._request(*gmail.SEND_ENDPOINT, {"id": "r1"})
    assert session.calls == []
    session = FakeSession(FakeResponse(503, {"error": {"message": "backend error"}}),
                          FakeResponse(200, {"id": "m-2", "threadId": "t-1"}))
    sleeps = []
    with pytest.raises(GmailError, match="503"):
        real_client(tmp_path, session, sleeps).send_draft("r1")
    assert len(session.calls) == 1 and sleeps == []            # one try, no retry, whatever the error
    session = FakeSession(FakeResponse(200, {"id": "m-1", "threadId": "t-1"}))
    sent = real_client(tmp_path, session).send_draft("r1")
    assert (sent.message_id, sent.thread_id) == ("m-1", "t-1")
    assert [(m, u) for m, u, _ in session.calls] == [("POST", gmail.API + gmail.SEND_ENDPOINT[1])]
    with pytest.raises(GmailError, match="letters and digits"):
        real_client(tmp_path, FakeSession()).send_draft("r1/../../messages")


def test_the_clerks_read_calls_are_allowed_and_parse_a_reply(tmp_path, no_network):
    import base64 as b64
    text = b64.urlsafe_b64encode("No thanks.\n\nOn Mon, Example Web Studio wrote:\n> Hi there".encode()).decode().rstrip("=")
    message = {"id": "m9", "threadId": "t1", "labelIds": ["INBOX"], "payload": {"mimeType": "multipart/alternative",
               "headers": [{"name": "From", "value": "Owner <Owner@Bakery.example>"}, {"name": "Subject", "value": "Re: hi"}],
               "parts": [{"mimeType": "text/plain", "body": {"data": text}}]}}
    session = FakeSession(FakeResponse(200, {"messages": [{"id": "m9", "threadId": "t1"}]}), FakeResponse(200, message),
                          FakeResponse(200, {"messages": [message]}))
    client = real_client(tmp_path, session)
    assert client.list_message_ids("from:mailer-daemon", 10) == [("m9", "t1")]
    item = client.get_message("m9")
    assert item.from_address == "owner@bakery.example" and item.text.startswith("No thanks.")
    assert [i.message_id for i in client.get_thread("t1")] == ["m9"]
    assert [u for _, u, _ in session.calls] == [f"{gmail.API}/messages", f"{gmail.API}/messages/m9", f"{gmail.API}/threads/t1"]
    with pytest.raises(GmailError, match="letters and digits"):
        client.get_thread("t1" + DENIED_PATHS[-1])
