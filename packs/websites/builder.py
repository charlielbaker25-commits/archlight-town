"""The Builder: builds a one-page sample site for a build-worthy lead, in a private workspace folder.

How one build works:
1. Plain code makes a fresh workspace (<instance data>/builds/<label>-<time>/, never in git) from the
   ONE shared Astro + Tailwind template. The template's packages are installed once per laptop
   (`npm ci --prefix packs/websites/site_template`) and the workspace reaches them through a folder
   junction, so a build never reinstalls anything.
2. Plain code writes brief.json: only real public facts (see builder_content.py).
3. `claude -p` (Opus, from models.yaml, on Charlie's subscription, through engine/claude_cli.py)
   designs and writes the site with Anthropic's frontend-design skill. It may only read and edit
   files inside the workspace: no commands, no internet, and the locked files are denied.
4. Plain code scans every file (builder_check.scan_sources) and only then runs the Astro build itself.
   If either fails, Builder gets the errors back in the same session, up to `building.fix_rounds` times.
5. Plain code runs the facts check on the finished pages. A failed check is a failed build, with the reason.
6. Plain code measures the site at 375px and with Lighthouse, and saves every number (time, tokens,
   turns, skill use) in the `builds` table and the log.

Builder proposes nothing: publishing a preview is Session F, after the Critic (Session E).
It checks the kill switch throughout, refuses to start while a Claude plan usage-limit wait is in
force, and stops for the day at building.sites_per_day (limits.yaml).
"""
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from engine.claude_cli import CliError, CliResult, CliTimeout, SubscriptionLimitReached, clean_env
from engine.db import utc_now
from engine.events import log_event
from engine.killswitch import TownStopped
from engine.limits import Limits
from engine.town import Town
from packs.websites.builder_check import facts_check, file_hashes, measure_site, motion_problems, scan_sources
from packs.websites.builder_content import (Fetch, homepage_content, homepage_words, live_fetch, make_brief,
                                            make_homepage_brief, privacy_lines, write_brief)
from packs.websites.critic_measure import dist_hash
from packs.websites import photo_library
from packs.websites.inspector import KIND_OWN
from packs.websites.leads import HELD_WORDS, is_held
from packs.websites.instructions import agent_instructions
from packs.websites.scout import local_day_start_utc

AGENT = "builder"
PACK_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = PACK_DIR / "site_template"
PLUGIN_DIR = PACK_DIR / "builder_plugin"
TEMPLATE_PARTS = ("src", "public", "package.json", "astro.config.mjs")
ASTRO = Path("node_modules") / "astro" / "bin" / "astro.mjs"
INSTALL_HINT = "npm ci --prefix packs/websites/site_template"
# Builder's only tools: read and edit files in its workspace, and open its design skill.
TOOLS = ["Read", "Write", "Edit", "Glob", "Grep", "Skill"]
LOCKED = ("package.json", "astro.config.mjs", "brief.json", ".claude/**")
DENIED = [f"{tool}(./{name})" for name in LOCKED for tool in ("Edit", "Write")]
DESIGN_SKILL = "frontend-design"
ASTRO_BUILD_TIMEOUT_S = 300
ANSI = re.compile(r"\x1b\[[0-9;]*m")

