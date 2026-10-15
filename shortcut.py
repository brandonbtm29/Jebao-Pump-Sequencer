"""Put a "Jebao Pump Sequencer" shortcut on the desktop.

* Windows: a .lnk shortcut that starts the app with pythonw.exe (no console window)
  and shows the app icon. Made with Windows' own WScript.Shell through PowerShell.
* macOS: a small "Jebao Pump Sequencer.app" that starts the app with this copy's
  Python. It can be dragged to the Dock or Applications.
* Linux: a .desktop launcher.

The shortcut points at this folder and its Python, so it keeps working after updates.
Creating it again replaces the old one. No Qt here, so it can be tested on its own.
"""

import os
import plistlib
import shlex
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(ROOT, "assets")
APP_NAME = "Jebao Pump Sequencer"
BUNDLE_ID = "io.github.brandonbtm29.jebao-pump-sequencer"
DESCRIPTION = "Run doses and sequences on Jebao dosing pumps"
ENTRY_SCRIPT = os.path.join(ROOT, "jebao_gui.py")


class ShortcutError(Exception):
    """A problem with a plain-language message for the user."""


def launch_command(platform=sys.platform, python=None):
    """(program, arguments) that start this copy of the app without a terminal window."""
    if getattr(sys, "frozen", False):  # a built app: the executable is the app
        return sys.executable, []
    python = python or sys.executable
    if platform == "win32":
        pythonw = os.path.join(os.path.dirname(python), "pythonw.exe")
        if os.path.exists(pythonw):
            python = pythonw
    return python, [ENTRY_SCRIPT]


def desktop_folder(platform=sys.platform):
    """The user's desktop, including a desktop moved into OneDrive on Windows."""
    if platform == "win32":
        result = _powershell("[Environment]::GetFolderPath('Desktop')")
        path = result.stdout.strip()
        if result.returncode == 0 and path:
            return path
    path = os.path.join(os.path.expanduser("~"), "Desktop")
    if not os.path.isdir(path):
        raise ShortcutError(f"Couldn't find your desktop folder ({path}).")
    return path


def create_shortcut(desktop=None, platform=sys.platform, version=""):
    """Create (or replace) the desktop shortcut and return its path."""
    desktop = desktop or desktop_folder(platform)
    if platform == "win32":
        return _create_windows(desktop)
    if platform == "darwin":
        return _create_mac(desktop, version)
    return _create_linux(desktop)


# --- Windows ------------------------------------------------------------------------------


def _powershell(script, env=None):
    kwargs = {"creationflags": 0x08000000} if os.name == "nt" else {}  # no console window
    return subprocess.run(  # noqa: S603 - fixed script; values are passed in environment variables
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, **(env or {})},
        **kwargs,
    )


# Values go in through environment variables, so paths with spaces or quotes are safe.
LNK_SCRIPT = """
$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:JPS_LINK)
$s.TargetPath = $env:JPS_TARGET
$s.Arguments = $env:JPS_ARGS
$s.WorkingDirectory = $env:JPS_WORKDIR
$s.IconLocation = $env:JPS_ICON
$s.Description = $env:JPS_DESCRIPTION
$s.Save()
"""


def _create_windows(desktop, powershell=None):
    program, args = launch_command("win32")
    link = os.path.join(desktop, APP_NAME + ".lnk")
    icon = os.path.join(ASSETS, "jebao.ico")
    if not os.path.exists(icon):
        icon = program
    result = (powershell or _powershell)(
        LNK_SCRIPT,
        env={
            "JPS_LINK": link,
            "JPS_TARGET": program,
            "JPS_ARGS": subprocess.list2cmdline(args),
            "JPS_WORKDIR": ROOT,
            "JPS_ICON": icon + ",0",
            "JPS_DESCRIPTION": DESCRIPTION,
        },
    )
    if result.returncode != 0 or not os.path.exists(link):
        raise ShortcutError(
            "Windows couldn't create the shortcut:\n" + (result.stderr or result.stdout).strip()
        )
    return link


# --- macOS --------------------------------------------------------------------------------


def _create_mac(desktop, version=""):
    program, args = launch_command("darwin")
    app = os.path.join(desktop, APP_NAME + ".app")
    if os.path.exists(app):
        shutil.rmtree(app)
    macos = os.path.join(app, "Contents", "MacOS")
    resources = os.path.join(app, "Contents", "Resources")
    os.makedirs(macos)
    os.makedirs(resources)

    launcher = os.path.join(macos, "launch")
    command = " ".join(shlex.quote(part) for part in [program, *args])
    with open(launcher, "w", encoding="utf-8", newline="\n") as f:
        f.write(
            "#!/bin/bash\n"
            '# Made by the Jebao Pump Sequencer\'s "Create Desktop Shortcut" button.\n'
            f"cd {shlex.quote(ROOT)} || exit 1\n"
            f'exec {command} "$@"\n'
        )
    os.chmod(launcher, 0o755)  # noqa: S103 - a launcher must be executable

    info = {
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleExecutable": "launch",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": version or "1.0",
        "LSMinimumSystemVersion": "11.0",
        "NSHighResolutionCapable": True,
    }
    icns = os.path.join(ASSETS, "jebao.icns")
    if os.path.exists(icns):
        shutil.copyfile(icns, os.path.join(resources, "AppIcon.icns"))
        info["CFBundleIconFile"] = "AppIcon"
    with open(os.path.join(app, "Contents", "Info.plist"), "wb") as f:
        plistlib.dump(info, f)
    os.utime(app)  # nudge Finder to pick up the new icon
    return app


# --- Linux --------------------------------------------------------------------------------


def _create_linux(desktop):
    program, args = launch_command("linux")
    path = os.path.join(desktop, "jebao-pump-sequencer.desktop")
    command = " ".join(shlex.quote(part) for part in [program, *args])
    with open(path, "w", encoding="utf-8") as f:
        f.write(
            "[Desktop Entry]\n"
            "Type=Application\n"
            f"Name={APP_NAME}\n"
            f"Comment={DESCRIPTION}\n"
            f"Exec={command}\n"
            f"Path={ROOT}\n"
            f"Icon={os.path.join(ASSETS, 'jebao.png')}\n"
            "Terminal=false\n"
            "Categories=Science;Utility;\n"
        )
    os.chmod(path, 0o755)  # noqa: S103 - a launcher must be executable
    return path
