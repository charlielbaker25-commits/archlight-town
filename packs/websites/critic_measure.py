"""What the Critic's plain code measures, and the hard checks no model can overrule.

Code measures, Opus judges. Before any model looks at a site, this file serves the built site on
127.0.0.1 and collects:
- full-page screenshots at 375, 768 and 1440 px wide, cut into screen-sized slices (one very tall
  image would be shrunk until its text can't be read),
- axe-core's accessibility results (axe runs from the pinned local copy in tools/axe/),
- Lighthouse on a phone (or the Builder's own run, if it measured exactly these files),
- all of the browser checks above for every page of the site, not just the first (Example Web Studio's
  homepage has a second page, /privacy/; its results go in "other_pages"),
- sideways scrolling at 375 px, broken internal links and #anchors, console errors, the size of the
  call button, phone numbers that break onto two lines (at every width), graphics that overlap text
  (at every width), the reduced-motion check, and the facts check.

The browser can't reach the internet: Chrome is started with a rule that makes every hostname but
127.0.0.1 fail to resolve, and Playwright aborts any request that leaves this laptop (and records it).

hard_failures() turns the measurements into plain-word failures. Any one fails the build without a
model call, the same principle as the Guard: a model can add a fail, never clear one.
"""
import hashlib
import math
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Sequence
from urllib.parse import unquote, urlparse

from playwright.sync_api import Error as PlaywrightError, sync_playwright

from packs.websites.builder_check import facts_check, motion_problems, reduced_motion_check, serve_folder
from packs.websites.inspector_measure import better_lighthouse, run_lighthouse

REPO_ROOT = Path(__file__).resolve().parents[2]
AXE_JS = REPO_ROOT / "tools" / "axe" / "node_modules" / "axe-core" / "axe.min.js"
AXE_INSTALL_HINT = "npm ci --prefix tools/axe"
# Every hostname except this laptop resolves to nothing, for both Playwright's and Lighthouse's Chrome.
OFFLINE_RULE = "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1"
LIGHTHOUSE_OFFLINE_FLAG = '--host-resolver-rules="MAP * ~NOTFOUND, EXCLUDE 127.0.0.1"'
# Screen widths (px) and the height of one screenshot slice at each: a phone, a tablet, a laptop.
SCREENS = {375: 812, 768: 1024, 1440: 900}
MAX_SLICES = 8
SETTLE_MS = 1800            # let entrance animations finish so screenshots show the finished page
AXE_FAILING_IMPACTS = ("serious", "critical")
LighthouseRunner = Callable[[str, str], dict[str, Any]]

# Finds every phone number on the page and reports the ones the browser broke across two lines.
# Text is read per block (a paragraph, a button), joining its inline pieces, so a number split over
# <span>s is still one number. A number is wrapped when one piece's middle sits below another piece's
# bottom edge (big numbers often have a tight line height, so the two lines' boxes overlap).
PHONE_WRAPS_JS = r"""() => {
  const phone = /\(?\d{3}\)?[\s.\-\u2011]*\d{3}[\s.\-\u2011]*\d{4}/g;
  const blockOf = node => {
    let el = node.parentElement;
    while (el && el !== document.body && getComputedStyle(el).display.startsWith('inline')) el = el.parentElement;
    return el;
  };
  const groups = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const block = blockOf(node);
    const last = groups[groups.length - 1];
    if (last && last.block === block) last.nodes.push(node); else groups.push({block, nodes: [node]});
  }
  const wraps = [];
  for (const group of groups) {
    const starts = [];
    let text = '';
    for (const node of group.nodes) { starts.push(text.length); text += node.data; }
    const at = offset => {
      let i = starts.length - 1;
      while (i > 0 && starts[i] > offset) i--;
      return [group.nodes[i], offset - starts[i]];
    };
    for (const match of text.matchAll(phone)) {
      const range = document.createRange();
      range.setStart(...at(match.index));
      range.setEnd(...at(match.index + match[0].length - 1));
      range.setEnd(range.endContainer, range.endOffset + 1);
      const rects = [...range.getClientRects()].filter(r => r.width > 0 && r.height > 0);
      if (rects.some(a => rects.some(b => (b.top + b.bottom) / 2 > a.bottom))) wraps.push(match[0].replace(/\s+/g, ' ').trim());
    }
  }
  return wraps;
}"""


