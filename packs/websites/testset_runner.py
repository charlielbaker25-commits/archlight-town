"""`town testset`: scores the agents on the fictional businesses in packs/websites/testset/.

The test set is rerun after every prompt, playbook, model or engine change, and a change
only ships if the score holds or improves (BLUEPRINT 7.1). How one run works:
1. Load every case (the business, shaped like a lead row) and its answer key (Charlie's judgment).
2. Each agent that exists so far does its job on the cases, and plain code compares its
   answers with the key: one point per answer that matches.
3. Steps 1-2 repeat N times (`--runs N`, default 3), because Haiku can answer differently
   each time. Each agent gets an average and a lowest score.
4. The run is saved in `testset_runs` (time, git commit, N, scores, cost), and its average
   is compared with the last run's on this instance.

Nothing here proposes, saves or sends anything: the test set only asks agents for their
judgment. Paid calls go through the same kill switch, budget and cost log as real work.

Offline rule: in a test-set run, a case's URL always means its frozen copy in the case's
`site/` folder. Agents that look at a site must go through resolve_case_url(), which refuses
anything else, so a test-set run never fetches from the internet.
"""
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from engine.db import utc_now
from engine.events import log_event
from engine.town import Town
from engine.yamlio import load_yaml
from packs.websites.builder import (INSTALL_HINT, BuilderLimits, BuildSubject, build_site, builds_today,
                                    template_ready)
from packs.websites.builder_check import motion_problems, page_text, times_of_day
from packs.websites.inspector import CATEGORIES, KIND_OWN, InspectorLimits, grade, site_kind
from packs.websites.inspector_measure import FixtureAccess, SiteMeasurer
from packs.websites.leads import lead_route
from packs.websites.scout import UNREADABLE_PREFIX, pick_leads, screen_candidates

CASES_DIR = Path(__file__).resolve().parent / "testset" / "cases"
REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT = "testset"

INSPECTOR_CATEGORIES = CATEGORIES
# The year the fixture sites were frozen. The Inspector judges "outdated" against this, not today's
# date, so case 08's "(c) 2022" doesn't quietly turn outdated in 2027 and change the answers.
TESTSET_YEAR = 2026
INSPECTOR_MODEL = "plain code (Playwright + Lighthouse)"
NOT_INSPECTED = "not_inspected"
SCOUT_ANSWERS = ("pick", "reject", "inspector_decides")
CONTACT_ROUTES = ("email", "held", "none")   # held = no public email: saved, never contacted (email-first)
CASE_FIELDS = ("business_name", "business_type", "area", "address", "phone", "website", "public_email",
               "source", "source_id", "confidence")
# What Scout is told about the test-set shortlist (the cases mix business types and towns).
TESTSET_BUSINESS_TYPE = "mixed local businesses (test set)"
TESTSET_AREA = "St. Louis area"


class TestsetError(ValueError):
    __test__ = False  # tells pytest this "Test..." name isn't a test class


class OfflineViolation(RuntimeError):
    """An agent tried to reach a URL during a test-set run that isn't a case's own local copy."""


@dataclass(frozen=True)
class Case:
    name: str           # the folder name, e.g. "01-salon-no-site"
    folder: Path
    info: dict[str, Any]
    answer: dict[str, Any]

    @property
    def source_id(self) -> str:
        return self.info["source_id"]


@dataclass
class AgentScore:
    agent: str
    earned: int = 0
    possible: int = 0
    cost_usd: float = 0.0
    model: str = ""
    parts: dict[str, list[int]] = field(default_factory=dict)  # e.g. {"picks": [5, 7]}
    misses: list[str] = field(default_factory=list)
    note: str = ""

    def add(self, part: str, ok: bool, miss: str) -> None:
        earned, possible = self.parts.get(part, [0, 0])
        self.parts[part] = [earned + ok, possible + 1]
        self.earned += ok
        self.possible += 1
        if not ok:
            self.misses.append(miss)


