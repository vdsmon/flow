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


def _pr(
    num: int,
    key: str,
    *,
    rollup=GREEN,
    state: str = "CLEAN",
    draft: bool = False,
    files: list[str] | None = None,
) -> dict:
    # files default [] keeps every existing PR non-hot (the regression baseline);
    # gh `pr list --json files` shape is [{"path": ...}].
    return {
        "number": num,
        "headRefName": f"feature/{key}-some-desc",
        "isDraft": draft,
        "mergeStateStatus": state,
        "statusCheckRollup": rollup,
        "files": [{"path": p} for p in (files or [])],
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
        {
            "pr": 1,
            "key": "flow-a",
            "branch": "feature/flow-a-some-desc",
            "is_draft": False,
            "is_hot": False,
        }
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


def test_green_nonhot_dirty_is_version_recoverable():
    prs = [_pr(1, "flow-a", state="DIRTY")]
    out = er.classify(prs, _idx(**{"flow-a": ["evolve"]}))
    assert out["version_recoverable"] == [
        {"pr": 1, "key": "flow-a", "branch": "feature/flow-a-some-desc"}
    ]
    assert out["blocked"] == []


def test_hot_dirty_is_blocked_not_recoverable():
    # hot never auto-recovers: a green hot DIRTY PR is blocked, NOT version_recoverable
    # and NOT skipped_hot (skipped_hot means green+mergeable awaiting isolation).
    prs = [_pr(1, "flow-h", state="DIRTY")]
    out = er.classify(prs, _idx(**{"flow-h": ["evolve", "hot"]}))
    assert out["blocked"] == [
        {"pr": 1, "key": "flow-h", "branch": "feature/flow-h-some-desc", "reason": "DIRTY"}
    ]
    assert out["version_recoverable"] == []
    assert out["skipped_hot"] == []


# ---- classify: guard-file hotness (no `hot` label) ----


def test_guard_file_dirty_no_label_is_blocked_not_recoverable():
    # the flow-1fy bug: a guard-file PR (snapshot.py) DIRTY with no `hot` label must
    # be treated as hot (blocked, never auto-recovered), not version_recoverable.
    prs = [_pr(1, "flow-a", state="DIRTY", files=["snapshot.py"])]
    out = er.classify(prs, _idx(**{"flow-a": ["evolve"]}))
    assert out["blocked"] == [
        {"pr": 1, "key": "flow-a", "branch": "feature/flow-a-some-desc", "reason": "DIRTY"}
    ]
    assert out["version_recoverable"] == []


def test_non_guard_dirty_no_label_still_version_recoverable():
    # no over-broadening: a non-guard DIRTY PR with no label stays version_recoverable.
    prs = [_pr(1, "flow-a", state="DIRTY", files=["evolve_reap.py"])]
    out = er.classify(prs, _idx(**{"flow-a": ["evolve"]}))
    assert out["version_recoverable"] == [
        {"pr": 1, "key": "flow-a", "branch": "feature/flow-a-some-desc"}
    ]
    assert out["blocked"] == []


def test_guard_file_green_clean_skipped_hot_when_off():
    prs = [_pr(1, "flow-a", files=["lease.py"])]
    out = er.classify(prs, _idx(**{"flow-a": ["evolve"]}))
    assert out["merge"] == []
    assert out["skipped_hot"] == [{"pr": 1, "key": "flow-a", "branch": "feature/flow-a-some-desc"}]


def test_guard_file_promotes_with_is_hot_true():
    prs = [_pr(1, "flow-a", files=["state.py"])]
    out = er.classify(prs, _idx(**{"flow-a": ["evolve"]}), auto_merge_hot=True)
    assert out["merge"] == [
        {
            "pr": 1,
            "key": "flow-a",
            "branch": "feature/flow-a-some-desc",
            "is_draft": False,
            "is_hot": True,
        }
    ]
    assert out["skipped_hot"] == []


def test_guard_file_and_label_hot_serialize():
    # the guard-file PR (no label) joins the isolation count: two hot-eligible PRs
    # this pass → neither promotes, both land in skipped_hot.
    prs = [_pr(1, "flow-a", files=["dispatch_stage.py"]), _pr(2, "flow-h")]
    out = er.classify(
        prs, _idx(**{"flow-a": ["evolve"], "flow-h": ["evolve", "hot"]}), auto_merge_hot=True
    )
    assert out["merge"] == []
    assert out["skipped_hot"] == [
        {"pr": 1, "key": "flow-a", "branch": "feature/flow-a-some-desc"},
        {"pr": 2, "key": "flow-h", "branch": "feature/flow-h-some-desc"},
    ]


def test_green_clean_still_merges():
    prs = [_pr(1, "flow-a", state="CLEAN")]
    out = er.classify(prs, _idx(**{"flow-a": ["evolve"]}))
    assert out["merge"][0]["key"] == "flow-a"
    assert out["version_recoverable"] == []


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
        {
            "pr": 1,
            "key": "flow-a",
            "branch": "feature/flow-a-some-desc",
            "is_draft": True,
            "is_hot": False,
        }
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
    assert all(
        out[b] == []
        for b in ("merge", "not_green", "skipped_hot", "version_recoverable", "blocked")
    )


# ---- classify: auto_merge_hot ----


def test_hot_auto_merge_single_clean():
    prs = [_pr(1, "flow-h")]
    out = er.classify(prs, _idx(**{"flow-h": ["evolve", "hot"]}), auto_merge_hot=True)
    assert out["merge"] == [
        {
            "pr": 1,
            "key": "flow-h",
            "branch": "feature/flow-h-some-desc",
            "is_draft": False,
            "is_hot": True,
        }
    ]
    assert out["skipped_hot"] == []


def test_hot_auto_merge_single_draft_carries_is_draft():
    prs = [_pr(1, "flow-h", state="DRAFT", draft=True)]
    out = er.classify(prs, _idx(**{"flow-h": ["evolve", "hot"]}), auto_merge_hot=True)
    assert out["merge"] == [
        {
            "pr": 1,
            "key": "flow-h",
            "branch": "feature/flow-h-some-desc",
            "is_draft": True,
            "is_hot": True,
        }
    ]
    assert out["skipped_hot"] == []


def test_hot_auto_merge_two_eligible_serialize():
    prs = [_pr(1, "flow-h"), _pr(2, "flow-g")]
    out = er.classify(
        prs, _idx(**{"flow-h": ["evolve", "hot"], "flow-g": ["evolve", "hot"]}), auto_merge_hot=True
    )
    assert out["merge"] == []
    assert out["skipped_hot"] == [
        {"pr": 1, "key": "flow-h", "branch": "feature/flow-h-some-desc"},
        {"pr": 2, "key": "flow-g", "branch": "feature/flow-g-some-desc"},
    ]


def test_hot_auto_merge_clean_promotes_dirty_blocks():
    # the unpromoted hot PR is DIRTY (conflicted), so it is blocked, not skipped_hot:
    # hot never auto-recovers, and skipped_hot is reserved for green+mergeable hots
    # held back only by the one-hot-per-pass isolation.
    prs = [_pr(1, "flow-h"), _pr(2, "flow-g", state="DIRTY")]
    out = er.classify(
        prs, _idx(**{"flow-h": ["evolve", "hot"], "flow-g": ["evolve", "hot"]}), auto_merge_hot=True
    )
    assert out["merge"] == [
        {
            "pr": 1,
            "key": "flow-h",
            "branch": "feature/flow-h-some-desc",
            "is_draft": False,
            "is_hot": True,
        }
    ]
    assert out["skipped_hot"] == []
    assert out["blocked"] == [
        {"pr": 2, "key": "flow-g", "branch": "feature/flow-g-some-desc", "reason": "DIRTY"}
    ]


def test_hot_auto_merge_does_not_gate_non_hot_leaf():
    prs = [_pr(1, "flow-h"), _pr(2, "flow-a")]
    out = er.classify(
        prs, _idx(**{"flow-h": ["evolve", "hot"], "flow-a": ["evolve"]}), auto_merge_hot=True
    )
    assert out["merge"] == [
        {
            "pr": 1,
            "key": "flow-h",
            "branch": "feature/flow-h-some-desc",
            "is_draft": False,
            "is_hot": True,
        },
        {
            "pr": 2,
            "key": "flow-a",
            "branch": "feature/flow-a-some-desc",
            "is_draft": False,
            "is_hot": False,
        },
    ]
    assert out["skipped_hot"] == []


def test_hot_auto_merge_off_by_default_still_skips():
    prs = [_pr(1, "flow-h")]
    out = er.classify(prs, _idx(**{"flow-h": ["evolve", "hot"]}))
    assert out["merge"] == []
    assert out["skipped_hot"] == [{"pr": 1, "key": "flow-h", "branch": "feature/flow-h-some-desc"}]


def test_merge_entries_flag_is_hot():
    # the reap loop runs the guard property-check only on is_hot entries, so each
    # merge entry must say whether it was a hot promotion or a plain leaf.
    prs = [_pr(1, "flow-h"), _pr(2, "flow-a")]
    out = er.classify(
        prs, _idx(**{"flow-h": ["evolve", "hot"], "flow-a": ["evolve"]}), auto_merge_hot=True
    )
    assert {e["key"]: e["is_hot"] for e in out["merge"]} == {"flow-h": True, "flow-a": False}


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
        {
            "pr": 1,
            "key": "flow-a",
            "branch": "feature/flow-a-some-desc",
            "is_draft": False,
            "is_hot": False,
        }
    ]
    assert out["skipped_hot"] == [{"pr": 2, "key": "flow-h", "branch": "feature/flow-h-some-desc"}]
    assert out["not_green"] == [{"pr": 3, "key": "flow-b", "branch": "feature/flow-b-some-desc"}]