# Graphics on top of or behind text (Charlie, 2026-10-26: any graphic overlapping text, at any width, is a
# must-fix). For each line of visible text, a few points along its middle are checked with
# elementsFromPoint, which lists everything painted at a point, top to bottom. A hit on a graphic that
# isn't part of that text is an overlap. SVG is hit-tested on its painted shapes (path, circle...), never on
# the <svg> box, so the empty corners of a drawing don't count; images, canvas and video count by their box.
# pointer-events is switched on for the test (decorations often switch it off), graphics that are invisible
# (opacity under 5%) don't count, and graphics inside fixed or sticky bars are skipped (content scrolls
# under a sticky header by design). The page is scrolled a screen at a time, because elementsFromPoint only
# sees what's on screen.
# Faint watermarks (Charlie, 2026-10-26): a graphic BEHIND the text that is marked decorative (aria-hidden,
# empty alt, role presentation/none) and fainter than critic.decorative_max_opacity (limits.yaml) is allowed;
# the text's contrast is still a hard check through axe-core. "Fainter" is the paint actually shown: CSS
# opacity of it and every parent, times the fill/stroke opacity and the color's own alpha. Anything on top of
# the text, not marked decorative, or more visible than that still fails (like build #3's flower).
OVERLAP_JS = r"""async (maxDecorativeOpacity) => {
  const style = document.createElement('style');
  style.textContent = '*, *::before, *::after { pointer-events: auto !important; }';
  document.head.appendChild(style);
  const SHAPES = new Set(['path','circle','ellipse','rect','line','polyline','polygon','text','tspan','use','image']);
  const BOXES = new Set(['img','canvas','video']);
  const pinned = el => { for (let e = el; e && e !== document.body; e = e.parentElement) {
    const p = getComputedStyle(e).position; if (p === 'fixed' || p === 'sticky') return true; } return false; };
  const opacity = el => { let o = 1; for (let e = el; e && e.nodeType === 1; e = e.parentElement)
    o *= parseFloat(getComputedStyle(e).opacity || '1'); return o; };
  const svgOf = el => { let top = null; for (let e = el; e && e.nodeType === 1; e = e.parentElement)
    if (e.tagName.toLowerCase() === 'svg') top = e; return top; };
  const name = el => { const t = el.tagName.toLowerCase();
    const label = el.getAttribute('aria-label') || el.getAttribute('alt') || el.id ||
      (el.getAttribute('class') || '').split(/\s+/).slice(0, 2).join('.') ||
      (el.getAttribute('src') || '').split('/').pop().slice(0, 40);
    return label ? `${t} (${label})` : t; };
  const alpha = color => { const m = /rgba?\(([^)]+)\)/.exec(color || '');
    if (!m) return 1; const parts = m[1].split(/[\s,\/]+/).filter(Boolean);
    return parts.length > 3 ? parseFloat(parts[3]) * (parts[3].endsWith('%') ? 0.01 : 1) : 1; };
  // How visible the hit's paint is: an SVG shape shows its fill or stroke, whichever is stronger.
  const shown = hitEl => { const cs = getComputedStyle(hitEl); let paint = 1;
    if (SHAPES.has(hitEl.tagName.toLowerCase()) && hitEl.tagName.toLowerCase() !== 'image') {
      const fill = cs.fill === 'none' ? 0 : parseFloat(cs.fillOpacity || '1') * alpha(cs.fill);
      const stroke = cs.stroke === 'none' ? 0 : parseFloat(cs.strokeOpacity || '1') * alpha(cs.stroke);
      paint = Math.max(fill, stroke); }
    return opacity(hitEl) * paint; };
  const decorative = el => { if (el.tagName.toLowerCase() === 'img' && el.getAttribute('alt') === '') return true;
    for (let e = el; e && e.nodeType === 1; e = e.parentElement) {
      if (e.getAttribute('aria-hidden') === 'true') return true;
      const role = e.getAttribute('role'); if (role === 'presentation' || role === 'none') return true; }
    return false; };
  const graphicAt = (hitEl, textEl) => {
    if (hitEl === textEl || hitEl.contains(textEl)) return null;
    const tag = hitEl.tagName.toLowerCase();
    let graphic = null;
    if (BOXES.has(tag)) graphic = hitEl;
    else if (SHAPES.has(tag)) graphic = svgOf(hitEl);
    if (!graphic || graphic.contains(textEl) || pinned(graphic) || opacity(hitEl) < 0.05) return null;
    return graphic;
  };
  const lines = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const el = node.parentElement;
    if (!el || !node.data.trim() || el.closest('svg, script, style, noscript, template') || pinned(el)) continue;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || opacity(el) < 0.05) continue;
    const range = document.createRange(); range.selectNodeContents(node);
    for (const r of range.getClientRects()) {
      if (r.width < 4 || r.height < 4) continue;
      lines.push({el, text: node.data.trim().replace(/\s+/g, ' ').slice(0, 50),
                  top: r.top + scrollY, left: r.left, width: r.width, height: r.height});
    }
  }
  const found = new Map(), allowed = new Map();
  const total = document.documentElement.scrollHeight;
  for (let y = 0; y < total; y += innerHeight) {
    scrollTo(0, y);
    await new Promise(done => setTimeout(done, 120));
    for (const line of lines) {
      const docMid = line.top + line.height / 2;      // each line is checked in the one step that covers it
      if (docMid < y || docMid >= y + innerHeight) continue;
      const mid = docMid - scrollY;
      if (mid < 0 || mid >= innerHeight) continue;
      for (const f of [0.1, 0.3, 0.5, 0.7, 0.9]) {
        const x = line.left + line.width * f;
        if (x < 0 || x >= innerWidth) continue;
        let behind = false;     // elementsFromPoint lists top to bottom: after the text = behind it
        for (const hit of document.elementsFromPoint(x, mid)) {
          if (hit === line.el) { behind = true; continue; }
          const graphic = graphicAt(hit, line.el);
          if (!graphic) continue;
          const seen = Math.round(shown(hit) * 1000) / 1000;
          const faint = behind && decorative(graphic) && seen < maxDecorativeOpacity;
          const key = line.text + '|' + name(graphic);
          if (!faint) found.set(key, {text: line.text, graphic: name(graphic), opacity: seen, behind,
                                      decorative: decorative(graphic)});
          else if (!found.has(key)) allowed.set(key, {text: line.text, graphic: name(graphic), opacity: seen});
          break;
        }
      }
    }
  }
  style.remove();
  scrollTo(0, 0);
  for (const key of found.keys()) allowed.delete(key);
  return {overlaps: [...found.values()].slice(0, 20), faint: [...allowed.values()].slice(0, 20)};
}"""


