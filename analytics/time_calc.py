"""Reliable time totals computed from activity_sessions.

Rules (see the project spec, section 16)
  Available Time = Active + Idle
  Usage Rate     = Active / Available x 100
  Unmonitored    = observed period minus every second that has a recorded session

How non-overlap is guaranteed
  Sessions are clipped to the requested window and "swept" in start order; any second that
  an earlier row already claimed is never counted again. So even if bad data ever contained
  overlapping rows, each second is allocated to at most one category and the categories
  always add up to the window length (recorded + unmonitored).

Day boundaries use the machine's LOCAL timezone, converted to UTC for querying. Days that
are 23 or 25 hours long (daylight saving) are therefore handled correctly.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Iterator, Optional

from database.db_manager import Database
from database.models import from_ts, to_ts

STATUSES = ("ACTIVE", "IDLE", "LOCKED", "SLEEP_OFF", "UNMONITORED")


@dataclass
class Totals:
    active: float = 0.0
    idle: float = 0.0
    locked: float = 0.0
    sleep_off: float = 0.0
    unmonitored: float = 0.0   # includes explicitly stored UNMONITORED rows + uncovered gaps
    period_seconds: float = 0.0  # length of the observed window (clipped to "now")

    @property
    def available(self) -> float:
        return self.active + self.idle

    @property
    def usage_rate(self) -> Optional[float]:
        """Percent, or None when there is no available time (avoid dividing by zero)."""
        return None if self.available <= 0 else self.active / self.available * 100.0

    @property
    def accounted(self) -> float:
        return self.active + self.idle + self.locked + self.sleep_off + self.unmonitored


def local_day_bounds(day: date) -> tuple[datetime, datetime]:
    """[start, end) of a local calendar day as aware UTC datetimes."""
    start = datetime(day.year, day.month, day.day).astimezone()          # local midnight
    nxt = day + timedelta(days=1)
    end = datetime(nxt.year, nxt.month, nxt.day).astimezone()
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def clipped_sessions(db: Database, start: datetime, end: datetime,
                     now: Optional[datetime] = None) -> Iterator[tuple]:
    """Yield (row, clipped_start, clipped_end) with no two yielded spans overlapping."""
    now = now or _utcnow()
    end = min(end, now)
    if end <= start:
        return
    rows = db.query(
        "SELECT * FROM activity_sessions WHERE start_timestamp < ? "
        "AND (end_timestamp IS NULL OR end_timestamp > ?) "
        "ORDER BY start_timestamp, id", (to_ts(end), to_ts(start)))
    cursor = start
    for row in rows:
        s = max(from_ts(row["start_timestamp"]), cursor)
        e = min(from_ts(row["end_timestamp"]) if row["end_timestamp"] else now, end)
        if e <= s:
            continue
        yield row, s, e
        cursor = e


def get_totals(db: Database, start: datetime, end: datetime,
               now: Optional[datetime] = None) -> Totals:
    now = now or _utcnow()
    eff_end = min(end, now)
    t = Totals(period_seconds=max(0.0, (eff_end - start).total_seconds()))
    if t.period_seconds == 0:
        return t
    recorded = 0.0
    for row, s, e in clipped_sessions(db, start, end, now):
        secs = (e - s).total_seconds()
        recorded += secs
        status = row["activity_status"]
        if status == "ACTIVE":
            t.active += secs
        elif status == "IDLE":
            t.idle += secs
        elif status == "LOCKED":
            t.locked += secs
        elif status == "SLEEP_OFF":
            t.sleep_off += secs
        else:
            t.unmonitored += secs
    t.unmonitored += max(0.0, t.period_seconds - recorded)  # time nothing was recorded
    return t


def get_day_totals(db: Database, day: date, now: Optional[datetime] = None) -> Totals:
    return get_totals(db, *local_day_bounds(day), now=now)


def application_totals(db: Database, start: datetime, end: datetime,
                       now: Optional[datetime] = None) -> list[tuple[str, float]]:
    """ACTIVE seconds per application, most used first."""
    acc: dict[str, float] = defaultdict(float)
    for row, s, e in clipped_sessions(db, start, end, now):
        if row["activity_status"] == "ACTIVE":
            acc[row["application_name"]] += (e - s).total_seconds()
    return sorted(acc.items(), key=lambda kv: -kv[1])


def document_totals(db: Database, start: datetime, end: datetime,
                    application_name: Optional[str] = None,
                    now: Optional[datetime] = None) -> list[tuple[str, str, float]]:
    """ACTIVE seconds per (application, document), most used first."""
    acc: dict[tuple[str, str], float] = defaultdict(float)
    for row, s, e in clipped_sessions(db, start, end, now):
        if row["activity_status"] != "ACTIVE" or not row["document_name"]:
            continue
        if application_name and row["application_name"] != application_name:
            continue
        acc[(row["application_name"], row["document_name"])] += (e - s).total_seconds()
    return sorted(((a, d, v) for (a, d), v in acc.items()), key=lambda x: -x[2])


def rebuild_daily_summary(db: Database, day: date, now: Optional[datetime] = None) -> Totals:
    """Recompute and store the cached summary for one local day."""
    now = now or _utcnow()
    start, end = local_day_bounds(day)
    t = get_totals(db, start, end, now)
    apps = {a for a, _ in application_totals(db, start, end, now)}
    docs = {(a, d) for a, d, _ in document_totals(db, start, end, now=now)}
    with db.transaction() as c:
        c.execute(
            "INSERT INTO daily_summaries(day, active_seconds, idle_seconds, locked_seconds,"
            " sleep_off_seconds, unmonitored_seconds, application_count, document_count,"
            " updated_at) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(day) DO UPDATE SET"
            " active_seconds=excluded.active_seconds, idle_seconds=excluded.idle_seconds,"
            " locked_seconds=excluded.locked_seconds, sleep_off_seconds=excluded.sleep_off_seconds,"
            " unmonitored_seconds=excluded.unmonitored_seconds,"
            " application_count=excluded.application_count,"
            " document_count=excluded.document_count, updated_at=excluded.updated_at",
            (day.isoformat(), t.active, t.idle, t.locked, t.sleep_off, t.unmonitored,
             len(apps), len(docs), to_ts(now)))
    return t


def bucket_totals(db: Database, start: datetime, end: datetime,
                  step: timedelta = timedelta(hours=1),
                  now: Optional[datetime] = None) -> list[tuple[datetime, Totals]]:
    """Split [start, end) into equal buckets (default hourly) with per-bucket Totals.
    One pass over the sessions; each second lands in exactly one bucket."""
    now = now or _utcnow()
    edges = []
    t = start
    while t < end:
        edges.append(t)
        t += step
    buckets = [Totals() for _ in edges]
    for i, b_start in enumerate(edges):
        b_end = min(b_start + step, end)
        buckets[i].period_seconds = max(0.0, (min(b_end, now) - b_start).total_seconds())
    for row, s, e in clipped_sessions(db, start, end, now):
        i = int((s - start) / step)
        while i < len(edges) and edges[i] < e:
            lo, hi = max(s, edges[i]), min(e, edges[i] + step)
            secs = max(0.0, (hi - lo).total_seconds())
            b = buckets[i]
            status = row["activity_status"]
            if status == "ACTIVE":
                b.active += secs
            elif status == "IDLE":
                b.idle += secs
            elif status == "LOCKED":
                b.locked += secs
            elif status == "SLEEP_OFF":
                b.sleep_off += secs
            else:
                b.unmonitored += secs
            i += 1
    for b in buckets:
        b.unmonitored += max(0.0, b.period_seconds - (b.active + b.idle + b.locked + b.sleep_off
                                                      + b.unmonitored))
    return list(zip(edges, buckets))
