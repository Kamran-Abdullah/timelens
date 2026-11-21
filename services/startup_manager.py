"""Start TimeLens automatically with Windows - without administrator rights.

Method: a shortcut (.lnk) in the user's own Startup folder
    %APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\Startup
Windows runs everything in that folder at sign-in. It needs no admin rights and the user
can see/remove it in Task Manager > Startup apps. Disabling simply deletes the shortcut.

The shortcut starts TimeLens with --background, so it goes to the tray (if enabled)
instead of opening the window.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

SHORTCUT_NAME = "TimeLens.lnk"


def default_startup_dir() -> Path:
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def current_launch_command() -> tuple[str, str, str]:
    """(program, arguments, working directory) that would start this TimeLens."""
    if getattr(sys, "frozen", False):                       # packaged EXE
        exe = Path(sys.executable)
        return str(exe), "--background", str(exe.parent)
    project = Path(__file__).resolve().parents[1]           # running from source
    pythonw = Path(sys.executable).with_name("pythonw.exe")  # no console window
    program = pythonw if pythonw.exists() else Path(sys.executable)
    return str(program), f'"{project / "main.py"}" --background', str(project)


class StartupManager:
    def __init__(self, startup_dir: Optional[Path] = None,
                 command: Optional[tuple[str, str, str]] = None):
        self.startup_dir = Path(startup_dir) if startup_dir else default_startup_dir()
        self._command = command

    @property
    def shortcut_path(self) -> Path:
        return self.startup_dir / SHORTCUT_NAME

    def command(self) -> tuple[str, str, str]:
        return self._command or current_launch_command()

    def is_enabled(self) -> bool:
        return self.shortcut_path.exists()

    def enable(self) -> None:
        import pythoncom
        import win32com.client
        program, args, workdir = self.command()
        self.startup_dir.mkdir(parents=True, exist_ok=True)
        pythoncom.CoInitialize()
        try:
            shell = win32com.client.Dispatch("WScript.Shell")
            link = shell.CreateShortCut(str(self.shortcut_path))
            link.TargetPath = program
            link.Arguments = args
            link.WorkingDirectory = workdir
            link.Description = "TimeLens - offline activity tracker"
            link.WindowStyle = 7          # minimized
            link.Save()
        finally:
            pythoncom.CoUninitialize()
        log.info("Startup shortcut created: %s", self.shortcut_path)

    def disable(self) -> None:
        self.shortcut_path.unlink(missing_ok=True)
        log.info("Startup shortcut removed")

    def set_enabled(self, enabled: bool) -> None:
        self.enable() if enabled else self.disable()

    def points_to_current(self) -> bool:
        """True if the existing shortcut launches the TimeLens that is running now."""
        if not self.is_enabled():
            return False
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()
        try:
            link = win32com.client.Dispatch("WScript.Shell").CreateShortCut(str(self.shortcut_path))
            program, args, _ = self.command()
            return (Path(link.TargetPath).resolve() == Path(program).resolve()
                    and link.Arguments.strip() == args.strip())
        finally:
            pythoncom.CoUninitialize()

    def sync(self, wanted: bool) -> None:
        """Make the Startup folder match the setting (repairs a stale or missing shortcut)."""
        if wanted and not self.points_to_current():
            self.enable()
        elif not wanted and self.is_enabled():
            self.disable()
