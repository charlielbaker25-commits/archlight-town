"""The sample-photo library: free-to-use photos that show a preview owner what their finished site looks like
with real pictures in it. Plain code, no model.

Where the photos live: packs/websites/photo_library/<folder>/ (salon, barber, dentist, ...), and
types.yaml says which business types each folder serves. Charlie downloads the photos himself
(this file never goes online); the shot list is in photo_library/README.md.

The license rule (Charlie, 2026-11-02): a photo is usable ONLY if photo_library/LICENSES.md has a row for
that exact file naming its source page, its author and a license that allows commercial use without asking
(the Unsplash License or the Pexels License), and the source page is on that license's own site. A photo
without a complete row is never copied into a build, whatever it shows. library_check() lists every problem
so `town photos` can show Charlie what's missing.

The labeling rule: every sample photo is labeled "Sample photo" on the page, and its file name starts
with "sample-photo-" so the facts check can find it in the built HTML (Astro keeps the name and adds a
hash) and fail any copy that isn't labeled. These photos never show the business itself, so they may
never pass for it.
"""
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from engine.yamlio import load_yaml

LIBRARY_DIR = Path(__file__).resolve().parent / "photo_library"
LICENSES_FILE = LIBRARY_DIR / "LICENSES.md"
TYPES_FILE = LIBRARY_DIR / "types.yaml"
# License name (lower case) -> the site its source page must be on.
ALLOWED_LICENSES = {"unsplash license": "unsplash.com", "pexels license": "pexels.com"}
PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".webp", ".png"}
SAMPLE_PREFIX = "sample-photo-"
SAMPLE_PHOTO_LABEL = "Sample photo"
WORKSPACE_DIR = Path("src") / "assets" / "sample-photos"
# Cells a row may not leave like this (an unfinished row is no license at all).
PLACEHOLDER = re.compile(r"^\s*(|-+|\?+|todo|tbd|n/?a|unknown|xxx+)\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class Photo:
    folder: str
    path: Path          # the file in the library
    shows: str          # what the photo shows, in plain words (from LICENSES.md)
    source: str         # the photo's own page on Unsplash or Pexels
    author: str
    license: str

    @property
    def credit(self) -> str:
        site = "Unsplash" if "unsplash" in self.license.lower() else "Pexels"
        return f"Photo by {self.author} on {site}"


def license_rows(path: Path = LICENSES_FILE) -> dict[str, dict[str, str]]:
    """LICENSES.md's table rows, keyed by file ("salon/salon-01-styling-station.jpg").
    Columns: File | Shows | Source page | Author | License."""
    rows: dict[str, dict[str, str]] = {}
    if not Path(path).is_file():
        return rows
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip().strip("`") for c in line.strip().strip("|").split("|")]
        if len(cells) != 5 or cells[0].lower() == "file" or set(cells[0]) <= {"-", ":", " "}:
            continue
        rows[cells[0].replace("\\", "/")] = dict(zip(("file", "shows", "source", "author", "license"), cells))
    return rows


def row_problems(row: dict[str, str] | None) -> list[str]:
    """Why a LICENSES.md row doesn't make its photo usable (empty = usable)."""
    if row is None:
        return ["no row in LICENSES.md"]
    out = [f"the {name} is missing" for name in ("shows", "source", "author", "license") if PLACEHOLDER.match(row[name])]
    site = ALLOWED_LICENSES.get(row["license"].strip().lower())
    if not PLACEHOLDER.match(row["license"]) and site is None:
        out.append(f"license \"{row['license']}\" isn't one we accept (Unsplash License or Pexels License)")
    host = (urlparse(row["source"]).hostname or "").lower()
    if site and not PLACEHOLDER.match(row["source"]) and not (host == site or host.endswith("." + site)):
        out.append(f"the source page ({row['source']}) isn't on {site}")
    if not PLACEHOLDER.match(row["source"]) and urlparse(row["source"]).scheme != "https":
        out.append("the source page must be an https:// link to the photo's own page")
    return out


def folders(path: Path = TYPES_FILE) -> dict[str, list[str]]:
    return {str(k): [str(t).lower() for t in (v or [])] for k, v in (load_yaml(path) or {}).items()}


def folder_for(business_type: str | None, types_file: Path = TYPES_FILE) -> str | None:
    wanted = (business_type or "").strip().lower()
    for folder, types in folders(types_file).items():
        if wanted and (wanted == folder or wanted in types):
            return folder
    return None


def library_check(library: Path = LIBRARY_DIR, max_kb: float | None = None) -> dict[str, Any]:
    """Every photo in the library and every LICENSES.md row, checked. Returns
    {"usable": {folder: [Photo]}, "problems": [...], "waiting": [rows with no file yet]}."""
    library = Path(library)
    rows = license_rows(library / "LICENSES.md")
    usable: dict[str, list[Photo]] = {f: [] for f in folders(library / "types.yaml")}
    problems: list[str] = []
    seen: set[str] = set()
    for folder in usable:
        for path in sorted((library / folder).glob("*")) if (library / folder).is_dir() else []:
            if not path.is_file() or path.name.lower() in ("readme.md", ".gitkeep"):
                continue
            key = f"{folder}/{path.name}"
            seen.add(key)
            if path.suffix.lower() not in PHOTO_EXTENSIONS:
                problems.append(f"{key}: not a photo file ({', '.join(sorted(PHOTO_EXTENSIONS))})")
                continue
            why = row_problems(rows.get(key))
            if max_kb is not None and path.stat().st_size > max_kb * 1024:
                why.append(f"{path.stat().st_size // 1024} KB is over {max_kb:g} KB (download a smaller size)")
            if why:
                problems.append(f"{key}: can't be used: {'; '.join(why)}")
                continue
            row = rows[key]
            usable[folder].append(Photo(folder, path, row["shows"], row["source"], row["author"], row["license"]))
    waiting = sorted(k for k in rows if k not in seen)
    return {"usable": usable, "problems": problems, "waiting": waiting}


def photos_for(business_type: str | None, limit: int, max_kb: float, library: Path = LIBRARY_DIR) -> list[Photo]:
    """The licensed photos for this type of business, at most `limit`. [] when the type has no folder or the
    folder has no licensed photos yet (the gallery then shows styled placeholder tiles)."""
    folder = folder_for(business_type, Path(library) / "types.yaml")
    if not folder or limit <= 0:
        return []
    return library_check(library, max_kb)["usable"].get(folder, [])[:limit]


def copy_into(workspace: Path, photos: list[Photo]) -> list[dict[str, str]]:
    """Copies the photos into the build's workspace as src/assets/sample-photos/sample-photo-<name>, and returns
    what brief.json says about each one."""
    target = Path(workspace) / WORKSPACE_DIR
    entries = []
    for photo in photos:
        target.mkdir(parents=True, exist_ok=True)
        name = SAMPLE_PREFIX + photo.path.name.lower()
        shutil.copy2(photo.path, target / name)
        entries.append({"file": (WORKSPACE_DIR / name).as_posix(), "shows": photo.shows,
                        "label_required": SAMPLE_PHOTO_LABEL, "credit": photo.credit})
    return entries
