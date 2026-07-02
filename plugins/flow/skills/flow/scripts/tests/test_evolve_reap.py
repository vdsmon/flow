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
    commits: list[str] | None = None,
) -> dict:
    # files default [] keeps every existing PR non-hot (the regression baseline);
    # gh `pr list --json files` shape is [{"path": ...}].
    # commits default None omits the key (mirrors gh when no commits requested);
    # gh `pr list --json commits` shape is [{"messageHeadline", "messageBody", ...}].
    pr: dict = {
        "number": num,
        "headRefName": f"feature/{key}-some-desc",
        "isDraft": draft,
        "mergeStateStatus": state,
        "statusCheckRollup": rollup,
        "files": [{"path": p} for p in (files or [])],
    }
    if commits is not None:
        pr["commits"] = [
            {
                "messageHeadline": m.split("\n", 1)[0],
                "messageBody": m.split("\n", 1)[1] if "\n" in m else "",
            }
            for m in commits
        ]
    return pr


def _idx(**keys: list[str]) -> dict[str, list[str]]:
    return dict(keys.items())


def _stripped(prs: list[dict]) -> list[dict]:
    # mirror production: the bulk `gh pr list` OMITS files/commits (the GraphQL
    # node-cost rejection, flow-4dxr); a runner serves them only via `gh pr view`.
    return [{k: v for k, v in p.items() if k not in ("files", "commits")} for p in prs]


def _view_detail(args: list[str], prs: list[dict]) -> str:
    # answer `gh pr view <n> --json files,commits` from the matching PR fixture.
    number = int(args[3])
    pr = next((p for p in prs if p.get("number") == number), {})
    return json.dumps({"files": pr.get("files", []), "commits": pr.get("commits", [])})


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
            "covers": [],
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


def test_green_nonhot_dirty_is_blocked():
    # branches carry no version line, so a green non-hot DIRTY is a genuine code
    # conflict for a human: it routes to `blocked` with reason "DIRTY".
    prs = [_pr(1, "flow-a", state="DIRTY")]
    out = er.classify(prs, _idx(**{"flow-a": ["evolve"]}))
    assert out["blocked"] == [
        {"pr": 1, "key": "flow-a", "branch": "feature/flow-a-some-desc", "reason": "DIRTY"}
    ]


def test_hot_dirty_is_blocked():
    # a green hot DIRTY PR is blocked, NOT skipped_hot (skipped_hot means
    # green+mergeable awaiting isolation).
    prs = [_pr(1, "flow-h", state="DIRTY")]
    out = er.classify(prs, _idx(**{"flow-h": ["evolve", "hot"]}))
    assert out["blocked"] == [
        {"pr": 1, "key": "flow-h", "branch": "feature/flow-h-some-desc", "reason": "DIRTY"}
    ]
    assert out["skipped_hot"] == []


# ---- classify: guard-file hotness (no `hot` label) ----


def test_guard_file_dirty_no_label_is_blocked():
    # the flow-1fy bug: a guard-file PR (snapshot.py) DIRTY with no `hot` label is
    # still treated as hot. A DIRTY PR routes to `blocked` regardless of hotness.
    prs = [_pr(1, "flow-a", state="DIRTY", files=["snapshot.py"])]
    out = er.classify(prs, _idx(**{"flow-a": ["evolve"]}))
    assert out["blocked"] == [
        {"pr": 1, "key": "flow-a", "branch": "feature/flow-a-some-desc", "reason": "DIRTY"}
    ]


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
            "covers": [],
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
            "covers": [],
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
    assert all(out[b] == [] for b in ("merge", "not_green", "skipped_hot", "blocked"))


# ---- classify: lease-liveness gate (flow-ztfv) ----


def test_live_lease_holds_green_clean_nonhot():
    prs = [_pr(1, "flow-a")]
    out = er.classify(prs, _idx(**{"flow-a": ["evolve"]}), liveness={"flow-a": "live"})
    assert out["skipped_live"] == [{"pr": 1, "key": "flow-a", "branch": "feature/flow-a-some-desc"}]
    assert out["merge"] == []


