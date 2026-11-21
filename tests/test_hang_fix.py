"""Regression tests for the real freeze: TimeLens's tracker thread and GUI thread waiting on each other."""
import ctypes
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
import win32api
import win32con
import win32gui

from services.hang_watchdog import HangWatchdog
from tracker import window_detector as wd
from tracker.foreground_monitor import ForegroundMonitor
from tracker.session_manager import Snapshot
from tracker.window_detector import WindowInfo

T0 = datetime(2026, 11, 3, 9, 0, tzinfo=timezone.utc)
XL = WindowInfo(1, 1, "A.xlsx - Excel", "EXCEL.EXE", "Microsoft Excel")


# ---------------------------------------------- 1. never ask our own window for its title
@pytest.fixture
def own_window():
    """A real top-level window owned by THIS (the test's "GUI") thread."""
    wc = win32gui.WNDCLASS()
    wc.hInstance = win32api.GetModuleHandle(None)
    wc.lpszClassName = f"HangRepro{time.time_ns()}"
    wc.lpfnWndProc = {win32con.WM_DESTROY: lambda *a: 0}
    win32gui.RegisterClass(wc)
    hwnd = win32gui.CreateWindow(wc.lpszClassName, "secret title", win32con.WS_OVERLAPPEDWINDOW,
                                 0, 0, 200, 100, 0, 0, wc.hInstance, None)
    yield hwnd
    win32gui.DestroyWindow(hwnd)


def test_reading_another_threads_window_title_really_does_block(own_window):
    """Documents WHY the fix exists: the owner thread must pump messages before the call returns."""
    user32 = ctypes.WinDLL("user32")
    done = threading.Event()
    threading.Thread(target=lambda: (user32.GetWindowTextLengthW(own_window), done.set()), daemon=True).start()
    assert not done.wait(1.0)                       # blocked while the owner thread is not pumping
    win32gui.PumpWaitingMessages()                  # now let it answer
    assert done.wait(3.0)


def test_own_window_is_never_asked_for_its_title(own_window, monkeypatch):
    monkeypatch.setattr(wd.user32, "GetForegroundWindow", lambda: own_window)
    monkeypatch.setattr(wd, "_window_title", lambda h: pytest.fail("asked our own window for its title"))
    result = {}
    t = threading.Thread(target=lambda: result.update(w=wd.get_foreground_window()), daemon=True)
    t.start()
    time.sleep(1.5)                                  # the "GUI thread" is busy and NOT pumping messages
    assert not t.is_alive(), "tracker thread is stuck waiting for the GUI thread"
    w = result["w"]
    assert (w.pid, w.title, w.application_name) == (os.getpid(), "TimeLens", "TimeLens")
    assert w.executable_name and "\\" not in w.executable_name


def test_other_applications_are_still_read_normally(monkeypatch):
    monkeypatch.setattr(wd.user32, "GetForegroundWindow", lambda: 4242)
    monkeypatch.setattr(wd, "_pid_of", lambda h: 99999)
    monkeypatch.setattr(wd, "_window_title", lambda h: "Budget.xlsx - Excel")
    monkeypatch.setattr(wd, "_exe_name_of", lambda pid: "EXCEL.EXE")
    w = wd.get_foreground_window()
    assert (w.title, w.executable_name, w.application_name) == ("Budget.xlsx - Excel", "EXCEL.EXE", "Microsoft Excel")


# ------------------------------ 2. the GUI never waits for the tracker (no lock held while sampling)
def make_monitor(provider):
    return ForegroundMonitor(300, 2.0, provider, lambda: T0)


def test_gui_reads_and_controls_the_monitor_while_a_sample_is_stuck():
    gate = threading.Event()

    def stuck_provider():
        gate.wait(10)                                # a Windows call that cannot finish yet
        return Snapshot(T0, XL, T0, False)

    mon = make_monitor(stuck_provider)
    ticker = threading.Thread(target=mon.tick, daemon=True)
    ticker.start()
    time.sleep(0.3)                                  # the tracker is now inside its blocking call
    t0 = time.perf_counter()
    assert mon.current_segment() is None             # (Live Monitor does this every second)
    mon.pause()                                      # (tray / button)
    assert time.perf_counter() - t0 < 0.5, "GUI had to wait for the tracker thread"
    gate.set()
    ticker.join(3)
    assert not ticker.is_alive()
    assert mon.manager.current is None and mon.current_segment() is None   # paused meanwhile: sample dropped


def test_current_segment_is_published_after_every_change():
    mon = make_monitor(lambda: Snapshot(T0, XL, T0, False))
    assert mon.current_segment() is None
    mon.tick()
    seg = mon.current_segment()
    assert seg.document_name == "A.xlsx" and seg.application_name == "Microsoft Excel"
    seg.document_name = "tampered"                   # callers get a copy
    assert mon.manager.current.document_name == "A.xlsx"
    mon.pause()
    assert mon.current_segment() is None
    mon.resume()
    mon.tick()
    assert mon.current_segment() is not None
    mon.suspend(T0 + timedelta(seconds=5))
    assert mon.current_segment() is None
    mon.unsuspend()
    mon.tick()
    mon.stop()
    assert mon.current_segment() is None


def test_many_gui_polls_while_the_tracker_runs_never_block():
    mon = make_monitor(lambda: (time.sleep(0.01), Snapshot(T0, XL, T0, False))[1])
    stop = threading.Event()
    threading.Thread(target=lambda: [mon.tick() for _ in iter(stop.is_set, True)], daemon=True).start()
    worst = 0.0
    for _ in range(300):
        t = time.perf_counter()
        mon.current_segment()
        worst = max(worst, time.perf_counter() - t)
        time.sleep(0.002)
    stop.set()
    assert worst < 0.05


# ------------------------------------------------------------------ 3. the hang watchdog
def test_watchdog_writes_all_thread_stacks_when_the_gui_stops_beating(tmp_path):
    log = tmp_path / "hang.log"
    dog = HangWatchdog(log, threshold=0.3, check_every=0.05)
    dog.start()
    time.sleep(1.0)                                  # no beat() -> looks frozen
    dog.stop()
    text = log.read_text(encoding="utf-8")
    assert "interface stalled" in text and "Thread" in text and dog.reports == 1
    assert "test_watchdog_writes_all_thread_stacks" in text      # it shows where each thread is


def test_watchdog_stays_quiet_while_the_gui_is_alive_and_reports_recovery(tmp_path):
    log = tmp_path / "hang.log"
    dog = HangWatchdog(log, threshold=0.3, check_every=0.05)
    dog.start()
    end = time.time() + 0.8
    while time.time() < end:
        dog.beat()
        time.sleep(0.05)
    assert not log.exists() and dog.reports == 0
    time.sleep(0.6)                                  # freeze
    dog.beat()
    time.sleep(0.3)                                  # recovered
    dog.stop()
    text = log.read_text(encoding="utf-8")
    assert dog.reports == 1 and "responding again" in text


def test_watchdog_reports_a_long_freeze_only_once(tmp_path):
    dog = HangWatchdog(tmp_path / "hang.log", threshold=0.2, check_every=0.05)
    dog.start()
    time.sleep(1.0)
    dog.stop()
    assert dog.reports == 1
