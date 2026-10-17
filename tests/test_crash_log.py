"""Tests for crash_log.py (shared by the UAkron lab apps; keep the copies identical)."""

import logging
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    import crash_log
except ImportError:  # packaged apps keep it inside their package
    from porometry import crash_log


@pytest.fixture(autouse=True)
def clean():
    crash_log._reset_for_tests()
    yield
    crash_log._reset_for_tests()


def read(path):
    for h in logging.getLogger().handlers:
        h.flush()
    with open(path, encoding="utf-8") as f:
        return f.read()


def test_setup_writes_start_line(tmp_path):
    path = crash_log.setup("demo_app", str(tmp_path / "logs"), "1.2.3", qt_dialog=False)
    assert path == str(tmp_path / "logs" / "demo_app.log") == crash_log.log_path()
    assert "demo_app 1.2.3 starting" in read(path)
    assert os.path.exists(tmp_path / "logs" / "demo_app.fatal.log")


def test_setup_twice_adds_no_duplicate_handlers(tmp_path):
    crash_log.setup("demo_app", str(tmp_path), qt_dialog=False)
    count = len(logging.getLogger().handlers)
    assert crash_log.setup("demo_app", str(tmp_path), qt_dialog=False) == str(tmp_path / "demo_app.log")
    assert len(logging.getLogger().handlers) == count


def test_uncaught_error_is_logged_and_app_keeps_going(tmp_path):
    path = crash_log.setup("demo_app", str(tmp_path), qt_dialog=False)
    try:
        raise ValueError("pump stopped answering")
    except ValueError:
        sys.excepthook(*sys.exc_info())  # what Python/PyQt would call; must not raise
    text = read(path)
    assert "Unexpected error" in text and "ValueError: pump stopped answering" in text


def test_background_thread_error_is_logged(tmp_path):
    path = crash_log.setup("demo_app", str(tmp_path), qt_dialog=False)

    def work():
        raise RuntimeError("serial port vanished")

    t = threading.Thread(target=work, name="SerialReader")
    t.start()
    t.join()
    text = read(path)
    assert "background thread SerialReader" in text and "RuntimeError: serial port vanished" in text


def test_extra_loggers_reach_the_file(tmp_path):
    other = logging.getLogger("demo.not_propagating")
    other.propagate = False
    path = crash_log.setup("demo_app", str(tmp_path), qt_dialog=False, extra_loggers=["demo.not_propagating"])
    other.error("Uncaught app execution")
    assert "Uncaught app execution" in read(path)


def test_unwritable_folder_does_not_raise(tmp_path):
    blocker = tmp_path / "a_file"
    blocker.write_text("x")
    assert crash_log.setup("demo_app", str(blocker / "logs"), qt_dialog=False) is None


def test_user_log_dir_names_the_app():
    assert "Demo App" in crash_log.user_log_dir("Demo App")