def test_corrupt_lease_holds_green_clean_nonhot():
    prs = [_pr(1, "flow-a")]
    out = er.classify(prs, _idx(**{"flow-a": ["evolve"]}), liveness={"flow-a": "corrupt"})
    assert out["skipped_live"] == [{"pr": 1, "key": "flow-a", "branch": "feature/flow-a-some-desc"}]
    assert out["merge"] == []


def test_live_lease_holds_green_dirty_over_blocked():
    prs = [_pr(1, "flow-a", state="DIRTY")]
    out = er.classify(prs, _idx(**{"flow-a": ["evolve"]}), liveness={"flow-a": "live"})
    assert out["skipped_live"] == [{"pr": 1, "key": "flow-a", "branch": "feature/flow-a-some-desc"}]
    assert out["blocked"] == []


def test_live_lease_holds_green_hot_over_skipped_hot():
    prs = [_pr(1, "flow-h")]
    out = er.classify(prs, _idx(**{"flow-h": ["evolve", "hot"]}), liveness={"flow-h": "live"})
    assert out["skipped_live"] == [{"pr": 1, "key": "flow-h", "branch": "feature/flow-h-some-desc"}]
    assert out["skipped_hot"] == []
    assert out["merge"] == []


def test_live_lease_holds_green_guard_file_over_skipped_hot():
    prs = [_pr(1, "flow-a", files=["lease.py"])]
    out = er.classify(prs, _idx(**{"flow-a": ["evolve"]}), liveness={"flow-a": "live"})
    assert out["skipped_live"] == [{"pr": 1, "key": "flow-a", "branch": "feature/flow-a-some-desc"}]
    assert out["skipped_hot"] == []


def test_non_live_lease_states_merge_green_clean():
    # a non-live lease state (orphan) is reapable: the green PR merges.
    for state in ("expired_foreign", "absent", "free"):
        prs = [_pr(1, "flow-a")]
        out = er.classify(prs, _idx(**{"flow-a": ["evolve"]}), liveness={"flow-a": state})
        assert [e["key"] for e in out["merge"]] == ["flow-a"], state
        assert out["skipped_live"] == [], state


def test_liveness_none_default_keeps_legacy_merge():
    # default (omitted) liveness -> byte-identical to today: green clean merges.
    prs = [_pr(1, "flow-a")]
    out = er.classify(prs, _idx(**{"flow-a": ["evolve"]}))
    assert [e["key"] for e in out["merge"]] == ["flow-a"]
    assert out["skipped_live"] == []


