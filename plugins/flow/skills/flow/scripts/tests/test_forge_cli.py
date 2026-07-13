from __future__ import annotations

import json
from typing import ClassVar, override

import pytest

import forge_cli
from forge import ForgeError, NotSupported


class _FakeForge:
    """Records calls; scripts responses. Mirrors the Forge Protocol surface."""

    backend = "github"
    capabilities: ClassVar[list] = []

    def __init__(self, *, threads_supported: bool = True, bot_review_supported: bool = True):
        self.calls: list[tuple] = []
        self._threads_supported = threads_supported
        self._bot_review_supported = bot_review_supported

    def detect_pr(self, branch, state="open"):
        self.calls.append(("detect_pr", branch, state))
        return {
            "id": "7",
            "url": "u",
            "number": 7,
            "draft": True,
            "base": "main",
            "head": branch,
            "state": "OPEN",
        }

    def pr_info(self, pr_id):
        self.calls.append(("pr_info", pr_id))
        return {
            "id": pr_id,
            "url": "u",
            "number": int(pr_id),
            "draft": False,
            "base": "main",
            "head": "feature/flow-x",
            "state": "OPEN",
        }

    def open_pr(self, base, head, title, body, draft):
        self.calls.append(("open_pr", base, head, draft))
        return {
            "id": "8",
            "url": "u8",
            "number": 8,
            "draft": draft,
            "base": base,
            "head": head,
            "state": "OPEN",
        }

    def ci_rollup(self, pr_id):
        self.calls.append(("ci_rollup", pr_id))
        return {"status": "green", "checks": [], "detail": "ok"}

    def review_threads(self, pr_id):
        self.calls.append(("review_threads", pr_id))
        if not self._threads_supported:
            raise NotSupported("no review threads")
        return [{"id": "1", "severity": "major", "title": "x", "resolved": False}]

    def bot_review_present(self, pr_id):
        self.calls.append(("bot_review_present", pr_id))
        if not self._bot_review_supported:
            raise NotSupported("no bot review status")
        return True

    def post_reply(self, pr_id, thread_id, body):
        self.calls.append(("post_reply", pr_id, thread_id))

    def resolve_thread(self, pr_id, thread_id):
        self.calls.append(("resolve_thread", pr_id, thread_id))
        return True

    def mark_ready(self, pr_id):
        self.calls.append(("mark_ready", pr_id))

    def merge(self, pr_id, squash=True):
        self.calls.append(("merge", pr_id, squash))

    def delete_branch(self, branch):
        self.calls.append(("delete_branch", branch))


class _FailingForge(_FakeForge):
    @override
    def detect_pr(self, branch, state="open"):
        raise ForgeError(f"network failed for {branch}")


@pytest.fixture
def ws(tmp_path):
    (tmp_path / ".flow").mkdir()
    (tmp_path / ".flow" / "workspace.toml").write_text(
        '[forge]\nbackend = "github"\n[forge.github]\n', encoding="utf-8"
    )
    return tmp_path


def _run(argv, ws, **fake_kwargs):
    fake = _FakeForge(**fake_kwargs)
    rc = forge_cli.cli_main(["--workspace-root", str(ws), *argv], forge_factory=lambda _cfg: fake)
    return rc, fake


def test_detect_pr(ws, capsys):
    rc, fake = _run(["detect-pr", "--branch", "feature/flow-x"], ws)
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["number"] == 7
    assert ("detect_pr", "feature/flow-x", "open") in fake.calls


def test_detect_pr_passes_state_selector(ws, capsys):
    rc, fake = _run(["detect-pr", "--branch", "feature/flow-x", "--state", "merged"], ws)
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["number"] == 7
    assert ("detect_pr", "feature/flow-x", "merged") in fake.calls


def test_pr_info(ws, capsys):
    rc, fake = _run(["pr-info", "--pr", "7"], ws)
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["number"] == 7
    assert out["head"] == "feature/flow-x"
    assert ("pr_info", "7") in fake.calls


