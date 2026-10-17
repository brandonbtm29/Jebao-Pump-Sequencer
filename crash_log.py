"""Write errors to a log file, so a crash during an experiment leaves a trace.

Shared by the UAkron lab apps; keep every copy byte-identical (like settings_location.py).
Call ``setup()`` once at start-up, as early as possible. Standard library only; the Qt
dialog is imported lazily and only if a QApplication is running.

What it does:
- Log messages (``logging``) go to ``<log_dir>/<name>.log``, kept small: three files of 1 MB.
- An error nobody caught is logged with its traceback. In a Qt app it is also shown in a
  dialog, and the app keeps running instead of closing (PyQt6 otherwise ends the program
  when an error escapes a button handler or timer, which would stop a recording).
- Errors in background threads (serial readers, pump workers) are logged too.
- Hard crashes (a segfault in a driver or Qt) write a low-level traceback to
  ``<name>.fatal.log`` in the same folder.
"""

import faulthandler
import logging
import logging.handlers
import os
import platform
import sys
import threading
import time

_MARK = "_uakron_crash_log"  # tags our handlers so setup() can be called twice
_state = {"path": None, "fatal_file": None}


def user_log_dir(app_folder_name):
    """A per-user folder for logs when the app has no data folder of its own."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser(os.path.join("~", "AppData", "Local"))
        return os.path.join(base, app_folder_name, "logs")
    if sys.platform == "darwin":
        return os.path.expanduser(os.path.join("~", "Library", "Logs", app_folder_name))
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser(os.path.join("~", ".local", "state"))
    return os.path.join(base, app_folder_name, "logs")


def log_path():
    """Path of the log file, or None before setup()."""
    return _state["path"]


def setup(name, log_dir, version="", *, qt_dialog=True, extra_loggers=(), level=logging.INFO):
    """Start logging to ``<log_dir>/<name>.log`` and catch uncaught errors. Returns the log path.

    ``extra_loggers`` names loggers that don't pass messages up to the root logger but whose
    errors should be in the file too (e.g. "streamlit.error_util"). Never raises: if the
    folder can't be written, logging just goes to the console.
    """
    if _state["path"]:
        return _state["path"]
    root = logging.getLogger()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s [%(threadName)s]: %(message)s")
    path = os.path.join(log_dir, f"{name}.log")
    try:
        os.makedirs(log_dir, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=1_000_000, backupCount=2, encoding="utf-8"
        )
    except OSError as e:
        handler, path = None, None
        print(f"Couldn't open the log file in {log_dir}: {e}", file=sys.stderr)
    targets = [root] + [logging.getLogger(n) for n in extra_loggers]
    if handler is not None:
        setattr(handler, _MARK, True)
        handler.setFormatter(fmt)
        for logger in targets:
            logger.addHandler(handler)
    has_console = any(type(h) is logging.StreamHandler for h in root.handlers)
    if sys.stderr is not None and not has_console:
        console = logging.StreamHandler()  # pythonw (Windows launcher/shortcut) has no console
        setattr(console, _MARK, True)
        console.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        root.addHandler(console)
    if root.level == logging.NOTSET or root.level > level:
        root.setLevel(level)

    if path:
        try:
            fatal = open(os.path.join(log_dir, f"{name}.fatal.log"), "a", encoding="utf-8")
            fatal.write(f"--- {name} {version} started {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
            fatal.flush()
            faulthandler.enable(fatal)
            _state["fatal_file"] = fatal  # must stay open for the life of the app
        except (OSError, ValueError, RuntimeError):
            pass

    _install_hooks(name, qt_dialog)
    _state["path"] = path
    logging.getLogger(name).info(
        "%s %s starting (Python %s, %s)", name, version or "", platform.python_version(), platform.platform()
    )
    return path


def _install_hooks(name, qt_dialog):
    log = logging.getLogger(name)
    last_shown = [0.0]

    def excepthook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        log.critical("Unexpected error", exc_info=(exc_type, exc, tb))
        if qt_dialog:
            if time.monotonic() - last_shown[0] < 60:  # an error in a timer would repeat every tick
                return
            last_shown[0] = time.monotonic()
            _show_qt_dialog(exc)

    def thread_hook(args):
        if args.exc_type is SystemExit:
            return
        thread = args.thread.name if args.thread is not None else "?"
        log.error(
            "Unexpected error in background thread %s",
            thread,
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = excepthook
    threading.excepthook = thread_hook


def _show_qt_dialog(exc):
    try:
        from PyQt6.QtWidgets import QApplication, QMessageBox

        if QApplication.instance() is None:
            return
        where = f"\n\nDetails were saved to {_state['path']}." if _state["path"] else ""
        QMessageBox.warning(None, "Unexpected error", f"Something went wrong: {exc}{where}")
    except Exception:  # noqa: S110 - the error handler itself must never crash the app
        pass


def _reset_for_tests():
    """Undo setup() so tests can call it again."""
    for logger in [logging.getLogger(), *logging.Logger.manager.loggerDict.values()]:
        if isinstance(logger, logging.Logger):
            for h in list(logger.handlers):
                if getattr(h, _MARK, False):
                    logger.removeHandler(h)
                    h.close()
    if _state["fatal_file"] is not None:
        faulthandler.disable()
        _state["fatal_file"].close()
    _state.update(path=None, fatal_file=None)
    sys.excepthook = sys.__excepthook__
    threading.excepthook = threading.__excepthook__
