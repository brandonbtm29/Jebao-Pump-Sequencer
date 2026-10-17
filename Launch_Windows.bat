@echo off
rem Double-click launcher for Windows. Starts the app without leaving a console window open.
rem (The desktop shortcut the app makes opens it without any console window at all.)
cd /d "%~dp0"
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

rem pythonw runs the app without a console; errors from here on go to crash_log.txt in the data folder.
start "" "%PYW%" jebao_gui.py
exit /b 0