JOB = """You are Builder for Example Web Studio, a small St. Louis web-design studio. You build a sample website (a
private preview) for a real local business, to show the owner what their new site could look like. It has to
be something the owner actually WANTS: rich, specific to their trade, and beautiful, never a thin page anyone
could put together. brief.json in this folder has everything you may say about the business.

The folder is an Astro 7 + Tailwind CSS 4 project:
- Write the site in src/: src/pages/index.astro, src/layouts/Base.astro, src/components/,
  src/styles/global.css (Tailwind v4: put design tokens in @theme there). SVGs and images go in
  src/assets/ only (or inline in the page). Leave public/ empty: every published site shares one root.
- package.json, astro.config.mjs and brief.json are locked. No other packages exist. Installed fonts
  (self-hosted, import in the layout like `import "@fontsource-variable/fraunces";`): {fonts}.
- Only .astro, .css and image files in src/. No .js or .ts files.
- Nothing loads from outside the site: no CDNs, Google Fonts, outside images, scripts, iframes or map
  embeds. A small inline <script> (say, a menu toggle) is fine if it fetches and stores nothing.
- You can't run commands. When you finish, the Town builds the site and sends you any errors.

What's already there: src/pages/index.astro is a STARTER page that builds a complete, honest preview from
brief.json (header, hero, services, the sample sections, a closing call band, footer), with components in
src/components/ and src/components/samples/. It is a floor, not the design. Rework it into THIS business's
own site: your own palette, type pairing, layout, hero, section order and drawings for this trade and its
corner of the St. Louis area. Restyle or rewrite any component; keep what the rules below require.

First open the frontend-design skill (Skill tool, "frontend-design") and follow it: plan a palette,
type and layout for THIS business, then build.

The full product, shown honestly (sample sections). A preview shows everything the finished site will have,
so the owner can picture it. Include ALL of these, each as a tagged sample (brief.json's sample_sections):
- a photo gallery (gallery), an "Our story" section (story: a styled placeholder saying the owner's story goes
  here, never an invented story), a reviews area (reviews: empty placeholder cards, NEVER a fake review,
  quote, name, star or rating), a Book online button that opens a "coming with your site" note (booking:
  the component uses the browser's popover; no form; its note says the finished site links the booking app the
  business already uses, or a free one we set up with them: keep that, and never name a booking app they use), a map (map: an original drawing beside the real
  address, with a directions link; no embed), and an hours table (hours) ONLY if the business doesn't
  publish its own hours (if it does, show their real hours instead, untagged).
- At tablet width (640-1023 px) keep it compact: the template's gallery shows three square tiles a row and its
  sections use tighter spacing there. Keep that (or tighter) in your design: no oversized empty tiles or tall empty
  bands at tablet width.
- Each sample section sits in an element marked data-sample="<kind>" and shows its tag, word for word, in a
  visible element marked data-sample-tag (the SampleTag component does this). Keep the tags small and clear.
- Inside a sample: placeholder words only ("Your hours", "Your photo"). Plain code fails ANY time of day,
  rating, star, price, year, name or claim inside a sample exactly as it would outside one.

Content rules. Plain code checks the finished page, and one slip fails the whole build:
- Facts come only from brief.json. Write the address exactly as address_exactly (you may break the line
  at a comma). Show phone_display and link it with phone_tel_link. Show public_email as a mailto: link
  only if there is one. No other phone numbers or email addresses anywhere.
- Never invent reviews, testimonials, ratings or stars, awards, founding years ("since 19xx"), years of
  experience, prices or offers ("free estimates"), staff or doctor names, certifications, "licensed",
  "insured" or "bonded", guarantees, "#1" or "best in" claims, "trusted by", opening hours or days,
  insurance plans, "same-day", "walk-ins" or areas served. If the business's own homepage text (in
  brief.json) says one of these, you may repeat it in their words. Outside a tagged sample section, the
  words "reviews" and "testimonials" count as a review claim too.
- Use the useful facts the business publishes in their_own_homepage: opening hours, the services they
  list, insurance or payment notes and the like. A visitor needs these, so leaving them out is a fault.
  Copy times exactly as they give them. If what_is_wrong_with_their_web_presence_now includes "outdated",
  their site may be stale: put "Please call to confirm hours" (or similar "call to confirm" wording)
  right next to the hours, and don't repeat dated offers or prices. Never add a fact they didn't publish.
  Marketing lines ("since 1998", slogans) and stale details (a discontinued plan, an old special) can
  be left out.
- Services: name a service ONLY if it is in business.core_services (what nearly every business of this
  type offers) or their own homepage names it. Nothing else, however ordinary it sounds: no
  specialties, brands or extras. If core_services is empty and they have no homepage, name no services;
  describe the business plainly so the owner can confirm or correct it.
- Photos: the business's own photos from brief.json come first (in src/assets/their-photos/; show them
  with Astro's <Image /> from "astro:assets"). Otherwise use ONLY the licensed sample photos listed in
  brief.json's sample_photos (in src/assets/sample-photos/), each inside an element marked
  data-sample-photo that visibly says "Sample photo" (a caption under it, never on top of it). No other
  photos, ever, and nothing that could pass for a picture of the actual business. With no photos, the
  gallery shows its styled tiles; carry the design with type, color, layout and original SVG drawings.
- No forms or input fields of any kind (no booking, contact or patient forms). Contact is the phone link
  and the email link.
- Keep Base.astro's head tags (noindex, viewport, lang) and keep <PreviewNote> with its exact words;
  restyle it to fit.
- You may name their town or neighborhood (from the address), but don't invent local facts.

Richness and design: a real header (name, jump links, call button) and a real footer (name, address,
phone, email, links); at least seven sections with real detail; a hero with presence (a bold type-led
composition, a rich gradient or an original drawing; never a photo that isn't theirs or a labeled
sample); copy written for this trade, not generic filler. The owner should look at it and want it.

Line breaks: a phone number must NEVER break across two lines at any width. Put every phone number in
an element with Tailwind's whitespace-nowrap, and size big ones so the whole number fits one line on a
375px phone. The Town measures at 375, 768 and 1440px and fails a site where a number wraps. Size the
business name and headlines so they break cleanly too (never mid-word or mid-name).

Graphics never touch text: no decorative shape, drawing, icon, pattern or photo may sit on top of or behind
any text at any width, however faint. Put decorations in empty space beside, above or below the text, and
check the tablet width (768px) too: shrink, move or hide a decoration there if it would reach the text.
The Town checks every line of text at 375, 768 and 1440px and fails a site where a graphic overlaps it.
(CSS background gradients behind text are fine; drawings and photos are not.)

Quality: mobile-first (right at 375px wide, never scrolls sideways), fast (Lighthouse 90+ on a phone: no
heavy effects; images through <Image />), accessible (semantic HTML, alt text, WCAG AA contrast, visible
focus), and clearly its own design, not a template.

Motion: subtle scroll animations are welcome (Base.astro already fades in anything marked class="reveal"
as it scrolls into view, only when motion is allowed). Every animation, transition and smooth scroll
must switch off for visitors whose device asks for less motion. Keep this rule in global.css:
@media (prefers-reduced-motion: reduce) {{ *, ::before, ::after {{ animation: none !important;
  transition: none !important; scroll-behavior: auto !important; }} }}
The Town opens the site with reduced motion on, hovers every link, and fails anything that still moves.

When you're done, reply in 2-3 plain sentences: the design direction, and what the owner should confirm."""

