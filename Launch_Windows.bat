@echo off
cd /d "%~dp0"
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
"%PY%" jebao_gui.py
if errorlevel 1 (
  echo.
  echo The app stopped with an error. Copy the messages above when asking for help.
  pause
)
