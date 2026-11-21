"""Run slow work (database queries, calculations) off the GUI thread.

    run_async(fn, receiver.on_done, receiver.on_error)

`fn` runs in a worker thread. The result is delivered to `receiver.on_done` back on the
GUI thread. `on_done` must be a method of a QObject (e.g. a page widget) so Qt can queue
the call to the right thread.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

log = logging.getLogger(__name__)
_alive: set = set()   # keep jobs referenced until they report back


class _Signals(QObject):
    done = Signal(object)
    failed = Signal(str)


class _Job(QRunnable):
    def __init__(self, fn: Callable):
        super().__init__()
        self.fn = fn
        self.signals = _Signals()

    def run(self):
        try:
            result = self.fn()
        except Exception as e:  # report to the GUI instead of dying silently
            log.exception("Background job failed")
            self.signals.failed.emit(str(e))
        else:
            self.signals.done.emit(result)


def run_async(fn: Callable, on_done: Callable, on_error: Optional[Callable] = None) -> None:
    job = _Job(fn)
    _alive.add(job)

    def _finish(*_):
        _alive.discard(job)

    job.signals.done.connect(on_done)
    if on_error:
        job.signals.failed.connect(on_error)
    job.signals.done.connect(_finish)
    job.signals.failed.connect(_finish)
    QThreadPool.globalInstance().start(job)


def wait_for_jobs(timeout_ms: int = 10_000) -> bool:
    """Block until running background jobs finish. Call before closing the database:
    closing a SQLite connection while another thread is mid-query crashes the process."""
    return QThreadPool.globalInstance().waitForDone(timeout_ms)
