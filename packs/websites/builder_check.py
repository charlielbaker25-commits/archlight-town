"""The Builder's plain-code checks. No model judges anything here.

1. scan_sources(): BEFORE any build runs. An Astro build executes code in the site's pages, so code
   reads every file first: the locked files are unchanged, only page/style/image files exist, and
   nothing imports outside packages, reaches the network, reads the environment or runs programs.
2. facts_check(): AFTER the build, on the finished HTML (visible text plus alt/title/aria/meta text).
   The phone, address and email match the lead exactly, no banned claim appears (unless the
   business says it itself), there are no forms, and the noindex tag and preview note are there.
   Tagged sample sections (samples.py) must carry their tag word for word, sample photos must be
   labeled "Sample photo", and inside a sample section only the word "reviews" is let through.
3. measure_site(): serves the site on 127.0.0.1 only, then checks it at 375px with Playwright
   (sideways scrolling, any request leaving this laptop) and runs Lighthouse once.
"""
import hashlib
import re
import threading
from contextlib import contextmanager
from html.parser import HTMLParser
from http.server import SimpleHTTPRequestHandler
from pathlib import Path
from typing import Any, Iterator, Sequence
from urllib.parse import urlparse

from playwright.sync_api import Error as PlaywrightError, sync_playwright

from packs.websites.inspector_measure import PHONE, QuietServer, run_lighthouse
from packs.websites.leads import find_emails, mailto_address
from packs.websites.photo_library import SAMPLE_PHOTO_LABEL, SAMPLE_PREFIX
from packs.websites.samples import ALLOWED_IN_SAMPLES, SAMPLE_TAGS

LOCKED_FILES = ("package.json", "astro.config.mjs")
SKIPPED_DIRS = {"node_modules", "dist", ".astro"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".avif", ".gif", ".svg", ".ico"}
SOURCE_EXTENSIONS = {".astro", ".css"} | IMAGE_EXTENSIONS
# Files Builder may leave at the top of its workspace (notes are never built into the site).
ROOT_FILES = set(LOCKED_FILES) | {"brief.json"}
# Imports a page or stylesheet may use: its own files, Astro's built-ins, Tailwind and the installed fonts.
ALLOWED_IMPORT = re.compile(r"^(\.{1,2}/|astro:|astro/|tailwindcss$|@fontsource(-variable)?/)")
IMPORT = re.compile(r"""(?:^|[\s;])import\s+(?:[^'";]+?\s+from\s+)?['"]([^'"]+)['"]|@import\s+['"]([^'"]+)['"]""")
FORBIDDEN_CODE = {
    r"\bprocess\b": "reads the environment",
    r"child_process|\bspawn\(|\bexec\(": "runs a program",
    r"\brequire\s*\(": "loads code with require()",
    r"\bimport\s*\(": "loads code at run time",
    r"\beval\s*\(|new\s+Function\b": "runs text as code",
    r"\bfetch\s*\(|XMLHttpRequest|WebSocket|sendBeacon|EventSource": "reaches the network",
    r"\bnode:|\bfrom\s+['\"](?:fs|path|os|http|https|net)['\"]": "uses Node's system modules",
    r"set:html": "inserts raw HTML",
    r"document\.cookie|localStorage|sessionStorage": "stores data in the visitor's browser",
    r"<script[^>]*\bsrc\s*=": "loads an outside script",
    r"<(?:iframe|object|embed)\b": "embeds another site",
    r"url\(\s*['\"]?(?:https?:)?//": "loads a file from outside the site",
    r"<link[^>]+href\s*=\s*['\"](?:https?:)?//": "loads a file from outside the site",
}

