"""The explanation and choice shown before website tracking is turned on."""
from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import QMessageBox, QWidget

EXPLANATION = (
    "Website tracking reads the browsing history that Chrome and Edge already keep on this "
    "computer, and saves a copy inside TimeLens so you can see which sites you used and what you "
    "searched for.\n\n"
    "TimeLens will store:\n"
    "   - the site name (for example github.com)\n"
    "   - the page title\n"
    "   - the words you typed into a search engine\n"
    "   - when you visited and how long the page was open\n\n"
    "TimeLens will NOT store full web addresses, page contents, passwords, or private/incognito "
    "browsing (browsers do not record incognito visits in their history).\n\n"
    "Everything stays on this computer. You can exclude sites, turn this off, and delete all "
    "website history at any time in Settings."
)


def ask_web_consent(parent: Optional[QWidget] = None) -> Optional[int]:
    """Returns how many days of existing history to include (0 = only from now on), or None if
    the user said no."""
    box = QMessageBox(parent)
    box.setWindowTitle("Turn on website tracking?")
    box.setIcon(QMessageBox.Question)
    box.setText("Track which websites you visit in Chrome and Edge?")
    box.setInformativeText(EXPLANATION)
    now_only = box.addButton("Turn on, from now on", QMessageBox.AcceptRole)
    with_past = box.addButton("Turn on and include the past 30 days", QMessageBox.AcceptRole)
    box.addButton("No, keep it off", QMessageBox.RejectRole)
    box.setDefaultButton(now_only)
    box.exec()
    clicked = box.clickedButton()
    if clicked is now_only:
        return 0
    if clicked is with_past:
        return 30
    return None
