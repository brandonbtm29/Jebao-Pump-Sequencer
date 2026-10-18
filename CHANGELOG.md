# Changelog

All notable changes to this project are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/), and the project uses [Semantic Versioning](https://semver.org/): new features bump the middle number (1.1.0 → 1.2.0), fixes bump the last (1.1.0 → 1.1.1), and the first number changes for a major redesign or for changes that break existing settings or saved sequences.

## [Unreleased]

## [2.0.0] - 2026-10-17

A new layout built around the rack of pumps. Your login, calibrations, pump order and saved sequences carry over; the pumps start in one stack in your old order until you arrange them in the Rack tab.

### Added
- **Manual** tab: every pump as it sits on the bench, each head as a tile with its name, flow rate, status and time left. Select heads (or **All 4** of a pump) and dispense a volume or run them for a time, all at once; each head runs for its own calibrated time. Pause, resume or cancel the selected heads. Tile size S / M / L.
- **Rack** tab: set the number of stacks and pumps per stack, drag pumps into place (or click a pump, then a slot), **Auto-arrange A→Z**, and name each pump and head (e.g. "Water → Col 3"). Names show on the tiles, in the Sequencer and in the log.
- Heads still on the 1.0 mL/min default are marked "not calibrated"; saving a calibration records its date.
- If a pump won't start, nothing is dispensed there and the log says so; a failed STOP is reported too.

### Changed
- Tabs are now Manual | Sequencer | Calibration | Rack. Saved sequences and the queue moved into the Sequencer tab.
- Refresh, Settings and the connection status moved to a bar along the top. The system log sits under every tab; drag the divider to give it more or less room.
- Emergency Stop also cancels manual doses.

### Removed
- The one-pump Manual Control tab and the pump list in the sidebar (replaced by the Manual and Rack tabs). Sort A→Z is now **Auto-arrange A→Z** in the Rack tab.

## [1.4.0] - 2026-10-17

### Added
- The system log is saved on each computer (one file per day in the data folder's `logs/`) and kept for 30 days; the number of days is a setting. When the app opens, recent history is shown again above the new session. **Open Log Folder** opens it.
- **Error log**: errors, with tracebacks (also from background threads), go to `logs/jebao_pump_sequencer.log`
  in the data folder (replaces `crash_log.txt`, which only kept the last crash). An unexpected error is
  shown in a dialog and the app keeps running instead of closing.
- `Setup_Windows.bat` / `Setup_Mac.command`: one double-click creates the `.venv` and installs everything.
- **Sort A→Z** for the pump list, with numbers in order (JP 2 before JP 10). Dragging still sets your own order.

### Changed
- A step waiting for review shows an orange bar across the top, with an Approve button for each waiting channel, instead of the Pending Approvals block in the sidebar.
- The clock moved to the status bar, next to the version.
- The Restart button moved from the main window to Settings → App. Updates and the Settings Folder option still restart the app by themselves.

### Fixed
- When the update check at startup couldn't reach GitHub (offline, or GitHub limiting requests), writing that to the log raised an error, so the version label never showed the result. Logging from background work is now safe.

## [1.3.0] - 2026-10-15

### Added
- The version is shown in the bottom-right corner. Each time the app starts it checks GitHub, and if a newer release is out the version turns orange ("Update available"); click it to update.
- The desktop shortcut is made automatically the first time the app starts and refreshed (icon and target) after each update. A checkbox in Settings turns this off.

### Changed
- The launchers (`Launch_Windows.bat`, `Launch_Mac.command`) close their console or Terminal window once the app has started. If the app can't start, the window stays open and says why.

## [1.2.0] - 2026-10-15

### Added
- **Check for Updates** in Settings: looks for a newer release on GitHub and can update and restart the app. It only moves to published releases, never over your own changes, reinstalls dependencies only when they changed (and goes back if that fails), and won't update while a sequence, dose or calibration is running.
- **Create Desktop Shortcut** in Settings: a desktop icon that starts the app (a shortcut without a console window on Windows, a small app you can put in the Dock on a Mac, a launcher on Linux).
- An app icon, shown in the window, the Dock and the Windows taskbar.

### Fixed
- The Settings dialog opens tall enough that its explanations aren't cut off.

## [1.1.0] - 2026-10-15

First tracked release. Version 1.0.0 is the app as it was already being used in the lab; this release fixes three bugs and tidies up the project around it. Earlier versions were co-developed with Google Gemini in Antigravity; development continues with Claude (Anthropic).

### Fixed
- Cloud requests now give up after 10 seconds, so a stalled connection can no longer hold up a dose, a login or the emergency stop.
- The emergency stop now names any pump that didn't confirm the stop (or says that no pumps are listed) instead of always reporting success.
- A sequence step whose channel can't be read is now logged instead of silently running on channel 1.

### Added
- Example settings files in `examples/`.
- A `jebao-pump` command, added when you install the app.

### Changed
- Installs from `pyproject.toml` (`pip install -e .`) and needs Python 3.11 or newer. `requirements.txt` is gone.
- New README with screenshots, setup steps for Windows and macOS, and known limitations.

## [1.0.0] - 2026-10-11

The working app before tracked releases.

### Added
- Control through the Jebao (Gizwits) cloud. The pumps stop answering local commands once they are on Wi-Fi.
- Manual dosing by volume or time, with per-channel pause and cancel.
- Calibration by collected volume or mass (water, ethanol, methanol or custom density), saved with tubing size.
- Sequence editor with Dose, Staccato, Pause, Pause for user review, Wait for all and Run sub-sequence steps, running each channel side by side.
- Saved sequences and a queue that runs them one after another.
- Global emergency stop for every channel on every pump.
- Dark and light themes and three text sizes.
- Settings Folder option, so your login and pump settings can live in a synced folder and stay the same on every computer.
- Double-click launchers for Windows and macOS.

[Unreleased]: https://github.com/brandonbtm29/Jebao-Pump-Sequencer/compare/v2.0.0...HEAD
[2.0.0]: https://github.com/brandonbtm29/Jebao-Pump-Sequencer/compare/v1.4.0...v2.0.0
[1.4.0]: https://github.com/brandonbtm29/Jebao-Pump-Sequencer/compare/v1.3.0...v1.4.0
[1.3.0]: https://github.com/brandonbtm29/Jebao-Pump-Sequencer/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/brandonbtm29/Jebao-Pump-Sequencer/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/brandonbtm29/Jebao-Pump-Sequencer/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/brandonbtm29/Jebao-Pump-Sequencer/releases/tag/v1.0.0
