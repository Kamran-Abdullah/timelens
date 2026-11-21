"""Foreground window detection using the Win32 API (via ctypes).

Privacy: only the executable *file name* (e.g. EXCEL.EXE) and the window title are kept.
The full path returned by Windows is used transiently and discarded.
"""
from __future__ import annotations

import ctypes
import logging
import ntpath
import os
from ctypes import wintypes
from dataclasses import dataclass
from typing import Optional

import config

log = logging.getLogger(__name__)

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD

_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumChildWindows.argtypes = [wintypes.HWND, _WNDENUMPROC, wintypes.LPARAM]
user32.EnumChildWindows.restype = wintypes.BOOL

kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL

# Lowest-privilege access right; works for most processes, even elevated ones.
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# Friendly names for common executables. Unknown executables fall back to the file name.
FRIENDLY_APP_NAMES = {
    "excel.exe": "Microsoft Excel",
    "winword.exe": "Microsoft Word",
    "powerpnt.exe": "Microsoft PowerPoint",
    "outlook.exe": "Microsoft Outlook",
    "onenote.exe": "Microsoft OneNote",
    "ms-teams.exe": "Microsoft Teams",
    "teams.exe": "Microsoft Teams",
    "code.exe": "Visual Studio Code",
    "notepad.exe": "Notepad",
    "notepad++.exe": "Notepad++",
    "chrome.exe": "Google Chrome",
    "msedge.exe": "Microsoft Edge",
    "firefox.exe": "Mozilla Firefox",
    "acrord32.exe": "Adobe Acrobat Reader",
    "acrobat.exe": "Adobe Acrobat",
    "sumatrapdf.exe": "SumatraPDF",
    "foxitreader.exe": "Foxit Reader",
    "explorer.exe": "File Explorer",
    "cmd.exe": "Command Prompt",
    "powershell.exe": "Windows PowerShell",
    "windowsterminal.exe": "Windows Terminal",
    "calculatorapp.exe": "Calculator",
    "lockapp.exe": "Windows Lock Screen",
}

_UWP_HOST = "applicationframehost.exe"


@dataclass(frozen=True)
class WindowInfo:
    """What we know about the current foreground window."""
    hwnd: int
    pid: int
    title: str
    executable_name: str      # e.g. "EXCEL.EXE"; "" if it could not be determined
    application_name: str     # e.g. "Microsoft Excel"


def _window_title(hwnd) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _pid_of(hwnd) -> int:
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _exe_name_of(pid: int) -> str:
    """Return only the executable file name for a process id ('' if inaccessible)."""
    if pid <= 0:
        return ""
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return ntpath.basename(buf.value)  # drop the folder path immediately
        return ""
    finally:
        kernel32.CloseHandle(handle)


def _resolve_uwp_pid(frame_hwnd, host_pid: int) -> int:
    """Modern (UWP) apps are hosted inside ApplicationFrameHost.exe.

    The real app owns a child window belonging to a different process, so we look
    for that child to report the real application instead of the host.
    """
    found = []

    def _cb(child, _lparam):
        child_pid = _pid_of(child)
        if child_pid and child_pid != host_pid:
            found.append(child_pid)
            return False  # stop enumeration
        return True

    user32.EnumChildWindows(frame_hwnd, _WNDENUMPROC(_cb), 0)
    return found[0] if found else host_pid


def friendly_name(executable_name: str) -> str:
    if not executable_name:
        return "Unknown Application"
    known = FRIENDLY_APP_NAMES.get(executable_name.lower())
    if known:
        return known
    stem = ntpath.splitext(executable_name)[0]
    return stem.replace("_", " ").strip().title() or executable_name


def get_foreground_window() -> Optional[WindowInfo]:
    """Return the window the user is currently working in, or None if there is none
    (for example while the desktop is switching between windows)."""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    try:
        pid = _pid_of(hwnd)
        if pid == os.getpid():
            # It is TimeLens's OWN window. Reading its title from this (tracker) thread would make
            # Windows ask the GUI thread to answer and WAIT for it - and if the GUI thread is busy
            # waiting for something this thread holds, both freeze forever (a real hang we hit).
            # We already know our own title, so we never ask.
            return WindowInfo(int(hwnd), pid, config.APP_NAME, _exe_name_of(pid), config.APP_NAME)
        title = _window_title(hwnd)
        exe = _exe_name_of(pid)
        if exe.lower() == _UWP_HOST:
            real_pid = _resolve_uwp_pid(hwnd, pid)
            real_exe = _exe_name_of(real_pid)
            if real_exe:
                exe = real_exe
        return WindowInfo(int(hwnd), pid, title, exe, friendly_name(exe))
    except Exception:  # never let a single bad window kill the monitor
        log.exception("Failed to read foreground window")
        return None
