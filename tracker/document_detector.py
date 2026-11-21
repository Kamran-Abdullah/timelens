"""Extract a document/file NAME from a window title.

Only window titles are used; document contents are never read and full paths are never
kept. Each family of applications has its own small adapter because their title formats
differ. Applications without an adapter (and browsers, whose titles are web page names
that we deliberately do not store) report no document - we do not guess.

Honest support matrix (document detection depends on what the app puts in its title):
  * Excel / Word / PowerPoint ... supported (title: "Book.xlsx - Excel")
  * Visual Studio Code .......... supported (title: "file.py - Folder - Visual Studio Code")
  * Notepad / Notepad++ ......... supported
  * PDF readers (Acrobat, Sumatra, Foxit) ... supported when title carries the file name
  * Everything else ............. only if a known file extension appears in the title
  * Browsers .................... never (privacy)
"""
from __future__ import annotations

import ntpath
import re
from dataclasses import dataclass
from typing import Callable, Optional

# Separators between title parts: hyphen, en dash, em dash surrounded by spaces.
_SPLIT = re.compile(r"\s[-–—]\s")
_DOC_EXT = re.compile(
    r"\.(xlsx|xlsm|xlsb|xls|csv|docx|docm|doc|rtf|pptx|pptm|ppt|pdf|txt|md|json|xml|"
    r"py|js|ts|html|css|sql|log|odt|ods|odp)$", re.IGNORECASE)
_BRACKET_SUFFIX = re.compile(r"\s*\[(?:Read-Only|Compatibility Mode|Protected View|Repaired|"
                             r"Group|Not saved)[^\]]*\]", re.IGNORECASE)
_UNSAVED_MARKS = "*●• "  # leading '*', '●', '•' mark unsaved changes

BROWSER_EXES = {"chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe",
                "vivaldi.exe", "iexplore.exe"}


@dataclass(frozen=True)
class DocumentInfo:
    name: str              # file name only, never a path
    is_unsaved: bool = False   # True for never-saved docs ("Book1", "Untitled-1")


def _basename(text: str) -> str:
    """Strip any folder path some apps put in the title (privacy: names only)."""
    if "\\" in text or "/" in text:
        return ntpath.basename(text.replace("/", "\\"))
    return text


def _clean(part: str) -> str:
    part = _BRACKET_SUFFIX.sub("", part).strip()
    part = part.lstrip(_UNSAVED_MARKS).strip()
    return _basename(part).strip()


def _looks_unsaved(name: str) -> bool:
    return bool(re.fullmatch(r"(Book|Document|Presentation|Untitled)[- ]?\d*", name, re.I))


# --- adapters --------------------------------------------------------------------
# Each takes the window title and returns DocumentInfo or None.

def _office(app_word: str) -> Callable[[str], Optional[DocumentInfo]]:
    def adapter(title: str) -> Optional[DocumentInfo]:
        if title.strip().lower() in {app_word.lower(), f"microsoft {app_word.lower()}", ""}:
            return None  # start screen, no document open
        # PowerPoint slideshow: "PowerPoint Slide Show - [Deck.pptx]"
        m = re.match(r"PowerPoint Slide Show\s*-\s*\[(.+)\]", title, re.I)
        if m:
            return DocumentInfo(_clean(m.group(1)))
        first = _clean(_SPLIT.split(title)[0])
        if not first or first.lower() in {app_word.lower(), "microsoft " + app_word.lower()}:
            return None
        return DocumentInfo(first, is_unsaved=_looks_unsaved(first) and "." not in first)
    return adapter


def _vscode(title: str) -> Optional[DocumentInfo]:
    parts = _SPLIT.split(title)
    if len(parts) < 2:
        return None  # e.g. just "Visual Studio Code" (no editor tab open)
    first = _clean(parts[0])
    if first.lower().startswith("untitled"):
        return DocumentInfo(first, is_unsaved=True)
    return DocumentInfo(first) if _DOC_EXT.search(first) else None


def _notepad(title: str) -> Optional[DocumentInfo]:
    parts = _SPLIT.split(title)
    first = _clean(parts[0])
    if not first or first.lower().startswith("notepad"):
        return None
    return DocumentInfo(first, is_unsaved=first.lower().startswith("untitled"))


def _pdf_reader(title: str) -> Optional[DocumentInfo]:
    for part in _SPLIT.split(title):
        cleaned = _clean(part)
        if cleaned.lower().endswith(".pdf"):
            return DocumentInfo(cleaned)
    return None


def _generic(title: str) -> Optional[DocumentInfo]:
    """Fallback: accept a title part only if it ends with a known document extension."""
    for part in _SPLIT.split(title):
        cleaned = _clean(part)
        if _DOC_EXT.search(cleaned) and len(cleaned) <= 255:
            return DocumentInfo(cleaned)
    return None


_ADAPTERS: dict[str, Callable[[str], Optional[DocumentInfo]]] = {
    "excel.exe": _office("Excel"),
    "winword.exe": _office("Word"),
    "powerpnt.exe": _office("PowerPoint"),
    "code.exe": _vscode,
    "notepad.exe": _notepad,
    "notepad++.exe": _notepad,
    "acrord32.exe": _pdf_reader,
    "acrobat.exe": _pdf_reader,
    "sumatrapdf.exe": _pdf_reader,
    "foxitreader.exe": _pdf_reader,
    "foxitpdfreader.exe": _pdf_reader,
}


def detect_document(executable_name: str, title: str) -> Optional[DocumentInfo]:
    """Return the document shown in this window, or None if it cannot be determined."""
    try:
        exe = (executable_name or "").lower()
        if exe in BROWSER_EXES or not title:
            return None
        adapter = _ADAPTERS.get(exe, _generic)
        return adapter(title)
    except Exception:  # unknown/odd titles must never crash tracking
        return None
