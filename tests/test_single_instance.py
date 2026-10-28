"""single_instance: a second launch finds the first, asks it to come forward, and quits."""

import os
import subprocess
import sys
import textwrap
import uuid

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt6.QtNetwork")

import single_instance  # noqa: E402  (each repo: adjust this import to where the module lives)

QUITTER = textwrap.dedent(
    """
    import sys
    sys.path.insert(0, sys.argv[1])
    from PyQt6.QtCore import QTimer
    from PyQt6.QtWidgets import QApplication
    import single_instance

    app = QApplication([])
    guard = single_instance.acquire(sys.argv[2])
    print("LOCKED" if guard and guard.active else "NOLOCK", flush=True)
    QTimer.singleShot(0, app.quit)
    app.exec()
    print("RELEASED" if not guard.active else "STILL HELD", flush=True)
    """
)

HOLDER = textwrap.dedent(
    """
    import sys
    sys.path.insert(0, sys.argv[1])
    from PyQt6.QtCore import QTimer
    from PyQt6.QtWidgets import QApplication, QWidget
    import single_instance

    app = QApplication([])
    guard = single_instance.acquire(sys.argv[2])
    print("LOCKED" if guard else "NOLOCK", flush=True)
    window = QWidget()
    guard.set_window(window)

    def shown():
        print("SHOWN", flush=True)
        app.quit()

    guard.bring_to_front = shown
    QTimer.singleShot(20000, app.quit)
    app.exec()
    """
)


@pytest.fixture
def qapp():
    from PyQt6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def name():
    return f"Single Instance Test {uuid.uuid4().hex[:8]}"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv(single_instance.ENV_OFF, raising=False)
    single_instance.release()
    yield
    single_instance.release()


def test_key_is_safe_and_stable():
    a = single_instance.key_for("Jebao Pump Sequencer")
    assert a == single_instance.key_for("Jebao Pump Sequencer")
    assert a != single_instance.key_for("Balance Streamer")
    assert all(c.isalnum() or c == "-" for c in a) and len(a) <= 40


def test_acquire_release_acquire(qapp, name):
    guard = single_instance.acquire(name)
    assert guard is not None and guard.active
    assert single_instance.acquire(name) is guard  # same process asks again: same guard
    single_instance.release()
    assert not guard.active
    again = single_instance.acquire(name)
    assert again is not None and again.active


def test_off_switch(qapp, name, monkeypatch):
    monkeypatch.setenv(single_instance.ENV_OFF, "1")
    guard = single_instance.acquire(name)
    assert guard is not None and not guard.active
    guard.release()  # harmless


def test_second_copy_brings_first_forward(qapp, name):
    folder = os.path.dirname(os.path.abspath(single_instance.__file__))
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
    env.pop(single_instance.ENV_OFF, None)
    holder = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", HOLDER, folder, name],
        stdout=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        assert holder.stdout.readline().strip() == "LOCKED"
        assert single_instance.acquire(name, wait_ms=200) is None
        assert holder.stdout.readline().strip() == "SHOWN"
        holder.wait(timeout=20)
    finally:
        holder.kill()
        holder.wait()
    # the first copy has quit (and given its lock back), so now this one can run
    assert not os.path.exists(lock_path(name))
    assert single_instance.acquire(name) is not None


def lock_path(name):
    from PyQt6.QtCore import QDir

    return os.path.join(QDir.tempPath(), single_instance.key_for(name) + ".lock")


def test_lock_given_back_on_normal_quit(qapp, name):
    folder = os.path.dirname(os.path.abspath(single_instance.__file__))
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
    env.pop(single_instance.ENV_OFF, None)
    out = subprocess.run(  # noqa: S603
        [sys.executable, "-c", QUITTER, folder, name],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert out.stdout.split() == ["LOCKED", "RELEASED"], out.stderr
    assert not os.path.exists(lock_path(name))