# Sample sections (samples.py): every one must SHOW its tag. A tag that is missing, hidden, see-through or too
# small to read fails, so a sample can't pass as the business's own content. The booking note's tag lives in
# a popover that opens from its button, so there the button must be visible instead.
SAMPLE_TAGS_JS = r"""() => {
  const visible = el => { if (!el) return false; const r = el.getBoundingClientRect(); const cs = getComputedStyle(el);
    let o = 1; for (let e = el; e && e.nodeType === 1; e = e.parentElement) o *= parseFloat(getComputedStyle(e).opacity || '1');
    return r.width >= 20 && r.height >= 8 && cs.visibility !== 'hidden' && cs.display !== 'none' && o >= 0.6
      && parseFloat(cs.fontSize) >= 10; };
  const hidden = [];
  for (const section of document.querySelectorAll('[data-sample]')) {
    const kind = section.getAttribute('data-sample');
    const tag = section.querySelector('[data-sample-tag]');
    if (tag && tag.closest('[popover]')) {
      const opener = section.querySelector('[popovertarget]');
      if (!opener || !visible(opener)) hidden.push(kind + ' (its button is hidden)');
      continue;
    }
    if (!visible(tag)) hidden.push(kind);
  }
  return hidden;
}"""
# Scroll-triggered entrance animations only run as a visitor scrolls, so the page is scrolled to the bottom and
# back (as a visitor would) before the screenshots, or every section below the first screen would be blank.
# 'instant' because a site with smooth scrolling would otherwise still be gliding when the next step starts.
SCROLL_THROUGH_JS = r"""async () => {
  const step = Math.max(200, Math.floor(innerHeight / 2));
  for (let y = 0; y < document.documentElement.scrollHeight; y += step) {
    scrollTo({top: y, behavior: 'instant'}); await new Promise(done => setTimeout(done, 90)); }
  scrollTo({top: 0, behavior: 'instant'});
}"""


