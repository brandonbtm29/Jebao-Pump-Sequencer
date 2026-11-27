"""Tests for updater.py: release check (no network) and updating a real local git clone."""

import os
import subprocess
import sys
import threading
import time

import pytest
import requests

import updater

REAL_RELEASE_SIGNED = updater.release_signed


class Resp:
    def __init__(self, status=200, data=None, bad_json=False):
        self.status_code = status
        self._data = data or {}
        self._bad = bad_json

    def json(self):
        if self._bad:
            raise ValueError("bad json")
        return self._data


def fake_get(resp=None, exc=None):
    def get(url, headers=None, timeout=None):
        assert url == updater.API
        assert timeout
        if exc:
            raise exc
        return resp

    return get


@pytest.mark.parametrize(
    "text,expected",
    [("v1.2.0", (1, 2, 0)), ("1.2", (1, 2, 0)), ("v10.0.3", (10, 0, 3)), ("v1.2.0-rc1", None), ("", None)],
)
def test_parse_version(text, expected):
    assert updater.parse_version(text) == expected


def test_is_newer():
    assert updater.is_newer("v1.2.0", "1.1.0")
    assert updater.is_newer("1.10.0", "1.9.9")
    assert not updater.is_newer("v1.1.0", "1.1.0")
    assert not updater.is_newer("v1.0.0", "1.1.0")
    assert not updater.is_newer("nightly", "1.1.0")


def test_latest_release_reads_github_answer():
    data = {"tag_name": "v1.2.0", "name": "Jebao 1.2.0", "body": "notes", "html_url": "https://x/y"}
    rel = updater.latest_release("1.1.0", get=fake_get(Resp(200, data)))
    assert (rel.version, rel.tag, rel.name, rel.notes, rel.url) == (
        "1.2.0",
        "v1.2.0",
        "Jebao 1.2.0",
        "notes",
        "https://x/y",
    )


@pytest.mark.parametrize(
    "kwargs,words",
    [
        ({"resp": Resp(404)}, "No releases"),
        ({"resp": Resp(403)}, "limiting"),
        ({"resp": Resp(500)}, "(500)"),
        ({"resp": Resp(200, bad_json=True)}, "couldn't be read"),
        ({"resp": Resp(200, {"tag_name": "nightly"})}, "isn't a version"),
        ({"exc": requests.ConnectionError("down")}, "internet"),
        ({"exc": requests.exceptions.SSLError("cert")}, "secure connection"),
    ],
)
def test_latest_release_problems_have_plain_messages(kwargs, words):
    with pytest.raises(LookupError, match=words if words != "(500)" else r"\(500\)"):
        updater.latest_release("1.1.0", get=fake_get(**kwargs))


# --- a real git clone -------------------------------------------------------------------


def git(cwd, *args):
    out = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)  # noqa: S603, S607
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


@pytest.fixture
def clone(tmp_path, monkeypatch):
    """An 'origin' repo with release v1.0.0 and v1.1.0, and a clone sitting at v1.0.0. These throwaway
    releases aren't signed, so the signature check lets them through here; the signing tests use the real one."""
    monkeypatch.setattr(updater, "release_signed", lambda root, ref: "")
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-q", "-b", "main")
    git(origin, "config", "user.email", "t@example.com")
    git(origin, "config", "user.name", "Test")
    git(origin, "config", "commit.gpgsign", "false")
    git(origin, "config", "tag.gpgsign", "false")
    (origin / "app.py").write_text("v1\n")
    (origin / "pyproject.toml").write_text("deps = 1\n")
    git(origin, "add", ".")
    git(origin, "commit", "-q", "-m", "one")
    git(origin, "tag", "v1.0.0")
    (origin / "app.py").write_text("v1.1\n")
    git(origin, "commit", "-q", "-am", "two")
    git(origin, "tag", "v1.1.0")
    (origin / "app.py").write_text("unreleased\n")
    git(origin, "commit", "-q", "-am", "work in progress")

    work = tmp_path / "work"
    git(tmp_path, "clone", "-q", str(origin), str(work))
    git(work, "reset", "-q", "--hard", "v1.0.0")
    git(work, "config", "user.email", "t@example.com")
    git(work, "config", "user.name", "Test")
    git(work, "config", "commit.gpgsign", "false")
    return origin, work


def test_ready_when_clean(clone):
    _, work = clone
    assert updater.update_readiness(str(work)).ok


def test_not_ready_without_git_folder(tmp_path):
    ready = updater.update_readiness(str(tmp_path))
    assert not ready.ok and "ZIP" in ready.reason


