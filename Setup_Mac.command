#!/bin/bash
# Repair or reinstall Jebao Pump Sequencer on macOS. You don't need this to start: Launch_Mac.command
# sets the app up by itself the first time. Run this if the app stops starting (missing or broken
# packages): it creates this folder's own Python environment (.venv) if needed and reinstalls
# everything the app needs.
cd "$(dirname "${BASH_SOURCE[0]}")" || exit 1
fail() { echo; echo "$1"; read -r -p "Press Enter to close"; exit 1; }
new_enough() { "$1" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; }
echo "Setting up Jebao Pump Sequencer in $PWD"
echo
if [ -x .venv/bin/python ]; then
  echo "Using the existing .venv folder."
else
  PYTHON=""
  for p in python3.12 python3.13 python3.11 python3; do
    if command -v "$p" >/dev/null && new_enough "$p"; then PYTHON="$p"; break; fi
  done
  [ -n "$PYTHON" ] || fail "Python 3.11 or newer isn't installed. Get it from https://www.python.org/downloads/ and run this again."
  echo "Creating .venv with $PYTHON ..."
  "$PYTHON" -m venv .venv || fail "Couldn't create the .venv folder."
fi
PY=.venv/bin/python
new_enough "$PY" || fail "The .venv folder uses a Python older than 3.11. Delete it, install a newer Python, and run this again."
"$PY" -m pip install --upgrade pip || fail "Setup didn't finish. Copy the messages above when asking for help."
"$PY" -m pip install -e . || fail "Setup didn't finish. Copy the messages above when asking for help."

echo
echo "Checking that the app can start..."
"$PY" -c "import PyQt6.QtWidgets, qasync, requests" || fail "Setup didn't finish. Copy the messages above when asking for help."
echo
echo "Done. Start Jebao Pump Sequencer with Launch_Mac.command."
read -r -p "Press Enter to close"
