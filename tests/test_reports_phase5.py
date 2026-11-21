from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

from analytics import time_calc as tc
from analytics.app_reports import application_usage
from analytics.document_reports import SORT_MODES, document_usage, sort_documents
from analytics.periods import PRESETS, preset_range
from database.db_manager import Database

TODAY = date(2026, 11, 4)                       # a Wednesday
NOW = tc.local_day_bounds(date(2026, 11, 20))[0]  # "now" is later, so nothing is clipped


def at(day: date, hours: float) -> datetime:
    return tc.local_day_bounds(day)[0] + timedelta(hours=hours)


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "r.db").open()
    add = d.add_closed_session
    X, W, C = ("Microsoft Excel", "EXCEL.EXE"), ("Microsoft Word", "WINWORD.EXE"), ("Google Chrome", "chrome.exe")
    add(at(date(2026, 10, 20), 9), at(date(2026, 10, 20), 10), "ACTIVE", *X, "Payroll.xlsx")
    add(at(date(2026, 11, 1), 9), at(date(2026, 11, 1), 9.5), "ACTIVE", *X, "Payroll.xlsx")
    add(at(date(2026, 11, 3), 9), at(date(2026, 11, 3), 10), "ACTIVE", *X, "Payroll.xlsx")
    add(at(date(2026, 11, 3), 11), at(date(2026, 11, 3), 11.25), "ACTIVE", *C)
    add(at(date(2026, 11, 4), 9), at(date(2026, 11, 4), 9 + 1 / 3), "ACTIVE", *X, "Payroll.xlsx")
    add(at(date(2026, 11, 4), 10), at(date(2026, 11, 4), 10 + 2 / 3), "ACTIVE", *X, "Bank.xlsx")
    add(at(date(2026, 11, 4), 12), at(date(2026, 11, 4), 12 + 1 / 6), "ACTIVE", *W, "Policy.docx")
    # crosses midnight Nov 3 -> Nov 4: 30 min on each day
    add(at(date(2026, 11, 3), 23.5), at(date(2026, 11, 4), 0.5), "ACTIVE", *X, "Bank.xlsx")
    add(at(date(2026, 11, 4), 14), at(date(2026, 11, 4), 15), "IDLE", "Idle")   # never counted
    yield d
    d.close()


def row(df, key, value):
    return df[df[key] == value].iloc[0]


def test_application_usage_columns_are_exact(db):
    lo, hi = preset_range("All time", TODAY)
    df = application_usage(db, lo, hi, TODAY, NOW)
    x = row(df, "application", "Microsoft Excel")
    assert (x.today, x.yesterday, x.week, x.month, x.total) == (5400, 5400, 10800, 12600, 16200)
    w = row(df, "application", "Microsoft Word")
    assert (w.today, w.total) == (600, 600)
    c = row(df, "application", "Google Chrome")
    assert (c.today, c.yesterday, c.week) == (0, 900, 900)
    assert list(df["application"]) == ["Microsoft Excel", "Google Chrome", "Microsoft Word"]
    assert "Idle" not in set(df["application"])     # idle time is not application usage


def test_midnight_crossing_session_is_split_between_days(db):
    df = document_usage(db, *preset_range("All time", TODAY), today=TODAY, now=NOW)
    bank = row(df, "document", "Bank.xlsx")
    assert bank.today == 2400 + 1800 and bank.week == 4200 + 1800 and bank.range_seconds == 6000


def test_date_filter_changes_total_only(db):
    lo, hi = preset_range("Last month", TODAY)       # October
    df = application_usage(db, lo, hi, TODAY, NOW)
    x = row(df, "application", "Microsoft Excel")
    assert x.total == 3600                            # only the Oct-20 session
    assert x.today == 5400                            # fixed columns unaffected
    chrome = row(df, "application", "Google Chrome")
    assert chrome.total == 0 and chrome.yesterday == 900   # still listed: it has other periods


