from __future__ import annotations

import subprocess

import pytest

import _evolve_common as ec


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
