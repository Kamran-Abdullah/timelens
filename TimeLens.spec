# PyInstaller build recipe for TimeLens.   Build with:  build_exe.bat   (or: pyinstaller TimeLens.spec)
#
# Output: dist\TimeLens\TimeLens.exe  (a folder build - see README for why not one single file)
from PyInstaller.utils.hooks import collect_submodules

hidden = [
    # pywin32 pieces that are imported indirectly
    "win32timezone", "win32com.client", "pythoncom", "pywintypes", "win32ts", "win32gui",
    "win32api", "win32con", "win32com.shell.shell", "win32com.shell.shellcon",   # known folders (Assistant)
    # chart backends used by the UI (Qt) and by the PDF export (Agg)
    "matplotlib.backends.backend_qtagg", "matplotlib.backends.backend_agg",
    # the Windows-only Qt network pipe used for "second launch shows the first window"
    "PySide6.QtNetwork",
] + collect_submodules("openpyxl.cell") + collect_submodules("reportlab.pdfbase")

# Things TimeLens never uses - leaving them out keeps the download smaller.
excluded = [
    "tkinter", "matplotlib.backends.backend_tkagg", "matplotlib.backends.backend_tk",
    "PyQt5", "PyQt6", "IPython", "scipy", "pytest", "sphinx", "notebook",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtQml", "PySide6.QtQuick",
    "PySide6.Qt3DCore", "PySide6.QtMultimedia", "PySide6.QtPdf", "PySide6.QtSql", "PySide6.QtTest",
]

a = Analysis(
    ["main.py"],
    pathex=["."],
    datas=[("assets", "assets")],
    hiddenimports=hidden,
    excludes=excluded,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,          # folder build: libraries sit next to the EXE
    name="TimeLens",
    icon="assets/timelens.ico",
    console=False,                  # no black console window
    disable_windowed_traceback=False,
    upx=False,                      # UPX-compressed EXEs trigger more antivirus warnings
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="TimeLens")
