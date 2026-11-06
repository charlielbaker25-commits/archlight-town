"""Talking to Cloudflare Pages: the real client, the fake one, and keeping the token secret.

There are two clients with the same four jobs: deploy a folder, list the project's deployments,
delete one, and create the project if it's missing.
- RealCloudflare uploads with Wrangler (pinned in tools/wrangler/) and lists and deletes through the
  Pages REST API (Python's own urllib, no new library). It's only ever made for Charlie's real
  instance, by the launcher (see launcher._cloudflare), and it refuses to exist inside pytest.
- FakeCloudflare does nothing outside this laptop: it records what it would have deployed in a
  folder. The dev instance and every test use it.

Secrets: the Cloudflare API token and account ID are read from .env at the moment they're needed,
handed to Wrangler through its environment (together with only the basic system variables Node
needs to start), and scrubbed from every piece of text before it's logged, printed or raised.
"""
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from dotenv import dotenv_values

REPO_ROOT = Path(__file__).resolve().parents[2]
WRANGLER_JS = REPO_ROOT / "tools" / "wrangler" / "node_modules" / "wrangler" / "bin" / "wrangler.js"
WRANGLER_INSTALL_HINT = "npm ci --prefix tools/wrangler"
API = "https://api.cloudflare.com/client/v4"
TOKEN_KEY, ACCOUNT_KEY = "CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID"
PRODUCTION_BRANCH = "main"
DEPLOY_TIMEOUT_S = 600
HTTP_TIMEOUT_S = 30
MAX_LIST_PAGES = 40
# `town cloudflare-check` asks to delete a Workers script with this prefix and a random ending. It never exists.
WORKERS_PROBE_PREFIX = "archlight-permission-probe-"
# The few system variables Node and Wrangler need to start on Windows (and elsewhere). Nothing else
# from Charlie's environment reaches Wrangler: no ANTHROPIC_* key, no other secret.
SYSTEM_VARS = ("PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "TEMP", "TMP",
               "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "HOME", "APPDATA", "LOCALAPPDATA")


class CloudflareError(RuntimeError):
    """Something on Cloudflare's side failed. Its message never contains the token or account ID."""


@dataclass(frozen=True)
class Deployment:
    id: str
    url: str
    created_on: str


@dataclass(frozen=True)
class FetchedPage:
    """What a plain HTTP GET of a link answered (the outreach package's live check). `body` is None from the fake,
    which only keeps each file's SHA-256."""
    status: int
    headers: dict[str, str]          # lowercase names
    sha256: str
    body: bytes | None = None


class CloudflareClient(Protocol):
    real: bool

    def fetch(self, url: str) -> FetchedPage: ...                    # through this computer's own DNS
    def fetch_public_dns(self, url: str) -> FetchedPage: ...         # the host resolved by public DNS over HTTPS
    def fetch_pages_dev(self, url: str) -> FetchedPage: ...          # the newest deployment's *.pages.dev address

    def deploy(self, folder: Path) -> Deployment: ...
    def list_deployments(self) -> list[Deployment]: ...
    def delete_deployment(self, deployment_id: str) -> None: ...
    def ensure_project(self) -> bool: ...


def redact(text: str, secrets: list[str]) -> str:
    """Every secret (and its URL-encoded form) replaced with [hidden]."""
    for secret in sorted((s for s in secrets if s and len(s) >= 6), key=len, reverse=True):
        text = text.replace(secret, "[hidden]").replace(urllib.parse.quote(secret, safe=""), "[hidden]")
    return text


# ---------------- the fake (dev instance and tests) ----------------

class FakeCloudflare:
    """Never leaves this laptop. Records each 'deployment' (its file list and every file's hash) in
    `folder`/deployments.json, so dev runs and tests can see exactly what would have gone live."""
    real = False

    def __init__(self, folder: Path, fail_deploy: bool = False, fail_delete: bool = False) -> None:
        self.folder = Path(folder)
        self.fail_deploy = fail_deploy
        self.fail_delete = fail_delete
        self.fetch_status: int | None = None     # tests: make the "live" link answer this status instead
        self.fetches: list[str] = []
        self.blocked_routes: set[str] = set()   # tests: "local", "public_dns", "pages_dev" fail like a blocked network

    @property
    def _file(self) -> Path:
        return self.folder / "deployments.json"

    def _load(self) -> dict[str, Any]:
        if self._file.is_file():
            return json.loads(self._file.read_text(encoding="utf-8"))
        return {"project": False, "deployments": []}

    def _save(self, state: dict[str, Any]) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        self._file.write_text(json.dumps(state, indent=2), encoding="utf-8")

    def deploy(self, folder: Path) -> Deployment:
        if self.fail_deploy:
            raise CloudflareError("the (fake) upload failed")
        state = self._load()
        folder = Path(folder)
        files = {p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in sorted(folder.rglob("*")) if p.is_file()}
        number = len(state["deployments"]) + 1
        deployment = Deployment(f"fake-{number}", f"https://fake-{number}.showcaseexample.pages.dev",
                                datetime.now(timezone.utc).isoformat(timespec="seconds"))
        headers = (folder / "_headers").read_text(encoding="utf-8") if (folder / "_headers").is_file() else ""
        state["deployments"].append({**deployment.__dict__, "files": files, "deleted": False, "headers": headers})
        self._save(state)
        return deployment

    def list_deployments(self) -> list[Deployment]:
        live = [d for d in self._load()["deployments"] if not d["deleted"]]
        return [Deployment(d["id"], d["url"], d["created_on"]) for d in reversed(live)]   # newest first

    def delete_deployment(self, deployment_id: str) -> None:
        if self.fail_delete:
            raise CloudflareError(f"the (fake) delete of {deployment_id} failed")
        state = self._load()
        for d in state["deployments"]:
            if d["id"] == deployment_id:
                d["deleted"] = True
        self._save(state)

    def ensure_project(self) -> bool:
        state = self._load()
        created = not state["project"]
        state["project"] = True
        self._save(state)
        return created

    def fetch(self, url: str) -> FetchedPage:
        return self._route("local", url)

    def fetch_public_dns(self, url: str) -> FetchedPage:
        return self._route("public_dns", url)

    def fetch_pages_dev(self, url: str) -> FetchedPage:
        return self._route("pages_dev", url)

    def _route(self, route: str, url: str) -> FetchedPage:
        if route in self.blocked_routes:
            raise CloudflareError(f"(fake) {route} is blocked on this network")
        self.fetches.append(url)
        return self._answer(url)

    def _answer(self, url: str) -> FetchedPage:
        """What the newest recorded deployment would answer for `url` (nothing goes online): the page's hash and,
        for a /p/ path, the noindex header only if that deployment's _headers file sets it."""
        live = [d for d in self._load()["deployments"] if not d["deleted"]]
        path = urllib.parse.urlparse(url).path.strip("/")
        name = f"{path}/index.html" if path else "index.html"
        if not live or name not in live[-1]["files"]:
            return FetchedPage(404, {}, "")
        headers = {}
        rules = live[-1].get("headers", "")
        if path.startswith("p/") and re.search(r"^/p/\*\s*\n(?:[ \t]+.*\n)*?[ \t]+X-Robots-Tag:\s*noindex", rules + "\n",
                                               re.MULTILINE):
            headers["x-robots-tag"] = "noindex"
        return FetchedPage(self.fetch_status or 200, headers, live[-1]["files"][name])

    def recorded(self) -> list[dict[str, Any]]:
        """Every recorded deployment, oldest first (for tests and `town cloudflare-check` on dev)."""
        return self._load()["deployments"]


# ---------------- the real one (Charlie's real instance only) ----------------

Runner = Callable[..., subprocess.CompletedProcess]
Http = Callable[[str, str, dict[str, str], bytes | None], tuple[int, dict[str, Any]]]


def _urllib_http(method: str, url: str, headers: dict[str, str], body: bytes | None) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_S) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as err:
        try:
            return err.code, json.loads(err.read() or b"{}")
        except ValueError:
            return err.code, {}


