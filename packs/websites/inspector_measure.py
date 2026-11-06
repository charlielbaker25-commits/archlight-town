"""The Inspector's measuring tools: plain code that looks at a website and writes down facts.

Nothing here judges or scores. It loads the homepage once in a real browser (Playwright's
own Chromium), takes a desktop and a 375px phone-width screenshot, reads what the page says
about itself (viewport tag, forms, links, copyright year, old-fashioned tags), checks a few
internal links, and runs Lighthouse once for the mobile performance score. inspector.py turns
the facts into problem categories.

Two ways to reach a site:
- LiveAccess: the real internet, politely. Only http/https, never an address inside a
  private network (so a bad listing can't point the browser at this laptop), a normal
  timeout, and no clicks, form submits or downloads.
- FixtureAccess: a test-set case. Every request is answered from the case's own frozen
  copy through resolve_case_url(), so nothing ever reaches the internet (the offline rule).
"""
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import urldefrag, urlparse

from playwright.sync_api import Browser, BrowserContext, Error as PlaywrightError, Route, sync_playwright

REPO_ROOT = Path(__file__).resolve().parents[2]
LIGHTHOUSE_CLI = REPO_ROOT / "tools" / "lighthouse" / "node_modules" / "lighthouse" / "cli" / "index.js"
# An honest browser name, so a site owner reading their logs can see who looked.
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/140.0 Safari/537.36 showcaseexampleSiteCheck/1.0")
DESKTOP = {"width": 1366, "height": 900}
PHONE = {"width": 375, "height": 812}
# Statuses that mean "this link points at nothing". 401/403/429 usually mean "no robots",
# so those count as "couldn't check", never as broken.
BROKEN_STATUSES = {404, 410} | set(range(500, 600))
MAX_RESPONSES_WEIGHED = 300

# What the page says about itself, read in the browser after it loads.
PAGE_FACTS_JS = """() => {
  const vp = document.querySelector('meta[name="viewport" i]');
  const forms = [...document.forms].map(f => {
    try { return new URL(f.getAttribute('action') || '', location.href).href; } catch (e) { return ''; }
  });
  const imgs = [...document.images].filter(i => i.complete && i.naturalWidth === 0 && i.currentSrc)
                                    .map(i => i.currentSrc);
  const flash = [...document.querySelectorAll('object, embed')]
                  .some(e => /\\.swf|shockwave/i.test(e.outerHTML));
  return {
    viewport: vp ? (vp.getAttribute('content') || '') : null,
    form_actions: forms,
    links: [...document.querySelectorAll('a[href]')].map(a => a.href),
    broken_images: imgs,
    text: document.body ? document.body.innerText : '',
    tags: {
      marquee: document.querySelectorAll('marquee').length,
      blink: document.querySelectorAll('blink').length,
      font: document.querySelectorAll('font').length,
      frames: document.querySelectorAll('frameset, frame').length,
      flash: flash ? 1 : 0,
    },
  };
}"""
# The browser's own count of bytes the page downloaded (0 for some cross-site files it isn't told about).
PAGE_BYTES_JS = """() => performance.getEntriesByType('navigation').concat(performance.getEntriesByType('resource'))
                       .reduce((sum, e) => sum + (e.transferSize || e.encodedBodySize || 0), 0)"""
OVERFLOW_JS = "() => ({width: document.documentElement.scrollWidth, window: window.innerWidth})"
COPYRIGHT_YEAR = re.compile(r"(?:©|\(c\)|copyright)\s*(?:©\s*)?(?:\d{4}\s*[-–]\s*)?((?:19|20)\d{2})", re.I)
BEST_VIEWED = re.compile(r"best viewed (?:in|with|at)", re.I)
# Words too common in business names to prove a page is about this business.
GENERIC_NAME_WORDS = {"the", "and", "inc", "llc", "ltd", "co", "company", "corp", "pc", "dds", "dmd", "md", "of",
                      "services", "service", "agency", "insurance", "agent", "group", "shop", "store", "studio"}


class UnsafeURL(ValueError):
    """Not http/https, or it points inside a private network."""


class DomainNotFound(ValueError):
    """The website's domain doesn't exist (DNS has no address for it)."""


