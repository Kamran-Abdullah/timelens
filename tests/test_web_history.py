import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from analytics import time_calc as tc
from analytics import web_reports as wr
from database import migrations
from database.db_manager import Database
from database.models import to_ts
from services import web_history as wh
from services.web_history import history_sources as real_history_sources   # conftest replaces wh.history_sources

NOW = datetime(2026, 10, 21, 15, 0, tzinfo=timezone.utc)


def at(hours_ago: float = 0, days_ago: float = 0) -> datetime:
    return NOW - timedelta(hours=hours_ago, days=days_ago)


def make_history(path: Path, visits: list[tuple]) -> Path:
    """A Chrome-style History file. visits: (url, title, datetime, seconds_open)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE urls (id INTEGER PRIMARY KEY, url TEXT, title TEXT, visit_count INTEGER, "
                 "typed_count INTEGER, last_visit_time INTEGER, hidden INTEGER)")
    conn.execute("CREATE TABLE visits (id INTEGER PRIMARY KEY AUTOINCREMENT, url INTEGER, visit_time INTEGER, "
                 "from_visit INTEGER, transition INTEGER, segment_id INTEGER, visit_duration INTEGER)")
    for i, (url, title, when, secs) in enumerate(visits, 1):
        conn.execute("INSERT INTO urls VALUES (?,?,?,1,0,0,0)", (i, url, title))
        conn.execute("INSERT INTO visits(url, visit_time, visit_duration) VALUES (?,?,?)",
                     (i, wh.datetime_to_webkit(when), None if secs is None else int(secs * 1_000_000)))
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "w.db").open()
    yield d
    d.close()


def tracking(db, days=0):
    wh.enable_tracking(db, days, now=NOW - timedelta(days=2))     # tracking switched on two days ago


VISITS = [
    ("https://www.github.com/myorg/secret-repo/settings?token=SECRET123", "Settings - secret-repo", at(3), 600),
    ("https://github.com/explore", "Explore GitHub", at(2.5), 120),
    ("https://www.google.com/search?q=payroll+software+pakistan&oq=x", "payroll software pakistan - Google Search", at(2), 30),
    ("https://news.example.org/a/b/c?utm=1", "Big story", at(1), 90),
    ("chrome://settings/", "Settings", at(1), 5),
    ("file:///C:/Users/me/doc.pdf", "doc.pdf", at(1), 5),
    ("https://www.youtube.com/results?search_query=excel+vlookup+tutorial", "excel vlookup tutorial - YouTube", at(0.5), 20),
]


# ------------------------------------------------------------------- pure helpers
def test_webkit_time_roundtrip():
    assert wh.webkit_to_datetime(wh.datetime_to_webkit(NOW)) == NOW
    assert wh.webkit_to_datetime(13_000_000_000_000_000).year == 2012
    assert wh.datetime_to_webkit(datetime(1601, 1, 1, tzinfo=timezone.utc)) == 0


@pytest.mark.parametrize("url,expected", [
    ("https://www.github.com/a/b?token=1", ("github.com", None)),
    ("http://Mail.Example.COM:8080/inbox", ("mail.example.com", None)),
    ("https://www.google.com/search?q=hello+world", ("google.com", "hello world")),
    ("https://www.google.com.pk/search?q=nadra%20card", ("google.com.pk", "nadra card")),
    ("https://www.bing.com/search?q=excel+tips", ("bing.com", "excel tips")),
    ("https://duckduckgo.com/?q=privacy", ("duckduckgo.com", "privacy")),
    ("https://www.youtube.com/results?search_query=lofi", ("youtube.com", "lofi")),
    ("https://www.google.com/maps?q=lahore", ("google.com", None)),        # not a web search page
    ("https://www.google.com/search?q=%20", ("google.com", None)),          # empty search
])
def test_parse_url_keeps_only_site_and_search_words(url, expected):
    assert wh.parse_url(url) == expected


@pytest.mark.parametrize("url", ["chrome://settings", "file:///C:/x.pdf", "about:blank", "chrome-extension://abc/x",
                                 "javascript:alert(1)", "", "http://"])
def test_non_web_addresses_are_ignored(url):
    assert wh.parse_url(url) is None


def test_exclusion_matches_parent_domains():
    ex = wh.parse_excluded("MyBank.com\n www.paypal.com ; health.gov.pk,")
    assert ex == {"mybank.com", "paypal.com", "health.gov.pk"}
    assert wh.is_excluded("online.mybank.com", ex) and wh.is_excluded("mybank.com", ex)
    assert not wh.is_excluded("notmybank.com", ex) and not wh.is_excluded("gov.pk", ex)


# ------------------------------------------------------------------------ import
def test_nothing_is_read_while_tracking_is_off(db, tmp_path):
    hist = make_history(tmp_path / "H", VISITS)
    assert db.get_bool("track_websites") is False
    r = wh.import_history(db, {"chrome:Default": hist})
    assert (r.new_visits, r.sources) == (0, 0)
    assert db.query("SELECT COUNT(*) c FROM web_visits")[0]["c"] == 0


def test_import_stores_sites_titles_searches_and_durations(db, tmp_path):
    tracking(db)
    r = wh.import_history(db, {"chrome:Default": make_history(tmp_path / "H", VISITS)})
    assert (r.new_visits, r.sources, r.errors) == (5, 1, [])       # chrome:// and file:// skipped
    rows = {x["domain"] + "|" + (x["search_query"] or ""): x for x in db.query("SELECT * FROM web_visits")}
    gh = rows["github.com|"]
    assert gh["title"] in ("Settings - secret-repo", "Explore GitHub") and gh["source"] == "chrome:Default"
    assert rows["google.com|payroll software pakistan"]["duration_seconds"] == 30
    assert rows["youtube.com|excel vlookup tutorial"]["title"] == "excel vlookup tutorial - YouTube"
    assert {x["domain"] for x in db.query("SELECT domain FROM web_visits")} == {
        "github.com", "google.com", "news.example.org", "youtube.com"}


def test_full_addresses_and_secrets_are_never_stored(db, tmp_path):
    tracking(db)
    wh.import_history(db, {"chrome:Default": make_history(tmp_path / "H", VISITS)})
    dump = " ".join(str(tuple(r)) for t in ("web_visits", "settings", "system_events")
                    for r in db.query(f"SELECT * FROM {t}"))
    for leaked in ("SECRET123", "secret-repo/settings", "myorg", "token=", "https://", "utm=1", "/a/b/c", "oq=x"):
        assert leaked not in dump.replace("Settings - secret-repo", "")      # page TITLE is allowed, the address is not
    assert "C:/Users/me" not in dump


def test_incremental_import_never_duplicates(db, tmp_path):
    tracking(db)
    hist = make_history(tmp_path / "H", VISITS[:3])
    assert wh.import_history(db, {"chrome:Default": hist}).new_visits == 3
    assert wh.import_history(db, {"chrome:Default": hist}).new_visits == 0       # nothing new
    conn = sqlite3.connect(hist)
    conn.execute("INSERT INTO urls VALUES (99, 'https://new.site/x', 'New', 1, 0, 0, 0)")
    conn.execute("INSERT INTO visits(url, visit_time, visit_duration) VALUES (99, ?, 5000000)",
                 (wh.datetime_to_webkit(at(0.1)),))
    conn.commit()
    conn.close()
    assert wh.import_history(db, {"chrome:Default": hist}).new_visits == 1
    assert db.query("SELECT COUNT(*) c FROM web_visits")[0]["c"] == 4
    # even if the marker is lost, the database's UNIQUE rule still prevents duplicates
    with db.transaction() as c:
        c.execute("DELETE FROM settings WHERE key = 'web_last_chrome:Default'")
    assert wh.import_history(db, {"chrome:Default": hist}).new_visits == 0
    assert db.query("SELECT COUNT(*) c FROM web_visits")[0]["c"] == 4


def test_duration_is_capped_and_bad_values_become_zero(db, tmp_path):
    tracking(db)
    hist = make_history(tmp_path / "H", [("https://a.com/", "A", at(5), 3 * 86400), ("https://b.com/", "B", at(4), None),
                                         ("https://c.com/", "C", at(3), 0)])
    wh.import_history(db, {"chrome:Default": hist})
    d = {r["domain"]: r["duration_seconds"] for r in db.query("SELECT domain, duration_seconds FROM web_visits")}
    assert d == {"a.com": 3600, "b.com": 0, "c.com": 0}


def test_only_visits_after_the_chosen_start_are_imported(db, tmp_path):
    hist = make_history(tmp_path / "H", [("https://old.com/", "Old", at(days_ago=45), 10),
                                         ("https://month.com/", "Month", at(days_ago=10), 10),
                                         ("https://today.com/", "Today", at(1), 10)])
    wh.enable_tracking(db, 0, now=at(2))                    # "from now on" = from 2 h ago
    wh.import_history(db, {"chrome:Default": hist})
    assert [r["domain"] for r in db.query("SELECT domain FROM web_visits")] == ["today.com"]
    d2 = Database(tmp_path / "w2.db").open()
    wh.enable_tracking(d2, 30, now=NOW)                     # "include the past 30 days"
    wh.import_history(d2, {"chrome:Default": hist})
    assert sorted(r["domain"] for r in d2.query("SELECT domain FROM web_visits")) == ["month.com", "today.com"]
    d2.close()


def test_excluded_sites_are_skipped_on_import(db, tmp_path):
    tracking(db)
    db.set_setting("web_excluded_domains", "github.com\nexample.org")
    wh.import_history(db, {"chrome:Default": make_history(tmp_path / "H", VISITS)})
    assert {r["domain"] for r in db.query("SELECT domain FROM web_visits")} == {"google.com", "youtube.com"}


def test_unreadable_history_is_reported_not_raised(db, tmp_path):
    tracking(db)
    db.set_setting("web_profiles", "chrome:Bad,chrome:Missing,chrome:Default")
    junk = tmp_path / "junk"
    junk.write_bytes(b"this is not sqlite" * 100)
    good = make_history(tmp_path / "H", VISITS[:2])
    r = wh.import_history(db, {"chrome:Bad": junk, "chrome:Missing": tmp_path / "nope", "chrome:Default": good})
    assert r.sources == 1 and r.new_visits == 2 and len(r.errors) == 2
    assert not list(Path(__import__("tempfile").gettempdir()).glob("timelens-hist-*"))      # temp copies are removed


def test_history_file_is_copied_not_modified(db, tmp_path):
    tracking(db)
    hist = make_history(tmp_path / "H", VISITS)
    before = (hist.stat().st_size, hist.stat().st_mtime)
    wh.import_history(db, {"chrome:Default": hist})
    assert (hist.stat().st_size, hist.stat().st_mtime) == before


def test_disabling_keeps_data_and_stops_importing(db, tmp_path):
    tracking(db)
    hist = make_history(tmp_path / "H", VISITS[:2])
    wh.import_history(db, {"chrome:Default": hist})
    wh.disable_tracking(db)
    conn = sqlite3.connect(hist)
    conn.execute("INSERT INTO urls VALUES (50, 'https://later.com/', 'L', 1, 0, 0, 0)")
    conn.execute("INSERT INTO visits(url, visit_time, visit_duration) VALUES (50, ?, 1)", (wh.datetime_to_webkit(at(0.1)),))
    conn.commit()
    conn.close()
    assert wh.import_history(db, {"chrome:Default": hist}).new_visits == 0
    assert db.query("SELECT COUNT(*) c FROM web_visits")[0]["c"] == 2


# ------------------------------------------------------------ finding the browsers
def test_history_sources_finds_profiles(tmp_path):
    for rel in ("Google/Chrome/User Data/Default", "Google/Chrome/User Data/Profile 1",
                "Google/Chrome/User Data/Guest Profile", "Google/Chrome/User Data/System Profile",
                "Microsoft/Edge/User Data/Default"):
        make_history(tmp_path / rel / "History", [])
    make_history(tmp_path / "Google/Chrome/User Data/Profile 2/NotHistory", [])
    found = real_history_sources(["chrome", "edge"], tmp_path)
    assert sorted(found) == ["chrome:Default", "chrome:Profile 1", "edge:Default"]
    assert sorted(real_history_sources(["edge"], tmp_path)) == ["edge:Default"]
    assert real_history_sources(["firefox", "chrome"], tmp_path / "empty") == {}


# --------------------------------------------------------------------- reports
def test_web_reports(db, tmp_path):
    tracking(db)
    wh.import_history(db, {"chrome:Default": make_history(tmp_path / "H", VISITS)})
    lo, hi = NOW - timedelta(days=1), NOW + timedelta(days=1)
    s = wr.web_summary(db, lo, hi)
    assert (s.visits, s.sites, s.searches, s.seconds) == (5, 4, 2, 600 + 120 + 30 + 90 + 20)
    sites = wr.top_sites(db, lo, hi)
    assert sites[0][:3] == ("github.com", 2, 720) and [x[0] for x in sites][1:] == ["news.example.org", "google.com", "youtube.com"]
    searches = wr.search_history(db, lo, hi)
    assert [q for _, q, _ in searches] == ["excel vlookup tutorial", "payroll software pakistan"]    # newest first
    visits = wr.recent_visits(db, lo, hi)
    assert visits[0][1] == "youtube.com" and len(visits) == 5
    assert wr.web_summary(db, NOW + timedelta(days=5), NOW + timedelta(days=6)).visits == 0


def test_deleting_history_removes_website_visits_too(db, tmp_path):
    tracking(db)
    wh.import_history(db, {"chrome:Default": make_history(tmp_path / "H", VISITS)})
    db.delete_sessions_between(NOW - timedelta(hours=2.2), NOW)                # a time range
    left = db.query("SELECT domain FROM web_visits")
    assert {r["domain"] for r in left} == {"github.com"}                        # only the 3 h and 2.5 h visits remain
    assert db.delete_web_history() == len(left)
    assert db.query("SELECT COUNT(*) c FROM web_visits")[0]["c"] == 0


# ------------------------------------------------------------------- migration
def test_upgrade_from_v1_keeps_history_and_adds_the_table(tmp_path, monkeypatch):
    p = tmp_path / "old.db"
    real = migrations.MIGRATIONS
    monkeypatch.setattr(migrations, "MIGRATIONS", [real[0]])           # a v1 database, as shipped earlier
    with Database(p) as d:
        d.add_closed_session(NOW - timedelta(hours=1), NOW, "ACTIVE", "Microsoft Excel", "EXCEL.EXE", "A.xlsx")
    monkeypatch.setattr(migrations, "MIGRATIONS", real)
    with Database(p) as d:
        assert migrations.current_version(d._writer) == 2
        assert d.query("SELECT document_name FROM activity_sessions")[0][0] == "A.xlsx"
        assert d.query("SELECT COUNT(*) c FROM web_visits")[0]["c"] == 0
        assert d.get_bool("track_websites") is False                    # new setting defaults to OFF
    assert (tmp_path / "old.pre-upgrade-v1.db").exists()


# ------------------------------------------------------------------- profiles
def test_only_the_default_profile_is_read_until_the_user_picks_more(db, tmp_path):
    tracking(db)
    mine = make_history(tmp_path / "A" / "History", [("https://mine.com/", "Mine", at(1), 10)])
    other = make_history(tmp_path / "B" / "History", [("https://someone-else.com/", "Theirs", at(1), 10)])
    sources = {"chrome:Default": mine, "chrome:Profile 1": other, "edge:Default": mine}
    assert wh.import_history(db, sources).sources == 2                       # chrome Default + edge Default
    assert {r["domain"] for r in db.query("SELECT domain FROM web_visits")} == {"mine.com"}


def test_selected_profiles_replace_the_default_rule(db, tmp_path):
    tracking(db)
    mine = make_history(tmp_path / "A" / "History", [("https://mine.com/", "Mine", at(1), 10)])
    other = make_history(tmp_path / "B" / "History", [("https://work.com/", "Work", at(1), 10)])
    db.set_setting("web_profiles", "chrome:Profile 1")
    wh.import_history(db, {"chrome:Default": mine, "chrome:Profile 1": other})
    assert {r["domain"] for r in db.query("SELECT domain FROM web_visits")} == {"work.com"}


def test_profile_rule_and_names(tmp_path):
    assert wh.profile_allowed("chrome:Default", set()) and not wh.profile_allowed("chrome:Profile 2", set())
    assert wh.profile_allowed("chrome:Profile 2", {"chrome:Profile 2"}) and not wh.profile_allowed("chrome:Default", {"chrome:Profile 2"})
    state = tmp_path / "Google" / "Chrome" / "User Data" / "Local State"
    state.parent.mkdir(parents=True)
    state.write_text('{"profile": {"info_cache": {"Default": {"name": "Ali"}, "Profile 1": {"name": "Work", "user_name": "x@y.z"}}}}')
    assert wh.profile_names(["chrome"], tmp_path) == {"chrome:Default": "Ali", "chrome:Profile 1": "Work"}
    assert wh.profile_names(["edge", "chrome"], tmp_path / "nowhere") == {}