HOMEPAGE_JOB = """
THIS BUILD IS DIFFERENT: it is Example Web Studio's OWN homepage (showcase.example.com), not a preview for a
client. brief.json has "site": "homepage". Where this conflicts with anything above, this wins:
- The facts are brief.json's our_facts (Example Web Studio's own, approved by Charlie). Say only those. You
  may reword and arrange them, but add nothing: no clients, portfolio, testimonials, prices, numbers,
  results or extra promises. Follow every line in rules_for_this_page.
- Remove the noindex meta tag from Base.astro: this page must be findable in search engines. Remove
  <PreviewNote> from the layout: this page isn't a preview.
- No sample sections and no sample photos here: replace the starter page entirely (it's for client previews).
- Contact is the email link (mailto:). A phone number only if brief.json's business has phone_display (then
  show exactly that number, linked with phone_tel_link); otherwise no phone number at all. No street
  address, no forms.
- Who reads it: local business owners who got a preview link from us and want to check we're real.
  Make it warm, clear and credible for St. Louis, and clearly designed, not a template.
- Every other quality rule above still applies (375px, speed, accessibility, line breaks, motion).
- This build has TWO pages. The second is the privacy page, src/pages/privacy.astro (the Town serves it
  at /privacy/), built from brief.json's privacy_page. Use its title, "Last updated" date, intro,
  headings and paragraphs WORD FOR WORD, in that order: don't reword, shorten or add to them (plain
  code checks every line). Give it the same design as the homepage (the same layout, fonts, colors,
  header and footer), set as a calm, easy-to-read text page. It needs a link back to the homepage
  (href="/").
- The homepage's footer links to the privacy page with the word "Privacy" (href="/privacy/"). Keep the
  same footer on both pages. The privacy page is held to every rule above too (no noindex, no preview
  note, every width, Lighthouse, accessibility, graphics never touching text)."""

PROMPT = "Build the sample homepage for the business in brief.json. Follow your instructions exactly."
FIX_PROMPT = ("The Town couldn't accept the site yet. Fix every problem below in place, then reply in one "
              "sentence.\n\n{problems}")


class BuilderError(RuntimeError):
    pass


@dataclass(frozen=True)
class BuilderLimits:
    per_day: int
    timeout_s: float
    fix_rounds: int
    limit_wait_minutes: float
    min_performance: float
    min_accessibility: float
    lighthouse_timeout_s: float
    sample_photos: int = 0
    max_sample_photo_kb: float = 0

    @classmethod
    def read(cls, limits: Limits) -> "BuilderLimits":
        get = lambda key: limits.get(f"building.{key}")
        return cls(int(get("sites_per_day")), float(get("build_timeout_minutes")) * 60, int(get("fix_rounds")),
                   float(get("usage_limit_wait_minutes")), float(get("min_lighthouse_performance")),
                   float(get("min_lighthouse_accessibility")),
                   float(limits.get("inspector.lighthouse_timeout_seconds")),
                   int(get("sample_photos_per_site")), float(get("max_sample_photo_kb")))