def _urllib_get(url: str) -> tuple[int, dict[str, str], bytes]:
    """A plain GET (the outreach package's live check): no browser, no cookies, at most 5 MB read."""
    request = urllib.request.Request(url, headers={"User-Agent": "showcaseexample-Town/1.0 (live check)"})
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_S) as response:
            return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read(5 * 1024 * 1024)
    except urllib.error.HTTPError as err:
        return err.code, {k.lower(): v for k, v in (err.headers or {}).items()}, b""
    except (urllib.error.URLError, OSError) as err:
        raise CloudflareError(f"couldn't reach {url} ({type(err).__name__})") from None


DOH_URLS = ("https://cloudflare-dns.com/dns-query", "https://1.1.1.1/dns-query")


def _doh_lookup(host: str) -> list[str]:
    """The host's IPv4 addresses from Cloudflare's public DNS over HTTPS (the JSON API, plain urllib), so a home
    network whose own DNS doesn't resolve showcase.example.com can still reach it. Tried at cloudflare-dns.com, then at
    1.1.1.1 directly (its certificate covers that address). Raises CloudflareError if neither answers."""
    last = "no answer"
    for base in DOH_URLS:
        request = urllib.request.Request(f"{base}?name={urllib.parse.quote(host)}&type=A",
                                         headers={"Accept": "application/dns-json",
                                                  "User-Agent": "showcaseexample-Town/1.0 (live check)"})
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_S) as response:
                answer = json.loads(response.read(64 * 1024) or b"{}")
        except (urllib.error.URLError, OSError, ValueError) as err:
            last = f"{base}: {type(err).__name__}"
            continue
        ips = [a["data"] for a in answer.get("Answer") or [] if a.get("type") == 1 and a.get("data")]
        if ips:
            return ips
        last = f"{base}: no address for {host}"
    raise CloudflareError(f"public DNS over HTTPS didn't resolve {host} ({last})")


