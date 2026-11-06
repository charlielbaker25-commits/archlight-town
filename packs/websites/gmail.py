"""Talking to Gmail (town@example.com): the real client, the fake one, and the sign-in.

The Town puts an approved outreach email into the Town Gmail's Drafts folder, exactly as the prospect would
receive it. Until Charlie switches go-live on (`town golive`, packs/websites/golive.py), that is all: nothing is
sent. Once it's on, an approved email's draft is SENT with exactly one call, drafts.send, from exactly one
function (`send_draft`), which only outreach.Outbox.send calls, after every gate.

Scopes: exactly two. `gmail.compose` (create a draft, send a draft) and `gmail.readonly` (the Clerk reads
replies on the Town's own threads; it can't change, delete or send anything). The code is what keeps the Town
to the few calls it needs, because compose could technically do more:
- every request goes through one function, `_request`, that refuses any endpoint not on ALLOWED_ENDPOINTS
  (the profile, create a draft, list drafts; list messages, read one message, read one thread) and any path
  with a DENIED_ENDPOINTS word in it. The one sending call, SEND_ENDPOINT, is refused unless the caller is
  `send_draft` itself, and it's never retried;
- pytests scan the whole codebase: no message send, insert, import, modify or trash call is named anywhere
  outside DENIED_ENDPOINTS, and SEND_ENDPOINT's path appears only on its own line here.

Two clients with the same jobs:
- RealGmail calls the Gmail REST API through google-auth's AuthorizedSession. Only launcher._gmail() makes one,
  for Charlie's real instance run by Charlie, and it refuses to do anything inside pytest.
- FakeGmail never leaves this laptop: it records each draft (headers and body) in <state>/gmail-fake/drafts.json.
  The dev instance, every other instance, Claude Code and every test get it.

Secrets: GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET live in .env (Charlie adds them). The sign-in token lives in
secrets/gmail_token.json (gitignored): the one exception to "secrets in .env only", because the token changes
every time it refreshes. The token file never holds the client secret (it's stripped before saving and read
from .env when needed). No token, secret or login code is ever printed, logged or put in an error: messages
are scrubbed first.
"""
import base64
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path
from typing import Any, Callable, Protocol

from dotenv import dotenv_values

from packs.websites.guard import SENDER_NAME, SENDING_ACCOUNT

COMPOSE_SCOPE = "https://www.googleapis.com/auth/gmail.compose"
READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
# Exactly these two, nothing more and nothing less (gmail-connect keeps a token only for exactly this set).
SCOPES = (COMPOSE_SCOPE, READONLY_SCOPE)
REQUIRED_SCOPES = frozenset(SCOPES)
API = "https://gmail.googleapis.com/gmail/v1/users/me"
TOKEN_URI = "https://oauth2.googleapis.com/token"
AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
CLIENT_ID_KEY, CLIENT_SECRET_KEY = "GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET"
# The only Gmail calls the Town may make: (method, the whole path under API as a pattern). An id is letters and
# digits only, so nothing can ride along in it.
ALLOWED_ENDPOINTS = (
    ("GET", r"/profile"),                 # which account this is
    ("POST", r"/drafts"),                 # create a draft (the approved email, exactly)
    ("GET", r"/drafts"),                  # count drafts (gmail-check)
    ("GET", r"/messages"),                # list message ids (the Clerk: bounce notices), read-only scope
    ("GET", r"/messages/[A-Za-z0-9]+"),   # read one message (the Clerk), read-only scope
    ("GET", r"/threads/[A-Za-z0-9]+"),    # read one of the Town's own threads (the Clerk), read-only scope
)
# The ONE sending call (drafts.send): only `send_draft` may make it (checked in `_request`), never retried.
SEND_ENDPOINT = ("POST", "/drafts/send")
# Calls the Town must never make. This constant is the only place in the codebase allowed to name them (a
# pytest checks), and `_request` refuses any path that contains one of them.
DENIED_ENDPOINTS = ("messages.send", "/messages/send", "messages.insert", "messages.import", "/messages/import", "messages.modify", "threads.modify", "/modify", "batchModify", "messages.trash", "threads.trash", "/trash", "/untrash", "messages.delete", "threads.delete", "drafts.delete", "drafts.update")
UNSUBSCRIBE = f"<mailto:{SENDING_ACCOUNT}?subject=unsubscribe>"
FROM_HEADER = f"{SENDER_NAME} <{SENDING_ACCOUNT}>"
TEST_SUBJECT = "Example Web Studio: Town connection test"
SIGN_IN_AGAIN = "Gmail needs you to sign in again: run town gmail-connect"
HTTP_TIMEOUT_S = 30
RETRY_WAIT_S = 3.0
RETRY_STATUSES = (429, 500, 502, 503, 504)
MAX_DRAFT_PAGES = 20
ADDRESS = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+")


