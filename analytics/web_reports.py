"""Reads of the optional web_visits table (empty unless the user opted in to website tracking)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from database.db_manager import Database
from database.models import from_ts, to_ts


@dataclass
class WebSummary:
    visits: int
    sites: int
    searches: int
    seconds: float


def web_summary(db: Database, start: datetime, end: datetime) -> WebSummary:
    r = db.query("SELECT COUNT(*) v, COUNT(DISTINCT domain) s, SUM(search_query IS NOT NULL) q, "
                 "COALESCE(SUM(duration_seconds), 0) t FROM web_visits WHERE visit_time >= ? AND visit_time < ?",
                 (to_ts(start), to_ts(end)))[0]
    return WebSummary(r["v"], r["s"], int(r["q"] or 0), float(r["t"]))


def top_sites(db: Database, start: datetime, end: datetime, limit: int = 300
              ) -> list[tuple[str, int, float, datetime]]:
    """(domain, visits, seconds open, last visit), most time first (then most visits)."""
    rows = db.query("SELECT domain, COUNT(*) v, SUM(duration_seconds) t, MAX(visit_time) last FROM web_visits "
                    "WHERE visit_time >= ? AND visit_time < ? GROUP BY domain "
                    "ORDER BY t DESC, v DESC, domain LIMIT ?", (to_ts(start), to_ts(end), limit))
    return [(r["domain"], r["v"], float(r["t"] or 0), from_ts(r["last"])) for r in rows]


def search_history(db: Database, start: datetime, end: datetime, limit: int = 500
                   ) -> list[tuple[datetime, str, str]]:
    """(time, search words, search site), newest first."""
    rows = db.query("SELECT visit_time, search_query, domain FROM web_visits WHERE search_query IS NOT NULL "
                    "AND visit_time >= ? AND visit_time < ? ORDER BY visit_time DESC LIMIT ?",
                    (to_ts(start), to_ts(end), limit))
    return [(from_ts(r["visit_time"]), r["search_query"], r["domain"]) for r in rows]


def recent_visits(db: Database, start: datetime, end: datetime, limit: int = 500
                  ) -> list[tuple[datetime, str, str, float]]:
    """(time, domain, page title, seconds open), newest first."""
    rows = db.query("SELECT visit_time, domain, title, duration_seconds FROM web_visits "
                    "WHERE visit_time >= ? AND visit_time < ? ORDER BY visit_time DESC LIMIT ?",
                    (to_ts(start), to_ts(end), limit))
    return [(from_ts(r["visit_time"]), r["domain"], r["title"], float(r["duration_seconds"])) for r in rows]
