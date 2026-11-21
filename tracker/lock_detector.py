"""Detect whether the Windows session is locked.

Phase 1 uses polling, which is simple and needs no message loop:
  * When the workstation is locked, Windows switches to the secure "Winlogon" desktop.
    A normal user process is not allowed to open that desktop, so OpenInputDesktop()
    fails (returns NULL). That is the primary signal.
  * Secondary signal: the foreground process is LockApp.exe (the Win10/11 lock screen).

Known limitation: polling notices a lock up to one sample (~2 s) late. Phase 8 adds
event-based WTS session notifications (lock/unlock/logoff) for exact timestamps.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
user32.OpenInputDesktop.restype = wintypes.HANDLE
user32.CloseDesktop.argtypes = [wintypes.HANDLE]
user32.CloseDesktop.restype = wintypes.BOOL

_DESKTOP_SWITCHDESKTOP = 0x0100


def is_session_locked(foreground_exe: str = "") -> bool:
    if foreground_exe.lower() == "lockapp.exe":
        return True
    desktop = user32.OpenInputDesktop(0, False, _DESKTOP_SWITCHDESKTOP)
    if not desktop:
        return True  # cannot open the input desktop -> secure desktop is showing
    user32.CloseDesktop(desktop)
    return False