class GmailError(RuntimeError):
    """Gmail refused or failed. Its message never contains a token, the client secret or a login code."""


class GmailSignInNeeded(GmailError):
    """The token is missing, expired or revoked. Never retried: Charlie runs `town gmail-connect`."""


class EndpointRefused(GmailError):
    """Code asked for a Gmail call that isn't on the allowlist. Nothing was sent to Google."""


@dataclass(frozen=True)
class DraftSaved:
    draft_id: str
    message_id: str
    thread_id: str


@dataclass(frozen=True)
class DraftSent:
    message_id: str
    thread_id: str


@dataclass(frozen=True)
class MailItem:
    """One message read from Gmail (the Clerk). Everything in it is DATA from outside the Town."""
    message_id: str
    thread_id: str
    from_address: str
    to: str
    subject: str
    date: str
    text: str
    labels: tuple[str, ...] = ()
    auto_submitted: str = ""        # the Auto-Submitted header ("auto-replied" = an out-of-office)


class GmailClient(Protocol):
    real: bool

    def profile_email(self) -> str: ...
    def create_draft(self, raw: bytes) -> DraftSaved: ...
    def count_drafts(self) -> int: ...
    def send_draft(self, draft_id: str) -> DraftSent: ...
    def list_message_ids(self, query: str, limit: int) -> list[tuple[str, str]]: ...
    def get_message(self, message_id: str) -> MailItem: ...
    def get_thread(self, thread_id: str) -> list[MailItem]: ...


def endpoint_allowed(method: str, path: str) -> bool:
    """On the allowlist (the whole path matches one pattern) and no denied word in it. Not the send call."""
    if any(word in path for word in DENIED_ENDPOINTS):
        return False
    return any(method == m and re.fullmatch(pattern, path) for m, pattern in ALLOWED_ENDPOINTS)


def redact(text: str, secrets: list[str]) -> str:
    """Every secret replaced with [hidden] (the same idea as the Cloudflare client's)."""
    for secret in sorted((s for s in secrets if s and len(s) >= 6), key=len, reverse=True):
        text = text.replace(secret, "[hidden]")
    return text


# ---------------- the message itself ----------------

def single_address(text: str) -> str:
    """One plain, valid email address (no name, no list, no line break), lowercased. Raises GmailError."""
    text = str(text or "")
    if "\r" in text or "\n" in text:
        raise GmailError("the To address contains a line break (refused: header injection)")
    name, address = parseaddr(text)
    if name or "," in text or ";" in text or not ADDRESS.fullmatch(address or "") or address != text.strip():
        raise GmailError(f"the To address must be one plain email address, not '{text}'")
    return address.lower()


def build_message(to: str, subject: str, body: str) -> bytes:
    """The draft exactly as the prospect would receive it: plain text, UTF-8, from Example Web Studio, with a
    List-Unsubscribe header that matches the opt-out line. Refuses a line break in the subject or To."""
    subject = str(subject or "")
    if "\r" in subject or "\n" in subject:
        raise GmailError("the subject contains a line break (refused: header injection)")
    if not subject.strip():
        raise GmailError("the subject is empty")
    message = EmailMessage(policy=policy.SMTP)
    message["From"] = FROM_HEADER
    message["To"] = single_address(to)
    message["Subject"] = subject
    message["List-Unsubscribe"] = UNSUBSCRIBE
    # quoted-printable keeps every byte 7-bit on the wire (safe everywhere) while staying readable.
    message.set_content(str(body or ""), subtype="plain", charset="utf-8", cte="quoted-printable")
    return message.as_bytes()


