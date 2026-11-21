"""Backup and restore of the TimeLens database.

Backups use SQLite's online backup API, which produces a consistent copy even while
TimeLens is writing. Restore validates the file first and keeps a safety copy of the
current database, so a bad restore can be undone.
"""
from __future__ import annotations

import logging
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

import config
from database import migrations

log = logging.getLogger(__name__)


class BackupError(Exception):
    pass


def create_backup(db_path: Path | str, dest: Path | str | None = None) -> Path:
    db_path = Path(db_path)
    if dest is None:
        config.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        dest = config.BACKUP_DIR / f"timelens-{datetime.now():%Y%m%d-%H%M%S}.db"
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(db_path)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    log.info("Backup written to %s", dest)
    return dest


def validate_backup(path: Path | str) -> int:
    """Return the schema version if `path` is a healthy TimeLens database, else raise."""
    path = Path(path)
    if not path.is_file():
        raise BackupError(f"File not found: {path}")
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise BackupError("Backup failed the integrity check")
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "activity_sessions" not in tables or "settings" not in tables:
                raise BackupError("This is not a TimeLens database")
            version = migrations.current_version(conn)
        finally:
            conn.close()
    except sqlite3.DatabaseError as e:
        raise BackupError(f"Not a valid database: {e}") from e
    if version > migrations.latest_version():
        raise BackupError("Backup was made by a newer TimeLens version")
    return version


def restore_backup(backup_path: Path | str, db_path: Path | str) -> Path:
    """Replace the live database with a backup. The Database object must be closed first.
    Returns the path of the safety copy of the previous database."""
    backup_path, db_path = Path(backup_path), Path(db_path)
    validate_backup(backup_path)
    safety = db_path.with_name(f"{db_path.stem}.before-restore{db_path.suffix}")
    if db_path.exists():
        create_backup(db_path, safety)
    for suffix in ("-wal", "-shm"):  # stale journals must not be replayed onto the restored file
        Path(str(db_path) + suffix).unlink(missing_ok=True)
    shutil.copy2(backup_path, db_path)
    log.info("Restored %s (previous database saved as %s)", backup_path, safety)
    return safety
