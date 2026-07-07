from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

import evolve_select as es
import fleet
import lease
from _evolve_common import ToolError
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


def test_proposal_label_never_launched():
    # the `proposal`-exclusion guard keeps a mislabeled `evolve,proposal` bead out of `launch`
    # (defense-in-depth, since plain proposals now live in a separate non-`evolve` backlog and
    # never reach drain).
    cands = [_cand("flow-prop", labels=["evolve", "proposal"]), _cand("flow-a")]
    out = es.partition(cands, set(), False, 0, cap=10, concurrency=5)
    assert out["launch"] == ["flow-a"]


def test_priority_ranking():
    cands = [_cand("flow-lo", priority=3), _cand("flow-hi", priority=1)]
    out = es.partition(cands, set(), False, 0, cap=10, concurrency=1)
    assert out["launch"] == ["flow-hi"]


def test_include_proposals_drops_exclusion_guard():
    # the DANGEROUS opt-in: a plain `proposal` bead now launches alongside audit work.
    cands = [_cand("flow-prop", labels=["proposal"]), _cand("flow-a")]
    out = es.partition(cands, set(), False, 0, cap=10, concurrency=5, include_proposals=True)
    assert set(out["launch"]) == {"flow-prop", "flow-a"}


# ---- helpers ----


def test_primary_anchor_first_path():
    desc = "EVIDENCE\nBLAST RADIUS: a/b.py, c/d.py, e.py\nVALUE"
    assert es.primary_anchor(desc) == "a/b.py"


def test_primary_anchor_absent():
    assert es.primary_anchor("no blast radius here") is None


def test_key_from_ref():
    assert es._key_from_ref("feat/flow-7mb-evolve-verb") == "flow-7mb"
    assert es._key_from_ref("origin/feat/flow-aut.6-fix") == "flow-aut.6"
    assert es._key_from_ref("feature/flow-7mb-evolve-verb") == "flow-7mb"  # legacy
    assert es._key_from_ref("main") is None


def test_is_inflight_prefix_match():
    refs = {"feat/flow-a-some-desc"}
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


def test_select_launches_leaves(tmp_path):
    ws = _marked_ws(tmp_path)
    run, calls = _dispatch(ready=[_cand("flow-a"), _cand("flow-b")])
    out = es.select(ws, cap=5, concurrency=3, runner=run)
    assert set(out["launch"]) == {"flow-a", "flow-b"}
    assert out["open_pr_count"] == 0
    assert out["open_pr_keys"] == []
    assert ["bd", "ready", "-l", "evolve", "--json"] in calls
    assert out["include_proposals"] is False


def _label_aware_dispatch(
    *, by_label: dict[str, list[dict]]
) -> tuple[Callable[..., subprocess.CompletedProcess[str]], Recorder]:
    """`bd ready -l <label>` returns the per-label fixture; everything else is empty."""
    calls: Recorder = []

    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[:2] == ["bd", "ready"]:
            label = args[args.index("-l") + 1]
            return subprocess.CompletedProcess(args, 0, json.dumps(by_label.get(label, [])), "")
        if args[:2] == ["bd", "list"]:
            return subprocess.CompletedProcess(args, 0, "[]", "")
        if args[:3] == ["gh", "pr", "list"]:
            return subprocess.CompletedProcess(args, 0, "[]", "")
        if args[:2] == ["git", "for-each-ref"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 1, "", f"unexpected: {args}")

    return run, calls


def test_select_excludes_proposals_by_default(tmp_path):
    ws = _marked_ws(tmp_path)
    run, calls = _label_aware_dispatch(
        by_label={
            "evolve": [_cand("flow-a")],
            "proposal": [_cand("flow-prop", labels=["proposal"])],
        }
    )
    out = es.select(ws, cap=5, concurrency=3, runner=run)
    assert out["launch"] == ["flow-a"]  # proposal backlog never queried
    assert not any(a[:2] == ["bd", "ready"] and "proposal" in a for a in calls)


def test_select_include_proposals_dual_query(tmp_path):
    ws = _marked_ws(tmp_path)
    run, calls = _label_aware_dispatch(
        by_label={
            "evolve": [_cand("flow-a")],
            "proposal": [_cand("flow-prop", labels=["proposal"])],
        }
    )
    out = es.select(ws, cap=5, concurrency=3, runner=run, include_proposals=True)
    assert set(out["launch"]) == {"flow-a", "flow-prop"}
    assert out["include_proposals"] is True
    assert ["bd", "ready", "-l", "proposal", "--json"] in calls