def test_non_green_live_lease_stays_not_green():
    # a non-green PR lands in not_green before the liveness gate; untouched.
    prs = [_pr(1, "flow-a", rollup=PENDING)]
    out = er.classify(prs, _idx(**{"flow-a": ["evolve"]}), liveness={"flow-a": "live"})
    assert out["not_green"] == [{"pr": 1, "key": "flow-a", "branch": "feature/flow-a-some-desc"}]
    assert out["skipped_live"] == []


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
            "covers": [],
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
            "covers": [],
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
            "covers": [],
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
            "covers": [],
        },
        {
            "pr": 2,
            "key": "flow-a",
            "branch": "feature/flow-a-some-desc",
            "is_draft": False,
            "is_hot": False,
            "covers": [],
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


# ---- classify: main-CI health gate (flow-a1ti.3) ----


def test_main_red_holds_merge_leaf():
    # a green non-hot leaf that would merge routes into held_main_red and empties merge.
    prs = [_pr(1, "flow-a")]
    out = er.classify(prs, _idx(**{"flow-a": ["evolve"]}), main_ci_status="failed")
    assert out["merge"] == []
    assert out["held_main_red"] == [
        {
            "pr": 1,
            "key": "flow-a",
            "branch": "feature/flow-a-some-desc",
            "is_draft": False,
            "is_hot": False,
            "covers": [],
        }
    ]


def test_main_red_does_not_promote_hot():
    # a hot-eligible PR (auto_merge_hot on, isolated) is NOT promoted under red main;
    # it lands in held_main_red, not merge.
    prs = [_pr(1, "flow-h")]
    out = er.classify(
        prs, _idx(**{"flow-h": ["evolve", "hot"]}), auto_merge_hot=True, main_ci_status="failed"
    )
    assert out["merge"] == []
    assert out["held_main_red"] == [
        {
            "pr": 1,
            "key": "flow-h",
            "branch": "feature/flow-h-some-desc",
            "is_draft": False,
            "is_hot": True,
            "covers": [],
        }
    ]


def test_held_main_red_key_always_present():
    # the bucket is present (empty) even when main is not red.
    prs = [_pr(1, "flow-a")]
    out = er.classify(prs, _idx(**{"flow-a": ["evolve"]}))
    assert out["held_main_red"] == []


def test_main_red_none_is_byte_for_byte_legacy():
    prs = [_pr(1, "flow-a")]
    legacy = er.classify(prs, _idx(**{"flow-a": ["evolve"]}))
    explicit_none = er.classify(prs, _idx(**{"flow-a": ["evolve"]}), main_ci_status=None)
    assert legacy == explicit_none
    assert legacy["merge"] != []
    assert legacy["held_main_red"] == []


def test_main_green_preserves_buckets_byte_for_byte():
    prs = [_pr(1, "flow-a"), _pr(2, "flow-h"), _pr(3, "flow-b", rollup=PENDING)]
    idx = _idx(**{"flow-a": ["evolve"], "flow-h": ["evolve", "hot"], "flow-b": ["evolve"]})
    legacy = er.classify(prs, idx)
    green = er.classify(prs, idx, main_ci_status="green")
    # green is a no-op: every legacy bucket is preserved, held_main_red stays empty.
    legacy.pop("held_main_red")
    held = green.pop("held_main_red")
    assert green == legacy
    assert held == []


# ---- classify: covers surfacing (flow-n7lz) ----


def test_merge_entry_surfaces_covers_from_commit_trailers():
    # a folded run's commits carry `Closes <cover>` trailers; the merge entry must
    # surface them so the §A orphan-reap prose can close each cover.
    prs = [
        _pr(
            1,
            "flow-lead",
            commits=["feat: lead\n\nticket: flow-lead\nCloses flow-c1\nCloses flow-c2"],
        )
    ]
    out = er.classify(prs, _idx(**{"flow-lead": ["evolve"]}))
    assert out["merge"][0]["covers"] == ["flow-c1", "flow-c2"]


def test_merge_entry_covers_empty_when_no_trailers():
    prs = [_pr(1, "flow-a", commits=["feat: solo\n\nticket: flow-a"])]
    out = er.classify(prs, _idx(**{"flow-a": ["evolve"]}))
    assert out["merge"][0]["covers"] == []


def test_merge_entry_covers_empty_when_commits_absent():
    # a _pr() with no commits key (the regression baseline) surfaces covers: [].
    prs = [_pr(1, "flow-a")]
    out = er.classify(prs, _idx(**{"flow-a": ["evolve"]}))
    assert out["merge"][0]["covers"] == []


def test_merge_entry_covers_excludes_lead_own_key():
    # compose_commit emits `Closes <lead>` too; the lead's own key is not a cover.
    prs = [
        _pr(
            1,
            "flow-lead",
            commits=["feat: lead\n\nticket: flow-lead\nCloses flow-lead\nCloses flow-c1"],
        )
    ]
    out = er.classify(prs, _idx(**{"flow-lead": ["evolve"]}))
    assert out["merge"][0]["covers"] == ["flow-c1"]


def test_covers_surfaced_on_held_main_red_entry():
    # held_main_red shares the merge append; covers must ride it too.
    prs = [_pr(1, "flow-lead", commits=["feat: x\n\nticket: flow-lead\nCloses flow-c1"])]
    out = er.classify(prs, _idx(**{"flow-lead": ["evolve"]}), main_ci_status="failed")
    assert out["held_main_red"][0]["covers"] == ["flow-c1"]


def test_covers_dedups_across_multiple_commits():
    prs = [
        _pr(
            1,
            "flow-lead",
            commits=[
                "feat: a\n\nticket: flow-lead\nCloses flow-c1",
                "feat: b\n\nticket: flow-lead\nCloses flow-c1\nCloses flow-c2",
            ],
        )
    ]
    out = er.classify(prs, _idx(**{"flow-lead": ["evolve"]}))
    assert out["merge"][0]["covers"] == ["flow-c1", "flow-c2"]


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
        if args[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(args, 0, _view_detail(args, prs), "")
        if args[:3] == ["gh", "pr", "list"]:
            return subprocess.CompletedProcess(args, 0, json.dumps(_stripped(prs)), "")
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
            "covers": [],
        }
    ]
    assert out["skipped_hot"] == [{"pr": 2, "key": "flow-h", "branch": "feature/flow-h-some-desc"}]
    assert out["not_green"] == [{"pr": 3, "key": "flow-b", "branch": "feature/flow-b-some-desc"}]


