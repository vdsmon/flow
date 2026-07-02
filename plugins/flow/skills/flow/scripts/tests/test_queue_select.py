from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

import fleet
import lease
import queue_select as qs
from _timeutil import utcnow_iso

Recorder = list[list[str]]


def _write_lease(run_dir: Path, *, expired: bool = False) -> None:
    """Acquire a real lease in run_dir (live by default, expired on request)."""
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
        "labels": labels if labels is not None else [],
        "issue_type": issue_type,
        "description": desc,
    }


# ---- pure partition ----


def test_all_leaf_fans_out_to_concurrency():
    cands = [_cand(f"flow-{i}") for i in range(5)]
    out = qs.partition(cands, set(), 0, cap=10, concurrency=3)
    assert len(out["launch"]) == 3
    assert out["held_backpressure"] is False


def test_budget_is_cap_minus_open_prs():
    cands = [_cand(f"flow-{i}") for i in range(5)]
    out = qs.partition(cands, set(), open_pr_count=3, cap=5, concurrency=3)
    assert len(out["launch"]) == 2  # min(5-3, 3)


def test_backpressure_empties_launch():
    cands = [_cand("flow-a")]
    out = qs.partition(cands, set(), open_pr_count=5, cap=5, concurrency=3)
    assert out["launch"] == []
    assert out["held_backpressure"] is True


def test_in_flight_excluded():
    cands = [_cand("flow-a"), _cand("flow-b")]
    out = qs.partition(cands, {"flow-a"}, 0, cap=10, concurrency=5)
    assert out["launch"] == ["flow-b"]
    assert out["skipped_in_flight"] == ["flow-a"]


def test_anchor_collision_serializes():
    cands = [
        _cand("flow-a", priority=1, blast="plugins/flow/scripts/x.py"),
        _cand("flow-b", priority=2, blast="plugins/flow/scripts/x.py"),
        _cand("flow-c", priority=3, blast="plugins/flow/scripts/y.py"),
    ]
    out = qs.partition(cands, set(), 0, cap=10, concurrency=5)
    assert out["launch"] == ["flow-a", "flow-c"]
    assert out["held_anchor"] == ["flow-b"]


def test_epic_is_skipped():
    cands = [_cand("flow-epi", issue_type="epic"), _cand("flow-a")]
    out = qs.partition(cands, set(), 0, cap=10, concurrency=5)
    assert out["launch"] == ["flow-a"]


def test_evolve_label_never_launched():
    # evolve beads belong to the evolve drain's queue, never the day-job queue
    cands = [_cand("flow-ev", labels=["evolve", "audit"]), _cand("flow-a")]
    out = qs.partition(cands, set(), 0, cap=10, concurrency=5)
    assert out["launch"] == ["flow-a"]


def test_proposal_label_never_launched():
    # judgment work never auto-launches; no opt-in exists on this queue
    cands = [_cand("flow-prop", labels=["proposal"]), _cand("flow-a")]
    out = qs.partition(cands, set(), 0, cap=10, concurrency=5)
    assert out["launch"] == ["flow-a"]


def test_hot_label_defensively_never_launched():
    # a hot non-evolve bead would be invisible to evolve's _hot_inflight gate, silently breaking
    # the one-hot invariant across queues, so it must never launch here
    cands = [_cand("flow-hot", labels=["hot"]), _cand("flow-a")]
    out = qs.partition(cands, set(), 0, cap=10, concurrency=5)
    assert out["launch"] == ["flow-a"]


def test_inflight_evolve_bead_not_reported():
    # queue-scoped report: an in-flight bead from ANOTHER queue is dropped
    # entirely, not surfaced in skipped_in_flight (that would couple the
    # queue drain's liveness wait to the evolve fleet)
    cands = [_cand("flow-ev", labels=["evolve"]), _cand("flow-a")]
    out = qs.partition(cands, {"flow-ev", "flow-a"}, 0, cap=10, concurrency=5)
    assert out["launch"] == []
    assert out["skipped_in_flight"] == ["flow-a"]


