from __future__ import annotations

import subprocess
from typing import ClassVar

import pytest

import create_pr as cp
from forge import ForgeError, PullRequest

Recorder = list[list[str]]


def _pr(url: str, head: str, *, base: str = "main", draft: bool = False) -> PullRequest:
    return {
        "id": "1",
        "url": url,
        "number": 1,
        "draft": draft,
        "base": base,
        "head": head,
        "state": "OPEN",
    }


def _git_runner(*, branch: str = "feature/flow-aut.7-x", push_rc: int = 0):
    """Fake runner for the git-only calls create_pr still makes directly."""
    calls: Recorder = []

    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(args, 0, branch + "\n", "")
        if args[:2] == ["git", "push"]:
            return subprocess.CompletedProcess(args, push_rc, "", "remote rejected")
        if args[:2] == ["git", "log"]:
            return subprocess.CompletedProcess(args, 0, "test: add coverage\n", "")
        return subprocess.CompletedProcess(args, 1, "", f"unexpected {args}")

    return run, calls


class _FakeForge:
    backend = "github"
    capabilities: ClassVar[list] = []

    def __init__(self, *, existing: str | None = None, created="https://github.com/o/r/pull/42"):
        self._existing = existing
        self._created = created
        self.opened: list[dict] = []
        self.raise_on_open: Exception | None = None

    def detect_pr(self, branch: str) -> PullRequest | None:
        return _pr(self._existing, branch) if self._existing else None

    def open_pr(self, base: str, head: str, title: str, body: str, draft: bool) -> PullRequest:
        if self.raise_on_open:
            raise self.raise_on_open
        self.opened.append({"base": base, "head": head, "draft": draft, "title": title})
        return _pr(self._created, head, base=base, draft=draft)

    def ci_rollup(self, pr_id: str):
        raise NotImplementedError

    def review_threads(self, pr_id: str):
        raise NotImplementedError

    def post_reply(self, pr_id: str, thread_id: str, body: str) -> None: ...
    def resolve_thread(self, pr_id: str, thread_id: str) -> bool:
        return True

    def mark_ready(self, pr_id: str) -> None: ...
    def merge(self, pr_id: str, squash: bool = True) -> None: ...
    def delete_branch(self, branch: str) -> None: ...


def _ran(calls: Recorder, prefix: list[str]) -> bool:
    return any(c[: len(prefix)] == prefix for c in calls)


def test_creates_when_no_existing_pr(tmp_path):
    run, calls = _git_runner()
    fg = _FakeForge(existing=None)
    url = cp.open_or_get_pr(tmp_path, base="main", runner=run, forge=fg)
    assert url == "https://github.com/o/r/pull/42"
    assert len(fg.opened) == 1
    assert _ran(calls, ["git", "push"])


def test_idempotent_reuses_existing_pr(tmp_path):
    run, _ = _git_runner()
    fg = _FakeForge(existing="https://github.com/o/r/pull/7")
    url = cp.open_or_get_pr(tmp_path, base="main", runner=run, forge=fg)
    assert url == "https://github.com/o/r/pull/7"
    assert fg.opened == []  # never double-open


def test_refuses_protected_branch(tmp_path):
    run, _ = _git_runner(branch="main")
    with pytest.raises(cp.RefusedBranch):
        cp.open_or_get_pr(tmp_path, runner=run, forge=_FakeForge())


def test_push_failure_is_tool_error(tmp_path):
    run, _ = _git_runner(push_rc=1)
    with pytest.raises(cp.ToolError):
        cp.open_or_get_pr(tmp_path, runner=run, forge=_FakeForge())


def test_forge_error_is_tool_error(tmp_path):
    run, _ = _git_runner()
    fg = _FakeForge()
    fg.raise_on_open = ForgeError("gh pr create failed")
    with pytest.raises(cp.ToolError):
        cp.open_or_get_pr(tmp_path, runner=run, forge=fg)


def test_open_ready_omits_draft(tmp_path):
    run, _ = _git_runner()
    fg = _FakeForge()
    cp.open_or_get_pr(tmp_path, base="main", runner=run, forge=fg)
    assert fg.opened[0]["draft"] is False


def test_open_draft_passes_draft(tmp_path):
    run, _ = _git_runner()
    fg = _FakeForge()
    cp.open_or_get_pr(tmp_path, base="main", draft=True, runner=run, forge=fg)
    assert fg.opened[0]["draft"] is True


def test_cli_prints_pr_url_token(tmp_path, monkeypatch, capsys):
    run, _ = _git_runner(branch="feature/flow-x")
    fg = _FakeForge(existing="https://github.com/o/r/pull/5")
    monkeypatch.setattr(cp, "_default_runner", lambda _repo: run)
    monkeypatch.setattr(cp, "_resolve_forge", lambda _ws: fg)
    rc = cp.cli_main(["--workspace-root", str(tmp_path), "--base", "main", "--ticket", "flow-x"])
    assert rc == 0
    assert "PR_URL=https://github.com/o/r/pull/5" in capsys.readouterr().out


def test_cli_missing_forge_block_is_tool_error(tmp_path, monkeypatch):
    run, _ = _git_runner(branch="feature/flow-x")
    monkeypatch.setattr(cp, "_default_runner", lambda _repo: run)
    # no [forge] block in tmp_path -> _resolve_forge raises ToolError -> exit 2
    rc = cp.cli_main(["--workspace-root", str(tmp_path), "--base", "main"])
    assert rc == 2


def test_draft_config_default_false_when_no_workspace(tmp_path):
    assert cp._draft_config(tmp_path) is False


def test_draft_config_reads_true(tmp_path):
    (tmp_path / ".flow").mkdir()
    (tmp_path / ".flow" / "workspace.toml").write_text(
        "[create_pr]\ndraft = true\n", encoding="utf-8"
    )
    assert cp._draft_config(tmp_path) is True
