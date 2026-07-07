"""Select + partition the next batch of evolve beads to launch (drain's select core).

Pure selection over flow's OWN backlog, no side effects. Given the ready evolve
beads plus the in-flight branches/PRs, decide which keys to fan out as
`/flow <key> --auto` runs. The `/flow evolve drain` loop consumes this (via
`evolve_drain.py`, which adds in-flight lease liveness) and does the launching.

Partition is best-effort coarse, NOT a disjointness guarantee. Planning is
post-launch (the headless Plan subagent runs after `claude --bg` fires), so the
selector never knows a bead's real file set. It serializes on the two signals it
does have (the `hot` label + a primary-file anchor parsed from the bead's BLAST
RADIUS line) and relies on the keystone gate: each run is worktree/lease-isolated,
so any residual file overlap surfaces as a merge conflict at human review
(friction, never corruption). Keep CONCURRENCY low so that stays rare.

Selection inputs (all read-only, queryable):
  - `bd ready -l evolve --json`, open dependency-unblocked candidates (bd ready
    already excludes blocked beads and carries structured `labels` incl. `hot`).
  - `gh pr list` + `git for-each-ref`, in-flight join by branch name
    `feat/<key>-*` (drop already-running beads; count open PRs for backpressure).

Lib only (no CLI): consumed by evolve_drain.py's select + decide round.
Raises NotMaintainer (drain maps to exit 4) and ToolError (exit 2).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from _evolve_common import ACTIVE_STATUSES as _ACTIVE_STATUSES
from _evolve_common import BRANCH_PREFIXES as _BRANCH_PREFIXES
from _evolve_common import NotMaintainer, backpressure_budget, bead_labels, primary_anchor
from _evolve_common import fleet_live_keys as _fleet_live_keys
from _evolve_common import gather_refs as _gather_refs_common
from _evolve_common import is_inflight as _is_inflight
from _evolve_common import key_from_ref as _key_from_ref
from _evolve_common import live_run_keys as _live_run_keys
from _evolve_common import loads as _loads
from _evolve_common import model_per_key as _model_per_key
from _evolve_common import ok as _ok
from _evolve_common import read_cap_concurrency as _read_cap_concurrency
from _evolve_common import read_worker_model as _worker_model
from _runner import CwdRunner as Runner
from _runner import cwd_default_runner as _default_runner
from maintainer import resolve_maintainer_repo

DEFAULT_CAP = 5
DEFAULT_CONCURRENCY = 3


def partition(
    candidates: list[dict[str, Any]],
    inflight_keys: set[str],
    hot_inflight: bool,
    open_pr_count: int,
    cap: int = DEFAULT_CAP,
    concurrency: int = DEFAULT_CONCURRENCY,
    inflight_count: int = 0,
    include_proposals: bool = False,
) -> dict[str, Any]:
    """Pure core: decide the launch batch from already-extracted inputs.

    candidates: parsed `bd ready -l evolve` items (id, priority, labels, issue_type,
    description). Epics are skipped (you launch a run on leaf work, not a container).
    `inflight_count` shrinks the concurrency budget by the in-flight active-session
    count (launched_pending UNION live_runs), so launched-but-not-yet-open runs back off.
    Generative proposals now live in a separate (non-`evolve`) backlog and only
    reach drain at all if mislabeled; the `proposal`-exclusion filter in `active`
    is retained as a defensive guard (judgment work never auto-launches), no
    longer surfaced as `held_proposal`.

    `include_proposals` is the DANGEROUS opt-in: it drops the proposal-exclusion
    guard so judgment beads auto-launch alongside audit work, bypassing the human
    spec-plan accept gate. The caller also has to feed the proposal candidates in
    (see `select`); flipping this alone over an evolve-only candidate set is a no-op.
    """
    skipped_in_flight = [c["id"] for c in candidates if c.get("id") in inflight_keys]

    active = [
        c
        for c in candidates
        if c.get("id")
        and c.get("issue_type") != "epic"
        and (include_proposals or "proposal" not in (c.get("labels") or []))
        and c["id"] not in inflight_keys
    ]
    active.sort(key=lambda c: (c.get("priority", 99), str(c.get("id"))))

    if open_pr_count >= cap:
        return {
            "launch": [],
            "skipped_in_flight": skipped_in_flight,
            "held_backpressure": True,
            "held_hot": [],
            "held_anchor": [],
        }

    budget = backpressure_budget(cap, open_pr_count, concurrency, inflight_count)
    launch: list[str] = []
    held_hot: list[str] = []
    held_anchor: list[str] = []
    used_anchors: set[str] = set()
    hot_used = hot_inflight  # a hot PR already open consumes the single hot slot

    for c in active:
        key = c["id"]
        labels = c.get("labels") or []
        is_hot = "hot" in labels
        anchor = primary_anchor(c.get("description", ""))
        if is_hot and hot_used:
            held_hot.append(key)
            continue
        if anchor and anchor in used_anchors:
            held_anchor.append(key)
            continue
        if len(launch) >= budget:
            break
        launch.append(key)
        if is_hot:
            hot_used = True
        if anchor:
            used_anchors.add(anchor)

    return {
        "launch": launch,
        "skipped_in_flight": skipped_in_flight,
        "held_backpressure": False,
        "held_hot": held_hot,
        "held_anchor": held_anchor,
    }


def _gather_refs(runner: Runner) -> tuple[set[str], set[str], int]:
    """Return (in-flight head refs, open-PR head refs, GLOBAL open flow-PR count)."""
    refs, pr_refs = _gather_refs_common(runner)
    open_pr_count = sum(
        1 for r in pr_refs if any(r.startswith(f"{p}flow-") for p in _BRANCH_PREFIXES)
    )
    return refs, pr_refs, open_pr_count


def _hot_inflight(
    runner: Runner,
    refs: set[str],
    *,
    include_proposals: bool = False,
    extra_keys: frozenset[str] | set[str] = frozenset(),
) -> bool:
    """True if any in-flight `feat/flow-*` ref maps to a hot evolve bead.

    Under `include_proposals` the hot slot also serializes hot *proposal* beads, so
    a hot proposal already in flight blocks the next hot launch (the `proposal`
    label can carry `hot` too, see references/verb-evolve.md §propose).

    `extra_keys` seeds the in-flight set with keys known live by another channel
    (e.g. a pre-PR lease that has no ref/PR yet), so a hot pre-PR run blocks the
    next hot launch.
    """
    inflight_flow_keys = {k for r in refs if (k := _key_from_ref(r))} | set(extra_keys)
    if not inflight_flow_keys:
        return False
    labels = bead_labels(include_proposals)
    hot_keys: set[str] = set()
    for label in labels:
        raw = _ok(
            runner(
                ["bd", "list", "-l", label, "--status", _ACTIVE_STATUSES, "--limit", "0", "--json"]
            ),
            "bd list",
        )
        hot_keys |= {
            str(b["id"])
            for b in _loads(raw)
            if isinstance(b, dict) and b.get("id") and "hot" in (b.get("labels") or [])
        }
    return bool(inflight_flow_keys & hot_keys)


def _ready_candidates(run: Runner, include_proposals: bool) -> list[dict[str, Any]]:
    """Ready evolve beads, plus the `proposal` backlog when explicitly opted in.

    Two label-scoped `bd ready` calls merged by id (not `-l evolve,proposal`, whose
    AND/OR semantics are ambiguous); a bead carrying both labels is kept once.
    """
    cands = _loads(_ok(run(["bd", "ready", "-l", "evolve", "--json"]), "bd ready"))
    if include_proposals:
        seen = {c.get("id") for c in cands}
        props = _loads(_ok(run(["bd", "ready", "-l", "proposal", "--json"]), "bd ready"))
        cands += [p for p in props if p.get("id") and p.get("id") not in seen]
    return cands


def select(
    workspace_root: Path,
    *,
    cap: int,
    concurrency: int,
    runner: Runner | None = None,
    include_proposals: bool = False,
) -> dict[str, Any]:
    repo = resolve_maintainer_repo(workspace_root)
    if repo is None:
        raise NotMaintainer("not a flow maintainer setup; nothing to select")
    run = runner or _default_runner(repo)

    candidates = _ready_candidates(run, include_proposals)
    refs, pr_refs, open_pr_count = _gather_refs(run)
    # live_keys is LEASE-ONLY: it surfaces as result["live_runs"], which the drain
    # uses to decide a launched run has "registered" a lease (evolve_drain.cli_main).
    # Fleet registers at launch (before claude --bg), so letting fleet leak into
    # live_runs would mark a still-booting pre-lease run "registered" and evict it
    # from launched_pending a turn early, re-opening the launch->init blind window
    # (flow-d4s). The reconciled lease|fleet read is for the IN-FLIGHT suppression set
    # only (don't re-launch / don't over-budget a fleet-live run); flow-8by2.3.
    live_keys = _live_run_keys(repo)  # lease-only -> result["live_runs"]
    fleet_keys = _fleet_live_keys(repo)  # lease | fleet (reconciled in-flight authority)
    launched_keys = fleet_keys - live_keys  # pre-lease fleet entries -> result["launched_pending"]
    inflight_pre = fleet_keys
    inflight_keys = {
        c["id"] for c in candidates if c.get("id") and _is_inflight(c["id"], refs)
    } | inflight_pre
    hot_inflight = _hot_inflight(
        run, refs, include_proposals=include_proposals, extra_keys=inflight_pre
    )

    result = partition(
        candidates,
        inflight_keys,
        hot_inflight,
        open_pr_count,
        cap=cap,
        concurrency=concurrency,
        inflight_count=len(inflight_pre),
        include_proposals=include_proposals,
    )
    result["cap"] = cap
    result["concurrency"] = concurrency
    result["open_pr_count"] = open_pr_count
    # the flow keys behind the open PRs, so evolve_drain reuses this gather
    # instead of re-running `gh pr list`
    result["open_pr_keys"] = sorted({k for r in pr_refs if (k := _key_from_ref(r))})
    result["include_proposals"] = include_proposals
    result["live_runs"] = sorted(live_keys)
    result["launched_pending"] = sorted(launched_keys)
    labels_by_id = {c["id"]: (c.get("labels") or []) for c in candidates if c.get("id")}
    result["model_per_key"] = _model_per_key(
        result["launch"], labels_by_id, _worker_model(workspace_root)
    )
    return result


def _config_defaults(workspace_root: Path) -> tuple[int, int]:
    return _read_cap_concurrency(workspace_root, "evolve", DEFAULT_CAP, DEFAULT_CONCURRENCY)
