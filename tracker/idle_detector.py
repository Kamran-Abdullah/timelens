"""Idle detection from the time of the last keyboard/mouse input.

Windows keeps one timestamp of the last input event (GetLastInputInfo). We never see
*what* was typed - only *when* the last input happened.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


user32.GetLastInputInfo.argtypes = [ctypes.POINTER(_LASTINPUTINFO)]
user32.GetLastInputInfo.restype = wintypes.BOOL
kernel32.GetTickCount.restype = wintypes.DWORD


def get_idle_seconds() -> float:
    """Seconds since the last keyboard or mouse input in this Windows session.

    Both values are 32-bit millisecond tick counts that wrap every ~49.7 days, so the
    subtraction is masked to 32 bits to stay correct across a wrap.
    """
    info = _LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(_LASTINPUTINFO)
    if not user32.GetLastInputInfo(ctypes.byref(info)):
        raise ctypes.WinError(ctypes.get_last_error())
    elapsed_ms = (kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF
    return elapsed_ms / 1000.0


def is_idle(idle_seconds: float, threshold_seconds: float) -> bool:
    """Idle begins once inactivity reaches the configured threshold."""
    return idle_seconds >= threshold_seconds
