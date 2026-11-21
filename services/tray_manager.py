"""System tray icon and menu.

Menu (as specified): Open Dashboard, Pause Monitoring, Resume Monitoring, Settings,
Open Data Folder, Exit TimeLens. Pause/Resume are enabled according to the current state.
"""
from __future__ import annotations

import logging
import os
from typing import Callable

from PySide6.QtCore import QObject
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

import config
from tracker.engine import TrackingEngine
from ui.icons import app_icon

log = logging.getLogger(__name__)


def open_data_folder() -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    os.startfile(str(config.DATA_DIR))


class TrayManager(QObject):
    def __init__(self, engine: TrackingEngine, open_dashboard: Callable, open_settings: Callable,
                 exit_app: Callable, open_folder: Callable = open_data_folder):
        super().__init__()
        self.engine = engine
        self.available = QSystemTrayIcon.isSystemTrayAvailable()
        self.tray = QSystemTrayIcon(app_icon())
        self.tray.setToolTip("TimeLens")
        self.menu = QMenu()
        self.act_open = self._add("Open Dashboard", open_dashboard)
        self.menu.addSeparator()
        self.act_pause = self._add("Pause Monitoring", self._pause)
        self.act_resume = self._add("Resume Monitoring", self._resume)
        self.menu.addSeparator()
        self.act_settings = self._add("Settings", open_settings)
        self.act_folder = self._add("Open Data Folder", open_folder)
        self.menu.addSeparator()
        self.act_exit = self._add("Exit TimeLens", exit_app)
        self.tray.setContextMenu(self.menu)
        self.menu.aboutToShow.connect(self.refresh_state)
        # left-click / double-click opens the dashboard
        self.tray.activated.connect(
            lambda reason: open_dashboard() if reason in (QSystemTrayIcon.Trigger,
                                                          QSystemTrayIcon.DoubleClick) else None)
        self.refresh_state()

    def _add(self, text: str, slot: Callable) -> QAction:
        act = QAction(text, self.menu)
        act.triggered.connect(lambda _checked=False: slot())
        self.menu.addAction(act)
        return act

    def _pause(self) -> None:
        self.engine.pause()
        self.refresh_state()

    def _resume(self) -> None:
        self.engine.resume()
        self.refresh_state()

    def refresh_state(self) -> None:
        running = self.engine.running
        paused = self.engine.paused
        self.act_pause.setEnabled(running and not paused)
        self.act_resume.setEnabled(running and paused)
        self.tray.setIcon(app_icon(paused=paused))
        self.tray.setToolTip("TimeLens - paused" if paused else "TimeLens - monitoring")

    def set_visible(self, visible: bool) -> None:
        if visible and self.available:
            self.tray.show()
        else:
            self.tray.hide()

    @property
    def visible(self) -> bool:
        return self.available and self.tray.isVisible()