# Claims Builder may never invent. Each is allowed only if the same words appear in the lead or on the
# business's own homepage (then it's their own public claim, not ours).
BANNED_CLAIMS = {
    r"★|⭐|\b(?:five|5)[- ]star\b|\b\d(?:\.\d)?\s*(?:/\s*5|out of 5|stars?)\b": "a star rating",
    r"\bratings?\b|\brated\b": "a rating",
    r"\breviews\b|\btestimonials?\b": "reviews or testimonials",
    r"\bawards?\b|\baward[- ]winning\b": "an award",
    r"\b(?:since|established|est\.?)\s+(?:in\s+)?(?:19|20)\d\d\b": "a founding year",
    r"\b\d+\+?\s+years\b": "a number of years",
    r"#\s*1\b|\bnumber one\b|\bno\.\s*1\b|\bbest in\b": "a #1 / best-in claim",
    r"\$\s?\d|\b\d+\s?(?:dollars|% off)\b": "a price",
    r"\blicensed\b|\binsured\b|\bbonded\b": "licensed / insured / bonded",
    r"\bcertified\b|\baccredited\b|\bboard[- ]certified\b": "a certification",
    r"\bguarantee[ds]?\b|\bwarrant(?:y|ies)\b|\bsatisfaction\b": "a guarantee",
    r"\bfree\s+(?:estimates?|consultations?|quotes?|exams?)\b": "a free offer",
    r"\btrusted by\b|\bvoted\b": "an endorsement",
}
# A person's name after a title must come from the business's own data ("Dr. Patel" only if they say so).
TITLED_NAME = re.compile(r"\b(?:Dr|Mr|Mrs|Ms|Mx)\.?\s+([A-Z][a-z]+)")
PHONE_LIKE = re.compile(r"(?:\+?1[\s.\-]?)?\(?\b\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}\b")
EMAIL_LIKE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
FORM_TAGS = re.compile(r"<(?:form|input|textarea|select)\b", re.IGNORECASE)
NOINDEX = re.compile(r"<meta[^>]+name=[\"']?robots[\"']?[^>]+noindex", re.IGNORECASE)
PREVIEW_NOTE = "preview made by example web studio for"
PRIVACY_PAGE = "privacy/index.html"
# Opening hours. Every time of day on the site must be one the business publishes on its own homepage,
# and "open Saturdays"-style claims need published hours too. Hours copied from a stale site must say
# to call and confirm them.
# (?<![\d:]) rather than \b: page text can run a day straight into a time ("Thu8:00").
TIME_OF_DAY = re.compile(r"(?<![\d:])(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s?m\b\.?|(?<![\d:])(\d{1,2}):(\d{2})(?!\d)",
                         re.IGNORECASE)
DAY_CLAIM = re.compile(
    r"\b(?:open|closed)\s+(?:on\s+|every\s+)?(?:mon|tues|wednes|thurs|fri|satur|sun)(?:day)?s?\b"
    r"|\b(?:mon|tues|wednes|thurs|fri|satur|sun)(?:day)?s?\s+(?:hours|appointments)\b"
    r"|\bopen\s+(?:7|seven)\s+days\b|\b24/7\b|\bopen\s+24\s+hours\b|\bopen\s+late\b"
    r"|\b(?:evening|weekend|holiday)\s+(?:hours|appointments)\b", re.IGNORECASE)
CONFIRM_HOURS = re.compile(r"\bcall\b[^.]{0,40}\bconfirm|\bconfirm\b[^.]{0,30}\bhours\b", re.IGNORECASE)


# ---------------- 1. before the build ----------------

def file_hashes(folder: Path) -> dict[str, str]:
    return {name: hashlib.sha256((Path(folder) / name).read_bytes()).hexdigest() for name in LOCKED_FILES}


def scan_sources(workspace: Path, template_hashes: dict[str, str]) -> list[str]:
    """Every rule the workspace breaks, in plain words. Empty means it's safe to build."""
    workspace = Path(workspace)
    problems = []
    for name, digest in template_hashes.items():
        path = workspace / name
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            problems.append(f"{name} was changed (it's locked)")
    for path in sorted(workspace.rglob("*")):
        relative = path.relative_to(workspace)
        if relative.parts[0] in SKIPPED_DIRS or relative.parts[0] == ".claude" or not path.is_file():
            continue
        where = relative.as_posix()
        if len(relative.parts) == 1:
            if where not in ROOT_FILES and path.suffix.lower() != ".md":
                problems.append(f"{where}: unexpected file at the top of the site")
            continue
        if relative.parts[0] == "public":
            # public/ files land at the site's root, and every published site shares showcase.example.com's
            # root (previews.py), so they could clash. Images go in src/assets/ (Astro names them uniquely).
            problems.append(f"{where}: nothing may go in public/; put images in src/assets/")
            continue
        if relative.parts[0] != "src" or path.suffix.lower() not in SOURCE_EXTENSIONS:
            problems.append(f"{where}: only .astro, .css and image files are allowed in src/")
            continue
        if path.suffix.lower() in (".astro", ".css", ".svg"):
            problems += [f"{where}: {p}" for p in _code_problems(path.read_text(encoding="utf-8", errors="replace"))]
    return problems