def _pinned_get(url: str, ip: str) -> tuple[int, dict[str, str], bytes]:
    """GET `url` from one IP address, with the real Host header and TLS name (SNI): the certificate is still
    verified for the link's own host name (Python's default checks), only the DNS lookup is skipped."""
    import http.client
    import socket
    import ssl
    parts = urllib.parse.urlparse(url)
    host = parts.hostname or ""
    context = ssl.create_default_context()

    class Pinned(http.client.HTTPSConnection):
        def connect(self) -> None:
            sock = socket.create_connection((ip, self.port), self.timeout)
            self.sock = context.wrap_socket(sock, server_hostname=host)   # verifies the cert for `host`

    connection = Pinned(host, parts.port or 443, timeout=HTTP_TIMEOUT_S, context=context)
    try:
        connection.request("GET", (parts.path or "/") + (f"?{parts.query}" if parts.query else ""),
                           headers={"Host": host, "User-Agent": "showcaseexample-Town/1.0 (live check)"})
        response = connection.getresponse()
        return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read(5 * 1024 * 1024)
    except (OSError, http.client.HTTPException) as err:
        raise CloudflareError(f"couldn't reach {host} at {ip} ({type(err).__name__})") from None
    finally:
        connection.close()


class RealCloudflare:
    """Cloudflare Pages for real. Made only by launcher._cloudflare() for Charlie's real instance.
    Inside pytest it refuses to do anything (every call reads the credentials first, and that refuses),
    so no test can ever reach Cloudflare."""
    real = True

    def __init__(self, env_file: Path, project: str, runner: Runner = subprocess.run, http: Http = _urllib_http,
                 node: str | None = None, get: Callable[[str], tuple[int, dict[str, str], bytes]] = _urllib_get,
                 doh: Callable[[str], list[str]] = _doh_lookup,
                 pinned_get: Callable[[str, str], tuple[int, dict[str, str], bytes]] = _pinned_get) -> None:
        self.env_file = Path(env_file)
        self.project = project
        self.runner = runner
        self.http = http
        self.node = node
        self.get = get
        self.doh = doh
        self.pinned_get = pinned_get

    def fetch(self, url: str) -> FetchedPage:
        """A plain public GET of a live link (no credentials: it's what any visitor sees). Never inside pytest."""
        self._no_tests()
        status, headers, body = self.get(url)
        return FetchedPage(status, headers, hashlib.sha256(body).hexdigest(), body)

    def _no_tests(self) -> None:
        if "PYTEST_CURRENT_TEST" in os.environ and not getattr(self, "allow_in_tests", False):
            raise CloudflareError("the real Cloudflare client is never used in tests")

    def fetch_public_dns(self, url: str) -> FetchedPage:
        """The same public GET, but the host is looked up with public DNS over HTTPS and the page fetched from that
        address (Host and TLS name stay the link's own, the certificate is verified). Tries each address returned."""
        self._no_tests()
        host = urllib.parse.urlparse(url).hostname or ""
        last: CloudflareError | None = None
        for ip in self.doh(host):
            try:
                status, headers, body = self.pinned_get(url, ip)
            except CloudflareError as err:
                last = err
                continue
            return FetchedPage(status, headers, hashlib.sha256(body).hexdigest(), body)
        raise last or CloudflareError(f"public DNS over HTTPS gave no address for {host}")

    def fetch_pages_dev(self, url: str) -> FetchedPage:
        """The same path on the newest deployment's own *.pages.dev address (from the Pages API), for a network that
        blocks showcase.example.com itself. That address serves the same files with the same noindex rule."""
        self._no_tests()
        deployments = self.list_deployments()
        if not deployments or not deployments[0].url:
            raise CloudflareError("no deployment with a pages.dev address")
        parts = urllib.parse.urlparse(url)
        target = deployments[0].url.rstrip("/") + (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        status, headers, body = self.get(target)
        return FetchedPage(status, headers, hashlib.sha256(body).hexdigest(), body)

    # The token and account ID are read from .env each time, and never kept on the object.
    def _credentials(self) -> tuple[str, str]:
        if "PYTEST_CURRENT_TEST" in os.environ and not getattr(self, "allow_in_tests", False):
            raise CloudflareError("the real Cloudflare client is never used in tests")
        values = dotenv_values(self.env_file)
        token, account = (values.get(TOKEN_KEY) or "").strip(), (values.get(ACCOUNT_KEY) or "").strip()
        if not token or not account:
            raise CloudflareError(f"{TOKEN_KEY} and {ACCOUNT_KEY} must both be set in .env")
        return token, account

    def _call(self, method: str, path: str, payload: dict[str, Any] | None = None,
              account_scoped: bool = True) -> tuple[int, dict[str, Any]]:
        token, account = self._credentials()
        url = f"{API}/accounts/{account}{path}" if account_scoped else f"{API}{path}"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        try:
            status, body = self.http(method, url, headers, json.dumps(payload).encode() if payload else None)
        except (OSError, ValueError) as err:
            raise CloudflareError(redact(f"couldn't reach Cloudflare: {err}", [token, account])) from None
        return status, json.loads(redact(json.dumps(body), [token, account]))

    def _ok(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        status, body = self._call(method, path, payload)
        if status >= 300 or not body.get("success", False):
            errors = "; ".join(str(e.get("message")) for e in body.get("errors") or []) or f"HTTP {status}"
            raise CloudflareError(f"{method} {path.split('?')[0]} failed: {errors}")
        return body

    def deploy(self, folder: Path) -> Deployment:
        token, account = self._credentials()
        if not WRANGLER_JS.is_file():
            raise CloudflareError(f"Wrangler isn't installed. Run once: {WRANGLER_INSTALL_HINT}")
        node = self.node or shutil.which("node")
        if not node:
            raise CloudflareError("Node.js isn't installed (Wrangler needs it).")
        env = {k: os.environ[k] for k in SYSTEM_VARS if k in os.environ}
        env.update({TOKEN_KEY: token, ACCOUNT_KEY: account, "WRANGLER_SEND_METRICS": "false", "CI": "true",
                    "NO_COLOR": "1"})
        command = [node, str(WRANGLER_JS), "pages", "deploy", str(folder), "--project-name", self.project,
                   "--branch", PRODUCTION_BRANCH, "--commit-dirty=true"]
        try:
            done = self.runner(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                               timeout=DEPLOY_TIMEOUT_S, env=env, check=False)
        except subprocess.TimeoutExpired:
            raise CloudflareError(f"Wrangler took longer than {DEPLOY_TIMEOUT_S}s") from None
        output = redact((done.stdout or "") + (done.stderr or ""), [token, account])
        if done.returncode != 0:
            raise CloudflareError(f"Wrangler failed: {output.strip()[-800:]}")
        printed = re.search(rf"https://[a-z0-9-]+\.{re.escape(self.project)}\.pages\.dev", output)
        newest = self.list_deployments()
        match = [d for d in newest if printed and d.url.rstrip("/") == printed.group(0)]
        if not (match or newest):
            raise CloudflareError("Wrangler finished, but Cloudflare lists no deployment")
        return (match or newest)[0]

    def list_deployments(self) -> list[Deployment]:
        found: list[Deployment] = []
        for page in range(1, MAX_LIST_PAGES + 1):
            body = self._ok("GET", f"/pages/projects/{self.project}/deployments?page={page}&per_page=25")
            rows = body.get("result") or []
            found += [Deployment(str(r["id"]), str(r.get("url") or ""), str(r.get("created_on") or "")) for r in rows]
            info = body.get("result_info") or {}
            if not rows or page >= int(info.get("total_pages") or page):
                break
        return sorted(found, key=lambda d: d.created_on, reverse=True)

    def delete_deployment(self, deployment_id: str) -> None:
        self._ok("DELETE", f"/pages/projects/{self.project}/deployments/{deployment_id}?force=true")

    def ensure_project(self) -> bool:
        status, _ = self._call("GET", f"/pages/projects/{self.project}")
        if status == 200:
            return False
        self._ok("POST", "/pages/projects", {"name": self.project, "production_branch": PRODUCTION_BRANCH})
        return True

    def _workers_write_check(self) -> tuple[bool, str]:
        """Can the token CHANGE Workers? Reading the Workers list is fine: Cloudflare merged Workers and Pages,
        so a token with only Account > Cloudflare Pages > Edit can read it (Charlie's token does).
        The write test changes nothing: it asks Cloudflare to delete a Workers script that doesn't exist (a
        random name, checked against the list first). A token without write permission is refused (401/403);
        "no such script" (404) means Cloudflare would have deleted it, so the token can write Workers.
        Anything else counts as FAIL (fail closed: we couldn't tell)."""
        status, body = self._call("GET", "/workers/scripts")
        existing = {str(s.get("id")) for s in body.get("result") or []} if status == 200 else set()
        probe = f"{WORKERS_PROBE_PREFIX}{secrets.token_hex(6)}"
        while probe in existing:
            probe = f"{WORKERS_PROBE_PREFIX}{secrets.token_hex(6)}"
        status, body = self._call("DELETE", f"/workers/scripts/{probe}")
        if status in (401, 403):
            return True, "It can't change Workers (good: Pages only; reading the Workers list is fine)."
        if status == 404:
            return False, ("It CAN change Workers: Cloudflare would have deleted a Workers script (it only said "
                           "there was none by that name). Make a new token with only Account > Cloudflare Pages > Edit.")
        return False, (f"Couldn't tell whether it can change Workers (HTTP {status} to a delete of a script that "
                       "doesn't exist). Nothing was changed. Check the token has only Account > Cloudflare Pages > Edit.")

    def check(self) -> list[tuple[bool, str]]:
        """`town cloudflare-check`: read-only. (passed, plain-words line) for each check."""
        lines: list[tuple[bool, str]] = []
        try:
            self._credentials()
        except CloudflareError as err:
            return [(False, str(err))]
        status, body = self._call("GET", "/user/tokens/verify", account_scoped=False)
        if status != 200:
            status, body = self._call("GET", "/tokens/verify")   # an account-owned token
        active = status == 200 and (body.get("result") or {}).get("status") == "active"
        lines.append((active, "The token works and is active." if active
                      else f"The token didn't verify (HTTP {status})."))
        status, body = self._call("GET", "/pages/projects?per_page=10")
        pages_ok = status == 200 and body.get("success", False)
        lines.append((pages_ok, "It can read Cloudflare Pages." if pages_ok else f"It can't read Pages (HTTP {status})."))
        lines.append(self._workers_write_check())
        status, body = self._call("GET", "/zones?per_page=5", account_scoped=False)
        no_zones = status in (401, 403) or not body.get("result")
        lines.append((no_zones, "It can't see DNS zones (good: Pages only)." if no_zones
                      else "It CAN see DNS zones: make a new token with only Account > Cloudflare Pages > Edit."))
        status, body = self._call("GET", f"/pages/projects/{self.project}")
        if status == 200:
            domains = (body.get("result") or {}).get("domains") or []
            lines.append((True, f"Found the Pages project '{self.project}' (domains: {', '.join(domains) or 'none'})."))
        else:
            lines.append((True, f"No Pages project '{self.project}' yet: the first approved homepage publish "
                                "creates it."))
        return lines
