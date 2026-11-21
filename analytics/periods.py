"""Named reporting periods in LOCAL calendar terms, returned as UTC [start, end) ranges.

Weeks start on Monday. All functions take `today` (a local date) so tests can fix it.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from analytics.time_calc import local_day_bounds

PRESETS = ["All time", "Today", "Yesterday", "This week", "Last week",
           "This month", "Last month", "Custom"]

_EPOCH = datetime(2000, 1, 1, tzinfo=timezone.utc)
_FAR = datetime(2100, 1, 1, tzinfo=timezone.utc)


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def month_start(d: date) -> date:
    return d.replace(day=1)


def range_for_days(first: date, last_inclusive: date) -> tuple[datetime, datetime]:
    return local_day_bounds(first)[0], local_day_bounds(last_inclusive)[1]


def preset_range(name: str, today: date, custom: tuple[date, date] | None = None
                 ) -> tuple[datetime, datetime]:
    if name == "All time":
        return _EPOCH, _FAR
    if name == "Today":
        return range_for_days(today, today)
    if name == "Yesterday":
        y = today - timedelta(days=1)
        return range_for_days(y, y)
    if name == "This week":
        return range_for_days(week_start(today), today)
    if name == "Last week":
        ws = week_start(today) - timedelta(days=7)
        return range_for_days(ws, ws + timedelta(days=6))
    if name == "This month":
        return range_for_days(month_start(today), today)
    if name == "Last month":
        last_end = month_start(today) - timedelta(days=1)
        return range_for_days(month_start(last_end), last_end)
    if name == "Custom":
        if custom is None:
            raise ValueError("custom range needs two dates")
        a, b = sorted(custom)
        return range_for_days(a, b)
    raise ValueError(f"unknown period: {name}")