def read_message(raw: bytes) -> dict[str, Any]:
    """Headers and body of a built message (the fake records this, so a test or Charlie can read it)."""
    parsed = message_from_bytes(raw, policy=policy.default)
    # On the wire, email lines end in CRLF (the standard); shown here as plain lines, the way it reads.
    return {"headers": {k: str(v) for k, v in parsed.items()},
            "body": parsed.get_content().replace("\r\n", "\n")}


# ---------------- the fake (dev instance, Claude Code and tests) ----------------

class FakeGmail:
    """Never leaves this laptop. Records each draft (its headers and body) in `folder`/drafts.json."""
    real = False

    def __init__(self, folder: Path, account: str = SENDING_ACCOUNT, fail: GmailError | None = None) -> None:
        self.folder = Path(folder)
        self.account = account
        self.fail = fail
        self.calls = 0

    @property
    def _file(self) -> Path:
        return self.folder / "drafts.json"

    def recorded(self) -> list[dict[str, Any]]:
        return json.loads(self._file.read_text(encoding="utf-8")) if self._file.is_file() else []

    def profile_email(self) -> str:
        return self.account

    def create_draft(self, raw: bytes) -> DraftSaved:
        self.calls += 1
        if self.fail:
            raise self.fail
        drafts = self.recorded()
        number = len(drafts) + 1
        saved = DraftSaved(f"fake-draft-{number}", f"fake-message-{number}", f"fake-thread-{number}")
        drafts.append({**saved.__dict__, **read_message(raw),
                       "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        self.folder.mkdir(parents=True, exist_ok=True)
        self._file.write_text(json.dumps(drafts, indent=2, ensure_ascii=False), encoding="utf-8")
        return saved

    def count_drafts(self) -> int:
        return len(self.recorded())

    # Go-live: the fake "sends" by recording it in sent.json. It never leaves this laptop.
    def sent(self) -> list[dict[str, Any]]:
        path = self.folder / "sent.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []

    def send_draft(self, draft_id: str) -> DraftSent:
        self.calls += 1
        if self.fail:
            raise self.fail
        draft = next((d for d in self.recorded() if d["draft_id"] == draft_id), None)
        if draft is None:
            raise GmailError(f"no draft {draft_id}")
        sent = self.sent()
        if any(s["draft_id"] == draft_id for s in sent):
            raise GmailError(f"draft {draft_id} was already sent")
        result = DraftSent(f"fake-sent-{len(sent) + 1}", draft["thread_id"])
        sent.append({"draft_id": draft_id, **result.__dict__, "headers": draft["headers"], "body": draft["body"],
                     "sent_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        self.folder.mkdir(parents=True, exist_ok=True)
        (self.folder / "sent.json").write_text(json.dumps(sent, indent=2, ensure_ascii=False), encoding="utf-8")
        return result

    # The Clerk's reads: incoming mail is whatever a test (or `add_incoming` on dev) put in inbox.json.
    def inbox(self) -> list[dict[str, Any]]:
        path = self.folder / "inbox.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []

    def add_incoming(self, thread_id: str, from_address: str, subject: str, text: str,
                     auto_submitted: str = "") -> str:
        """Puts one incoming message in the fake inbox (tests and dev only). Returns its id."""
        items = self.inbox()
        message_id = f"fake-in-{len(items) + 1}"
        items.append({"message_id": message_id, "thread_id": thread_id, "from_address": from_address,
                      "to": self.account, "subject": subject, "date": datetime.now(timezone.utc).isoformat(),
                      "text": text, "labels": ["INBOX"], "auto_submitted": auto_submitted})
        self.folder.mkdir(parents=True, exist_ok=True)
        (self.folder / "inbox.json").write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")
        return message_id

    def list_message_ids(self, query: str, limit: int) -> list[tuple[str, str]]:
        self.calls += 1
        bounce_only = "mailer-daemon" in query
        found = [(m["message_id"], m["thread_id"]) for m in self.inbox()
                 if not bounce_only or is_bounce_sender(m["from_address"])]
        return found[:limit]

    def get_message(self, message_id: str) -> MailItem:
        self.calls += 1
        item = next((m for m in self.inbox() if m["message_id"] == message_id), None)
        if item is None:
            raise GmailError(f"no message {message_id}")
        return MailItem(**{**item, "labels": tuple(item.get("labels") or ())})

    def get_thread(self, thread_id: str) -> list[MailItem]:
        self.calls += 1
        return [MailItem(**{**m, "labels": tuple(m.get("labels") or ())}) for m in self.inbox()
                if m["thread_id"] == thread_id]


BOUNCE_SENDERS = ("mailer-daemon", "postmaster")


def is_bounce_sender(address: str) -> bool:
    return any(word in str(address or "").lower() for word in BOUNCE_SENDERS)


# ---------------- reading a real message ----------------

def _header(headers: list[dict[str, Any]], name: str) -> str:
    return next((str(h.get("value") or "") for h in headers if str(h.get("name", "")).lower() == name.lower()), "")


def _plain_text(payload: dict[str, Any]) -> str:
    """The text/plain body of a Gmail message payload (its parts searched in order)."""
    if str(payload.get("mimeType", "")).startswith("text/plain") and (payload.get("body") or {}).get("data"):
        data = payload["body"]["data"]
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")
    for part in payload.get("parts") or []:
        found = _plain_text(part)
        if found:
            return found
    return ""


def mail_item(message: dict[str, Any]) -> MailItem:
    payload = message.get("payload") or {}
    headers = payload.get("headers") or []
    return MailItem(str(message.get("id") or ""), str(message.get("threadId") or ""),
                    parseaddr(_header(headers, "From"))[1].lower(), _header(headers, "To"), _header(headers, "Subject"),
                    _header(headers, "Date"), _plain_text(payload) or str(message.get("snippet") or ""),
                    tuple(message.get("labelIds") or ()), _header(headers, "Auto-Submitted").lower())


# ---------------- the real one (Charlie's real instance only) ----------------

def _client_config(values: dict[str, Any]) -> dict[str, str]:
    client_id = (values.get(CLIENT_ID_KEY) or "").strip()
    client_secret = (values.get(CLIENT_SECRET_KEY) or "").strip()
    if not client_id or not client_secret:
        raise GmailError(f"{CLIENT_ID_KEY} and {CLIENT_SECRET_KEY} must both be set in .env")
    return {"client_id": client_id, "client_secret": client_secret}


def save_token(token_file: Path, credentials: Any) -> None:
    """Writes the token WITHOUT the client secret (that stays in .env). A temporary file first, so a crash
    never leaves half a token."""
    info = json.loads(credentials.to_json(strip=["client_secret"]))
    token_file.parent.mkdir(parents=True, exist_ok=True)
    temporary = token_file.with_suffix(".tmp")
    temporary.write_text(json.dumps(info), encoding="utf-8")
    os.replace(temporary, token_file)


def delete_token(token_file: Path) -> None:
    for path in (token_file, token_file.with_suffix(".tmp")):
        if path.exists():
            path.unlink()


def scopes_of(credentials: Any) -> set[str]:
    granted = credentials.granted_scopes or []
    return set(granted.split() if isinstance(granted, str) else granted)


class RealGmail:
    """The Gmail REST API for real. Made only by launcher._gmail() for Charlie's real instance (and by
    gmail-connect with the brand-new sign-in). Inside pytest it refuses to do anything."""
    real = True

    def __init__(self, env_file: Path, token_file: Path, session_factory: Callable[[Any], Any] | None = None,
                 sleep: Callable[[float], None] = time.sleep, credentials: Any = None) -> None:
        self.env_file = Path(env_file)
        self.token_file = Path(token_file)
        self.session_factory = session_factory
        self.sleep = sleep
        self._credentials = credentials      # set only by gmail-connect, before the token is saved
        self._saved_token = None

    def _refuse_in_tests(self) -> None:
        if "PYTEST_CURRENT_TEST" in os.environ and not getattr(self, "allow_in_tests", False):
            raise GmailError("the real Gmail client is never used in tests")

    def _secrets(self, creds: Any = None) -> list[str]:
        found = [str(dotenv_values(self.env_file).get(CLIENT_SECRET_KEY) or "")]
        if creds is not None:
            found += [str(creds.token or ""), str(creds.refresh_token or "")]
        return found

    def credentials(self, force_refresh: bool = False) -> Any:
        """The token from secrets/, refreshed if it expired (or always, with force_refresh: Google's refresh
        answer is what tells us the scopes it really granted). A missing, expired or revoked token raises
        GmailSignInNeeded (never retried)."""
        self._refuse_in_tests()
        if self._credentials is not None and not force_refresh:
            return self._credentials
        from google.auth.exceptions import RefreshError, TransportError
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

        config = _client_config(dotenv_values(self.env_file))
        if not self.token_file.is_file():
            raise GmailSignInNeeded(SIGN_IN_AGAIN)
        try:
            info = {**json.loads(self.token_file.read_text(encoding="utf-8")), **config}
            creds = Credentials.from_authorized_user_info(info, scopes=list(SCOPES))
        except (ValueError, KeyError):
            raise GmailSignInNeeded(SIGN_IN_AGAIN) from None
        if force_refresh or not creds.valid:
            try:
                creds.refresh(Request())
            except RefreshError:             # expired or revoked (invalid_grant): Charlie signs in again
                raise GmailSignInNeeded(SIGN_IN_AGAIN) from None
            except TransportError as err:
                raise GmailError(redact(f"couldn't reach Google to refresh the token: {err}",
                                        self._secrets(creds))) from None
            save_token(self.token_file, creds)
        self._credentials, self._saved_token = creds, creds.token
        return creds

    def granted_scopes(self) -> set[str]:
        """What Google says this token may do, read from a fresh refresh (the saved token doesn't carry it).
        If Google doesn't say, this is empty, and `gmail-check` fails (fail closed)."""
        return scopes_of(self.credentials(force_refresh=True))

    def _session(self, creds: Any) -> Any:
        if self.session_factory:
            return self.session_factory(creds)
        from google.auth.transport.requests import AuthorizedSession
        # No automatic "refresh and send again" on a 401: a sign-in problem is never retried (we refresh the
        # token ourselves in credentials(), before any request).
        return AuthorizedSession(creds, refresh_status_codes=(), max_refresh_attempts=0)

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None,
                 params: dict[str, Any] | None = None) -> dict[str, Any]:
        """THE ONE DOOR to Gmail. Refuses anything not on the allowlist before a byte goes out. The send call is
        refused unless the caller is `send_draft` (the frame just above this one), and never retried. Any other
        429 or 5xx gets one retry after a short wait; a sign-in problem is never retried."""
        sending = (method, path) == SEND_ENDPOINT
        if sending and sys._getframe(1).f_code.co_name != "send_draft":
            raise EndpointRefused("only send_draft may send a draft. Nothing was sent to Google.")
        if not sending and not endpoint_allowed(method, path):
            raise EndpointRefused(f"the Town may not call Gmail {method} {path} (only: the profile, create or list "
                                  "drafts, read messages and threads, and send an approved draft). Nothing was sent "
                                  "to Google.")
        creds = self.credentials()
        from google.auth.exceptions import RefreshError, TransportError
        session = self._session(creds)
        for attempt in (1, 2):
            try:
                response = session.request(method, API + path, json=payload, params=params, timeout=HTTP_TIMEOUT_S)
            except RefreshError:
                raise GmailSignInNeeded(SIGN_IN_AGAIN) from None
            except (TransportError, OSError) as err:
                raise GmailError(redact(f"couldn't reach Gmail: {type(err).__name__}: {err}",
                                        self._secrets(creds))) from None
            status = int(response.status_code)
            if status < 300:
                self._keep_refreshed_token(creds)
                return response.json() if response.content else {}
            # One retry after a short wait. Creating a draft is retried only on 429 (Google didn't process it):
            # after a 5xx the draft may already exist, and a retry could make a second one. Sending is NEVER
            # retried, whatever the error: a second try could send the email twice.
            retryable = () if sending else (429,) if method == "POST" else RETRY_STATUSES
            if status in retryable and attempt == 1:
                self.sleep(RETRY_WAIT_S)
                continue
            raise self._error(method, path, status, response, creds)
        raise GmailError(f"Gmail {method} {path} failed")   # not reached: the second attempt returns or raises

    def _keep_refreshed_token(self, creds: Any) -> None:
        """The session refreshes an expired token by itself; save the new one (google-auth rewrites it)."""
        if self._saved_token is not None and creds.token != self._saved_token:
            save_token(self.token_file, creds)
            self._saved_token = creds.token

    def _error(self, method: str, path: str, status: int, response: Any, creds: Any) -> GmailError:
        try:
            detail = str((response.json().get("error") or {}).get("message") or "")
        except (ValueError, AttributeError):
            detail = ""
        if status == 401 or "invalid_grant" in detail:
            return GmailSignInNeeded(SIGN_IN_AGAIN)
        return GmailError(redact(f"Gmail {method} {path} failed (HTTP {status}){f': {detail}' if detail else ''}",
                                 self._secrets(creds)))

    def profile_email(self) -> str:
        return str(self._request("GET", "/profile").get("emailAddress") or "").lower()

    def create_draft(self, raw: bytes) -> DraftSaved:
        body = self._request("POST", "/drafts", {"message": {"raw": base64.urlsafe_b64encode(raw).decode()}})
        message = body.get("message") or {}
        if not body.get("id"):
            raise GmailError("Gmail answered without a draft id")
        return DraftSaved(str(body["id"]), str(message.get("id") or ""), str(message.get("threadId") or ""))

    def send_draft(self, draft_id: str) -> DraftSent:
        """THE ONE SENDING CALL (drafts.send). Only outreach.Outbox.send calls this, after every gate."""
        if not re.fullmatch(r"[A-Za-z0-9_-]+", str(draft_id or "")):
            raise GmailError("a draft id is letters and digits only")
        body = self._request(*SEND_ENDPOINT, {"id": draft_id})
        if not body.get("id"):
            raise GmailError("Gmail answered the send without a message id")
        return DraftSent(str(body["id"]), str(body.get("threadId") or ""))

    def list_message_ids(self, query: str, limit: int) -> list[tuple[str, str]]:
        body = self._request("GET", "/messages", params={"q": query, "maxResults": max(1, min(int(limit), 100))})
        return [(str(m.get("id")), str(m.get("threadId") or "")) for m in body.get("messages") or [] if m.get("id")]

    def get_message(self, message_id: str) -> MailItem:
        if not re.fullmatch(r"[A-Za-z0-9]+", str(message_id or "")):
            raise GmailError("a message id is letters and digits only")
        return mail_item(self._request("GET", f"/messages/{message_id}", params={"format": "full"}))

    def get_thread(self, thread_id: str) -> list[MailItem]:
        if not re.fullmatch(r"[A-Za-z0-9]+", str(thread_id or "")):
            raise GmailError("a thread id is letters and digits only")
        body = self._request("GET", f"/threads/{thread_id}", params={"format": "full"})
        return [mail_item(m) for m in body.get("messages") or []]

    def count_drafts(self) -> int:
        count, page_token = 0, None
        for _ in range(MAX_DRAFT_PAGES):
            params = {"maxResults": 500, **({"pageToken": page_token} if page_token else {})}
            body = self._request("GET", "/drafts", params=params)
            count += len(body.get("drafts") or [])
            page_token = body.get("nextPageToken")
            if not page_token:
                break
        return count


# ---------------- signing in (`town gmail-connect`, Charlie only) ----------------

FlowRunner = Callable[[dict[str, Any], list[str]], Any]


def run_installed_flow(config: dict[str, Any], scopes: list[str]) -> Any:
    """Google's installed-app sign-in: a one-time server on 127.0.0.1 and Charlie's own browser. Charlie signs
    in himself; the Town never sees his password or any login code."""
    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_config(config, scopes)
    return flow.run_local_server(
        host="127.0.0.1", port=0, open_browser=True, timeout_seconds=600, access_type="offline",
        prompt="consent", login_hint=SENDING_ACCOUNT,
        authorization_prompt_message=f"Your browser should open Google's sign-in page. Sign in as {SENDING_ACCOUNT}. "
                                     "(If it didn't open, visit this address: {url})",
        success_message="Signed in. You can close this tab and go back to the terminal.")


def connect(env_file: Path, token_file: Path, flow: FlowRunner = run_installed_flow,
            session_factory: Callable[[Any], Any] | None = None) -> list[tuple[bool, str]]:
    """Runs the sign-in, then checks it in plain code. Keeps the token only if Google granted exactly
    gmail.compose + gmail.readonly AND the account is exactly the sending account; otherwise the token is deleted
    and the sign-in refused. Returns (passed, plain-words line) for each check."""
    config = _client_config(dotenv_values(env_file))
    installed = {"installed": {**config, "auth_uri": AUTH_URI, "token_uri": TOKEN_URI,
                               "redirect_uris": ["http://127.0.0.1"]}}
    secrets = [config["client_secret"]]
    try:
        creds = flow(installed, list(SCOPES))
    except Exception as err:     # a cancelled sign-in, a closed browser, Google saying no, a scope mismatch
        delete_token(token_file)
        return [(False, redact(f"The sign-in didn't finish ({type(err).__name__}: {err}). Nothing was saved.", secrets))]
    secrets += [str(creds.token or ""), str(creds.refresh_token or "")]
    scopes = scopes_of(creds)
    if scopes != REQUIRED_SCOPES:
        delete_token(token_file)
        return [(False, f"Google granted {', '.join(sorted(scopes)) or 'nothing it reported'}, not exactly "
                        "gmail.compose + gmail.readonly. The token was deleted. Tick both boxes on Google's page (or "
                        "remove the Town's access at myaccount.google.com/permissions) and sign in again.")]
    lines = [(True, "Google granted exactly two scopes: gmail.compose (drafts, and sending an approved one once "
                    "go-live is on) and gmail.readonly (the Clerk reads replies; it can't change or delete anything).")]
    if not creds.refresh_token:
        delete_token(token_file)
        return lines + [(False, "Google gave no refresh token, so the sign-in wouldn't last. The token was deleted. "
                                "Run town gmail-connect again.")]
    client = RealGmail(env_file, token_file, session_factory=session_factory, credentials=creds)
    client.allow_in_tests = getattr(flow, "allow_in_tests", False)
    try:
        account = client.profile_email()
    except GmailError as err:
        delete_token(token_file)
        return lines + [(False, redact(f"Couldn't read which account signed in ({err}). The token was deleted.",
                                       secrets))]
    if account != SENDING_ACCOUNT:
        delete_token(token_file)
        return lines + [(False, f"Signed in as the wrong account ({account or 'unknown'}): it must be "
                                f"{SENDING_ACCOUNT}. The token was deleted.")]
    save_token(token_file, creds)
    return lines + [(True, f"Signed in as {SENDING_ACCOUNT}."),
                    (True, "Token saved in secrets/gmail_token.json (gitignored; it never leaves this laptop).")]


def check(client: GmailClient, test_draft: bool = False) -> list[tuple[bool, str]]:
    """`town gmail-check`: read-only, except --test-draft (one draft addressed to the Town's own Gmail)."""
    lines: list[tuple[bool, str]] = []
    try:
        if isinstance(client, RealGmail):
            scopes = client.granted_scopes()
            lines.append((True, "The token exists and refreshes."))
            exact = scopes == REQUIRED_SCOPES
            lines.append((exact, "The granted scopes are exactly gmail.compose + gmail.readonly." if exact else
                          f"The granted scopes are {', '.join(sorted(scopes)) or 'unknown'}, not exactly "
                          "gmail.compose + gmail.readonly: run town gmail-connect again."))
        account = client.profile_email()
        right = account == SENDING_ACCOUNT
        lines.append((right, f"The account is {SENDING_ACCOUNT}." if right
                      else f"The account is {account or 'unknown'}, not {SENDING_ACCOUNT}: run town gmail-connect."))
        lines.append((True, f"Drafts in the Town Gmail: {client.count_drafts()}."))
        if test_draft and right:
            raw = build_message(SENDING_ACCOUNT, TEST_SUBJECT,
                                "This is a test draft from the Town, addressed to the Town's own Gmail. It was never "
                                "sent. Look at it in Gmail's Drafts folder, then delete it.\n\nNever send a Town "
                                "draft by hand: that skips the Guard's daily cap and do-not-contact check.\n")
            saved = client.create_draft(raw)
            lines.append((True, f"Test draft saved (draft {saved.draft_id}) to {SENDING_ACCOUNT}, subject "
                                f"\"{TEST_SUBJECT}\". Nothing was sent."))
    except GmailError as err:
        lines.append((False, str(err)))
    return lines