@dataclass(frozen=True)
class BuildSubject:
    """What to build: a lead's facts, its measured problems, and its own homepage (if it has a real one)."""
    label: str                       # names the workspace folder, e.g. "lead-1" or "testset-01-salon-no-site"
    lead: dict[str, Any]
    problems: list[str]
    own_site_url: str | None = None
    lead_id: int | None = None
    testset_case: str | None = None
    inspection_id: int | None = None
    kind: str = "preview"            # "homepage" = Example Web Studio's own (archlight_home.yaml)
    privacy: tuple[str, ...] = ()    # the homepage's privacy page text, required on it word for word


@dataclass
class BuilderRun:
    builds: list[dict[str, Any]] = field(default_factory=list)
    message: str = ""


# ---------------- the workspace ----------------

def template_ready() -> bool:
    return (TEMPLATE_DIR / ASTRO).is_file()


def _link_packages(workspace: Path) -> None:
    """A junction (Windows' folder shortcut, no admin needed) to the template's installed packages."""
    target, link = TEMPLATE_DIR / "node_modules", workspace / "node_modules"
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


def new_workspace(root: Path, label: str) -> Path:
    stamp = f"{label}-{datetime.now():%Y%m%d-%H%M%S}"
    workspace, n = Path(root) / stamp, 1
    while workspace.exists():   # two builds in the same second
        n += 1
        workspace = Path(root) / f"{stamp}-{n}"
    workspace.mkdir(parents=True)
    for part in TEMPLATE_PARTS:
        source = TEMPLATE_DIR / part
        if source.is_dir():
            shutil.copytree(source, workspace / part, ignore=shutil.ignore_patterns(".gitkeep"))
        else:
            shutil.copy2(source, workspace / part)
    _link_packages(workspace)
    return workspace


def workspaces_root(town: Town) -> Path:
    return Path(town.settings.database).parent / "builds"


def run_astro_build(workspace: Path, timeout_s: float) -> tuple[bool, str]:
    node = shutil.which("node")
    if not node:
        raise BuilderError("Node.js isn't installed (it builds the sites).")
    env = {**clean_env(), "ASTRO_TELEMETRY_DISABLED": "1"}
    try:
        done = subprocess.run([node, str(ASTRO), "build"], cwd=workspace, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout_s, env=env, check=False)
    except subprocess.TimeoutExpired:
        return False, f"the Astro build took longer than {timeout_s:g}s"
    output = ANSI.sub("", (done.stdout or "") + (done.stderr or "")).strip()
    return done.returncode == 0 and (workspace / "dist" / "index.html").is_file(), output[-1500:]


# ---------------- one build ----------------

def builds_today(town: Town) -> int:
    """Sites started today. A Critic send-back rebuilds the same site, so it doesn't count again."""
    return town.conn.execute("SELECT COUNT(*) FROM builds WHERE created_at >= ? AND send_back_of IS NULL",
                             (local_day_start_utc(),)).fetchone()[0]


def _fonts() -> str:
    deps = json.loads((TEMPLATE_DIR / "package.json").read_text(encoding="utf-8"))["dependencies"]
    return ", ".join(name for name in sorted(deps) if name.startswith("@fontsource"))


class _Tally:
    """Adds up what every `claude -p` round used."""

    def __init__(self) -> None:
        self.rounds = 0
        self.seconds = 0.0
        self.turns = 0
        self.tokens = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
                       "cache_creation_input_tokens": 0}
        self.api_equivalent = 0.0
        self.tools: dict[str, int] = {}
        self.skills: list[str] = []
        self.model = ""
        self.session = ""

    def add(self, result: CliResult) -> None:
        self.rounds += 1
        self.seconds += result.wall_seconds
        self.turns += result.num_turns
        for key in self.tokens:
            self.tokens[key] += result.usage.get(key, 0)
        self.api_equivalent += result.api_equivalent_usd
        for name, count in result.tool_uses.items():
            self.tools[name] = self.tools.get(name, 0) + count
        self.skills += result.skills_used
        self.model, self.session = result.model, result.session_id or self.session


