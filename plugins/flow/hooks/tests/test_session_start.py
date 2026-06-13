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
from datetime import UTC, datetime, timedelta
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


# ─── supersession exclusion via the REAL recall.py subprocess ──────────────────


def test_build_context_excludes_superseded_entry(tmp_path: Path) -> None:
    """The hook reaches knowledge.jsonl only through a recall.py subprocess, so the
    default supersession exclusion applies transitively. Seed a pair X<-Y (both
    branch=main, both would surface absent the filter); Y supersedes X. The
    rendered block must contain Y's marker and omit X's marker.
    """
    _init_workspace(tmp_path, with_knowledge=False)
    ns = tmp_path / ".flow" / "mem"
    ns.mkdir(parents=True, exist_ok=True)
    entries = [
        {
            "id": "supx",
            "type": "gotcha",
            "branch": "main",
            "ticket": "FT-1",
            "body": "supersededmarkerxxx stale claim about cooldown",
            "ts": "2026-05-01T00:00:00Z",
        },
        {
            "id": "supy",
            "type": "gotcha",
            "branch": "main",
            "ticket": "FT-1",
            "body": "survivormarkeryyy corrected claim about cooldown",
            "ts": "2026-05-02T00:00:00Z",
            "supersedes": "supx",
        },
    ]
    (ns / "knowledge.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8"
    )
    _init_repo(tmp_path)

    block = hook.build_context(tmp_path, tmp_path)
    assert block.startswith("## /flow recall")
    assert "survivormarkeryyy" in block
    assert "supersededmarkerxxx" not in block


# ─── evolve-loop staleness (deadman) ───────────────────────────────────────────


def _now() -> datetime:
    return datetime(2026, 6, 11, 12, 0, 0, tzinfo=UTC)


