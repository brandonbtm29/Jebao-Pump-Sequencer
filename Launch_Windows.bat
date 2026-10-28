@echo off
rem Double-click launcher for Windows. Starts the app without leaving a console window open.
rem The first time, it sets the app up: it creates this folder's own Python environment (.venv)
rem and installs what the app needs, the same way Setup_Windows.bat does.
rem (The desktop shortcut the app makes opens it without any console window at all.)
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" goto launch

rem First run: set up .venv (the same commands and checks as Setup_Windows.bat).
echo Setting up Jebao Pump Sequencer in %CD%
echo This is the first start, so it takes a minute or two. The app opens when it's done.
echo.
echo Creating .venv ...
py -3.12 -m venv .venv 2>nul || py -3 -m venv .venv 2>nul || python -m venv .venv
if not exist ".venv\Scripts\python.exe" goto nopython
".venv\Scripts\python.exe" -c "import sys; sys.exit(sys.version_info < (3, 11))" || goto oldpython
".venv\Scripts\python.exe" -m pip install --upgrade pip || goto failed
".venv\Scripts\python.exe" -m pip install -e . || goto failed
echo.
echo Setup finished. Starting the app...

:launch
set "PY=python"
set "PYW=pythonw"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if exist ".venv\Scripts\pythonw.exe" set "PYW=.venv\Scripts\pythonw.exe"

rem Quick check that the app can start. If not, show why and keep this window open.
"%PY%" -c "import PyQt6.QtWidgets, qasync, requests" 2>nul
if errorlevel 1 (
  echo The app can't start: its Python packages are missing or broken.
  echo.
  "%PY%" -c "import PyQt6.QtWidgets, qasync, requests"
  echo.
  echo To install what it needs, double-click Setup_Windows.bat.
  pause
  exit /b 1
)

rem pythonw runs the app without a console; errors from here on go to logs\jebao_pump_sequencer.log in the data folder.
start "" "%PYW%" jebao_gui.py
exit /b 0

:nopython
echo.
echo Python 3.11 or newer isn't installed, or isn't on PATH.
echo Install it from https://www.python.org/downloads/ (tick "Add python.exe to PATH"), then double-click Launch_Windows.bat again.
pause
exit /b 1

:oldpython
echo.
echo The .venv folder uses a Python older than 3.11. Delete the .venv folder, install a newer
echo Python from https://www.python.org/downloads/, then double-click Launch_Windows.bat again.
pause
exit /b 1

:failed
echo.
echo Setup didn't finish. Copy the messages above when asking for help.
echo To try again, double-click Setup_Windows.bat.
pause
exit /b 1