# A full-page capture makes Chrome lay the page out again, which restarts CSS animations: a fade-in was caught on
# its first, blank frame (Playwright's animations="disabled" doesn't survive that). So once the page has settled,
# every element with a finished-length animation keeps the look it has NOW (its opacity and transform written on
# the element itself) and its animation is switched off. Endless animations (a rolling wave) are left alone.
FREEZE_ANIMATIONS_JS = r"""() => {
  const done = new Set();
  for (const a of document.getAnimations()) {
    const el = a.effect && a.effect.target;
    const timing = a.effect && a.effect.getComputedTiming ? a.effect.getComputedTiming() : {};
    if (!el || done.has(el) || !isFinite(timing.endTime) || !(el instanceof Element)) continue;
    done.add(el);
    const cs = getComputedStyle(el);
    el.style.setProperty('opacity', cs.opacity, 'important');
    el.style.setProperty('transform', cs.transform, 'important');
    el.style.setProperty('animation', 'none', 'important');
  }
}"""


def overlap_check(browser: Any, url: str, width: int, height: int, blocked: list[str],
                  max_decorative_opacity: float) -> dict[str, list[dict[str, Any]]]:
    """Graphics painted on top of or behind text at one screen width (see OVERLAP_JS): "overlaps" fail the
    build; "faint" are the decorative watermarks allowed behind text (shown to the Critic, never a fail).
    Reduced motion is on, so entrance animations show their finished state."""
    context = _offline_context(browser, width, height, blocked, reduced_motion="reduce")
    try:
        page = context.new_page()
        page.goto(url, wait_until="load", timeout=30_000)
        page.wait_for_timeout(SETTLE_MS)
        return page.evaluate(OVERLAP_JS, max_decorative_opacity)
    finally:
        context.close()


