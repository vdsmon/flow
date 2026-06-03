"""Tests for the /flow SessionStart hook.

The hook file is hyphenated (`session-start.py`), not an importable module name,
so it is loaded via importlib from its path. The scripts dir is added to sys.path
so the hook's child scripts (recall.py / branch_ticket.py / recall_pending.py)
import their shared leaf modules when run under sys.executable.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

HOOK_PATH = Path(__file__).resolve().parent.parent / "session-start.py"
SCRIPTS_DIR = HOOK_PATH.parent.parent / "skills" / "flow" / "scripts"


def _load_hook() -> ModuleType:
    spec = importlib.util.spec_from_file_location("flow_session_start", HOOK_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hook = _load_hook()


# ─── git helpers ────────────────────────────────────────────────────────────


def _git(args: list[str], cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )
    return result.stdout


def _init_repo(root: Path) -> None:
    _git(["init", "--initial-branch=main"], root)
    _git(["config", "user.email", "test@example.com"], root)
    _git(["config", "user.name", "test"], root)
    (root / "README.md").write_text("# initial\n", encoding="utf-8")
    _git(["add", "README.md"], root)
    _git(["commit", "-m", "initial"], root)


# ─── workspace fixture ─────────────────────────────────────────────────────────


_WORKSPACE_TOML = (
    '[tracker]\nbackend = "jira"\n\n'
    '[tracker.jira]\ncloud_id = "x"\nproject_key = "FT"\n\n'
    '[memory]\nnamespace = "mem"\n'
    'recall_by = ["branch", "current-ticket"]\nrecall_top_n = 5\n'
)


def _init_workspace(root: Path, *, with_knowledge: bool = True) -> None:
    flow = root / ".flow"
    flow.mkdir(parents=True, exist_ok=True)
    (flow / ".initialized").write_text("", encoding="utf-8")
    (flow / "workspace.toml").write_text(_WORKSPACE_TOML, encoding="utf-8")
    if with_knowledge:
        ns = flow / "mem"
        ns.mkdir(parents=True, exist_ok=True)
        entries = [
            {
                "id": "k1",
                "type": "gotcha",
                "branch": "main",
                "ticket": "FT-1",
                "body": "distinctivealpha cooldown must be cleared before retry",
                "ts": "2026-05-01T00:00:00Z",
            },
            {
                "id": "k2",
                "type": "decision",
                "branch": "main",
                "ticket": "FT-2",
                "body": "distinctivebeta picked polars over pandas for the join",
                "ts": "2026-05-02T00:00:00Z",
            },
        ]
        (ns / "knowledge.jsonl").write_text(
            "".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8"
        )


@pytest.fixture
def flow_workspace(tmp_path: Path) -> Path:
    _init_workspace(tmp_path)
    _init_repo(tmp_path)
    return tmp_path


# ─── happy path (real runner: exercises recall.py for real) ───────────────────


def test_build_context_returns_recalled_block(flow_workspace: Path) -> None:
    block = hook.build_context(flow_workspace, flow_workspace)
    assert block.startswith("## /flow recall")
    # entries surface (recall returns top_n regardless of BM25 text overlap).
    assert "distinctivealpha" in block or "distinctivebeta" in block
    assert "gotcha" in block or "decision" in block


def test_records_recall_pending(flow_workspace: Path) -> None:
    hook.build_context(flow_workspace, flow_workspace)
    pending = flow_workspace / ".flow" / "recall-pending.jsonl"
    assert pending.exists()
    lines = [json.loads(line) for line in pending.read_text().splitlines() if line.strip()]
    assert lines
    assert any(rec.get("branch") == "main" for rec in lines)


def test_find_workspace_root_walks_up(flow_workspace: Path) -> None:
    nested = flow_workspace / "src" / "deep"
    nested.mkdir(parents=True)
    assert hook.find_workspace_root(nested) == flow_workspace


def test_ticket_branch_runs_both_queries(tmp_path: Path) -> None:
    """On a ticket-bearing branch the current-ticket query fires too: the second
    recall + cross-query dedupe + the ticket-stamped pending record all run.
    """
    _init_workspace(tmp_path)
    # branch_ticket.py matches FT-\d+ -> resolved_ticket == "FT-1".
    _git(["init", "--initial-branch=FT-1-add-cooldown"], tmp_path)
    _git(["config", "user.email", "test@example.com"], tmp_path)
    _git(["config", "user.name", "test"], tmp_path)
    (tmp_path / "README.md").write_text("# initial\n", encoding="utf-8")
    _git(["add", "README.md"], tmp_path)
    _git(["commit", "-m", "initial"], tmp_path)

    block = hook.build_context(tmp_path, tmp_path)
    assert block.startswith("## /flow recall")
    # both entries surface; dedupe by id keeps each once across the two queries.
    assert block.count("- ") == 2
    assert "distinctivealpha" in block and "distinctivebeta" in block

    # both queries record pending. pending_id omits the query and the hook
    # self-stamps hook_observed_at at second precision, so two appends in the
    # same wall-clock second collapse to one record; >= 1 keeps this robust.
    pending = tmp_path / ".flow" / "recall-pending.jsonl"
    lines = [json.loads(line) for line in pending.read_text().splitlines() if line.strip()]
    assert len(lines) >= 1
    assert all(rec.get("hook_time_resolved_ticket") == "FT-1" for rec in lines)


# ─── non-flow dir returns empty ────────────────────────────────────────────────


def test_non_flow_dir_returns_empty(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    assert hook.find_workspace_root(tmp_path) is None
    # build_context also short-circuits when workspace.toml is absent.
    assert hook.build_context(tmp_path, tmp_path) == ""


def test_cli_main_silent_outside_workspace(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    assert hook.cli_main([str(tmp_path)]) == 0
    assert capsys.readouterr().out == ""


# ─── git / recall failure returns empty (no exception) ─────────────────────────


def _failing_git_runner(workspace_root: Path):
    """Runner that fails every git call; passes python scripts straight through."""

    def run(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        if args and args[0] == "git":
            return subprocess.CompletedProcess(args, 128, "", "fatal: not a git repository")
        return subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, check=False)

    return run


def test_git_failure_returns_empty(flow_workspace: Path) -> None:
    runner = _failing_git_runner(flow_workspace)
    assert hook.build_context(flow_workspace, flow_workspace, runner) == ""


def test_recall_failure_returns_empty(flow_workspace: Path) -> None:
    """git succeeds, recall.py fails -> no entries -> empty block, no raise."""

    def run(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        if args and args[0] == "git":
            real = subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, check=False)
            return real
        if "recall.py" in " ".join(args):
            return subprocess.CompletedProcess(args, 1, "", "recall: boom")
        return subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, check=False)

    assert hook.build_context(flow_workspace, flow_workspace, run) == ""


def test_runner_raising_does_not_crash(
    flow_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def raising_runner():
        def run(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
            raise RuntimeError("subprocess blew up")

        return run

    # cli_main is the outer net: any exception from the runner -> exit 0, silent.
    monkeypatch.setattr(hook, "_default_runner", raising_runner)
    assert hook.cli_main([str(flow_workspace)]) == 0