@dataclass(frozen=True)
class MeasureSettings:
    max_pages: int
    page_timeout_s: float
    lighthouse_timeout_s: float
    run_lighthouse: bool = True
    # Given one Lighthouse result, says whether it reads "slow". When it does, Lighthouse runs a second
    # time, because "your site is slow" is a claim to a prospect and real sites vary run to run.
    reads_slow: Callable[[dict[str, Any]], bool] | None = None


# ---------------- reaching a site ----------------

def same_site(a: str, b: str) -> bool:
    host = lambda u: (urlparse(u).hostname or "").lower().removeprefix("www.")
    return host(a) == host(b) != ""


def check_public_url(url: str) -> None:
    """Refuse anything that isn't a public http(s) address. Raises DomainNotFound if DNS has no answer."""
    parts = urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise UnsafeURL(f"refused {url}: only http and https websites are inspected")
    host = parts.hostname.lower()
    if host == "localhost" or host.endswith((".local", ".internal", ".localhost")):
        raise UnsafeURL(f"refused {url}: a private address")
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except socket.gaierror as err:
        raise DomainNotFound(f"{host} doesn't exist ({err})") from err
    for address in addresses:
        if not ipaddress.ip_address(address.split("%", 1)[0]).is_global:
            raise UnsafeURL(f"refused {url}: {host} points inside a private network")


class LiveAccess:
    """The real internet, with every page load (including redirects) checked by check_public_url."""

    def __init__(self) -> None:
        self._checked: dict[str, str | None] = {}   # host -> refusal reason (None = fine)

    def check(self, url: str) -> None:
        check_public_url(url)

    def prepare(self, context: BrowserContext) -> None:
        def guard(route: Route) -> None:
            request = route.request
            if request.is_navigation_request():
                host = urlparse(request.url).hostname or ""
                if host not in self._checked:
                    try:
                        check_public_url(request.url)
                        self._checked[host] = None
                    except (UnsafeURL, DomainNotFound) as err:
                        self._checked[host] = str(err)
                if self._checked[host]:
                    route.abort("blockedbyclient")
                    return
            route.continue_()
        context.route("**/*", guard)

    def link_status(self, context: BrowserContext, url: str, timeout_s: float) -> int | None:
        """A plain GET of the link (HTML only, no images or scripts). None = couldn't check; 0 = unreachable."""
        try:
            check_public_url(url)
        except UnsafeURL:
            return None
        except DomainNotFound:
            return 0
        try:
            response = context.request.get(url, timeout=timeout_s * 1000, max_redirects=5,
                                           fail_on_status_code=False)
            status = response.status
            response.dispose()
            return status
        except PlaywrightError:
            return 0

    @contextmanager
    def lighthouse_url(self, url: str) -> Iterator[str]:
        yield url


class FixtureAccess:
    """A test-set case: every request is answered from the case's own site/ folder."""

    def __init__(self, case: Any, resolve: Callable[[Any, str], Path], offline_error: type[Exception]) -> None:
        self.case = case
        self.resolve = resolve
        self.offline_error = offline_error

    def check(self, url: str) -> None:
        self.resolve(self.case, url)   # raises if the URL isn't the case's own

    def prepare(self, context: BrowserContext) -> None:
        def answer(route: Route) -> None:
            try:
                path = self.resolve(self.case, route.request.url)
            except self.offline_error:
                route.abort("blockedbyclient")
                return
            if path.is_file():
                route.fulfill(path=str(path), status=200)
            else:
                route.fulfill(status=404, body="Not found", content_type="text/plain")
        context.route("**/*", answer)

    def link_status(self, context: BrowserContext, url: str, timeout_s: float) -> int | None:
        try:
            return 200 if self.resolve(self.case, url).is_file() else 404
        except self.offline_error:
            return None

    @contextmanager
    def lighthouse_url(self, url: str) -> Iterator[str]:
        """Lighthouse runs its own browser, so it gets a tiny server on this laptop only
        (127.0.0.1) that answers every path through resolve_case_url()."""
        access = self
        website = str(self.case.info.get("website") or "").rstrip("/")

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 (the name http.server expects)
                try:
                    path = access.resolve(access.case, website + self.path)
                except access.offline_error:
                    path = None
                if path is None or not path.is_file():
                    self.send_error(404)
                    return
                body = path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", _content_type(path))
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:
                pass

        server = QuietServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_address[1]}/"
        finally:
            server.shutdown()
            server.server_close()


