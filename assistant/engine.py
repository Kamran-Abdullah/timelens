"""The assistant's brain: understands a request, searches the index, answers, and opens files.

Everything is local and rule-based - no internet and no AI model - so it understands a fixed
set of request styles (see HELP_TEXT) rather than free conversation.

Safety rules enforced here:
  * only SAFE_EXTENSIONS files are ever opened (never programs or scripts);
  * only an explicit request opens something (a search just lists results);
  * folders are only *shown* in Explorer, never "run".
"""
from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from analytics.app_reports import application_usage
from analytics.document_reports import document_usage, sort_documents
from analytics.periods import preset_range, range_for_days
from assistant import intent as it
from assistant.apps import AppEntry, AppIndex, is_blocked_name
from assistant.file_index import FileEntry, FileIndex, Hit
from assistant.safety import check_openable
from database.db_manager import Database
from ui.theme import fmt_duration

log = logging.getLogger(__name__)

MAX_RESULTS = 12
HELP_TEXT = (
    "I can find and open your documents. Try:\n"
    "  - open October Payroll.xlsx\n"
    "  - find invoices in downloads\n"
    "  - show my recent excel files\n"
    "  - open my latest pdf\n"
    "  - where is the bank reconciliation file\n"
    "  - open my downloads folder   |   open the payroll folder\n"
    "  - open chrome   |   open excel   |   launch calculator   (installed apps)\n"
    "  - what did I work on today / yesterday / this week\n"
    "  - show my most used folders\n"
    "I open documents, images, audio, video, folders and your installed apps - never scripts, "
    "command-line or admin tools. "
    "Everything stays on this computer."
)


@dataclass
class Reply:
    text: str
    files: list[FileEntry] = field(default_factory=list)   # results the UI shows with Open buttons
    apps: list[AppEntry] = field(default_factory=list)     # app choices ("Which app?")
    launched: Optional[str] = None                          # app started by this reply
    opened: Optional[str] = None                            # path that was opened by this reply
    revealed: Optional[str] = None                          # path shown in Explorer by this reply


def default_open(path: str) -> None:
    os.startfile(path)                                      # the file's default program


def default_launch(target: str) -> None:
    os.startfile(target)                                    # a Start Menu shortcut or a fixed Windows tool


def default_reveal(path: str) -> None:
    subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])


def known_folders() -> dict[str, Path]:
    """Folders a request can mention ("in downloads")."""
    out: dict[str, Path] = {}
    try:
        from win32com.shell import shell, shellcon
        for key, fid in (("desktop", shellcon.FOLDERID_Desktop), ("documents", shellcon.FOLDERID_Documents),
                         ("downloads", shellcon.FOLDERID_Downloads)):
            out[key] = Path(shell.SHGetKnownFolderPath(fid, 0, 0))
    except Exception:
        home = Path.home()
        out = {"desktop": home / "Desktop", "documents": home / "Documents", "downloads": home / "Downloads"}
    return out