def _code_problems(text: str) -> list[str]:
    found = []
    for match in IMPORT.finditer(text):
        module = match.group(1) or match.group(2)
        if not ALLOWED_IMPORT.match(module):
            found.append(f"imports '{module}' (only the site's own files, Astro, Tailwind and the installed fonts)")
    for pattern, why in FORBIDDEN_CODE.items():
        if re.search(pattern, text, re.IGNORECASE):
            found.append(why)
    return found


# ---------------- 2. after the build ----------------

VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


class _PageText(HTMLParser):
    """Visible text plus the words in alt/title/aria-label/meta content, and every link.

    It also keeps track of where each piece of text sits: inside a tagged sample section
    (data-sample="<kind>", see samples.py) or not, and inside that section's tag (data-sample-tag) or not.
    Sample photos (file names starting "sample-photo-") are recorded with the text of the nearest
    element marked data-sample-photo around them, which must say "Sample photo"."""
    ATTRS = ("alt", "title", "aria-label", "content", "placeholder")
    BREAKS = ("br", "p", "div", "li", "address", "h1", "h2", "h3", "h4", "h5", "h6", "section", "footer",
              "header", "main", "nav", "span", "a", "dt", "dd", "td", "th", "tr", "time", "strong", "b", "em",
              "figure", "figcaption", "button", "summary", "article", "aside", "small", "ol", "ul")

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []              # every word on the page
        self.outside: list[str] = []            # words NOT in a tagged sample section
        self.links: list[str] = []
        self.samples: list[dict[str, Any]] = [] # {"kind", "parts", "tag_parts"}
        self.photo_boxes: list[list[str]] = []  # the text of each data-sample-photo element
        self.sample_photos: list[dict[str, Any]] = []   # {"src", "box"}
        # Open elements: (tag, sample index, inside the tag element?, photo box index).
        self._stack: list[tuple[str, int | None, bool, int | None]] = []
        self._skip = 0

    def _where(self) -> tuple[int | None, bool, int | None]:
        return self._stack[-1][1:] if self._stack else (None, False, None)

    def _add(self, text: str) -> None:
        sample, in_tag, box = self._where()
        self.parts.append(text)
        if sample is None:
            self.outside.append(text)
        else:
            self.samples[sample]["parts"].append(text)
            if in_tag:
                self.samples[sample]["tag_parts"].append(text)
        if box is not None:
            self.photo_boxes[box].append(text)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag in ("script", "style"):
            self._skip += 1
        sample, in_tag, box = self._where()
        if "data-sample" in values:
            self.samples.append({"kind": (values.get("data-sample") or "").strip(), "parts": [], "tag_parts": []})
            sample, in_tag = len(self.samples) - 1, False
        if "data-sample-tag" in values:
            in_tag = True
        if "data-sample-photo" in values:
            self.photo_boxes.append([])
            box = len(self.photo_boxes) - 1
        if tag not in VOID_TAGS:
            self._stack.append((tag, sample, in_tag, box))
        for name, value in attrs:
            # The viewport tag's "width=device-width" is markup, not words a visitor reads.
            if value and name in self.ATTRS and not (tag == "meta" and "width=" in value):
                self._add(f" {value} ")
            if value and name == "href":
                self.links.append(value)
        if tag in ("img", "source"):
            for src in [values.get("src") or ""] + [p.split()[0] for p in (values.get("srcset") or "").split(",")
                                                    if p.strip()]:
                if Path(urlparse(src).path).name.lower().startswith(SAMPLE_PREFIX):
                    self.sample_photos.append({"src": src, "box": box})
        if tag in self.BREAKS:
            self._add(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        elif tag in self.BREAKS:
            self._add(" ")
        if any(open_tag == tag for open_tag, *_ in self._stack):
            while self._stack and self._stack.pop()[0] != tag:
                pass

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self._add(data)


def _joined(parts: list[str]) -> str:
    return " ".join("".join(parts).split())


def page_text(html: str) -> tuple[str, list[str]]:
    parser = _PageText()
    parser.feed(html)
    return _joined(parser.parts), parser.links


def sample_problems(name: str, parser: "_PageText") -> list[str]:
    """Every tagged sample section carries its tag word for word, and every sample photo is labeled."""
    problems = []
    for sample in parser.samples:
        kind = sample["kind"]
        if kind not in SAMPLE_TAGS:
            problems.append(f"{name}: a sample section of an unknown kind (data-sample=\"{kind}\"); the kinds are "
                            f"{', '.join(SAMPLE_TAGS)}")
            continue
        if f" {plain_words(SAMPLE_TAGS[kind])} " not in f" {plain_words(_joined(sample['tag_parts']))} ":
            problems.append(f"{name}: the {kind} sample section is missing its tag \"{SAMPLE_TAGS[kind]}\" (word for "
                            f"word, in an element marked data-sample-tag)")
    for photo in parser.sample_photos:
        box = photo["box"]
        label = _joined(parser.photo_boxes[box]) if box is not None else ""
        if plain_words(SAMPLE_PHOTO_LABEL) not in plain_words(label):
            problems.append(f"{name}: a sample photo ({Path(urlparse(photo['src']).path).name}) isn't labeled "
                            f"\"{SAMPLE_PHOTO_LABEL}\" (put it in an element marked data-sample-photo with that label)")
    return problems


def digits(phone: str | None) -> str:
    only = re.sub(r"\D", "", phone or "")
    return only[1:] if len(only) == 11 and only.startswith("1") else only


def plain_words(text: str | None) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).split())


