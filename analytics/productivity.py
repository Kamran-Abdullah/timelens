"""Productivity categories, rules, transparent scoring and comparisons.

Nothing is assumed: an application counts as productive/distracting only if the USER
assigned it a category. Everything else is reported as "uncategorized".

Rules = "application (executable) -> category". Categories have a KIND (productive,
neutral, distraction, personal, custom) and the score weights are per kind.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

from analytics.periods import range_for_days, week_start
from analytics.time_calc import clipped_sessions, local_day_bounds
from database.db_manager import Database

UNCATEGORIZED = "uncategorized"          # the "kind" used for unassigned applications
UNCAT_NAME = "Uncategorized"
BASE_KINDS = ("productive", "neutral", "distraction", "personal")
KINDS = BASE_KINDS + ("custom",)


# --- distribution by kind (used by the Dashboard) -----------------------------------
def category_distribution(db: Database, start: datetime, end: datetime,
                          now: Optional[datetime] = None) -> dict[str, float]:
    """ACTIVE seconds per category kind (productive, neutral, ..., uncategorized)."""
    kinds = {r["executable_name"].lower(): r["kind"] for r in db.query(
        "SELECT a.executable_name, c.kind FROM applications a "
        "JOIN categories c ON c.id = a.category_id")}
    acc: dict[str, float] = defaultdict(float)
    for row, s, e in clipped_sessions(db, start, end, now):
        if row["activity_status"] != "ACTIVE":
            continue
        kind = kinds.get((row["executable_name"] or "").lower(), UNCATEGORIZED)
        acc[kind] += (e - s).total_seconds()
    return dict(acc)


# --- categories and rules ------------------------------------------------------------
def list_categories(db: Database) -> list[dict]:
    return [dict(r) for r in db.query(
        "SELECT id, name, kind, color FROM categories ORDER BY "
        "CASE kind WHEN 'productive' THEN 0 WHEN 'neutral' THEN 1 WHEN 'distraction' THEN 2 "
        "WHEN 'personal' THEN 3 ELSE 4 END, name COLLATE NOCASE")]


def add_category(db: Database, name: str, color: str = "#b06fd6") -> int:
    """Create a custom category. Names are unique (case-insensitive)."""
    name = name.strip()
    if not name:
        raise ValueError("Category name cannot be empty")
    if name.lower() == UNCAT_NAME.lower():
        raise ValueError(f"'{UNCAT_NAME}' is reserved")
    existing = db.query("SELECT 1 FROM categories WHERE name = ?", (name,))
    if existing:
        raise ValueError(f"A category named '{name}' already exists")
    with db.transaction() as c:
        return c.execute("INSERT INTO categories(name, kind, color) VALUES (?, 'custom', ?)",
                         (name, color)).lastrowid


def delete_category(db: Database, category_id: int) -> None:
    """Only custom categories can be deleted; their applications become uncategorized."""
    with db.transaction() as c:
        row = c.execute("SELECT kind FROM categories WHERE id = ?", (category_id,)).fetchone()
        if row is None:
            return
        if row["kind"] != "custom":
            raise ValueError("Built-in categories cannot be deleted")
        c.execute("DELETE FROM categories WHERE id = ?", (category_id,))


def list_applications(db: Database) -> list[dict]:
    """Every application TimeLens has seen, with its current category (None = uncategorized)."""
    return [dict(r) for r in db.query(
        "SELECT a.executable_name, a.display_name, a.category_id, c.name AS category_name "
        "FROM applications a LEFT JOIN categories c ON c.id = a.category_id "
        "WHERE a.executable_name <> 'LockApp.exe' "      # old rows from before the fix
        "ORDER BY a.display_name COLLATE NOCASE")]


def set_app_category(db: Database, executable_name: str, category_id: Optional[int]) -> None:
    with db.transaction() as c:
        n = c.execute("UPDATE applications SET category_id = ? WHERE executable_name = ?",
                      (category_id, executable_name)).rowcount
        if n == 0:
            raise KeyError(executable_name)


# --- scoring weights ----------------------------------------------------------------
def get_weights(db: Database) -> dict[str, float]:
    return {k: float(db.get_setting(f"weight_{k}")) for k in KINDS}


def set_weight(db: Database, kind: str, percent: float) -> None:
    if kind not in KINDS:
        raise ValueError(kind)
    if not 0 <= percent <= 100:
        raise ValueError("Weight must be between 0 and 100")
    db.set_setting(f"weight_{kind}", percent)


# --- breakdown ----------------------------------------------------------------------
def category_breakdown(db: Database, start: datetime, end: datetime,
                       now: Optional[datetime] = None) -> list[dict]:
    """ACTIVE seconds per category: [{name, kind, seconds}], largest first.
    Applications without a category are reported as 'Uncategorized'."""
    cats = {r["executable_name"].lower(): (r["name"], r["kind"]) for r in db.query(
        "SELECT a.executable_name, c.name, c.kind FROM applications a "
        "JOIN categories c ON c.id = a.category_id")}
    acc: dict[tuple[str, str], float] = defaultdict(float)
    for row, s, e in clipped_sessions(db, start, end, now):
        if row["activity_status"] != "ACTIVE":
            continue
        key = cats.get((row["executable_name"] or "").lower(), (UNCAT_NAME, UNCATEGORIZED))
        acc[key] += (e - s).total_seconds()
    out = [{"name": n, "kind": k, "seconds": v} for (n, k), v in acc.items()]
    return sorted(out, key=lambda r: (-r["seconds"], r["name"]))


def seconds_by_kind(breakdown: list[dict]) -> dict[str, float]:
    acc: dict[str, float] = defaultdict(float)
    for r in breakdown:
        acc[r["kind"]] += r["seconds"]
    return dict(acc)


@dataclass
class ScoreResult:
    score: Optional[float]          # 0-100, or None when nothing is categorized
    categorized_seconds: float
    total_seconds: float
    lines: list[str] = field(default_factory=list)   # the working, step by step

    @property
    def coverage(self) -> Optional[float]:
        return None if self.total_seconds <= 0 else self.categorized_seconds / self.total_seconds * 100


def _hours(seconds: float) -> str:
    return f"{seconds / 3600:.2f}h"


def compute_score(by_kind: dict[str, float], weights: dict[str, float]) -> ScoreResult:
    """Productivity score = sum(time_k x weight_k) / sum(time_k), over CATEGORIZED active
    time only. Uncategorized time is left out (never guessed) and its share is reported.
    Weights are the percent credit each kind earns (defaults: productive 100, neutral 50,
    distraction 0, personal 50, custom 50 - all editable)."""
    total = sum(by_kind.values())
    cat = {k: v for k, v in by_kind.items() if k != UNCATEGORIZED and v > 0}
    cat_total = sum(cat.values())
    if cat_total <= 0:
        msg = ("No categorized active time in this period, so there is no score. Assign "
               "categories to applications in the Rules tab." if total > 0
               else "No active time in this period.")
        return ScoreResult(None, 0.0, total, [msg])
    weighted = sum(v * weights.get(k, 0.0) for k, v in cat.items())
    score = weighted / cat_total
    lines = [f"{k.capitalize()}: {_hours(v)} x {weights.get(k, 0):g}% = "
             f"{_hours(v * weights.get(k, 0) / 100)} credit"
             for k, v in sorted(cat.items(), key=lambda kv: -kv[1])]
    lines.append(f"Score = total credit / categorized time = "
                 f"{_hours(weighted / 100)} / {_hours(cat_total)} = {score:.0f}%")
    unc = by_kind.get(UNCATEGORIZED, 0.0)
    if unc > 0:
        lines.append(f"Not included: {_hours(unc)} uncategorized "
                     f"({unc / total * 100:.0f}% of active time).")
    return ScoreResult(score, cat_total, total, lines)


# --- comparisons --------------------------------------------------------------------
@dataclass
class PeriodSummary:
    label: str
    by_kind: dict[str, float]
    active_seconds: float
    score: ScoreResult


def summarize(db: Database, label: str, start: datetime, end: datetime,
              weights: dict[str, float], now: Optional[datetime] = None) -> PeriodSummary:
    by_kind = seconds_by_kind(category_breakdown(db, start, end, now))
    return PeriodSummary(label, by_kind, sum(by_kind.values()), compute_score(by_kind, weights))


def comparisons(db: Database, today: date, now: Optional[datetime] = None
                ) -> list[tuple[PeriodSummary, PeriodSummary]]:
    """(current, previous) pairs: Today vs Yesterday, This week vs Last week.
    Last week is cut to the same number of days so the comparison is fair."""
    weights = get_weights(db)
    y = today - timedelta(days=1)
    ws = week_start(today)
    lws = ws - timedelta(days=7)
    same_days = lws + timedelta(days=(today - ws).days)
    return [
        (summarize(db, "Today", *range_for_days(today, today), weights, now),
         summarize(db, "Yesterday", *range_for_days(y, y), weights, now)),
        (summarize(db, "This week", *range_for_days(ws, today), weights, now),
         summarize(db, "Last week (same days)", *range_for_days(lws, same_days), weights, now)),
    ]


def daily_series(db: Database, today: date, days: int = 7,
                 now: Optional[datetime] = None) -> list[tuple[date, dict[str, float]]]:
    """Seconds per category kind for each of the last `days` local days (oldest first)."""
    out = []
    for i in range(days - 1, -1, -1):
        d = today - timedelta(days=i)
        s, e = local_day_bounds(d)
        out.append((d, seconds_by_kind(category_breakdown(db, s, e, now))))
    return out
