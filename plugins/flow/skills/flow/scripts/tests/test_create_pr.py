from __future__ import annotations

import json
import subprocess
from collections.abc import Callable

import pytest

import create_pr as cp

Recorder = list[list[str]]


def _runner(
    *,
    branch: str = "feature/flow-aut.7-x",
    existing: list[dict] | None = None,
    created_url: str = "https://github.com/o/r/pull/42",
) -> tuple[Callable[..., subprocess.CompletedProcess[str]], Recorder]:
    calls: Recorder = []

    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(args, 0, branch + "\n", "")
        if args[:2] == ["git", "push"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:2] == ["git", "log"]:
            return subprocess.CompletedProcess(args, 0, "test: add coverage\n", "")
        if args[:3] == ["gh", "pr", "list"]:
            return subprocess.CompletedProcess(args, 0, json.dumps(existing or []), "")
        if args[:3] == ["gh", "pr", "create"]:
            return subprocess.CompletedProcess(args, 0, created_url + "\n", "")
        return subprocess.CompletedProcess(args, 1, "", f"unexpected {args}")

    return run, calls


def _ran(calls: Recorder, prefix: list[str]) -> bool:
    return any(c[: len(prefix)] == prefix for c in calls)


def test_creates_when_no_existing_pr(tmp_path):
    run, calls = _runner(existing=[])
    url = cp.open_or_get_pr(tmp_path, base="main", runner=run)
    assert url == "https://github.com/o/r/pull/42"
    assert _ran(calls, ["gh", "pr", "create"])
    assert _ran(calls, ["git", "push"])


def test_idempotent_reuses_existing_pr(tmp_path):
    run, calls = _runner(existing=[{"url": "https://github.com/o/r/pull/7"}])
    url = cp.open_or_get_pr(tmp_path, base="main", runner=run)
    assert url == "https://github.com/o/r/pull/7"
    assert not _ran(calls, ["gh", "pr", "create"])  # never double-open


def test_refuses_protected_branch(tmp_path):
    run, _ = _runner(branch="main")
    with pytest.raises(cp.RefusedBranch):
        cp.open_or_get_pr(tmp_path, runner=run)


def test_push_failure_is_tool_error(tmp_path):
    def run(args):
        if args[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(args, 0, "feature/flow-x\n", "")
        if args[:2] == ["git", "push"]:
            return subprocess.CompletedProcess(args, 1, "", "remote rejected")
        return subprocess.CompletedProcess(args, 0, "[]", "")

    with pytest.raises(cp.ToolError):
        cp.open_or_get_pr(tmp_path, runner=run)


def test_create_url_is_last_stdout_line(tmp_path):
    def run(args):
        if args[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(args, 0, "feature/flow-x\n", "")
        if args[:2] == ["git", "push"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:2] == ["git", "log"]:
            return subprocess.CompletedProcess(args, 0, "test: x\n", "")
        if args[:3] == ["gh", "pr", "list"]:
            return subprocess.CompletedProcess(args, 0, "[]", "")
        if args[:3] == ["gh", "pr", "create"]:
            # gh sometimes prints a preamble line before the URL
            return subprocess.CompletedProcess(
                args, 0, "Warning: ...\nhttps://github.com/o/r/pull/9\n", ""
            )
        return subprocess.CompletedProcess(args, 1, "", "x")

    assert cp.open_or_get_pr(tmp_path, runner=run) == "https://github.com/o/r/pull/9"


def test_cli_prints_pr_url_token(tmp_path, monkeypatch, capsys):
    run, _ = _runner(existing=[{"url": "https://github.com/o/r/pull/5"}])
    monkeypatch.setattr(cp, "_default_runner", lambda _repo: run)
    rc = cp.cli_main(
        ["--workspace-root", str(tmp_path), "--base", "main", "--ticket", "flow-aut.7"]
    )
    assert rc == 0
    assert "PR_URL=https://github.com/o/r/pull/5" in capsys.readouterr().out


def test_open_ready_omits_draft_flag(tmp_path):
    run, calls = _runner(existing=[])
    cp.open_or_get_pr(tmp_path, base="main", runner=run)
    create = next(c for c in calls if c[:3] == ["gh", "pr", "create"])
    assert "--draft" not in create


def test_open_draft_passes_draft_flag(tmp_path):
    run, calls = _runner(existing=[])
    cp.open_or_get_pr(tmp_path, base="main", draft=True, runner=run)
    create = next(c for c in calls if c[:3] == ["gh", "pr", "create"])
    assert "--draft" in create


def test_draft_config_default_false_when_no_workspace(tmp_path):
    assert cp._draft_config(tmp_path) is False


def test_draft_config_reads_true(tmp_path):
    (tmp_path / ".flow").mkdir()
    (tmp_path / ".flow" / "workspace.toml").write_text(
        "[create_pr]\ndraft = true\n", encoding="utf-8"
    )
    assert cp._draft_config(tmp_path) is True
