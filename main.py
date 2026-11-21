"""TimeLens entry point.

    python main.py                                 # open TimeLens (window + tracking)

Command-line modes:

    python main.py --run                           # track silently, saving to the database
    python main.py --summary                       # print today's totals from the database

    python main.py --diagnose                      # run until Ctrl+C
    python main.py --diagnose --seconds 60         # run for 60 seconds
    python main.py --diagnose --idle-threshold 10  # quick idle test (10 s instead of 5 min)
    python main.py --diagnose --verbose            # also print every 2-second sample
"""
from __future__ import annotations

import argparse
import logging
import sys
import time

from datetime import date

import config
from analytics import time_calc
from database.db_manager import Database
from services.single_instance import SingleInstance
from services.startup_manager import StartupManager
from tracker.engine import TrackingEngine
from tracker.foreground_monitor import ForegroundMonitor
from tracker.session_manager import EventKind, SessionEvent


def _fmt(seconds: float) -> str:
    m, s = divmod(int(round(seconds)), 60)
    h, m = divmod(m, 60)
    return f"{h:d}:{m:02d}:{s:02d}"


def _describe(ev: SessionEvent) -> str:
    local = lambda d: d.astimezone().strftime("%H:%M:%S")
    if ev.kind is EventKind.GAP:
        return (f"GAP     {local(ev.gap_start)} -> {local(ev.gap_end)} "
                f"(not counted: sleep / TimeLens not sampling)")
    s = ev.segment
    what = f"{s.application_name}" + (f" | {s.document_name}" if s.document_name else "")
    if ev.kind is EventKind.OPENED:
        return f"START   {local(s.start)}  [{s.status.value}] {what}   (exe={s.executable_name or '-'})"
    return (f"END     {local(s.end)}  [{s.status.value}] {what}   "
            f"duration {_fmt(s.duration_seconds)}")


def run_diagnose(args: argparse.Namespace) -> int:
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(config.LOG_DIR / "diagnostic.log", encoding="utf-8")])

    monitor = ForegroundMonitor(idle_threshold_seconds=args.idle_threshold,
                                poll_interval=args.interval)

    def on_event(ev: SessionEvent) -> None:
        line = _describe(ev)
        print(line, flush=True)
        logging.getLogger("diagnose").info(line)

    monitor.add_listener(on_event)
    print(f"TimeLens diagnostics - idle threshold {args.idle_threshold:g}s, "
          f"sampling every {args.interval:g}s. Ctrl+C to stop.")
    print(f"Log file: {config.LOG_DIR / 'diagnostic.log'}")
    monitor.start()
    deadline = time.monotonic() + args.seconds if args.seconds else None
    try:
        while deadline is None or time.monotonic() < deadline:
            time.sleep(args.interval)
            if args.verbose:
                snap = monitor.take_snapshot()
                w = snap.window
                print(f"  sample: exe={w.executable_name if w else '-'} "
                      f"title={w.title if w else '-'!r} "
                      f"idle={(snap.now - snap.last_input).total_seconds():.0f}s "
                      f"locked={snap.locked}", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        monitor.stop()
    return 0


def _setup_logging() -> None:
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(config.LOG_DIR / "timelens.log", encoding="utf-8")])


def run_tracker(args: argparse.Namespace) -> int:
    """Headless tracking: save everything to the database until Ctrl+C."""
    _setup_logging()
    guard = SingleInstance()
    if not guard.acquire():
        print("TimeLens is already running. Only one tracker may run at a time.")
        return 1
    db = Database(config.DB_PATH).open()
    engine = TrackingEngine(db)
    recovered = engine.start()
    print(f"TimeLens tracking (database: {config.DB_PATH}). Ctrl+C to stop."
          + (f" Recovered {recovered} unfinished session(s) from a previous crash." if recovered else ""))
    deadline = time.monotonic() + args.seconds if args.seconds else None
    try:
        while deadline is None or time.monotonic() < deadline:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        engine.stop()
        db.close()
        guard.release()
    print("Stopped. Data saved.")
    return 0