@dataclass(frozen=True)
class AgentSummary:
    """One agent's result over all N runs. Haiku varies from run to run, so one run is a small
    sample: the average is what "holds or improves" compares, and the lowest shows the worst case."""
    agent: str
    runs: int
    earned: float          # the average over the runs
    lowest: int
    possible: int
    cost_usd: float        # all runs together
    model: str
    parts: dict[str, list[float]]   # e.g. {"picks": [6.67, 7]}: average earned, possible
    misses: list[str]               # each distinct miss, with how many runs it happened in
    note: str


@dataclass
class TestsetRun:
    __test__ = False  # tells pytest this "Test..." name isn't a test class
    run_id: int
    cases: int
    runs: int
    git_commit: str
    scores: list[AgentSummary]
    earned: float          # the sum of the agents' averages
    possible: int
    cost_usd: float
    previous: dict[str, Any] | None   # the last saved run on this instance, if any


# ---------------- loading cases ----------------

def _plain(value: Any) -> Any:
    """ruamel's YAML types -> plain str/list/dict, so they compare and serialize simply."""
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def _check(case: Case) -> None:
    """Fail loudly on a typo in a case or answer key instead of scoring against it."""
    missing = [f for f in CASE_FIELDS if f not in case.info]
    if missing:
        raise TestsetError(f"{case.name}/case.yaml is missing {', '.join(missing)}")
    if not str(case.info["source"]).startswith("testset"):
        raise TestsetError(f"{case.name}: source must be 'testset' (test cases are never real leads)")
    fixture = case.info.get("fixture")
    if fixture and not (case.folder / fixture).is_file():
        raise TestsetError(f"{case.name}: fixture {fixture} doesn't exist")
    answer = case.answer
    rules = {"worth_pursuing": ("yes", "no"), "scout": SCOUT_ANSWERS, "contact_route": CONTACT_ROUTES}
    for key, allowed in rules.items():
        if answer.get(key) not in allowed:
            raise TestsetError(f"{case.name}/answer.yaml: {key} must be one of {', '.join(allowed)}, "
                               f"got {answer.get(key)!r}")
    problems = answer.get("site_problems")
    if problems != NOT_INSPECTED and not (isinstance(problems, list)
                                          and set(problems) <= set(INSPECTOR_CATEGORIES)):
        raise TestsetError(f"{case.name}/answer.yaml: site_problems must be '{NOT_INSPECTED}' or a list of "
                           f"{', '.join(INSPECTOR_CATEGORIES)}, got {problems!r}")
    for fact in answer.get("published_facts") or []:
        if not (isinstance(fact, dict) and fact.get("fact") and isinstance(fact.get("shows_times"), list)
                and times_of_day(" ".join(fact["shows_times"]))):
            raise TestsetError(f"{case.name}/answer.yaml: each published_facts entry needs 'fact' and a list of "
                               f"'shows_times' (like \"8:00\"), got {fact!r}")


def load_cases(folder: Path = CASES_DIR) -> list[Case]:
    cases = []
    for path in sorted(Path(folder).iterdir()):
        if not path.is_dir():
            continue
        # YAML 1.2 (what ruamel reads) keeps yes/no as the words "yes"/"no", not True/False.
        case = Case(path.name, path, _plain(load_yaml(path / "case.yaml")), _plain(load_yaml(path / "answer.yaml")))
        _check(case)
        cases.append(case)
    if not cases:
        raise TestsetError(f"No test cases found in {folder}")
    ids = [c.source_id for c in cases]
    if len(ids) != len(set(ids)):
        raise TestsetError("Two test cases share a source_id")
    return cases


# ---------------- the offline rule ----------------

