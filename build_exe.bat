@echo off
rem Builds the standalone TimeLens EXE and checks it. Run from the TimeLens folder.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo The virtual environment .venv was not found. See README.md.
    exit /b 1
)
echo === Installing PyInstaller (build tool) ===
".venv\Scripts\python.exe" -m pip install --quiet pyinstaller
echo === Building TimeLens.exe (this takes several minutes) ===
".venv\Scripts\python.exe" -m PyInstaller TimeLens.spec --noconfirm --clean
if errorlevel 1 (
    echo BUILD FAILED
    exit /b 1
)
echo === Checking the built EXE with the built-in self-test ===
"dist\TimeLens\TimeLens.exe" --selftest "dist\selftest-result.txt"
if errorlevel 1 (
    echo SELF-TEST FAILED - see dist\selftest-result.txt
    type "dist\selftest-result.txt"
    exit /b 1
)
type "dist\selftest-result.txt"
echo.
echo Done. Run: dist\TimeLens\TimeLens.exe
