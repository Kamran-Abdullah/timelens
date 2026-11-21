"""What the assistant is allowed to open.

Opening a file asks Windows to run it with its default program. That is harmless for a
document and dangerous for a program or script, so the assistant only opens an allow-list of
document, image, audio and video types and flatly refuses everything else (including
double extensions such as "invoice.pdf.exe", which are judged by their LAST extension).
"""
from __future__ import annotations

from pathlib import Path

SAFE_EXTENSIONS = frozenset({
    # office documents
    ".doc", ".docx", ".docm", ".rtf", ".odt", ".xls", ".xlsx", ".xlsm", ".xlsb", ".csv", ".ods",
    ".ppt", ".pptx", ".pptm", ".odp", ".pdf",
    # text
    ".txt", ".md", ".log", ".json", ".xml",
    # images
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff",
    # audio / video
    ".mp3", ".wav", ".m4a", ".flac", ".mp4", ".mkv", ".avi", ".mov", ".wmv",
})

# Listed so the refusal can say WHY; anything not in SAFE_EXTENSIONS is refused regardless.
DANGEROUS_EXTENSIONS = frozenset({
    ".exe", ".bat", ".cmd", ".com", ".scr", ".msi", ".msp", ".ps1", ".psm1", ".vbs", ".vbe", ".js",
    ".jse", ".wsf", ".wsh", ".hta", ".lnk", ".url", ".reg", ".jar", ".dll", ".sys", ".cpl", ".py",
    ".pyw", ".sh", ".appx", ".msix", ".iso", ".html", ".htm", ".svg",
})

# Words used to group extensions in questions ("my excel files").
KIND_EXTENSIONS = {
    "excel": (".xlsx", ".xlsm", ".xls", ".xlsb", ".csv"),
    "word": (".docx", ".doc", ".docm", ".rtf"),
    "powerpoint": (".pptx", ".ppt", ".pptm"),
    "pdf": (".pdf",),
    "text": (".txt", ".md", ".log"),
    "image": (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff"),
    "video": (".mp4", ".mkv", ".avi", ".mov", ".wmv"),
    "audio": (".mp3", ".wav", ".m4a", ".flac"),
}


def check_openable(path: Path | str) -> tuple[bool, str]:
    """(allowed, reason). The reason is shown to the user when the answer is no."""
    p = Path(str(path).strip().rstrip(". "))      # Windows ignores trailing dots and spaces
    ext = p.suffix.lower()
    if not ext:
        return False, "That has no file type, so I can't tell it is a document."
    if ext in SAFE_EXTENSIONS:
        return True, ""
    if ext in DANGEROUS_EXTENSIONS:
        return False, (f"I won't open {ext} files: they can run programs or scripts. "
                       "I only open documents, images, audio and video.")
    return False, f"I only open documents, images, audio and video, not {ext} files."
