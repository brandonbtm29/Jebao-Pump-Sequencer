# Jebao Pump Sequencer

PyQt6 app that drives Jebao MD-4.4 / Doser 2.4 4-channel dosing pumps (5 pumps, e.g. "JP 1: EtOH" … "JP 5: Sample") for SPE sequences: manual doses, per-channel calibration, multi-step sequences, and a global emergency stop. Part of Brandon's UAkron lab workspace (shared conventions in `../CLAUDE.md` when the repos are cloned side by side in `GitHub Projects/UAkron/`). GitHub: `brandonbtm29/Jebao-Pump-Sequencer`.

## Run, test, release
- `python jebao_gui.py` (PyQt6 + qasync + requests; Python 3.11+ because of `asyncio.Barrier`). Install with `pip install -e ".[dev]"`; `pyproject.toml` is the only manifest.
- Checks (same as CI, `.github/workflows/ci.yml`): `ruff check .`, `ruff format --check .`, `pytest`. Tests in `tests/` run Qt offscreen, point the data and settings folders at a temp dir before importing `jebao_gui`, and block the network.
- `settings_location.py` is excluded from ruff on purpose: keep it byte-identical to the other UAkron repos.
- Version lives only in `jebao_gui.py` (`__version__`). Releases follow `UAkron Playground/repo-modernization-playbook.md` (Phase 6): bump version, date the CHANGELOG section, annotated tag `vX.Y.Z`, `gh release create --verify-tag`.
- Commits: one concern each, end with a `Co-Authored-By:` trailer for the assistant that did the work. Commit email is the GitHub noreply address.

## Architecture
- **Cloud, not local.** The pumps disable local TCP control once they're on Wi-Fi, so the app uses the Gizwits Open API: log in, list bound devices, then send JSON control POSTs to `https://usapi.gizwits.com/app/control/{did}` (e.g. `{"channe3": true}`; the "channe" spelling is the API's own).
- Emergency stop sends all four channels `false` to every pump.
- `ConfigManager` → `jebao_config.json`: names, calibration (flow rates) and last volumes, keyed by cloud device id (`did`).
- `SecretsManager` → `secrets.json`: Jebao account email and password. **Gitignored. Never print, log, commit or copy its contents.**
- Main classes in `jebao_gui.py` (~1,900 lines): `GUILogHandler`, `CheckableComboBox`, `SettingsDialog`, `JebaoPumpGUI`.

## History and open work
- Jun 2026: the original spec (now in SyncThing `Jebao Pump/Archive/Removed from repo 2026-10-15/`) was a local UDP/TCP app using `python-jebao` + `qasync`. It was superseded by the cloud refactor on 26 Jun 2026.
- Jul 2026: UI layout, async handling and user-review flow updates.
- 15 Oct 2026: repo modernized per the playbook (pyproject, ruff, tests, CI, README, CHANGELOG). Released as v1.1.0; v1.0.0 tags the last commit before the modernization. History was rewritten to use the noreply email and drop personal paths; a pre-rewrite bundle is in SyncThing `Jebao Pump/Archive/`. The three bugs found in the audit were fixed in 1.1.0.
- Open: **Wi-Fi provisioning reverse engineering** (`docs/provisioning-research.md`). The goal is to bulk-provision factory-reset pumps without the phone app. The likely protocol is ESPTouch/SmartConfig: capture the Jebao Aqua app's traffic with Wireshark on the Mac, then write `jebao_provisioner.py`.
- Reference material is kept out of this public repo, in SyncThing: `UAkron Playground/Jebao Pump/Reference/` holds the Jebao manual (`Manuals, Refs/`) and the third-party reference repos and test scripts (`archive_tests_scripts/`). Don't modify them, and don't copy them back into the repo.

## Where code and data live
- **Code** (this repo) is cloned outside SyncThing: `%USERPROFILE%\GitHub Projects\UAkron\Jebao-Pump-Sequencer` on Windows, `~/GitHub Projects/UAkron/Jebao-Pump-Sequencer` on the Mac. It syncs through GitHub only.
- **Instrument data** stays in SyncThing: `%USERPROFILE%\SyncThing\UAkron Playground\Jebao Pump\` (Mac: `~/SyncThing/UAkron Playground/Jebao Pump/`). Never copy data into the repo, and never commit the settings files listed below.
- **How it works:** at startup the app `chdir`s into its SyncThing data folder (`DATA_DIR`, override with `UAKRON_APP_DATA_DIR` or `UAKRON_DATA_DIR`). Every relative data path (`Data/`, `Data Backups/`, `Sessions/`, run logs) therefore resolves inside SyncThing, and the default save folder is `Data` there. Files that ship with the code (icons, the OCR model) must be loaded via `script_dir` / `__file__`, never by bare relative name. Run the app from this clone.
- **Settings folder (since 10 Oct 2026):** the settings files (`jebao_config.json`, `secrets.json`) are read from `CONFIG_DIR`, resolved by `settings_location.py` (identical copy in every UAkron repo). Default: this repo folder (gitignored), so a fresh clone starts from the app's defaults. The per-machine pointer `settings_location.json` (gitignored), set from the app's "Settings Folder" box, can move them; a relative path is relative to the data folder. Brandon's machines point at `Settings`, i.e. `UAkron Playground/<App>/Settings/` in SyncThing, so settings stay identical across computers (including `secrets.json`; never print it). `UAKRON_SETTINGS_DIR` overrides the pointer. Store save folders relative to the data folder (e.g. `Data`), not as absolute paths.

## Launch
Double-click `Launch_Windows.bat` / `Launch_Mac.command`. They use this repo's `.venv` (create it with `python -m venv .venv` and `pip install -e ".[dev]"`). Windows Smart App Control may be on; if a DLL import is "blocked by an Application Control policy", retry once before investigating.
