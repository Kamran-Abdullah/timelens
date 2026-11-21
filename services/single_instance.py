"""Prevent two TimeLens tracking processes from running at once.

Uses a named Windows mutex. The mutex belongs to the process, so Windows releases it
automatically if TimeLens crashes - there is no stale lock file to clean up.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

_ERROR_ALREADY_EXISTS = 183

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.CreateMutexW.restype = wintypes.HANDLE
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


class SingleInstance:
    def __init__(self, name: str = "Local\\TimeLens.SingleInstance"):
        self.name = name
        self._handle = None

    def acquire(self) -> bool:
        """True if we are the only instance; False if another one already holds the lock."""
        handle = _kernel32.CreateMutexW(None, False, self.name)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
            _kernel32.CloseHandle(handle)
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle:
            _kernel32.CloseHandle(self._handle)
            self._handle = None
