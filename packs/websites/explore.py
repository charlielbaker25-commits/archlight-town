"""The 70/30 explore split (BLUEPRINT 5.4): where `town scout --auto` looks next. Plain code, no model.

The Librarian keeps the numbers per business type and area (cell_stats): leads found, build-worthy, Critic-passed,
previews Charlie approved, and replies once they're tracked. A cell is PROVEN once it has at least
explore.min_tries_to_judge leads and at least one preview Charlie approved (or, before any preview is approved,
a Critic-passed build). Each auto run is an EXPLOIT run with chance explore.exploit_share (about 70%): it picks a
proven cell, weighted by its win rate. Otherwise (about 30%), or whenever nothing is proven yet, it EXPLORES: an
untried or under-tried type and area, fewest tries first. Every pick says why in the log (`scout_pick`).

Never an empty cell (Charlie, 2026-11-04: four of six auto runs landed on cells with nothing usable): before a cell
is picked, plain code counts the Overture candidates in the local file that would pass Scout's own filters
(scout.usable_candidates: confidence, reachable, not already a lead or proposed, the captive and lead-gen screen,
and email-first: a public email), and a cell with fewer than explore.min_candidates is skipped. Free: local data,
no model call. Cells are counted only as they come up, so a pick reads a few cells, not all of them.
"""
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from engine.yamlio import load_yaml

EXPLORE_FILE = Path(__file__).resolve().parent / "explore.yaml"


def territory(path: Path = EXPLORE_FILE) -> tuple[dict[str, list[str]], list[str]]:
    data = load_yaml(path)
    types = {str(k): [str(t).lower() for t in v] for k, v in (data.get("types") or {}).items()}
    return types, [str(a) for a in data.get("areas") or []]


def type_of(business_type: str | None, types: dict[str, list[str]]) -> str | None:
    """The territory word ("salon") a saved lead's Overture type ("hair_salon") belongs to."""
    wanted = (business_type or "").lower()
    return next((word for word, names in types.items() if wanted in names), None)


def cell_stats(conn: Any, path: Path = EXPLORE_FILE) -> list[dict[str, Any]]:
    """Results per (type, area), from the database. Leads whose type isn't in the territory count under their own
    Overture type, so nothing found is hidden."""
    types, _ = territory(path)
    cells: dict[tuple[str, str], dict[str, Any]] = {}
    rows = conn.execute("""
        SELECT l.business_type, l.area,
          EXISTS (SELECT 1 FROM inspections i WHERE i.lead_id = l.id AND i.build_worthy = 1) AS build_worthy,
          EXISTS (SELECT 1 FROM builds b WHERE b.lead_id = l.id AND b.critic = 'passed') AS critic_passed,
          EXISTS (SELECT 1 FROM previews p WHERE p.lead_id = l.id AND p.kind = 'preview') AS preview_approved
        FROM leads l""")
    for r in rows:
        key = (type_of(r["business_type"], types) or (r["business_type"] or "unknown"), r["area"] or "unknown")
        cell = cells.setdefault(key, {"type": key[0], "area": key[1], "found": 0, "build_worthy": 0,
                                      "critic_passed": 0, "preview_approved": 0, "replies": None})
        cell["found"] += 1
        cell["build_worthy"] += int(r["build_worthy"])
        cell["critic_passed"] += int(r["critic_passed"])
        cell["preview_approved"] += int(r["preview_approved"])
    return sorted(cells.values(), key=lambda c: (-c["found"], c["type"], c["area"]))


@dataclass(frozen=True)
class Pick:
    business_type: str
    area: str
    mode: str          # "exploit" or "explore"
    why: str


def win_rate(cell: dict[str, Any]) -> float:
    """Previews Charlie approved per lead; before any approval, half credit for a Critic pass."""
    return (cell["preview_approved"] + 0.5 * cell["critic_passed"]) / max(cell["found"], 1)


class NoUsableCell(RuntimeError):
    """Every cell the split could pick has fewer usable candidates than explore.min_candidates."""


