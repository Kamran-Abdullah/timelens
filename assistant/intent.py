"""Turns a typed request into a structured Intent. Pure Python, no Windows, no AI model.

This is plain rule-based language handling, not machine learning: it recognises a handful
of verbs (open / find / show / where is ...), file kinds (excel, pdf ...), places (downloads,
desktop ...) and times (today, last week ...). Anything else becomes a name search.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from assistant.apps import is_blocked_name
from assistant.safety import DANGEROUS_EXTENSIONS, KIND_EXTENSIONS, SAFE_EXTENSIONS

HELP, OPEN, SEARCH, REVEAL, ACTIVITY, RECENT = "help", "open", "search", "reveal", "activity", "recent"
FOLDERS = "folders"      # "show my most used folders"

OPEN_VERBS = ("open", "launch", "start", "run", "load", "display")
SEARCH_VERBS = ("find", "search", "look for", "locate", "list", "get me", "show me", "show", "pull up",
                "do i have")

# Words that carry no file-name information.
STOPWORDS = frozenset({
    "the", "a", "an", "my", "me", "mine", "file", "files", "document", "documents", "doc", "docs",
    "please", "pls", "can", "could", "you", "would", "for", "of", "to", "in", "on", "from", "named",
    "called", "with", "that", "which", "is", "are", "i", "want", "need", "like", "it", "up", "all",
    "any", "some", "there", "one", "again", "just", "also", "and", "or", "at", "by", "using", "use",
    "open", "launch", "start", "run", "load", "display", "find", "search", "look", "locate", "list",
    "get", "show", "pull", "where", "wheres", "folder", "location", "explorer", "reveal", "do", "have",
    "recent", "recently", "latest", "last", "newest", "today", "yesterday", "this", "week", "month",
    "ago", "modified", "edited", "saved", "created", "worked", "used", "s", "folders", "directory",
    "directories",
})
RECENCY_WORDS = frozenset({"recent", "recently", "latest", "last", "newest"})
FILE_WORDS = frozenset({"file", "files", "document", "documents", "doc", "docs", "folder", "folders",
                        "directory", "directories"})

# Words that name a kind of file. Ambiguous words ("notes", "sheet", "slides") are NOT here: they
# are far more likely to be part of a file name.
KIND_WORDS = {
    "excel": "excel", "spreadsheet": "excel", "spreadsheets": "excel", "xlsx": "excel", "xls": "excel",
    "csv": "excel",
    "word": "word", "docx": "word", "rtf": "word",
    "powerpoint": "powerpoint", "ppt": "powerpoint", "pptx": "powerpoint", "presentation": "powerpoint",
    "presentations": "powerpoint",
    "pdf": "pdf", "pdfs": "pdf",
    "text": "text", "txt": "text",
    "image": "image", "images": "image", "picture": "image", "pictures": "image", "photo": "image",
    "photos": "image", "png": "image", "jpg": "image", "jpeg": "image",
    "video": "video", "videos": "video", "mp4": "video",
    "audio": "audio", "music": "audio", "mp3": "audio",
}
PLACES = {"downloads": "downloads", "download": "downloads", "desktop": "desktop"}
# "documents" is also a generic word ("word documents"), so it names a folder only after a preposition.
_PLACE_AFTER_PREPOSITION = re.compile(
    r"\b(?:in|from|inside|under|on)\s+(?:my\s+|the\s+)?(downloads?|desktop|documents)(?:\s+folder)?\b")
# A full Windows path such as C:\Users\me\file.pdf (stops at quotes and trailing punctuation).
_WINDOWS_PATH = re.compile(r'[A-Za-z]:\\[^"<>|?*\r\n]*[^"<>|?*\r\n\s.,;!]')
_BLOCKED_NAME = re.compile(r"[\w\-&'()]+\.(?:%s)\b" % "|".join(e.lstrip(".") for e in sorted(DANGEROUS_EXTENSIONS)),
                           re.IGNORECASE)
TIME_PHRASES = (
    (r"\blast week\b", "Last week"), (r"\blast month\b", "Last month"),
    (r"\bthis week\b", "This week"), (r"\bthis month\b", "This month"),
    (r"\byesterday\b", "Yesterday"), (r"\btoday\b", "Today"),
)
ACTIVITY_PATTERNS = (
    r"what (did|have) i (work|worked|do|done|use|used)", r"what was i (working|doing)",
    r"which (documents|files|apps|applications|programs) (did|have) i",
    r"(top|most used) (apps|applications|documents|files)",
    r"how (long|much time).*(spend|spent|use|used|work|worked)", r"\btime spent\b", r"\bsummary\b",
    r"what('s| is| was) (my )?(activity|usage)", r"documents? i (used|worked|opened|edited)",
)
FREQUENT_FOLDERS = (re.compile(r"\b(most used|most-used|frequent|frequently used|often used|favou?rite|top|useful|"
                               r"important|usual|regular|popular)\b.{0,25}\bfolders?\b"),
                    re.compile(r"\bfolders?\b.{0,25}\b(i use|i used|use most|use often|used most|used often)\b"),
                    re.compile(r"\bwhich folders?\b.{0,30}\b(do i|did i|i)\b"))
REVEAL_PHRASES = ("show in folder", "show in explorer", "folder of", "folder for", "folder containing",
                  "file location", "location of", "reveal")
_FOLDER_WORD = re.compile(r"\b(folders?|directory|directories)\b")

_EXT_ALT = "|".join(sorted((e.lstrip(".") for e in SAFE_EXTENSIONS), key=len, reverse=True))
_NAME_WITH_EXT = re.compile(r"((?:[\w\-&'()]+\s+){0,8}[\w\-&'()]+\.(?:%s))(?![\w])" % _EXT_ALT, re.IGNORECASE)
_LEADING = STOPWORDS | set(OPEN_VERBS) | {"find", "search", "show", "where", "pull", "up"}


@dataclass
class Intent:
    kind: str
    terms: list[str] = field(default_factory=list)        # words to match in the file name
    exts: Optional[tuple[str, ...]] = None                 # file types, e.g. (".xlsx", ".xls")
    place: Optional[str] = None                            # downloads / desktop / documents
    period: Optional[str] = None                           # "Today", "Last week" ...
    newest: bool = False                                   # "latest", "last", "recent"
    wants_folder: bool = False                             # "show in folder", "where is"
    exact_name: Optional[str] = None                       # "October Payroll.xlsx" typed in full
    path: Optional[str] = None                             # a full Windows path typed in the request
    folder_target: bool = False                            # the user means a FOLDER ("open the payroll folder")
    blocked_name: Optional[str] = None                     # a program/script name ("setup.exe") - refused
    app_terms: list[str] = field(default_factory=list)    # words that may name an installed APP ("chrome", "excel")
    mentions_file: bool = False                            # said file/document/folder: they mean a file, not an app
    verb: str = ""                                         # open / launch / start / run ...
    blocked_app: Optional[str] = None                      # a command-line/admin tool name - refused
    text: str = ""


def _exact_file_name(raw: str) -> Optional[str]:
    m = _NAME_WITH_EXT.search(raw)
    if not m:
        return None
    words = m.group(1).split()
    while len(words) > 1 and words[0].lower() in _LEADING:
        words.pop(0)
    name = " ".join(words)
    return name if "." in name else None


def parse(text: str) -> Intent:
    raw = (text or "").strip()
    low = raw.lower()
    if not low or re.fullmatch(r"(help|\?|what can you do\??|commands|examples?|how do (i|you) use (this|you)\??)", low):
        return Intent(kind=HELP, text=raw)
    intent = Intent(kind=SEARCH, text=raw)

    path_match = _WINDOWS_PATH.search(raw)
    if path_match:
        intent.path = path_match.group(0)
    blocked = _BLOCKED_NAME.search(raw)
    if blocked:
        intent.blocked_name = blocked.group(0)

    quoted = re.search(r"[\"“‘]([^\"”’]+)[\"”’]", raw)
    exact = _exact_file_name(quoted.group(1) if quoted else raw)
    intent.exact_name = exact

    if not exact and any(p.search(low) for p in FREQUENT_FOLDERS):
        intent.kind = FOLDERS
        intent.period = next((n for pat, n in TIME_PHRASES if re.search(pat, low)), None)
        return intent

    if not exact and any(re.search(p, low) for p in ACTIVITY_PATTERNS):
        intent.kind = ACTIVITY
        intent.period = next((n for pat, n in TIME_PHRASES if re.search(pat, low)), "Today")
        return intent

    for pat, name in TIME_PHRASES:
        if re.search(pat, low):
            intent.period = name
            low = re.sub(pat, " ", low)
            break

    place = _PLACE_AFTER_PREPOSITION.search(low)
    if place:
        intent.place = "downloads" if place.group(1).startswith("download") else place.group(1)
        low = low[:place.start()] + " " + low[place.end():]

    words = re.findall(r"[a-z0-9]+", low)
    intent.newest = any(w in RECENCY_WORDS for w in words)
    verb_open = bool(re.match(r"^(please\s+|can you\s+|could you\s+)?(%s)\b" % "|".join(OPEN_VERBS), low))
    verb_search = any(re.match(r"^(please\s+|can you\s+|could you\s+)?" + re.escape(v) + r"\b", low)
                      for v in SEARCH_VERBS)
    wants_folder = any(p in low for p in REVEAL_PHRASES) or bool(re.search(r"\bwhere('s| is| are)\b", low))

    if wants_folder:
        intent.kind, intent.wants_folder = REVEAL, True
    elif verb_open and not verb_search:
        intent.kind = OPEN
    elif intent.newest and not (verb_open or verb_search) and not exact:
        intent.kind = RECENT

    verb = re.match(r"^(?:please\s+|can you\s+|could you\s+)?(%s)\b" % "|".join(OPEN_VERBS), low)
    intent.verb = verb.group(1) if verb else ""
    intent.mentions_file = any(w in FILE_WORDS for w in words)
    skip = set(re.findall(r"[a-z0-9]+", exact.lower())) if exact else set()
    quoted_words = set(re.findall(r"[a-z0-9]+", quoted.group(1).lower())) if quoted else set()
    terms: list[str] = []
    exts: list[str] = []
    app_terms: list[str] = []
    for w in re.findall(r"[a-z0-9]+(?:['&-][a-z0-9]+)*", low):
        if w in skip:
            continue
        if w not in STOPWORDS and w not in PLACES:
            app_terms.append(w)
        if w in PLACES:
            intent.place = PLACES[w]
        elif w in KIND_WORDS and w not in quoted_words:
            exts += [e for e in KIND_EXTENSIONS[KIND_WORDS[w]] if e not in exts]
        elif w in STOPWORDS and w not in quoted_words:
            continue
        else:
            terms.append(w)
    intent.terms = terms
    intent.exts = tuple(exts) if exts else None
    intent.app_terms = app_terms
    if intent.kind == OPEN and not exact and app_terms and is_blocked_name(" ".join(app_terms)):
        intent.blocked_app = " ".join(app_terms)            # "open command prompt", "open powershell"
    if not wants_folder and _FOLDER_WORD.search(low) and (terms or intent.place):
        intent.folder_target = True                          # "open downloads folder", "find the payroll folder"
    elif (intent.kind == OPEN and intent.place and not terms and not intent.exts and not exact
          and not intent.newest):
        intent.folder_target = True                          # "open downloads"
    elif wants_folder and _FOLDER_WORD.search(low) and not exact and terms and not any(
            p in low for p in ("folder of", "folder for", "folder containing", "show in folder")):
        intent.folder_target = True                          # "where is the payroll folder"
    return intent
