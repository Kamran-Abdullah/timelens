"""A fast in-memory index of the user's document files, and ranked name search.

Privacy: the index lives only in memory. Nothing from it (names, folders) is written to the
TimeLens database or to disk. It is rebuilt each time TimeLens starts (or on request).
Only files with a type from assistant.safety.SAFE_EXTENSIONS are indexed, so programs and
scripts never even appear in results.
"""
from __future__ import annotations

import difflib
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from assistant.safety import SAFE_EXTENSIONS

log = logging.getLogger(__name__)

# Folders that only ever hold program data, caches or build output.
SKIP_DIRS = frozenset({
    "node_modules", ".git", ".svn", "__pycache__", ".venv", "venv", "env", "site-packages",
    "$recycle.bin", "appdata", "windows", "program files", "program files (x86)", "programdata",
    ".cache", ".vscode", ".idea", "pyside6", "matplotlib", "mpl-data", "_internal", "build",
    "dist", "msocache", "system volume information", ".pytest_cache",
})
MAX_FILES = 250_000
MAX_FOLDERS = 100_000
MAX_DEPTH = 10
_SEPARATORS = re.compile(r"[\W_]+", re.UNICODE)


def normalize(text: str) -> str:
    """Lower-case and replace punctuation with single spaces ('October_Payroll-2026.xlsx'
    -> 'october payroll 2026 xlsx')."""
    return _SEPARATORS.sub(" ", text.lower()).strip()


@dataclass(frozen=True)
class FileEntry:
    name: str          # file name, e.g. "October Payroll.xlsx"
    norm: str          # normalised stem used for matching
    ext: str           # ".xlsx"
    path: str          # full path - kept in memory only
    mtime: float
    size: int
    is_dir: bool = False        # True for a folder entry

    @property
    def folder(self) -> str:
        return os.path.dirname(self.path)

    @property
    def modified(self) -> datetime:
        return datetime.fromtimestamp(self.mtime)


@dataclass
class Hit:
    entry: FileEntry
    score: float


def default_roots() -> list[Path]:
    """Desktop, Documents, Downloads (following OneDrive redirection) and the OneDrive
    folder, without duplicates or folders nested inside another root."""
    found: list[Path] = []
    try:
        from win32com.shell import shell, shellcon
        for fid in (shellcon.FOLDERID_Desktop, shellcon.FOLDERID_Documents, shellcon.FOLDERID_Downloads):
            found.append(Path(shell.SHGetKnownFolderPath(fid, 0, 0)))
    except Exception:
        log.debug("Known-folder lookup failed; using the home folder", exc_info=True)
        home = Path.home()
        found += [home / "Desktop", home / "Documents", home / "Downloads"]
    one = os.environ.get("OneDrive") or os.environ.get("OneDriveConsumer") or os.environ.get("OneDriveCommercial")
    if one:
        found.append(Path(one))
    return dedupe_roots(found)


def dedupe_roots(roots: Iterable[Path]) -> list[Path]:
    existing = []
    for r in roots:
        try:
            r = Path(r)
            if r.is_dir() and r.resolve() not in [e.resolve() for e in existing]:
                existing.append(r)
        except OSError:
            continue
    keep = []
    for r in existing:       # drop a root that sits inside another one (it would be scanned twice)
        rl = str(r.resolve()).lower()
        if not any(rl != str(o.resolve()).lower() and rl.startswith(str(o.resolve()).lower() + os.sep)
                   for o in existing):
            keep.append(r)
    return keep