def dist_hash(dist: Path) -> str:
    """SHA-256 over every file's path and bytes. Any change to the built site changes it."""
    dist = Path(dist)
    digest = hashlib.sha256()
    for path in sorted(p for p in dist.rglob("*") if p.is_file()):
        digest.update(path.relative_to(dist).as_posix().encode("utf-8") + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()


# ---------------- internal links (read from the files; no browser needed) ----------------

class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.refs: list[str] = []
        self.ids: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        for key in ("id", "name") if tag == "a" else ("id",):
            if values.get(key):
                self.ids.add(values[key] or "")
        for key in ("href", "src"):
            if values.get(key):
                self.refs.append(values[key] or "")
        for part in (values.get("srcset") or "").split(","):
            if part.strip():
                self.refs.append(part.split()[0])


def _parse(page: Path) -> _Links:
    parser = _Links()
    parser.feed(page.read_text(encoding="utf-8", errors="replace"))
    return parser


def link_check(dist: Path) -> dict[str, Any]:
    """Every link and file a page points at inside the site exists, and every #anchor has its target.
    Outside links are listed, never visited (nothing here goes online)."""
    dist = Path(dist).resolve()
    pages = {p: _parse(p) for p in sorted(dist.rglob("*.html"))}
    broken, outside = [], []
    for page, parsed in pages.items():
        name = page.relative_to(dist).as_posix()
        for ref in parsed.refs:
            parts = urlparse(ref)
            if parts.scheme in ("tel", "mailto", "data", "sms") or ref.startswith("javascript:"):
                continue
            if parts.scheme or ref.startswith("//"):
                if parts.hostname not in ("127.0.0.1", "localhost"):
                    outside.append(ref)
                continue
            target = page if not parts.path else _resolve(dist, page, unquote(parts.path))
            if target is None or not target.is_file():
                broken.append(f"{name}: {ref} points at a file that doesn't exist")
                continue
            if parts.fragment and target.suffix == ".html":
                ids = pages[target].ids if target in pages else _parse(target).ids
                if parts.fragment not in ids:
                    broken.append(f"{name}: {ref} jumps to #{parts.fragment}, which isn't on the page")
    return {"broken": sorted(set(broken)), "outside_links": sorted(set(outside))}


def _resolve(dist: Path, page: Path, path: str) -> Path | None:
    base = dist if path.startswith("/") else page.parent
    target = (base / path.lstrip("/")).resolve()
    if dist not in target.parents and target != dist:
        return None   # points outside the site folder
    return target / "index.html" if target.is_dir() else target


# ---------------- the browser ----------------

def _offline_context(browser: Any, width: int, height: int, blocked: list[str], **options: Any) -> Any:
    context = browser.new_context(viewport={"width": width, "height": height}, **options)

    def only_this_laptop(route: Any) -> None:
        url = route.request.url
        if url.startswith("data:") or urlparse(url).hostname == "127.0.0.1":
            route.continue_()
        else:
            blocked.append(url)
            route.abort()

    context.route("**/*", only_this_laptop)
    return context


def _screenshots(context: Any, url: str, width: int, height: int, folder: Path,
                 console: list[str], prefix: str = "") -> dict[str, Any]:
    page = context.new_page()
    page.on("console", lambda m: console.append(f"{width}px: {m.text}"[:300]) if m.type == "error" else None)
    page.on("pageerror", lambda e: console.append(f"{width}px: {e}"[:300]))
    page.goto(url, wait_until="load", timeout=30_000)
    page.evaluate(SCROLL_THROUGH_JS)
    page.wait_for_timeout(SETTLE_MS)
    page.evaluate(FREEZE_ANIMATIONS_JS)
    total = int(page.evaluate("() => document.documentElement.scrollHeight"))
    count = min(MAX_SLICES, max(1, math.ceil(total / height)))
    files = []
    for n in range(count):
        top = n * height
        path = folder / f"{prefix}{width}px-{n + 1:02d}.png"
        page.screenshot(path=str(path), full_page=True,
                        clip={"x": 0, "y": top, "width": width, "height": min(height, total - top)})
        files.append(path.name)
    info: dict[str, Any] = {"page_height": total, "slices": files, "cut_off": total > MAX_SLICES * height,
                            "phone_wraps": page.evaluate(PHONE_WRAPS_JS),
                            "hidden_sample_tags": page.evaluate(SAMPLE_TAGS_JS)}
    if width == 375:
        size = page.evaluate("() => ({width: document.documentElement.scrollWidth, window: innerWidth})")
        info["width"] = size["width"]
        info["overflows"] = size["width"] > size["window"]
        info["call_buttons"] = page.evaluate("""() => [...document.querySelectorAll('a[href^="tel:"]')]
          .filter(a => a.getClientRects().length > 0).map(a => {
            const r = a.getBoundingClientRect();
            return {text: a.innerText.trim().slice(0, 40), width: Math.round(r.width), height: Math.round(r.height),
                    top: Math.round(r.top + scrollY), on_first_screen: r.top + scrollY < innerHeight};
          })""")
    page.close()
    return info


def _axe(context: Any, url: str) -> dict[str, Any]:
    if not AXE_JS.is_file():
        return {"error": f"axe-core isn't installed ({AXE_INSTALL_HINT})"}
    page = context.new_page()
    page.goto(url, wait_until="load", timeout=30_000)
    page.add_script_tag(path=str(AXE_JS))   # injected from the local file: nothing is fetched
    result = page.evaluate("async () => await axe.run(document, {resultTypes: ['violations']})")
    page.close()
    violations = [{"id": v["id"], "impact": v.get("impact"), "help": v.get("help"), "count": len(v.get("nodes", [])),
                   "where": [", ".join(map(str, n.get("target", []))) for n in v.get("nodes", [])[:3]]}
                  for v in result.get("violations", [])]
    return {"version": result.get("testEngine", {}).get("version"), "violations": violations}


def other_pages(dist: Path) -> dict[str, str]:
    """Every built page but the first, as {url path: file}: privacy/index.html -> "privacy/"."""
    dist = Path(dist)
    found = {}
    for page in sorted(dist.rglob("*.html")):
        name = page.relative_to(dist).as_posix()
        if name != "index.html":
            found[name.removesuffix("index.html")] = name
    return found


def _measure_page(browser: Any, url: str, screenshots_dir: Path, prefix: str, blocked: list[str],
                  console: list[str], max_decorative_opacity: float) -> dict[str, Any]:
    """The browser checks for one page: screenshots, phone wraps and overlaps at every width, the 375px
    width and call buttons, axe-core and reduced motion."""
    out: dict[str, Any] = {"screens": {}}
    for width, height in SCREENS.items():
        scale = 2 if width == 375 else 1   # a phone screen is sharp: 2x, like a real phone
        context = _offline_context(browser, width, height, blocked, device_scale_factor=scale)
        out["screens"][str(width)] = _screenshots(context, url, width, height, screenshots_dir, console, prefix)
        context.close()
        found = overlap_check(browser, url, width, height, blocked, max_decorative_opacity)
        out["screens"][str(width)]["overlaps"] = found["overlaps"]
        out["screens"][str(width)]["faint_decorations"] = found["faint"]
    context = _offline_context(browser, 375, 812, blocked, reduced_motion="reduce")
    out["axe"] = _axe(context, url)
    context.close()
    out["reduced_motion"] = reduced_motion_check(browser, url)
    return out


def measure_for_review(dist: Path, screenshots_dir: Path, lead: dict[str, Any], own_words: str,
                       stale_source: bool, lighthouse_timeout_s: float, min_performance: float,
                       min_accessibility: float, reused_lighthouse: dict[str, Any] | None = None,
                       lighthouse: LighthouseRunner | None = None, homepage: bool = False,
                       *, max_decorative_opacity: float, privacy: Sequence[str] = ()) -> dict[str, Any]:
    """Everything the Critic's code measures about one built site. Never raises for a broken site:
    a measurement that couldn't run is recorded as an error (and hard_failures() fails it).
    The first page's results sit at the top level; any other page's go in other_pages[its path]."""
    screenshots_dir.mkdir(parents=True, exist_ok=True)
    out: dict[str, Any] = {"facts_check": facts_check(dist, lead, own_words, stale_source, homepage=homepage,
                                                      privacy=privacy),
                           "links": link_check(dist)}
    extra = other_pages(dist)
    if extra:
        out["other_pages"] = {path: {} for path in extra}
    blocked: list[str] = []
    console: list[str] = []
    chrome = ""
    with serve_folder(dist) as url:
        try:
            with sync_playwright() as playwright:
                chrome = playwright.chromium.executable_path
                browser = playwright.chromium.launch(headless=True, args=[OFFLINE_RULE])
                try:
                    out.update(_measure_page(browser, url, screenshots_dir, "", blocked, console,
                                             max_decorative_opacity))
                    for path in extra:
                        prefix = path.strip("/").replace("/", "-") + "-"
                        out["other_pages"][path] = _measure_page(browser, url + path, screenshots_dir, prefix,
                                                                 blocked, console, max_decorative_opacity)
                finally:
                    browser.close()
        except PlaywrightError as err:
            out["error"] = str(err).splitlines()[0][:300]
        out["console_errors"] = console[:20]
        out["outside_requests_blocked"] = sorted(set(blocked))
        run = lighthouse or (lambda u, c: run_lighthouse(u, c, lighthouse_timeout_s, LIGHTHOUSE_OFFLINE_FLAG))
        if reused_lighthouse and reused_lighthouse.get("performance") is not None:
            out["lighthouse"] = {**reused_lighthouse, "reused_from_build": True}
        elif chrome or lighthouse:
            out["lighthouse"] = _lighthouse(run, url, chrome, min_performance, min_accessibility)
        if chrome or lighthouse:
            for path in extra:   # the Builder's own Lighthouse run only measured the first page
                out["other_pages"][path]["lighthouse"] = _lighthouse(run, url + path, chrome, min_performance,
                                                                     min_accessibility)
    return out


def _lighthouse(run: LighthouseRunner, url: str, chrome: str, min_performance: float,
                min_accessibility: float) -> dict[str, Any]:
    first = run(url, chrome)
    if not _under_cutoffs(first, min_performance, min_accessibility):
        return first
    # Same rule as the Inspector's "slow": one bad run isn't proof. A second run decides.
    second = run(url, chrome)
    best = first if _under_cutoffs(second, min_performance, min_accessibility) else second
    return {**best, "runs": 2}


def _under_cutoffs(lighthouse: dict[str, Any], min_performance: float, min_accessibility: float) -> bool:
    perf, a11y = lighthouse.get("performance"), lighthouse.get("accessibility")
    return perf is None or a11y is None or perf < min_performance or a11y < min_accessibility


def hard_failures(m: dict[str, Any], min_performance: float, min_accessibility: float) -> list[str]:
    """The plain-code checks, in plain words. Any one fails the build; no model can clear it.
    Fail closed: a check that couldn't run counts as failed. Every page of the site is held to the
    same browser checks; a failure on another page says which one ("On /privacy/: ...")."""
    fails: list[str] = []
    if m.get("error"):
        fails.append(f"The site couldn't be measured in the browser ({m['error']}).")
    facts = m.get("facts_check") or {}
    if not facts.get("passed"):
        fails += [f"Facts check: {p}" for p in facts.get("problems") or ["didn't run"]]
    for url in m.get("outside_requests_blocked") or []:
        fails.append(f"Loads a file from outside the site ({url}); everything must be self-hosted.")
    measured = not m.get("error")
    fails += _page_failures(m, min_performance, min_accessibility, measured)
    fails += [f"Broken internal link: {b}." for b in (m.get("links") or {}).get("broken") or []]
    fails += [f"Reduced motion: {p}." for p in motion_problems(m)]
    for path, page in (m.get("other_pages") or {}).items():
        fails += [f"On /{path}: {f}" for f in _page_failures(page, min_performance, min_accessibility, measured)]
        fails += [f"On /{path}: Reduced motion: {p}." for p in motion_problems({**page, "error": m.get("error")})]
    return list(dict.fromkeys(fails))


def _page_failures(m: dict[str, Any], min_performance: float, min_accessibility: float,
                   measured: bool) -> list[str]:
    """One page's Lighthouse, axe-core, 375px width, phone-wrap and overlap checks. `measured` is False
    when the browser couldn't run at all (already its own failure, so the missing parts aren't repeated)."""
    fails: list[str] = []
    lighthouse = m.get("lighthouse") or {"error": "didn't run"}
    if lighthouse.get("error"):
        fails.append(f"Lighthouse couldn't score the site ({lighthouse['error']}).")
    elif _under_cutoffs(lighthouse, min_performance, min_accessibility):
        fails.append(f"Lighthouse on a phone: performance {lighthouse.get('performance')} (need {min_performance:g}), "
                     f"accessibility {lighthouse.get('accessibility')} (need {min_accessibility:g}).")
    axe = m.get("axe") or {"error": "didn't run"}
    if axe.get("error"):
        fails.append(f"The accessibility check (axe-core) couldn't run ({axe['error']}).")
    for v in axe.get("violations") or []:
        if v.get("impact") in AXE_FAILING_IMPACTS:
            fails.append(f"Accessibility ({v['impact']}): {v['help']} [{v['id']}] at {'; '.join(v['where'])}.")
    screens = m.get("screens") or {}
    phone = screens.get("375") or {}
    if phone.get("overflows") is not False:
        fails.append(f"Scrolls sideways on a 375px phone, or couldn't be measured (page is {phone.get('width')}px wide).")
    for width in SCREENS:
        wraps = (screens.get(str(width)) or {}).get("phone_wraps")
        if wraps is None:
            if measured:
                fails.append(f"Couldn't check whether phone numbers stay on one line at {width}px.")
            continue
        fails += [f"The phone number \"{w}\" breaks onto two lines at {width}px; it must stay on one line."
                  for w in wraps]
    for width in SCREENS:
        fails += [f"The {kind} sample section's tag isn't visible at {width}px; every sample must show its tag "
                  f"(\"Sample: ...\") clearly." for kind in (screens.get(str(width)) or {}).get("hidden_sample_tags") or []]
    for width in SCREENS:
        overlaps = (screens.get(str(width)) or {}).get("overlaps")
        if overlaps is None:
            if measured:
                fails.append(f"Couldn't check whether any graphic overlaps text at {width}px.")
            continue
        fails += [f"A graphic ({o['graphic']}) overlaps the text \"{o['text']}\" at {width}px. No graphic may sit "
                  f"on top of or behind text at any width; move or shrink it." for o in overlaps]
    return fails