def resolve_case_url(case: Case, url: str) -> Path:
    """Where a URL points during a test-set run: always a file inside the case's own site/ folder.

    The case's website (and any page under it) maps to its local fixture; a relative link
    like "menu.html" maps inside the same folder. Anything else is refused, so nothing is
    ever fetched from the internet. The file may not exist: that's a broken link to report.
    """
    fixture = case.info.get("fixture")
    if not fixture:
        raise OfflineViolation(f"{case.name} has no local site, so no URL can be loaded for it")
    page = (case.folder / fixture).resolve()
    site_root = page.parent
    website = str(case.info.get("website") or "").rstrip("/")
    url = (url or "").strip()
    if website and url.rstrip("/") == website:
        return page
    if website and url.startswith(website + "/"):
        relative = url[len(website) + 1:]
    elif "://" in url or url.startswith("//"):
        raise OfflineViolation(f"{case.name}: refused {url} (test-set runs never reach the internet)")
    else:
        relative = url
    relative = relative.split("#", 1)[0].split("?", 1)[0] or page.name
    target = (site_root / relative).resolve()
    if not target.is_relative_to(site_root):
        raise OfflineViolation(f"{case.name}: refused {url} (outside the case's site folder)")
    return target


# ---------------- scoring each agent ----------------

def scout_candidate(case: Case) -> dict[str, Any]:
    """A case, shaped like the Overture row Scout normally sees."""
    info = case.info
    return {
        "source_id": info["source_id"], "name": info["business_name"],
        "taxonomy_primary": info["business_type"], "basic_category": info["business_type"],
        "website": info["website"] or None, "email": info["public_email"] or None,
        "phone": info["phone"] or None, "locality": info["area"], "address": info["address"],
        "confidence": info["confidence"], "release": "testset",
        "brand": info.get("brand") or None,   # Overture's chain/franchise name, if the case has one
    }


def score_scout(town: Town, cases: list[Case]) -> AgentScore:
    """Scout is scored on two things:
    - picks: on cases it can judge from the lead row alone, did it pick or pass as the key says?
      (Cases marked inspector_decides need the website, which Scout never sees, so they don't count.)
    - contact route: does every lead get the right route (email, or held: no public email, never contacted)?"""
    # Plain code's screen runs first, as in a real Scout run: a skipped case counts as "reject".
    kept, skipped = screen_candidates([scout_candidate(c) for c in cases])
    picks, reply = pick_leads(town, kept, TESTSET_BUSINESS_TYPE, TESTSET_AREA, len(cases))
    why = {place["source_id"]: reason for place, reason in picks}
    score = AgentScore("scout", cost_usd=reply.cost_usd, model=reply.model)
    if any(reason.startswith(UNREADABLE_PREFIX) for reason in why.values()):
        score.note = "Haiku's reply couldn't be read; scored the code's fallback ranking"
    for case in cases:
        expected = case.answer["scout"]
        if expected != "inspector_decides":
            got = "pick" if case.source_id in why else "reject"
            reason = f' ("{why[case.source_id]}")' if got == "pick" else ""
            score.add("picks", got == expected, f"{case.name}: expected {expected}, got {got}{reason}")
        expected_route = case.answer["contact_route"]
        if expected_route != "none":
            got_route = lead_route(case.info["public_email"])
            score.add("contact route", got_route == expected_route,
                      f"{case.name}: expected route {expected_route}, got {got_route}")
    return score


def open_measurer(town: Town, lim: InspectorLimits) -> Any:
    """The real browser and Lighthouse. Tests swap this for a fake (pytest never opens a browser)."""
    return SiteMeasurer(lim.measure_settings(), check=town.killswitch.check)


def score_inspector(town: Town, cases: list[Case]) -> AgentScore:
    """One point per case when the Inspector's problems match the key exactly (missing or extra both miss).
    Cases marked not_inspected are rejected before the Inspector, so they don't count.
    The measuring and grading are plain code, so this costs nothing. The note isn't scored, so no
    Haiku call is made. Sites load only through resolve_case_url() (the offline rule)."""
    lim = InspectorLimits.read(town.limits)
    score = AgentScore("inspector", model=INSPECTOR_MODEL)
    scored = [c for c in cases if c.answer["site_problems"] != NOT_INSPECTED]
    with open_measurer(town, lim) as measurer:
        for case in scored:
            town.killswitch.check()
            website = str(case.info["website"] or "")
            kind = site_kind(website)
            identity = {"name": case.info["business_name"], "phone": case.info["phone"]}
            facts = (measurer.measure(website, FixtureAccess(case, resolve_case_url, OfflineViolation), None, identity)
                     if kind == KIND_OWN else {})
            got, expected = grade(kind, facts, lim, TESTSET_YEAR), case.answer["site_problems"]
            missed = [p for p in expected if p not in got]
            extra = [p for p in got if p not in expected]
            detail = "; ".join(x for x in (f"missed {', '.join(missed)}" if missed else "",
                                           f"extra {', '.join(extra)}" if extra else "") if x)
            score.add("site problems", not missed and not extra,
                      f"{case.name}: expected [{', '.join(expected)}], got [{', '.join(got)}] ({detail})")
    return score


