# Changelog

All notable changes to this project are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/), and the project uses [Semantic Versioning](https://semver.org/): new features bump the middle number (1.1.0 → 1.2.0), fixes bump the last (1.1.0 → 1.1.1), and changes that break existing settings or saved sequences bump the first.

## [Unreleased]

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

[Unreleased]: https://github.com/brandonbtm29/Jebao-Pump-Sequencer/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/brandonbtm29/Jebao-Pump-Sequencer/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/brandonbtm29/Jebao-Pump-Sequencer/releases/tag/v1.0.0
