"""Settings page: startup, background behaviour, idle threshold, privacy and backup.

Every control saves immediately (there is no Save button) and says what it did. Dialogs
(confirmations, file pickers) are attributes so tests can replace them.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QDate, Qt
from PySide6.QtCore import Qt as _Qt
from PySide6.QtWidgets import (QListWidget, QListWidgetItem, QPlainTextEdit, QCheckBox, QDateEdit, QFileDialog, QFrame, QHBoxLayout, QLabel,
                               QLineEdit, QMessageBox, QPushButton, QRadioButton, QScrollArea,
                               QSpinBox, QTableWidgetItem, QVBoxLayout, QWidget)

import config
from analytics.periods import range_for_days
from database.db_manager import Database
from services import backup_manager, web_history
from services.startup_manager import StartupManager
from services.tray_manager import open_data_folder
from tracker.engine import TrackingEngine
from ui.web_consent import ask_web_consent
from ui.widgets import Panel, add_header, make_table

log = logging.getLogger(__name__)


def _confirm(parent, title: str, text: str) -> bool:
    return QMessageBox.question(parent, title, text) == QMessageBox.Yes


class SettingsPage(QWidget):
    auto_refresh = False

    def __init__(self, db: Database, engine: TrackingEngine, startup: StartupManager,
                 on_changed: Callable[[], None] = lambda: None,
                 on_restore: Optional[Callable[[Path], str]] = None,
                 on_data_changed: Callable[[], None] = lambda: None,
                 on_web_changed: Callable[[], None] = lambda: None):
        super().__init__()
        self.setObjectName("page")
        self.db, self.engine, self.startup = db, engine, startup
        self._on_changed, self._on_restore, self._on_data_changed = on_changed, on_restore, on_data_changed
        self._on_web_changed = on_web_changed
        self.ask_web_consent: Callable[[], Optional[int]] = lambda: ask_web_consent(self)
        # Replaceable dialogs (tests substitute these)
        self.confirm: Callable[[str, str], bool] = lambda t, x: _confirm(self, t, x)
        self.pick_save: Callable[[str, str, str], str] = lambda cap, default, flt: \
            QFileDialog.getSaveFileName(self, cap, default, flt)[0]
        self.pick_open: Callable[[str, str], str] = lambda cap, flt: \
            QFileDialog.getOpenFileName(self, cap, str(config.BACKUP_DIR), flt)[0]
        self.open_folder: Callable[[], None] = open_data_folder

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        outer.addWidget(scroll)
        content = QWidget()
        content.setObjectName("page")
        scroll.setWidget(content)
        root = QVBoxLayout(content)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(12)

        self.status = add_header(root, "Settings", "Changes are saved immediately.")

        root.addWidget(self._startup_panel())
        root.addWidget(self._behaviour_panel())
        root.addWidget(self._idle_panel())
        root.addWidget(self._exclusions_panel())
        root.addWidget(self._web_panel())
        root.addWidget(self._delete_panel())
        root.addWidget(self._backup_panel())
        root.addWidget(self._privacy_panel())
        root.addStretch(1)
        self.load()

    # --- panels ---------------------------------------------------------------------
    def _startup_panel(self) -> Panel:
        p = Panel("Startup")
        self.rb_auto = QRadioButton("Start automatically with Windows (runs in the background)")
        self.rb_manual = QRadioButton("Manual launch only")
        self.rb_auto.toggled.connect(lambda on: self.set_startup(on) if self._loaded else None)
        for w in (self.rb_auto, self.rb_manual):
            p.body.addWidget(w)
        note = QLabel("Uses a shortcut in your own Windows Startup folder - no administrator "
                      "rights needed. Switching to manual removes that shortcut.")
        note.setObjectName("sub")
        note.setWordWrap(True)
        p.body.addWidget(note)
        return p

    def _behaviour_panel(self) -> Panel:
        p = Panel("Behaviour")
        self.flags: dict[str, QCheckBox] = {}
        for key, text in (("open_dashboard_on_startup", "Open the dashboard when TimeLens starts"),
                          ("background_monitoring", "Keep monitoring in the background when the window is closed"),
                          ("system_tray", "Show the system tray icon"),
                          ("minimize_to_tray", "Closing the window minimizes TimeLens to the tray")):
            box = QCheckBox(text)
            box.toggled.connect(lambda on, k=key: self.set_flag(k, on) if self._loaded else None)
            self.flags[key] = box
            p.body.addWidget(box)
        note = QLabel("Closing the window only hides it when the tray icon and minimize-to-tray are "
                      "both on; otherwise closing exits TimeLens and stops monitoring. If background "
                      "monitoring is off, monitoring pauses while the window is hidden. "
                      "Tray > Exit TimeLens always stops everything.")
        note.setObjectName("sub")
        note.setWordWrap(True)
        p.body.addWidget(note)
        return p

    def _idle_panel(self) -> Panel:
        p = Panel("Idle detection")
        row = QHBoxLayout()
        row.addWidget(QLabel("Count the computer as idle after"))
        self.idle_box = QSpinBox()
        self.idle_box.setRange(1, 240)
        self.idle_box.setSuffix(" minutes without keyboard or mouse input")
        self.idle_box.valueChanged.connect(lambda v: self.set_idle_minutes(v) if self._loaded else None)
        row.addWidget(self.idle_box)
        row.addStretch(1)
        p.body.addLayout(row)
        note = QLabel("Default 5 minutes. Takes effect immediately; history already recorded is not "
                      "recalculated.")
        note.setObjectName("sub")
        p.body.addWidget(note)
        return p

    def _exclusions_panel(self) -> Panel:
        p = Panel("Excluded applications")
        hint = QLabel("Excluded applications are still counted as time, but TimeLens does not store "
                      "their name, window titles or documents - they appear as \"Excluded Application\".")
        hint.setObjectName("sub")
        hint.setWordWrap(True)
        p.body.addWidget(hint)
        self.excl_table = make_table(["Application", "Executable", "Excluded"], scroll=True)
        self.excl_table.setMinimumHeight(150)
        self.excl_table.setMaximumHeight(220)
        p.body.addWidget(self.excl_table)
        row = QHBoxLayout()
        self.excl_name = QLineEdit()
        self.excl_name.setPlaceholderText("Add by executable name, e.g. chrome.exe")
        add = QPushButton("Exclude")
        add.clicked.connect(lambda: self.add_exclusion(self.excl_name.text()))
        self.excl_name.returnPressed.connect(add.click)
        self.btn_delete_app = QPushButton("Delete history of selected application")
        self.btn_delete_app.clicked.connect(self._delete_selected_app)
        row.addWidget(self.excl_name, 1)
        row.addWidget(add)
        row.addWidget(self.btn_delete_app)
        p.body.addLayout(row)
        return p

    def _web_panel(self) -> Panel:
        p = Panel("Websites (optional)")
        self.web_on = QCheckBox("Track the websites I visit in Chrome and Edge")
        self.web_on.toggled.connect(lambda on: self.set_web_tracking(on) if self._loaded else None)
        p.body.addWidget(self.web_on)
        note = QLabel("Off by default. When on, TimeLens copies the history your browser already keeps and "
                      "stores only the site name, page title, search words and times - never full web "
                      "addresses or page contents. Private/incognito browsing is not in browser history, so "
                      "it is never seen.")
        note.setObjectName("sub")
        note.setWordWrap(True)
        p.body.addWidget(note)
        row = QHBoxLayout()
        self.web_chrome = QCheckBox("Chrome")
        self.web_edge = QCheckBox("Edge")
        for key, box in (("chrome", self.web_chrome), ("edge", self.web_edge)):
            box.toggled.connect(lambda _on, k=key: self.save_web_browsers() if self._loaded else None)
            row.addWidget(box)
        row.addStretch(1)
        p.body.addLayout(row)
        p.body.addWidget(QLabel("Browser profiles to read (a browser can hold other people's profiles - "
                                "only Default is read until you tick more):"))
        self.web_profiles = QListWidget()
        self.web_profiles.setMaximumHeight(110)
        self.web_profiles.itemChanged.connect(lambda _i: self.save_web_profiles() if self._loaded else None)
        p.body.addWidget(self.web_profiles)
        p.body.addWidget(QLabel("Never record these sites (one per line, e.g. mybank.com):"))
        self.web_excluded = QPlainTextEdit()
        self.web_excluded.setMaximumHeight(84)
        p.body.addWidget(self.web_excluded)
        row2 = QHBoxLayout()
        save = QPushButton("Save excluded sites")
        save.clicked.connect(lambda: self.save_web_excluded(self.web_excluded.toPlainText()))
        wipe = QPushButton("Delete all website history")
        wipe.clicked.connect(self.delete_web_history)
        row2.addWidget(save)
        row2.addStretch(1)
        row2.addWidget(wipe)
        p.body.addLayout(row2)
        return p

    def _delete_panel(self) -> Panel:
        p = Panel("Delete activity history")
        row = QHBoxLayout()
        today = QDate.currentDate()
        self.del_from = QDateEdit(today.addDays(-7))
        self.del_to = QDateEdit(today)
        for d in (self.del_from, self.del_to):
            d.setCalendarPopup(True)
            d.setDisplayFormat("dd MMM yyyy")
        btn_range = QPushButton("Delete this date range")
        btn_range.clicked.connect(lambda: self.delete_range(self.del_from.date().toPython(),
                                                            self.del_to.date().toPython()))
        btn_all = QPushButton("Delete ALL history")
        btn_all.clicked.connect(self.delete_all)
        for w in (QLabel("From"), self.del_from, QLabel("to"), self.del_to, btn_range):
            row.addWidget(w)
        row.addStretch(1)
        row.addWidget(btn_all)
        p.body.addLayout(row)
        note = QLabel("Deleting cannot be undone (make a backup first). The session being tracked "
                      "right now is not deleted.")
        note.setObjectName("sub")
        p.body.addWidget(note)
        return p

    def _backup_panel(self) -> Panel:
        p = Panel("Backup, restore and data folder")
        row = QHBoxLayout()
        b1 = QPushButton("Back up database now...")
        b1.clicked.connect(self._choose_backup)
        b2 = QPushButton("Restore from backup...")
        b2.clicked.connect(self._choose_restore)
        b3 = QPushButton("Open data folder")
        b3.clicked.connect(lambda: self.open_folder())
        for b in (b1, b2, b3):
            row.addWidget(b)
        row.addStretch(1)
        p.body.addLayout(row)
        self.db_path_label = QLabel("")
        self.db_path_label.setObjectName("sub")
        self.db_path_label.setWordWrap(True)
        self.db_path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        p.body.addWidget(self.db_path_label)
        return p

    def _privacy_panel(self) -> Panel:
        p = Panel("Privacy")
        t = QLabel("TimeLens works completely offline: no internet, no cloud, no screenshots, no "
                   "keystroke logging, no reading of document contents, and no full file paths - "
                   "only application names, window-title-derived document names and times, stored "
                   "in a local database on this computer. Browser websites are recorded only if you "
                   "turn on the optional Websites setting above.")
        t.setObjectName("sub")
        t.setWordWrap(True)
        p.body.addWidget(t)
        return p

    # --- loading ------------------------------------------------------------------------
    _loaded = False

    def load(self) -> None:
        """Show the current values (does not trigger saves)."""
        self._loaded = False
        db = self.db
        auto = db.get_bool("start_with_windows")
        self.rb_auto.setChecked(auto)
        self.rb_manual.setChecked(not auto)
        for key, box in self.flags.items():
            box.setChecked(db.get_bool(key))
        self.flags["minimize_to_tray"].setEnabled(db.get_bool("system_tray"))
        self.idle_box.setValue(max(1, int(round(db.get_float("idle_threshold_seconds") / 60))))
        self.db_path_label.setText(f"Database: {db.path}")
        self.load_exclusions()
        self.load_web()
        self._loaded = True

    def refresh(self) -> None:
        self.load()

    def load_web(self) -> None:
        self.web_on.setChecked(self.db.get_bool("track_websites"))
        browsers = set(self.db.get_setting("web_browsers").split(","))
        self.web_chrome.setChecked("chrome" in browsers)
        self.web_edge.setChecked("edge" in browsers)
        self.web_excluded.setPlainText("\n".join(sorted(web_history.parse_excluded(
            self.db.get_setting("web_excluded_domains")))))
        self.load_web_profiles()

    def load_web_profiles(self) -> None:
        browsers = [b for b in self.db.get_setting("web_browsers").split(",") if b]
        sources = web_history.history_sources(browsers)
        names = web_history.profile_names(browsers)
        selected = {p for p in self.db.get_setting("web_profiles").split(",") if p}
        self.web_profiles.blockSignals(True)
        self.web_profiles.clear()
        for key in sources:
            browser, _, folder = key.partition(":")
            label = f"{web_history.BROWSERS[browser][0]} - {names.get(key) or folder}  ({folder})"
            item = QListWidgetItem(label)
            item.setData(_Qt.UserRole, key)
            item.setFlags(item.flags() | _Qt.ItemIsUserCheckable)
            item.setCheckState(_Qt.Checked if web_history.profile_allowed(key, selected) else _Qt.Unchecked)
            self.web_profiles.addItem(item)
        self.web_profiles.blockSignals(False)

    def save_web_profiles(self) -> None:
        keys = [self.web_profiles.item(i).data(_Qt.UserRole) for i in range(self.web_profiles.count())
                if self.web_profiles.item(i).checkState() == _Qt.Checked]
        self.db.set_setting("web_profiles", ",".join(keys))
        self._say(f"Browser profiles to read: {len(keys)}.")

    def load_exclusions(self) -> None:
        rows = self.db.query(
            "SELECT display_name, executable_name, excluded FROM applications "
            "WHERE executable_name <> 'LockApp.exe' ORDER BY excluded DESC, display_name COLLATE NOCASE")
        t = self.excl_table
        t.setRowCount(len(rows))
        for r, a in enumerate(rows):
            t.setItem(r, 0, QTableWidgetItem(a["display_name"]))
            t.setItem(r, 1, QTableWidgetItem(a["executable_name"]))
            box = QCheckBox()
            box.setChecked(bool(a["excluded"]))
            box.toggled.connect(lambda on, exe=a["executable_name"]: self.set_exclusion(exe, on))
            holder = QWidget()
            lay = QHBoxLayout(holder)
            lay.setContentsMargins(8, 0, 0, 0)
            lay.addWidget(box)
            t.setCellWidget(r, 2, holder)
        t.horizontalHeader().setSectionResizeMode(1, t.horizontalHeader().ResizeMode.ResizeToContents)

    # --- actions (each is also callable from tests) -------------------------------------
    def _say(self, text: str) -> None:
        self.status.setText(text)

    def set_startup(self, enabled: bool) -> None:
        try:
            self.startup.set_enabled(enabled)
            self.db.set_setting("start_with_windows", enabled)
            self._say("TimeLens will start automatically with Windows." if enabled
                      else "Automatic startup turned off: TimeLens now starts only when you open it.")
        except Exception as e:
            log.exception("Could not change startup setting")
            self._say(f"Could not change the startup setting: {e}")
            self.load()
            return
        self._on_changed()

    def set_flag(self, key: str, value: bool) -> None:
        self.db.set_setting(key, value)
        if key == "system_tray":
            self.flags["minimize_to_tray"].setEnabled(value)
        self._say(f"Saved: {self.flags[key].text()} = {'on' if value else 'off'}.")
        self._on_changed()

    def set_idle_minutes(self, minutes: int) -> None:
        self.db.set_setting("idle_threshold_seconds", int(minutes) * 60)
        self.engine.apply_settings()
        self._say(f"Saved: idle after {minutes} minute(s) without input.")
        self._on_changed()

    def set_web_tracking(self, on: bool) -> None:
        if on:
            days = self.ask_web_consent()
            if days is None:                              # the user said no: leave it off
                self._loaded = False
                self.web_on.setChecked(False)
                self._loaded = True
                self._say("Website tracking stays off.")
                return
            web_history.enable_tracking(self.db, days)
            self._say("Website tracking is on" + (f" (including the past {days} days)." if days else
                                                  " (from now on). Open the Websites page to see it."))
        else:
            web_history.disable_tracking(self.db)
            self._say("Website tracking is off. Data already saved stays until you delete it.")
        self._on_web_changed()

    def save_web_browsers(self) -> None:
        keys = [k for k, b in (("chrome", self.web_chrome), ("edge", self.web_edge)) if b.isChecked()]
        self.db.set_setting("web_browsers", ",".join(keys))
        self._say("Browsers to read: " + (", ".join(keys) or "none") + ".")

    def save_web_excluded(self, text: str) -> None:
        domains = sorted(web_history.parse_excluded(text))
        self.db.set_setting("web_excluded_domains", "\n".join(domains))
        removed = 0
        if domains:        # also forget what was already saved for those sites
            with self.db.transaction() as c:
                for d in domains:
                    removed += c.execute("DELETE FROM web_visits WHERE domain = ? OR domain LIKE ?",
                                         (d, "%." + d)).rowcount
        self._say(f"Excluded sites saved ({len(domains)}). Removed {removed} stored visit(s) to them.")
        self._on_data_changed()

    def delete_web_history(self) -> int:
        if not self.confirm("Delete website history", "Permanently delete ALL website history stored by "
                                                       "TimeLens? (Your browser's own history is not touched.)"):
            return 0
        n = self.db.delete_web_history()
        self._say(f"Deleted {n} stored website visit(s).")
        self._on_data_changed()
        return n

    def set_exclusion(self, exe: str, excluded: bool) -> None:
        self.db.set_excluded(exe, excluded)
        self._say(f"{exe} is now {'excluded from' if excluded else 'included in'} tracking details "
                  "(applies to future activity).")

    def add_exclusion(self, text: str) -> None:
        exe = text.strip()
        if not exe or "\\" in exe or "/" in exe:
            self._say("Enter just the executable file name, for example chrome.exe.")
            return
        if not exe.lower().endswith(".exe"):
            exe += ".exe"
        self.set_exclusion(exe, True)
        self.excl_name.clear()
        self.load_exclusions()

    def _selected_exe(self) -> Optional[str]:
        rows = self.excl_table.selectionModel().selectedRows()
        return self.excl_table.item(rows[0].row(), 1).text() if rows else None

    def _delete_selected_app(self) -> None:
        exe = self._selected_exe()
        if exe is None:
            self._say("Select an application in the table first.")
            return
        self.delete_app_history(exe)

    def delete_app_history(self, exe: str) -> int:
        if not self.confirm("Delete history", f"Permanently delete all recorded activity for {exe}?"):
            return 0
        n = self.db.delete_application_history(exe)
        self._say(f"Deleted {n} recorded session(s) of {exe}.")
        self._on_data_changed()
        return n

    def delete_range(self, first: date, last: date) -> int:
        first, last = min(first, last), max(first, last)
        if not self.confirm("Delete history", f"Permanently delete all activity from {first:%d %b %Y} "
                                              f"to {last:%d %b %Y}?"):
            return 0
        start, end = range_for_days(first, last)
        n = self.db.delete_sessions_between(start, end)
        self._say(f"Deleted {n} recorded session(s) between {first:%d %b %Y} and {last:%d %b %Y}.")
        self._on_data_changed()
        return n

    def delete_all(self) -> int:
        if not self.confirm("Delete ALL history",
                            "Permanently delete ALL recorded activity? This cannot be undone."):
            return 0
        n = self.db.delete_sessions_between(datetime(2000, 1, 1).astimezone(),
                                            datetime.now().astimezone() + timedelta(days=1))
        self._say(f"Deleted {n} recorded session(s). All history is cleared.")
        self._on_data_changed()
        return n

    def _choose_backup(self) -> None:
        default = config.BACKUP_DIR / f"timelens-{datetime.now():%Y%m%d-%H%M%S}.db"
        path = self.pick_save("Save database backup", str(default), "TimeLens database (*.db)")
        if path:
            self.backup_to(Path(path))

    def backup_to(self, dest: Path) -> Optional[Path]:
        try:
            out = backup_manager.create_backup(self.db.path, dest)
        except Exception as e:
            log.exception("Backup failed")
            self._say(f"Backup failed: {e}")
            return None
        self._say(f"Backup saved: {out}")
        return out

    def _choose_restore(self) -> None:
        path = self.pick_open("Choose a TimeLens backup", "TimeLens database (*.db)")
        if path:
            self.restore_from(Path(path))

    def restore_from(self, path: Path) -> bool:
        try:
            backup_manager.validate_backup(path)
        except backup_manager.BackupError as e:
            self._say(f"Cannot restore: {e}")
            return False
        if not self.confirm("Restore backup",
                            "Replace ALL current data with this backup? Your current database is "
                            "kept as a safety copy next to it."):
            return False
        if self._on_restore is None:
            self._say("Restore is not available here.")
            return False
        self._say(self._on_restore(path))
        self.load()
        return True
