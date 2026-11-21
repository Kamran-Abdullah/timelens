"""Shared database vocabulary: timestamp format and category kinds.

All timestamps are stored as UTC text in ONE fixed format, e.g.
    2026-11-03T09:00:00.000000Z
Because the format is fixed-width, plain string comparison and sorting in SQLite is
chronologically correct, and indexes on the columns work for range queries.
"""
from __future__ import annotations

from datetime import datetime, timezone

TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"

CATEGORY_KINDS = ("productive", "neutral", "distraction", "personal", "custom")


def to_ts(dt: datetime) -> str:
    """Datetime -> stored UTC string. Naive datetimes are rejected to avoid timezone bugs."""
    if dt.tzinfo is None:
        raise ValueError("naive datetime: timestamps must be timezone-aware")
    return dt.astimezone(timezone.utc).strftime(TS_FORMAT)


def from_ts(text: str) -> datetime:
    """Stored UTC string -> aware UTC datetime (use .astimezone() to show local time)."""
    return datetime.strptime(text, TS_FORMAT).replace(tzinfo=timezone.utc)
