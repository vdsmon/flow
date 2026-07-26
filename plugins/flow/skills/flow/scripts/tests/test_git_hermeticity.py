"""Guards `conftest._hermetic_git`: developer git config must not reach test repos."""

from __future__ import annotations

import subprocess
from pathlib import Path


def _config(repo: Path, key: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "config", "--get", key],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()


def _fresh_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return repo


def test_fsmonitor_does_not_leak_into_test_repos(tmp_path: Path) -> None:
    repo = _fresh_repo(tmp_path)
    assert _config(repo, "core.fsmonitor") != "true"
    assert _config(repo, "core.untrackedCache") != "true"


def test_diff_external_does_not_leak_into_test_repos(tmp_path: Path) -> None:
    # A leaked diff.external makes `git diff` emit a side-by-side rendering, which any
    # test parsing unified output would silently misread.
    assert _config(_fresh_repo(tmp_path), "diff.external") == ""


def test_identity_and_default_branch_are_supplied(tmp_path: Path) -> None:
    repo = _fresh_repo(tmp_path)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "x"], check=True)
    branch = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert branch == "main"
    assert _config(repo, "user.email") == "flow-tests@example.invalid"