def test_hot_inflight_include_proposals_queries_both_labels():
    seen_labels = []

    def run(args):
        if args[:2] == ["bd", "list"]:
            seen_labels.append(args[args.index("-l") + 1])
            label = seen_labels[-1]
            beads = (
                [{"id": "flow-old", "labels": ["proposal", "hot"]}] if label == "proposal" else []
            )
            return subprocess.CompletedProcess(args, 0, json.dumps(beads), "")
        return subprocess.CompletedProcess(args, 1, "", "unexpected")

    # a hot PROPOSAL already in flight consumes the single hot slot under the flag
    assert es._hot_inflight(run, {"feat/flow-old-wip"}, include_proposals=True) is True
    assert seen_labels == ["evolve", "proposal"]


def test_select_drops_inflight_branch(tmp_path):
    ws = _marked_ws(tmp_path)
    run, _ = _dispatch(
        ready=[_cand("flow-a"), _cand("flow-b")],
        branches="feat/flow-a-wip\nmain\n",
    )
    out = es.select(ws, cap=5, concurrency=3, runner=run)
    assert out["launch"] == ["flow-b"]
    assert out["skipped_in_flight"] == ["flow-a"]


def test_select_hot_inflight_from_open_pr(tmp_path):
    ws = _marked_ws(tmp_path)
    run, _ = _dispatch(
        ready=[_cand("flow-new", labels=["evolve", "hot"], blast="z.py")],
        prs=[{"headRefName": "feat/flow-old-wip"}],
        evolve_list=[{"id": "flow-old", "labels": ["evolve", "hot"]}],
    )
    out = es.select(ws, cap=5, concurrency=3, runner=run)
    assert out["launch"] == []  # hot slot consumed by the in-flight hot PR
    assert out["held_hot"] == ["flow-new"]
    assert out["open_pr_count"] == 1
    # the open-PR keys ride along so evolve_drain skips its own `gh pr list`
    assert out["open_pr_keys"] == ["flow-old"]


def test_select_open_pr_keys_only_from_prs_not_branches(tmp_path):
    # a local/remote branch with no open PR must NOT surface in open_pr_keys
    ws = _marked_ws(tmp_path)
    run, _ = _dispatch(
        ready=[],
        prs=[{"headRefName": "feat/flow-pr-wip"}, {"headRefName": "main"}],
        branches="feat/flow-branch-only-wip\n",
    )
    out = es.select(ws, cap=5, concurrency=3, runner=run)
    assert out["open_pr_keys"] == ["flow-pr"]


def _status_aware_runner(
    beads: list[dict],
) -> tuple[Callable[..., subprocess.CompletedProcess[str]], Recorder]:
    """bd list runner that honors --status, filtering fixture beads by their status field."""
    calls: Recorder = []

    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        wanted = set(args[args.index("--status") + 1].split(","))
        filtered = [b for b in beads if b.get("status") in wanted]
        return subprocess.CompletedProcess(args, 0, json.dumps(filtered), "")

    return run, calls


def test_hot_inflight_ignores_closed_bead_with_leaked_ref():
    for dead_status in ("closed", "deferred"):
        beads = [{"id": "flow-old", "labels": ["evolve", "hot"], "status": dead_status}]
        run, _ = _status_aware_runner(beads)
        assert es._hot_inflight(run, {"feat/flow-old-wip"}) is False, dead_status


def test_hot_inflight_queries_active_statuses():
    run, calls = _status_aware_runner([])
    es._hot_inflight(run, {"feat/flow-x-wip"})
    list_calls = [a for a in calls if a[:2] == ["bd", "list"]]
    assert len(list_calls) == 1
    args = list_calls[0]
    assert args[args.index("--status") + 1] == "open,in_progress,blocked"


def test_hot_inflight_queries_unlimited():
    # --limit 0 so a large active backlog never truncates a launched hot key out
    # of the hot-keys set (bd's default 50-row + priority sort), which would let a
    # second hot launch and break >=1-hot-at-a-time (flow-qmp5, PR#299 class).
    run, calls = _status_aware_runner([])
    es._hot_inflight(run, {"feat/flow-x-wip"})
    list_calls = [a for a in calls if a[:2] == ["bd", "list"]]
    assert len(list_calls) == 1
    args = list_calls[0]
    assert args[args.index("--limit") + 1] == "0"


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

    with pytest.raises(ToolError):
        es.select(ws, cap=5, concurrency=3, runner=run)


