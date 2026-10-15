# Jebao Pump Sequencer

PyQt6 desktop app for Jebao MD-4.4 / Doser 2.4 four-channel dosing pumps. It talks to the pumps through the Gizwits cloud API (the same service the Jebao app uses) and supports manual doses, per-channel calibration, multi-step sequences and a global emergency stop.

## Setup
```
python -m venv .venv
.venv/bin/pip install -e .      # Windows: .venv\Scripts\pip install -e .
python jebao_gui.py                             # or double-click Launch_Mac.command / Launch_Windows.bat
```
Open **Settings**, enter your Jebao account, and the app lists the pumps bound to it.

## Settings and data
- Your login (`secrets.json`) and pump names, calibration and sequences (`jebao_config.json`) are stored in the app folder by default and are gitignored. The **Settings Folder** option can move them, for example to a synced folder.
- Logs and other data go to the data folder (`~/SyncThing/UAkron Playground/Jebao Pump` by default; override with `UAKRON_APP_DATA_DIR`).

Not affiliated with Jebao or Gizwits. MIT licensed (see `LICENSE`).
