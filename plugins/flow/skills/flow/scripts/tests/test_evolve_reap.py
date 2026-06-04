from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

import evolve_reap as er

Recorder = list[list[str]]

GREEN = [{"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS", "name": "test"}]
PENDING = [{"__typename": "CheckRun", "status": "IN_PROGRESS", "name": "test"}]
FAILING = [
    {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "FAILURE", "name": "test"}
]


def _pr(num: int, key: str, *, rollup=GREEN, state: str = "CLEAN", draft: bool = False) -> dict:
    return {
        "number": num,
        "headRefName": f"feature/{key}-some-desc",
        "isDraft": draft,
        "mergeStateStatus": state,
        "statusCheckRollup": rollup,
    }


def _idx(**keys: list[str]) -> dict[str, list[str]]:
    return dict(keys.items())


# ---- rollup_is_green ----


def test_rollup_green():
    assert er.rollup_is_green(GREEN)


def test_rollup_pending_not_green():
    assert not er.rollup_is_green(PENDING)


def test_rollup_failing_not_green():
    assert not er.rollup_is_green(FAILING)


def test_rollup_empty_not_green():
    assert not er.rollup_is_green([])


def test_rollup_status_context_shape():
    assert er.rollup_is_green([{"__typename": "StatusContext", "state": "SUCCESS"}])
    assert not er.rollup_is_green([{"__typename": "StatusContext", "state": "PENDING"}])


# ---- classify ----


def test_green_clean_leaf_merges():
    prs = [_pr(1, "flow-a")]
    out = er.classify(prs, _idx(**{"flow-a": ["evolve", "audit"]}))
    assert out["merge"] == [
        {"pr": 1, "key": "flow-a", "branch": "feature/flow-a-some-desc", "is_draft": False}
    ]


def test_hot_bead_skipped_even_when_green():
    prs = [_pr(1, "flow-h")]
    out = er.classify(prs, _idx(**{"flow-h": ["evolve", "hot"]}))
    assert out["merge"] == []
    assert out["skipped_hot"] == [{"pr": 1, "key": "flow-h", "branch": "feature/flow-h-some-desc"}]


def test_pending_is_not_green():
    prs = [_pr(1, "flow-a", rollup=PENDING)]
    out = er.classify(prs, _idx(**{"flow-a": ["evolve"]}))
    assert out["not_green"] == [{"pr": 1, "key": "flow-a", "branch": "feature/flow-a-some-desc"}]
    assert out["merge"] == []


def test_dirty_is_blocked():
    prs = [_pr(1, "flow-a", state="DIRTY")]
    out = er.classify(prs, _idx(**{"flow-a": ["evolve"]}))
    assert out["blocked"] == [
        {"pr": 1, "key": "flow-a", "branch": "feature/flow-a-some-desc", "reason": "DIRTY"}
    ]


def test_behind_is_blocked():
    prs = [_pr(1, "flow-a", state="BEHIND")]
    out = er.classify(prs, _idx(**{"flow-a": ["evolve"]}))
    assert out["blocked"] == [
        {"pr": 1, "key": "flow-a", "branch": "feature/flow-a-some-desc", "reason": "BEHIND"}
    ]


def test_draft_but_green_is_mergeable():
    prs = [_pr(1, "flow-a", state="DRAFT", draft=True)]
    out = er.classify(prs, _idx(**{"flow-a": ["evolve"]}))
    assert out["merge"] == [
        {"pr": 1, "key": "flow-a", "branch": "feature/flow-a-some-desc", "is_draft": True}
    ]


def test_merge_entry_carries_branch():
    # the reap loop tears down the local branch + worktree; it needs headRefName.
    prs = [_pr(7, "flow-a")]
    out = er.classify(prs, _idx(**{"flow-a": ["evolve"]}))
    assert out["merge"][0]["branch"] == "feature/flow-a-some-desc"


def test_non_flow_branch_ignored():
    prs = [
        {
            "number": 9,
            "headRefName": "dependabot/pip/x",
            "mergeStateStatus": "CLEAN",
            "statusCheckRollup": GREEN,
        }
    ]
    out = er.classify(prs, _idx(**{"flow-a": ["evolve"]}))
    assert out["merge"] == [] and out["not_green"] == []


def test_unknown_key_ignored():
    prs = [_pr(1, "flow-ghost")]
    out = er.classify(prs, _idx(**{"flow-a": ["evolve"]}))
    assert all(out[b] == [] for b in ("merge", "not_green", "skipped_hot", "blocked"))


# ---- reap integration ----


def _marked_ws(tmp_path: Path) -> Path:
    d = tmp_path / "flow"
    (d / ".flow").mkdir(parents=True)
    (d / ".flow" / "workspace.toml").write_text(
        "[maintainer]\nself_target = true\n", encoding="utf-8"
    )
    return d


def _dispatch(
    *, prs: list[dict], evolve_list: list[dict]
) -> tuple[Callable[..., subprocess.CompletedProcess[str]], Recorder]:
    calls: Recorder = []

    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[:3] == ["gh", "pr", "list"]:
            return subprocess.CompletedProcess(args, 0, json.dumps(prs), "")
        if args[:2] == ["bd", "list"]:
            return subprocess.CompletedProcess(args, 0, json.dumps(evolve_list), "")
        return subprocess.CompletedProcess(args, 1, "", f"unexpected: {args}")

    return run, calls


def test_reap_integration(tmp_path):
    ws = _marked_ws(tmp_path)
    run, _ = _dispatch(
        prs=[_pr(1, "flow-a"), _pr(2, "flow-h"), _pr(3, "flow-b", rollup=PENDING)],
        evolve_list=[
            {"id": "flow-a", "labels": ["evolve"]},
            {"id": "flow-h", "labels": ["evolve", "hot"]},
            {"id": "flow-b", "labels": ["evolve"]},
        ],
    )
    out = er.reap(ws, runner=run)
    assert out["merge"] == [
        {"pr": 1, "key": "flow-a", "branch": "feature/flow-a-some-desc", "is_draft": False}
    ]
    assert out["skipped_hot"] == [{"pr": 2, "key": "flow-h", "branch": "feature/flow-h-some-desc"}]
    assert out["not_green"] == [{"pr": 3, "key": "flow-b", "branch": "feature/flow-b-some-desc"}]


def test_reap_not_maintainer(tmp_path, monkeypatch):
    monkeypatch.setattr("maintainer._global_config_path", lambda: tmp_path / "absent.toml")
    plain = tmp_path / "proj"
    (plain / ".flow").mkdir(parents=True)
    (plain / ".flow" / "workspace.toml").write_text(
        '[tracker]\nbackend = "beads"\n', encoding="utf-8"
    )
    with pytest.raises(er.NotMaintainer):
        er.reap(plain)