def test_not_ready_with_local_changes(clone):
    _, work = clone
    (work / "app.py").write_text("my edit\n")
    ready = updater.update_readiness(str(work))
    assert not ready.ok and "app.py" in ready.reason


def test_update_moves_to_the_release_not_main(clone):
    _, work = clone
    calls = []
    result = updater.update_to("v1.1.0", root=str(work), install_command=["false"], progress=calls.append)
    assert result.ok, result.message
    assert (work / "app.py").read_text() == "v1.1\n"
    assert "dependencies" not in result.message  # pyproject.toml didn't change, so nothing was installed
    assert calls and calls[0].startswith("Downloading")


def test_update_reinstalls_when_dependencies_change(clone):
    origin, work = clone
    (origin / "pyproject.toml").write_text("deps = 2\n")
    git(origin, "commit", "-q", "-am", "new deps")
    git(origin, "tag", "v1.2.0")
    ok_cmd = [sys.executable, "-c", "print('installed')"]
    result = updater.update_to("v1.2.0", root=str(work), install_command=ok_cmd)
    assert result.ok and "dependencies were updated" in result.message
    assert (work / "pyproject.toml").read_text() == "deps = 2\n"


def test_failed_install_rolls_back(clone):
    origin, work = clone
    (origin / "pyproject.toml").write_text("deps = 3\n")
    git(origin, "commit", "-q", "-am", "broken deps")
    git(origin, "tag", "v1.3.0")
    before = git(work, "rev-parse", "HEAD")
    bad_cmd = [sys.executable, "-c", "import sys; sys.exit('no wheel for you')"]
    result = updater.update_to("v1.3.0", root=str(work), install_command=bad_cmd)
    assert not result.ok and "left on the old version" in result.message
    assert git(work, "rev-parse", "HEAD") == before
    assert (work / "app.py").read_text() == "v1\n"


def test_missing_tag(clone):
    _, work = clone
    result = updater.update_to("v9.9.9", root=str(work))
    assert not result.ok and "wasn't found" in result.message


def test_local_commits_are_never_overwritten(clone):
    _, work = clone
    (work / "notes.txt").write_text("mine\n")
    git(work, "add", "notes.txt")
    git(work, "commit", "-q", "-m", "local work")
    result = updater.update_to("v1.1.0", root=str(work))
    assert not result.ok and "commits that aren't in the release" in result.message
    assert os.path.exists(work / "notes.txt")


def _new_deps_release(origin, tag):
    (origin / "pyproject.toml").write_text(f"deps = {tag}\n")
    git(origin, "commit", "-q", "-am", f"deps for {tag}")
    git(origin, "tag", tag)


def test_install_output_is_shown_as_it_happens(clone):
    origin, work = clone
    _new_deps_release(origin, "v1.2.0")
    seen = []
    cmd = [sys.executable, "-c", "print('Collecting hatchling'); print('Successfully installed')"]
    result = updater.update_to("v1.2.0", root=str(work), install_command=cmd, progress=seen.append)
    assert result.ok
    assert any("Collecting hatchling" in s for s in seen)


def test_slow_install_times_out_and_rolls_back(clone, monkeypatch):
    origin, work = clone
    _new_deps_release(origin, "v1.2.0")
    monkeypatch.setattr(updater, "INSTALL_TIMEOUT", 1)
    before = git(work, "rev-parse", "HEAD")
    started = time.monotonic()
    result = updater.update_to(
        "v1.2.0", root=str(work), install_command=[sys.executable, "-c", "import time; time.sleep(30)"]
    )
    assert time.monotonic() - started < 15
    assert not result.ok and "took over" in result.message and "left on the old version" in result.message
    assert git(work, "rev-parse", "HEAD") == before


def test_cancel_during_install_rolls_back(clone):
    origin, work = clone
    _new_deps_release(origin, "v1.2.0")
    before = git(work, "rev-parse", "HEAD")
    cancel = threading.Event()

    def say(msg):
        if msg.startswith("Installing"):
            cancel.set()

    cmd = [sys.executable, "-c", "import time; time.sleep(30)"]
    result = updater.update_to("v1.2.0", root=str(work), install_command=cmd, progress=say, cancel=cancel)
    assert not result.ok and "cancelled" in result.message
    assert git(work, "rev-parse", "HEAD") == before


def test_cancel_before_starting_changes_nothing(clone):
    _, work = clone
    cancel = threading.Event()
    cancel.set()
    before = git(work, "rev-parse", "HEAD")
    result = updater.update_to("v1.1.0", root=str(work), cancel=cancel)
    assert not result.ok and "Nothing was changed" in result.message
    assert git(work, "rev-parse", "HEAD") == before