# ---- _live_run_keys - pre-PR lease scan ----


def _pool_run_dir(repo: Path, key: str, slug: str = "wip") -> Path:
    return repo / ".flow" / "worktrees" / f"feat-{key}-{slug}" / ".flow" / "runs" / key


def test_fleet_live_keys_finds_live_lease(tmp_path):
    # evolve_select now reads the reconciled lease | fleet helper; with no fleet
    # dir it equals the live-lease set (flow-8by2.3).
    repo = tmp_path / "flow"
    repo.mkdir()
    _write_lease(_pool_run_dir(repo, "flow-x"))
    assert es._fleet_live_keys(repo) == {"flow-x"}


def test_fleet_live_keys_skips_expired(tmp_path):
    repo = tmp_path / "flow"
    repo.mkdir()
    _write_lease(_pool_run_dir(repo, "flow-x"), expired=True)
    assert es._fleet_live_keys(repo) == set()


def test_fleet_live_keys_empty_when_no_worktrees(tmp_path):
    repo = tmp_path / "flow"
    repo.mkdir()
    assert es._fleet_live_keys(repo) == set()


def test_hot_inflight_extra_keys_no_refs():
    # no refs at all, but a live pre-PR run for flow-old that is hot -> serialized
    beads = [{"id": "flow-old", "labels": ["evolve", "hot"], "status": "in_progress"}]
    run, _ = _status_aware_runner(beads)
    assert es._hot_inflight(run, set(), extra_keys={"flow-old"}) is True


def test_hot_inflight_no_refs_no_extra_keys_short_circuits():
    # both empty -> early return, no bd list call
    calls: Recorder = []

    def run(args):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "[]", "")

    assert es._hot_inflight(run, set()) is False
    assert calls == []


def test_select_pre_pr_live_run_is_inflight(tmp_path):
    ws = _marked_ws(tmp_path)
    repo = es.resolve_maintainer_repo(ws)
    assert repo is not None
    _write_lease(_pool_run_dir(repo, "flow-x"))
    run, _ = _dispatch(ready=[_cand("flow-x"), _cand("flow-y")])
    out = es.select(ws, cap=5, concurrency=3, runner=run)
    assert out["launch"] == ["flow-y"]
    assert out["skipped_in_flight"] == ["flow-x"]
    assert out["live_runs"] == ["flow-x"]


def test_select_fleet_only_key_inflight_but_absent_from_live_runs(tmp_path):
    # flow-8by2.3 regression: a key registered in the fleet ledger at launch (no lease yet) must
    # suppress relaunch (in-flight) but must NOT appear in live_runs. Otherwise the drain's
    # marker-remove (registered = live_runs | open_pr_keys) would evict it from launched_pending
    # a turn early and re-open the launch->init blind window (flow-d4s).
    ws = _marked_ws(tmp_path)
    repo = es.resolve_maintainer_repo(ws)
    assert repo is not None
    fleet.register(fleet.resolve_fleet_dir(repo), "flow-fleet", "", now=utcnow_iso())
    run, _ = _dispatch(ready=[_cand("flow-fleet"), _cand("flow-y")])
    out = es.select(ws, cap=5, concurrency=3, runner=run)
    assert out["launch"] == ["flow-y"]  # fleet-live key suppressed, not relaunched
    assert out["skipped_in_flight"] == ["flow-fleet"]  # in-flight via fleet
    assert out["live_runs"] == []  # NOT in live_runs (lease-only, the bug guard)


def test_select_trivial_non_hot_downshifts_to_sonnet(tmp_path):
    ws = _marked_ws(tmp_path)
    run, _ = _dispatch(ready=[_cand("flow-t", labels=["evolve", "tier:trivial"])])
    out = es.select(ws, cap=5, concurrency=3, runner=run)
    assert out["launch"] == ["flow-t"]
    assert out["model_per_key"]["flow-t"] == "sonnet"


def test_select_light_non_hot_downshifts_to_sonnet(tmp_path):
    ws = _marked_ws(tmp_path)
    run, _ = _dispatch(ready=[_cand("flow-l", labels=["evolve", "tier:light"])])
    out = es.select(ws, cap=5, concurrency=3, runner=run)
    assert out["launch"] == ["flow-l"]
    assert out["model_per_key"]["flow-l"] == "sonnet"