def test_reap_bulk_list_omits_files_field(tmp_path):
    # the bulk `gh pr list` must OMIT files: the nested commits->authors connection
    # blows gh's GraphQL node-cost estimator past its limit and the query is rejected
    # pre-execution (flow-4dxr). files are grafted per-PR via `gh pr view` instead.
    ws = _marked_ws(tmp_path)
    run, calls = _dispatch(prs=[], evolve_list=[])
    er.reap(ws, runner=run)
    pr_list = next(a for a in calls if a[:3] == ["gh", "pr", "list"])
    json_fields = pr_list[pr_list.index("--json") + 1]
    assert "files" not in json_fields.split(",")


def test_reap_bulk_list_omits_commits_field(tmp_path):
    # the bulk `gh pr list` must OMIT commits: the nested authors connection is what
    # blows gh's GraphQL node-cost estimator (flow-4dxr). commits are grafted per-PR
    # via `gh pr view` instead.
    ws = _marked_ws(tmp_path)
    run, calls = _dispatch(prs=[], evolve_list=[])
    er.reap(ws, runner=run)
    pr_list = next(a for a in calls if a[:3] == ["gh", "pr", "list"])
    json_fields = pr_list[pr_list.index("--json") + 1]
    assert "commits" not in json_fields.split(",")


