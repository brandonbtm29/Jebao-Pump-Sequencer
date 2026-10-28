@echo off
rem Repair or reinstall Jebao Pump Sequencer. You don't need this to start: Launch_Windows.bat sets
rem the app up by itself the first time. Run this if the app stops starting (missing or broken
rem packages): it creates this folder's own Python environment (.venv) if needed and reinstalls
rem everything the app needs.
cd /d "%~dp0"
echo Setting up Jebao Pump Sequencer in %CD%
echo.
if exist ".venv\Scripts\python.exe" (
  echo Using the existing .venv folder.
) else (
  echo Creating .venv ...
  py -3.12 -m venv .venv 2>nul || py -3 -m venv .venv 2>nul || python -m venv .venv
  if not exist ".venv\Scripts\python.exe" goto nopython
)
set "PY=.venv\Scripts\python.exe"
"%PY%" -c "import sys; sys.exit(sys.version_info < (3, 11))" || goto oldpython
"%PY%" -m pip install --upgrade pip || goto failed
"%PY%" -m pip install -e . || goto failed

echo.
echo Checking that the app can start...
"%PY%" -c "import PyQt6.QtWidgets, qasync, requests" || goto failed
echo.
echo Done. Start Jebao Pump Sequencer with Launch_Windows.bat.
pause
exit /b 0

:nopython
echo.
echo Python 3.11 or newer isn't installed, or isn't on PATH.
echo Install it from https://www.python.org/downloads/ (tick "Add python.exe to PATH"), then run this again.
pause
exit /b 1

:oldpython
echo.
echo The .venv folder uses a Python older than 3.11. Delete the .venv folder, install a newer
echo Python from https://www.python.org/downloads/, then run this again.
pause
exit /b 1

:failed
echo.
echo Setup didn't finish. Copy the messages above when asking for help.
pause
exit /b 1