def test_select_light_hot_never_downshifts(tmp_path):
    # a mis-stamped tier:light+hot bead launches (single hot, no in-flight); with no
    # worker_model configured, hot-first leaves it ABSENT from model_per_key (omit
    # --model), so it never downshifts to sonnet.
    ws = _marked_ws(tmp_path)
    run, _ = _dispatch(
        ready=[_cand("flow-lh", labels=["evolve", "tier:light", "hot"], blast="z.py")]
    )
    out = es.select(ws, cap=5, concurrency=3, runner=run)
    assert "flow-lh" in out["launch"]
    assert "flow-lh" not in out["model_per_key"]


def test_select_worker_model_light_beats_worker_model(tmp_path):
    ws = _worker_ws(tmp_path)
    run, _ = _dispatch(ready=[_cand("flow-l", labels=["evolve", "tier:light"])])
    out = es.select(ws, cap=5, concurrency=3, runner=run)
    assert out["model_per_key"]["flow-l"] == "sonnet"


def test_select_trivial_hot_never_downshifts(tmp_path):
    # belt-and-suspenders: a mis-stamped tier:trivial+hot bead launches (single hot,
    # no in-flight, so it takes the hot slot); with no worker_model configured,
    # hot-first leaves it ABSENT from model_per_key, so it never downshifts to sonnet.
    ws = _marked_ws(tmp_path)
    run, _ = _dispatch(
        ready=[_cand("flow-th", labels=["evolve", "tier:trivial", "hot"], blast="z.py")]
    )
    out = es.select(ws, cap=5, concurrency=3, runner=run)
    assert "flow-th" in out["launch"]  # launched, so absence below is the guard, not a hold
    assert "flow-th" not in out["model_per_key"]


def test_select_plain_bead_no_downshift(tmp_path):
    ws = _marked_ws(tmp_path)
    run, _ = _dispatch(ready=[_cand("flow-p")])  # default labels carry no tier
    out = es.select(ws, cap=5, concurrency=3, runner=run)
    assert out["launch"] == ["flow-p"]
    assert "flow-p" not in out["model_per_key"]


def test_select_worker_model_plain_bead(tmp_path):
    ws = _worker_ws(tmp_path)
    run, _ = _dispatch(ready=[_cand("flow-p")])  # non-trivial, non-hot
    out = es.select(ws, cap=5, concurrency=3, runner=run)
    assert out["model_per_key"]["flow-p"] == "opus"


def test_select_worker_model_trivial_beats_worker_model(tmp_path):
    ws = _worker_ws(tmp_path)
    run, _ = _dispatch(ready=[_cand("flow-t", labels=["evolve", "tier:trivial"])])
    out = es.select(ws, cap=5, concurrency=3, runner=run)
    assert out["model_per_key"]["flow-t"] == "sonnet"


def test_select_worker_model_hot_pinned_to_worker(tmp_path):
    # hot-first: a hot bead follows the split (opus plans/reviews, sonnet writes), so its
    # session is pinned to worker_model (opus) rather than omitted -- the explicit pin
    # keeps the opus judgment layer real instead of inheriting a maybe-Fable launcher.
    ws = _worker_ws(tmp_path)
    run, _ = _dispatch(ready=[_cand("flow-h", labels=["evolve", "hot"], blast="z.py")])
    out = es.select(ws, cap=5, concurrency=3, runner=run)
    assert "flow-h" in out["launch"]
    assert out["model_per_key"]["flow-h"] == "opus"


def test_select_worker_model_trivial_hot_stays_opus(tmp_path):
    # hot is checked BEFORE the tier branch, so a mis-stamped tier:trivial+hot bead pins
    # to worker_model (opus), NOT sonnet -- hot's opus session wins over the downshift.
    ws = _worker_ws(tmp_path)
    run, _ = _dispatch(
        ready=[_cand("flow-th", labels=["evolve", "tier:trivial", "hot"], blast="z.py")]
    )
    out = es.select(ws, cap=5, concurrency=3, runner=run)
    assert "flow-th" in out["launch"]
    assert out["model_per_key"]["flow-th"] == "opus"


