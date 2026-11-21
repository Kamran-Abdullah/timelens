import pytest

from tracker.document_detector import detect_document


@pytest.mark.parametrize("exe,title,expected,unsaved", [
    ("EXCEL.EXE", "October Payroll.xlsx - Excel", "October Payroll.xlsx", False),
    ("EXCEL.EXE", "Bank Rec.xlsm  -  Excel", "Bank Rec.xlsm", False),
    ("EXCEL.EXE", "Old.xls [Compatibility Mode] - Excel", "Old.xls", False),
    ("EXCEL.EXE", "Report.xlsx [Read-Only] - Excel", "Report.xlsx", False),
    ("EXCEL.EXE", "Book1 - Excel", "Book1", True),
    ("WINDWORD.EXE".replace("WINDWORD", "WINWORD"), "Letter.docx - Word", "Letter.docx", False),
    ("WINWORD.EXE", "Document1 - Word", "Document1", True),
    ("POWERPNT.EXE", "Deck.pptx - PowerPoint", "Deck.pptx", False),
    ("POWERPNT.EXE", "PowerPoint Slide Show - [Deck.pptx]", "Deck.pptx", False),
    ("Code.exe", "main.py - TimeLens - Visual Studio Code", "main.py", False),
    ("Code.exe", "\u25cf main.py - TimeLens - Visual Studio Code", "main.py", False),
    ("Code.exe", "Untitled-1 - Visual Studio Code", "Untitled-1", True),
    ("notepad.exe", "notes.txt - Notepad", "notes.txt", False),
    ("notepad.exe", "*Untitled - Notepad", "Untitled", True),
    ("notepad++.exe", "C:\\Users\\me\\secret\\plan.txt - Notepad++", "plan.txt", False),
    ("AcroRd32.exe", "Invoice.pdf - Adobe Acrobat Reader (64-bit)", "Invoice.pdf", False),
    ("SumatraPDF.exe", "[Not saved] Manual.pdf - SumatraPDF", "Manual.pdf", False),
    ("somecad.exe", "Drawing.sql - SomeCAD", "Drawing.sql", False),
])
def test_documents_detected(exe, title, expected, unsaved):
    doc = detect_document(exe, title)
    assert doc is not None
    assert doc.name == expected
    assert doc.is_unsaved == unsaved


@pytest.mark.parametrize("exe,title", [
    ("EXCEL.EXE", "Excel"),
    ("EXCEL.EXE", ""),
    ("WINWORD.EXE", "Microsoft Word"),
    ("Code.exe", "Visual Studio Code"),
    ("Code.exe", "Welcome - Visual Studio Code"),
    ("chrome.exe", "Annual Report.pdf - Google Chrome"),   # browsers: never
    ("chrome.exe", "Bank login - Google Chrome"),
    ("Teams.exe", "Weekly sync | Microsoft Teams"),
    ("unknown.exe", "\x00\ufffd weird ---"),
])
def test_no_document(exe, title):
    assert detect_document(exe, title) is None


def test_never_returns_path():
    doc = detect_document("notepad.exe", "C:\\a\\b\\c.txt - Notepad")
    assert "\\" not in doc.name and "/" not in doc.name
