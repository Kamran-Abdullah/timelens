"""Main window: sidebar navigation + stacked pages, tray integration and exit handling."""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (QApplication, QButtonGroup, QHBoxLayout, QLabel, QMainWindow,
                               QPushButton, QStackedWidget, QVBoxLayout, QWidget)

import config
from database.db_manager import Database
from services import backup_manager
from services.startup_manager import StartupManager
from services.tray_manager import TrayManager
from tracker.engine import TrackingEngine
from ui import theme
from ui.app_analytics import ApplicationAnalyticsPage
from ui.assistant_page import AssistantPage
from ui.dashboard import DashboardPage
from ui.document_analytics import DocumentAnalyticsPage
from ui.icons import app_icon, make_pixmap
from ui.nav_icons import ensure_icons, nav_icon
from ui.live_monitor import LiveMonitorPage
from ui.productivity import ProductivityPage
from ui.reports import ReportsPage
from ui.settings import SettingsPage
from ui.timeline import TimelinePage
from ui.websites_page import WebsitesPage
from ui.workers import run_async, wait_for_jobs

log = logging.getLogger(__name__)

PAGE_REFRESH_MS = 10_000
SETTINGS_PAGE = 9
WEB_IMPORT_MS = 5 * 60 * 1000


class MainWindow(QMainWindow):
    def __init__(self, db: Database, engine: TrackingEngine, day_provider=None,
                 startup: Optional[StartupManager] = None, with_tray: bool = False):
        super().__init__()
        self.db, self.engine = db, engine
        self.startup = startup or StartupManager()
        self._quitting = False
        self._paused_by_hide = False
        self.setWindowTitle(config.APP_NAME)
        self.setWindowIcon(app_icon())
        self.resize(1180, 780)
        self.setMinimumSize(900, 620)

        central = QWidget()
        self.setCentralWidget(central)
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # --- sidebar ---
        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(220)
        sb = QVBoxLayout(sidebar)
        sb.setContentsMargins(0, 0, 0, 14)
        sb.setSpacing(0)
        brand_box = QWidget()
        brand_row = QHBoxLayout(brand_box)
        brand_row.setContentsMargins(18, 20, 14, 18)
        brand_row.setSpacing(12)
        logo = QLabel()
        logo.setPixmap(make_pixmap(88).scaled(40, 40, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        names = QVBoxLayout()
        names.setSpacing(0)
        brand = QLabel("TimeLens")
        brand.setObjectName("brandName")
        tag = QLabel("Activity intelligence")
        tag.setObjectName("brandTag")
        names.addWidget(brand)
        names.addWidget(tag)
        brand_row.addWidget(logo)
        brand_row.addLayout(names)
        brand_row.addStretch(1)
        sb.addWidget(brand_box)

        self.stack = QStackedWidget()
        self.dashboard = (DashboardPage(db, self.theme_name, day_provider) if day_provider
                          else DashboardPage(db, self.theme_name))
        self.live = LiveMonitorPage(db, engine)
        day = day_provider or date.today
        self.apps = ApplicationAnalyticsPage(db, day)
        self.docs = DocumentAnalyticsPage(db, day)
        self.timeline = TimelinePage(db, day)
        self.productivity = ProductivityPage(db, self.theme_name, day)
        self.reports = ReportsPage(db, day)
        self.assistant = AssistantPage(db)
        self.websites = WebsitesPage(db, day)
        self.settings = SettingsPage(db, engine, self.startup, on_changed=self.apply_settings,
                                     on_restore=self.restore_database,
                                     on_data_changed=self.refresh_all,
                                     on_web_changed=self.import_websites)
        pages = [
            ("Dashboard", self.dashboard),
            ("Live Monitor", self.live),
            ("Application Analytics", self.apps),
            ("Document Analytics", self.docs),
            ("Activity Timeline", self.timeline),
            ("Productivity Analytics", self.productivity),
            ("Reports", self.reports),
            ("Websites", self.websites),
            ("Assistant", self.assistant),
            ("Settings", self.settings),
        ]
        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        self.nav_buttons: dict[str, QPushButton] = {}
        for i, (name, page) in enumerate(pages):
            self.stack.addWidget(page)
            btn = QPushButton(name)
            btn.setObjectName("nav")
            btn.setIcon(nav_icon(name))
            btn.setIconSize(QSize(20, 20))
            btn.setCursor(Qt.PointingHandCursor)
            btn.setCheckable(True)
            btn.clicked.connect(lambda _=False, idx=i: self.show_page(idx))
            self.nav_group.addButton(btn)
            self.nav_buttons[name] = btn
            sb.addWidget(btn)
        sb.addStretch(1)
        self.theme_btn = QPushButton()
        self.theme_btn.setObjectName("nav")
        self.theme_btn.setIconSize(QSize(20, 20))
        self.theme_btn.setCursor(Qt.PointingHandCursor)
        self.theme_btn.clicked.connect(self.toggle_theme)
        sb.addWidget(self.theme_btn)

        layout.addWidget(sidebar)
        layout.addWidget(self.stack, 1)

        self.live.open_timeline_requested.connect(lambda: self.show_page(4))

        self.tray: Optional[TrayManager] = None
        if with_tray:
            self.tray = TrayManager(engine, self.show_dashboard, self.open_settings, self.exit_app)

        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self._refresh_current)
        self.refresh_timer.start(PAGE_REFRESH_MS)

        self.web_timer = QTimer(self)
        self.web_timer.timeout.connect(self.import_websites)
        self.web_timer.start(WEB_IMPORT_MS)
        self.apply_theme()
        self.apply_settings()
        self.show_page(0)

    # --- navigation -------------------------------------------------------------
    def show_page(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        self.nav_buttons[list(self.nav_buttons)[index]].setChecked(True)
        page = self.stack.currentWidget()
        if hasattr(page, "refresh"):
            page.refresh()

    def _refresh_current(self) -> None:
        """Timer refresh. Analytics pages opt out (auto_refresh = False) so a table the
        user is reading or sorting is not reshuffled underneath them."""
        page = self.stack.currentWidget()
        if hasattr(page, "refresh") and getattr(page, "auto_refresh", True):
            page.refresh()

    def import_websites(self) -> None:
        """Read new browser history in the background - only if the user turned website tracking on."""
        if not self.db.get_bool("track_websites"):
            return
        from services import web_history
        db = self.db
        run_async(lambda: web_history.import_history(db), lambda _result: None)

    def refresh_all(self) -> None:
        """Data changed underneath the pages (deleted history, restored backup)."""
        self.show_page(self.stack.currentIndex())

    # --- theme ------------------------------------------------------------------
    def theme_name(self) -> str:
        return self.db.get_setting("theme")

    def apply_theme(self) -> None:
        name = self.theme_name()
        QApplication.instance().setStyleSheet(theme.stylesheet(name, ensure_icons()))
        self.theme_btn.setText("Switch to dark theme" if name == "light" else "Switch to light theme")
        self.theme_btn.setIcon(nav_icon("moon" if name == "light" else "sun"))
        self.dashboard.restyle()
        self.productivity.restyle()

    def toggle_theme(self) -> None:
        self.db.set_setting("theme", "dark" if self.theme_name() == "light" else "light")
        self.apply_theme()

    # --- settings that affect the running app -------------------------------------------
    def apply_settings(self) -> None:
        self.engine.apply_settings()
        if self.tray:
            self.tray.set_visible(self.db.get_bool("system_tray"))
            self.tray.refresh_state()

    def will_hide_to_tray(self) -> bool:
        """Closing the window hides it (instead of exiting) only if a tray icon is there to
        bring it back."""
        return bool(self.tray and self.tray.visible and self.db.get_bool("system_tray")
                    and self.db.get_bool("minimize_to_tray"))

    # --- tray actions -----------------------------------------------------------------
    def show_dashboard(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()
        if self._paused_by_hide:                   # we paused only because the window was hidden
            self._paused_by_hide = False
            self.engine.resume()
        self._refresh_current()

    def open_settings(self) -> None:
        self.show_dashboard()
        self.show_page(SETTINGS_PAGE)

    def exit_app(self) -> None:
        """Exit TimeLens completely: stop monitoring, then quit."""
        self._quitting = True
        self.close()

    # --- closing ----------------------------------------------------------------
    def closeEvent(self, event: QCloseEvent) -> None:
        if not self._quitting and self.will_hide_to_tray():
            event.ignore()
            self.hide()
            if not self.db.get_bool("background_monitoring") and not self.engine.paused:
                self.engine.pause()
                self._paused_by_hide = True
            if self.tray:
                self.tray.refresh_state()
            return
        self._shutdown()
        event.accept()

    def _shutdown(self) -> None:
        self.refresh_timer.stop()
        self.web_timer.stop()
        self.live.tick.stop()
        self.engine.stop()          # after this returns nothing is monitored
        wait_for_jobs()             # let report/dashboard queries finish before the database closes
        if self.tray:
            self.tray.set_visible(False)
        app = QApplication.instance()
        if app:
            app.quit()

    # --- restore a backup ---------------------------------------------------------------
    def restore_database(self, path: Path) -> str:
        """Swap the live database for a backup, then continue tracking on it."""
        try:
            backup_manager.validate_backup(path)
            was_running = self.engine.running
            self.engine.stop()
            wait_for_jobs()
            self.db.close()
            safety = backup_manager.restore_backup(path, self.db.path)
            self.db.open()
            if was_running:
                self.engine.restart()
            self.apply_theme()
            self.apply_settings()
            self.refresh_all()
            return f"Restored from {path.name}. Your previous data was kept as {safety.name}."
        except Exception as e:
            log.exception("Restore failed")
            try:
                if self.db._writer is None:
                    self.db.open()            # make sure we are never left without a database
                if not self.engine.running:
                    self.engine.restart()
            except Exception:
                log.exception("Could not reopen the database after a failed restore")
            return f"Restore failed: {e}"
