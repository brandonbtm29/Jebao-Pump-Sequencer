# Changelog

All notable changes to this project are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/), and the project uses [Semantic Versioning](https://semver.org/): new features bump the middle number (1.1.0 → 1.2.0), fixes bump the last (1.1.0 → 1.1.1), and the first number changes for a major redesign or for changes that break existing settings or saved sequences.

## [Unreleased]

## [2.5.0] - 2026-10-27

A pump head was left running overnight and burnt out its resistor. This release makes sure no head is left running, and that none runs too long.

### Fixed
- **Sequence steps ignored a failed OFF.** A Dose or Staccato step that couldn't turn its head off (Wi-Fi drop, cloud error) didn't retry or say anything, so the head kept running.
- **A sequence stopped by an error left other channels running.** The other channels' doses were cut off without an OFF.
- **A manual dose whose ON reply timed out was never turned off.** The app reported "couldn't start", but the pump had started.
- **An OFF sent while a pump was off Wi-Fi was lost**, so the pump kept running after it reconnected.
- **Emergency Stop didn't stop Async Dose/Staccato steps.** A staccato waiting between bursts started its next burst.
- **Calibration gave up on a head that wouldn't stop** after three quick tries.
- **The app stopped checking the cloud after a failed login** until Refresh was pressed. It now keeps trying in the background.

### Added
- **Pump guard.**
  - **OFF until it works.** Every OFF is retried, with growing gaps, until it gets through.
  - **Pump reports.** Every 30 s the app reads each pump's own report of its heads. An OFF only counts once the pump confirms it, and any head running that the app didn't start is turned off and logged (a lost OFF, a pump that reconnected, the phone app, or left over from before the app started, which is also checked at start-up).
  - **Forgotten heads.** A head left on with nothing in the app running it is turned off.
  - **Red bar.** While a head isn't confirmed off, a red bar names it, with an "I've switched it off" button. Closing or updating then asks first.
- **On-time limit.**
  - By default a head may run at most **30 min in any 60 min**. Change it in **Settings → Pump safety**, or give a head its own limit or none in the **Rack** tab.
  - A dose or sequence step that reaches the limit turns the head off to rest, then carries on, so the full volume still goes in, just later. The tile shows "Resting · back in m:ss".
  - Calibration skips a head without enough on-time left, and says when to redo it.
- **ON that won't go through.** If a dose can't turn its head on after three tries, the step stops with an error instead of counting down as if it were pumping.
- **Emergency Stop** also refuses any ON for 15 s, and cancels Async steps and manual doses.

## [2.4.0] - 2026-10-26

### Added
- **Pump Wi-Fi watch.** Every 30 s the app asks the Jebao cloud which pumps are online.
  - The tiles update without pressing Refresh, and a **Pumps: 9/10 online** count sits in the bottom-right corner (orange when any are offline).
  - Each drop and reconnection goes in the System Log, with how long the pump was gone.
- **Connection report.** Click the count, or go to **Settings → Pump Connections**.
  - For each pump: whether it's online and since when, drops in the last 24 h and 7 days, time offline, the longest outage, when it was last seen online, and its MAC address (for router address reservations).
  - How quickly the cloud has been answering, and **Copy report** to paste the table elsewhere.
  - Tips for travel-router settings.
  - The history is kept on each computer in the log folder (`connection-<computer>.jsonl`), for as long as the System Log.
- If this computer itself can't reach the cloud, the app says so once, and again when it's back, so a PC or internet problem isn't mistaken for the pumps dropping.

### Changed
- When the cloud session runs out (the app left open for days), the app logs in again by itself instead of failing the pump checks.

## [2.3.1] - 2026-10-21

### Changed
- **Calibration results table:**
  - Columns now get sensible widths, with units in the headers (Rate mL/min, Change vs old, R², Steady, Delays start / stop, Old mL/min).
  - The Result column takes the spare room and shows why a head failed, or how late the pump started or stopped.
  - Accept and Redo are small buttons instead of one stretched across the row.
  - The new **Change vs old** column shows how far the new rate is from the one saved before.
- On narrower windows the live chart sits above the results instead of beside them, so the table gets the full width.
- The head being calibrated says what it is doing in the results table: settling, waiting for a still reading, turning the pump on, or running.

### Fixed
- While the cloud was still confirming ON, the live view said "Running · 0.00 mL" with an empty chart. It now says "Turning the pump on…".

## [2.3.0] - 2026-10-21

### Changed
- **Automatic calibration now saves the flow rate from the fitted line (the slope).** It used to save the Δmass rate (mass delivered ÷ commanded on-time).
  - Real runs showed the pumps starting up to 11 s late and stopping up to 19 s late after the app sent ON and OFF. These delays changed from one command to the next, so the Δmass rate of one head moved 16 % between tries while its slope moved 0.7 %.
  - The slope is the pump's own rate; the delays come from the cloud, not the pump.
- **A head passes when the line fits well (R² ≥ 0.99) and the flow was steady.** Steady means lines through the first and second half of the run agree within the tolerance (2 % by default).
  - Before, a head had to have its slope and Δmass rate agree, which mostly measured cloud delays.
  - The tolerance setting is now called **Steady within**. A saved value carries over.
- **The ignore margins are counted from when the flow really started and stopped**, not from when ON and OFF were sent, so a late start or stop can't put still readings into the fit.
- **Before and after each head, the routine waits until the balance reading is still**, not just for the settle time. A pump that stops late, or a drip, can no longer spill into the next head or the end mass.
- **The results table shows** Rate, R², Steady and **Delays**, the start/stop delay of each head; long delays are shown in amber, with a tooltip.
- **The CSV adds** the delays, the steadiness and the stretch used for the fit. The Δmass rate stays in it, for the record.

### Fixed
- The calibration chart's time axis started at "0 s" although it begins with the settle before ON. It now shows the real times, with dotted lines where ON and OFF were sent.

## [2.2.0] - 2026-10-20

### Added
- **Hide** button on the System Log: shrinks it to one line showing the latest message, to give the tabs more room. **Show** opens it again, and the app remembers your choice.

### Changed
- Calibration tab: the rack, the live chart and the results scroll together in one area, so each gets its full height, instead of being squeezed into a split. The rack's name, the quick-select buttons and the tile size stay at the top.

## [2.1.0] - 2026-10-20

A new Calibration tab that works like the Manual tab, with automatic calibration using a balance.

### Added
- **Automatic calibration with a balance.**
  - Select heads on the rack (or press **Uncalibrated** or **All**), put their outlets in one beaker on the balance, and press **Calibrate**.
  - Each head runs on its own until about the chosen volume has arrived, with a settle before and after.
  - The app compares the **slope rate** (a straight line through the readings, leaving out the start and end) with the **Δmass rate** (mass delivered ÷ density ÷ on-time). A head passes when they agree within 2 % and R² ≥ 0.99, and its Δmass rate is saved. Every limit is adjustable.
  - Heads that miss are redone (twice by default), then left for you to **Accept** or **Redo**.
- **Live view.** A chart of mass against time with the fitted line, the current rates, time left, and a results table. You can pause after the current head or stop.
- **Balances.** Mettler Toledo (MT-SICS, asked for each reading with `SI`) and Bonvoisin (continuous output), at 9600 baud, 8N1. The port list refreshes when you plug the balance in. Needs the new `pyserial` dependency, which an update installs.
- **A record of every run.** Saved in the data folder under `calibrations/`: one row per try, plus the raw balance readings.
- **Safety stops.** A head with no flow is marked and skipped. A pump that won't start or stop, or a balance that goes quiet, stops the run. Emergency Stop, or Cancel on that head in the Manual tab, stops it too. Queued sequences wait until the calibration has finished.
- **Asks before closing.** Closing the app while a sequence, dose or calibration is running now asks first, and stops every pump if you go ahead.

### Changed
- **Manual (timed run) calibration** now uses the same head selection.
  - Run any heads, on any pumps, for a set time, then enter what each delivered (mL, or grams of water, ethanol, methanol or isopropanol) and save.
  - Tubing sizes are no longer asked for; saved values are kept.
  - Water's density is 0.997 g/mL (it was 1.000).
- **Tile size** is shared by the Manual and Calibration tabs.
- A head being calibrated shows as running on the Manual tab. **Cancel** there stops the calibration; **Pause** doesn't apply to it.

## [2.0.1] - 2026-10-20

### Fixed
- Updating could sit on "Updating…" for up to 15 minutes with no sign of progress, mostly on Windows, where the app runs without a console. The dependency install now runs under the normal `python.exe` and never waits for input. It also shows what it's doing as it goes, along with how long it has been running, and gives up after 10 minutes (downloading after 2). Anything that stops it puts the app back on the old version.

### Added
- A **Cancel** button while updating. It stops the update and leaves the app exactly as it was.

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

[Unreleased]: https://github.com/brandonbtm29/Jebao-Pump-Sequencer/compare/v2.5.0...HEAD
[2.5.0]: https://github.com/brandonbtm29/Jebao-Pump-Sequencer/compare/v2.4.0...v2.5.0
[2.4.0]: https://github.com/brandonbtm29/Jebao-Pump-Sequencer/compare/v2.3.1...v2.4.0
[2.3.1]: https://github.com/brandonbtm29/Jebao-Pump-Sequencer/compare/v2.3.0...v2.3.1
[2.3.0]: https://github.com/brandonbtm29/Jebao-Pump-Sequencer/compare/v2.2.0...v2.3.0
[2.2.0]: https://github.com/brandonbtm29/Jebao-Pump-Sequencer/compare/v2.1.0...v2.2.0
[2.1.0]: https://github.com/brandonbtm29/Jebao-Pump-Sequencer/compare/v2.0.1...v2.1.0
[2.0.1]: https://github.com/brandonbtm29/Jebao-Pump-Sequencer/compare/v2.0.0...v2.0.1
[2.0.0]: https://github.com/brandonbtm29/Jebao-Pump-Sequencer/compare/v1.4.0...v2.0.0
[1.4.0]: https://github.com/brandonbtm29/Jebao-Pump-Sequencer/compare/v1.3.0...v1.4.0
[1.3.0]: https://github.com/brandonbtm29/Jebao-Pump-Sequencer/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/brandonbtm29/Jebao-Pump-Sequencer/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/brandonbtm29/Jebao-Pump-Sequencer/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/brandonbtm29/Jebao-Pump-Sequencer/releases/tag/v1.0.0