def test_reap_requests_files_field(tmp_path):
    # classify reads guard-file hotness off pr["files"], so reap must request it.
    ws = _marked_ws(tmp_path)
    run, calls = _dispatch(prs=[], evolve_list=[])
    er.reap(ws, runner=run)
    pr_list = next(a for a in calls if a[:3] == ["gh", "pr", "list"])
    json_fields = pr_list[pr_list.index("--json") + 1]
    assert "files" in json_fields.split(",")


def _auto_merge_hot_ws(tmp_path: Path) -> Path:
    d = tmp_path / "flow"
    (d / ".flow").mkdir(parents=True)
    (d / ".flow" / "workspace.toml").write_text(
        "[maintainer]\nself_target = true\n[evolve]\nauto_merge_hot = true\n", encoding="utf-8"
    )
    return d


def test_reap_auto_merge_hot_from_config(tmp_path):
    ws = _auto_merge_hot_ws(tmp_path)
    run, _ = _dispatch(
        prs=[_pr(1, "flow-h")],
        evolve_list=[{"id": "flow-h", "labels": ["evolve", "hot"]}],
    )
    out = er.reap(ws, runner=run)
    assert out["merge"] == [
        {
            "pr": 1,
            "key": "flow-h",
            "branch": "feature/flow-h-some-desc",
            "is_draft": False,
            "is_hot": True,
        }
    ]
    assert out["skipped_hot"] == []


