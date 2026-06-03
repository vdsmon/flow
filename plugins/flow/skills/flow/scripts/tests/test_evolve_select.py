from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

import evolve_select as es

Recorder = list[list[str]]


def _cand(
    key: str,
    *,
    priority: int = 2,
    labels: list[str] | None = None,
    blast: str | None = None,
    issue_type: str = "task",
) -> dict:
    desc = f"some evidence\nBLAST RADIUS: {blast}\nmore" if blast else "no blast line"
    return {
        "id": key,
        "priority": priority,
        "labels": labels or ["evolve", "audit"],
        "issue_type": issue_type,
        "description": desc,
    }


# ---- pure partition ----


def test_all_leaf_fans_out_to_concurrency():
    cands = [_cand(f"flow-{i}") for i in range(5)]
    out = es.partition(cands, set(), False, 0, cap=10, concurrency=3)
    assert len(out["launch"]) == 3
    assert out["held_backpressure"] is False


def test_budget_is_cap_minus_open_prs():
    cands = [_cand(f"flow-{i}") for i in range(5)]
    out = es.partition(cands, set(), False, open_pr_count=3, cap=5, concurrency=3)
    assert len(out["launch"]) == 2  # min(5-3, 3)


def test_hot_serialization_one_at_most():
    cands = [
        _cand("flow-h1", labels=["evolve", "hot"], blast="a.py"),
        _cand("flow-h2", labels=["evolve", "hot"], blast="b.py"),
        _cand("flow-h3", labels=["evolve", "hot"], blast="c.py"),
    ]
    out = es.partition(cands, set(), False, 0, cap=10, concurrency=5)
    assert out["launch"] == ["flow-h1"]
    assert set(out["held_hot"]) == {"flow-h2", "flow-h3"}


def test_hot_inflight_blocks_all_hot():
    cands = [_cand("flow-h1", labels=["evolve", "hot"], blast="a.py")]
    out = es.partition(cands, set(), hot_inflight=True, open_pr_count=0, cap=10, concurrency=5)
    assert out["launch"] == []
    assert out["held_hot"] == ["flow-h1"]


def test_in_flight_excluded():
    cands = [_cand("flow-a"), _cand("flow-b")]
    out = es.partition(cands, {"flow-a"}, False, 0, cap=10, concurrency=5)
    assert out["launch"] == ["flow-b"]
    assert out["skipped_in_flight"] == ["flow-a"]


def test_backpressure_empties_launch():
    cands = [_cand("flow-a")]
    out = es.partition(cands, set(), False, open_pr_count=5, cap=5, concurrency=3)
    assert out["launch"] == []
    assert out["held_backpressure"] is True


def test_anchor_collision_serializes():
    cands = [
        _cand("flow-a", priority=1, blast="plugins/flow/scripts/x.py"),
        _cand("flow-b", priority=2, blast="plugins/flow/scripts/x.py"),
        _cand("flow-c", priority=3, blast="plugins/flow/scripts/y.py"),
    ]
    out = es.partition(cands, set(), False, 0, cap=10, concurrency=5)
    assert out["launch"] == ["flow-a", "flow-c"]
    assert out["held_anchor"] == ["flow-b"]


def test_epic_is_skipped():
    cands = [_cand("flow-aut", issue_type="epic"), _cand("flow-a")]
    out = es.partition(cands, set(), False, 0, cap=10, concurrency=5)
    assert out["launch"] == ["flow-a"]


def test_priority_ranking():
    cands = [_cand("flow-lo", priority=3), _cand("flow-hi", priority=1)]
    out = es.partition(cands, set(), False, 0, cap=10, concurrency=1)
    assert out["launch"] == ["flow-hi"]


# ---- helpers ----


def test_primary_anchor_first_path():
    desc = "EVIDENCE\nBLAST RADIUS: a/b.py, c/d.py, e.py\nVALUE"
    assert es.primary_anchor(desc) == "a/b.py"


def test_primary_anchor_absent():
    assert es.primary_anchor("no blast radius here") is None