def test_worker_model_reads_evolve_section(tmp_path):
    d = tmp_path / "flow"
    (d / ".flow").mkdir(parents=True)
    (d / ".flow" / "workspace.toml").write_text(
        '[evolve]\nworker_model = "opus"\n', encoding="utf-8"
    )
    assert es._worker_model(d) == "opus"


def test_worker_model_absent_section_is_none(tmp_path):
    d = tmp_path / "flow"
    (d / ".flow").mkdir(parents=True)
    (d / ".flow" / "workspace.toml").write_text(
        "[maintainer]\nself_target = true\n", encoding="utf-8"
    )
    assert es._worker_model(d) is None


def test_worker_model_empty_or_nonstr_is_none(tmp_path):
    for body in ('[evolve]\nworker_model = ""\n', "[evolve]\nworker_model = 5\n"):
        d = tmp_path / f"flow-{hash(body) & 0xFFFF}"
        (d / ".flow").mkdir(parents=True)
        (d / ".flow" / "workspace.toml").write_text(body, encoding="utf-8")
        assert es._worker_model(d) is None, body


def test_select_pre_pr_live_hot_blocks_second_hot(tmp_path):
    ws = _marked_ws(tmp_path)
    repo = es.resolve_maintainer_repo(ws)
    assert repo is not None
    _write_lease(_pool_run_dir(repo, "flow-old"))
    run, _ = _dispatch(
        ready=[_cand("flow-new", labels=["evolve", "hot"], blast="z.py")],
        evolve_list=[{"id": "flow-old", "labels": ["evolve", "hot"]}],
    )
    out = es.select(ws, cap=5, concurrency=3, runner=run)
    assert out["launch"] == []
    assert out["held_hot"] == ["flow-new"]
    assert out["live_runs"] == ["flow-old"]


# ---- fleet-derived launched_pending - the launch->init blind-window regression ----


def _ledger_add(repo: Path, key: str) -> None:
    """Seed a pre-lease fleet register, mirroring the launch-time write."""
    fleet.register(fleet.resolve_fleet_dir(repo), key, "", now=utcnow_iso())


def test_select_launched_key_is_inflight_not_relaunched(tmp_path):
    # a non-hot key registered in the fleet ledger (no ref, no lease yet) must
    # read as in-flight: held in skipped_in_flight, never re-launched.
    ws = _marked_ws(tmp_path)
    repo = es.resolve_maintainer_repo(ws)
    assert repo is not None
    _ledger_add(repo, "flow-hso")
    run, _ = _dispatch(ready=[_cand("flow-hso"), _cand("flow-y")])
    out = es.select(ws, cap=5, concurrency=3, runner=run)
    assert out["launch"] == ["flow-y"]
    assert out["skipped_in_flight"] == ["flow-hso"]
    assert out["launched_pending"] == ["flow-hso"]


def test_select_launched_hot_blocks_second_hot(tmp_path):
    # the real incident: flow-jud(hot) was launched (fleet-registered only, no ref/lease),
    # and the next pass must NOT offer flow-4lb as a 2nd hot. flow-hso (non-hot,
    # also launched) lands in skipped_in_flight, not re-launched.
    ws = _marked_ws(tmp_path)
    repo = es.resolve_maintainer_repo(ws)
    assert repo is not None
    _ledger_add(repo, "flow-jud")
    _ledger_add(repo, "flow-hso")
    run, _ = _dispatch(
        ready=[
            _cand("flow-4lb", labels=["evolve", "hot"], blast="z.py"),
            _cand("flow-hso"),
        ],
        # _hot_inflight reads the launched key as hot only if bd list reports it hot
        evolve_list=[{"id": "flow-jud", "labels": ["evolve", "hot"]}],
    )
    out = es.select(ws, cap=5, concurrency=3, runner=run)
    assert out["launch"] == []
    assert out["held_hot"] == ["flow-4lb"]
    assert "flow-hso" in out["skipped_in_flight"]
    assert set(out["launched_pending"]) == {"flow-jud", "flow-hso"}


# ---- budget shrinks by in-flight active-session count ----


def test_budget_subtracts_inflight_count():
    # active-session count (launched_pending UNION live_runs) shrinks the concurrency budget.
    cands = [_cand(f"flow-{i}") for i in range(10)]
    full = es.partition(cands, set(), False, 0, cap=10, concurrency=8, inflight_count=8)
    assert full["launch"] == []  # concurrency - inflight floored to 0
    partial = es.partition(cands, set(), False, 0, cap=10, concurrency=8, inflight_count=6)
    assert len(partial["launch"]) == 2  # min(10-0, 8-6)