def build_site(town: Town, subject: BuildSubject, lim: BuilderLimits, fetch: Fetch = live_fetch,
               on_progress: Callable[[str], None] = lambda message: None) -> dict[str, Any]:
    """One build, start to finish. Returns the saved record. Raises TownStopped and
    SubscriptionLimitReached (after saving the failed record), so a run stops right there."""
    town.cli.check_available()
    started = time.monotonic()
    workspace = new_workspace(workspaces_root(town), subject.label)
    build_id = town.conn.execute(
        "INSERT INTO builds (lead_id, testset_case, inspection_id, workspace, status, kind, created_at)"
        " VALUES (?, ?, ?, ?, 'running', ?, ?)",
        (subject.lead_id, subject.testset_case, subject.inspection_id, str(workspace), subject.kind,
         utc_now())).lastrowid
    log_event(town.conn, AGENT, "build_started", {"build": build_id, "lead_id": subject.lead_id,
                                                  "testset_case": subject.testset_case, "workspace": str(workspace)})
    tally = _Tally()
    record: dict[str, Any] = {"id": build_id, "label": subject.label, "lead_id": subject.lead_id,
                              "testset_case": subject.testset_case, "business_name": subject.lead["business_name"],
                              "kind": subject.kind, "workspace": str(workspace), "facts_check": {}, "quality": {}}
    failure = None
    try:
        own_site, own_words = None, ""
        if subject.own_site_url:
            try:
                own_site = homepage_content(subject.own_site_url, fetch, workspace)
                own_words = own_site["text"]
            except Exception as err:   # their site can be down today; build from the lead facts alone
                log_event(town.conn, AGENT, "homepage_unavailable", {"build": build_id, "error": str(err)[:300]})
        if subject.kind == "homepage":
            brief = make_homepage_brief()
            own_words = homepage_words(brief)   # our own approved words, like a business's homepage
            write_brief(workspace, brief)
        else:
            # Licensed sample photos for this type (none until Charlie has downloaded them: the gallery then
            # shows styled tiles). A photo without a complete LICENSES.md row is never copied.
            photos = photo_library.photos_for(subject.lead.get("business_type"), lim.sample_photos,
                                              lim.max_sample_photo_kb)
            write_brief(workspace, make_brief(subject.lead, subject.problems, own_site,
                                              photo_library.copy_into(workspace, photos)))
        failure = _design_and_build(town, workspace, lim, tally, started, on_progress)
        if failure is None:
            failure = _check_built_site(record, workspace, subject, own_words, lim, on_progress)
    except (TownStopped, SubscriptionLimitReached) as err:
        _save(town, record, "failed", str(err), tally, started)
        raise
    except (CliTimeout, CliError, BuilderError, OSError) as err:
        failure = str(err)
    return _save(town, record, "failed" if failure else "built", failure, tally, started)


SEND_BACK_PROMPT = """The Critic, a separate reviewer who only saw the finished site, sent it back. Fix every item
below in place (keep what works, keep your design), then reply in one sentence. The same content rules
still apply: never add a fact that isn't in brief.json.

Must fix:
{must_fix}"""


def subject_from_brief(workspace: Path, label: str = "", **ids: Any) -> tuple[BuildSubject, str]:
    """Rebuilds what a build was given from its own brief.json: the lead's facts, its problems and the
    frozen homepage text. Used by Critic send-backs and reviews, so they judge against exactly that."""
    brief = json.loads((Path(workspace) / "brief.json").read_text(encoding="utf-8"))
    business = brief["business"]
    lead = {"business_name": business["name"], "business_type": business.get("type", ""),
            "area": business.get("area", ""), "address": business.get("address_exactly", ""),
            "phone": (business.get("phone_tel_link") or "")[len("tel:"):], "public_email": business.get("public_email", ""),
            "website": business.get("their_current_website", "")}
    own = brief.get("their_own_homepage") or {}
    kind = "homepage" if brief.get("site") == "homepage" else "preview"
    subject = BuildSubject(label=label or Path(workspace).name, lead=lead,
                           problems=list(brief.get("what_is_wrong_with_their_web_presence_now") or []),
                           kind=kind, privacy=tuple(privacy_lines(brief)), **ids)
    words = homepage_words(brief) if kind == "homepage" else str(own.get("text") or "")
    return subject, words


