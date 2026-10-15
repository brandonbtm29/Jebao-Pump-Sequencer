# Jebao Pump Sequencer

A desktop app for running Jebao MD-4.4 / Doser 2.4 four-channel dosing pumps from a computer: manual doses, per-channel calibration, multi-step sequences across several pumps, and one big emergency stop.

![Sequencer tab with a six-step sequence across three pumps](docs/screenshot-sequencer.png)

## Why

Jebao dosing pumps are cheap and reliable, but their phone app is built for aquariums: one pump at a time, a few doses a day. In the lab we use several of them to push solvents and samples through solid-phase extraction (SPE) cartridges, which needs exact volumes, steps in order, pauses for a person to check things, and a way to stop everything at once. This app does that on Windows and macOS.

## Features

- **Manual control**: dose any channel by volume (mL) or by time (s), pause it, or cancel it.
- **Calibration**: run a channel for 60 seconds, type in the volume or mass you collected (water, ethanol, methanol or a custom density), and the app saves that channel's flow rate together with the tubing size.
- **Sequences**: build a table of steps (Dose, Staccato, Pause, Pause for user review, Wait for all, Run sub-sequence) across any of your pumps, save it under a name, and reuse it.
- **Channels run side by side**: each channel works through its own steps at the same time as the others. "Wait for All" lines them up again, and async doses run in the background.
- **Queue**: line up several saved sequences and they run one after another.
- **User review steps**: a sequence can stop and wait until you click **Approve** for that channel.
- **Emergency stop**: one button cancels the running sequence, clears the queue and turns off all four channels on every pump.
- **Shared settings**: keep your login, calibrations and sequences in a synced folder so every computer uses the same ones.

![Manual Control tab](docs/screenshot-manual.png)

## Requirements

- Python 3.11 or newer
- Windows 10/11 or macOS (Linux works for development)
- Pumps already connected to Wi-Fi and added to a Jebao account in the **Jebao Aqua** phone app
- An internet connection: the app controls the pumps through the Jebao cloud, not over your local network

## Install

1. Get the code:
   ```
   git clone https://github.com/brandonbtm29/Jebao-Pump-Sequencer.git
   cd Jebao-Pump-Sequencer
   ```
2. Create a virtual environment and install the app.

   **Windows** (PowerShell or Command Prompt):
   ```
   python -m venv .venv
   .venv\Scripts\pip install -e .
   ```
   **macOS**:
   ```
   python3 -m venv .venv
   .venv/bin/pip install -e .
   ```
3. Start it: double-click `Launch_Windows.bat` or `Launch_Mac.command`. You can also run `python jebao_gui.py`, or the `jebao-pump` command that the install adds to the virtual environment.

## First run

1. Click **Settings** and enter the email and password of your Jebao account. Choose a theme and text size if you like.
2. The app logs in and lists your pumps under **Connected Pumps**. It uses the names you gave them in the phone app.
3. Calibrate each channel you plan to use (**Calibration** tab). Until you do, the app assumes 1 mL/min, so volume doses will be wrong.
4. Try a small manual dose, then build a sequence.

## Configuration

The app keeps two settings files:

| File | What's in it | Example |
|---|---|---|
| `secrets.json` | Your Jebao login | [`examples/secrets.example.json`](examples/secrets.example.json) |
| `jebao_config.json` | Theme, pump order, calibrations, tubing sizes, last volumes, the current sequence and saved sequences | [`examples/jebao_config.example.json`](examples/jebao_config.example.json) |

You don't need to write either file yourself. The app creates them when you save settings.

- **Where they live:** by default in the app folder. Both are listed in `.gitignore`, so they never get committed. **Settings → Settings Folder** can move them. A folder inside a synced drive (SyncThing, OneDrive…) shares them between computers. The choice is stored in `settings_location.json`, and the `UAKRON_SETTINGS_DIR` environment variable overrides it.
- **Data folder:** the app runs inside `~/SyncThing/UAkron Playground/Jebao Pump` and writes `crash_log.txt` there if it crashes. Set `UAKRON_APP_DATA_DIR` to use a different folder. The app creates the folder if it is missing. The event log is shown in the window but not saved to a file.

## How it works

```
 Jebao Pump Sequencer ──HTTPS──▶ Gizwits cloud (usapi.gizwits.com) ──▶ pumps on Wi-Fi
```

Once a Jebao pump is on Wi-Fi it only takes commands from the cloud service behind the Jebao app (Gizwits). The app logs in with your account, lists the pumps bound to it, and turns channels on and off with commands like `{"attrs": {"channe1": true}}`. "channe" is the API's own spelling.

A dose of *V* mL turns the channel on, waits *V ÷ flow rate* minutes, then turns it off. Flow rates come from calibration. Network calls run in the background (PyQt6 with an asyncio loop through `qasync`), so the window stays responsive.

## Limitations

- **Accuracy depends on calibration and the network.** Doses are timed on/off commands, so a slow cloud round trip makes a dose slightly longer or shorter. Recalibrate when you change tubing.
- **If the app or the internet stops mid-dose, the pump can keep running**, because the off command never arrives. Stop it with the pump's own button or the phone app.
- **No local control.** If the Jebao cloud is down, the app can't reach the pumps.
- **Adding new pumps to Wi-Fi** still needs the phone app. See [`docs/provisioning-research.md`](docs/provisioning-research.md) for the plan to automate it.
- Changes and fixes for each version are listed in the [CHANGELOG](CHANGELOG.md).

## Development

```
pip install -e ".[dev]"
ruff check .
ruff format --check .
pytest
```
The tests run headless (`QT_QPA_PLATFORM=offscreen` is set for you) and never touch the network or your real settings. CI runs the same checks on Ubuntu and Windows with Python 3.11–3.13, plus a secret scan of the whole history.

The version number lives in one place, `__version__` in `jebao_gui.py`. To make a release, bump it, move the "Unreleased" notes in `CHANGELOG.md` under the new version, then tag `vX.Y.Z` and create a GitHub release from those notes.

`settings_location.py` is shared, unchanged, by several of my lab apps, so ruff leaves it alone.

## How this was built

Jebao Pump Sequencer is written and maintained by Brandon T. McReynolds with the help of AI coding assistants. Versions before October 2026 were co-developed with Google Gemini in Antigravity. Later versions were co-developed with Claude (Anthropic), and those commits list Claude as a co-author. Brandon directs the work, reviews and tests the changes, and is responsible for the project.

Not affiliated with Jebao or Gizwits.

## License

MIT. See [LICENSE](LICENSE).
