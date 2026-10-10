# Jebao Pump Sequencer

PyQt6 app that drives Jebao MD-4.4 / Doser 2.4 4-channel dosing pumps (5 pumps, e.g. "JP 1: EtOH" … "JP 5: Sample") for SPE sequences: manual doses, per-channel calibration, multi-step sequences, and a global emergency stop. Part of Brandon's UAkron lab workspace (shared conventions in `../CLAUDE.md` when the repos are cloned side by side in `GitHub Projects/UAkron/`). GitHub: `brandonbtm29/Jebao-Pump-Sequencer`.

## Run
`python jebao_gui.py` (PyQt6, requests; async work runs off the GUI thread)

## Architecture
- **Cloud, not local.** The pumps disable local TCP control once they're on Wi-Fi, so the app uses the Gizwits Open API: log in, list bound devices, then send JSON control POSTs to `https://usapi.gizwits.com/app/control/{did}` (e.g. `{"channe3": true}`; the "channe" spelling is the API's own).
- Emergency stop sends all four channels `false` to every pump.
- `ConfigManager` → `jebao_config.json`: names, calibration (flow rates) and last volumes, keyed by cloud device id (`did`).
- `SecretsManager` → `secrets.json`: Jebao account email and password. **Gitignored. Never print, log, commit or copy its contents.**
- Main classes in `jebao_gui.py` (~1,750 lines): `GUILogHandler`, `CheckableComboBox`, `SettingsDialog`, `JebaoPumpGUI`.

## History and open work
- Jun 2026: the original spec (`Agent Instructions/Project Specification.docx`) was a local UDP/TCP app using `python-jebao` + `qasync`. It was superseded by the cloud refactor on 26 Jun 2026.
- Jul 2026: UI layout, async handling and user-review flow updates.
- Open: **Wi-Fi provisioning reverse engineering** (`Reverse_Engineering_Plan.md`). The goal is to bulk-provision factory-reset pumps without the phone app. The likely protocol is ESPTouch/SmartConfig: capture the Jebao Aqua app's traffic with Wireshark on the Mac, then write `jebao_provisioner.py`.
- `archive_tests_scripts/` contains third-party repos used for reference. Don't modify them.

## Where code and data live
- **Code** (this repo) is cloned outside SyncThing: `%USERPROFILE%\GitHub Projects\UAkron\Jebao-Pump-Sequencer` on Windows, `~/GitHub Projects/UAkron/Jebao-Pump-Sequencer` on the Mac. It syncs through GitHub only.
- **Instrument data** stays in SyncThing: `%USERPROFILE%\SyncThing\UAkron Playground\Jebao Pump\` (Mac: `~/SyncThing/UAkron Playground/Jebao Pump/`). Never copy data into the repo, and never commit the settings files listed below.
- **How it works:** at startup the app `chdir`s into its SyncThing data folder (`DATA_DIR`, override with `UAKRON_APP_DATA_DIR` or `UAKRON_DATA_DIR`). Every relative data path (`Data/`, `Data Backups/`, `Sessions/`, run logs) therefore resolves inside SyncThing, and the default save folder is `Data` there. Files that ship with the code (icons, the OCR model) must be loaded via `script_dir` / `__file__`, never by bare relative name. Run the app from this clone.
- **Settings are per machine (since 10 Oct 2026):** `config.json` / `pump_config.json` / `jebao_config.json` / `secrets.json` / `global_rois.json` live in this repo folder next to the app (`CONFIG_DIR`; next to the executable in frozen builds). They are gitignored and don't sync, so each machine (and anyone who clones the repo) keeps its own, and a fresh clone starts from the app's defaults. Store save folders relative to the data folder (e.g. `Data`), not as absolute paths.

## Launch
Double-click `Launch_Windows.bat` / `Launch_Mac.command`. They use this repo's `.venv` (create it with `python -m venv .venv` and `pip install -r requirements.txt`). Windows Smart App Control may be on; if a DLL import is "blocked by an Application Control policy", retry once before investigating.
