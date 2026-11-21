import os

import pytest

# Run Qt without a visible window so UI tests work anywhere.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(autouse=True)
def _assistant_does_not_scan_the_real_computer(monkeypatch):
    """Tests must not index the developer's real Desktop/Documents. A test that wants a file
    index builds its own FileIndex over a temporary folder."""
    monkeypatch.setattr("assistant.file_index.default_roots", lambda: [])
    monkeypatch.setattr("ui.assistant_page.default_roots", lambda: [])
    monkeypatch.setattr("assistant.apps.default_start_menu_roots", lambda: [])    # only the built-in Windows tools


@pytest.fixture(autouse=True)
def _never_read_real_browser_history(monkeypatch):
    """Tests must never open the developer's real Chrome/Edge history."""
    monkeypatch.setattr("services.web_history.history_sources", lambda browsers, local_appdata=None: {})


@pytest.fixture(autouse=True)
def _delete_qt_windows_after_each_test():
    """Destroy every window a test created.

    Without this, windows pile up for the whole test run, and each new window re-applies the
    application stylesheet to ALL of them, so every UI test is slower than the one before.
    (The real app only ever has one window, so this is purely a test-suite concern.)
    """
    yield
    try:
        from PySide6.QtWidgets import QApplication
    except Exception:
        return
    app = QApplication.instance()
    if app is None:
        return
    for widget in app.topLevelWidgets():
        widget.hide()
        widget.deleteLater()
    app.processEvents()
    app.sendPostedEvents(None, 52)       # 52 = QEvent.DeferredDelete: really delete them now
    app.processEvents()
