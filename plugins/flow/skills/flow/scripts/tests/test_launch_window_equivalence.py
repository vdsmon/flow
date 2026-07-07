"""Property-equivalence proof: fleet-derived launched_pending vs the retired
launch_ledger TTL-marker logic (flow-8by2.5, the merge-gate deliverable).

`oracle_launched_pending` replicates the retired launch_ledger net effect
(`markers - registered`, with the physical remove()-latch modeled explicitly)
without importing the deleted module, so the proof survives the deletion.
Each lifecycle test drives the REAL production pipeline (fleet.py seeding,
evolve_select.select, evolve_drain.liveness_map/stranded_pre_pr/decide) and
asserts its post-reconciliation launched_pending / decide() action matches
the oracle, except for the one pinned divergence (crash-post-lease), which is
asserted bounded by fleet.STALE_AFTER_S and safe-direction (never a premature
`done`).
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import evolve_drain as ed
import evolve_select as es
import fleet
import lease
from _timeutil import utcnow_iso

Recorder = list[list[str]]

# the retired launch_ledger.LAUNCH_TTL_SECONDS carried the same ceiling
TTL = fleet.STALE_AFTER_S


def _offset(base: str, secs: int) -> str:
    dt = lease.parse_iso(base)
    assert dt is not None
    return (dt + timedelta(seconds=secs)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _age(now: str, ts: str) -> float:
    now_dt = lease.parse_iso(now)
    ts_dt = lease.parse_iso(ts)
    assert now_dt is not None
    assert ts_dt is not None
    return (now_dt - ts_dt).total_seconds()


def oracle_launched_pending(
    launched_at: dict[str, str],
    latched: set[str],
    registered_now: set[str],
    now: str,
    ttl: int = TTL,
) -> set[str]:
    """Retired launch_ledger's net effect: TTL-live, un-latched markers minus registered.

    `latched` models `launch_ledger.remove()`'s physical, one-way marker deletion at
    first registration (any lease/PR): a key that ever left `latched` never re-enters
    the marker set even if it later crashes and drops back out of `registered`. The
    fleet ledger has no such latch (`register` is a bare upsert) -- that is the one
    behavioral delta this file exists to bound (see the crash-post-lease test).
    """
    live = {k for k, ts in launched_at.items() if k not in latched and _age(now, ts) < ttl}
    return live - registered_now


# ─── fixtures ──────────────────────────────────────────────────────────────


def _marked_ws(tmp_path: Path) -> Path:
    d = tmp_path / "flow"
    (d / ".flow").mkdir(parents=True)
    (d / ".flow" / "workspace.toml").write_text(
        "[maintainer]\nself_target = true\n", encoding="utf-8"
    )
    return d


def _pool_run_dir(repo: Path, key: str, slug: str = "wip") -> Path:
    return repo / ".flow" / "worktrees" / f"feat-{key}-{slug}" / ".flow" / "runs" / key


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


def _cp(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


def _runner(
    *,
    ready: list[dict] | None = None,
    open_prs: list[dict] | None = None,
    merged_prs: list[dict] | None = None,
) -> tuple[Callable[[list[str]], subprocess.CompletedProcess[str]], Recorder]:
    """Stub tool runner covering every call select() + stranded_pre_pr() can make."""
    calls: Recorder = []

    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        if args[:2] == ["bd", "ready"]:
            return _cp(json.dumps(ready or []))
        if args[:2] == ["bd", "list"]:
            return _cp("[]")  # no hot beads in these scenarios
        if args[:3] == ["gh", "pr", "list"]:
            if "--state" in args and args[args.index("--state") + 1] == "merged":
                return _cp(json.dumps(merged_prs or []))
            return _cp(json.dumps(open_prs or []))
        if args[:2] == ["git", "for-each-ref"]:
            return _cp("")
        raise AssertionError(f"unexpected tool call: {args}")

    return run, calls


def _select_and_reconcile(ws: Path, run, *, cap: int = 5, concurrency: int = 3) -> dict:
    """select() + the drain reconciliation line (`pending - registered`) that
    evolve_drain.cli_main / queue_drain.cli_main apply -- the only launch_ledger-era
    step that survives the retirement (the physical marker remove() does not)."""
    sel = es.select(ws, cap=cap, concurrency=concurrency, runner=run)
    registered = set(sel["live_runs"]) | set(sel["open_pr_keys"])
    sel["launched_pending"] = sorted(set(sel["launched_pending"]) - registered)
    return sel


def _liveness(repo: Path, sel: dict) -> dict[str, str]:
    inflight = sorted(
        set(sel.get("skipped_in_flight") or []) | set(sel["open_pr_keys"]) | set(sel["live_runs"])
    )
    return ed.liveness_map(repo, inflight)


# ─── invariant (c), direct: decide() is pure, test it without any plumbing ──


def test_decide_never_returns_done_while_launched_pending_nonempty():
    result = ed.decide({"launch": [], "launched_pending": ["flow-x"]}, {}, stranded=[])
    assert result["action"] == "wait"
    assert result["action"] != "done"


# ─── lifecycle rows: must-match states ──────────────────────────────────────


def test_pre_lease_pre_pr_blind_window_blocks_termination(tmp_path):
    # invariant (a): a just-launched, pre-lease, pre-PR run is IN launched_pending
    # and blocks the drain from reading `done`.
    ws = _marked_ws(tmp_path)
    repo = es.resolve_maintainer_repo(ws)
    assert repo is not None
    now0 = utcnow_iso()
    fleet.register(fleet.resolve_fleet_dir(repo), "flow-a", "", now=now0)

    run, _ = _runner()
    sel = _select_and_reconcile(ws, run)
    assert sel["launched_pending"] == ["flow-a"]

    oracle = oracle_launched_pending(
        {"flow-a": now0}, latched=set(), registered_now=set(), now=utcnow_iso()
    )
    assert set(sel["launched_pending"]) == oracle == {"flow-a"}

    result = ed.decide(sel, _liveness(repo, sel), stranded=[])
    assert result["action"] == "wait"


def test_lease_live_drops_pending_but_blocks_via_liveness(tmp_path):
    # a run that acquired its lease: dropped from launched_pending (old and new
    # agree on the net set), but the drain still waits -- on the live lease, not
    # on the launch window.
    ws = _marked_ws(tmp_path)
    repo = es.resolve_maintainer_repo(ws)
    assert repo is not None
    now0 = utcnow_iso()
    fleet.register(fleet.resolve_fleet_dir(repo), "flow-a", "", now=now0)
    _write_lease(_pool_run_dir(repo, "flow-a"))

    run, _ = _runner()
    sel = _select_and_reconcile(ws, run)
    assert sel["launched_pending"] == []
    assert sel["live_runs"] == ["flow-a"]

    oracle = oracle_launched_pending(
        {"flow-a": now0}, latched=set(), registered_now={"flow-a"}, now=utcnow_iso()
    )
    assert set(sel["launched_pending"]) == oracle == set()

    liveness = _liveness(repo, sel)
    assert liveness == {"flow-a": "live"}
    result = ed.decide(sel, liveness, stranded=[])
    assert result["action"] == "wait"


def test_open_pr_lease_expired_parks_and_reads_done(tmp_path):
    # registered via the OPEN PR alone (session ended, lease expired): parks,
    # never blocks -- the run is done bootstrapping either way.
    ws = _marked_ws(tmp_path)
    repo = es.resolve_maintainer_repo(ws)
    assert repo is not None
    now0 = utcnow_iso()
    fleet.register(fleet.resolve_fleet_dir(repo), "flow-a", "", now=now0)
    _write_lease(_pool_run_dir(repo, "flow-a"), expired=True)

    run, _ = _runner(open_prs=[{"headRefName": "feat/flow-a-wip"}])
    sel = _select_and_reconcile(ws, run)
    assert sel["open_pr_keys"] == ["flow-a"]
    assert sel["launched_pending"] == []

    oracle = oracle_launched_pending(
        {"flow-a": now0}, latched=set(), registered_now={"flow-a"}, now=utcnow_iso()
    )
    assert set(sel["launched_pending"]) == oracle == set()

    result = ed.decide(sel, _liveness(repo, sel), stranded=[])
    assert result["action"] == "done"
    assert result["parked"] == ["flow-a"]


def test_clean_finish_and_merged_not_open_never_reappears(tmp_path):
    # invariant (b): a registered-then-cleanly-finished run is never IN
    # launched_pending again. Clean finish deregisters the fleet entry
    # (dispatch_stage.cmd_finish -> fleet.deregister_run); its PR, now merged,
    # is absent from the open-PR gather by construction (`--state open`).
    ws = _marked_ws(tmp_path)
    repo = es.resolve_maintainer_repo(ws)
    assert repo is not None
    fleet.register(fleet.resolve_fleet_dir(repo), "flow-a", "run-1", now=utcnow_iso())
    fleet.deregister_run(ws, "flow-a", run_id="run-1")

    run, _ = _runner(merged_prs=[{"headRefName": "feat/flow-a-wip", "number": 9}])
    sel = _select_and_reconcile(ws, run)
    assert sel["launched_pending"] == []
    assert sel["open_pr_keys"] == []  # merged, not open: never leaks in

    oracle = oracle_launched_pending({}, latched=set(), registered_now=set(), now=utcnow_iso())
    assert set(sel["launched_pending"]) == oracle == set()

    result = ed.decide(sel, _liveness(repo, sel), stranded=[])
    assert result["action"] == "done"


def test_crash_pre_lease_ttl_boundary_exact(tmp_path):
    # exact boundary on the shared primitive: age < STALE_AFTER_S is live, >= is
    # not -- the same `<` launch_ledger.live_keys used, so the cutoff carries
    # over byte-for-byte.
    fd = tmp_path / "fleet"
    now = "2020-01-01T01:00:00Z"
    fleet.register(fd, "flow-a", "", now=_offset(now, -(TTL - 1)))
    assert fleet.live_keys(fd, now=now) == {"flow-a"}
    fleet.register(fd, "flow-b", "", now=_offset(now, -TTL))
    assert fleet.live_keys(fd, now=now) == {"flow-a"}  # flow-b aged out at exactly TTL


def test_crash_pre_lease_select_level_margin(tmp_path):
    # select()-level, comfortable margins either side of the boundary: a
    # fresh-enough fleet entry blocks; a stale one lets the drain read `done`
    # (a crashed pre-lease run ages out, matching the retired marker TTL).
    ws = _marked_ws(tmp_path)
    repo = es.resolve_maintainer_repo(ws)
    assert repo is not None
    fd = fleet.resolve_fleet_dir(repo)
    now0 = utcnow_iso()
    fresh_at = _offset(now0, -(TTL - 60))
    stale_at = _offset(now0, -(TTL + 60))
    fleet.register(fd, "flow-fresh", "", now=fresh_at)
    fleet.register(fd, "flow-stale", "", now=stale_at)

    run, _ = _runner()
    sel = _select_and_reconcile(ws, run)
    assert sel["launched_pending"] == ["flow-fresh"]

    oracle = oracle_launched_pending(
        {"flow-fresh": fresh_at, "flow-stale": stale_at},
        latched=set(),
        registered_now=set(),
        now=utcnow_iso(),
    )
    assert set(sel["launched_pending"]) == oracle == {"flow-fresh"}


def test_crash_post_lease_divergence_bounded_by_staleness(tmp_path):
    # the ONE real non-equivalence (flow-8by2.5 commitment 2): launch_ledger
    # LATCHED "registered" by physically deleting its marker the first time a
    # lease/PR registered; the fleet ledger has no latch, so a run that goes
    # live then crashes WITHOUT ever opening a PR re-enters launched_pending
    # instead of staying latched gone. Net: stranded-recovery is delayed from
    # ~lease-expiry (OLD, immediate `recover`) to ~fleet-staleness (NEW, `wait`
    # until STALE_AFTER_S, then `recover`) -- both bounded by STALE_AFTER_S,
    # both the conservative, never-premature-`done` direction.
    ws = _marked_ws(tmp_path)
    repo = es.resolve_maintainer_repo(ws)
    assert repo is not None
    fd = fleet.resolve_fleet_dir(repo)
    now0 = utcnow_iso()
    heartbeat_at = _offset(now0, -300)  # last heartbeat 5 min before the crash
    fleet.register(fd, "flow-a", "run-real", now=heartbeat_at)
    _write_lease(_pool_run_dir(repo, "flow-a"), expired=True)  # acquired once, now dead

    run, _ = _runner()
    sel = _select_and_reconcile(ws, run)
    assert sel["launched_pending"] == ["flow-a"]  # NEW: fleet entry lingers, still pending

    # OLD: the marker was physically removed the turn the lease first went live,
    # so by crash time it no longer exists -- `latched` models that.
    oracle_new = oracle_launched_pending(
        {"flow-a": heartbeat_at}, latched=set(), registered_now=set(), now=utcnow_iso()
    )
    oracle_old = oracle_launched_pending(
        {"flow-a": heartbeat_at}, latched={"flow-a"}, registered_now=set(), now=utcnow_iso()
    )
    assert set(sel["launched_pending"]) == oracle_new == {"flow-a"}
    assert oracle_old == set()  # OLD's marker is already gone

    new_stranded = ed.stranded_pre_pr(
        repo,
        run,
        launched_pending=set(sel["launched_pending"]),
        open_pr_keys=set(),
        in_progress_keys={"flow-a"},
    )
    old_stranded = ed.stranded_pre_pr(
        repo, run, launched_pending=oracle_old, open_pr_keys=set(), in_progress_keys={"flow-a"}
    )
    assert new_stranded == []  # NEW: still counted booting, not yet stranded
    assert [e["key"] for e in old_stranded] == ["flow-a"]  # OLD: immediately stranded

    new_result = ed.decide(sel, _liveness(repo, sel), stranded=[])
    assert new_result["action"] == "wait"  # bounded, safe: never done, never a premature recover
    old_result = ed.decide(sel, {}, stranded=["flow-a"])
    assert old_result["action"] == "recover"

    # bound: once the fleet entry itself ages past STALE_AFTER_S, NEW converges
    # to the same recover outcome (no wall-clock sleep -- backdate the write).
    fleet.register(fd, "flow-a", "run-real", now=_offset(now0, -(TTL + 60)))
    sel_aged = _select_and_reconcile(ws, run)
    assert sel_aged["launched_pending"] == []
    aged_stranded = ed.stranded_pre_pr(
        repo,
        run,
        launched_pending=set(sel_aged["launched_pending"]),
        open_pr_keys=set(),
        in_progress_keys={"flow-a"},
    )
    assert [e["key"] for e in aged_stranded] == ["flow-a"]
    aged_result = ed.decide(sel_aged, _liveness(repo, sel_aged), stranded=["flow-a"])
    assert aged_result["action"] == "recover"
