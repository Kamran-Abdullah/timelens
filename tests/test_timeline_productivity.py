from datetime import date, datetime, timedelta, timezone

import pytest

from analytics import productivity as pr
from analytics import time_calc as tc
from analytics.periods import range_for_days
from analytics.timeline import application_names, build_timeline
from database.db_manager import Database

DAY = date(2026, 10, 14)
TODAY = date(2026, 10, 21)       # Wednesday
NOW = tc.local_day_bounds(date(2026, 11, 1))[0]


def at(day, hours):
    return tc.local_day_bounds(day)[0] + timedelta(hours=hours)


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t6.db").open()
    yield d
    d.close()


X = ("Microsoft Excel", "EXCEL.EXE")
C = ("Google Chrome", "chrome.exe")
T = ("Microsoft Teams", "ms-teams.exe")


def seed_day(d):
    a = d.add_closed_session
    a(at(DAY, 9), at(DAY, 9.75), "ACTIVE", *X, "October Payroll.xlsx")
    a(at(DAY, 9.75), at(DAY, 10.25), "ACTIVE", *X, "Bank Reconciliation.xlsx")
    a(at(DAY, 10.25), at(DAY, 10.5), "ACTIVE", *C)
    a(at(DAY, 10.5), at(DAY, 10.8333333333), "IDLE", "Idle")
    # 10:50 -> 11:00 nothing recorded (TimeLens not running)
    a(at(DAY, 11), at(DAY, 11.5), "ACTIVE", *T)
    a(at(DAY, 11.5), at(DAY, 12), "LOCKED", "Windows", "LockApp.exe")
    a(at(DAY, 12), at(DAY, 12.5), "SLEEP_OFF", "Sleep")


# ---------------------------------------------------------------- timeline
def test_timeline_entries_in_order_with_gap_marked(db):
    seed_day(db)
    entries, truncated = build_timeline(db, *range_for_days(DAY, DAY), now=NOW)
    assert not truncated
    kinds = [(e.label, e.status) for e in entries]
    assert kinds == [
        ("October Payroll.xlsx", "ACTIVE"), ("Bank Reconciliation.xlsx", "ACTIVE"),
        ("Google Chrome", "ACTIVE"), ("Idle", "IDLE"), ("Unmonitored", "UNMONITORED"),
        ("Microsoft Teams", "ACTIVE"), ("Locked", "LOCKED"), ("Sleep / Off", "SLEEP_OFF"),
        ("Unmonitored", "UNMONITORED")]            # after 12:30 until end of day
    assert entries[0].detail == "Microsoft Excel" and entries[3].detail == "No user input"
    assert entries[0].seconds == 2700
    gap = entries[4]
    assert gap.start == at(DAY, 10.8333333333) and gap.end == at(DAY, 11)
    for a, b in zip(entries, entries[1:]):          # contiguous: nothing missing, nothing doubled
        assert a.end == b.start


def test_timeline_nothing_before_first_session(db):
    seed_day(db)
    entries, _ = build_timeline(db, *range_for_days(DAY - timedelta(days=3), DAY), now=NOW)
    assert entries[0].start == at(DAY, 9)             # no invented unmonitored time before 09:00


def test_timeline_open_ended_range_runs_to_now_only(db):
    seed_day(db)
    now = at(DAY, 13)
    entries, _ = build_timeline(db, *range_for_days(DAY, DAY), now=now)
    assert entries[-1].status == "UNMONITORED" and entries[-1].end == now


def test_timeline_application_filter(db):
    seed_day(db)
    entries, _ = build_timeline(db, *range_for_days(DAY, DAY), application="Microsoft Excel", now=NOW)
    assert [e.label for e in entries] == ["October Payroll.xlsx", "Bank Reconciliation.xlsx"]
    assert all(e.status == "ACTIVE" for e in entries)
    assert application_names(db) == ["Google Chrome", "Microsoft Excel", "Microsoft Teams"]


def test_timeline_merges_contiguous_identical_entries_and_truncates(db):
    for i in range(10):
        db.add_closed_session(at(DAY, 9 + i * 0.1), at(DAY, 9 + (i + 1) * 0.1), "ACTIVE", *X, "A.xlsx")
    entries, _ = build_timeline(db, *range_for_days(DAY, DAY), now=NOW)
    assert len([e for e in entries if e.status == "ACTIVE"]) == 1
    for i in range(10):
        db.add_closed_session(at(DAY, 14 + i * 0.1), at(DAY, 14 + i * 0.1 + 0.05), "IDLE", "Idle")
    entries, truncated = build_timeline(db, *range_for_days(DAY, DAY), now=NOW, max_rows=3)
    assert truncated and len(entries) == 3


def test_timeline_empty_database(db):
    assert build_timeline(db, *range_for_days(DAY, DAY), now=NOW) == ([], False)


# ---------------------------------------------------------- productivity
def cat_id(db, kind):
    return next(c["id"] for c in pr.list_categories(db) if c["kind"] == kind)


