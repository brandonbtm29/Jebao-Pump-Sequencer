"""Check GitHub for a newer release and update a git copy of the app to it.

Checking asks GitHub's public API for the latest *release* of this repository (no
account needed, nothing about you is sent beyond a normal web request). It only
happens when you press "Check for Updates" in Settings.

Updating works for copies made with ``git clone``: it fetches the release tag and
moves this folder to it (fast-forward only, so local work is never overwritten).
If the release changes the dependencies (``pyproject.toml``), they are reinstalled;
if that fails, the folder is moved back. Unreleased work on ``main`` is never pulled
in. Copies downloaded as a ZIP can't update themselves; they get a link to the
release page instead.

No Qt here, so it can be tested on its own.
"""

import logging
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass

import requests

log = logging.getLogger("JebaoGUI.updater")

REPO = "brandonbtm29/Jebao-Pump-Sequencer"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"
ROOT = os.path.dirname(os.path.abspath(__file__))
_VERSION = re.compile(r"^v?(\d+)\.(\d+)(?:\.(\d+))?$")


def parse_version(text):
    """'v1.2.0' -> (1, 2, 0). Pre-releases ('1.2.0-rc1') and other tags give None."""
    m = _VERSION.match((text or "").strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)


def is_newer(candidate, current):
    a, b = parse_version(candidate), parse_version(current)
    return a is not None and b is not None and a > b


@dataclass
class Release:
    version: str  # "1.2.0"
    tag: str  # "v1.2.0"
    name: str
    notes: str
    url: str


def latest_release(current_version, timeout=10, get=requests.get):
    """The newest published release on GitHub. Raises LookupError with a plain message."""
    try:
        resp = get(
            API,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": f"JebaoPumpSequencer/{current_version}",
            },
            timeout=timeout,
        )
    except requests.exceptions.SSLError as e:
        raise LookupError(
            "Couldn't make a secure connection to GitHub (the certificate couldn't be checked). "
            "A network filter or an outdated certificate store can cause this."
        ) from e
    except requests.RequestException as e:
        raise LookupError("Couldn't reach GitHub. Check the internet connection.") from e
    if resp.status_code == 404:
        raise LookupError("No releases have been published yet.")
    if resp.status_code in (403, 429):
        raise LookupError("GitHub is limiting requests right now. Try again in an hour.")
    if resp.status_code != 200:
        raise LookupError(f"GitHub answered with an error ({resp.status_code}).")
    try:
        data = resp.json()
    except ValueError as e:
        raise LookupError("GitHub sent an answer that couldn't be read.") from e
    tag = str(data.get("tag_name") or "")
    version = parse_version(tag)
    if version is None:
        raise LookupError(f"The latest release tag {tag!r} isn't a version number.")
    return Release(
        version=".".join(map(str, version)),
        tag=tag,
        name=str(data.get("name") or tag),
        notes=str(data.get("body") or ""),
        url=str(data.get("html_url") or RELEASES_PAGE),
    )


# --- updating a git copy ---------------------------------------------------------------


def _run(cmd, cwd, timeout=120):
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW: no console pops up
    return subprocess.run(  # noqa: S603 - fixed commands, no shell
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        **kwargs,
    )


def _git(root, *args, timeout=120):
    return _run(["git", *args], root, timeout)


@dataclass
class Readiness:
    ok: bool
    reason: str = ""


def update_readiness(root=ROOT):
    """Can this copy update itself? If not, say why in plain words."""
    if getattr(sys, "frozen", False):
        return Readiness(False, "This is a standalone build. Download the new version from the release page.")
    if not os.path.isdir(os.path.join(root, ".git")):
        return Readiness(
            False,
            "This copy wasn't downloaded with git (for example it came from a ZIP file), so it "
            "can't update itself. Download the new version from the release page.",
        )
    if shutil.which("git") is None:
        return Readiness(False, "Git isn't installed on this computer, so the app can't update itself.")
    status = _git(root, "status", "--porcelain", "--untracked-files=no")
    if status.returncode != 0:
        return Readiness(False, "This folder's git information looks broken:\n" + status.stderr.strip()[:300])
    if status.stdout.strip():
        changed = ", ".join(line[3:] for line in status.stdout.splitlines()[:5])
        return Readiness(
            False,
            "Some of the app's own files were changed in this folder, so updating could overwrite "
            f"them ({changed}). Commit or undo those changes first.",
        )
    return Readiness(True)


@dataclass
class UpdateResult:
    ok: bool
    message: str


def default_install_command():
    return [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-e", "."]


def update_to(tag, root=ROOT, install_command=None, progress=None):
    """Move this git copy to release ``tag``; reinstall dependencies if they changed.

    Rolls back to the previous version if reinstalling fails.
    ``progress`` is called with short status messages.
    """
    say = progress or (lambda _msg: None)
    install_command = install_command or default_install_command()

    old = _git(root, "rev-parse", "HEAD")
    if old.returncode != 0:
        return UpdateResult(False, "Couldn't read this folder's current version:\n" + old.stderr.strip())
    old_head = old.stdout.strip()

    say("Downloading from GitHub…")
    fetch = _git(root, "fetch", "--tags", "--force", "origin", timeout=300)
    if fetch.returncode != 0:
        return UpdateResult(False, "Couldn't download the update:\n" + fetch.stderr.strip()[:500])
    if _git(root, "rev-parse", "--verify", "--quiet", f"refs/tags/{tag}^{{commit}}").returncode != 0:
        return UpdateResult(False, f"The release tag {tag} wasn't found on GitHub.")

    say(f"Switching to {tag}…")
    merge = _git(root, "merge", "--ff-only", tag)
    if merge.returncode != 0:
        return UpdateResult(
            False,
            "This copy has commits that aren't in the release, so it can't move to it without "
            "losing them. Update it with git yourself.\n\n" + merge.stderr.strip()[:500],
        )

    deps_changed = _git(root, "diff", "--quiet", old_head, "HEAD", "--", "pyproject.toml").returncode != 0
    if deps_changed:
        say("Installing the new dependencies… (this can take a minute)")
        try:
            install = _run(install_command, root, timeout=900)
            failed = install.returncode != 0
            detail = (install.stderr or install.stdout).strip()[-800:]
        except (OSError, subprocess.TimeoutExpired) as e:
            failed, detail = True, str(e)
        if failed:
            log.error("Installing dependencies failed; going back to %s:\n%s", old_head, detail)
            back = _git(root, "reset", "--keep", old_head)
            note = "" if back.returncode == 0 else "\n\nGoing back also failed: " + back.stderr.strip()
            return UpdateResult(
                False,
                "Installing the new dependencies failed, so the app was left on the old version."
                + note
                + "\n\n"
                + detail,
            )

    log.info("Updated from %s to %s", old_head[:10], tag)
    extra = " The dependencies were updated too." if deps_changed else ""
    return UpdateResult(True, f"Updated to {tag}.{extra}")
