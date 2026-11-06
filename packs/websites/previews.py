"""Preview links and Example Web Studio's own homepage, on Cloudflare Pages at showcase.example.com.

The site is ONE Cloudflare Pages project:
- showcase.example.com/            Example Web Studio's homepage (indexable)
- showcase.example.com/p/<token>/  one private preview per lead; <token> is 256 random bits, never reused,
                               never linked or listed anywhere. Every /p/ page sends X-Robots-Tag:
                               noindex (_headers), and robots.txt disallows /p/.

A Pages upload replaces the whole site, so every deploy is rebuilt from the database: the live
homepage plus every live, unexpired preview, nothing else. Each built site loads its fonts and images
from /_astro/..., so the Town copies each site's index.html to its place and merges every site's
_astro/ files at the root. Astro names those files by their content, so the same name means the same
bytes; if two ever differ, the deploy is refused. The files are never changed, so what goes live is
byte-for-byte what the Critic passed.

Rules, all plain code:
- Publishing is an approved action (publish_preview / publish_homepage). It can only be proposed when
  critic.can_propose_publish() is true, and a preview only once the homepage is live. At execution,
  every check runs again and the dist hash must still be the approved, Critic-passed one.
- Every old deployment is deleted on Cloudflare after each new one (its own *.pages.dev address
  would otherwise keep serving a preview that was taken down). A failed delete is retried at the
  next upkeep.
- Previews come down after previews.expire_days (limits.yaml), checked whenever the Town starts or
  runs. Nothing here runs while the Town is stopped.
- Which Cloudflare client this gets is decided by the launcher: only Charlie's real instance gets the
  real one. The dev instance and tests get the fake (cloudflare.py).
"""
import json
import os
import re
import secrets
import shutil
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from engine.budget import month_start_utc
from engine.db import transaction, utc_now
from engine.events import log_event
from engine.executor import Handler
from engine.killswitch import KillSwitch
from engine.limits import Limits
from packs.websites.cloudflare import CloudflareClient, CloudflareError, Deployment
from packs.websites.critic import can_propose_publish, latest_review
from packs.websites.critic_measure import dist_hash

AGENT = "builder"          # proposes publishing its Critic-passed sites
SITE_URL = "https://showcase.example.com"
PROJECT = "showcaseexample"   # the Cloudflare Pages project
PREVIEW_DIR = "p"
TOKEN_BYTES = 32           # 256 bits; Charlie asked for at least 128
TOKEN = re.compile(r"^[0-9a-f]{64}$")
PUBLISH_PREVIEW, PUBLISH_HOMEPAGE = "publish_preview", "publish_homepage"
LIVE_TYPES = frozenset({PUBLISH_PREVIEW, PUBLISH_HOMEPAGE})   # really run once approved (not dry-run)
ALLOWED_ROOT = {"index.html", "_astro"}
# The homepage's second page (showcase.example.com/privacy/). Only the homepage may have it: a preview is one page.
HOMEPAGE_PAGES = ("privacy/index.html",)
LOCK_STALE_S = 30 * 60
TIME = "%Y-%m-%dT%H:%M:%SZ"

HEADERS = """# Written by the Town on every deploy. Previews stay out of search engines, and their private
# address isn't passed on to any site a visitor clicks through to.
/p/*
  X-Robots-Tag: noindex
  Referrer-Policy: no-referrer

# Cloudflare's own *.pages.dev addresses for this project never go into search engines either.
https://:project.pages.dev/*
  X-Robots-Tag: noindex

https://:version.:project.pages.dev/*
  X-Robots-Tag: noindex
"""
ROBOTS = "User-agent: *\nDisallow: /p/\n"


class PublishError(RuntimeError):
    pass


@dataclass(frozen=True)
class PreviewLimits:
    expire_days: int
    deploys_per_month: int
    max_files: int
    max_file_bytes: int

    @classmethod
    def read(cls, limits: Limits) -> "PreviewLimits":
        return cls(int(limits.get("previews.expire_days")), int(limits.get("previews.deploys_per_month")),
                   int(limits.get("previews.max_files_per_deploy")),
                   int(float(limits.get("previews.max_file_mb")) * 1024 * 1024))


