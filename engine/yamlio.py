"""Reading and writing YAML files with ruamel.yaml.

ruamel's "round-trip" mode keeps comments and layout when a file is saved again,
so an approved limit change doesn't wipe out the notes in limits.yaml.
"""
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML


def _yaml() -> YAML:
    yaml = YAML()  # round-trip mode by default
    yaml.preserve_quotes = True
    return yaml


def load_yaml(path: Path) -> Any:
    with Path(path).open(encoding="utf-8") as f:
        return _yaml().load(f)


def save_yaml(path: Path, data: Any) -> None:
    # Write to a temporary file first, then swap it in, so a crash mid-write
    # can never leave a half-written limits file behind.
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        _yaml().dump(data, f)
    tmp.replace(path)
