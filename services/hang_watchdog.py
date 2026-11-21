"""Hang diagnostics: if the window stops responding, record what EVERY thread was doing.

The GUI calls beat() every second. A background thread checks that heartbeat; if it has not
arrived for `threshold` seconds the interface is frozen, so the watchdog appends a timestamped
stack trace of all threads to logs/hang.log (once per freeze). That file shows exactly which
line of code each thread is stuck on, which is what is needed to fix a freeze.
It only writes locally and never changes behaviour.
"""
from __future__ import annotations

import faulthandler
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)


class HangWatchdog:
    def __init__(self, path: Path | str, threshold: float = 10.0, check_every: float = 2.0,
                 clock: Callable[[], float] = time.monotonic):
        self.path = Path(path)
        self.threshold, self.check_every, self._clock = threshold, check_every, clock
        self._last = clock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._reported = False
        self.reports = 0

    def beat(self) -> None:
        """Called by the GUI thread; proves the event loop is alive."""
        self._last = self._clock()

    def start(self) -> None:
        self._last = self._clock()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="TimeLensWatchdog", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.check_every + 1)

    def _run(self) -> None:
        while not self._stop.wait(self.check_every):
            stalled = self._clock() - self._last
            if stalled >= self.threshold:
                if not self._reported:
                    self._reported = True
                    self._dump(stalled)
            elif self._reported:
                self._reported = False
                self._note(f"interface responding again after the freeze ({datetime.now():%H:%M:%S})")

    def _note(self, text: str) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(text + "\n")
        except OSError:
            log.exception("Could not write the hang log")

    def _dump(self, stalled: float) -> None:
        self.reports += 1
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(f"\n=== {datetime.now():%Y-%m-%d %H:%M:%S}: interface stalled for {stalled:.0f} s "
                        f"- all thread stacks follow ===\n")
                f.flush()
                faulthandler.dump_traceback(file=f, all_threads=True)
            log.error("Interface stalled for %.0f s; thread stacks written to %s", stalled, self.path)
        except Exception:
            log.exception("Could not write the hang report")
