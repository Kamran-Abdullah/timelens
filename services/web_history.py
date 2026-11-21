"""Optional website tracking: import visits from Chrome / Edge browsing history.

THIS IS OFF BY DEFAULT. It only runs after the user opts in (Settings > Websites, with an
explanation and a Yes/No prompt) and can be turned off and wiped at any time.

How it works: Chrome and Edge keep a local SQLite file called "History". While the browser is
running the file is in use, so we copy it to a temporary file, read the copy, and delete it.
We store ONLY: the site (domain, e.g. github.com), the page title, the words typed into a search
engine (if the page was a search) and the visit time and duration.
We never store: full web addresses (paths and query strings can contain tokens and personal
data), page contents, passwords, cookies or private/incognito browsing (browsers do not put
incognito visits in History at all).

Nothing here uses the network. URL parsing uses urllib.parse, which only splits strings.
"""
from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, unquote_plus, urlsplit

from database.db_manager import Database
from database.models import from_ts, to_ts

log = logging.getLogger(__name__)

# Chrome stores times as microseconds since 1601-01-01 UTC ("WebKit time").
_WEBKIT_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)
MAX_VISIT_SECONDS = 3600          # a tab left open for days must not count as days of use
MAX_ROWS_PER_IMPORT = 200_000
TITLE_LIMIT = 200

BROWSERS = {
    "chrome": ("Google Chrome", Path("Google") / "Chrome" / "User Data"),
    "edge": ("Microsoft Edge", Path("Microsoft") / "Edge" / "User Data"),
}

# (host ends with, path starts with, query parameter that holds the search words)
_SEARCH_RULES = (
    ("google.", "/search", "q"), ("bing.com", "/search", "q"), ("duckduckgo.com", "/", "q"),
    ("search.yahoo.com", "/search", "p"), ("youtube.com", "/results", "search_query"),
    ("ecosia.org", "/search", "q"), ("search.brave.com", "/search", "q"),
)


@dataclass
class ImportResult:
    new_visits: int = 0
    sources: int = 0
    errors: list[str] = field(default_factory=list)


# --- pure helpers (unit-tested) --------------------------------------------------------
def webkit_to_datetime(microseconds: int) -> datetime:
    return _WEBKIT_EPOCH + timedelta(microseconds=int(microseconds))


def datetime_to_webkit(dt: datetime) -> int:
    return int((dt.astimezone(timezone.utc) - _WEBKIT_EPOCH).total_seconds() * 1_000_000)


def clean_domain(host: str) -> str:
    host = (host or "").lower().strip(".")
    return host[4:] if host.startswith("www.") else host


def parse_url(url: str) -> Optional[tuple[str, Optional[str]]]:
    """(domain, search words or None) for an http(s) address; None for anything else
    (chrome://, file://, extensions, ...). The path and query string are discarded here."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    domain = clean_domain(parts.hostname)
    search = None
    for host_part, path_prefix, param in _SEARCH_RULES:
        if host_part in domain and parts.path.startswith(path_prefix):
            values = parse_qs(parts.query).get(param)
            if values and values[0].strip():
                search = unquote_plus(values[0]).strip()[:200]
            break
    return domain, search


def is_excluded(domain: str, excluded: set[str]) -> bool:
    """A domain is excluded if it, or any parent domain, is on the list (mail.example.com is
    excluded by 'example.com')."""
    parts = domain.split(".")
    return any(".".join(parts[i:]) in excluded for i in range(len(parts)))


def parse_excluded(text: str) -> set[str]:
    entries = text.replace(",", "\n").replace(";", "\n").splitlines()
    return {clean_domain(e.strip()) for e in entries if e.strip()}


# --- finding the history files ------------------------------------------------------------
def history_sources(browsers: list[str], local_appdata: Optional[Path] = None) -> dict[str, Path]:
    """{'chrome:Default': path-to-History, ...} for every profile that exists."""
    base = Path(local_appdata or os.environ.get("LOCALAPPDATA", ""))
    found: dict[str, Path] = {}
    for key in browsers:
        if key not in BROWSERS:
            continue
        user_data = base / BROWSERS[key][1]
        if not user_data.is_dir():
            continue
        for profile in sorted(user_data.iterdir()):
            if profile.is_dir() and (profile.name == "Default" or profile.name.startswith("Profile ")):
                history = profile / "History"
                if history.is_file():
                    found[f"{key}:{profile.name}"] = history
    return found


def profile_allowed(key: str, selected: set[str]) -> bool:
    """A browser can hold several profiles (other family members, work accounts ...). Until the user
    ticks specific profiles in Settings, only each browser's Default profile is read."""
    return key in selected if selected else key.endswith(":Default")


