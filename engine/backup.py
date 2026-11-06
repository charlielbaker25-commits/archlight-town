"""Database backups: `town backup`, plus one automatic backup the first time the Town starts each day.

It uses SQLite's own backup feature, which copies a consistent snapshot even
while agents are writing. A plain file copy could catch the database halfway
through a write and save a broken copy.

Backups are named by local date and time (town-2026-10-19-031245.db), so
"each day" means Charlie's day. Only the newest N are kept (N comes from
limits.yaml). Backups live outside git (the backups/ folder is ignored).
"""
import sqlite3
from datetime import datetime
from pathlib import Path

from engine.events import log_event

PREFIX = "town-"


def backup_database(conn: sqlite3.Connection, folder: Path, keep_last: int, now: datetime | None = None) -> Path:
    now = now or datetime.now()
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    dest_path = folder / f"{PREFIX}{now:%Y-%m-%d-%H%M%S}.db"
    dest = sqlite3.connect(dest_path)
    try:
        conn.backup(dest)
    finally:
        dest.close()
    log_event(conn, "librarian", "backup_created", {"path": str(dest_path), "bytes": dest_path.stat().st_size})
    _prune(conn, folder, keep_last)
    return dest_path


def ensure_daily_backup(conn: sqlite3.Connection, folder: Path, keep_last: int,
                        now: datetime | None = None) -> Path | None:
    """Back up once per local day. Returns the new file, or None if today's already exists."""
    now = now or datetime.now()
    if any(Path(folder).glob(f"{PREFIX}{now:%Y-%m-%d}-*.db")):
        return None
    return backup_database(conn, folder, keep_last, now)


def _prune(conn: sqlite3.Connection, folder: Path, keep_last: int) -> None:
    # The names sort by date and time, so the oldest come first.
    backups = sorted(folder.glob(f"{PREFIX}*.db"))
    old = backups[:-keep_last] if keep_last > 0 else []
    for path in old:
        path.unlink()
    if old:
        log_event(conn, "librarian", "backups_pruned", {"deleted": [p.name for p in old], "kept": keep_last})