def test_reap_not_maintainer(tmp_path, monkeypatch):
    monkeypatch.setattr("maintainer._global_config_path", lambda: tmp_path / "absent.toml")
    plain = tmp_path / "proj"
    (plain / ".flow").mkdir(parents=True)
    (plain / ".flow" / "workspace.toml").write_text(
        '[tracker]\nbackend = "beads"\n', encoding="utf-8"
    )
    with pytest.raises(er.NotMaintainer):
        er.reap(plain)


# ---- _labels_index / reap: include_proposals ----


def _label_aware_list_runner(
    *, prs: list[dict], by_label: dict[str, list[dict]]
) -> tuple[Callable[..., subprocess.CompletedProcess[str]], Recorder]:
    """`bd list -l <label>` returns the per-label fixture; pr list returns `prs`."""
    calls: Recorder = []

    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[:3] == ["gh", "pr", "list"]:
            return subprocess.CompletedProcess(args, 0, json.dumps(prs), "")
        if args[:2] == ["bd", "list"]:
            label = args[args.index("-l") + 1]
            return subprocess.CompletedProcess(args, 0, json.dumps(by_label.get(label, [])), "")
        return subprocess.CompletedProcess(args, 1, "", f"unexpected: {args}")

    return run, calls


def test_labels_index_default_evolve_only():
    run, calls = _label_aware_list_runner(
        prs=[], by_label={"evolve": [{"id": "flow-a", "labels": ["evolve"]}]}
    )
    index = er._labels_index(run)
    assert index == {"flow-a": ["evolve"]}
    assert not any(a[:2] == ["bd", "list"] and "proposal" in a for a in calls)


def test_labels_index_include_proposals_merges_both():
    run, _ = _label_aware_list_runner(
        prs=[],
        by_label={
            "evolve": [{"id": "flow-a", "labels": ["evolve"]}],
            "proposal": [{"id": "flow-prop", "labels": ["proposal"]}],
        },
    )
    index = er._labels_index(run, include_proposals=True)
    assert index == {"flow-a": ["evolve"], "flow-prop": ["proposal"]}


def test_reap_include_proposals_reaps_proposal_orphan(tmp_path):
    # a proposal orphan (run died before self-merging) only reaps under the flag;
    # without it the PR's key is absent from the label index and classify skips it.
    ws = _marked_ws(tmp_path)
    by_label = {
        "evolve": [{"id": "flow-a", "labels": ["evolve"]}],
        "proposal": [{"id": "flow-prop", "labels": ["proposal"]}],
    }
    prs = [_pr(1, "flow-a"), _pr(2, "flow-prop")]

    run, _ = _label_aware_list_runner(prs=prs, by_label=by_label)
    off = er.reap(ws, runner=run)
    assert {e["key"] for e in off["merge"]} == {"flow-a"}  # proposal orphan invisible

    run, _ = _label_aware_list_runner(prs=prs, by_label=by_label)
    on = er.reap(ws, runner=run, include_proposals=True)
    assert {e["key"] for e in on["merge"]} == {"flow-a", "flow-prop"}
