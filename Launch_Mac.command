#!/bin/bash
# Double-click launcher for macOS. Starts the app, then closes this Terminal window.
# The first time, it sets the app up: it creates this folder's own Python environment (.venv)
# and installs what the app needs, the same way Setup_Mac.command does.
# (The desktop shortcut the app makes opens it without any Terminal window at all.)
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR" || exit 1

# First run: set up .venv (the same commands and checks as Setup_Mac.command).
if [ ! -x ".venv/bin/python" ]; then
  fail() { echo; echo "$1"; read -r -p "Press Enter to close"; exit 1; }
  new_enough() { "$1" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; }
  echo "Setting up Jebao Pump Sequencer in $PWD"
  echo "This is the first start, so it takes a minute or two. The app opens when it's done."
  echo
  PYTHON=""
  for p in python3.12 python3.13 python3.11 python3; do
    if command -v "$p" >/dev/null && new_enough "$p"; then PYTHON="$p"; break; fi
  done
  [ -n "$PYTHON" ] || fail "Python 3.11 or newer isn't installed. Get it from https://www.python.org/downloads/ and double-click Launch_Mac.command again."
  echo "Creating .venv with $PYTHON ..."
  "$PYTHON" -m venv .venv || fail "Couldn't create the .venv folder. To try again, double-click Setup_Mac.command."
  .venv/bin/python -m pip install --upgrade pip || fail "Setup didn't finish. Copy the messages above when asking for help. To try again, double-click Setup_Mac.command."
  .venv/bin/python -m pip install -e . || fail "Setup didn't finish. Copy the messages above when asking for help. To try again, double-click Setup_Mac.command."
  echo
  echo "Setup finished. Starting the app..."
fi
if [ -x ".venv/bin/python" ]; then PY=".venv/bin/python"; else PY="python3"; fi

# Quick check that the app can start. If not, show why and keep this window open.
if ! "$PY" -c "import PyQt6.QtWidgets, qasync, requests" 2>/dev/null; then
  echo "The app can't start: its Python packages are missing or broken."
  echo
  "$PY" -c "import PyQt6.QtWidgets, qasync, requests"
  echo
  echo "To install what it needs, double-click Setup_Mac.command."
  read -r -p "Press Enter to close"
  exit 1
fi

# Start the app in its own session, detached from this window, so closing the
# window doesn't stop it. Errors after this point go to logs/jebao_pump_sequencer.log in the data folder.
"$PY" -c 'import subprocess, sys; subprocess.Popen([sys.executable, "jebao_gui.py"], start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)'

# Close this Terminal window once this script has exited.
TTY=$(tty)
osascript -e "tell application \"Terminal\" to close (every window whose selected tab's tty is \"$TTY\")" >/dev/null 2>&1 &
exit 0