def test_budget_open_prs_dont_consume_concurrency():
    # the discriminator: open PRs bound only the cap term, NOT the concurrency term.
    cands = [_cand(f"flow-{i}") for i in range(10)]
    out = es.partition(
        cands, set(), False, open_pr_count=4, cap=10, concurrency=8, inflight_count=0
    )
    assert len(out["launch"]) == 6  # min(10-4, 8-0), NOT 8-4


def test_select_budget_shrinks_with_launched_pending(tmp_path):
    # six launched_pending keys consume the budget; only concurrency-6 slots remain.
    ws = _marked_ws(tmp_path)
    repo = es.resolve_maintainer_repo(ws)
    assert repo is not None
    pending = [f"flow-p{i}" for i in range(1, 7)]
    for key in pending:
        _ledger_add(repo, key)
    # ready candidates are DISJOINT from the launched set, so they only feel the budget.
    run, _ = _dispatch(ready=[_cand(f"flow-r{i}") for i in range(5)])
    out = es.select(ws, cap=10, concurrency=8, runner=run)
    assert len(out["launch"]) == 2  # 8 - 6 launched_pending
    assert sorted(out["launched_pending"]) == sorted(pending)


# ---- _config_defaults (mirrors the queue_select suite; was uncovered) ----


def _ws_with_toml(tmp_path: Path, body: str) -> Path:
    d = tmp_path / "flow"
    (d / ".flow").mkdir(parents=True)
    (d / ".flow" / "workspace.toml").write_text(body, encoding="utf-8")
    return d


def test_config_defaults_reads_evolve_section(tmp_path):
    ws = _ws_with_toml(tmp_path, "[evolve]\ncap = 7\nconcurrency = 2\n")
    assert es._config_defaults(ws) == (7, 2)


def test_config_defaults_absent_section(tmp_path):
    ws = _ws_with_toml(tmp_path, "[maintainer]\nself_target = true\n")
    assert es._config_defaults(ws) == (es.DEFAULT_CAP, es.DEFAULT_CONCURRENCY)


def test_config_defaults_ignores_queue_section(tmp_path):
    ws = _ws_with_toml(tmp_path, "[queue]\ncap = 9\nconcurrency = 9\n")
    assert es._config_defaults(ws) == (es.DEFAULT_CAP, es.DEFAULT_CONCURRENCY)


def test_config_defaults_invalid_values(tmp_path):
    ws = _ws_with_toml(tmp_path, '[evolve]\ncap = 0\nconcurrency = "lots"\n')
    assert es._config_defaults(ws) == (es.DEFAULT_CAP, es.DEFAULT_CONCURRENCY)


def test_config_defaults_no_workspace(tmp_path):
    assert es._config_defaults(tmp_path / "nope") == (es.DEFAULT_CAP, es.DEFAULT_CONCURRENCY)


def test_select_launched_pending_open_hot_serializes_next_hot(tmp_path):
    # a hot key sits in launched_pending with status `open` (newly launched, pre-transition),
    # no lease and no ref/PR. It must consume the single hot slot, holding the next hot.
    ws = _marked_ws(tmp_path)
    repo = es.resolve_maintainer_repo(ws)
    assert repo is not None
    _ledger_add(repo, "flow-hot1")
    calls: Recorder = []

    def run(args):
        calls.append(args)
        if args[:2] == ["bd", "ready"]:
            return subprocess.CompletedProcess(
                args,
                0,
                json.dumps([_cand("flow-hot2", labels=["evolve", "hot"], blast="z.py")]),
                "",
            )
        if args[:2] == ["bd", "list"]:
            wanted = set(args[args.index("--status") + 1].split(","))
            beads = [{"id": "flow-hot1", "labels": ["evolve", "hot"], "status": "open"}]
            filtered = [b for b in beads if b.get("status") in wanted]
            return subprocess.CompletedProcess(args, 0, json.dumps(filtered), "")
        if args[:3] == ["gh", "pr", "list"]:
            return subprocess.CompletedProcess(args, 0, "[]", "")
        if args[:2] == ["git", "for-each-ref"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 1, "", f"unexpected: {args}")

    out = es.select(ws, cap=10, concurrency=8, runner=run)
    assert out["launch"] == []
    assert out["held_hot"] == ["flow-hot2"]
