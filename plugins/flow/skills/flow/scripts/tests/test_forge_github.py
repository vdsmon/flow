from __future__ import annotations

import json
import subprocess

import pytest

from forge import NotSupported
from forge_github import GitHubAdapter

Recorder = list[list[str]]


def _adapter(responses: dict | None = None) -> tuple[GitHubAdapter, Recorder]:
    """GitHubAdapter with a fake runner that pattern-matches argv prefixes.

    `responses` keys: 'list' (json str), 'create' (stdout), 'view' (json str).
    """
    responses = responses or {}
    calls: Recorder = []

    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[:3] == ["gh", "pr", "list"]:
            return subprocess.CompletedProcess(args, 0, responses.get("list", "[]"), "")
        if args[:3] == ["gh", "pr", "create"]:
            return subprocess.CompletedProcess(args, 0, responses.get("create", ""), "")
        if args[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(args, 0, responses.get("view", "{}"), "")
        if args[:3] in (["gh", "pr", "ready"], ["gh", "pr", "merge"]):
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:3] == ["git", "push", "origin"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 1, "", f"unexpected {args}")

    return GitHubAdapter({"workspace_root": "."}, runner=run), calls


def _ran(calls: Recorder, prefix: list[str]) -> bool:
    return any(c[: len(prefix)] == prefix for c in calls)


def test_detect_pr_parses_first_item():
    listing = json.dumps(
        [
            {
                "number": 7,
                "url": "https://github.com/o/r/pull/7",
                "isDraft": True,
                "baseRefName": "main",
                "headRefName": "feature/flow-x",
                "state": "OPEN",
            }
        ]
    )
    fg, _ = _adapter({"list": listing})
    pr = fg.detect_pr("feature/flow-x")
    assert pr is not None
    assert pr["number"] == 7
    assert pr["id"] == "7"
    assert pr["draft"] is True
    assert pr["url"].endswith("/pull/7")


def test_detect_pr_none_when_empty():
    fg, _ = _adapter({"list": "[]"})
    assert fg.detect_pr("feature/flow-x") is None


def test_open_pr_omits_draft_flag_when_ready():
    fg, calls = _adapter({"create": "https://github.com/o/r/pull/42\n"})
    pr = fg.open_pr("main", "feature/flow-x", "feat: x", "body", draft=False)
    assert pr["number"] == 42
    create = next(c for c in calls if c[:3] == ["gh", "pr", "create"])
    assert "--draft" not in create


def test_open_pr_passes_draft_flag():
    fg, calls = _adapter({"create": "https://github.com/o/r/pull/9\n"})
    fg.open_pr("main", "feature/flow-x", "feat: x", "body", draft=True)
    create = next(c for c in calls if c[:3] == ["gh", "pr", "create"])
    assert "--draft" in create


def test_open_pr_number_from_last_stdout_line():
    fg, _ = _adapter({"create": "Warning: foo\nhttps://github.com/o/r/pull/13\n"})
    pr = fg.open_pr("main", "feature/flow-x", "t", "b", draft=False)
    assert pr["number"] == 13


def test_ci_rollup_green():
    view = json.dumps(
        {"statusCheckRollup": [{"name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS"}]}
    )
    fg, _ = _adapter({"view": view})
    assert fg.ci_rollup("7")["status"] == "green"


def test_ci_rollup_failed():
    view = json.dumps(
        {
            "statusCheckRollup": [
                {"name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS"},
                {"name": "lint", "status": "COMPLETED", "conclusion": "FAILURE"},
            ]
        }
    )
    fg, _ = _adapter({"view": view})
    assert fg.ci_rollup("7")["status"] == "failed"


def test_ci_rollup_pending_when_running():
    view = json.dumps(
        {"statusCheckRollup": [{"name": "ci", "status": "IN_PROGRESS", "conclusion": ""}]}
    )
    fg, _ = _adapter({"view": view})
    assert fg.ci_rollup("7")["status"] == "pending"


def test_ci_rollup_pending_when_empty():
    fg, _ = _adapter({"view": json.dumps({"statusCheckRollup": []})})
    assert fg.ci_rollup("7")["status"] == "pending"


def test_ci_rollup_status_context_shape():
    view = json.dumps({"statusCheckRollup": [{"context": "buildkite", "state": "SUCCESS"}]})
    fg, _ = _adapter({"view": view})
    assert fg.ci_rollup("7")["status"] == "green"


def test_mark_ready_merge_delete_argv():
    fg, calls = _adapter()
    fg.mark_ready("7")
    fg.merge("7", squash=True)
    fg.delete_branch("feature/flow-x")
    assert _ran(calls, ["gh", "pr", "ready", "7"])
    assert _ran(calls, ["gh", "pr", "merge", "7", "--squash"])
    assert _ran(calls, ["git", "push", "origin", "--delete", "feature/flow-x"])


def test_review_threads_not_supported():
    fg, _ = _adapter()
    with pytest.raises(NotSupported):
        fg.review_threads("7")
    with pytest.raises(NotSupported):
        fg.post_reply("7", "t1", "body")
    with pytest.raises(NotSupported):
        fg.resolve_thread("7", "t1")


def test_capabilities_review_threads_off():
    fg, _ = _adapter()
    caps = {c["name"]: c["supported"] for c in fg.capabilities}
    assert caps["review_threads"] is False
    assert caps["ci_rollup"] is True
    assert caps["squash_merge"] is True