# The Builder check runs only on demand (`town testset --agents builder`): each build uses Charlie's
# Claude plan, so it's 2 cases by default, not every case on every run.
DEFAULT_BUILDER_CASES = ("01-salon-no-site", "07-dentist-outdated")


def score_builder(town: Town, cases: list[Case], case_names: tuple[str, ...] = DEFAULT_BUILDER_CASES,
                  on_progress: Callable[[str], None] = lambda message: None) -> AgentScore:
    """Five points per case: the site builds, the facts check passes, it fits a 375px phone with no
    sideways scrolling, Lighthouse performance and accessibility reach the cutoffs in limits.yaml, and
    nothing moves when the visitor asks for reduced motion. A sixth point for a case whose answer key
    lists `published_facts` (like case 07's office hours): the site shows them.
    The business's homepage (cases with a site) is read only through resolve_case_url()."""
    if not template_ready():
        raise TestsetError(f"The site template's packages aren't installed. Run once: {INSTALL_HINT}")
    lim = BuilderLimits.read(town.limits)
    by_name = {c.name: c for c in cases}
    unknown = [n for n in case_names if n not in by_name]
    if unknown:
        raise TestsetError(f"No test case named {', '.join(unknown)}")
    score = AgentScore("builder", model=town.routing.for_agent("builder").model)
    used = []
    for name in case_names:
        case = by_name[name]
        town.killswitch.check()
        if builds_today(town) >= lim.per_day:
            raise TestsetError(f"Today's build cap ({lim.per_day}, building.sites_per_day) is reached.")
        website = str(case.info.get("website") or "")
        problems = case.answer["site_problems"]
        subject = BuildSubject(label=f"testset-{case.name}", lead=case.info,
                               problems=problems if isinstance(problems, list) else [],
                               own_site_url=website if site_kind(website) == KIND_OWN else None,
                               testset_case=case.name)
        on_progress(f"Test build: {case.name} ...")
        b = build_site(town, subject, lim, fetch=lambda url, c=case: resolve_case_url(c, url).read_bytes(),
                       on_progress=on_progress)
        built = bool(b["facts_check"])   # the facts check only runs once the site has built
        quality = b["quality"]
        lighthouse = quality.get("lighthouse") or {}
        perf, a11y = lighthouse.get("performance"), lighthouse.get("accessibility")
        score.add("builds", built, f"{name}: didn't build (build #{b['id']}: {b['failure']})")
        score.add("facts check", built and b["facts_check"].get("passed", False),
                  f"{name}: facts check failed (build #{b['id']}): "
                  + "; ".join(b["facts_check"].get("problems", [])[:3]))
        score.add("fits a phone", built and quality.get("overflows_at_375") is False,
                  f"{name}: scrolls sideways at 375px ({quality.get('width_at_375')}px wide)")
        good_lighthouse = (perf is not None and a11y is not None and perf >= lim.min_performance
                           and a11y >= lim.min_accessibility)
        score.add("lighthouse", built and good_lighthouse,
                  f"{name}: Lighthouse performance {perf} (need {lim.min_performance:g}), accessibility {a11y} "
                  f"(need {lim.min_accessibility:g})")
        motion = motion_problems(quality)
        score.add("reduced motion", built and not motion, f"{name}: " + "; ".join(motion[:3]))
        for fact in case.answer.get("published_facts") or []:
            shown = published_times_shown(Path(b["workspace"]) / "dist") if built else set()
            wanted = times_of_day(" ".join(fact["shows_times"]))
            score.add("published facts", wanted <= shown,
                      f"{name}: left out their published {fact['fact']} (build #{b['id']})")
        used.append(f"#{b['id']} {b['wall_seconds']:.0f}s, {b['tokens']['output_tokens']:,} output tokens")
    score.note = "builds " + "; ".join(used) + " (Claude plan usage, $0 billed)"
    return score