def test_key_from_ref():
    assert es._key_from_ref("feature/flow-7mb-evolve-verb") == "flow-7mb"
    assert es._key_from_ref("origin/feature/flow-aut.6-fix") == "flow-aut.6"
    assert es._key_from_ref("main") is None


def test_is_inflight_prefix_match():
    refs = {"feature/flow-a-some-desc"}
    assert es._is_inflight("flow-a", refs)
    assert not es._is_inflight("flow-ab", refs)  # must not prefix-bleed


# ---- select integration (injected runner) ----


def _marked_ws(tmp_path: Path) -> Path:
    d = tmp_path / "flow"
    (d / ".flow").mkdir(parents=True)
    (d / ".flow" / "workspace.toml").write_text(
        "[maintainer]\nself_target = true\n", encoding="utf-8"
    )
    return d


def _dispatch(
    *,
    ready: list[dict],
    prs: list[dict] | None = None,
    branches: str = "",
    evolve_list: list[dict] | None = None,
) -> tuple[Callable[..., subprocess.CompletedProcess[str]], Recorder]:
    calls: Recorder = []

    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[:2] == ["bd", "ready"]:
            return subprocess.CompletedProcess(args, 0, json.dumps(ready), "")
        if args[:2] == ["bd", "list"]:
            return subprocess.CompletedProcess(args, 0, json.dumps(evolve_list or []), "")
        if args[:3] == ["gh", "pr", "list"]:
            return subprocess.CompletedProcess(args, 0, json.dumps(prs or []), "")
        if args[:2] == ["git", "for-each-ref"]:
            return subprocess.CompletedProcess(args, 0, branches, "")
        return subprocess.CompletedProcess(args, 1, "", f"unexpected: {args}")

    return run, calls


def test_select_launches_leaves(tmp_path):
    ws = _marked_ws(tmp_path)
    run, calls = _dispatch(ready=[_cand("flow-a"), _cand("flow-b")])
    out = es.select(ws, cap=5, concurrency=3, runner=run)
    assert set(out["launch"]) == {"flow-a", "flow-b"}
    assert out["open_pr_count"] == 0
    assert ["bd", "ready", "-l", "evolve", "--json"] in calls


def test_select_drops_inflight_branch(tmp_path):
    ws = _marked_ws(tmp_path)
    run, _ = _dispatch(
        ready=[_cand("flow-a"), _cand("flow-b")],
        branches="feature/flow-a-wip\nmain\n",
    )
    out = es.select(ws, cap=5, concurrency=3, runner=run)
    assert out["launch"] == ["flow-b"]
    assert out["skipped_in_flight"] == ["flow-a"]


def test_select_hot_inflight_from_open_pr(tmp_path):
    ws = _marked_ws(tmp_path)
    run, _ = _dispatch(
        ready=[_cand("flow-new", labels=["evolve", "hot"], blast="z.py")],
        prs=[{"headRefName": "feature/flow-old-wip"}],
        evolve_list=[{"id": "flow-old", "labels": ["evolve", "hot"]}],
    )
    out = es.select(ws, cap=5, concurrency=3, runner=run)
    assert out["launch"] == []  # hot slot consumed by the in-flight hot PR
    assert out["held_hot"] == ["flow-new"]
    assert out["open_pr_count"] == 1


def test_select_not_maintainer_raises(tmp_path, monkeypatch):
    monkeypatch.setattr("maintainer._global_config_path", lambda: tmp_path / "absent.toml")
    plain = tmp_path / "proj"
    (plain / ".flow").mkdir(parents=True)
    (plain / ".flow" / "workspace.toml").write_text(
        '[tracker]\nbackend = "beads"\n', encoding="utf-8"
    )
    with pytest.raises(es.NotMaintainer):
        es.select(plain, cap=5, concurrency=3)  # raises before any runner call


def test_select_tool_error(tmp_path):
    ws = _marked_ws(tmp_path)

    def run(args):
        return subprocess.CompletedProcess(args, 1, "", "bd boom")

    with pytest.raises(es.ToolError):
        es.select(ws, cap=5, concurrency=3, runner=run)
