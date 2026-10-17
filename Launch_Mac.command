#!/bin/bash
# Double-click launcher for macOS. Starts the app, then closes this Terminal window.
# (The desktop shortcut the app makes opens it without any Terminal window at all.)
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR" || exit 1
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