def times_of_day(text: str) -> set[tuple[int, int]]:
    """'8:00 AM - 5 p.m.' -> {(8, 0), (5, 0)}. Hours are read on a 12-hour clock, so '17:00' = '5:00'."""
    found = set()
    for m in TIME_OF_DAY.finditer(text or ""):
        hour, minute = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(5))
        if int(hour) <= 24 and int(minute or 0) < 60:
            found.add((int(hour) % 12, int(minute or 0)))
    return found


def hours_problems(name: str, text: str, own_words: str, stale_source: bool) -> list[str]:
    """Opening hours may only come from the business's own homepage (BLUEPRINT: never invent facts)."""
    published = times_of_day(own_words)
    problems = []
    shown = times_of_day(text)
    invented = sorted(shown - published)
    if invented:
        problems.append(f"{name}: shows opening times their homepage doesn't publish "
                        f"({', '.join(f'{h or 12}:{m:02d}' for h, m in invented)})")
    if not published:
        for match in DAY_CLAIM.finditer(text):
            problems.append(f"{name}: an opening-days claim with no published hours (\"{_around(text, match)}\")")
    if shown and stale_source and not CONFIRM_HOURS.search(text):
        problems.append(f"{name}: hours copied from an outdated site must say to call to confirm them")
    return problems