def run_gui(background: bool = False) -> int:
    """Normal launch: tracking engine + tray + main window.

    background=True (used by the Windows Startup shortcut) starts hidden in the tray, unless
    the tray is off or "open dashboard on startup" is on."""
    _setup_logging()
    from PySide6.QtWidgets import QApplication
    from ui.instance_bridge import InstanceServer, notify_running_instance
    from ui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName(config.APP_NAME)
    app.setQuitOnLastWindowClosed(False)        # hiding to the tray must not end the program
    guard = SingleInstance()
    if not guard.acquire():
        notify_running_instance()               # ask the running TimeLens to show its window
        print("TimeLens is already running - bringing its window to the front.")
        return 1
    db = Database(config.DB_PATH).open()
    engine = TrackingEngine(db)
    engine.start()
    if not engine.start_power_monitor():
        logging.getLogger(__name__).warning("Lock/sleep notifications unavailable; polling only")
    startup = StartupManager()
    if getattr(sys, "frozen", False):           # packaged EXE: keep the Startup shortcut in sync
        try:
            startup.sync(db.get_bool("start_with_windows"))
        except Exception:
            logging.getLogger(__name__).exception("Could not sync the startup shortcut")
    window = MainWindow(db, engine, startup=startup, with_tray=True)
    from PySide6.QtCore import QTimer
    from services.hang_watchdog import HangWatchdog
    dog = HangWatchdog(config.LOG_DIR / "hang.log")        # records all thread stacks if the window freezes
    dog.start()
    beat = QTimer()
    beat.timeout.connect(dog.beat)
    beat.start(1000)
    bridge = InstanceServer()
    bridge.show_requested.connect(window.show_dashboard)
    hidden = (background and window.will_hide_to_tray()
              and not db.get_bool("open_dashboard_on_startup"))
    if not hidden:
        window.show()
    try:
        return app.exec()
    finally:
        dog.stop()
        engine.stop()      # no-op if the window already stopped it
        db.close()
        guard.release()


def run_summary(args: argparse.Namespace) -> int:
    db = Database(config.DB_PATH).open()
    try:
        day = date.today()
        t = time_calc.get_day_totals(db, day)
        start, end = time_calc.local_day_bounds(day)
        rate = "n/a" if t.usage_rate is None else f"{t.usage_rate:.1f}%"
        print(f"TimeLens summary for {day} (local time)")
        print(f"  Active      {_fmt(t.active)}")
        print(f"  Idle        {_fmt(t.idle)}")
        print(f"  Locked      {_fmt(t.locked)}")
        print(f"  Sleep/Off   {_fmt(t.sleep_off)}")
        print(f"  Unmonitored {_fmt(t.unmonitored)}")
        print(f"  Available   {_fmt(t.available)}  (usage rate {rate})")
        print("Top applications:")
        for name, secs in time_calc.application_totals(db, start, end)[:5]:
            print(f"  {_fmt(secs)}  {name}")
        print("Top documents:")
        for app, doc, secs in time_calc.document_totals(db, start, end)[:5]:
            print(f"  {_fmt(secs)}  {doc}  ({app})")
    finally:
        db.close()
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="TimeLens (Phases 1-3: command-line tracking)")
    p.add_argument("--background", action="store_true",
                   help="start hidden in the tray (used by the Windows Startup shortcut)")
    p.add_argument("--selftest", metavar="RESULT_FILE",
                   help="check every part of TimeLens in a temp folder and write the result to a file")
    p.add_argument("--run", action="store_true", help="track and save to the database")
    p.add_argument("--summary", action="store_true", help="print today's totals from the database")
    p.add_argument("--diagnose", action="store_true", help="print live activity detection")
    p.add_argument("--seconds", type=float, default=0, help="stop after N seconds (0 = until Ctrl+C)")
    p.add_argument("--idle-threshold", type=float, default=config.DEFAULT_IDLE_THRESHOLD_SECONDS,
                   help="idle threshold in seconds (default 300 = 5 minutes)")
    p.add_argument("--interval", type=float, default=config.POLL_INTERVAL_SECONDS,
                   help="sampling interval in seconds (default 2)")
    p.add_argument("--verbose", action="store_true", help="print each sample")
    args = p.parse_args(argv)
    if args.selftest:
        from selftest import run_selftest
        return run_selftest(args.selftest)
    if not (args.run or args.summary or args.diagnose):
        return run_gui(args.background)
    if args.run:
        return run_tracker(args)
    if args.summary:
        return run_summary(args)
    if args.diagnose:
        return run_diagnose(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
