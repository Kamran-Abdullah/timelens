"""Smoke tests that call the real Windows APIs (the machine must be a Windows desktop)."""
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")

from tracker.idle_detector import get_idle_seconds, is_idle
from tracker.lock_detector import is_session_locked
from tracker.window_detector import friendly_name, get_foreground_window


def test_idle_seconds_is_sane():
    assert 0 <= get_idle_seconds() < 60 * 60 * 24 * 60


def test_is_idle_threshold_boundary():
    assert not is_idle(299.9, 300)
    assert is_idle(300, 300)


def test_foreground_window_has_exe_and_no_path():
    w = get_foreground_window()
    if w is None:
        pytest.skip("no foreground window right now")
    assert w.pid > 0
    assert "\\" not in w.executable_name and "/" not in w.executable_name


def test_lock_detector_runs():
    assert isinstance(is_session_locked(), bool)


def test_friendly_name():
    assert friendly_name("EXCEL.EXE") == "Microsoft Excel"
    assert friendly_name("my_tool.exe") == "My Tool"
    assert friendly_name("") == "Unknown Application"