def test_ci_rollup(ws, capsys):
    rc, _ = _run(["ci-rollup", "--pr", "7"], ws)
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["status"] == "green"


def test_merge(ws, capsys):
    rc, fake = _run(["merge", "--pr", "7", "--squash"], ws)
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert ("merge", "7", True) in fake.calls


def test_resolve_thread(ws, capsys):
    rc, _ = _run(["resolve-thread", "--pr", "7", "--thread", "1"], ws)
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["resolved"] is True


def test_review_threads_degrades_on_not_supported(ws, capsys):
    rc, _ = _run(["review-threads", "--pr", "7"], ws, threads_supported=False)
    assert rc == 0  # degrade, not error
    assert json.loads(capsys.readouterr().out) == {"supported": False}


def test_review_status_emits_reviewed(ws, capsys):
    rc, fake = _run(["review-status", "--pr", "7"], ws)
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {"reviewed": True}
    assert ("bot_review_present", "7") in fake.calls


def test_review_status_degrades_on_not_supported(ws, capsys):
    rc, _ = _run(["review-status", "--pr", "7"], ws, bot_review_supported=False)
    assert rc == 0  # degrade, not error, review_loop skips the wait
    assert json.loads(capsys.readouterr().out) == {"supported": False}


def test_missing_forge_block_is_config_error(tmp_path):
    (tmp_path / ".flow").mkdir()
    (tmp_path / ".flow" / "workspace.toml").write_text('[tracker]\nbackend = "beads"\n', "utf-8")
    rc = forge_cli.cli_main(
        ["--workspace-root", str(tmp_path), "detect-pr", "--branch", "x"],
        forge_factory=lambda _cfg: _FakeForge(),
    )
    assert rc == 2


def test_forge_error_returns_1(ws, capsys):
    rc = forge_cli.cli_main(
        ["--workspace-root", str(ws), "detect-pr", "--branch", "x"],
        forge_factory=lambda _cfg: _FailingForge(),
    )
    assert rc == 1
    assert "forge error" in capsys.readouterr().err


def test_post_reply_dispatches_args(ws, capsys):
    rc, fake = _run(["post-reply", "--pr", "7", "--thread", "42", "--text", "lgtm"], ws)
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True}
    assert ("post_reply", "7", "42") in fake.calls


def test_mark_ready_dispatches(ws, capsys):
    rc, fake = _run(["mark-ready", "--pr", "7"], ws)
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True}
    assert ("mark_ready", "7") in fake.calls


def test_delete_branch_dispatches(ws, capsys):
    rc, fake = _run(["delete-branch", "--branch", "feature/flow-x"], ws)
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True}
    assert ("delete_branch", "feature/flow-x") in fake.calls


def test_factory_error_returns_2(ws, capsys):
    def bad_factory(_cfg):
        raise RuntimeError("boom")

    rc = forge_cli.cli_main(
        ["--workspace-root", str(ws), "detect-pr", "--branch", "x"],
        forge_factory=bad_factory,
    )
    assert rc == 2
    assert "factory error" in capsys.readouterr().err


class _KeyErrorForge(_FakeForge):
    @override
    def mark_ready(self, pr_id):
        raise KeyError("bad-pr")


def test_invalid_argument_returns_3(ws, capsys):
    rc = forge_cli.cli_main(
        ["--workspace-root", str(ws), "mark-ready", "--pr", "7"],
        forge_factory=lambda _cfg: _KeyErrorForge(),
    )
    assert rc == 3
    assert "invalid argument" in capsys.readouterr().err


class _PostReplyNotSupportedForge(_FakeForge):
    @override
    def post_reply(self, pr_id, thread_id, body):
        raise NotSupported("no replies")


def test_post_reply_degrades_on_not_supported(ws, capsys):
    rc = forge_cli.cli_main(
        ["--workspace-root", str(ws), "post-reply", "--pr", "7", "--thread", "1", "--text", "x"],
        forge_factory=lambda _cfg: _PostReplyNotSupportedForge(),
    )
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {"supported": False}