def published_times_shown(dist: Path) -> set[tuple[int, int]]:
    """Every time of day the built pages show (to check the site kept the hours the business publishes)."""
    shown: set[tuple[int, int]] = set()
    for page in Path(dist).rglob("*.html"):
        shown |= times_of_day(page_text(page.read_text(encoding="utf-8", errors="replace"))[0])
    return shown


# ---------------- the Critic (on demand: each review uses Charlie's Claude plan) ----------------

CRITIC_DIR = CASES_DIR.parent / "critic"
CRITIC_VERDICTS = ("pass", "fail")


@dataclass(frozen=True)
class CriticFixture:
    """A frozen sample site with a known verdict: brief.json (what the Builder was given), dist/ (the built
    site), src/ (its source, if any) and answer.yaml (Charlie's key: verdict, and for a fail, the words
    the Critic's reasons must use to show it found the flaw)."""
    name: str
    folder: Path
    answer: dict[str, Any]


def load_critic_fixtures(folder: Path = CRITIC_DIR) -> list[CriticFixture]:
    fixtures = []
    for path in sorted(Path(folder).iterdir()):
        if not path.is_dir():
            continue
        answer = _plain(load_yaml(path / "answer.yaml"))
        if answer.get("verdict") not in CRITIC_VERDICTS:
            raise TestsetError(f"critic/{path.name}/answer.yaml: verdict must be pass or fail")
        if answer["verdict"] == "fail" and not (isinstance(answer.get("reason_mentions"), list)
                                                and answer["reason_mentions"]):
            raise TestsetError(f"critic/{path.name}/answer.yaml: a fail needs reason_mentions (words naming the flaw)")
        if not ((path / "dist" / "index.html").is_file() and (path / "brief.json").is_file()):
            raise TestsetError(f"critic/{path.name}: needs dist/index.html and brief.json")
        fixtures.append(CriticFixture(path.name, path, answer))
    if not fixtures:
        raise TestsetError(f"No Critic fixtures found in {folder}")
    return fixtures


def critic_reasons(review: dict[str, Any]) -> str:
    """Everything the Critic said about a site, as one lowercase text, to look for the flaw's name."""
    parts = list(review.get("must_fix") or []) + list(review.get("invented_claims") or [])
    parts += [s.get("reason", "") for s in (review.get("scores") or {}).values()] + [review.get("summary") or ""]
    return " ".join(parts).lower()


def score_critic(town: Town, cases: list[Case], fixtures_dir: Path = CRITIC_DIR,
                 on_progress: Callable[[str], None] = lambda message: None) -> AgentScore:
    """One point per frozen site: the verdict matches Charlie's key, and for a flawed site the Critic's
    reasons name the flaw. Each fixture passes every plain-code check on purpose, so this scores the
    Critic's judgment, not the code's. Sites are served from this laptop only (127.0.0.1)."""
    from packs.websites.critic import CriticLimits, ReviewSubject, review_site   # critic imports builder too
    lim = CriticLimits.read(town.limits)
    score = AgentScore("critic", model=town.routing.for_agent("critic").model)
    used = []
    for fixture in load_critic_fixtures(fixtures_dir):
        town.killswitch.check()
        on_progress(f"Critic test: {fixture.name} ...")
        src = fixture.folder / "src"
        review = review_site(town, ReviewSubject(label=f"testset-{fixture.name}", dist=fixture.folder / "dist",
                                                 brief=fixture.folder / "brief.json",
                                                 src=src if src.is_dir() else None,
                                                 testset_case=f"critic/{fixture.name}"), lim, on_progress=on_progress)
        expected = fixture.answer["verdict"]
        if review["hard_failures"]:
            score.add("verdict and reason", False, f"{fixture.name}: the fixture fails a plain-code check, so the "
                      f"Critic never judged it (fix the fixture): {review['hard_failures'][0]}")
        elif review["verdict"] != expected:
            score.add("verdict and reason", False, f"{fixture.name}: expected {expected}, got {review['verdict']} "
                      f"(review #{review['id']}: {'; '.join(review['must_fix'][:2]) or review.get('summary')})")
        else:
            words = [w.lower() for w in fixture.answer.get("reason_mentions") or []]
            named = expected == "pass" or any(w in critic_reasons(review) for w in words)
            score.add("verdict and reason", named, f"{fixture.name}: right verdict, but its reasons don't name the "
                      f"flaw ({fixture.answer.get('flaw')}) (review #{review['id']})")
        used.append(f"{fixture.name} {review['wall_seconds']:.0f}s, {review['tokens']['output_tokens']:,} out")
    score.note = "reviews " + "; ".join(used) + " (Claude plan usage, $0 billed)"
    return score