@dataclass
class Publisher:
    """Everything publishing needs. The launcher picks the client (real only on Charlie's real Town)."""
    client: CloudflareClient
    limits: Limits
    killswitch: KillSwitch
    work_dir: Path          # <instance data>/deploys: each upload is assembled here, then removed

    def handlers(self) -> dict[str, Handler]:
        run = lambda conn, action_id, content: execute_publish(self, conn, action_id, content)
        return {PUBLISH_PREVIEW: Handler(PUBLISH_PREVIEW, True, run, describe_publish),
                PUBLISH_HOMEPAGE: Handler(PUBLISH_HOMEPAGE, True, run, describe_publish)}


@dataclass(frozen=True)
class LiveSite:
    kind: str
    build_id: int
    token: str | None
    dist: Path
    dist_hash: str
    preview_id: int | None = None


def preview_url(token: str) -> str:
    return f"{SITE_URL}/{PREVIEW_DIR}/{token}/"


def describe_publish(content: dict[str, Any]) -> str:
    return f"publish build #{content.get('build_id')} ({content.get('business')}) at {content.get('url') or SITE_URL}"


def _plus_days(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime(TIME)


# ---------------- what's live ----------------

def homepage_live(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM previews WHERE kind = 'homepage' AND status = 'live'").fetchone() is not None


def live_sites(conn: sqlite3.Connection) -> list[LiveSite]:
    """The homepage and every live preview that hasn't reached its expiry time."""
    rows = conn.execute(
        "SELECT p.*, b.workspace FROM previews p JOIN builds b ON b.id = p.build_id WHERE p.status = 'live'"
        " AND (p.kind = 'homepage' OR p.expires_at > ?) ORDER BY p.kind = 'preview', p.id", (utc_now(),))
    return [LiveSite(r["kind"], r["build_id"], r["token"], Path(r["workspace"]) / "dist", r["dist_hash"], r["id"])
            for r in rows]


def check_layout(dist: Path, kind: str = "preview") -> None:
    """Only index.html and _astro/ at a site's root, so sites can share the root /_astro/ folder. The
    homepage may also have its privacy page (privacy/index.html, nothing else in that folder)."""
    if not (dist / "index.html").is_file():
        raise PublishError(f"{dist} has no index.html")
    allowed = ALLOWED_ROOT | ({"privacy"} if kind == "homepage" else set())
    extra = sorted(p.name for p in dist.iterdir() if p.name not in allowed)
    if kind == "homepage" and (dist / "privacy").exists():
        extra += sorted(f"privacy/{p.name}" for p in (dist / "privacy").iterdir() if p.name != "index.html")
    if extra:
        raise PublishError(f"the built site has files outside index.html and _astro/ ({', '.join(extra)}), "
                           "which can't share the live site's root")


def assemble(sites: list[LiveSite], folder: Path) -> dict[str, Any]:
    """Builds the whole live site in `folder`. Copies files unchanged; refuses a clash in /_astro/.
    The homepage is always in it when it's live. It is only missing if its files changed after approval:
    then a takedown must still be able to remove previews, so the deploy goes ahead without it."""
    folder.mkdir(parents=True)
    homepages = [s for s in sites if s.kind == "homepage"]
    if len(homepages) > 1:
        raise PublishError("the live site can only have one homepage")
    astro: dict[str, bytes] = {}
    for site in sites:
        check_layout(site.dist, site.kind)
        target = folder if site.kind == "homepage" else folder / PREVIEW_DIR / str(site.token)
        target.mkdir(parents=True, exist_ok=True)
        shutil.copy2(site.dist / "index.html", target / "index.html")
        for page in HOMEPAGE_PAGES if site.kind == "homepage" else ():
            if (site.dist / page).is_file():
                (folder / page).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(site.dist / page, folder / page)
        assets = site.dist / "_astro"
        for path in sorted(assets.rglob("*")) if assets.is_dir() else []:
            if not path.is_file():
                continue
            name = path.relative_to(site.dist).as_posix()
            data = path.read_bytes()
            if name in astro and astro[name] != data:
                raise PublishError(f"two sites have different files named {name}; refusing to deploy")
            if name not in astro:
                astro[name] = data
                (folder / name).parent.mkdir(parents=True, exist_ok=True)
                (folder / name).write_bytes(data)
    (folder / "_headers").write_text(HEADERS, encoding="utf-8")
    (folder / "robots.txt").write_text(ROBOTS, encoding="utf-8")
    return {"homepage_build": homepages[0].build_id if homepages else None,
            "previews": [s.preview_id for s in sites if s.kind == "preview"]}


# ---------------- deploying (one at a time) ----------------

class _DeployLock:
    """One deploy at a time on this instance (an expiry and a publish could otherwise race)."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def __enter__(self) -> "_DeployLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and time.time() - self.path.stat().st_mtime > LOCK_STALE_S:
            self.path.unlink(missing_ok=True)   # left behind by a crash
        try:
            os.close(os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        except FileExistsError:
            raise PublishError("another deploy is running; try again in a minute") from None
        return self

    def __exit__(self, *exc: Any) -> None:
        self.path.unlink(missing_ok=True)


def deploys_this_month(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM deployments WHERE created_at >= ?", (month_start_utc(),)).fetchone()[0]


def deploy_live_set(pub: Publisher, conn: sqlite3.Connection, reason: str,
                    adding: LiveSite | None = None, replacing_homepage: bool = False) -> Deployment:
    """Uploads the full live set (plus `adding`), then deletes every older deployment on Cloudflare."""
    pub.killswitch.check()
    lim = PreviewLimits.read(pub.limits)
    with _DeployLock(pub.work_dir / "deploy.lock"):
        used = deploys_this_month(conn)
        if used >= lim.deploys_per_month:
            raise PublishError(f"{used} deploys this month already (previews.deploys_per_month is "
                               f"{lim.deploys_per_month}); it waits for next month or a limit change")
        sites = _checked_live_sites(conn)
        if replacing_homepage:
            sites = [s for s in sites if s.kind != "homepage"]
        sites += [adding] if adding else []
        folder = pub.work_dir / f"upload-{datetime.now():%Y%m%d-%H%M%S-%f}"
        try:
            live_set = assemble(sites, folder)
            files = [p for p in folder.rglob("*") if p.is_file()]
            if len(files) > lim.max_files:
                raise PublishError(f"{len(files)} files is over Cloudflare's {lim.max_files} per upload")
            big = [p.name for p in files if p.stat().st_size > lim.max_file_bytes]
            if big:
                raise PublishError(f"over Cloudflare's per-file size limit: {', '.join(big[:3])}")
            pub.killswitch.check()
            if pub.client.ensure_project():
                log_event(conn, AGENT, "cloudflare_project_created", {"project": PROJECT, "fake": not pub.client.real})
            deployment = pub.client.deploy(folder)
        finally:
            shutil.rmtree(folder, ignore_errors=True)
        conn.execute("INSERT INTO deployments (deployment_id, url, fake, reason, live_set, files, created_at)"
                     " VALUES (?, ?, ?, ?, ?, ?, ?)", (deployment.id, deployment.url, int(not pub.client.real),
                                                       reason, json.dumps(live_set), len(files), utc_now()))
        log_event(conn, AGENT, "deployment_made", {"deployment": deployment.id, "fake": not pub.client.real,
                                                   "reason": reason, "files": len(files),
                                                   "previews_live": len(live_set["previews"])})
        delete_old_deployments(pub, conn, keep=deployment.id)
        return deployment


def _checked_live_sites(conn: sqlite3.Connection) -> list[LiveSite]:
    """Live sites whose files are still exactly what went live. One that changed or vanished is taken
    down rather than republished with files nobody approved."""
    good = []
    for site in live_sites(conn):
        current = dist_hash(site.dist) if (site.dist / "index.html").is_file() else None
        if current == site.dist_hash:
            good.append(site)
        else:
            end_preview(conn, site.preview_id, "taken_down", "its files changed after approval", "code")
    return good


def delete_old_deployments(pub: Publisher, conn: sqlite3.Connection, keep: str) -> int:
    """Deletes every deployment on Cloudflare except `keep` (the newest), so no old *.pages.dev address
    keeps serving a preview that came down. Returns how many deletes failed (retried at next upkeep)."""
    failed = 0
    try:
        existing = pub.client.list_deployments()
    except CloudflareError as err:
        log_event(conn, AGENT, "deployment_delete_failed", {"error": str(err)[:300]})
        return 1
    for old in existing:
        if old.id == keep:
            continue
        try:
            pub.client.delete_deployment(old.id)
            conn.execute("UPDATE deployments SET deleted_at = ?, delete_error = NULL WHERE deployment_id = ?",
                         (utc_now(), old.id))
            log_event(conn, AGENT, "deployment_deleted", {"deployment": old.id})
        except CloudflareError as err:
            failed += 1
            conn.execute("UPDATE deployments SET delete_error = ? WHERE deployment_id = ?", (str(err)[:300], old.id))
            log_event(conn, AGENT, "deployment_delete_failed", {"deployment": old.id, "error": str(err)[:300]})
    return failed


# ---------------- proposing (agent work) ----------------

def _build_row(conn: sqlite3.Connection, build_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT b.*, l.business_name FROM builds b LEFT JOIN leads l ON l.id = b.lead_id"
                       " WHERE b.id = ?", (build_id,)).fetchone()
    if not row:
        raise PublishError(f"No build #{build_id}.")
    return row


def _waiting_publish(conn: sqlite3.Connection, build_id: int, homepage: bool) -> bool:
    """A publish for this build (or, for the homepage, any homepage publish) still waiting or approved."""
    for row in conn.execute("SELECT action_type, content FROM actions WHERE status IN ('proposed', 'approved')"
                            " AND action_type IN (?, ?)", (PUBLISH_PREVIEW, PUBLISH_HOMEPAGE)):
        if json.loads(row["content"]).get("build_id") == build_id or (homepage and row["action_type"] == PUBLISH_HOMEPAGE):
            return True
    return False


def _gate(conn: sqlite3.Connection, build: sqlite3.Row) -> None:
    """The checks shared by proposing and executing. Raises PublishError with the reason."""
    if build["status"] != "built":
        raise PublishError(f"Build #{build['id']} isn't a finished build.")
    if not can_propose_publish(conn, build["id"]):
        raise PublishError(f"Build #{build['id']} can't be published: the Critic hasn't passed these exact files.")
    check_layout(Path(build["workspace"]) / "dist", build["kind"])
    if build["kind"] == "preview":
        if not build["lead_id"]:
            raise PublishError(f"Build #{build['id']} is a test-set build; only a lead's site can be published.")
        if not homepage_live(conn):
            raise PublishError("Example Web Studio's homepage isn't live yet. Publish it first (a prospect will "
                               "look at the domain behind their preview link).")
        if conn.execute("SELECT 1 FROM previews WHERE lead_id = ? AND status = 'live'", (build["lead_id"],)).fetchone():
            raise PublishError(f"Lead #{build['lead_id']} already has a live preview. Take it down first "
                               "(`town takedown`).")


def new_token(conn: sqlite3.Connection, build_id: int) -> str:
    """256 random bits, never handed out before (every token ever made is kept)."""
    while True:
        token = secrets.token_hex(TOKEN_BYTES)
        if not conn.execute("SELECT 1 FROM preview_tokens WHERE token = ?", (token,)).fetchone():
            conn.execute("INSERT INTO preview_tokens (token, build_id, created_at) VALUES (?, ?, ?)",
                         (token, build_id, utc_now()))
            return token


def propose_publish(town: Any, build_id: int) -> int:
    """Proposes publishing a Critic-passed build: a lead's preview, or the homepage. Returns the action id."""
    town.killswitch.check()
    conn = town.conn
    build = _build_row(conn, build_id)
    _gate(conn, build)
    homepage = build["kind"] == "homepage"
    if _waiting_publish(conn, build_id, homepage):
        raise PublishError(f"A publish for build #{build_id} (or the homepage) is already waiting in `town pending`.")
    review = latest_review(conn, build_id) or {}
    days = PreviewLimits.read(town.limits).expire_days
    token = None if homepage else new_token(conn, build_id)
    business = "Example Web Studio" if homepage else build["business_name"]
    content = {"kind": build["kind"], "build_id": build_id, "lead_id": build["lead_id"], "business": business,
               "dist_hash": review.get("dist_hash"), "review_id": review.get("id"), "critic_verdict": "pass",
               "critic_summary": review.get("summary"),
               "url": SITE_URL + "/" if homepage else preview_url(str(token)), "token": token,
               "expires_after_days": None if homepage else days}
    reason = (f"The Critic passed build #{build_id} (review #{review.get('id')}). " +
              ("This puts Example Web Studio's homepage live at showcase.example.com (indexable)." if homepage else
               f"This puts {business}'s sample site at a private, unlisted link for {days} days."))
    action_id = town.mayor.propose(AGENT, PUBLISH_HOMEPAGE if homepage else PUBLISH_PREVIEW,
                                   "showcase.example.com (homepage)" if homepage else str(business), content, reason)
    if token:
        conn.execute("UPDATE preview_tokens SET action_id = ? WHERE token = ?", (action_id, token))
    return action_id


# ---------------- executing (only after Charlie's approval) ----------------

def execute_publish(pub: Publisher, conn: sqlite3.Connection, action_id: int, content: dict[str, Any]) -> str:
    """Runs an approved publish. Every check again, and the files must still be the approved,
    Critic-passed ones (same dist hash), or it refuses."""
    build = _build_row(conn, int(content.get("build_id") or 0))
    kind = content.get("kind")
    action_type = conn.execute("SELECT action_type FROM actions WHERE id = ?", (action_id,)).fetchone()[0]
    if kind != build["kind"] or action_type != (PUBLISH_HOMEPAGE if kind == "homepage" else PUBLISH_PREVIEW):
        raise PublishError("the action doesn't match the build's kind")
    where, deployment_id = publish_site(pub, conn, action_id, build, content.get("token"), content.get("dist_hash"))
    if not pub.client.real:
        return f"FAKE Cloudflare (dev/test): recorded deployment {deployment_id}; nothing went online. Would be {where}"
    return f"Live at {where} (deployment {deployment_id}; older deployments deleted)."


def publish_site(pub: Publisher, conn: sqlite3.Connection, action_id: int, build: sqlite3.Row, token: Any,
                 approved_hash: Any) -> tuple[str, str]:
    """The publish itself, shared by publish_preview/publish_homepage and the outreach package: every gate again,
    the files byte-for-byte the approved Critic-passed ones, the token the one issued with THIS action. Returns
    (the address, the deployment id)."""
    kind = build["kind"]
    _gate(conn, build)
    dist = Path(build["workspace"]) / "dist"
    current = dist_hash(dist)
    if current != approved_hash or current != (latest_review(conn, build["id"]) or {}).get("dist_hash"):
        raise PublishError(f"build #{build['id']}'s files changed after approval; it needs a new review and approval")
    if kind == "preview":
        issued = conn.execute("SELECT action_id, build_id FROM preview_tokens WHERE token = ?", (str(token),)).fetchone()
        if not (isinstance(token, str) and TOKEN.match(token)) or not issued or issued["action_id"] != action_id \
                or issued["build_id"] != build["id"]:
            raise PublishError("the preview path isn't the one issued with this proposal")
        if conn.execute("SELECT 1 FROM previews WHERE token = ?", (token,)).fetchone():
            raise PublishError("that preview path was already used")
    else:
        token = None
    site = LiveSite(kind, build["id"], token, dist, current)
    deployment = deploy_live_set(pub, conn, f"publish {kind} (action #{action_id})", adding=site,
                                 replacing_homepage=kind == "homepage")
    days = PreviewLimits.read(pub.limits).expire_days
    with transaction(conn):
        if kind == "homepage":
            for old in conn.execute("SELECT id FROM previews WHERE kind = 'homepage' AND status = 'live'").fetchall():
                end_preview(conn, old["id"], "replaced", f"replaced by build #{build['id']}", "code")
        preview_id = conn.execute(
            "INSERT INTO previews (kind, build_id, lead_id, token, dist_hash, action_id, status, published_at,"
            " expires_at) VALUES (?, ?, ?, ?, ?, ?, 'live', ?, ?)",
            (kind, build["id"], build["lead_id"], token, current, action_id, utc_now(),
             None if kind == "homepage" else _plus_days(days))).lastrowid
        log_event(conn, AGENT, "preview_published", {"preview": preview_id, "kind": kind, "build": build["id"],
                                                      "deployment": deployment.id, "fake": not pub.client.real},
                  action_id)
    where = f"{SITE_URL}/" if kind == "homepage" else preview_url(str(token))
    return where, deployment.id


# ---------------- is it really live? (the outreach package checks before its email goes) ----------------

ASSET_REF = re.compile(rb"/_astro/[A-Za-z0-9._-]+")
TITLE = re.compile(rb"<title>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def same_page(body: bytes | None, body_sha256: str, approved: bytes) -> bool:
    """Does what the link served match the approved page? Byte-for-byte first. If Cloudflare rewrote a small part
    of the HTML on the way out (it can hide email addresses behind a script, for example), the page still counts
    as the approved one when its title and every /_astro/ file it loads are exactly the approved page's."""
    import hashlib
    if body_sha256 == hashlib.sha256(approved).hexdigest():
        return True
    if body is None:
        return False
    title = lambda page: (TITLE.search(page).group(1).strip() if TITLE.search(page) else None)
    return (title(body) is not None and title(body) == title(approved)
            and set(ASSET_REF.findall(body)) == set(ASSET_REF.findall(approved)) != set())


# The ways to reach a live link, in order (Charlie, 2026-11-06: his home Wi-Fi's DNS doesn't resolve showcase.example.com,
# so the laptop's own DNS can't be the only way). Each is (the name logged, the client method). Any route counts only
# if the page, the status and the noindex header all check out.
LIVE_ROUTES = (("this laptop's DNS", "fetch"),
               ("public DNS over HTTPS (Cloudflare 1.1.1.1)", "fetch_public_dns"),
               ("the deployment's own pages.dev address", "fetch_pages_dev"))


@dataclass(frozen=True)
class LiveCheck:
    line: str           # what to tell Charlie
    route: str          # which route answered with the approved page


def check_live(pub: Publisher, url: str, approved_page: bytes, sleep: Callable[[float], None] = time.sleep) -> LiveCheck:
    """Plain HTTP GET of the link (no browser): it must answer 200 with the noindex header and the approved page.
    Each try goes through LIVE_ROUTES in order and stops at the first route that checks out; tried
    previews.live_check_tries times, previews.live_check_wait_seconds apart (a new deploy can take a few seconds). A
    read only; nothing changes anywhere. Raises PublishError with what each route saw."""
    tries = max(1, int(pub.limits.get("previews.live_check_tries")))
    wait = float(pub.limits.get("previews.live_check_wait_seconds"))
    seen: dict[str, str] = {}
    for attempt in range(tries):
        for route, method in LIVE_ROUTES:
            pub.killswitch.check()
            fetch = getattr(pub.client, method, None)
            if fetch is None:
                seen[route] = "not available with this client"
                continue
            try:
                page = fetch(url)
            except CloudflareError as err:
                seen[route] = f"didn't answer ({err})"
                continue
            robots = str(page.headers.get("x-robots-tag", "")).lower()
            if page.status != 200:
                seen[route] = f"answered HTTP {page.status}"
            elif "noindex" not in robots:
                seen[route] = "answered without the noindex header"
            elif not same_page(page.body, page.sha256, approved_page):
                seen[route] = "answered with a different page than the one approved"
            else:
                return LiveCheck(f"checked live through {route}: HTTP 200, noindex, the approved page "
                                 f"(try {attempt + 1} of {tries})", route)
        if attempt + 1 < tries:
            sleep(wait)
    raise PublishError(f"the link didn't check out after {tries} tries by any route: "
                       + "; ".join(f"{route} {what}" for route, what in seen.items()))


# ---------------- coming down ----------------

def end_preview(conn: sqlite3.Connection, preview_id: int | None, status: str, reason: str, by: str) -> None:
    if preview_id is None:
        return
    conn.execute("UPDATE previews SET status = ?, ended_at = ?, end_reason = ? WHERE id = ? AND status = 'live'",
                 (status, utc_now(), reason, preview_id))
    event = {"expired": "preview_expired", "taken_down": "preview_taken_down"}.get(status, "preview_replaced")
    log_event(conn, AGENT, event, {"preview": preview_id, "reason": reason, "by": by})


def takedown(pub: Publisher, conn: sqlite3.Connection, build_id: int, reason: str, by: str) -> Deployment:
    """`town takedown <build id> --reason`: Charlie's decision. The preview comes down at once."""
    reason = (reason or "").strip()
    if not reason:
        raise PublishError("A takedown needs a reason.")
    pub.killswitch.check()
    row = conn.execute("SELECT id FROM previews WHERE build_id = ? AND kind = 'preview' AND status = 'live'",
                       (build_id,)).fetchone()
    if not row:
        raise PublishError(f"Build #{build_id} has no live preview.")
    end_preview(conn, row["id"], "taken_down", reason, by)
    return deploy_live_set(pub, conn, f"takedown of build #{build_id}")


def upkeep(pub: Publisher, conn: sqlite3.Connection, on_note: Callable[[str], None] = lambda m: None) -> int:
    """Whenever the Town starts or runs: expired previews come down (one redeploy for all of them), and
    any old deployment whose delete failed before is deleted again. Never while the Town is stopped.
    Returns how many previews came down."""
    if pub.killswitch.is_stopped():
        return 0
    due = conn.execute("SELECT id FROM previews WHERE kind = 'preview' AND status = 'live' AND expires_at <= ?",
                       (utc_now(),)).fetchall()
    days = PreviewLimits.read(pub.limits).expire_days
    for row in due:
        end_preview(conn, row["id"], "expired", f"{days} days after publishing (previews.expire_days)", "code")
    if due:
        deployment = deploy_live_set(pub, conn, f"{len(due)} preview(s) expired")
        on_note(f"{len(due)} preview(s) expired and came down (deployment {deployment.id}).")
        return len(due)
    newest = conn.execute("SELECT deployment_id FROM deployments ORDER BY id DESC LIMIT 1").fetchone()
    stale = conn.execute("SELECT 1 FROM deployments WHERE deleted_at IS NULL AND id < (SELECT MAX(id) FROM"
                         " deployments)").fetchone()
    if newest and stale and delete_old_deployments(pub, conn, keep=newest["deployment_id"]) == 0:
        on_note("Old Cloudflare deployments that failed to delete earlier are now deleted.")
    return 0


def live_previews(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """For `town status`: what's live now (paths are private: shown only in Charlie's terminal)."""
    return [dict(r) for r in conn.execute(
        "SELECT p.id, p.kind, p.build_id, p.token, p.expires_at, l.business_name FROM previews p"
        " LEFT JOIN leads l ON l.id = p.lead_id WHERE p.status = 'live' ORDER BY p.id")]