def _write_record(path: Path, *rows: dict) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _ts(now: datetime, **delta: float) -> str:
    return (now - timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_staleness_absent_file_is_silent(tmp_path: Path) -> None:
    assert hook.staleness_block(tmp_path / "missing.jsonl", _now()) == ""


def test_staleness_fresh_runs_are_silent(tmp_path: Path) -> None:
    now = _now()
    rec = tmp_path / "run-record.jsonl"
    _write_record(
        rec,
        {"schedule": "nightly", "phase": "end", "ts": _ts(now, hours=10), "outcome": "ok"},
        {"schedule": "weekly", "phase": "end", "ts": _ts(now, days=3), "outcome": "ok"},
    )
    assert hook.staleness_block(rec, now) == ""


def test_staleness_nightly_stale_warns(tmp_path: Path) -> None:
    now = _now()
    rec = tmp_path / "run-record.jsonl"
    _write_record(
        rec, {"schedule": "nightly", "phase": "end", "ts": _ts(now, hours=40), "outcome": "ok"}
    )
    block = hook.staleness_block(rec, now)
    assert block.startswith("## /flow ops")
    assert "nightly evolve loop stale" in block
    assert ">36h" in block


def test_staleness_weekly_stale_warns(tmp_path: Path) -> None:
    now = _now()
    rec = tmp_path / "run-record.jsonl"
    _write_record(
        rec, {"schedule": "weekly", "phase": "end", "ts": _ts(now, days=9), "outcome": "ok"}
    )
    block = hook.staleness_block(rec, now)
    assert "weekly epic loop stale" in block
    assert ">8d" in block


def test_staleness_uses_latest_record_per_schedule(tmp_path: Path) -> None:
    """A fresh end record after an old start record clears the warning."""
    now = _now()
    rec = tmp_path / "run-record.jsonl"
    _write_record(
        rec,
        {"schedule": "nightly", "phase": "start", "ts": _ts(now, hours=50), "outcome": ""},
        {"schedule": "nightly", "phase": "end", "ts": _ts(now, hours=2), "outcome": "ok"},
    )
    assert hook.staleness_block(rec, now) == ""


def test_staleness_tolerates_garbage_lines(tmp_path: Path) -> None:
    now = _now()
    rec = tmp_path / "run-record.jsonl"
    rec.write_text(
        "not json\n"
        + json.dumps({"schedule": "nightly", "ts": "garbage-ts"})
        + "\n"
        + json.dumps({"schedule": "nightly", "phase": "end", "ts": _ts(now, hours=40)})
        + "\n",
        encoding="utf-8",
    )
    block = hook.staleness_block(rec, now)
    assert "nightly evolve loop stale" in block


def test_staleness_fail_outcome_warns(tmp_path: Path) -> None:
    """A latest `end` with outcome=fail (trap-EXIT crash-capture) fires a warning."""
    now = _now()
    rec = tmp_path / "run-record.jsonl"
    _write_record(
        rec,
        {"schedule": "nightly", "phase": "start", "ts": _ts(now, hours=2), "outcome": ""},
        {"schedule": "nightly", "phase": "end", "ts": _ts(now, hours=1), "outcome": "fail"},
    )
    block = hook.staleness_block(rec, now)
    assert block.startswith("## /flow ops")
    assert "nightly evolve" in block
    assert "fail" in block


def test_staleness_hung_start_no_end_warns(tmp_path: Path) -> None:
    """A start with no end past the nightly 3h grace reads as hung."""
    now = _now()
    rec = tmp_path / "run-record.jsonl"
    _write_record(
        rec, {"schedule": "nightly", "phase": "start", "ts": _ts(now, hours=4), "outcome": ""}
    )
    block = hook.staleness_block(rec, now)
    assert block.startswith("## /flow ops")
    assert "nightly evolve" in block
    assert "hung" in block


def test_staleness_hung_within_grace_is_silent(tmp_path: Path) -> None:
    """A start within the nightly 3h grace is an in-flight run, not a warning."""
    now = _now()
    rec = tmp_path / "run-record.jsonl"
    _write_record(
        rec, {"schedule": "nightly", "phase": "start", "ts": _ts(now, hours=1), "outcome": ""}
    )
    assert hook.staleness_block(rec, now) == ""


def test_staleness_hung_discriminates_from_pr266_dead_branch(tmp_path: Path) -> None:
    """A new pending start AFTER a prior completed run reads hung, not stale.

    This is the harvest's improvement over the closed PR #266: its hung branch
    keyed on `last_end is None`, so an accumulating record with any prior `end`
    never fired hung and would mis-report the old `end` as stale. The fix keys on
    `last_start > last_end`, so a fresh hung start is caught even with old ends present.
    """
    now = _now()
    rec = tmp_path / "run-record.jsonl"
    _write_record(
        rec,
        {"schedule": "nightly", "phase": "start", "ts": _ts(now, hours=50), "outcome": ""},
        {"schedule": "nightly", "phase": "end", "ts": _ts(now, hours=49), "outcome": "ok"},
        {"schedule": "nightly", "phase": "start", "ts": _ts(now, hours=4), "outcome": ""},
    )
    block = hook.staleness_block(rec, now)
    assert "hung" in block
    assert "stale" not in block


def test_staleness_weekly_hung_grace_is_separate(tmp_path: Path) -> None:
    """Weekly uses a 6h zombie grace, distinct from nightly's 3h."""
    now = _now()
    rec = tmp_path / "run-record.jsonl"
    _write_record(
        rec, {"schedule": "weekly", "phase": "start", "ts": _ts(now, hours=7), "outcome": ""}
    )
    block = hook.staleness_block(rec, now)
    assert "weekly epic" in block
    assert "hung" in block


def test_staleness_weekly_hung_within_grace_is_silent(tmp_path: Path) -> None:
    now = _now()
    rec = tmp_path / "run-record.jsonl"
    _write_record(
        rec, {"schedule": "weekly", "phase": "start", "ts": _ts(now, hours=5), "outcome": ""}
    )
    assert hook.staleness_block(rec, now) == ""


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