def send_back(town: Town, build_id: int, must_fix: list[str], lim: BuilderLimits,
              on_progress: Callable[[str], None] = lambda message: None) -> dict[str, Any]:
    """A Critic send-back: resumes the Builder's own session for `build_id`, in the same workspace, with
    the must-fix list. Then the same scan, Astro build, facts check and measuring as any build. Saved as
    a new builds row (send_back_of = the first build), which doesn't count toward sites_per_day."""
    town.cli.check_available()
    row = dict(town.conn.execute("SELECT * FROM builds WHERE id = ?", (build_id,)).fetchone())
    first = row["send_back_of"] or row["id"]
    workspace = Path(row["workspace"])
    subject, own_words = subject_from_brief(workspace, lead_id=row["lead_id"], testset_case=row["testset_case"],
                                            inspection_id=row["inspection_id"])
    started = time.monotonic()
    new_id = town.conn.execute(
        "INSERT INTO builds (lead_id, testset_case, inspection_id, workspace, status, send_back_of, kind,"
        " created_at) VALUES (?, ?, ?, ?, 'running', ?, ?, ?)",
        (row["lead_id"], row["testset_case"], row["inspection_id"], str(workspace), first, row["kind"],
         utc_now())).lastrowid
    log_event(town.conn, AGENT, "send_back_started", {"build": new_id, "send_back_of": first, "from_build": build_id,
                                                      "must_fix": must_fix[:20]})
    tally = _Tally()
    record: dict[str, Any] = {"id": new_id, "label": workspace.name, "lead_id": row["lead_id"],
                              "testset_case": row["testset_case"], "business_name": subject.lead["business_name"],
                              "kind": row["kind"], "workspace": str(workspace), "facts_check": {}, "quality": {},
                              "send_back_of": first}
    prompt = SEND_BACK_PROMPT.format(must_fix="\n".join(f"- {item}" for item in must_fix))
    failure = None
    try:
        failure = _design_and_build(town, workspace, lim, tally, started, on_progress, prompt=prompt,
                                    resume=row["session_id"] or None)
        if failure is None:
            failure = _check_built_site(record, workspace, subject, own_words, lim, on_progress)
    except (TownStopped, SubscriptionLimitReached) as err:
        _save(town, record, "failed", str(err), tally, started)
        raise
    except (CliTimeout, CliError, BuilderError, OSError) as err:
        failure = str(err)
    return _save(town, record, "failed" if failure else "built", failure, tally, started)


def _check_built_site(record: dict[str, Any], workspace: Path, subject: BuildSubject, own_words: str,
                      lim: BuilderLimits, on_progress: Callable[[str], None]) -> str | None:
    on_progress("Checking facts and measuring the site...")
    record["facts_check"] = facts_check(workspace / "dist", subject.lead, own_words,
                                        stale_source="outdated" in subject.problems,
                                        homepage=subject.kind == "homepage", privacy=subject.privacy)
    record["quality"] = measure_site(workspace / "dist", lim.lighthouse_timeout_s)
    outside = record["quality"].get("outside_requests") or []
    if outside:
        record["facts_check"]["passed"] = False
        record["facts_check"]["problems"].append(f"loads files from outside the site: {', '.join(outside[:3])}")
    failures = []
    if not record["facts_check"]["passed"]:
        failures.append("facts check failed: " + "; ".join(record["facts_check"]["problems"][:5]))
    motion = motion_problems(record["quality"])
    if motion:
        failures.append("reduced-motion check failed: " + "; ".join(motion[:5]))
    return " | ".join(failures) or None


def _design_and_build(town: Town, workspace: Path, lim: BuilderLimits, tally: _Tally, started: float,
                      on_progress: Callable[[str], None], prompt: str = PROMPT,
                      resume: str | None = None) -> str | None:
    """The `claude -p` rounds and the build. Returns None when the site built, else why it didn't.
    A Critic send-back starts with its must-fix prompt in the Builder's own earlier session (`resume`)."""
    hashes = file_hashes(TEMPLATE_DIR)
    brief = json.loads((workspace / "brief.json").read_text(encoding="utf-8"))
    job = JOB.format(fonts=_fonts()) + (HOMEPAGE_JOB if brief.get("site") == "homepage" else "")
    system = agent_instructions(AGENT, job)

    def tool_note(name: str, tool_input: dict[str, Any]) -> None:
        if "file_path" in tool_input:
            on_progress(f"  Builder: {name} {Path(str(tool_input['file_path'])).name}")
        elif name == "Skill":
            on_progress(f"  Builder: opened the {tool_input.get('skill')} skill")

    problems: list[str] = []
    for round_number in range(lim.fix_rounds + 1):
        remaining = lim.timeout_s - (time.monotonic() - started)
        if remaining <= 0:
            raise CliTimeout(f"the build took longer than {lim.timeout_s / 60:g} minutes")
        on_progress("Builder is designing the site..." if resume is None else f"Builder round {round_number + 1}...")
        result = town.cli.run(AGENT, prompt, workspace, TOOLS, remaining, lim.limit_wait_minutes,
                              denied=DENIED, plugin_dirs=[PLUGIN_DIR], system_append=system, resume=resume,
                              on_tool=tool_note)
        tally.add(result)
        town.killswitch.check()
        problems = scan_sources(workspace, hashes)
        if not problems:
            on_progress("Building the site (Astro)...")
            built, output = run_astro_build(workspace, ASTRO_BUILD_TIMEOUT_S)
            if built:
                return None
            problems = [f"the Astro build failed:\n{output}"]
        log_event(town.conn, AGENT, "build_round_rejected", {"workspace": str(workspace), "round": round_number,
                                                             "problems": problems[:10]})
        prompt = FIX_PROMPT.format(problems="\n".join(f"- {p}" for p in problems))
        resume = result.session_id
    return f"couldn't fix the site after {lim.fix_rounds} round(s): " + "; ".join(problems)[:600]