class FileIndex:
    def __init__(self, roots: Optional[list[Path]] = None, max_files: int = MAX_FILES,
                 max_depth: int = MAX_DEPTH):
        self.roots = list(roots) if roots is not None else default_roots()
        self.max_files, self.max_depth = max_files, max_depth
        self.entries: list[FileEntry] = []
        self.folders: list[FileEntry] = []     # folder names, so 'open the payroll folder' works
        self.built_at: Optional[datetime] = None
        self.build_seconds = 0.0
        self.truncated = False
        self._lock = threading.Lock()
        self._by_name: Optional[dict[str, list[FileEntry]]] = None   # built on first use

    @property
    def ready(self) -> bool:
        return self.built_at is not None

    def set_roots(self, roots: list[Path]) -> None:
        self.roots = dedupe_roots(roots)

    # --- building ---------------------------------------------------------------
    def build(self) -> int:
        """Walk the roots and (re)build the index. Safe to call from a worker thread."""
        t0 = time.perf_counter()
        entries: list[FileEntry] = []
        folders: list[FileEntry] = []
        truncated = False
        for root in self.roots:
            stack = [(str(root), 0)]
            while stack and not truncated:
                folder, depth = stack.pop()
                try:
                    with os.scandir(folder) as it:
                        for e in it:
                            try:
                                if e.is_dir(follow_symlinks=False):
                                    if depth < self.max_depth and e.name.lower() not in SKIP_DIRS \
                                            and not e.name.startswith("."):
                                        stack.append((e.path, depth + 1))
                                        if len(folders) < MAX_FOLDERS:
                                            folders.append(FileEntry(e.name, normalize(e.name), "", e.path,
                                                                     e.stat().st_mtime, 0, True))
                                elif e.is_file(follow_symlinks=False):
                                    ext = os.path.splitext(e.name)[1].lower()
                                    if ext in SAFE_EXTENSIONS and not e.name.startswith("~$"):
                                        st = e.stat()
                                        entries.append(FileEntry(e.name, normalize(os.path.splitext(e.name)[0]),
                                                                 ext, e.path, st.st_mtime, st.st_size))
                                        if len(entries) >= self.max_files:
                                            truncated = True
                                            break
                            except OSError:
                                continue
                except (PermissionError, FileNotFoundError, OSError):
                    continue
        with self._lock:
            self.entries = entries
            self.folders = folders
            self._by_name = None
            self.truncated = truncated
            self.built_at = datetime.now()
            self.build_seconds = time.perf_counter() - t0
        log.info("Indexed %d files in %.1fs", len(entries), self.build_seconds)
        return len(entries)

    # --- searching ----------------------------------------------------------------
    def search(self, terms: list[str], exts: Optional[Iterable[str]] = None,
               folder: Optional[Path] = None, since: Optional[datetime] = None,
               until: Optional[datetime] = None, limit: int = 15,
               newest_first: bool = False) -> list[Hit]:
        """Ranked matches. With no terms, every file passing the filters is a match (used for
        'recent excel files'). Ranking: exact name > starts with > contains the phrase > all
        words present > some words > close spelling."""
        with self._lock:
            entries = self.entries
        ext_set = {e.lower() for e in exts} if exts else None
        folder_prefix = (str(folder).lower().rstrip("\\/") + os.sep) if folder else None
        since_ts = since.timestamp() if since else None
        until_ts = until.timestamp() if until else None
        words = [normalize(t) for t in terms if normalize(t)]
        phrase = " ".join(words)

        pool = []
        for e in entries:
            if ext_set is not None and e.ext not in ext_set:
                continue
            if folder_prefix and not e.path.lower().startswith(folder_prefix):
                continue
            if since_ts is not None and e.mtime < since_ts:
                continue
            if until_ts is not None and e.mtime >= until_ts:
                continue
            pool.append(e)

        if not words:
            hits = [Hit(e, 50.0) for e in pool]
        else:
            hits = []
            for e in pool:
                s = self._score(e, phrase, words)
                if s > 0:
                    hits.append(Hit(e, s))
            if not hits:                                 # nothing matched: try forgiving spelling
                hits = self._fuzzy(pool, phrase)
        recent = time.time() - 30 * 86400
        for h in hits:                                   # small bonus for recently used files
            if h.entry.mtime > recent:
                h.score += 3
        if newest_first or not words:
            hits.sort(key=lambda h: (-h.entry.mtime, h.entry.name.lower()))
        else:
            hits.sort(key=lambda h: (-h.score, -h.entry.mtime, h.entry.name.lower()))
        return hits[:limit]

    def find_by_name(self, document_name: str) -> list[FileEntry]:
        """Files whose name is exactly `document_name` ("Budget.xlsx"), or, when the name has no
        extension ("Budget"), whose name without extension matches. Newest first."""
        with self._lock:
            if self._by_name is None:
                table: dict[str, list[FileEntry]] = {}
                for e in self.entries:
                    table.setdefault(e.name.lower(), []).append(e)
                    table.setdefault(normalize(os.path.splitext(e.name)[0]), []).append(e)
                self._by_name = table
            table = self._by_name
        key = document_name.strip().lower()
        found = table.get(key, [])
        if not found:
            found = table.get(normalize(os.path.splitext(document_name)[0] if "." in document_name else document_name), [])
        unique = {e.path: e for e in found}
        return sorted(unique.values(), key=lambda e: -e.mtime)

    def search_folders(self, terms: list[str], folder: Optional[Path] = None, limit: int = 10) -> list[Hit]:
        """Ranked folder-name matches (same ranking as files). Folders inside `folder` only, if given."""
        with self._lock:
            folders = self.folders
        prefix = (str(folder).lower().rstrip("\\/") + os.sep) if folder else None
        words = [normalize(t) for t in terms if normalize(t)]
        if not words:
            return []
        phrase = " ".join(words)
        hits = []
        for e in folders:
            if prefix and not e.path.lower().startswith(prefix):
                continue
            s = self._score(e, phrase, words)
            if s > 0:
                hits.append(Hit(e, s))
        hits.sort(key=lambda h: (-h.score, len(h.entry.path), h.entry.name.lower()))
        return hits[:limit]

    @staticmethod
    def _singular(word: str) -> str:
        """'invoices' -> 'invoice' (so a plural in the request still matches the file name)."""
        if len(word) > 3 and word.endswith("ies"):
            return word[:-3] + "y"
        if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
            return word[:-1]
        return word

    @classmethod
    def _score(cls, e: FileEntry, phrase: str, words: list[str]) -> float:
        n = e.norm
        alt_words = [cls._singular(w) for w in words]
        alt_phrase = " ".join(alt_words)
        for p in (phrase, alt_phrase):
            if n == p:
                return 100.0
        for p in (phrase, alt_phrase):
            if n.startswith(p):
                return 85.0 + min(5.0, len(p) / max(len(n), 1) * 5)
        for p in (phrase, alt_phrase):
            if p in n:
                return 75.0
        found = sum(1 for w, a in zip(words, alt_words) if w in n or a in n)
        if found == len(words):
            return 60.0 + 5.0 * len(words) / max(len(n.split()), 1)
        if found and found * 2 >= len(words):
            return 25.0 + 20.0 * found / len(words)
        return 0.0

    @staticmethod
    def _fuzzy(pool: list[FileEntry], phrase: str) -> list[Hit]:
        by_norm: dict[str, list[FileEntry]] = {}
        for e in pool:
            by_norm.setdefault(e.norm, []).append(e)
        close = difflib.get_close_matches(phrase, list(by_norm), n=10, cutoff=0.72)
        hits = []
        for rank, name in enumerate(close):
            ratio = difflib.SequenceMatcher(None, phrase, name).ratio()
            hits += [Hit(e, 20.0 + ratio * 30.0 - rank * 0.1) for e in by_norm[name]]
        return hits