def test_document_usage_exact(db):
    df = document_usage(db, *preset_range("All time", TODAY), today=TODAY, now=NOW)
    p = row(df, "document", "Payroll.xlsx")
    assert (p.today, p.week, p.month, p.range_seconds, p.sessions) == (1200, 4800, 6600, 10200, 4)
    b = row(df, "document", "Bank.xlsx")
    assert b.sessions == 3 - 1 and b.avg_session == 3000     # two sessions: 40 min + 60 min
    assert set(df["document"]) == {"Payroll.xlsx", "Bank.xlsx", "Policy.docx"}
    assert len(df) == 3


def test_document_filter_by_date_and_application(db):
    df = document_usage(db, *preset_range("Yesterday", TODAY), today=TODAY, now=NOW)
    assert set(df["document"]) == {"Payroll.xlsx", "Bank.xlsx"}
    assert row(df, "document", "Bank.xlsx").range_seconds == 1800
    assert row(df, "document", "Bank.xlsx").sessions == 1
    only_word = document_usage(db, *preset_range("All time", TODAY),
                               application="Microsoft Word", today=TODAY, now=NOW)
    assert list(only_word["document"]) == ["Policy.docx"]


def test_sort_modes(db):
    df = document_usage(db, *preset_range("All time", TODAY), today=TODAY, now=NOW)
    assert list(sort_documents(df, "Most used")["document"]) == ["Payroll.xlsx", "Bank.xlsx", "Policy.docx"]
    assert list(sort_documents(df, "Least used")["document"]) == ["Policy.docx", "Bank.xlsx", "Payroll.xlsx"]
    assert list(sort_documents(df, "Application")["application"]) == [
        "Microsoft Excel", "Microsoft Excel", "Microsoft Word"]
    # average session: Bank 3000, Payroll 2550, Policy 600
    assert list(sort_documents(df, "Duration")["document"]) == ["Bank.xlsx", "Payroll.xlsx", "Policy.docx"]
    assert set(SORT_MODES) == {"Most used", "Least used", "Application", "Duration"}


def test_empty_database_gives_empty_frames(tmp_path):
    d = Database(tmp_path / "e.db").open()
    lo, hi = preset_range("All time", TODAY)
    assert application_usage(d, lo, hi, TODAY, NOW).empty
    assert document_usage(d, lo, hi, today=TODAY, now=NOW).empty
    d.close()


def test_excel_document_total_equals_application_total_when_every_session_has_a_doc(db):
    lo, hi = preset_range("All time", TODAY)
    apps = application_usage(db, lo, hi, TODAY, NOW)
    docs = document_usage(db, lo, hi, today=TODAY, now=NOW)
    assert docs[docs["application"] == "Microsoft Excel"]["range_seconds"].sum() == \
        row(apps, "application", "Microsoft Excel").total


@pytest.mark.parametrize("name,first,last", [
    ("Today", date(2026, 11, 4), date(2026, 11, 4)),
    ("Yesterday", date(2026, 11, 3), date(2026, 11, 3)),
    ("This week", date(2026, 11, 2), date(2026, 11, 4)),
    ("Last week", date(2026, 10, 26), date(2026, 11, 1)),
    ("This month", date(2026, 11, 1), date(2026, 11, 4)),
    ("Last month", date(2026, 10, 1), date(2026, 10, 31)),
])
def test_preset_ranges(name, first, last):
    start, end = preset_range(name, TODAY)
    assert start == tc.local_day_bounds(first)[0]
    assert end == tc.local_day_bounds(last)[1]


def test_custom_range_and_january_last_month():
    s, e = preset_range("Custom", TODAY, (date(2026, 11, 3), date(2026, 11, 1)))   # reversed
    assert s == tc.local_day_bounds(date(2026, 11, 1))[0] and e == tc.local_day_bounds(date(2026, 11, 3))[1]
    s, e = preset_range("Last month", date(2026, 1, 15))
    assert s == tc.local_day_bounds(date(2025, 12, 1))[0] and e == tc.local_day_bounds(date(2025, 12, 31))[1]
    with pytest.raises(ValueError):
        preset_range("Custom", TODAY)
    assert "Custom" in PRESETS
