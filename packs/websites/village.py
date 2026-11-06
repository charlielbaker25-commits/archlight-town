"""The website Town's village map (BLUEPRINT 9.2): which district each building stands in and how the roads run.

Plain data for Reaper HQ's page, which draws whatever a pack lays out (the engine page knows nothing about this
business). The map is a field of hexagon tiles. Each building stands in its own district: a "flower" of 37 hex
tiles (a centre tile and three rings around it), tinted in the district's own colour, with a one-tile street
round it. The Town Hall's district is in the middle; six districts sit round it, and six more in the gaps
between those. A straight road runs from the Town Hall to every district, so a message can go from any building
to the Town Hall and on to any other (BLUEPRINT 3.3: everything goes through the Town Hall). The Orb Tower
(Jarvis) stands right behind the Town Hall.

Hex positions are "axial" coordinates (q, r): q counts along a row, r counts rows, and the third axis is
-(q + r). Districts of radius 3 with a one-tile street between them have centres 9 tiles apart, so the six
around the Town Hall sit at the six rotations of (9, -4), and the six in the gaps at the sum of two neighbouring
ones.

`style` picks the building's look from the page's art library (Kenney's free CC0 3D city kits). Buildings
whose agent doesn't exist yet (Mailroom, Maintenance Shed, Bulletin Board) are listed with
`hidden_until_agent`: the page draws their district as an empty lot until that agent appears in the Town.
"""
from typing import Any

DISTRICT_RADIUS = 3               # each district: a centre tile plus 3 rings = 37 tiles
# The six touching districts, going round the Town Hall; then the six in the gaps between them.
RING_1 = ((9, -4), (4, 5), (-5, 9), (-9, 4), (-4, -5), (5, -9))
RING_2 = tuple((a[0] + b[0], a[1] + b[1]) for a, b in zip(RING_1, RING_1[1:] + RING_1[:1]))

# Each building: its agent, its district centre (axial q, r), its look and its district colour. The production
# line (Inspector -> Builder -> Critic -> Messenger) runs round the right-hand side; the Gatehouse faces the
# front edge, where anyone from outside the Town comes in.
BUILDINGS: tuple[dict[str, Any], ...] = (
    {"agent": "mayor", "district": (0, 0), "style": "town_hall", "color": "#c9a86a"},
    {"agent": "jarvis", "district": RING_1[5], "style": "orb_tower", "color": "#5b6cff"},
    {"agent": "inspector", "district": RING_1[0], "style": "workshop", "color": "#6f8cff"},
    {"agent": "builder", "district": RING_1[1], "style": "builders_yard", "color": "#c46a3a"},
    {"agent": "guard", "district": RING_1[2], "style": "gatehouse", "color": "#d0705a"},
    {"agent": "librarian", "district": RING_1[3], "style": "library", "color": "#4f9ad9"},
    {"agent": "treasurer", "district": RING_1[4], "style": "bank", "color": "#b8a640"},
    {"agent": "critic", "district": RING_2[0], "style": "gallery", "color": "#b06ad9"},
    {"agent": "messenger", "district": RING_2[1], "style": "post_office", "color": "#d9629a"},
    {"agent": "scout", "district": RING_2[3], "style": "lookout", "color": "#3fb3a5"},
    # Not built yet: their districts stay empty lots until their agents exist (BLUEPRINT 9.2).
    {"agent": "herald", "title": "Herald", "building": "Bulletin Board", "district": RING_2[2],
     "style": "gallery", "color": "#8fa04a", "hidden_until_agent": True},
    {"agent": "clerk", "title": "Clerk", "building": "Mailroom", "district": RING_2[4],
     "style": "post_office", "color": "#4aa0a0", "hidden_until_agent": True},
    {"agent": "caretaker", "title": "Caretaker", "building": "Maintenance Shed", "district": RING_2[5],
     "style": "workshop", "color": "#a07a5a", "hidden_until_agent": True},
)
HUB = "mayor"
# How far the grass reaches around the town (hex tiles from the centre). Scenery only.
FIELD_RADIUS = 26


def district_tiles(center: tuple[int, int], radius: int = DISTRICT_RADIUS) -> list[tuple[int, int]]:
    """Every hex tile within `radius` steps of a district's centre."""
    cq, cr = center
    tiles = []
    for dq in range(-radius, radius + 1):
        for dr in range(max(-radius, -dq - radius), min(radius, -dq + radius) + 1):
            tiles.append((cq + dq, cr + dr))
    return tiles


def roads() -> list[dict[str, Any]]:
    """One straight road from the Town Hall's district to each other district (from centre to centre)."""
    return [{"from": HUB, "to": b["agent"]} for b in BUILDINGS if b["agent"] != HUB]


def village_layout(agents: tuple[Any, ...]) -> dict[str, Any]:
    """The layout as plain JSON-ready data. Titles and building names come from the Town's agent list."""
    names = {a.name: a for a in agents}
    buildings = []
    for b in BUILDINGS:
        info = names.get(b["agent"])
        buildings.append({"agent": b["agent"], "title": info.title if info else b.get("title", b["agent"]),
                          "building": info.building if info else b["building"], "district": list(b["district"]),
                          "style": b["style"], "color": b["color"],
                          "hidden_until_agent": bool(b.get("hidden_until_agent"))})
    return {"grid": "hex", "district_radius": DISTRICT_RADIUS, "field_radius": FIELD_RADIUS, "hub": HUB,
            "person": {"node": "charlie", "via": "jarvis"}, "outside_via": "guard",
            "roads": roads(), "buildings": buildings}