def facts_check(dist: Path, lead: dict[str, Any], own_words: str = "", stale_source: bool = False,
                homepage: bool = False, privacy: Sequence[str] = ()) -> dict[str, Any]:
    """Checks every built page against the lead. `own_words` is the business's own homepage text:
    a claim they make themselves is allowed. `stale_source` means the Inspector found their site
    outdated, so hours taken from it need "call to confirm". `homepage` is Example Web Studio's own homepage
    (archlight_home.yaml is its lead and own words): it must be indexable, carry no preview note and
    show its email. `privacy` is the homepage's privacy page text (archlight_home.yaml): the page must
    exist at privacy/index.html with every line word for word, and the two pages must link to each
    other. Returns {"passed": bool, "problems": [...]}."""
    dist = Path(dist)
    pages = sorted(dist.rglob("*.html"))
    if not pages:
        return {"passed": False, "problems": ["the build produced no pages"]}
    allowed = " ".join(str(lead.get(k) or "") for k in ("business_name", "business_type", "area", "address",
                                                        "phone", "public_email", "website")) + " " + own_words
    allowed_plain = f" {plain_words(allowed)} "
    lead_phone, lead_email = digits(lead.get("phone")), (lead.get("public_email") or "").strip().lower()
    problems: list[str] = []
    all_text = []
    kinds_found: set[str] = set()
    for page in pages:
        name = page.relative_to(dist).as_posix()
        html = page.read_text(encoding="utf-8", errors="replace")
        parser = _PageText()
        parser.feed(html)
        text, links = _joined(parser.parts), parser.links
        outside_text = _joined(parser.outside)
        sample_text = " ".join(_joined(s["parts"]) for s in parser.samples)
        all_text.append(text)
        kinds_found.update(s["kind"] for s in parser.samples if s["kind"] in SAMPLE_TAGS)
        problems += sample_problems(name, parser)
        if homepage and NOINDEX.search(html):
            problems.append(f"{name}: has a noindex tag (Example Web Studio's homepage must be findable in search engines)")
        elif not homepage and not NOINDEX.search(html):
            problems.append(f"{name}: no noindex tag (previews must stay out of search engines)")
        if FORM_TAGS.search(html):
            problems.append(f"{name}: has a form or input field (contact is phone and email only)")
        for found in PHONE_LIKE.findall(text) + [l[4:] for l in links if l.lower().startswith("tel:")]:
            if digits(found) != lead_phone:
                problems.append(f"{name}: phone {found.strip()} isn't the business's ({lead.get('phone') or 'none'})")
        # Whole addresses only (mailto: decoded), never a fragment: "lead@example.com" is one address, not
        # "splace01@gmail.com", and its mailto: form "fictional%27splace01@gmail.com" is the same address.
        for found in find_emails(text) + [mailto_address(l) for l in links if l.lower().startswith("mailto:")]:
            if found.lower() != lead_email:
                problems.append(f"{name}: email {found} isn't the business's ({lead_email or 'none listed'})")
        # Untagged text: every banned claim fails unless the business says it itself. Tagged sample text: the
        # same, except the word "reviews" (the reviews sample has to say what goes there). Never ratings,
        # stars, prices, years or anything else, tagged or not.
        for where, what_text, skip in (("", outside_text, ()), (" (in a sample section)", sample_text,
                                                                 ALLOWED_IN_SAMPLES)):
            for pattern, what in BANNED_CLAIMS.items():
                if what in skip:
                    continue
                for match in re.finditer(pattern, what_text, re.IGNORECASE):
                    words = plain_words(match.group(0))
                    if not words or f" {words} " not in allowed_plain:
                        problems.append(f"{name}: {what}{where} (\"{_around(what_text, match)}\")")
        for match in TITLED_NAME.finditer(text):
            if f" {match.group(1).lower()} " not in allowed_plain:
                problems.append(f"{name}: names a person ({match.group(0)}) who isn't in the business's data")
        problems += hours_problems(name, text, own_words, stale_source)
    everything = " ".join(all_text)
    if lead_phone and lead_phone not in "".join(digits(p) for p in PHONE_LIKE.findall(everything)):
        problems.append(f"the business's phone ({lead.get('phone')}) isn't on the site")
    if lead.get("address") and f" {plain_words(lead['address'])} " not in f" {plain_words(everything)} ":
        problems.append(f"the business's address isn't on the site exactly as listed ({lead['address']})")
    if homepage:
        if PREVIEW_NOTE in everything.lower():
            problems.append("the homepage carries a preview note, but it isn't a preview")
        if lead_email and lead_email not in everything.lower():
            problems.append(f"the contact email ({lead_email}) isn't on the homepage")
    elif PREVIEW_NOTE not in everything.lower():
        problems.append("the preview note (\"Preview made by Example Web Studio for ...\") is missing")
    if homepage and privacy:
        problems += privacy_page_problems(dist, privacy)
    return {"passed": not problems, "problems": list(dict.fromkeys(problems)),
            "sample_sections": sorted(kinds_found)}