def test_reap_enriches_candidates_via_pr_view(tmp_path):
    # the positive guard: every candidate PR (key in the evolve index) gets a
    # `gh pr view <n> --json files,commits` so classify still sees files/commits.
    ws = _marked_ws(tmp_path)
    run, calls = _dispatch(
        prs=[_pr(1, "flow-a", files=["state.py"], commits=["feat\n\nCloses flow-c1"])],
        evolve_list=[{"id": "flow-a", "labels": ["evolve"]}],
    )
    er.reap(ws, runner=run)
    views = [a for a in calls if a[:3] == ["gh", "pr", "view"]]
    assert len(views) == 1
    view = views[0]
    assert view[3] == "1"
    json_fields = view[view.index("--json") + 1].split(",")
    assert "files" in json_fields and "commits" in json_fields


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
            "covers": [],
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
        if args[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(args, 0, _view_detail(args, prs), "")
        if args[:3] == ["gh", "pr", "list"]:
            return subprocess.CompletedProcess(args, 0, json.dumps(_stripped(prs)), "")
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


# ---- reap: main-CI probe + deduped P0 filing (flow-a1ti.3) ----


def _red_main_runner(*, prs, evolve_list, open_beads, check_runs_failed=True):
    calls: Recorder = []
    concl = "failure" if check_runs_failed else "success"

    def run(args):
        calls.append(args)
        if args[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(args, 0, _view_detail(args, prs), "")
        if args[:3] == ["gh", "pr", "list"]:
            return subprocess.CompletedProcess(args, 0, json.dumps(_stripped(prs)), "")
        if args[:2] == ["bd", "list"] and "-l" in args:
            return subprocess.CompletedProcess(args, 0, json.dumps(evolve_list), "")
        if args[:2] == ["bd", "list"] and "--status" in args and "open" in args:
            return subprocess.CompletedProcess(args, 0, json.dumps(open_beads), "")
        if args[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(args, 0, "deadbeef\n", "")
        if args[:2] == ["gh", "api"]:
            payload = json.dumps(
                [{"name": "lint-and-test", "status": "completed", "conclusion": concl}]
            )
            return subprocess.CompletedProcess(args, 0, payload, "")
        return subprocess.CompletedProcess(args, 0, "", "")

    return run, calls


def test_reap_red_main_holds_and_files_p0(tmp_path):
    ws = _marked_ws(tmp_path)
    run, calls = _red_main_runner(
        prs=[_pr(1, "flow-a")],
        evolve_list=[{"id": "flow-a", "labels": ["evolve"]}],
        open_beads=[],
    )
    out = er.reap(ws, runner=run)
    assert out["merge"] == []
    assert {e["key"] for e in out["held_main_red"]} == {"flow-a"}
    creates = [a for a in calls if a[:2] == ["bd", "create"]]
    assert len(creates) == 1
    title = creates[0][creates[0].index("--title") + 1]
    assert "main-ci-red" in title
    assert "deadbeef" in title
    assert "lint-and-test" in title
    assert "P0" in creates[0]


def test_reap_red_main_dedups_when_p0_already_open(tmp_path):
    ws = _marked_ws(tmp_path)
    run, calls = _red_main_runner(
        prs=[_pr(1, "flow-a")],
        evolve_list=[{"id": "flow-a", "labels": ["evolve"]}],
        open_beads=[{"id": "flow-x", "title": "main-ci-red: oldsha lint-and-test"}],
    )
    out = er.reap(ws, runner=run)
    assert {e["key"] for e in out["held_main_red"]} == {"flow-a"}
    creates = [a for a in calls if a[:2] == ["bd", "create"]]
    assert creates == []  # an open P0 already covers this; do not refile


def test_reap_green_main_files_no_p0_and_merges(tmp_path):
    ws = _marked_ws(tmp_path)
    run, calls = _red_main_runner(
        prs=[_pr(1, "flow-a")],
        evolve_list=[{"id": "flow-a", "labels": ["evolve"]}],
        open_beads=[],
        check_runs_failed=False,
    )
    out = er.reap(ws, runner=run)
    assert {e["key"] for e in out["merge"]} == {"flow-a"}
    assert out["held_main_red"] == []
    assert [a for a in calls if a[:2] == ["bd", "create"]] == []


# ---- _labels_index: no silent truncation to bd's default --limit 50 (flow-8zdy) ----


def _truncating_label_runner(
    *, prs: list[dict], evolve_beads: list[dict], default_limit: int = 50
) -> tuple[Callable[..., subprocess.CompletedProcess[str]], Recorder]:
    """A runner whose `bd list -l evolve` HONORS --limit, mirroring bd's real default-50.

    Returns every evolve bead only when `--limit 0` is present; otherwise the first
    `default_limit` rows (bd sorts by priority, so a low-priority in_progress orphan
    sorts past the window once enough higher-priority closed beads accumulate). Models
    the flow-8zdy incident: without `--limit 0` the orphan's key never reaches the index.
    """
    calls: Recorder = []

    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(args, 0, _view_detail(args, prs), "")
        if args[:3] == ["gh", "pr", "list"]:
            return subprocess.CompletedProcess(args, 0, json.dumps(_stripped(prs)), "")
        if args[:2] == ["bd", "list"]:
            rows = evolve_beads if ("-l" in args and args[args.index("-l") + 1] == "evolve") else []
            if "--limit" in args:
                lim = int(args[args.index("--limit") + 1])
                rows = rows if lim == 0 else rows[:lim]
            else:
                rows = rows[:default_limit]
            return subprocess.CompletedProcess(args, 0, json.dumps(rows), "")
        return subprocess.CompletedProcess(args, 1, "", f"unexpected: {args}")

    return run, calls


def test_labels_index_query_is_unlimited():
    # the fix's direct guard: the label-index query must carry `--limit 0` so it is
    # never capped at bd's default 50.
    run, calls = _label_aware_list_runner(
        prs=[], by_label={"evolve": [{"id": "flow-a", "labels": ["evolve"]}]}
    )
    er._labels_index(run)
    bd_calls = [a for a in calls if a[:2] == ["bd", "list"]]
    assert bd_calls
    for a in bd_calls:
        assert "--limit" in a, f"label-index query must be limited explicitly: {a}"
        assert a[a.index("--limit") + 1] == "0", f"label-index query must be unlimited: {a}"


def test_reap_does_not_truncate_label_index_orphan(tmp_path):
    # flow-8zdy regression: a green + conflicting in_progress orphan whose evolve bead
    # sorts past bd's default-50 window must still be classified. If _labels_index omits
    # `--limit 0` the orphan's key is absent from the index and classify drops its PR
    # into NO bucket (the reap safety-net goes blind, every bucket empty).
    ws = _marked_ws(tmp_path)
    filler = [{"id": f"flow-old{i}", "labels": ["evolve"]} for i in range(50)]
    orphan = {"id": "flow-orph", "labels": ["evolve"]}
    evolve_beads = [*filler, orphan]  # the orphan sits past row 50
    prs = [_pr(1, "flow-orph", state="DIRTY")]  # green + conflicting (CONFLICTING/DIRTY)

    run, _ = _truncating_label_runner(prs=prs, evolve_beads=evolve_beads)
    out = er.reap(ws, runner=run)

    # green non-hot DIRTY -> blocked (reason "DIRTY"); the orphan must not vanish
    # from every bucket (the reap safety-net must still classify it).
    assert {e["key"] for e in out["blocked"]} == {"flow-orph"}
    assert "flow-orph" in {e["key"] for bucket in out.values() for e in bucket}


def test_file_main_red_p0_dedup_scan_is_unlimited():
    # flow-b0gl regression: the at-most-one-open dedup scan must pass `--limit 0`.
    # Without it bd's default-50 window can drop an already-open main-ci-red P0, so the
    # dedup misses and a DUPLICATE P0 gets filed (same footgun as flow-8zdy/PR#299).
    calls: Recorder = []

    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[:2] == ["bd", "list"]:
            return subprocess.CompletedProcess(args, 0, "[]", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    er._file_main_red_p0(run, "deadbeef", ["test"])

    list_calls = [a for a in calls if a[:2] == ["bd", "list"]]
    assert list_calls
    for a in list_calls:
        assert "--limit" in a, f"dedup-scan query must be limited explicitly: {a}"
        assert a[a.index("--limit") + 1] == "0", f"dedup-scan query must be unlimited: {a}"


def test_file_main_red_p0_dedup_fires_on_open_bead():
    # an open bead whose title carries the main-ci-red stem short-circuits filing.
    calls: Recorder = []

    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[:2] == ["bd", "list"]:
            body = json.dumps([{"id": "flow-x", "title": "main-ci-red: cafe test"}])
            return subprocess.CompletedProcess(args, 0, body, "")
        return subprocess.CompletedProcess(args, 0, "", "")

    er._file_main_red_p0(run, "deadbeef", ["test"])

    assert not [a for a in calls if a[:2] == ["bd", "create"]]