def test_priority_ranking():
    cands = [_cand("flow-lo", priority=3), _cand("flow-hi", priority=1)]
    out = qs.partition(cands, set(), 0, cap=10, concurrency=1)
    assert out["launch"] == ["flow-hi"]


# ---- select integration (injected runner) ----


def _marked_ws(tmp_path: Path) -> Path:
    d = tmp_path / "flow"
    (d / ".flow").mkdir(parents=True)
    (d / ".flow" / "workspace.toml").write_text(
        "[maintainer]\nself_target = true\n", encoding="utf-8"
    )
    return d


def _worker_ws(tmp_path: Path) -> Path:
    d = tmp_path / "flow"
    (d / ".flow").mkdir(parents=True)
    (d / ".flow" / "workspace.toml").write_text(
        '[maintainer]\nself_target = true\n\n[evolve]\nworker_model = "opus"\n',
        encoding="utf-8",
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


def test_select_launches_day_job_beads(tmp_path):
    ws = _marked_ws(tmp_path)
    run, calls = _dispatch(ready=[_cand("flow-a"), _cand("flow-b")])
    out = qs.select(ws, cap=5, concurrency=3, runner=run)
    assert set(out["launch"]) == {"flow-a", "flow-b"}
    assert out["open_pr_count"] == 0
    assert out["open_pr_keys"] == []
    assert ["bd", "ready", "--json"] in calls  # unlabelled query, no -l
    assert out["cap"] == 5
    assert out["concurrency"] == 3


def test_select_fleet_only_key_inflight_but_absent_from_live_runs(tmp_path):
    # flow-8by2.3 regression (queue sibling of the evolve_select guard): a fleet-only
    # key (registered at launch, no lease) suppresses relaunch but stays OUT of
    # live_runs so the queue-drain marker-remove can't evict it a turn early (flow-d4s).
    ws = _marked_ws(tmp_path)
    repo = qs.resolve_maintainer_repo(ws)
    assert repo is not None
    fleet.register(fleet.resolve_fleet_dir(repo), "flow-fleet", "", now=utcnow_iso())
    run, _ = _dispatch(ready=[_cand("flow-fleet"), _cand("flow-y")])
    out = qs.select(ws, cap=5, concurrency=3, runner=run)
    assert out["launch"] == ["flow-y"]
    assert out["skipped_in_flight"] == ["flow-fleet"]
    assert out["live_runs"] == []


def test_select_tolerates_null_labels(tmp_path):
    # live `bd ready --json` emits labels: null when absent
    ws = _marked_ws(tmp_path)
    cand = _cand("flow-a")
    cand["labels"] = None
    run, _ = _dispatch(ready=[cand])
    out = qs.select(ws, cap=5, concurrency=3, runner=run)
    assert out["launch"] == ["flow-a"]


def test_select_drops_inflight_branch(tmp_path):
    ws = _marked_ws(tmp_path)
    run, _ = _dispatch(
        ready=[_cand("flow-a"), _cand("flow-b")],
        branches="feature/flow-a-wip\nmain\n",
    )
    out = qs.select(ws, cap=5, concurrency=3, runner=run)
    assert out["launch"] == ["flow-b"]
    assert out["skipped_in_flight"] == ["flow-a"]


def test_select_evolve_pr_does_not_count_toward_cap(tmp_path):
    # an open PR whose key is an ACTIVE evolve bead belongs to the evolve
    # queue's cap, not the day-job one
    ws = _marked_ws(tmp_path)
    run, _ = _dispatch(
        ready=[_cand("flow-a")],
        prs=[{"headRefName": "feature/flow-ev-wip"}],
        evolve_list=[{"id": "flow-ev", "labels": ["evolve"], "status": "in_progress"}],
    )
    out = qs.select(ws, cap=5, concurrency=3, runner=run)
    assert out["open_pr_count"] == 0
    assert out["open_pr_keys"] == []
    assert out["launch"] == ["flow-a"]


def test_select_non_evolve_pr_counts_toward_cap(tmp_path):
    ws = _marked_ws(tmp_path)
    run, _ = _dispatch(
        ready=[_cand("flow-a")],
        prs=[{"headRefName": "feature/flow-day-wip"}],
        evolve_list=[],
    )
    out = qs.select(ws, cap=5, concurrency=3, runner=run)
    assert out["open_pr_count"] == 1
    assert out["open_pr_keys"] == ["flow-day"]


def test_select_queue_scoped_backpressure_holds_launch(tmp_path):
    ws = _marked_ws(tmp_path)
    run, _ = _dispatch(
        ready=[_cand("flow-a")],
        prs=[{"headRefName": "feature/flow-d1-wip"}, {"headRefName": "feature/flow-d2-wip"}],
        evolve_list=[],
    )
    out = qs.select(ws, cap=2, concurrency=3, runner=run)
    assert out["launch"] == []
    assert out["held_backpressure"] is True
    assert out["open_pr_count"] == 2


def test_select_no_flow_prs_short_circuits_bd_list(tmp_path):
    ws = _marked_ws(tmp_path)
    run, calls = _dispatch(
        ready=[_cand("flow-a")],
        prs=[{"headRefName": "main-fixup"}],
    )
    qs.select(ws, cap=5, concurrency=3, runner=run)
    assert not any(a[:2] == ["bd", "list"] for a in calls)


def test_select_bd_list_queries_active_evolve(tmp_path):
    ws = _marked_ws(tmp_path)
    run, calls = _dispatch(
        ready=[],
        prs=[{"headRefName": "feature/flow-day-wip"}],
        evolve_list=[],
    )
    qs.select(ws, cap=5, concurrency=3, runner=run)
    list_calls = [a for a in calls if a[:2] == ["bd", "list"]]
    assert len(list_calls) == 1
    args = list_calls[0]
    assert args[args.index("-l") + 1] == "evolve"
    assert args[args.index("--status") + 1] == "open,in_progress,blocked"


def test_select_pre_pr_live_run_is_inflight(tmp_path):
    ws = _marked_ws(tmp_path)
    repo = qs.resolve_maintainer_repo(ws)
    assert repo is not None
    _write_lease(_pool_run_dir(repo, "flow-x"))
    run, _ = _dispatch(ready=[_cand("flow-x"), _cand("flow-y")])
    out = qs.select(ws, cap=5, concurrency=3, runner=run)
    assert out["launch"] == ["flow-y"]
    assert out["skipped_in_flight"] == ["flow-x"]
    assert out["live_runs"] == ["flow-x"]


def test_select_launched_key_is_inflight_not_relaunched(tmp_path):
    # a key in the launch ledger (no ref, no lease yet) must read as in-flight
    ws = _marked_ws(tmp_path)
    repo = qs.resolve_maintainer_repo(ws)
    assert repo is not None
    import launch_ledger

    launch_ledger.add(repo, "flow-led")
    run, _ = _dispatch(ready=[_cand("flow-led"), _cand("flow-y")])
    out = qs.select(ws, cap=5, concurrency=3, runner=run)
    assert out["launch"] == ["flow-y"]
    assert out["skipped_in_flight"] == ["flow-led"]
    assert out["launched_pending"] == ["flow-led"]


def test_select_trivial_downshifts_to_sonnet(tmp_path):
    ws = _marked_ws(tmp_path)
    run, _ = _dispatch(ready=[_cand("flow-t", labels=["tier:trivial"])])
    out = qs.select(ws, cap=5, concurrency=3, runner=run)
    assert out["launch"] == ["flow-t"]
    assert out["model_per_key"]["flow-t"] == "sonnet"


def test_select_light_downshifts_to_sonnet(tmp_path):
    ws = _marked_ws(tmp_path)
    run, _ = _dispatch(ready=[_cand("flow-l", labels=["tier:light"])])
    out = qs.select(ws, cap=5, concurrency=3, runner=run)
    assert out["launch"] == ["flow-l"]
    assert out["model_per_key"]["flow-l"] == "sonnet"


def test_select_worker_model_light_beats_worker_model(tmp_path):
    ws = _worker_ws(tmp_path)
    run, _ = _dispatch(ready=[_cand("flow-l", labels=["tier:light"])])
    out = qs.select(ws, cap=5, concurrency=3, runner=run)
    assert out["model_per_key"]["flow-l"] == "sonnet"


def test_select_plain_bead_no_downshift(tmp_path):
    ws = _marked_ws(tmp_path)
    run, _ = _dispatch(ready=[_cand("flow-p")])
    out = qs.select(ws, cap=5, concurrency=3, runner=run)
    assert out["launch"] == ["flow-p"]
    assert "flow-p" not in out["model_per_key"]


def test_select_worker_model_plain_bead(tmp_path):
    ws = _worker_ws(tmp_path)
    run, _ = _dispatch(ready=[_cand("flow-p")])  # non-trivial
    out = qs.select(ws, cap=5, concurrency=3, runner=run)
    assert out["model_per_key"]["flow-p"] == "opus"


def test_select_worker_model_trivial_beats_worker_model(tmp_path):
    ws = _worker_ws(tmp_path)
    run, _ = _dispatch(ready=[_cand("flow-t", labels=["tier:trivial"])])
    out = qs.select(ws, cap=5, concurrency=3, runner=run)
    assert out["model_per_key"]["flow-t"] == "sonnet"


def test_select_worker_model_unset_plain_omitted(tmp_path):
    ws = _marked_ws(tmp_path)
    run, _ = _dispatch(ready=[_cand("flow-p")])
    out = qs.select(ws, cap=5, concurrency=3, runner=run)
    assert "flow-p" not in out["model_per_key"]


def test_worker_model_reads_evolve_section(tmp_path):
    d = tmp_path / "flow"
    (d / ".flow").mkdir(parents=True)
    (d / ".flow" / "workspace.toml").write_text(
        '[evolve]\nworker_model = "opus"\n', encoding="utf-8"
    )
    assert qs._worker_model(d) == "opus"


def test_worker_model_absent_section_is_none(tmp_path):
    d = tmp_path / "flow"
    (d / ".flow").mkdir(parents=True)
    (d / ".flow" / "workspace.toml").write_text(
        "[maintainer]\nself_target = true\n", encoding="utf-8"
    )
    assert qs._worker_model(d) is None


def test_worker_model_empty_or_nonstr_is_none(tmp_path):
    for body in ('[evolve]\nworker_model = ""\n', "[evolve]\nworker_model = 5\n"):
        d = tmp_path / f"flow-{hash(body) & 0xFFFF}"
        (d / ".flow").mkdir(parents=True)
        (d / ".flow" / "workspace.toml").write_text(body, encoding="utf-8")
        assert qs._worker_model(d) is None, body


def test_select_not_maintainer_raises(tmp_path, monkeypatch):
    monkeypatch.setattr("maintainer._global_config_path", lambda: tmp_path / "absent.toml")
    plain = tmp_path / "proj"
    (plain / ".flow").mkdir(parents=True)
    (plain / ".flow" / "workspace.toml").write_text(
        '[tracker]\nbackend = "beads"\n', encoding="utf-8"
    )
    with pytest.raises(qs.NotMaintainer):
        qs.select(plain, cap=5, concurrency=3)  # raises before any runner call


def test_select_tool_error(tmp_path):
    ws = _marked_ws(tmp_path)

    def run(args):
        return subprocess.CompletedProcess(args, 1, "", "bd boom")

    with pytest.raises(qs.ToolError):
        qs.select(ws, cap=5, concurrency=3, runner=run)


def _pool_run_dir(repo: Path, key: str, slug: str = "wip") -> Path:
    return repo / ".flow" / "worktrees" / f"feature-{key}-{slug}" / ".flow" / "runs" / key


# ---- _config_defaults ----


def _ws_with_toml(tmp_path: Path, body: str) -> Path:
    d = tmp_path / "flow"
    (d / ".flow").mkdir(parents=True)
    (d / ".flow" / "workspace.toml").write_text(body, encoding="utf-8")
    return d


def test_config_defaults_reads_queue_section(tmp_path):
    ws = _ws_with_toml(tmp_path, "[queue]\ncap = 7\nconcurrency = 2\n")
    assert qs._config_defaults(ws) == (7, 2)


def test_config_defaults_absent_section(tmp_path):
    ws = _ws_with_toml(tmp_path, "[maintainer]\nself_target = true\n")
    assert qs._config_defaults(ws) == (qs.DEFAULT_CAP, qs.DEFAULT_CONCURRENCY)


def test_config_defaults_ignores_evolve_section(tmp_path):
    ws = _ws_with_toml(tmp_path, "[evolve]\ncap = 9\nconcurrency = 9\n")
    assert qs._config_defaults(ws) == (qs.DEFAULT_CAP, qs.DEFAULT_CONCURRENCY)


def test_config_defaults_invalid_values(tmp_path):
    ws = _ws_with_toml(tmp_path, '[queue]\ncap = 0\nconcurrency = "lots"\n')
    assert qs._config_defaults(ws) == (qs.DEFAULT_CAP, qs.DEFAULT_CONCURRENCY)


def test_config_defaults_no_workspace(tmp_path):
    assert qs._config_defaults(tmp_path / "nope") == (qs.DEFAULT_CAP, qs.DEFAULT_CONCURRENCY)


# ---- budget shrinks by in-flight active-session count (mirrors evolve_select, flow-01ys) ----


def test_budget_subtracts_inflight_count():
    # active-session count (launched_pending UNION live_runs) shrinks the concurrency budget
    cands = [_cand(f"flow-{i}") for i in range(10)]
    full = qs.partition(cands, set(), 0, cap=10, concurrency=8, inflight_count=8)
    assert full["launch"] == []  # concurrency - inflight floored to 0
    partial = qs.partition(cands, set(), 0, cap=10, concurrency=8, inflight_count=6)
    assert len(partial["launch"]) == 2  # min(10-0, 8-6)


def test_budget_open_prs_dont_consume_concurrency():
    # the discriminator: open PRs bound only the cap term, NOT the concurrency term
    cands = [_cand(f"flow-{i}") for i in range(10)]
    out = qs.partition(cands, set(), open_pr_count=4, cap=10, concurrency=8, inflight_count=0)
    assert len(out["launch"]) == 6  # min(10-4, 8-0), NOT 8-4


def test_select_budget_shrinks_with_launched_pending(tmp_path):
    # launched_pending keys consume the concurrency budget; only concurrency - N slots remain
    ws = _marked_ws(tmp_path)
    repo = qs.resolve_maintainer_repo(ws)
    assert repo is not None
    import launch_ledger

    pending = [f"flow-p{i}" for i in range(1, 7)]  # 6 launched, pre-init
    for key in pending:
        launch_ledger.add(repo, key)
    # ready candidates DISJOINT from the launched set, so they only feel the budget
    run, _ = _dispatch(ready=[_cand(f"flow-r{i}") for i in range(5)])
    out = qs.select(ws, cap=10, concurrency=8, runner=run)
    assert len(out["launch"]) == 2  # 8 - 6 launched_pending
    assert sorted(out["launched_pending"]) == sorted(pending)