class AssistantEngine:
    def __init__(self, index: FileIndex, db: Optional[Database] = None,
                 opener: Callable[[str], None] = default_open,
                 revealer: Callable[[str], None] = default_reveal,
                 folders: Optional[dict[str, Path]] = None,
                 today: Callable[[], date] = date.today,
                 apps: Optional[AppIndex] = None,
                 launcher: Callable[[str], None] = default_launch):
        self.index, self.db = index, db
        self.apps = apps if apps is not None else AppIndex()
        self._launch = launcher
        self._open, self._reveal = opener, revealer
        self._folders = folders if folders is not None else known_folders()
        self._today = today

    # --- entry point ------------------------------------------------------------------
    def handle(self, text: str) -> Reply:
        if not self.index.ready:
            self.index.build()
        if not self.apps.ready:
            self.apps.build()
        intent = it.parse(text)
        if intent.kind == it.HELP:
            return Reply(HELP_TEXT)
        if intent.blocked_name:                              # "open setup.exe" and the like
            why = check_openable(intent.blocked_name)[1]
            if intent.blocked_name.lower().endswith(".exe"):
                why += " To start a program, just say its name, for example: open chrome."
            return Reply(why)
        if intent.blocked_app:
            return Reply("I don't open command-line or system-administration tools (Command Prompt, PowerShell, "
                         "Terminal, Registry Editor ...). Open those yourself if you need them.")
        if intent.path and intent.kind in (it.OPEN, it.REVEAL) and os.path.isdir(intent.path.rstrip("\\")):
            return self.open_folder(intent.path.rstrip("\\"))
        if intent.path and intent.kind in (it.OPEN, it.REVEAL):
            return self.reveal_path(intent.path) if intent.kind == it.REVEAL else self.open_path(intent.path)
        if intent.kind == it.ACTIVITY:
            return self._activity(intent)
        if intent.kind == it.FOLDERS:
            return self.frequent_folders(intent.period)
        if intent.folder_target:
            return self._folder_request(intent)
        if intent.kind == it.OPEN:
            return self._open_request(intent)
        if intent.kind == it.REVEAL:
            return self._reveal_request(intent)
        return self._search_request(intent)

    # --- searching ---------------------------------------------------------------------
    def _query(self, intent: it.Intent, limit: int = MAX_RESULTS) -> list[Hit]:
        folder = self._folders.get(intent.place) if intent.place else None
        since = until = None
        if intent.period and intent.kind in (it.SEARCH, it.RECENT, it.OPEN, it.REVEAL):
            start, end = preset_range(intent.period, self._today())
            since, until = start.astimezone().replace(tzinfo=None), end.astimezone().replace(tzinfo=None)
        terms = list(intent.terms)
        exts = intent.exts
        if intent.exact_name:
            stem, _, ext = intent.exact_name.rpartition(".")
            terms = stem.split() or terms
            exts = ("." + ext.lower(),)
        hits = self.index.search(terms, exts=exts, folder=folder, since=since, until=until, limit=limit,
                                 newest_first=intent.newest)
        if terms and hits and max(h.score for h in hits) >= 85:      # strong matches exist:
            hits = [h for h in hits if h.score >= 60]                # leave out the weak partial ones
        return hits

    @staticmethod
    def _describe(intent: it.Intent) -> str:
        bits = []
        if intent.terms or intent.exact_name:
            bits.append(f"'{intent.exact_name or ' '.join(intent.terms)}'")
        if intent.exts:
            bits.append("/".join(e.lstrip('.') for e in intent.exts[:3]) + " files")
        if intent.place:
            bits.append(f"in {intent.place}")
        if intent.period:
            bits.append(f"({intent.period.lower()})")
        return " ".join(bits) or "files"

    def _search_request(self, intent: it.Intent) -> Reply:
        hits = self._query(intent)
        what = self._describe(intent)
        if not hits:
            return Reply(f"I couldn't find {what}. {self._index_note()}Try fewer words, or check the "
                         "search folders with the Folders button.")
        files = [h.entry for h in hits]
        head = f"Found {len(files)} match{'es' if len(files) != 1 else ''} for {what}"
        if intent.newest:
            head += ", newest first"
        return Reply(head + ":", files)

    def _index_note(self) -> str:
        return (f"I searched {len(self.index.entries):,} documents in {len(self.index.roots)} folder(s). "
                + ("The index was truncated at its size limit. " if self.index.truncated else ""))

    # --- opening ------------------------------------------------------------------------
    # --- applications ----------------------------------------------------------------------
    def _try_app(self, intent: it.Intent) -> Optional[Reply]:
        """"open chrome" / "launch excel": start an installed application. Returns None when the
        request is really about a file, so the file search takes over."""
        if (intent.exact_name or intent.newest or intent.mentions_file or intent.place or intent.period
                or not intent.app_terms):
            return None
        hits = self.apps.search(intent.app_terms)
        if not hits:
            return None
        best, second = hits[0], (hits[1].score if len(hits) > 1 else 0.0)
        only_file_words = all(t in it.KIND_WORDS for t in intent.app_terms)
        if only_file_words and best.score < 100:               # "open pdf" means PDF files, not an app
            return None
        launch_verb = intent.verb in ("launch", "start", "run")
        confident = (best.score >= 100 or (best.score >= 85 and best.score - second >= 15)
                     or (len(hits) == 1 and best.score >= 60)
                     or (launch_verb and best.score >= 60 and best.score - second >= 10))
        file_hits = self._query(intent)
        if confident:
            if best.score < 100 and file_hits and file_hits[0].score >= 100:
                return None                                    # a file with exactly that name: file wins
            return self.launch_app(best.entry)
        if not file_hits:
            return Reply("Which app should I open?", apps=[h.entry for h in hits[:5]])
        return None

    def launch_app(self, entry: AppEntry) -> Reply:
        """Start an installed application. Only apps from our own list can be started."""
        if not self.apps.ready:
            self.apps.build()
        if not self.apps.is_known(entry.target) or is_blocked_name(entry.name):
            return Reply("I can only open apps from your installed programs list, and never command-line "
                         "or admin tools.")
        try:
            self._launch(entry.target)
        except Exception as e:
            log.exception("Could not start %s", entry.name)
            return Reply(f"Windows could not start {entry.name}: {e}")
        return Reply(f"Opened {entry.name}.", launched=entry.target)

    def _open_request(self, intent: it.Intent) -> Reply:
        app_reply = self._try_app(intent)
        if app_reply is not None:
            return app_reply
        hits = self._query(intent, limit=6)
        what = self._describe(intent)
        if not hits:
            return Reply(f"I couldn't find {what}. {self._index_note()}")
        best = hits[0]
        if intent.newest:                                    # "open my latest excel file"
            return self.open_path(best.entry.path, prefix=f"Opening your newest match for {what}: ")
        second = hits[1].score if len(hits) > 1 else 0.0
        exact_unique = best.score >= 100 and second < 100
        confident = best.score >= 85 and (best.score - second >= 15) or exact_unique
        if confident or len(hits) == 1 and best.score >= 60:
            return self.open_path(best.entry.path)
        return Reply(f"I found several matches for {what}. Which one should I open?",
                     [h.entry for h in hits])

    # --- folders --------------------------------------------------------------------------
    def _folder_request(self, intent: it.Intent) -> Reply:
        place = self._folders.get(intent.place) if intent.place else None
        if not intent.terms:                                   # "open my downloads folder"
            if place is None:
                return Reply("Which folder? Say its name, for example: open the payroll folder.")
            return self.reveal_path(str(place)) if intent.kind == it.REVEAL else self.open_folder(str(place))
        hits = self.index.search_folders(intent.terms, folder=place, limit=8)
        what = f"a folder called '{' '.join(intent.terms)}'" + (f" in {intent.place}" if intent.place else "")
        if not hits:
            return Reply(f"I couldn't find {what}. {self._index_note()}")
        best, second = hits[0], (hits[1].score if len(hits) > 1 else 0.0)
        confident = best.score >= 85 and (best.score - second >= 15) or len(hits) == 1
        if intent.kind in (it.OPEN, it.REVEAL) and confident:
            return self.reveal_path(best.entry.path) if intent.kind == it.REVEAL else self.open_folder(best.entry.path)
        head = "Which folder should I open?" if intent.kind == it.OPEN else f"Found {len(hits)} folder(s) for {what}:"
        return Reply(head, [h.entry for h in hits])

    def open_folder(self, path: str) -> Reply:
        """Show a folder in File Explorer. Safe: it only displays the folder, nothing is run."""
        if not os.path.isdir(path):
            return Reply(f"I can't find the folder {os.path.basename(path) or path} any more.")
        try:
            self._open(path)
        except Exception as e:
            log.exception("Could not open folder %s", path)
            return Reply(f"Windows could not open that folder: {e}")
        return Reply(f"Opened the folder {os.path.basename(path.rstrip(chr(92))) or path}.", opened=path)

    def open_path(self, path: str, prefix: str = "") -> Reply:
        if os.path.isdir(path):                                # a result row for a folder
            return self.open_folder(path)
        ok, why = check_openable(path)
        if not ok:
            return Reply(why)
        if not os.path.isfile(path):
            return Reply(f"I can't find {os.path.basename(path)} any more - it may have been moved or deleted.")
        try:
            self._open(path)
        except Exception as e:
            log.exception("Could not open %s", path)
            return Reply(f"Windows could not open {os.path.basename(path)}: {e}")
        return Reply(f"{prefix}Opened {os.path.basename(path)}.", opened=path)

    # --- showing in Explorer --------------------------------------------------------------
    def _reveal_request(self, intent: it.Intent) -> Reply:
        hits = self._query(intent, limit=6)
        what = self._describe(intent)
        if not hits:
            return Reply(f"I couldn't find {what}. {self._index_note()}")
        if len(hits) == 1 or (hits[0].score >= 85 and (len(hits) == 1 or hits[0].score - hits[1].score >= 15)):
            return self.reveal_path(hits[0].entry.path)
        return Reply(f"Here is where I found {what}:", [h.entry for h in hits])

    def reveal_path(self, path: str) -> Reply:
        if not os.path.exists(path):
            return Reply(f"{os.path.basename(path)} no longer exists.")
        try:
            self._reveal(path)
        except Exception as e:
            log.exception("Could not show %s", path)
            return Reply(f"Could not open Explorer: {e}")
        return Reply(f"Showing {os.path.basename(path)} in its folder ({os.path.dirname(path)}).", revealed=path)

    # --- most-used folders ------------------------------------------------------------------
    def frequent_folders(self, period: Optional[str] = None, limit: int = 8) -> Reply:
        """Folders ranked by the time you spent in the documents inside them.

        TimeLens never stores file locations, so this is worked out on the spot: take the
        documents you worked on in the period, find where each one lives (in the in-memory file
        index), and add up the time per folder."""
        if self.db is None:
            return Reply("I can't read your activity history here.")
        if not self.index.ready:
            self.index.build()
        today = self._today()
        if period:
            start, end = preset_range(period, today)
            label = period.lower()
        else:
            start, end = range_for_days(today - timedelta(days=29), today)
            label = "the last 30 days"
        docs = document_usage(self.db, start, end, today=today)
        if docs.empty:
            return Reply(f"I have no recorded documents for {label} yet, so I can't tell which folders you use most.")
        seconds: dict[str, float] = {}
        counts: dict[str, int] = {}
        for row in docs.itertuples():
            matches = self.index.find_by_name(row.document)
            if not matches:
                continue                                       # unsaved, renamed or outside the search folders
            folder = matches[0].folder                         # newest copy if the name exists in several places
            seconds[folder] = seconds.get(folder, 0.0) + float(row.range_seconds)
            counts[folder] = counts.get(folder, 0) + 1
        ranked = sorted(((f, s) for f, s in seconds.items() if os.path.isdir(f)), key=lambda kv: -kv[1])[:limit]
        if not ranked:
            return Reply(f"I couldn't match the documents you used in {label} to folders on this computer. "
                         "Add the folders you work in with the Folders button.")
        entries = []
        lines = [f"Your most-used folders ({label}, by time spent in the documents inside them):"]
        for i, (folder, secs) in enumerate(ranked, 1):
            name = os.path.basename(folder.rstrip("\\/")) or folder
            entries.append(FileEntry(name, "", "", folder, os.path.getmtime(folder), 0, True))
            lines.append(f"  {i}. {name} - {fmt_duration(secs)}, {counts[folder]} document(s)")
        return Reply("\n".join(lines), entries)

    # --- "what did I work on" ----------------------------------------------------------------
    def _activity(self, intent: it.Intent) -> Reply:
        if self.db is None:
            return Reply("I can't read your activity history here.")
        today = self._today()
        period = intent.period or "Today"
        start, end = preset_range(period, today)
        docs = sort_documents(document_usage(self.db, start, end, today=today), "Most used")
        apps = application_usage(self.db, start, end, today=today)
        apps = apps[apps["total"] > 0]               # the table also lists apps used only in other periods
        if docs.empty and apps.empty:
            return Reply(f"I have no recorded activity for {period.lower()} yet.")
        lines = [f"{period}: you were active in {len(apps)} application(s) and {len(docs)} document(s)."]
        if not apps.empty:
            lines.append("Top applications: " + ", ".join(
                f"{r.application} ({fmt_duration(r.total)})" for r in apps.head(5).itertuples()))
        files: list[FileEntry] = []
        if not docs.empty:
            lines.append("Documents you worked on:")
            for r in docs.head(8).itertuples():
                lines.append(f"  {r.document} - {fmt_duration(r.range_seconds)} ({r.application})")
                hit = self.index.search(r.document.rsplit(".", 1)[0].split(), exts=("." + r.document.rsplit(".", 1)[-1].lower(),),
                                        limit=1) if "." in r.document else []
                if hit and hit[0].score >= 100:
                    files.append(hit[0].entry)
        if files:
            lines.append("I found these on disk - use Open to reopen them:")
        elif not docs.empty:
            lines.append("(I couldn't match these names to files in your search folders.)")
        return Reply("\n".join(lines), files)
