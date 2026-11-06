"""Overture Maps places: download the Town's region to a local file, then read it with no API calls.

Overture is open data (CDLA Permissive 2.0 / Apache 2.0), so the Town may store it.
The download follows Overture's own DuckDB recipe: read the public Parquet files
on S3 and filter by each place's bounding box, so DuckDB only fetches the parts
of the files that cover the region. Then it keeps only places inside the circle
(center + radius from instance.yaml) and flattens the fields Scout needs.

Business types come from `basic_category` and `taxonomy` (Overture removed the
old `categories` field in September 2026). `brand` is the chain or franchise name
when Overture knows one (often it's just the business's own name), and
`brand_locations` counts the region's places with that brand. Scout uses both to
flag chain locations.

Refresh it when Overture publishes a new monthly release; each place records
the release it came from.
"""
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path

import duckdb

from engine.db import utc_now
from engine.settings import OvertureRegion

BUCKET = "s3://overturemaps-us-west-2/release"
MILES_PER_DEGREE_LAT = 69.0
EARTH_RADIUS_MILES = 3958.8


@dataclass(frozen=True)
class BoundingBox:
    min_lon: float
    max_lon: float
    min_lat: float
    max_lat: float


def bounding_box(region: OvertureRegion) -> BoundingBox:
    """The square around the circle. A degree of longitude shrinks away from the equator, hence cos(lat)."""
    dlat = region.radius_miles / MILES_PER_DEGREE_LAT
    dlon = region.radius_miles / (MILES_PER_DEGREE_LAT * math.cos(math.radians(region.center_lat)))
    return BoundingBox(region.center_lon - dlon, region.center_lon + dlon,
                       region.center_lat - dlat, region.center_lat + dlat)


RELEASE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}(\.\d+)?(-\w+)?")


def check_release(release: str) -> str:
    """Releases look like 2026-09-23.1. Anything else is refused before it goes near the SQL."""
    if not RELEASE_PATTERN.fullmatch(release or ""):
        raise ValueError(f"'{release}' doesn't look like an Overture release (e.g. 2026-09-23.1)")
    return release


def places_source(release: str) -> str:
    return f"{BUCKET}/{check_release(release)}/theme=places/type=place/*"


def connect_duckdb() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute("SET s3_region='us-west-2'")
    return con


def latest_release(con: duckdb.DuckDBPyConnection) -> str:
    row = con.execute(
        f"SELECT max(regexp_extract(file, 'release/([^/]+)/', 1)) FROM glob('{BUCKET}/*/theme=places/type=place/*')"
    ).fetchone()
    if not row or not row[0]:
        raise RuntimeError("Couldn't find any Overture places release in the public bucket.")
    return str(row[0])


def extract_query(source: str, release: str, region: OvertureRegion) -> str:
    """The SQL that pulls the region and flattens each place to one row."""
    box = bounding_box(region)
    lat, lon = region.center_lat, region.center_lon
    # Distance from the center in miles (the haversine formula), on each place's point.
    distance = (
        f"2 * {EARTH_RADIUS_MILES} * asin(sqrt("
        f"pow(sin(radians(bbox.ymin - {lat}) / 2), 2) + "
        f"cos(radians({lat})) * cos(radians(bbox.ymin)) * pow(sin(radians(bbox.xmin - {lon}) / 2), 2)))"
    )
    return f"""
        SELECT
            id                                   AS source_id,
            names.primary                        AS name,
            brand.names.primary                  AS brand,
            -- How many places in the region share this brand (a chain has several; 0 = no brand).
            CASE WHEN brand.names.primary IS NULL THEN 0
                 ELSE count(*) OVER (PARTITION BY brand.names.primary) END AS brand_locations,
            basic_category,
            taxonomy.primary                     AS taxonomy_primary,
            array_to_string(taxonomy.hierarchy, ' > ') AS taxonomy_hierarchy,
            confidence,
            websites[1]                          AS website,
            phones[1]                            AS phone,
            emails[1]                            AS email,
            addresses[1].freeform                AS address,
            addresses[1].locality                AS locality,
            addresses[1].region                  AS region,
            addresses[1].postcode                AS postcode,
            bbox.ymin                            AS lat,
            bbox.xmin                            AS lon,
            round({distance}, 2)                 AS distance_miles,
            '{release}'                          AS release
        FROM read_parquet('{source}')
        WHERE bbox.xmin BETWEEN {box.min_lon} AND {box.max_lon}
          AND bbox.ymin BETWEEN {box.min_lat} AND {box.max_lat}
          AND names.primary IS NOT NULL
          AND {distance} <= {region.radius_miles}
    """


def download(region: OvertureRegion, release: str | None = None, source: str | None = None,
             con: duckdb.DuckDBPyConnection | None = None) -> dict:
    """Pull the region into region.places_file. Returns the metadata it saved.

    `source` lets tests point at a small local file instead of Overture's bucket.
    """
    con = con or connect_duckdb()
    release = check_release(release or latest_release(con))
    source = source or places_source(release)
    out = Path(region.places_file)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp.parquet")
    con.execute(f"COPY ({extract_query(source, release, region)}) TO '{tmp.as_posix()}' (FORMAT PARQUET)")
    count = con.execute(f"SELECT count(*) FROM read_parquet('{tmp.as_posix()}')").fetchone()[0]
    tmp.replace(out)  # swap in only once the whole download worked
    meta = {"release": release, "region": region.name, "count": int(count), "downloaded_at": utc_now()}
    meta_path(region).write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return meta


def meta_path(region: OvertureRegion) -> Path:
    return Path(region.places_file).with_suffix(".meta.json")


def load_meta(region: OvertureRegion) -> dict | None:
    path = meta_path(region)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
