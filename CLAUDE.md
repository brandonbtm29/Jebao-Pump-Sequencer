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
- **Instrument data** stays in SyncThing: `%USERPROFILE%\SyncThing\UAkron Playground\Jebao Pump\` (Mac: `~/SyncThing/UAkron Playground/Jebao Pump/`). Never copy data, `config.json` or `secrets.json` into the repo.
- **Transition status (Oct 2026):** the app still writes its data relative to the folder it's launched from, so lab runs still use the copy inside SyncThing. Planned change: add a per-machine "data folder" setting (stored in the gitignored config) that defaults to the SyncThing path above. Once that lands, run the app from this clone and keep only data in SyncThing.
