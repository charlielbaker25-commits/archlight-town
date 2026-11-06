"""Per-instance settings: where this copy of the Town keeps its database, backups and state.

Each business runs as its own instance with its own instance.yaml. Settings hold
paths only. Limits never live here (they live in the pack's limits.yaml).
"""
from dataclasses import dataclass
from pathlib import Path

from engine.yamlio import load_yaml


@dataclass(frozen=True)
class OvertureRegion:
    name: str
    center_lat: float
    center_lon: float
    radius_miles: float
    places_file: Path


@dataclass(frozen=True)
class Settings:
    pack: str
    database: Path
    backup_folder: Path
    state_folder: Path
    overture: OvertureRegion
    # "real" (Charlie's business record) or "dev" (Claude Code's test copy). Missing means real.
    instance: str = "real"
    # Other instances' databases whose spend counts toward this instance's caps,
    # because every instance bills the same API key and the same monthly budget.
    budget_shared_with: tuple[Path, ...] = ()
    # The physical mailing address every commercial email must carry (CAN-SPAM): Example Web Studio's business address
    # (the operator's configured mailing address). "" = not set,
    # and the pack's Guard blocks every email until it is.
    mailing_address: str = ""

    @property
    def killswitch_file(self) -> Path:
        return self.state_folder / "killswitch.json"

    @property
    def paid_pause_file(self) -> Path:
        return self.state_folder / "paid_pause.json"


def load_settings(path: Path) -> Settings:
    """Read instance.yaml. Relative paths are resolved against the file's own folder."""
    path = Path(path).resolve()
    base = path.parent
    raw = load_yaml(path)

    def resolve(value: str) -> Path:
        p = Path(value)
        return p if p.is_absolute() else base / p

    ov = raw["overture"]
    return Settings(
        pack=str(raw["pack"]),
        database=resolve(raw["database"]),
        backup_folder=resolve(raw["backup_folder"]),
        state_folder=resolve(raw["state_folder"]),
        overture=OvertureRegion(
            name=str(ov["region_name"]),
            center_lat=float(ov["center_lat"]),
            center_lon=float(ov["center_lon"]),
            radius_miles=float(ov["radius_miles"]),
            places_file=resolve(ov["places_file"]),
        ),
        instance=str(raw.get("instance", "real")),
        budget_shared_with=tuple(resolve(p) for p in raw.get("budget_shared_with") or ()),
        mailing_address=str(raw.get("mailing_address") or "").strip(),
    )