def test_pip_runs_under_python_not_pythonw(tmp_path):
    (tmp_path / "python.exe").write_text("")
    (tmp_path / "pythonw.exe").write_text("")
    assert updater.console_python(str(tmp_path / "pythonw.exe")) == str(tmp_path / "python.exe")
    assert updater.console_python(str(tmp_path / "python.exe")) == str(tmp_path / "python.exe")
    assert updater.console_python("/usr/bin/python3") == "/usr/bin/python3"
    assert "--no-input" in updater.default_install_command()


# --- git's environment and signed releases ----------------------------------------------


def test_git_runs_without_the_callers_git_variables(monkeypatch):
    """GIT_CONFIG_* can make git run commands and GIT_DIR points it at another repo: none come along."""
    for name, value in {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.fsmonitor",
        "GIT_CONFIG_VALUE_0": "evil",
        "git_dir": "/elsewhere",
        "GIT_SSH_COMMAND": "evil",
    }.items():
        monkeypatch.setenv(name, value)
    env = updater._popen_kwargs()["env"]
    assert not [k for k in env if k.upper().startswith("GIT_") and k != "GIT_TERMINAL_PROMPT"]
    assert env["GIT_TERMINAL_PROMPT"] == "0" and "PATH" in {k.upper() for k in env}


def _signers_file(repo, text='me@example.com namespaces="git" ssh-ed25519 AAAA\n'):
    (repo / ".github").mkdir(parents=True, exist_ok=True)
    (repo / ".github" / "allowed_signers").write_text(text)


@pytest.mark.parametrize(
    "letter,code,words",
    [
        ("N", 0, "isn't signed"),
        ("U", 0, "key that isn't on the trusted list"),
        ("B", 0, "bad signature"),
        ("E", 0, "couldn't be checked"),
        ("R", 0, "revoked key"),
        ("", 0, "couldn't be checked"),  # git printed nothing
        ("G", 128, "couldn't be checked"),  # git failed
    ],
)
def test_only_good_signatures_pass(tmp_path, monkeypatch, letter, code, words):
    _signers_file(tmp_path)
    monkeypatch.setattr(updater, "_ssh_keygen", lambda: "/usr/bin/ssh-keygen")
    calls = []

    def run(cmd, cwd, timeout=120):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, code, stdout=letter + "\n", stderr="")

    monkeypatch.setattr(updater, "_run", run)
    why = updater.release_signed(str(tmp_path), "abc123")
    assert words in why and "allowed_signers" in why and "wasn't installed" in why
    cmd = calls[0]
    assert cmd[:2] == ["git", "-c"] and cmd[-1] == "abc123" and "--format=%G?" in cmd
    assert f"gpg.ssh.allowedSignersFile={tmp_path / '.github' / 'allowed_signers'}" in cmd  # this copy's own
    assert "gpg.ssh.program=/usr/bin/ssh-keygen" in cmd

    monkeypatch.setattr(
        updater, "_run", lambda cmd, cwd, timeout=120: subprocess.CompletedProcess(cmd, 0, "G\n", "")
    )
    assert updater.release_signed(str(tmp_path), "abc123") == ""


def test_ssh_keygen_prefers_gits_copy_on_windows(tmp_path, monkeypatch):
    """System32's ssh-keygen can hang on -Y verify in an SSH session (BMAX), so Git's own copy comes first."""
    git = tmp_path / "Git" / "cmd" / "git.exe"
    gits = tmp_path / "Git" / "usr" / "bin" / "ssh-keygen.exe"
    on_path = tmp_path / "OpenSSH" / "ssh-keygen.exe"
    for f in (git, gits, on_path):
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("")
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "nowhere"))
    monkeypatch.setattr(updater.shutil, "which", lambda name: str({"git": git, "ssh-keygen": on_path}[name]))
    assert os.path.samefile(updater._ssh_keygen(windows=True), gits)
    assert updater._ssh_keygen(windows=False) == str(on_path)  # elsewhere: the one on PATH
    gits.unlink()
    assert updater._ssh_keygen(windows=True) == str(on_path)


def test_no_list_or_no_ssh_keygen_means_no_update(tmp_path, monkeypatch):
    def run(cmd, cwd, timeout=120):
        raise AssertionError("git shouldn't run")

    monkeypatch.setattr(updater, "_run", run)
    assert "has no .github/allowed_signers" in updater.release_signed(str(tmp_path), "abc123")
    _signers_file(tmp_path)
    monkeypatch.setattr(updater, "_ssh_keygen", lambda: None)
    assert "ssh-keygen isn't installed" in updater.release_signed(str(tmp_path), "abc123")


