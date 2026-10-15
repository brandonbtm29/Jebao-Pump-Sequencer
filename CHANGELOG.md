# Changelog

All notable changes to this project are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/), and the project uses [Semantic Versioning](https://semver.org/). Below 1.0, new features bump the middle number (0.1.0 → 0.2.0) and fixes bump the last (0.1.0 → 0.1.1).

## [Unreleased]

## [0.1.0] - 2026-10-15

First tracked release. Earlier versions were co-developed with Google Gemini in Antigravity; development continues with Claude (Anthropic).

### Added
- Manual dosing by volume or time, with per-channel pause and cancel.
- Calibration by collected volume or mass (water, ethanol, methanol or custom density), saved with tubing size.
- Sequence editor with Dose, Staccato, Pause, Pause for user review, Wait for all and Run sub-sequence steps, running each channel side by side.
- Saved sequences and a queue that runs them one after another.
- Global emergency stop for every channel on every pump.
- Dark and light themes and three text sizes.
- Settings Folder option, so your login and pump settings can live in a synced folder and stay the same on every computer.
- Double-click launchers for Windows and macOS.
- Example settings files in `examples/`.

### Changed
- Controls the pumps through the Jebao (Gizwits) cloud instead of the local network. The pumps stop answering local commands once they are on Wi-Fi.
- Installs from `pyproject.toml` (`pip install -e .`) and needs Python 3.11 or newer. `requirements.txt` is gone.

### Known issues
- Cloud requests have no timeout, so a stalled connection can hold up a dose or the emergency stop.
- The emergency stop reports "Sent STOP" even if some commands failed. Check the log.
- A sequence step whose channel can't be read silently falls back to channel 1.

[Unreleased]: https://github.com/brandonbtm29/Jebao-Pump-Sequencer/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/brandonbtm29/Jebao-Pump-Sequencer/releases/tag/v0.1.0
