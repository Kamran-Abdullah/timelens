"""Applications the assistant is allowed to launch.

Launching software is more powerful than opening a document, so the rules are strict:
  * Only programs the user has INSTALLED (their Start Menu shortcuts) and a fixed list of
    everyday Windows tools can be launched. A program is never started from a typed path.
  * Command-line and administration tools (Command Prompt, PowerShell, Terminal, Registry
    Editor, Python shells, WSL ...) are never launched.
  * Uninstallers, installers, repair tools, help and web-link shortcuts are left out.
Like the file index, this list lives in memory only.
"""
from __future__ import annotations

import logging
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from assistant.file_index import FileIndex, normalize

log = logging.getLogger(__name__)

# (display name, what to run, extra names people say)
SYSTEM_APPS = (
    ("Notepad", "notepad.exe", ()),
    ("Calculator", "calc.exe", ("calc",)),
    ("Paint", "mspaint.exe", ("ms paint", "mspaint")),
    ("File Explorer", "explorer.exe", ("explorer", "windows explorer")),
    ("Task Manager", "taskmgr.exe", ("task manager",)),
    ("Settings", "ms-settings:", ("windows settings", "system settings")),
    ("Control Panel", "control.exe", ()),
    ("Snipping Tool", "snippingtool.exe", ("snip", "screenshot tool")),
)

# Names that are never launched, whatever the Start Menu says.
BLOCKED_WORDS = ("command prompt", "cmd", "powershell", "terminal", "console", "regedit", "registry",
                 "wsl", "ubuntu", "debian", "git bash", "bash", "python", "idle", "mmc", "shell",
                 # system administration
                 "administrative tools", "computer management", "event viewer", "component services",
                 "performance monitor", "print management", "odbc", "services", "task scheduler",
                 "disk management", "group policy", "local security", "system configuration",
                 "memory diagnostics", "resource monitor", "disk cleanup", "defragment", "msconfig")
VENDOR_WORDS = frozenset({"microsoft", "ms", "office", "google", "adobe"})
# Shortcuts that are not "the program itself".
_NOT_AN_APP = re.compile(r"uninstall|remove|repair|setup|installer|license|licence|readme|\bhelp\b|manual|"
                         r"website|documentation|dokumentation|\bfaq\b|homepage|activation|release notes|"
                         r"support|check for updates|\bupdate\b|additional tools", re.IGNORECASE)


def default_start_menu_roots() -> list[Path]:
    roots = []
    for var in ("ProgramData", "APPDATA"):
        base = os.environ.get(var)
        if base:
            roots.append(Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    return [r for r in roots if r.is_dir()]


def is_blocked_name(text: str) -> bool:
    n = " " + normalize(text) + " "
    return any(f" {w} " in n for w in BLOCKED_WORDS)


@dataclass(frozen=True)
class AppEntry:
    name: str          # "Google Chrome"
    norm: str
    target: str        # a Start Menu shortcut path, or a fixed Windows command
    kind: str          # "startmenu" | "system"


@dataclass
class AppHit:
    entry: AppEntry
    score: float


class AppIndex:
    def __init__(self, roots: Optional[Iterable[Path]] = None):
        self.roots = list(roots) if roots is not None else default_start_menu_roots()
        self.entries: list[AppEntry] = []
        self._targets: set[str] = set()
        self.built = False
        self._lock = threading.Lock()

    @property
    def ready(self) -> bool:
        return self.built

    def build(self) -> int:
        entries: list[AppEntry] = []
        seen: set[str] = set()
        for name, target, aliases in SYSTEM_APPS:
            for label in (name, *aliases):
                entries.append(AppEntry(name, normalize(label), target, "system"))
            seen.add(normalize(name))
        for root in self.roots:
            try:
                for lnk in root.rglob("*.lnk"):
                    name = lnk.stem
                    n = normalize(name)
                    if not n or n in seen or _NOT_AN_APP.search(name) or is_blocked_name(name):
                        continue
                    seen.add(n)
                    entries.append(AppEntry(name, n, str(lnk), "startmenu"))
            except OSError:
                log.debug("Could not read %s", root, exc_info=True)
        with self._lock:
            self.entries = entries
            self._targets = {e.target for e in entries}
            self.built = True
        return len(entries)

    def is_known(self, target: str) -> bool:
        """Only these targets may ever be launched (UI buttons cannot smuggle in other paths)."""
        with self._lock:
            return target in self._targets

    def search(self, terms: list[str], limit: int = 6) -> list[AppHit]:
        words = [normalize(t) for t in terms if normalize(t)]
        if not words:
            return []
        # People say "Microsoft Word" or "MS Word" but the Start Menu entry is just "Word":
        # also try the request without the vendor words.
        trimmed = [w for w in words if w not in VENDOR_WORDS]
        variants = [words] + ([trimmed] if trimmed and trimmed != words else [])
        with self._lock:
            entries = self.entries
        best: dict[str, AppHit] = {}
        for e in entries:
            for variant in variants:
                s = FileIndex._score(e, " ".join(variant), variant)     # same ranking as file names
                if s > 0 and (e.target not in best or s > best[e.target].score):
                    best[e.target] = AppHit(e, s)
        return sorted(best.values(), key=lambda h: (-h.score, len(h.entry.name), h.entry.name.lower()))[:limit]