class QuietServer(ThreadingHTTPServer):
    def handle_error(self, request: Any, client_address: Any) -> None:
        pass   # Lighthouse hangs up abruptly when it's done; that's not an error worth printing


def _content_type(path: Path) -> str:
    types = {".html": "text/html; charset=utf-8", ".css": "text/css", ".svg": "image/svg+xml",
             ".bmp": "image/bmp", ".png": "image/png", ".jpg": "image/jpeg", ".js": "text/javascript"}
    return types.get(path.suffix.lower(), "application/octet-stream")


# ---------------- Lighthouse ----------------

def run_lighthouse(url: str, chrome_path: str, timeout_s: float, extra_chrome_flags: str = "") -> dict[str, Any]:
    """One Lighthouse run (mobile, the default). Returns the scores, or {"error": ...}.
    `extra_chrome_flags` lets the Critic start Chrome so it can't reach anything but this laptop."""
    node = shutil.which("node")
    if not node or not LIGHTHOUSE_CLI.exists():
        return {"error": "Lighthouse isn't installed (npm install --prefix tools/lighthouse)"}
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    env["CHROME_PATH"] = chrome_path
    command = [node, str(LIGHTHOUSE_CLI), url, "--output=json", "--output-path=stdout", "--quiet",
               "--only-categories=performance,accessibility,best-practices,seo",
               f"--chrome-flags=--headless=new --disable-gpu --no-first-run {extra_chrome_flags}".strip()]
    try:
        done = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=timeout_s,
                              env=env, check=False)
    except subprocess.TimeoutExpired:
        return {"error": f"Lighthouse took longer than {timeout_s:g}s"}
    except OSError as err:
        return {"error": f"Lighthouse couldn't start: {err}"}
    try:
        report = json.loads(done.stdout)
    except json.JSONDecodeError:
        return {"error": f"Lighthouse failed: {(done.stderr or '').strip()[-300:]}"}
    if report.get("runtimeError"):
        return {"error": f"Lighthouse: {report['runtimeError'].get('message', 'runtime error')}"}
    categories = report.get("categories", {})
    audits = report.get("audits", {})
    score = lambda key: (round(categories[key]["score"] * 100)
                         if categories.get(key, {}).get("score") is not None else None)
    lcp_ms = audits.get("largest-contentful-paint", {}).get("numericValue")
    return {"performance": score("performance"), "accessibility": score("accessibility"),
            "best_practices": score("best-practices"), "seo": score("seo"),
            # Largest Contentful Paint: when the biggest image or text block shows up on a phone.
            "lcp_seconds": None if lcp_ms is None else round(lcp_ms / 1000, 1),
            "total_kb": round(audits.get("total-byte-weight", {}).get("numericValue", 0) / 1024, 1)}