def pick(stats: list[dict[str, Any]], exploit_share: float, min_tries: int, rng: random.Random,
         path: Path = EXPLORE_FILE, usable: Callable[[str, str], int] | None = None, min_candidates: int = 0) -> Pick:
    """`usable(type, area)` counts the candidates Scout could really propose there (None = don't check). A cell
    with fewer than `min_candidates` is never picked; the reason names how many it had."""
    types, areas = territory(path)
    by_cell = {(c["type"], c["area"]): c for c in stats}
    counted: dict[tuple[str, str], int] = {}

    def enough(cell: tuple[str, str]) -> bool:
        if usable is None or min_candidates <= 0:
            return True
        if cell not in counted:
            counted[cell] = usable(*cell)
        return counted[cell] >= min_candidates

    def note(cell: tuple[str, str]) -> str:
        skipped = [f"{t} in {a} ({n})" for (t, a), n in counted.items() if n < min_candidates]
        have = f"; {counted[cell]} usable candidate(s)" if cell in counted else ""
        return have + (f"; skipped for too few candidates: {', '.join(skipped[:6])}" if skipped else "")

    proven = [c for c in stats if c["type"] in types and c["area"] in areas and c["found"] >= min_tries
              and win_rate(c) > 0]
    roll = rng.random()
    if proven and roll < exploit_share:
        left = list(proven)
        while left:
            cell = rng.choices(left, weights=[win_rate(c) for c in left])[0]
            key = (cell["type"], cell["area"])
            if enough(key):
                return Pick(cell["type"], cell["area"], "exploit",
                            f"exploit (roll {roll:.2f} < {exploit_share:.2f}): {cell['type']} in {cell['area']} is "
                            f"proven: {cell['found']} leads, {cell['critic_passed']} Critic-passed, "
                            f"{cell['preview_approved']} previews approved (win rate {win_rate(cell):.0%}; "
                            f"{len(proven)} proven cell(s) to pick from){note(key)}")
            left.remove(cell)
        # every proven cell has run dry: explore instead
    tries = {(t, a): by_cell.get((t, a), {}).get("found", 0) for t in types for a in areas}
    under = [k for k, n in tries.items() if n < min_tries]
    pool = under or list(tries)
    # Fewest tries first, in random order within the same count; the first cell with enough candidates wins.
    order = sorted(pool, key=lambda k: (tries[k], rng.random()))
    choice = next((k for k in order if enough(k)), None)
    if choice is None:
        raise NoUsableCell(f"no type and area in the territory has {min_candidates}+ usable candidates "
                           "(explore.min_candidates); add types or areas to explore.yaml, or refresh the Overture "
                           "data with `town overture-download`")
    reason = ("nothing is proven yet (too little data), so it explores" if not proven
              else f"explore (roll {roll:.2f} >= {exploit_share:.2f})")
    return Pick(choice[0], choice[1], "explore",
                f"{reason}: {choice[0]} in {choice[1]} has {tries[choice]} lead(s) so far "
                f"(under {min_tries} = not judged yet; {len(under)} under-tried cell(s) left){note(choice)}")


def auto_pick(town: Any, rng: random.Random | None = None, path: Path = EXPLORE_FILE) -> Pick:
    """`town scout --auto`: one pick, with the reason logged as `scout_pick` (the Librarian's numbers, plain code)."""
    from engine.events import log_event   # (kept local: this module stays importable without a Town)
    town.killswitch.check()
    share = float(town.limits.get("explore.exploit_share"))
    min_tries = int(town.limits.get("explore.min_tries_to_judge"))
    from packs.websites.scout import usable_candidates
    minimum = int(town.limits.get("explore.min_candidates"))
    places = Path(town.settings.overture.places_file)
    # Without the Overture file there's nothing to count; Scout itself then refuses ("no Overture data yet").
    counter = (lambda kind, area: usable_candidates(town, kind, area)) if places.exists() else None
    choice = pick(cell_stats(town.conn, path), share, min_tries, rng or random.Random(), path, counter, minimum)
    log_event(town.conn, "librarian", "scout_pick", {"business_type": choice.business_type, "area": choice.area,
                                                     "mode": choice.mode, "why": choice.why,
                                                     "exploit_share": share, "min_tries": min_tries})
    return choice