# How the two pages may point at each other (Astro builds src/pages/privacy.astro as privacy/index.html).
LINKS_TO_PRIVACY = {"/privacy", "/privacy/", "/privacy/index.html", "privacy", "privacy/", "privacy/index.html"}
LINKS_TO_HOME = {"/", "/index.html", "../", "../index.html", ".."}


def privacy_page_problems(dist: Path, privacy: Sequence[str]) -> list[str]:
    """The privacy page exists, carries every line of Charlie's approved text word for word, and is
    linked from the homepage (and back). Compared as plain words, so markup, punctuation and curly
    quotes don't matter."""
    page = Path(dist) / PRIVACY_PAGE
    if not page.is_file():
        return [f"the privacy page is missing (src/pages/privacy.astro, built as {PRIVACY_PAGE})"]
    text, links = page_text(page.read_text(encoding="utf-8", errors="replace"))
    shown = f" {plain_words(text)} "
    problems = [f"{PRIVACY_PAGE}: doesn't have this line word for word: \"{line}\""
                for line in privacy if f" {plain_words(line)} " not in shown]
    _, home_links = page_text((Path(dist) / "index.html").read_text(encoding="utf-8", errors="replace"))
    if not LINKS_TO_PRIVACY & {_bare(l) for l in home_links}:
        problems.append("index.html: no link to the privacy page (href=\"/privacy/\") in the footer")
    if not LINKS_TO_HOME & {_bare(l) for l in links}:
        problems.append(f"{PRIVACY_PAGE}: no link back to the homepage (href=\"/\")")
    return problems


def _bare(link: str) -> str:
    return link.split("#")[0].split("?")[0].strip() or link.strip()


def _around(text: str, match: re.Match) -> str:
    return text[max(0, match.start() - 30):match.end() + 30].strip()


# ---------------- 3. measuring the built site ----------------

@contextmanager
def serve_folder(folder: Path, port: int = 0) -> Iterator[str]:
    """Serves a built site on 127.0.0.1 only (never reachable from another computer)."""
    folder = Path(folder)

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, directory=str(folder), **kwargs)

        def log_message(self, *args: Any) -> None:
            pass

    server = QuietServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/"
    finally:
        server.shutdown()
        server.server_close()


# Reduced motion: the browser is set to "reduce motion" (what a visitor's phone asks for when motion
# makes them ill), and these listeners record every CSS animation and every transition of a property
# that MOVES something (position, size, transform) that still runs. Color and opacity fades aren't
# motion, so Tailwind's everyday hover color changes don't count. Anything under 10 ms is a switched-off
# animation (a common reduced-motion reset uses 0.01 ms).
MOTION_LISTENERS = r"""
window.__motion = [];
const MOVES = /^(all|transform|translate|scale|rotate|top|left|right|bottom|inset|margin|padding|width|height|max-|min-|offset)/;
const label = el => !el || !el.tagName ? "page" : el.tagName.toLowerCase() + (el.id ? "#" + el.id : "")
  + (el.classList && el.classList.length ? "." + [...el.classList].slice(0, 2).join(".") : "");
const longest = value => Math.max(0, ...String(value).split(",").map(s => s.trim().endsWith("ms")
  ? parseFloat(s) / 1000 : parseFloat(s) || 0));
addEventListener("animationstart", e => {
  if (longest(getComputedStyle(e.target).animationDuration) > 0.01)
    window.__motion.push(label(e.target) + ': animation "' + e.animationName + '"');
}, true);
addEventListener("transitionrun", e => {
  if (MOVES.test(e.propertyName) && longest(getComputedStyle(e.target).transitionDuration) > 0.01)
    window.__motion.push(label(e.target) + ": transition of " + e.propertyName);
}, true);
"""
# Script-made animations (element.animate) fire no events, so they're read from the page itself.
MOTION_LEFTOVERS = r"""() => {
  const label = el => !el || !el.tagName ? "page" : el.tagName.toLowerCase() + (el.id ? "#" + el.id : "");
  const found = [];
  for (const a of document.getAnimations()) {
    const timing = a.effect && a.effect.getComputedTiming ? a.effect.getComputedTiming() : {};
    const scripted = !(window.CSSAnimation && a instanceof CSSAnimation)
      && !(window.CSSTransition && a instanceof CSSTransition);
    if (scripted && (timing.duration || 0) > 10) found.push(label(a.effect.target) + ": a script animation");
  }
  if (getComputedStyle(document.documentElement).scrollBehavior === "smooth") found.push("html: smooth scrolling");
  return found;
}"""
INTERACTIVE = "a[href], button, summary, [role=button], [tabindex]"
MAX_HOVERS = 40