def _save(town: Town, r: dict[str, Any], status: str, failure: str | None, tally: _Tally,
          started: float) -> dict[str, Any]:
    dist = Path(r["workspace"]) / "dist"
    r["dist_hash"] = dist_hash(dist) if (dist / "index.html").is_file() else None
    r.update(status=status, failure=failure, model=tally.model, rounds=tally.rounds,
             wall_seconds=round(time.monotonic() - started, 1), claude_seconds=round(tally.seconds, 1),
             turns=tally.turns, tokens=tally.tokens, api_equivalent_usd=round(tally.api_equivalent, 4),
             tool_uses=tally.tools, design_skill_used=any(DESIGN_SKILL in s for s in tally.skills))
    town.conn.execute(
        "UPDATE builds SET status = ?, failure = ?, facts_check = ?, quality = ?, model = ?, rounds = ?,"
        " wall_seconds = ?, claude_seconds = ?, turns = ?, input_tokens = ?, output_tokens = ?,"
        " cache_read_tokens = ?, cache_write_tokens = ?, api_equivalent_usd = ?, tool_uses = ?,"
        " design_skill_used = ?, session_id = ?, dist_hash = ?, finished_at = ? WHERE id = ?",
        (status, failure, json.dumps(r["facts_check"]), json.dumps(r["quality"]), tally.model, tally.rounds,
         r["wall_seconds"], r["claude_seconds"], tally.turns, tally.tokens["input_tokens"],
         tally.tokens["output_tokens"], tally.tokens["cache_read_input_tokens"],
         tally.tokens["cache_creation_input_tokens"], r["api_equivalent_usd"], json.dumps(tally.tools),
         int(r["design_skill_used"]), tally.session, r["dist_hash"], utc_now(), r["id"]))
    log_event(town.conn, AGENT, "build_done" if status == "built" else "build_failed", {
        "build": r["id"], "lead_id": r["lead_id"], "testset_case": r["testset_case"], "failure": failure,
        "model": tally.model, "rounds": tally.rounds, "wall_seconds": r["wall_seconds"],
        "claude_seconds": r["claude_seconds"], "turns": tally.turns, "tokens": tally.tokens,
        "api_equivalent_usd": r["api_equivalent_usd"], "design_skill_used": r["design_skill_used"],
        "facts_passed": r["facts_check"].get("passed")})
    return r


# ---------------- `town build` ----------------

def _latest_inspection(town: Town, lead_id: int) -> dict[str, Any] | None:
    row = town.conn.execute("SELECT * FROM inspections WHERE lead_id = ? AND status = 'done' ORDER BY id DESC"
                            " LIMIT 1", (lead_id,)).fetchone()
    return dict(row) if row else None


def _has_good_build(town: Town, lead_id: int) -> bool:
    return town.conn.execute("SELECT 1 FROM builds WHERE lead_id = ? AND status = 'built'",
                             (lead_id,)).fetchone() is not None


def lead_subject(lead: dict[str, Any], inspection: dict[str, Any]) -> BuildSubject:
    """Its own homepage is only used when it's really theirs (not a parked or social page)."""
    facts = json.loads(inspection.get("facts") or "{}")
    own_site = (facts.get("site_kind") == KIND_OWN and not facts.get("domain_not_found")
                and facts.get("mentions_business") is not False and bool(inspection.get("final_url")))
    return BuildSubject(label=f"lead-{lead['id']}", lead=lead, problems=json.loads(inspection["problems"]),
                        own_site_url=inspection["final_url"] if own_site else None, lead_id=lead["id"],
                        inspection_id=inspection["id"])


