"""Assistant page: a chat window to find and open files by typing what you want.

Offline and rule-based (no AI model, no internet). Chat history lives only on screen - it is
never saved - and file paths are held in memory only (see assistant.file_index).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QMenu, QPushButton,
                               QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

from assistant.engine import HELP_TEXT, AssistantEngine, Reply
from assistant.apps import AppEntry
from assistant.file_index import FileEntry, FileIndex, default_roots
from database.db_manager import Database
from ui.widgets import page_layout, add_header
from ui.workers import run_async

log = logging.getLogger(__name__)

WELCOME = ("Hi! Tell me which file, folder or app you want and I'll find or open it. For example:\n"
           "  open October Payroll.xlsx   |   find invoices in downloads   |   open chrome   |   open my downloads folder\n"
           "Type help to see everything I can do. I never run scripts or command-line tools.")
CHIPS = ("Recent Excel files", "Open my latest PDF", "What did I work on today?", "My most used folders", "Help")


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


def _short_folder(folder: str, keep: int = 3) -> str:
    """Shorten a long folder path to its last few parts (the full path stays in the tooltip)."""
    parts = Path(folder).parts
    return folder if len(parts) <= keep + 1 else "...\\" + "\\".join(parts[-keep:])


class _FileRow(QFrame):
    """One search result: name, folder, date, size, and Open / Show in folder buttons."""

    def __init__(self, entry: FileEntry, on_open: Callable[[str], None], on_reveal: Callable[[str], None]):
        super().__init__()
        self.setObjectName("fileRow")
        self.entry = entry
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 8, 10, 8)
        text = QVBoxLayout()
        text.setSpacing(0)
        name = QLabel(entry.name + ("  (folder)" if entry.is_dir else ""))
        name.setObjectName("fileName")
        meta = QLabel(f"{_short_folder(entry.folder)}   -   {entry.modified:%d %b %Y %H:%M}"
                      + ("" if entry.is_dir else f"   -   {_human_size(entry.size)}"))
        meta.setObjectName("sub")
        meta.setToolTip(entry.path)
        meta.setTextInteractionFlags(Qt.TextSelectableByMouse)
        text.addWidget(name)
        text.addWidget(meta)
        row.addLayout(text, 1)
        self.btn_open = QPushButton("Open folder" if entry.is_dir else "Open")
        self.btn_open.setObjectName("primary")
        self.btn_open.setCursor(Qt.PointingHandCursor)
        self.btn_open.clicked.connect(lambda: on_open(entry.path))
        self.btn_reveal = QPushButton("Show in folder")
        self.btn_reveal.setCursor(Qt.PointingHandCursor)
        self.btn_reveal.clicked.connect(lambda: on_reveal(entry.path))
        row.addWidget(self.btn_open)
        if not entry.is_dir:
            row.addWidget(self.btn_reveal)
        else:
            self.btn_reveal.hide()


class _AppRow(QFrame):
    """An application choice: name and an Open app button."""

    def __init__(self, entry: AppEntry, on_launch: Callable[[AppEntry], None]):
        super().__init__()
        self.setObjectName("fileRow")
        self.entry = entry
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 8, 10, 8)
        text = QVBoxLayout()
        text.setSpacing(0)
        name = QLabel(entry.name + "  (app)")
        name.setObjectName("fileName")
        note = QLabel("Installed application" if entry.kind == "startmenu" else "Windows tool")
        note.setObjectName("sub")
        text.addWidget(name)
        text.addWidget(note)
        row.addLayout(text, 1)
        self.btn_open = QPushButton("Open app")
        self.btn_open.setObjectName("primary")
        self.btn_open.setCursor(Qt.PointingHandCursor)
        self.btn_open.clicked.connect(lambda: on_launch(entry))
        row.addWidget(self.btn_open)


class AssistantPage(QWidget):
    auto_refresh = False

    def __init__(self, db: Database, index: Optional[FileIndex] = None,
                 engine: Optional[AssistantEngine] = None):
        super().__init__()
        self.setObjectName("page")
        self.db = db
        self.index = index or FileIndex(self._saved_roots() or None)
        self.engine = engine or AssistantEngine(self.index, db)
        self._busy = False
        self._indexing = False
        self._index_started = False
        self._folders_shown = False       # the most-used-folders message is posted once, after indexing
        self._reindex_again = False      # a rebuild was asked for while one was running
        # replaceable for tests
        self.pick_folder: Callable[[], str] = lambda: QFileDialog.getExistingDirectory(
            self, "Add a folder for the assistant to search")

        root = page_layout(self, 12)
        self.sub = add_header(root, "Assistant", "Ask me to find or open a file.")

        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.chips: list[QPushButton] = []
        for text in CHIPS:
            chip = QPushButton(text)
            chip.setObjectName("chip")
            chip.setCursor(Qt.PointingHandCursor)
            chip.clicked.connect(lambda _=False, t=text: self.ask(t))
            bar.addWidget(chip)
            self.chips.append(chip)
        bar.addStretch(1)
        self.btn_folders = QPushButton("Folders")
        self.menu = QMenu(self)
        self.menu.aboutToShow.connect(self._fill_folder_menu)
        self.btn_folders.setMenu(self.menu)
        self.btn_rebuild = QPushButton("Rebuild index")
        self.btn_rebuild.clicked.connect(self.rebuild_index)
        self.btn_clear = QPushButton("Clear chat")
        self.btn_clear.clicked.connect(self.clear_chat)
        for b in (self.btn_folders, self.btn_rebuild, self.btn_clear):
            bar.addWidget(b)
        root.addLayout(bar)

        # --- conversation
        panel = QFrame()
        panel.setObjectName("card")
        pl = QVBoxLayout(panel)
        pl.setContentsMargins(6, 6, 6, 6)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.chat = QWidget()
        self.chat.setObjectName("chatArea")
        self.chat_layout = QVBoxLayout(self.chat)
        self.chat_layout.setContentsMargins(12, 12, 12, 12)
        self.chat_layout.setSpacing(10)
        self.chat_layout.addStretch(1)
        self.scroll.setWidget(self.chat)
        pl.addWidget(self.scroll)
        root.addWidget(panel, 1)

        # --- input
        row = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setPlaceholderText("Ask me to open or find a file...   e.g. open October Payroll.xlsx")
        self.input.setMinimumHeight(40)
        self.input.returnPressed.connect(self._send)
        self.btn_send = QPushButton("Send")
        self.btn_send.setObjectName("primary")
        self.btn_send.setMinimumHeight(40)
        self.btn_send.clicked.connect(self._send)
        row.addWidget(self.input, 1)
        row.addWidget(self.btn_send)
        root.addLayout(row)

        self.messages: list[tuple[str, str]] = []        # (who, text) - kept in memory for tests
        self._say_bot(WELCOME)

    # --- folders ---------------------------------------------------------------------
    def _saved_roots(self) -> list[Path]:
        raw = self.db.get_setting("assistant_folders").strip()
        return [Path(p) for p in raw.split(";") if p.strip()] if raw else []

    def _save_roots(self, roots: list[Path]) -> None:
        self.db.set_setting("assistant_folders", ";".join(str(r) for r in roots))

    def current_roots(self) -> list[Path]:
        return list(self.index.roots)

    def add_folder(self, path: str) -> bool:
        p = Path(path)
        if not p.is_dir():
            self._say_bot(f"{path} is not a folder I can read.")
            return False
        roots = self.current_roots()
        if p not in roots:
            roots.append(p)
        self.index.set_roots(roots)
        self._save_roots(self.index.roots)
        self._say_bot(f"Added {p}. Re-indexing...")
        self.rebuild_index()
        return True

    def remove_folder(self, path: str) -> None:
        roots = [r for r in self.current_roots() if str(r) != path]
        self.index.set_roots(roots)
        self._save_roots(self.index.roots)
        self.rebuild_index()

    def reset_folders(self) -> None:
        self.index.set_roots(default_roots())
        self._save_roots([])                              # empty = "use the defaults"
        self.rebuild_index()

    def _fill_folder_menu(self) -> None:
        self.menu.clear()
        self.menu.addAction("Add a folder...").triggered.connect(lambda: self._add_folder_dialog())
        remove = self.menu.addMenu("Stop searching")
        for r in self.current_roots():
            remove.addAction(str(r)).triggered.connect(lambda _=False, p=str(r): self.remove_folder(p))
        remove.setEnabled(bool(self.current_roots()))
        self.menu.addSeparator()
        self.menu.addAction("Use the default folders").triggered.connect(self.reset_folders)

    def _add_folder_dialog(self) -> None:
        folder = self.pick_folder()
        if folder:
            self.add_folder(folder)

    # --- index -------------------------------------------------------------------------
    def refresh(self) -> None:
        """Called when the page is shown: build the index once, in the background."""
        if not self._index_started:
            self.rebuild_index()

    def rebuild_index(self) -> None:
        if self._indexing:
            self._reindex_again = True       # run once more as soon as the current build ends
            return
        self._index_started = self._indexing = True
        self.sub.setText("Indexing your folders ...")
        self.btn_rebuild.setEnabled(False)
        index = self.index
        run_async(index.build, self._on_indexed, self._on_index_error)

    def _on_indexed(self, count: int) -> None:
        self._indexing = False
        self.btn_rebuild.setEnabled(True)
        if self._reindex_again:              # folders changed during the build: the result is stale
            self._reindex_again = False
            self.rebuild_index()
            return
        self.sub.setText(self._status_text())
        if not self._folders_shown:
            self._folders_shown = True
            engine = self.engine
            run_async(lambda: engine.frequent_folders(), self._on_auto_folders, lambda _m: None)

    def _on_auto_folders(self, reply: Reply) -> None:
        if reply.files:                    # only speak up if there is something useful to show
            self._say_bot(reply.text, reply.files)

    def _on_index_error(self, message: str) -> None:
        self._indexing = False
        self.btn_rebuild.setEnabled(True)
        self.sub.setText(f"Could not index your folders: {message}")

    def _status_text(self) -> str:
        i = self.index
        extra = " (limit reached - add fewer folders)" if i.truncated else ""
        return (f"Searching {len(i.entries):,} documents in {len(i.roots)} folder(s) "
                f"- indexed in {i.build_seconds:.1f} s{extra}. Nothing leaves this computer.")

    # --- chat ------------------------------------------------------------------------------
    def clear_chat(self) -> None:
        while self.chat_layout.count() > 1:
            item = self.chat_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.messages.clear()
        self._say_bot(WELCOME)

    def ask(self, text: str) -> None:
        """Put `text` in the box and send it (used by the suggestion chips and by tests)."""
        self.input.setText(text)
        self._send()

    def _send(self) -> None:
        text = self.input.text().strip()
        if not text or self._busy:
            return
        self.input.clear()
        self._say_user(text)
        self._set_busy(True)
        engine = self.engine
        run_async(lambda: engine.handle(text), self._on_reply, self._on_reply_error)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.btn_send.setEnabled(not busy)
        self.input.setEnabled(not busy)
        if not busy:
            self.input.setFocus()

    def _on_reply(self, reply: Reply) -> None:
        self._set_busy(False)
        self._say_bot(reply.text, reply.files, reply.apps)

    def _on_reply_error(self, message: str) -> None:
        self._set_busy(False)
        self._say_bot(f"Sorry, something went wrong: {message}")

    # file-row actions run off the GUI thread too (opening can be slow for big files)
    def _open(self, path: str) -> None:
        run_async(lambda: self.engine.open_path(path), self._on_action_reply, self._on_reply_error)

    def _reveal(self, path: str) -> None:
        run_async(lambda: self.engine.reveal_path(path), self._on_action_reply, self._on_reply_error)

    def _launch(self, entry: AppEntry) -> None:
        run_async(lambda: self.engine.launch_app(entry), self._on_action_reply, self._on_reply_error)

    def _on_action_reply(self, reply: Reply) -> None:
        self._say_bot(reply.text)

    # --- drawing messages ---------------------------------------------------------------------
    def _say_user(self, text: str) -> None:
        self.messages.append(("user", text))
        bubble = QLabel(text)
        bubble.setObjectName("userBubble")
        bubble.setWordWrap(True)
        bubble.setTextInteractionFlags(Qt.TextSelectableByMouse)
        bubble.setMaximumWidth(560)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(bubble)
        self._add_row(row)

    def _say_bot(self, text: str, files: Optional[list[FileEntry]] = None,
                 apps: Optional[list[AppEntry]] = None) -> None:
        self.messages.append(("bot", text))
        box = QFrame()
        box.setObjectName("botBubble")
        lay = QVBoxLayout(box)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(8)
        label = QLabel(text)
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        label.setStyleSheet("background: transparent;")
        lay.addWidget(label)
        for entry in files or []:
            lay.addWidget(_FileRow(entry, self._open, self._reveal))
        for app_entry in apps or []:
            lay.addWidget(_AppRow(app_entry, self._launch))
        box.setMinimumWidth(420)       # short answers stay on one line
        box.setMaximumWidth(820)
        box.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        row = QHBoxLayout()
        row.addWidget(box)
        row.addStretch(1)
        self._add_row(row)

    def _add_row(self, row: QHBoxLayout) -> None:
        holder = QWidget()
        holder.setLayout(row)
        row.setContentsMargins(0, 0, 0, 0)
        self.chat_layout.insertWidget(self.chat_layout.count() - 1, holder)
        QTimer.singleShot(30, lambda: self.scroll.verticalScrollBar().setValue(
            self.scroll.verticalScrollBar().maximum()))

    def file_rows(self) -> list[_FileRow]:
        """All result rows currently on screen (for tests)."""
        return self.chat.findChildren(_FileRow)
