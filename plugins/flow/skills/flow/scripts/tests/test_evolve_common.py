from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import _evolve_common as ec
import lease
from _timeutil import utcnow_iso


def test_ok_returns_stdout():
    cp = subprocess.CompletedProcess(["x"], 0, "out", "")
    assert ec.ok(cp, "x") == "out"


def test_ok_raises_tool_error_with_context():
    cp = subprocess.CompletedProcess(["x"], 1, "", "boom")
    with pytest.raises(ec.ToolError, match="gh pr list failed: boom"):
        ec.ok(cp, "gh pr list")


def test_loads_tolerates_garbage_and_dict_shapes():
    assert ec.loads("") == []
    assert ec.loads("{not json") == []
    assert ec.loads('[{"a": 1}]') == [{"a": 1}]
    assert ec.loads('{"issues": [{"id": "flow-a"}]}') == [{"id": "flow-a"}]
    assert ec.loads('{"prs": [{"number": 7}]}') == [{"number": 7}]
    assert ec.loads('{"other": 1}') == []


def test_key_from_ref():
    assert ec.key_from_ref("feature/flow-7mb-evolve-verb") == "flow-7mb"
    assert ec.key_from_ref("origin/feature/flow-aut.6-fix") == "flow-aut.6"
    assert ec.key_from_ref("feature/flow-abc") == "flow-abc"
    assert ec.key_from_ref("main") is None


def test_bead_labels():
    assert ec.bead_labels(False) == ["evolve"]
    assert ec.bead_labels(True) == ["evolve", "proposal"]


def test_run_dir_for_absent_returns_none(tmp_path):
    repo = tmp_path / "flow"
    repo.mkdir()
    assert ec.run_dir_for(repo, "flow-nope") is None


def test_run_dir_for_finds_pool_worktree(tmp_path):
    repo = tmp_path / "flow"
    run_dir = repo / ".flow" / "worktrees" / "feature-flow-abc-slug" / ".flow" / "runs" / "flow-abc"
    run_dir.mkdir(parents=True)
    assert ec.run_dir_for(repo, "flow-abc") == run_dir


# ---- extracted selector helpers (shared by evolve_select + queue_select) ----


def test_active_statuses_constant():
    assert ec.ACTIVE_STATUSES == "open,in_progress,blocked"


def test_primary_anchor_first_path():
    desc = "EVIDENCE\nBLAST RADIUS: a/b.py, c/d.py, e.py\nVALUE"
    assert ec.primary_anchor(desc) == "a/b.py"


def test_primary_anchor_absent():
    assert ec.primary_anchor("no blast radius here") is None
    assert ec.primary_anchor(None) is None


def test_is_inflight_prefix_match():
    refs = {"feature/flow-a-some-desc"}
    assert ec.is_inflight("flow-a", refs)
    assert not ec.is_inflight("flow-ab", refs)  # must not prefix-bleed


def test_gather_refs_returns_refs_and_pr_refs():
    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        if args[:3] == ["gh", "pr", "list"]:
            prs = [{"headRefName": "feature/flow-pr-wip"}, {"headRefName": "main"}]
            return subprocess.CompletedProcess(args, 0, json.dumps(prs), "")
        if args[:2] == ["git", "for-each-ref"]:
            return subprocess.CompletedProcess(args, 0, "origin/feature/flow-br-wip\nmain\n", "")
        return subprocess.CompletedProcess(args, 1, "", f"unexpected: {args}")

    refs, pr_refs = ec.gather_refs(run)
    assert refs == {"feature/flow-pr-wip", "feature/flow-br-wip", "main"}
    assert pr_refs == {"feature/flow-pr-wip", "main"}


def test_gather_refs_tool_error():
    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 1, "", "gh boom")

    with pytest.raises(ec.ToolError):
        ec.gather_refs(run)


def _pool_run_dir(repo: Path, key: str) -> Path:
    return repo / ".flow" / "worktrees" / f"feature-{key}-wip" / ".flow" / "runs" / key


def _write_lease(run_dir: Path, *, expired: bool = False) -> None:
    now = "2020-01-01T00:00:00Z" if expired else utcnow_iso()
    ttl = 1 if expired else 3600
    lease.acquire(
        run_dir,
        "run-test",
        ttl,
        now,
        stage="implement",
        current_boot="boot-A",
        hostname="host-1",
        cwd=str(run_dir),
    )


def test_live_run_keys_finds_live_lease(tmp_path):
    repo = tmp_path / "flow"
    repo.mkdir()
    _write_lease(_pool_run_dir(repo, "flow-x"))
    assert ec.live_run_keys(repo) == {"flow-x"}


def test_live_run_keys_skips_expired(tmp_path):
    repo = tmp_path / "flow"
    repo.mkdir()
    _write_lease(_pool_run_dir(repo, "flow-x"), expired=True)
    assert ec.live_run_keys(repo) == set()


def test_live_run_keys_empty_when_no_worktrees(tmp_path):
    repo = tmp_path / "flow"
    repo.mkdir()
    assert ec.live_run_keys(repo) == set()
