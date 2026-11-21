"""Application usage report (Today / Yesterday / This week / This month / Total).

Built with pandas: all ACTIVE time spans are loaded once, then clipped against each
period. A span that crosses midnight is split correctly between days.
"Total" is the total inside the selected date filter (all time by default).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Optional

import pandas as pd

from analytics.periods import month_start, week_start
from analytics.time_calc import clipped_sessions, local_day_bounds
from database.db_manager import Database

SPAN_COLUMNS = ["session_id", "application", "executable", "document", "start", "end"]
USAGE_COLUMNS = ["application", "today", "yesterday", "week", "month", "total"]


def load_active_spans(db: Database, start: datetime, end: datetime,
                      now: Optional[datetime] = None) -> pd.DataFrame:
    """One row per ACTIVE session piece inside [start, end), clipped, never overlapping."""
    rows = [(row["id"], row["application_name"], row["executable_name"], row["document_name"],
             s, e)
            for row, s, e in clipped_sessions(db, start, end, now)
            if row["activity_status"] == "ACTIVE"]
    df = pd.DataFrame(rows, columns=SPAN_COLUMNS)
    df["start"] = pd.to_datetime(df["start"], utc=True)
    df["end"] = pd.to_datetime(df["end"], utc=True)
    return df


def overlap_seconds(df: pd.DataFrame, start: datetime, end: datetime) -> pd.Series:
    """Seconds of each span that fall inside [start, end)."""
    s = df["start"].clip(lower=pd.Timestamp(start))
    e = df["end"].clip(upper=pd.Timestamp(end))
    return (e - s).dt.total_seconds().clip(lower=0)


def usage_periods(today: date) -> dict[str, tuple[datetime, datetime]]:
    """The fixed report columns, as UTC ranges, relative to the local date `today`."""
    y = today - timedelta(days=1)
    day_end = local_day_bounds(today)[1]
    return {
        "today": local_day_bounds(today),
        "yesterday": local_day_bounds(y),
        "week": (local_day_bounds(week_start(today))[0], day_end),
        "month": (local_day_bounds(month_start(today))[0], day_end),
    }


def load_for_periods(db: Database, range_start: datetime, range_end: datetime,
                     today: date, now: datetime) -> pd.DataFrame:
    """Load every span any report column needs, in a single pass over the database."""
    periods = usage_periods(today)
    lo = min([range_start] + [p[0] for p in periods.values()])
    hi = max([range_end] + [p[1] for p in periods.values()])
    return load_active_spans(db, lo, hi, now)


def application_usage(db: Database, range_start: datetime, range_end: datetime,
                      today: Optional[date] = None, now: Optional[datetime] = None
                      ) -> pd.DataFrame:
    """Columns: application, today, yesterday, week, month, total (seconds),
    most used (by total) first. Applications with no time anywhere are omitted."""
    now = now or datetime.now(timezone.utc)
    today = today or datetime.now().date()
    spans = load_for_periods(db, range_start, range_end, today, now)
    if spans.empty:
        return pd.DataFrame(columns=USAGE_COLUMNS)
    out = pd.DataFrame({"application": spans["application"]})
    for name, (a, b) in usage_periods(today).items():
        out[name] = overlap_seconds(spans, a, b)
    out["total"] = overlap_seconds(spans, range_start, range_end)
    table = out.groupby("application", as_index=False).sum(numeric_only=True)
    table = table[table[USAGE_COLUMNS[1:]].sum(axis=1) > 0]
    return table.sort_values(["total", "application"], ascending=[False, True],
                             ignore_index=True)[USAGE_COLUMNS]