def profile_names(browsers: list[str], local_appdata: Optional[Path] = None) -> dict[str, str]:
    """{'chrome:Profile 1': 'Work', ...} from the browser's 'Local State' file (profile names only)."""
    import json
    base = Path(local_appdata or os.environ.get("LOCALAPPDATA", ""))
    names: dict[str, str] = {}
    for key in browsers:
        if key not in BROWSERS:
            continue
        state = base / BROWSERS[key][1] / "Local State"
        try:
            cache = json.loads(state.read_text(encoding="utf-8")).get("profile", {}).get("info_cache", {})
            for folder, info in cache.items():
                names[f"{key}:{folder}"] = str(info.get("name", "")) if isinstance(info, dict) else ""
        except (OSError, ValueError):
            continue
    return names


def _read_copy(history: Path, last_id: int) -> list[tuple]:
    """Copy the (possibly locked) History file to a temp folder and read new visits."""
    tmpdir = Path(tempfile.mkdtemp(prefix="timelens-hist-"))
    try:
        copy = tmpdir / "History"
        shutil.copy2(history, copy)
        for extra in ("-wal", "-journal"):             # uncommitted pages, if the browser uses them
            side = Path(str(history) + extra)
            if side.exists():
                shutil.copy2(side, Path(str(copy) + extra))
        conn = sqlite3.connect(f"file:{copy.as_posix()}?mode=ro", uri=True)
        try:
            return conn.execute(
                "SELECT v.id, v.visit_time, v.visit_duration, u.url, u.title FROM visits v "
                "JOIN urls u ON u.id = v.url WHERE v.id > ? ORDER BY v.id LIMIT ?",
                (last_id, MAX_ROWS_PER_IMPORT)).fetchall()
        finally:
            conn.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# --- the importer -----------------------------------------------------------------------------
def import_history(db: Database, sources: Optional[dict[str, Path]] = None) -> ImportResult:
    """Bring new visits from the browsers into the TimeLens database. Does nothing unless the
    user has turned website tracking on. Safe to call repeatedly: each browser profile is read
    from where the last import stopped, and the database ignores duplicates."""
    result = ImportResult()
    if not db.get_bool("track_websites"):
        return result
    browsers = [b for b in db.get_setting("web_browsers").split(",") if b]
    sources = sources if sources is not None else history_sources(browsers)
    selected = {p for p in db.get_setting("web_profiles").split(",") if p}
    sources = {k: v for k, v in sources.items() if profile_allowed(k, selected)}
    excluded = parse_excluded(db.get_setting("web_excluded_domains"))
    since_text = db.get_setting("web_since")
    since = from_ts(since_text) if since_text else None
    since_webkit = datetime_to_webkit(since) if since else 0

    for key, path in sources.items():
        marker = f"web_last_{key}"
        try:
            last_id = int(db.get_setting(marker)) if _has_setting(db, marker) else 0
            rows = _read_copy(path, last_id)
        except Exception as e:                          # locked, corrupt, or browser mid-write
            log.warning("Could not read %s: %s", path, e)
            result.errors.append(f"{key}: {e}")
            continue
        result.sources += 1
        batch = []
        for vid, vtime, vdur, url, title in rows:
            if vtime < since_webkit:
                continue
            parsed = parse_url(url)
            if parsed is None:
                continue
            domain, search = parsed
            if is_excluded(domain, excluded):
                continue
            seconds = min(max(0.0, (vdur or 0) / 1_000_000), MAX_VISIT_SECONDS)
            batch.append((key, vid, to_ts(webkit_to_datetime(vtime)), domain,
                          (title or "")[:TITLE_LIMIT], search, seconds))
        with db.transaction() as c:
            before = c.execute("SELECT COUNT(*) FROM web_visits").fetchone()[0]
            c.executemany("INSERT OR IGNORE INTO web_visits(source, source_visit_id, visit_time, domain,"
                          " title, search_query, duration_seconds) VALUES (?,?,?,?,?,?,?)", batch)
            result.new_visits += c.execute("SELECT COUNT(*) FROM web_visits").fetchone()[0] - before
        if rows:
            db.set_setting(marker, rows[-1][0])
    return result


def _has_setting(db: Database, key: str) -> bool:
    return bool(db.query("SELECT 1 FROM settings WHERE key = ?", (key,)))


def enable_tracking(db: Database, include_past_days: int = 0, now: Optional[datetime] = None) -> None:
    """Turn tracking on. `include_past_days` = 0 means 'only from now on'."""
    now = now or datetime.now(timezone.utc)
    db.set_setting("web_since", to_ts(now - timedelta(days=include_past_days)))
    db.set_setting("track_websites", True)


def disable_tracking(db: Database) -> None:
    db.set_setting("track_websites", False)
