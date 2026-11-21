"""Everything a report contains, computed once from SQLite and shared by the Excel and
PDF exporters and by the Reports page preview (so all three always agree).

Every report reconciles: Active + Idle + Locked + Sleep/Off + Unmonitored equals the
length of the observed period (see ReportData.reconciles).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from analytics import productivity as pr
from analytics import time_calc as tc
from analytics.app_reports import application_usage
from analytics.document_reports import document_usage, sort_documents
from analytics.periods import preset_range
from analytics.timeline import TimelineEntry, build_timeline
from database.db_manager import Database

REPORT_PERIODS = ["Today", "Yesterday", "This week", "Last week", "This month",
                  "Last month", "Custom"]

# The report types the user can include (spec section 15).
SECTIONS = {
    "applications": "Application usage",
    "documents": "Document usage",
    "idle": "Idle time",
    "availability": "Computer availability",
    "locked": "Locked time",
    "productivity": "Productivity",
    "timeline": "Daily activity timeline",
}
MAX_DAYS = 400


@dataclass
class ReportData:
    title: str
    period_label: str
    start: datetime
    end: datetime
    first_day: date
    last_day: date
    generated_at: datetime
    totals: tc.Totals
    daily: list[tuple[date, tc.Totals]]
    applications: list[tuple[str, float]]                  # (application, seconds)
    documents: list[tuple[str, str, float, int]]           # (document, application, seconds, sessions)
    categories: list[dict]                                 # productivity breakdown
    score: pr.ScoreResult
    weights: dict[str, float]
    timeline: list[TimelineEntry]
    timeline_truncated: bool
    idle_periods: list[TimelineEntry] = field(default_factory=list)
    locked_periods: list[TimelineEntry] = field(default_factory=list)
    sections: tuple[str, ...] = tuple(SECTIONS)

    @property
    def reconciles(self) -> bool:
        return abs(self.totals.accounted - self.totals.period_seconds) < 0.5

    @property
    def active_total(self) -> float:
        return self.totals.active

    @property
    def date_text(self) -> str:
        if self.first_day == self.last_day:
            return f"{self.first_day:%A, %d %B %Y}"
        return f"{self.first_day:%d %b %Y} to {self.last_day:%d %b %Y}"

    @property
    def file_stem(self) -> str:
        label = self.period_label.replace(" ", "_")
        if self.first_day == self.last_day:
            return f"TimeLens_{label}_{self.first_day:%Y-%m-%d}"
        return f"TimeLens_{label}_{self.first_day:%Y-%m-%d}_to_{self.last_day:%Y-%m-%d}"


def build_report(db: Database, period: str, today: Optional[date] = None,
                 custom: Optional[tuple[date, date]] = None, now: Optional[datetime] = None,
                 sections: Optional[tuple[str, ...]] = None) -> ReportData:
    now = now or datetime.now(timezone.utc)
    today = today or datetime.now().date()
    start, end = preset_range(period, today, custom)
    first_day = start.astimezone().date()
    last_day = (end - timedelta(microseconds=1)).astimezone().date()
    if (last_day - first_day).days + 1 > MAX_DAYS:
        raise ValueError(f"Reports are limited to {MAX_DAYS} days; choose a shorter period.")

    totals = tc.get_totals(db, start, end, now)
    daily = []
    d = first_day
    while d <= last_day:
        daily.append((d, tc.get_day_totals(db, d, now)))
        d += timedelta(days=1)

    apps_df = application_usage(db, start, end, today, now)
    applications = [(r.application, float(r.total)) for r in apps_df.itertuples() if r.total > 0]
    docs_df = sort_documents(document_usage(db, start, end, today=today, now=now), "Most used")
    documents = [(r.document, r.application, float(r.range_seconds), int(r.sessions))
                 for r in docs_df.itertuples()]

    weights = pr.get_weights(db)
    categories = pr.category_breakdown(db, start, end, now)
    score = pr.compute_score(pr.seconds_by_kind(categories), weights)

    entries, truncated = build_timeline(db, start, end, now=now)
    return ReportData(
        title="TimeLens Activity Report", period_label=period, start=start, end=end,
        first_day=first_day, last_day=last_day, generated_at=now, totals=totals, daily=daily,
        applications=applications, documents=documents, categories=categories, score=score,
        weights=weights, timeline=entries, timeline_truncated=truncated,
        idle_periods=[e for e in entries if e.status == "IDLE"],
        locked_periods=[e for e in entries if e.status == "LOCKED"],
        sections=tuple(sections) if sections else tuple(SECTIONS))