def seed_categories(db, day=DAY):
    a = db.add_closed_session
    a(at(day, 9), at(day, 12), "ACTIVE", *X, "A.xlsx")        # 3h productive
    a(at(day, 12), at(day, 13), "ACTIVE", *T)                  # 1h neutral
    a(at(day, 13), at(day, 14), "ACTIVE", *C)                  # 1h distraction
    a(at(day, 14), at(day, 16), "ACTIVE", "Notepad", "notepad.exe")   # 2h uncategorized
    pr.set_app_category(db, "EXCEL.EXE", cat_id(db, "productive"))
    pr.set_app_category(db, "ms-teams.exe", cat_id(db, "neutral"))
    pr.set_app_category(db, "chrome.exe", cat_id(db, "distraction"))


def test_nothing_is_categorized_by_default(db):
    seed_day(db)
    by_kind = pr.category_distribution(db, *range_for_days(DAY, DAY), now=NOW)
    assert set(by_kind) == {"uncategorized"}


def test_breakdown_and_exact_score(db):
    seed_categories(db)
    rows = pr.category_breakdown(db, *range_for_days(DAY, DAY), now=NOW)
    got = {r["name"]: r["seconds"] for r in rows}
    assert got == {"Productive": 10800, "Neutral": 3600, "Distraction": 3600, "Uncategorized": 7200}
    by_kind = pr.seconds_by_kind(rows)
    res = pr.compute_score(by_kind, pr.get_weights(db))
    # (3h*100 + 1h*50 + 1h*0) / 5h = 70
    assert res.score == pytest.approx(70.0)
    assert res.categorized_seconds == 18000 and res.total_seconds == 25200
    assert res.coverage == pytest.approx(18000 / 25200 * 100)
    text = " ".join(res.lines)
    assert "Productive: 3.00h x 100%" in text and "= 70%" in text and "uncategorized" in text


def test_weights_are_configurable_and_validated(db):
    seed_categories(db)
    by_kind = pr.seconds_by_kind(pr.category_breakdown(db, *range_for_days(DAY, DAY), now=NOW))
    pr.set_weight(db, "neutral", 100)
    pr.set_weight(db, "distraction", 20)
    assert pr.compute_score(by_kind, pr.get_weights(db)).score == pytest.approx((300 + 100 + 20) / 5)
    with pytest.raises(ValueError):
        pr.set_weight(db, "productive", 120)
    with pytest.raises(ValueError):
        pr.set_weight(db, "bogus", 10)


def test_score_none_when_nothing_categorized_or_no_activity(db):
    seed_day(db)
    by_kind = pr.category_distribution(db, *range_for_days(DAY, DAY), now=NOW)
    r = pr.compute_score(by_kind, pr.get_weights(db))
    assert r.score is None and "Assign categories" in r.lines[0]
    assert pr.compute_score({}, pr.get_weights(db)).lines == ["No active time in this period."]


def test_custom_categories(db):
    seed_categories(db)
    cid = pr.add_category(db, "Client work")
    pr.set_app_category(db, "notepad.exe", cid)
    rows = pr.category_breakdown(db, *range_for_days(DAY, DAY), now=NOW)
    assert {"name": "Client work", "kind": "custom", "seconds": 7200} in rows
    with pytest.raises(ValueError):
        pr.add_category(db, "client WORK")
    with pytest.raises(ValueError):
        pr.add_category(db, "  ")
    with pytest.raises(ValueError):
        pr.add_category(db, "Uncategorized")
    pr.delete_category(db, cid)                      # apps fall back to uncategorized
    assert [a for a in pr.list_applications(db) if a["executable_name"] == "notepad.exe"][0]["category_id"] is None
    with pytest.raises(ValueError):
        pr.delete_category(db, cat_id(db, "productive"))
    with pytest.raises(KeyError):
        pr.set_app_category(db, "nope.exe", None)


def test_idle_locked_and_other_states_never_count_as_productive(db):
    seed_categories(db)
    db.add_closed_session(at(DAY, 16), at(DAY, 20), "IDLE", "Idle")
    db.add_closed_session(at(DAY, 20), at(DAY, 22), "LOCKED", "Windows", "LockApp.exe")
    total = sum(r["seconds"] for r in pr.category_breakdown(db, *range_for_days(DAY, DAY), now=NOW))
    assert total == 25200


def test_comparisons_today_vs_yesterday_and_week_vs_last_week(db):
    seed_categories(db, TODAY)                             # Wed 21st
    seed_categories(db, TODAY - timedelta(days=1))         # Tue 20th: same pattern
    seed_categories(db, date(2026, 10, 13))                # last week's Tuesday
    # last week's Thursday must NOT be in the "same days" comparison (Mon-Wed only)
    seed_categories(db, date(2026, 10, 15))
    (today, yday), (week, last_week) = pr.comparisons(db, TODAY, now=NOW)
    assert today.label == "Today" and yday.label == "Yesterday"
    assert today.active_seconds == yday.active_seconds == 25200
    assert today.score.score == pytest.approx(70.0)
    assert week.active_seconds == 2 * 25200
    assert last_week.active_seconds == 25200


def test_daily_series_has_seven_days_oldest_first(db):
    seed_categories(db, TODAY)
    series = pr.daily_series(db, TODAY, 7, now=NOW)
    assert [d for d, _ in series] == [TODAY - timedelta(days=i) for i in range(6, -1, -1)]
    assert series[-1][1]["productive"] == 10800 and series[0][1] == {}