def reduced_motion_check(browser: Any, url: str) -> dict[str, Any]:
    """Loads the page with "reduce motion" on, scrolls to the bottom, hovers and tabs through every
    link and button, and lists anything that still moved. Empty `moving` = it respects reduced motion."""
    context = browser.new_context(viewport=PHONE, reduced_motion="reduce")
    try:
        context.add_init_script(MOTION_LISTENERS)
        page = context.new_page()
        page.goto(url, wait_until="load", timeout=30_000)
        page.wait_for_timeout(300)
        height = page.evaluate("() => document.documentElement.scrollHeight")
        for y in range(0, int(height) + 400, 400):
            page.evaluate(f"() => window.scrollTo(0, {y})")
            page.wait_for_timeout(60)
        for element in page.locator(INTERACTIVE).all()[:MAX_HOVERS]:
            try:
                if element.is_visible():
                    element.hover(timeout=1000)
                    page.wait_for_timeout(40)
            except PlaywrightError:
                continue   # covered by another element or off-screen: nothing a visitor can hover either
        page.mouse.move(0, 0)
        for _ in range(MAX_HOVERS):
            page.keyboard.press("Tab")
        page.wait_for_timeout(100)
        moving = page.evaluate("() => window.__motion") + page.evaluate(MOTION_LEFTOVERS)
        return {"moving": sorted(set(moving))[:20]}
    finally:
        context.close()


def measure_site(dist: Path, lighthouse_timeout_s: float) -> dict[str, Any]:
    """375px overflow, requests that leave this laptop, reduced motion, and one Lighthouse run (mobile)."""
    out: dict[str, Any] = {}
    with serve_folder(dist) as url:
        try:
            with sync_playwright() as playwright:
                chrome = playwright.chromium.executable_path
                browser = playwright.chromium.launch(headless=True)
                try:
                    context = browser.new_context(viewport=PHONE)
                    outside: list[str] = []
                    context.on("request", lambda r: outside.append(r.url)
                               if r.url.startswith("http") and urlparse(r.url).hostname != "127.0.0.1" else None)
                    page = context.new_page()
                    page.goto(url, wait_until="load", timeout=30_000)
                    size = page.evaluate("() => ({width: document.documentElement.scrollWidth, window: innerWidth})")
                    out["width_at_375"] = size["width"]
                    out["overflows_at_375"] = size["width"] > size["window"]
                    out["outside_requests"] = sorted(set(outside))
                    context.close()
                    out["reduced_motion"] = reduced_motion_check(browser, url)
                finally:
                    browser.close()
            out["lighthouse"] = run_lighthouse(url, chrome, lighthouse_timeout_s)
        except PlaywrightError as err:
            out["error"] = str(err).splitlines()[0][:300]
    return out


def motion_problems(quality: dict[str, Any]) -> list[str]:
    """Fail closed: a reduced-motion check that didn't run counts as failed."""
    motion = quality.get("reduced_motion")
    if motion is None:
        return [f"the reduced-motion check couldn't run ({quality.get('error') or 'no result'})"]
    return [f"still moves with reduced motion on: {m}" for m in motion["moving"]]