# Every agent's slot, in pipeline order. None = not built yet; fill each in as it's built.
# A plain `town testset` runs the built slots. The Builder, Critic and Messenger checks are on demand only
# (they use Charlie's Claude plan); the Guard's is free plain code but scored on its own (ON_DEMAND), so a plain
# run stays comparable with every earlier one. Their scorers live in testset_outreach.py and are loaded lazily.
Scorer = Callable[[Town, list[Case]], AgentScore]
AGENT_SLOTS: dict[str, Scorer | None] = {
    "scout": score_scout,
    "inspector": score_inspector,
}
ON_DEMAND = ("builder", "critic", "messenger", "guard", "reaper", "librarian")
DEFAULT_AGENTS = tuple(name for name, scorer in AGENT_SLOTS.items() if scorer)
NOT_BUILT: dict[str, str] = {}


# ---------------- a whole run ----------------

def current_commit(root: Path = REPO_ROOT) -> str:
    """The commit being scored, plus '+dirty' if there are uncommitted changes."""
    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=root, capture_output=True,
                                text=True, check=True, timeout=10).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True,
                               text=True, check=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return f"{commit}+dirty" if dirty else commit


def agents_of(row: dict[str, Any]) -> list[str]:
    """Which agents a saved run scored (runs saved before `--agents` existed scored the defaults)."""
    return json.loads(row["scores"]).get("agents_run") or sorted(DEFAULT_AGENTS)


def last_run(town: Town, agents: list[str] | None = None) -> dict[str, Any] | None:
    """The last run that scored the same agents, so a Builder-only run is never compared with a full one."""
    wanted = sorted(agents or DEFAULT_AGENTS)
    for row in town.conn.execute("SELECT * FROM testset_runs ORDER BY id DESC"):
        if agents_of(dict(row)) == wanted:
            return dict(row)
    return None