def better_lighthouse(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """The run that shows the site in the best light: highest performance, then quickest main content.
    A run that failed is only used when no run worked."""
    worked = [r for r in runs if r.get("performance") is not None]
    if not worked:
        return runs[-1]
    return max(worked, key=lambda r: (r["performance"], -(r.get("lcp_seconds") or 0)))


# ---------------- the browser ----------------

class SiteMeasurer:
    """One browser for a whole Inspector run. Use it as `with SiteMeasurer(settings) as m:`."""

    def __init__(self, settings: MeasureSettings, check: Callable[[], None] = lambda: None) -> None:
        self.settings = settings
        self.check = check   # the kill switch: called before every fetch
        self._playwright: Any = None
        self.browser: Browser | None = None

    def __enter__(self) -> "SiteMeasurer":
        self._playwright = sync_playwright().start()
        self.browser = self._playwright.chromium.launch(headless=True)
        return self

    def __exit__(self, *exc: Any) -> None:
        if self.browser:
            self.browser.close()
        if self._playwright:
            self._playwright.stop()

    def screenshots_only(self, url: str, access: Any, shots: dict[str, Path] | None) -> dict[str, Any]:
        """For free-builder pages: just the 'before' pictures (the builder's code isn't the owner's problem)."""
        facts: dict[str, Any] = {"fetched": False}
        if not shots:
            return facts
        try:
            access.check(url)
            context = self._context(access, ignore_https_errors=False)
            try:
                page = context.new_page()
                self.check()
                page.goto(url, wait_until="load", timeout=self.settings.page_timeout_s * 1000)
                self._screenshots(page, shots, facts)
                facts["fetched"] = True
            finally:
                context.close()
        except (PlaywrightError, UnsafeURL, DomainNotFound) as err:
            facts["screenshot_error"] = str(err).splitlines()[0][:300]
        return facts

    def measure(self, url: str, access: Any, shots: dict[str, Path] | None,
                identity: dict[str, str] | None = None) -> dict[str, Any]:
        """Everything about one site's homepage, as plain facts. Raises UnsafeURL / DomainNotFound.
        `identity` (the business's name and phone) checks the page is really about this business."""
        access.check(url)
        facts: dict[str, Any] = {"fetched": False, "listed_url": url}
        self._identity = identity
        try:
            self._load_and_read(url, access, shots, facts, ignore_https_errors=False)
        except PlaywrightError as err:
            if "ERR_CERT" not in str(err):
                facts["error"] = str(err).splitlines()[0][:300]
                return facts
            # A broken certificate: browsers show a full-page warning. That's a fact; then look past it.
            facts["certificate_error"] = str(err).splitlines()[0][:200]
            try:
                self._load_and_read(url, access, shots, facts, ignore_https_errors=True)
            except PlaywrightError as err2:
                facts["error"] = str(err2).splitlines()[0][:300]
                return facts
        if self.settings.run_lighthouse and facts.get("fetched"):
            runs = [self._lighthouse(access, facts["final_url"])]
            if self.settings.reads_slow and self.settings.reads_slow(runs[0]):
                runs.append(self._lighthouse(access, facts["final_url"]))   # confirm before calling it slow
            facts["lighthouse_runs"] = runs
            facts["lighthouse"] = better_lighthouse(runs)
        return facts

    def _lighthouse(self, access: Any, url: str) -> dict[str, Any]:
        self.check()
        with access.lighthouse_url(url) as lighthouse_url:
            return run_lighthouse(lighthouse_url, self._playwright.chromium.executable_path,
                                  self.settings.lighthouse_timeout_s)

    def _context(self, access: Any, ignore_https_errors: bool) -> BrowserContext:
        assert self.browser is not None
        context = self.browser.new_context(viewport=DESKTOP, user_agent=USER_AGENT, accept_downloads=False,
                                           service_workers="block", ignore_https_errors=ignore_https_errors)
        access.prepare(context)
        return context

    def _load_and_read(self, url: str, access: Any, shots: dict[str, Path] | None, facts: dict[str, Any],
                       ignore_https_errors: bool) -> None:
        context = self._context(access, ignore_https_errors)
        try:
            page = context.new_page()
            page.on("dialog", lambda dialog: dialog.dismiss())   # never answer a pop-up
            responses: list[Any] = []
            failed: list[str] = []
            page.on("response", lambda r: responses.append(r) if len(responses) < MAX_RESPONSES_WEIGHED else None)
            # A request the browser cancelled (ERR_ABORTED: trackers, beacons, a video that never played)
            # isn't a broken file; anything else that failed is.
            page.on("requestfailed", lambda r: failed.append(r.url)
                    if "ERR_ABORTED" not in (r.failure or "") else None)
            self.check()
            response = page.goto(url, wait_until="load", timeout=self.settings.page_timeout_s * 1000)
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except PlaywrightError:
                pass   # a busy page (chat widgets, trackers) never goes quiet; what loaded is enough
            facts.update(fetched=True, status=response.status if response else None, final_url=page.url,
                         redirects=_redirect_chain(response))
            read = page.evaluate(PAGE_FACTS_JS)
            self._screenshots(page, shots, facts)
            facts.update(_page_facts(read, page.url))
            facts["mentions_business"] = mentions_business(read["text"], getattr(self, "_identity", None))
            facts["broken_resources"] = _broken_resources(responses, failed, read["broken_images"], page.url)
            facts["page_kb"] = _weight_kb(responses, page.evaluate(PAGE_BYTES_JS))
            self._check_links(context, access, facts)
        finally:
            context.close()

    def _screenshots(self, page: Any, shots: dict[str, Path] | None, facts: dict[str, Any]) -> None:
        """Desktop first, then the same page at phone width: the overflow check and the 'before' pictures."""
        if shots:
            shots["desktop"].parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(shots["desktop"]))
        page.set_viewport_size(PHONE)
        page.wait_for_timeout(300)   # let the layout settle at the new width
        widths = page.evaluate(OVERFLOW_JS)
        facts["width_at_375"] = widths["width"]
        facts["overflows_at_375"] = widths["width"] > widths["window"] + 2
        if shots:
            page.screenshot(path=str(shots["mobile"]))
            facts["screenshots"] = {name: str(path) for name, path in shots.items()}

    def _check_links(self, context: BrowserContext, access: Any, facts: dict[str, Any]) -> None:
        checked, broken, unchecked = [], [], []
        for link in facts.pop("internal_links")[: max(self.settings.max_pages - 1, 0)]:
            self.check()
            status = access.link_status(context, link, self.settings.page_timeout_s)
            checked.append({"url": link, "status": status})
            if status is None or status in (401, 403, 429):
                unchecked.append(link)
            elif status == 0 or status in BROKEN_STATUSES:
                broken.append(link)
        facts.update(links_checked=checked, broken_links=broken, unchecked_links=unchecked)


