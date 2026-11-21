"""Time-calculation tests using a simulated clock (no Windows needed)."""
from datetime import datetime, timedelta, timezone

from tracker.session_manager import EventKind, SessionManager, Snapshot, Status
from tracker.window_detector import WindowInfo

T0 = datetime(2026, 11, 3, 9, 0, 0, tzinfo=timezone.utc)
THRESH = 300
POLL = 2


def win(exe, app, title):
    return WindowInfo(1, 1, title, exe, app)


EXCEL_A = win("EXCEL.EXE", "Microsoft Excel", "A.xlsx - Excel")
EXCEL_B = win("EXCEL.EXE", "Microsoft Excel", "B.xlsx - Excel")
CHROME = win("chrome.exe", "Google Chrome", "News - Google Chrome")


class Sim:
    """Drives a SessionManager through time; collects closed segments."""

    def __init__(self):
        self.m = SessionManager(THRESH, max_gap_seconds=10)
        self.t = T0
        self.last_input = T0
        self.closed = []
        self.gaps = []

    def run(self, seconds, window, locked=False, input_active=True):
        end = self.t + timedelta(seconds=seconds)
        while self.t < end:
            if input_active and not locked:
                self.last_input = self.t
            self._feed(window, locked)
            self.t += timedelta(seconds=POLL)

    def jump(self, seconds):
        self.t += timedelta(seconds=seconds)

    def _feed(self, window, locked):
        for ev in self.m.process(Snapshot(self.t, window, self.last_input, locked)):
            if ev.kind is EventKind.CLOSED:
                self.closed.append(ev.segment)
            elif ev.kind is EventKind.GAP:
                self.gaps.append(ev)

    def finish(self):
        for ev in self.m.stop(self.t):
            self.closed.append(ev.segment)
        return self.closed


def total(segs, status=None, doc=None):
    return sum(s.duration_seconds for s in segs
               if (status is None or s.status is status)
               and (doc is None or s.document_name == doc))


def assert_no_overlap(segs):
    for a, b in zip(segs, segs[1:]):
        assert a.end <= b.start


def test_excel_30_minutes():
    s = Sim()
    s.run(1800, EXCEL_A)
    segs = s.finish()
    assert len(segs) == 1
    assert segs[0].document_name == "A.xlsx"
    assert total(segs, Status.ACTIVE) == 1800


def test_switch_between_two_workbooks():
    s = Sim()
    s.run(600, EXCEL_A)
    s.run(300, EXCEL_B)
    segs = s.finish()
    assert [x.document_name for x in segs] == ["A.xlsx", "B.xlsx"]
    assert total(segs, doc="A.xlsx") == 600 and total(segs, doc="B.xlsx") == 300
    assert_no_overlap(segs)


def test_switch_excel_to_chrome():
    s = Sim()
    s.run(120, EXCEL_A)
    s.run(60, CHROME)
    segs = s.finish()
    assert [x.application_name for x in segs] == ["Microsoft Excel", "Google Chrome"]
    assert segs[1].document_name is None
    assert total(segs) == 180


def test_idle_splits_exactly_at_threshold():
    s = Sim()
    s.run(60, EXCEL_A)                          # 60 s of input (last input at 58 s)
    s.run(700, EXCEL_A, input_active=False)     # then no input
    segs = s.finish()
    active = [x for x in segs if x.status is Status.ACTIVE]
    idle = [x for x in segs if x.status is Status.IDLE]
    assert len(active) == 1 and len(idle) == 1
    last_input = T0 + timedelta(seconds=58)
    assert active[0].end == last_input + timedelta(seconds=THRESH)
    assert idle[0].start == active[0].end
    assert_no_overlap(segs)


def test_four_minutes_without_input_is_not_idle():
    s = Sim()
    s.run(10, EXCEL_A)
    s.run(240, EXCEL_A, input_active=False)
    segs = s.finish()
    assert all(x.status is Status.ACTIVE for x in segs) and len(segs) == 1


def test_return_after_20_minutes_idle_and_reconciliation():
    s = Sim()
    s.run(60, EXCEL_A)
    s.run(1200, EXCEL_A, input_active=False)    # 20 min away
    s.run(60, EXCEL_A)                          # back
    segs = s.finish()
    statuses = [x.status for x in segs]
    assert statuses == [Status.ACTIVE, Status.IDLE, Status.ACTIVE]
    assert_no_overlap(segs)
    # Contiguous and reconciles with wall-clock time observed (no loss, no double count).
    assert segs[0].start == T0
    for a, b in zip(segs, segs[1:]):
        assert a.end == b.start
    assert total(segs) == (s.t - T0).total_seconds()
    # idle = from (last input + 5 min) until input resumed
    idle = segs[1]
    assert idle.end == segs[2].start
    assert idle.duration_seconds == 1200 - THRESH + POLL  # idle ends at first new input


def test_lock_and_unlock():
    s = Sim()
    s.run(60, EXCEL_A)
    s.run(120, None, locked=True)
    s.run(60, EXCEL_A)
    segs = s.finish()
    assert [x.status for x in segs] == [Status.ACTIVE, Status.LOCKED, Status.ACTIVE]
    assert total(segs, Status.LOCKED) == 120


def test_lock_has_priority_over_idle():
    s = Sim()
    s.run(10, EXCEL_A)
    s.run(900, None, locked=True, input_active=False)   # locked 15 min, input also stale
    segs = s.finish()
    assert Status.IDLE not in [x.status for x in segs]
    assert total(segs, Status.LOCKED) == 900


def test_sleep_gap_is_not_counted_and_not_idle():
    s = Sim()
    s.run(60, EXCEL_A)
    s.jump(3600)                       # laptop asleep for 1 hour (no samples)
    s.run(60, EXCEL_A)
    segs = s.finish()
    assert len(s.gaps) == 1
    assert s.gaps[0].gap_end - s.gaps[0].gap_start >= timedelta(minutes=59)
    assert Status.IDLE not in [x.status for x in segs]
    assert total(segs) <= 125          # only the ~120 s actually observed
    assert_no_overlap(segs)


def test_starting_while_already_idle_does_not_invent_time():
    s = Sim()
    s.last_input = T0 - timedelta(hours=2)
    s.run(20, EXCEL_A, input_active=False)
    segs = s.finish()
    assert len(segs) == 1 and segs[0].status is Status.IDLE
    assert segs[0].start == T0


def test_pause_closes_segment_and_resume_starts_fresh():
    s = Sim()
    s.run(60, EXCEL_A)
    s.finish()                         # pause
    s.jump(600)
    s.closed.clear()
    s.run(30, EXCEL_A)
    segs = s.finish()
    assert len(segs) == 1 and total(segs) == 30
    assert s.gaps == []


def test_no_foreground_window_is_handled():
    s = Sim()
    s.run(10, None)
    segs = s.finish()
    assert segs[0].application_name == "Desktop"


def test_title_change_same_document_does_not_split():
    s = Sim()
    s.run(10, win("EXCEL.EXE", "Microsoft Excel", "A.xlsx - Excel"))
    s.run(10, win("EXCEL.EXE", "Microsoft Excel", "A.xlsx [Read-Only] - Excel"))
    assert len(s.finish()) == 1
