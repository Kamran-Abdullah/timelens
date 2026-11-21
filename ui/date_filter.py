"""Date filter bar: a preset dropdown plus two date pickers for a custom range."""
from __future__ import annotations

from datetime import date

from PySide6.QtCore import QDate, Signal
from PySide6.QtWidgets import QComboBox, QDateEdit, QHBoxLayout, QLabel, QWidget

from analytics.periods import PRESETS, preset_range


class DateFilter(QWidget):
    changed = Signal()

    def __init__(self, default: str = "All time", presets: list[str] | None = None):
        super().__init__()
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(QLabel("Period:"))
        self.combo = QComboBox()
        self.combo.addItems(presets or PRESETS)
        self.combo.setCurrentText(default)
        row.addWidget(self.combo)
        today = QDate.currentDate()
        self.start = QDateEdit(today.addDays(-6))
        self.end = QDateEdit(today)
        self.sep = QLabel("to")
        for d in (self.start, self.end):
            d.setCalendarPopup(True)
            d.setDisplayFormat("dd MMM yyyy")
            d.dateChanged.connect(lambda _=None: self.changed.emit())
        for w in (self.start, self.sep, self.end):
            row.addWidget(w)
        self.combo.currentTextChanged.connect(self._preset_changed)
        self._preset_changed()

    def _preset_changed(self, *_):
        custom = self.combo.currentText() == "Custom"
        for w in (self.start, self.sep, self.end):
            w.setVisible(custom)
        self.changed.emit()

    @property
    def label(self) -> str:
        return self.combo.currentText()

    def date_range(self, today: date | None = None):
        """(start_utc, end_utc) for the selected period."""
        today = today or date.today()
        custom = (self.start.date().toPython(), self.end.date().toPython())
        return preset_range(self.label, today, custom)