def _subjects(town: Town, lead_id: int | None, again: bool) -> list[BuildSubject]:
    if lead_id is not None:
        row = town.conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
        if not row:
            raise BuilderError(f"No saved lead #{lead_id}.")
        if is_held(row):
            raise BuilderError(f"Lead #{lead_id} is {HELD_WORDS}: it's never contacted (email-first), so no site "
                               "is built for it.")
        inspection = _latest_inspection(town, lead_id)
        if not inspection:
            raise BuilderError(f"Lead #{lead_id} hasn't been inspected yet. Run `town inspect {lead_id}` first.")
        if not inspection["build_worthy"]:
            raise BuilderError(f"Lead #{lead_id} isn't build-worthy (need {inspection['need_score']}/100).")
        if _has_good_build(town, lead_id) and not again:
            raise BuilderError(f"Lead #{lead_id} already has a built site. Use `--again` to build a new one.")
        return [lead_subject(dict(row), inspection)]
    subjects = []
    for row in town.conn.execute("SELECT * FROM leads ORDER BY id"):
        inspection = _latest_inspection(town, row["id"])
        if inspection and inspection["build_worthy"] and not is_held(row) and not _has_good_build(town, row["id"]):
            subjects.append(lead_subject(dict(row), inspection))
    return subjects


def homepage_subject() -> BuildSubject:
    """Example Web Studio's own homepage: its "lead" is archlight_home.yaml (no address; a phone only if the file
    has one, so the facts check accepts exactly that number and nothing else)."""
    full = make_homepage_brief()
    brief = full["business"]
    lead = {"business_name": brief["name"], "business_type": brief["type"], "area": brief["area"], "address": "",
            "phone": brief["phone_tel_link"].removeprefix("tel:"), "public_email": brief["public_email"],
            "website": ""}
    return BuildSubject(label="homepage", lead=lead, problems=[], kind="homepage",
                        privacy=tuple(privacy_lines(full)))


def run_builder(town: Town, lead_id: int | None = None, again: bool = False, homepage: bool = False,
                on_progress: Callable[[str], None] = lambda message: None) -> BuilderRun:
    """`town build <lead id>`, `town build` (every build-worthy lead not yet built, up to the daily cap),
    or `town build --homepage` (Example Web Studio's own homepage; it counts toward the daily cap too)."""
    town.killswitch.check()
    town.cli.check_available()
    if not template_ready():
        raise BuilderError(f"The site template's packages aren't installed. Run once: {INSTALL_HINT}")
    lim = BuilderLimits.read(town.limits)
    subjects = [homepage_subject()] if homepage else _subjects(town, lead_id, again)
    if not subjects:
        return BuilderRun(message="No build-worthy leads are waiting for a site.")
    run = BuilderRun()
    for subject in subjects:
        town.killswitch.check()
        if builds_today(town) >= lim.per_day:
            log_event(town.conn, AGENT, "builder_daily_cap_reached", {"cap": lim.per_day})
            break
        on_progress("Building Example Web Studio's own homepage..." if subject.kind == "homepage" else
                    f"Building a sample site for lead #{subject.lead_id} {subject.lead['business_name']}...")
        run.builds.append(build_site(town, subject, lim, on_progress=on_progress))
    left = len(subjects) - len(run.builds)
    run.message = (f"{len(run.builds)} build(s) finished." +
                   (f" {left} wait for tomorrow (daily cap {lim.per_day})." if left else ""))
    return run


def latest_built_workspace(town: Town, lead_id: int | None = None, build_id: int | None = None) -> Path:
    """For `town show`: the newest successful build of a lead, or one build by id."""
    if build_id is not None:
        row = town.conn.execute("SELECT * FROM builds WHERE id = ?", (build_id,)).fetchone()
    else:
        row = town.conn.execute("SELECT * FROM builds WHERE lead_id = ? AND status = 'built' ORDER BY id DESC"
                                " LIMIT 1", (lead_id,)).fetchone()
    if not row:
        raise BuilderError(f"No built site for {'build #' + str(build_id) if build_id else 'lead #' + str(lead_id)}.")
    dist = Path(row["workspace"]) / "dist"
    if not (dist / "index.html").is_file():
        raise BuilderError(f"Build #{row['id']} has no finished site to show ({row['failure'] or 'no dist folder'}).")
    return dist