def history(town: Town, limit: int = 10) -> list[dict[str, Any]]:
    rows = town.conn.execute("SELECT * FROM testset_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def _miss_key(miss: str) -> str:
    """The same miss in two runs can quote a different Haiku reason, so group on the text before it."""
    return miss.split(' ("', 1)[0]


def summarize(agent_runs: list[AgentScore]) -> AgentSummary:
    """Fold one agent's N scores into its average, lowest and repeated misses."""
    runs = len(agent_runs)
    first = agent_runs[0]
    parts = {name: [round(sum(r.parts.get(name, [0, 0])[0] for r in agent_runs) / runs, 2), possible]
             for name, (_, possible) in first.parts.items()}
    seen: dict[str, tuple[str, int]] = {}   # miss key -> (first full text, how many runs)
    for r in agent_runs:
        for miss in r.misses:
            text, count = seen.get(_miss_key(miss), (miss, 0))
            seen[_miss_key(miss)] = (text, count + 1)
    misses = [text if runs == 1 else f"{text} [in {count}/{runs} runs]" for text, count in seen.values()]
    notes = sorted({r.note for r in agent_runs if r.note})
    return AgentSummary(agent=first.agent, runs=runs, earned=round(sum(r.earned for r in agent_runs) / runs, 2),
                        lowest=min(r.earned for r in agent_runs), possible=first.possible,
                        cost_usd=sum(r.cost_usd for r in agent_runs), model=first.model, parts=parts,
                        misses=misses, note="; ".join(notes))


def _scorers(agents: list[str], builder_cases: tuple[str, ...] | None,
             on_progress: Callable[[str], None]) -> list[Scorer]:
    allowed = DEFAULT_AGENTS + ON_DEMAND
    unknown = [a for a in agents if a not in allowed]
    if unknown:
        raise TestsetError(f"Unknown agent {', '.join(unknown)}. Choose from {', '.join(allowed)}.")
    scorers: list[Scorer] = []
    for name in allowed:   # pipeline order
        if name not in agents:
            continue
        if name == "builder":
            names = builder_cases or DEFAULT_BUILDER_CASES
            scorers.append(lambda town, cases: score_builder(town, cases, names, on_progress))
        elif name == "critic":
            scorers.append(lambda town, cases: score_critic(town, cases, on_progress=on_progress))
        elif name == "messenger":
            from packs.websites.testset_outreach import score_messenger   # it imports this module too
            scorers.append(lambda town, cases: score_messenger(town, cases, on_progress=on_progress))
        elif name == "guard":
            from packs.websites.testset_outreach import score_guard
            scorers.append(score_guard)
        elif name == "librarian":
            from packs.websites.testset_librarian import score_librarian   # a throwaway Town with frozen history
            scorers.append(score_librarian)
        elif name == "reaper":
            from packs.websites.testset_reaper import score_reaper   # offline: scripted Haiku, throwaway towns
            scorers.append(score_reaper)
        else:
            scorers.append(AGENT_SLOTS[name])
    return scorers


def run_testset(town: Town, runs: int = 3, cases_dir: Path = CASES_DIR, agents: list[str] | None = None,
                builder_cases: tuple[str, ...] | None = None,
                on_progress: Callable[[str], None] = lambda message: None) -> TestsetRun:
    """Run the set `runs` times and save one row with each agent's average and lowest score.
    `agents` defaults to every built slot (Scout and Inspector); "builder" is on demand only."""
    if runs < 1:
        raise TestsetError("--runs must be at least 1")
    town.killswitch.check()
    agents = sorted(agents or DEFAULT_AGENTS)
    scorers = _scorers(agents, builder_cases, on_progress)
    cases = load_cases(cases_dir)
    previous = last_run(town, agents)
    by_agent: dict[str, list[AgentScore]] = {}
    for _ in range(runs):
        for scorer in scorers:
            town.killswitch.check()  # `town stop` mid-way stops the set before the next agent
            score = scorer(town, cases)
            by_agent.setdefault(score.agent, []).append(score)
    scores = [summarize(agent_runs) for agent_runs in by_agent.values()]
    earned = round(sum(s.earned for s in scores), 2)
    possible = sum(s.possible for s in scores)
    cost = sum(s.cost_usd for s in scores)
    commit = current_commit()
    detail = {
        "agents": {s.agent: {"earned": s.earned, "lowest": s.lowest, "possible": s.possible, "parts": s.parts,
                             "model": s.model, "cost_usd": round(s.cost_usd, 6), "misses": s.misses,
                             "note": s.note}
                   for s in scores},
        "not_built": NOT_BUILT,
        "agents_run": agents,
    }
    run_id = town.conn.execute(
        "INSERT INTO testset_runs (ts, instance, git_commit, cases, runs, earned, possible, cost_usd, scores)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (utc_now(), town.settings.instance, commit, len(cases), runs, earned, possible, cost, json.dumps(detail)),
    ).lastrowid
    log_event(town.conn, AGENT, "testset_run", {"run": run_id, "commit": commit, "runs": runs, "earned": earned,
                                                "possible": possible, "cost_usd": round(cost, 6),
                                                "by_agent": {s.agent: {"average": s.earned, "lowest": s.lowest,
                                                                       "possible": s.possible} for s in scores}})
    return TestsetRun(run_id, len(cases), runs, commit, scores, earned, possible, cost, previous)
