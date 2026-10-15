"""Test setup: run Qt headless and keep the app away from real data and settings.

jebao_gui changes into its data folder and reads its settings folder when it is
imported, so both are pointed at a throwaway folder before any test imports it.
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

_TMP = Path(tempfile.mkdtemp(prefix="jebao-tests-"))
os.environ["QT_QPA_PLATFORM"] = "offscreen"
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
    win.close()
