"""Document usage report: per (application, document) time and session counts."""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional

import pandas as pd

from analytics.app_reports import load_for_periods, overlap_seconds, usage_periods
from database.db_manager import Database

SORT_MODES = ["Most used", "Least used", "Application", "Duration"]
COLUMNS = ["document", "application", "today", "week", "month", "range_seconds",
           "sessions", "avg_session"]


def document_usage(db: Database, range_start: datetime, range_end: datetime,
                   application: Optional[str] = None, today: Optional[date] = None,
                   now: Optional[datetime] = None) -> pd.DataFrame:
    """One row per (document, application). Times are seconds.
      today / week / month .. fixed periods relative to `today`
      range_seconds ......... time inside the selected date filter
      sessions .............. tracked sessions touching the document inside the filter
      avg_session ........... range_seconds / sessions
    Only the document NAME is used; sessions with no detected document are excluded."""
    now = now or datetime.now(timezone.utc)
    today = today or datetime.now().date()
    periods = usage_periods(today)
    spans = load_for_periods(db, range_start, range_end, today, now)
    spans = spans[spans["document"].notna()]
    if application:
        spans = spans[spans["application"] == application]
    if spans.empty:
        return pd.DataFrame(columns=COLUMNS)

    df = spans[["session_id", "application", "document"]].copy()
    for name in ("today", "week", "month"):
        df[name] = overlap_seconds(spans, *periods[name])
    df["range_seconds"] = overlap_seconds(spans, range_start, range_end)
    df["range_session"] = df["session_id"].where(df["range_seconds"] > 0)
    grouped = df.groupby(["document", "application"], as_index=False).agg(
        today=("today", "sum"), week=("week", "sum"), month=("month", "sum"),
        range_seconds=("range_seconds", "sum"),
        sessions=("range_session", "nunique"))   # nunique ignores the NaNs (out of range)
    grouped = grouped[grouped["range_seconds"] > 0].copy()
    grouped["avg_session"] = grouped["range_seconds"] / grouped["sessions"].clip(lower=1)
    return grouped[COLUMNS].reset_index(drop=True)


def sort_documents(df: pd.DataFrame, mode: str) -> pd.DataFrame:
    if mode not in SORT_MODES:
        raise ValueError(mode)
    if df.empty:
        return df
    if mode == "Most used":
        keys, asc = ["range_seconds", "document"], [False, True]
    elif mode == "Least used":
        keys, asc = ["range_seconds", "document"], [True, True]
    elif mode == "Application":
        keys, asc = ["application", "range_seconds"], [True, False]
    else:  # "Duration": longest average session first
        keys, asc = ["avg_session", "document"], [False, True]
    return df.sort_values(keys, ascending=asc, ignore_index=True)
