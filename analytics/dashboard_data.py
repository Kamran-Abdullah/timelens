"""Everything the Dashboard shows, computed from SQLite in one call (run off the GUI thread)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

from analytics import time_calc as tc
from analytics.productivity import category_distribution
from database.db_manager import Database


@dataclass
class DashboardData:
    day: date
    totals: tc.Totals
    application_count: int
    document_count: int
    top_apps: list[tuple[str, float]]
    top_documents: list[tuple[str, str, float]]       # (application, document, seconds)
    distribution: dict[str, float]
    hourly: list[tuple[datetime, tc.Totals]]          # bucket start (UTC) + totals


def build_dashboard_data(db: Database, day: date, now: Optional[datetime] = None) -> DashboardData:
    start, end = tc.local_day_bounds(day)
    apps = tc.application_totals(db, start, end, now)
    docs = tc.document_totals(db, start, end, now=now)
    return DashboardData(
        day=day,
        totals=tc.get_totals(db, start, end, now),
        application_count=len(apps),
        document_count=len(docs),
        top_apps=apps[:5],
        top_documents=docs[:5],
        distribution=category_distribution(db, start, end, now),
        hourly=tc.bucket_totals(db, start, end, timedelta(hours=1), now),
    )