def _redirect_chain(response: Any) -> list[str]:
    chain, request = [], response.request.redirected_from if response else None
    while request:
        chain.append(request.url)
        request = request.redirected_from
    return list(reversed(chain))


def _page_facts(read: dict[str, Any], page_url: str) -> dict[str, Any]:
    years = [int(y) for y in COPYRIGHT_YEAR.findall(read["text"])]
    markers = [name for name, count in read["tags"].items() if count]
    if BEST_VIEWED.search(read["text"]):
        markers.append("best_viewed_in")
    internal: list[str] = []
    for link in read["links"]:
        link = urldefrag(link)[0]
        if (urlparse(link).scheme in ("http", "https") and same_site(link, page_url) and not urlparse(link).query
                and link.rstrip("/") != page_url.rstrip("/") and link not in internal):
            internal.append(link)   # links with ?query=... are skipped: they can trigger actions
    return {
        "viewport_tag": read["viewport"],
        "http_form_actions": sorted({a for a in read["form_actions"] if a.startswith("http://")}),
        "copyright_year": max(years) if years else None,
        "dated_markers": markers,
        "internal_links": internal,
        "outside_links": sum(1 for link in read["links"]
                             if urlparse(link).scheme in ("http", "https") and not same_site(link, page_url)),
    }


def _broken_resources(responses: list[Any], failed: list[str], broken_images: list[str], page_url: str) -> list[str]:
    """Images and files on the business's own site that point at nothing. Other sites' files (ads,
    analytics, video players, browser-extension probes) aren't the owner's broken links, and the
    browser's own favicon guess doesn't count either."""
    found = [r.url for r in responses if r.status in BROKEN_STATUSES and r.url != page_url] + failed + broken_images
    unique: list[str] = []
    for url in found:
        if (urlparse(url).scheme in ("http", "https") and same_site(url, page_url)
                and not urlparse(url).path.endswith("/favicon.ico") and url not in unique):
            unique.append(url)
    return unique


def mentions_business(text: str, identity: dict[str, str] | None) -> bool | None:
    """Is this page about the business? Its full name, its phone number, or every distinctive word
    of its name appears on the page. None when there's nothing to check against.
    A listed website that forwards to a parked-domain ad page fails this."""
    if not identity:
        return None
    plain = lambda t: re.sub(r"[^a-z0-9]", "", (t or "").lower())
    name, page = identity.get("name") or "", text or ""
    if plain(name) and plain(name) in plain(page):
        return True
    phone = re.sub(r"\D", "", identity.get("phone") or "")[-10:]
    if len(phone) == 10 and phone in re.sub(r"\D", "", page):
        return True
    words = {w for w in re.findall(r"[a-z0-9]+", name.lower()) if len(w) >= 3 and w not in GENERIC_NAME_WORDS}
    page_words = set(re.findall(r"[a-z0-9]+", page.lower()))
    return bool(words) and words <= page_words


def _weight_kb(responses: list[Any], browser_bytes: float) -> float:
    """The page's weight, from each response's Content-Length header and the browser's own count,
    whichever is larger. Never by reading response bodies: a tracker that keeps a request open
    would make that wait forever (it did, on a real site)."""
    from_headers = 0
    for response in responses:
        try:
            from_headers += int(response.headers.get("content-length") or 0)
        except ValueError:
            pass   # a malformed header counts as unknown
    return round(max(from_headers, browser_bytes or 0) / 1024, 1)
