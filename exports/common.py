"""Formatting helpers shared by the Excel and PDF exporters."""
from __future__ import annotations

from datetime import datetime

from analytics.report_data import ReportData


def hms(seconds: float) -> str:
    """Exact duration as h:mm:ss (reports show exact figures, not rounded summaries)."""
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}"


def hours(seconds: float) -> float:
    return round(seconds / 3600, 4)


def local_clock(dt: datetime, with_date: bool = False) -> str:
    local = dt.astimezone()
    return local.strftime("%d %b %Y %H:%M:%S" if with_date else "%H:%M:%S")


def percent(part: float, whole: float) -> float:
    return 0.0 if whole <= 0 else part / whole


def usage_rate_text(totals) -> str:
    return "n/a" if totals.usage_rate is None else f"{totals.usage_rate:.1f}%"


def summary_rows(data: ReportData) -> list[tuple[str, float, str]]:
    """(label, seconds, explanation) rows shared by every summary table."""
    t = data.totals
    return [
        ("Active time", t.active, "Awake, unlocked, recent input"),
        ("Idle time", t.idle, "Awake, unlocked, no input beyond the idle threshold"),
        ("Locked time", t.locked, "Windows session locked"),
        ("Sleep / off time", t.sleep_off, "Computer unavailable"),
        ("Unmonitored time", t.unmonitored, "TimeLens was not recording"),
    ]


def reconciliation_text(data: ReportData) -> str:
    t = data.totals
    ok = "reconciles" if data.reconciles else "DOES NOT RECONCILE"
    return (f"Active + Idle + Locked + Sleep/Off + Unmonitored = {hms(t.accounted)}; "
            f"observed period = {hms(t.period_seconds)} ({ok}).")


NO_TIMELINE_NOTE = "No activity was recorded in this period."
