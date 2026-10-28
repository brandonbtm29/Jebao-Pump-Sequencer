"""Test setup: run Qt headless and keep the app away from real data and settings.

jebao_gui changes into its data folder and reads its settings folder when it is
imported, so both are pointed at a throwaway folder before any test imports it.
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest


def _offscreen_platform(config_dir, run_dir):
    """Qt's headless platform, with a 1920x1080 screen instead of its default 800x800.

    The windows' minimum size is bigger than 800x800, so on the default screen Qt shrinks a
    restored window and its dividers can't be dragged, which made the window-size tests fail
    on Windows CI. Qt splits the platform string on ':', so the path goes without its drive
    letter; that only works on the drive the tests run from, otherwise the default screen is used.
    """
    config = Path(config_dir) / "offscreen_screen.json"
    if not config.exists():
        config.write_text(
            '{"screens": [{"name": "Test screen", "x": 0, "y": 0, "width": 1920, "height": 1080,'
            ' "logicalDpi": 96, "logicalBaseDpi": 96, "dpr": 1}]}',
            encoding="utf-8",
        )
    drive, path = os.path.splitdrive(str(config))
    if drive.lower() != os.path.splitdrive(str(run_dir))[0].lower() or ":" in path:
        return "offscreen"
    return "offscreen:configfile=" + path.replace("\\", "/")


_TMP = Path(tempfile.mkdtemp(prefix="jebao-tests-"))
# The screen config sits in tests/ so it is on the same drive as the run (CI: D:, temp: C:).
os.environ["QT_QPA_PLATFORM"] = _offscreen_platform(Path(__file__).parent, os.getcwd())
os.environ["UAKRON_APP_DATA_DIR"] = str(_TMP / "data")
os.environ["UAKRON_SETTINGS_DIR"] = str(_TMP / "settings")

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

_cwd = os.getcwd()
import jebao_gui  # noqa: E402  (must come after the environment is set)

os.chdir(_cwd)


@pytest.fixture(scope="session")
def qt_loop():
    """A QApplication with the same qasync event loop the app uses."""
    import asyncio

    import qasync
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)
    yield app, loop


@pytest.fixture
def window(qt_loop, monkeypatch, tmp_path):
    """The main window, with no saved login, and with the network blocked."""
    monkeypatch.setattr(
        jebao_gui, "APP_DIR", str(tmp_path)
    )  # where per-copy files like the shortcut record go

    def no_network(*args, **kwargs):
        raise AssertionError("tests must not reach the network")

    monkeypatch.setattr(jebao_gui.requests, "post", no_network)
    monkeypatch.setattr(jebao_gui.requests, "get", no_network)
    handlers_before = list(jebao_gui.logger.handlers)
    win = jebao_gui.JebaoPumpGUI()
    yield win
    # Each window attaches a log handler that writes into its own log box.
    # The app only ever has one window; here, detach it so later tests don't
    # log into a widget that Qt has already deleted.
    for handler in jebao_gui.logger.handlers[:]:
        if handler not in handlers_before:
            jebao_gui.logger.removeHandler(handler)
    win.guard.close()  # no OFF retries left running into the next test
    win._closing = True  # no "pumps are running" question when a test leaves one going
    win.close()
