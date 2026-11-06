"""The Overture download, run against a tiny fake file shaped like Overture's (no network)."""
from pathlib import Path

import duckdb

from engine.settings import OvertureRegion
from packs.websites.overture import bounding_box, download, load_meta

CENTER_LAT, CENTER_LON = 38.6488, -90.3108


def _fake_overture(path: Path) -> None:
    """Three places in Overture's nested layout: one near the center, one 20+ miles out, one unnamed."""
    con = duckdb.connect()
    con.execute(f"""
        COPY (
            SELECT * FROM (VALUES
                ('place-near', {{'primary': 'Hair Studio X'}}, 'hair_salon',
                 {{'primary': 'hair_salon', 'hierarchy': ['beauty_and_spa', 'hair_salon'], 'alternates': []}},
                 0.93, ['http://hairstudiox.example'], ['+13145550100'], ['hello@hairstudiox.example'],
                 [{{'freeform': '1 Main St', 'locality': 'Clayton', 'region': 'MO', 'postcode': '63105', 'country': 'US'}}],
                 {{'xmin': -90.32, 'xmax': -90.32, 'ymin': 38.65, 'ymax': 38.65}},
                 {{'names': {{'primary': 'Studio X Salons'}}, 'wikidata': NULL}}),
                ('place-far', {{'primary': 'Far Away Diner'}}, 'restaurant',
                 {{'primary': 'restaurant', 'hierarchy': ['food', 'restaurant'], 'alternates': []}},
                 0.80, [], [], [],
                 [{{'freeform': '9 Road', 'locality': 'Elsewhere', 'region': 'MO', 'postcode': '63000', 'country': 'US'}}],
                 {{'xmin': -90.31, 'xmax': -90.31, 'ymin': 38.98, 'ymax': 38.98}}, NULL),
                ('place-noname', {{'primary': NULL}}, 'gym',
                 {{'primary': 'gym', 'hierarchy': ['gym'], 'alternates': []}},
                 0.50, [], [], [], [], {{'xmin': -90.31, 'xmax': -90.31, 'ymin': 38.65, 'ymax': 38.65}}, NULL)
            ) AS t(id, names, basic_category, taxonomy, confidence, websites, phones, emails, addresses, bbox, brand)
        ) TO '{path.as_posix()}' (FORMAT PARQUET)
    """)


def _region(tmp_path) -> OvertureRegion:
    return OvertureRegion("Test region", CENTER_LAT, CENTER_LON, 15, tmp_path / "overture" / "places.parquet")


def test_release_names_are_checked_before_they_reach_sql():
    import pytest

    from packs.websites.overture import check_release

    assert check_release("2026-09-23.1") == "2026-09-23.1"
    for bad in ("x'; COPY (SELECT 1) TO 'evil.csv' --", "latest", ""):
        with pytest.raises(ValueError):
            check_release(bad)


def test_bounding_box_covers_the_radius():
    box = bounding_box(OvertureRegion("x", CENTER_LAT, CENTER_LON, 15, Path("p")))
    assert box.min_lat < CENTER_LAT < box.max_lat
    assert round(box.max_lat - CENTER_LAT, 3) == round(15 / 69.0, 3)
    assert (box.max_lon - CENTER_LON) > (box.max_lat - CENTER_LAT)  # longitude degrees are shorter here


def test_download_keeps_only_named_places_inside_the_circle(tmp_path):
    fake = tmp_path / "fake_overture.parquet"
    _fake_overture(fake)
    region = _region(tmp_path)

    meta = download(region, release="2026-09-23.1", source=fake.as_posix(), con=duckdb.connect())

    rows = duckdb.connect().execute(f"SELECT * FROM read_parquet('{region.places_file.as_posix()}')").fetchall()
    assert meta["count"] == 1 == len(rows)
    cols = [d[0] for d in duckdb.connect().execute(
        f"SELECT * FROM read_parquet('{region.places_file.as_posix()}')").description]
    place = dict(zip(cols, rows[0]))
    assert place["source_id"] == "place-near"
    assert place["name"] == "Hair Studio X"
    assert place["brand"] == "Studio X Salons"
    assert place["brand_locations"] == 1
    assert place["basic_category"] == "hair_salon"
    assert place["taxonomy_hierarchy"] == "beauty_and_spa > hair_salon"
    assert place["email"] == "hello@hairstudiox.example"
    assert place["locality"] == "Clayton"
    assert place["release"] == "2026-09-23.1"
    assert place["distance_miles"] < 15
    assert load_meta(region)["release"] == "2026-09-23.1"
