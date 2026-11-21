"""Exact Windows lock / unlock / sleep / wake / shutdown notifications.

A hidden window (never shown) receives these Windows messages, so TimeLens learns the
exact moment of each event instead of guessing from 2-second polling:

  WM_WTSSESSION_CHANGE  session locked / unlocked        (needs WTSRegisterSessionNotification)
  WM_POWERBROADCAST     going to sleep / hibernate / resumed
  WM_ENDSESSION         Windows is shutting down / restarting / signing out

The window runs its own message loop in a background thread. Events are delivered to the
callback as SystemEvent(kind, utc_time) from that thread.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Optional

import win32api
import win32con
import win32gui
import win32ts

log = logging.getLogger(__name__)

WM_WTSSESSION_CHANGE = 0x02B1
WTS_SESSION_LOCK, WTS_SESSION_UNLOCK = 0x7, 0x8
WM_POWERBROADCAST = 0x0218
PBT_APMSUSPEND = 0x4
PBT_APMRESUMESUSPEND = 0x7          # resumed by the user
PBT_APMRESUMEAUTOMATIC = 0x12       # resumed automatically (always sent on resume)
ENDSESSION_LOGOFF = 0x80000000


class SystemEventKind(str, Enum):
    LOCK = "LOCK"
    UNLOCK = "UNLOCK"
    SUSPEND = "SUSPEND"             # sleep or hibernate begins
    RESUME = "RESUME"
    SHUTDOWN = "SHUTDOWN"           # Windows shutting down or restarting (computer goes off)
    LOGOFF = "LOGOFF"               # user signing out (computer stays on)


@dataclass(frozen=True)
class SystemEvent:
    kind: SystemEventKind
    when: datetime


def _now() -> datetime:
    return datetime.now(timezone.utc)


class PowerMonitor:
    def __init__(self, callback: Callable[[SystemEvent], None]):
        self._callback = callback
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._hwnd = None
        self._class_name = f"TimeLensPowerMonitor{id(self)}"
        self._resume_seen = False

    @property
    def hwnd(self):
        return self._hwnd

    def start(self, timeout: float = 5.0) -> bool:
        self._thread = threading.Thread(target=self._run, name="TimeLensPower", daemon=True)
        self._thread.start()
        return self._ready.wait(timeout) and self._hwnd is not None

    def stop(self) -> None:
        if self._hwnd:
            try:
                win32gui.PostMessage(self._hwnd, win32con.WM_CLOSE, 0, 0)
            except Exception:
                log.debug("PowerMonitor window already closed", exc_info=True)
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=3)  # a handler on our own thread cannot join itself

    # --- thread -----------------------------------------------------------------
    def _run(self) -> None:
        try:
            wc = win32gui.WNDCLASS()
            wc.hInstance = win32api.GetModuleHandle(None)
            wc.lpszClassName = self._class_name
            wc.lpfnWndProc = self._wnd_proc
            win32gui.RegisterClass(wc)
            # A normal (hidden, never shown) top-level window: message-only windows do not
            # receive the shutdown and power broadcasts.
            self._hwnd = win32gui.CreateWindow(self._class_name, "TimeLens power monitor", 0, 0, 0,
                                               0, 0, 0, 0, wc.hInstance, None)
            try:
                win32ts.WTSRegisterSessionNotification(self._hwnd, win32ts.NOTIFY_FOR_THIS_SESSION)
            except Exception:
                log.exception("Could not register for lock/unlock notifications (polling still works)")
        except Exception:
            log.exception("PowerMonitor failed to start")
            self._ready.set()
            return
        self._ready.set()
        win32gui.PumpMessages()
        try:
            win32gui.UnregisterClass(self._class_name, win32api.GetModuleHandle(None))
        except Exception:
            pass

    def _emit(self, kind: SystemEventKind) -> None:
        try:
            self._callback(SystemEvent(kind, _now()))
        except Exception:
            log.exception("System event handler failed (%s)", kind)

    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        if msg == WM_WTSSESSION_CHANGE:
            if wparam == WTS_SESSION_LOCK:
                self._emit(SystemEventKind.LOCK)
            elif wparam == WTS_SESSION_UNLOCK:
                self._emit(SystemEventKind.UNLOCK)
            return 0
        if msg == WM_POWERBROADCAST:
            if wparam == PBT_APMSUSPEND:
                self._resume_seen = False
                self._emit(SystemEventKind.SUSPEND)
            elif wparam in (PBT_APMRESUMEAUTOMATIC, PBT_APMRESUMESUSPEND):
                if not self._resume_seen:          # Windows can send both; report one RESUME
                    self._resume_seen = True
                    self._emit(SystemEventKind.RESUME)
            return 1
        if msg == win32con.WM_QUERYENDSESSION:
            return 1                                # never block a shutdown
        if msg == win32con.WM_ENDSESSION:
            if wparam:                              # session really is ending
                self._emit(SystemEventKind.LOGOFF if lparam & ENDSESSION_LOGOFF
                           else SystemEventKind.SHUTDOWN)
            return 0
        if msg == win32con.WM_CLOSE:
            try:
                win32ts.WTSUnRegisterSessionNotification(hwnd)
            except Exception:
                pass
            win32gui.DestroyWindow(hwnd)
            return 0
        if msg == win32con.WM_DESTROY:
            win32gui.PostQuitMessage(0)
            return 0
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)
