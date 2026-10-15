"""Tests for updater.py: release check (no network) and updating a real local git clone."""

import os
import subprocess
import sys

import pytest
import requests

import updater


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
def clone(tmp_path):
    """An 'origin' repo with release v1.0.0 and v1.1.0, and a clone sitting at v1.0.0."""
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
