"""Allow only one copy of the app at a time. Opening it again brings the running copy forward.

Shared by Brandon's apps; keep every copy byte-identical (like crash_log.py and
settings_location.py). Needs PyQt6, which is imported only when ``acquire()`` is called.

Why: two copies of an instrument app would fight over the same serial port or pump, and
two copies of any app would overwrite each other's settings.

Use it right after creating the QApplication, before building the main window::

    guard = single_instance.acquire("Balance Streamer")
    if guard is None:  # another copy is running and was asked to come to the front
        sys.exit(0)
    window = MainWindow()
    guard.set_window(window)

**Restarting** (Restart App, Update and Restart): release the lock *before* starting the new
copy, or the new copy finds the old one still running and quits::

    single_instance.release()
    subprocess.Popen([sys.executable] + sys.argv)
    QApplication.quit()

How it works: a lock file in the user's temp folder (one per app and user, so two clones of
the same app share it on purpose), plus a local socket the running copy listens on. The lock
is given back when the app quits normally. If the app crashes, the next start sees that the
process holding the lock is gone and takes over.
Setting the environment variable ``SINGLE_INSTANCE_OFF=1`` turns the check off (tests,
or a deliberate second copy).
"""

import atexit
import getpass
import hashlib
import logging
import os
import re
import sys

ENV_OFF = "SINGLE_INSTANCE_OFF"
log = logging.getLogger("single_instance")
_active = None  # the Guard holding the lock in this process, if any
_exit_hook = []  # set once the interpreter-exit fallback is registered


def key_for(app_name):
    """Name of the lock file and socket for this app and user: short, safe on every OS."""
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001 - no user name (odd environments): still works per app
        user = "user"
    slug = re.sub(r"[^A-Za-z0-9]+", "-", app_name).strip("-").lower()[:24] or "app"
    digest = hashlib.sha1(f"{app_name}|{user}".encode()).hexdigest()[:10]  # noqa: S324 - a name, not security
    return f"{slug}-{digest}"


class Guard:
    """Holds the lock while the app runs. Keep a reference to it (or use the module's release())."""

    def __init__(self, app_name, key, lock=None, server=None):
        self.app_name = app_name
        self.key = key
        self._lock = lock
        self._server = server
        self._window = None
        if server is not None:
            server.newConnection.connect(self._on_connection)

    @property
    def active(self):
        """True while this guard holds the lock (False when checking is off or failed)."""
        return self._lock is not None

    def set_window(self, window):
        """The window to bring forward when someone opens the app again."""
        self._window = window

    def release(self):
        """Give up the lock and stop listening, so a new copy can start (restart, update)."""
        global _active
        if self._server is not None:
            self._server.close()
            self._server = None
        if self._lock is not None:
            self._lock.unlock()
            self._lock = None
        if _active is self:
            _active = None

    def bring_to_front(self):
        window = self._window
        if window is None:
            return
        if window.isMinimized():
            window.showNormal()
        else:
            window.show()
        window.raise_()
        window.activateWindow()

    def _on_connection(self):
        while self._server is not None and self._server.hasPendingConnections():
            sock = self._server.nextPendingConnection()
            sock.disconnectFromServer()
            sock.deleteLater()
            log.info("Another launch asked this copy to come to the front")
            self.bring_to_front()


def release():
    """Release this process's lock, if it holds one. Safe to call any time."""
    if _active is not None:
        _active.release()


def _unlock_at_exit():
    """Last resort when Python exits without a normal quit: remove the lock file only.

    Touches no Qt objects (by then the QApplication may already be gone).
    """
    guard = _active
    if guard is not None and guard._lock is not None:
        guard._lock.unlock()
        guard._lock = None


def acquire(app_name, wait_ms=1000):
    """Take the lock for ``app_name``. Returns a Guard, or None if another copy is running.

    When another copy is running it is asked to come to the front; if it doesn't answer, a
    message says the app is already open. If the lock can't be created at all (unusual
    permissions), the app is allowed to start anyway and a warning is logged.
    """
    global _active
    key = key_for(app_name)
    if os.environ.get(ENV_OFF):
        return Guard(app_name, key)
    if _active is not None:
        return _active

    from PyQt6.QtCore import QCoreApplication, QDir, QLockFile
    from PyQt6.QtNetwork import QLocalServer

    lock = QLockFile(os.path.join(QDir.tempPath(), key + ".lock"))
    lock.setStaleLockTime(0)  # a long recording never makes the lock "stale"; a dead process does
    if not lock.tryLock(wait_ms):
        if lock.error() == QLockFile.LockError.LockFailedError:
            log.info("%s is already running", app_name)
            if not _ask_running_copy(key):
                _tell_user(app_name)
            return None
        log.warning("Couldn't create the single-instance lock (%s); starting anyway", lock.error())
        return Guard(app_name, key)

    app = QCoreApplication.instance()
    server = QLocalServer(app)  # owned by the app, so Qt deletes it with the app, never after
    QLocalServer.removeServer(key)  # a socket left behind by a crash; we hold the lock, so it's ours
    if not server.listen(key):
        log.warning("Couldn't listen for other launches: %s", server.errorString())
        server = None
    _active = Guard(app_name, key, lock, server)
    if app is not None:
        app.aboutToQuit.connect(release)  # give the lock back on a normal quit
    if not _exit_hook:
        atexit.register(_unlock_at_exit)
        _exit_hook.append(True)
    return _active


def _ask_running_copy(key, timeout_ms=1500):
    """Ask the running copy to show its window. True if it answered."""
    from PyQt6.QtNetwork import QLocalSocket

    if sys.platform == "win32":
        try:  # let the running copy take the foreground (Windows blocks that by default)
            import ctypes

            ctypes.windll.user32.AllowSetForegroundWindow(-1)  # ASFW_ANY
        except Exception:  # noqa: BLE001, S110 - only makes the window come forward more reliably
            pass
    sock = QLocalSocket()
    sock.connectToServer(key)
    if not sock.waitForConnected(timeout_ms):
        return False
    sock.write(b"show\n")
    sock.waitForBytesWritten(timeout_ms)
    sock.disconnectFromServer()
    return True


def _tell_user(app_name):
    if os.environ.get("QT_QPA_PLATFORM") == "offscreen":  # tests/CI: nobody could close a dialog
        return
    try:
        from PyQt6.QtWidgets import QApplication, QMessageBox

        if QApplication.instance() is not None:
            QMessageBox.information(
                None,
                app_name,
                f"{app_name} is already open on this computer.\n\n"
                "Look for its window (it may be minimized or on another desktop). "
                "Only one copy can run at a time.",
            )
    except Exception:  # noqa: BLE001, S110 - the message is a courtesy; never crash over it
        pass