def test_update_refuses_a_release_that_is_not_signed(clone, monkeypatch):
    _, work = clone
    before = git(work, "rev-parse", "HEAD")
    monkeypatch.setattr(updater, "release_signed", REAL_RELEASE_SIGNED)  # this clone has no allowed_signers
    result = updater.update_to("v1.1.0", root=str(work), install_command=["false"])
    assert not result.ok and "has no .github/allowed_signers" in result.message
    monkeypatch.setattr(
        updater, "release_signed", lambda root, ref: "The release isn't signed, so it wasn't installed."
    )
    result = updater.update_to("v1.1.0", root=str(work), install_command=["false"])
    assert not result.ok and result.message.startswith("Release v1.1.0 isn't signed")
    assert git(work, "rev-parse", "HEAD") == before and (work / "app.py").read_text() == "v1\n"


def _key(tmp_path, name):
    key = tmp_path / name
    subprocess.run(  # noqa: S603 - fixed command, no shell
        [updater._ssh_keygen(), "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", str(key)],
        check=True,
        capture_output=True,
    )
    return key


def _signed_release(origin, tag, key, signers_text=None):
    """A release commit signed with ``key`` (it may also change the list of trusted keys), tagged ``tag``."""
    (origin / "app.py").write_text(tag + "\n")
    if signers_text is not None:
        _signers_file(origin, signers_text)
    git(origin, "add", "-A")
    sign = [
        "-c",
        "gpg.format=ssh",
        "-c",
        f"user.signingkey={key}",
        "-c",
        f"gpg.ssh.program={updater._ssh_keygen()}",
    ]
    cmd = ["git", *sign, "commit", "-S", "-qm", tag]
    r = subprocess.run(cmd, cwd=origin, capture_output=True, text=True)  # noqa: S603 - fixed command, no shell
    if r.returncode and os.name == "nt":
        pytest.skip(f"can't sign a test commit here: {r.stderr.strip()[:200]}")
    assert r.returncode == 0, r.stderr
    git(origin, "tag", tag)


def _unsigned_release(origin, tag):
    (origin / "app.py").write_text(tag + "\n")
    git(origin, "commit", "-q", "-am", tag)
    git(origin, "tag", tag)


@pytest.mark.skipif(updater._ssh_keygen() is None, reason="OpenSSH's ssh-keygen isn't installed")
def test_real_signatures(tmp_path):
    """Signed with a listed key: installs. Unsigned, signed by an unlisted key, or a release that lists its
    own new key: refused, because the check uses this copy's list, not the downloaded one."""
    trusted, stranger = _key(tmp_path, "trusted"), _key(tmp_path, "stranger")
    trusted_line = 'me@example.com namespaces="git" ' + (tmp_path / "trusted.pub").read_text().strip()
    stranger_line = 'me@example.com namespaces="git" ' + (tmp_path / "stranger.pub").read_text().strip()
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-q", "-b", "main")
    for key, value in {
        "user.email": "t@example.com",
        "user.name": "Test",
        "commit.gpgsign": "false",
        "tag.gpgsign": "false",
    }.items():
        git(origin, "config", key, value)
    _signed_release(origin, "v1.0.0", trusted, signers_text=trusted_line + "\n")
    work = tmp_path / "work"
    git(tmp_path, "clone", "-q", str(origin), str(work))

    _signed_release(origin, "v1.1.0", trusted)
    result = updater.update_to("v1.1.0", root=str(work), install_command=["false"])
    assert result.ok, result.message

    _unsigned_release(origin, "v1.2.0")
    result = updater.update_to("v1.2.0", root=str(work), install_command=["false"])
    assert not result.ok and "Release v1.2.0 isn't signed" in result.message

    _signed_release(origin, "v1.3.0", stranger)
    result = updater.update_to("v1.3.0", root=str(work), install_command=["false"])
    assert not result.ok and "isn't on the trusted list" in result.message

    # A release that adds its own key to the list is still checked against this copy's list
    _signed_release(origin, "v1.4.0", stranger, signers_text=trusted_line + "\n" + stranger_line + "\n")
    result = updater.update_to("v1.4.0", root=str(work), install_command=["false"])
    assert not result.ok and "isn't on the trusted list" in result.message
    assert (work / "app.py").read_text() == "v1.1.0\n"
