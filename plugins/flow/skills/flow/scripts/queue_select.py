"""Select + partition the next bounded batch of day-job planning candidates.

Day-job sibling of `evolve_select.py`: pure selection over the project's
NON-evolve backlog, no side effects. Given the ready beads plus the in-flight
branches/PRs, decide which keys fit the next candidate batch. The day-job queue-drain
loop consumes this and reports `plan_required`; selection never authorizes delivery.

Day-job = `bd ready --json` (unlabelled) minus epics and minus beads labelled
`evolve` (the evolve drain's queue), `proposal` (judgment work is excluded by
default with no opt-in here), `hot` (a hot non-evolve bead would be invisible to
evolve's one-hot gate, so it is excluded here too), or `hitl` (human-in-the-loop:
resolves only through a live exchange). No hot-serialization layer. Hotness is
evolve-machinery-only.

Backpressure is queue-scoped: only open `feat/flow-*` PRs whose key is NOT
an active evolve bead count toward the `[queue]` cap, and the concurrency
budget's in-flight session count (the shared worktree pool + fleet ledger)
subtracts the same active-evolve set, so a busy evolve drain never starves
this queue. Conservative edge: a flow-key PR whose evolve bead is already
closed/deferred counts toward the day-job cap (transient under-selection,
never over-selection).

Partition is best-effort coarse, not a disjointness guarantee. The selector does not
know a ticket's final planned file set. It serializes on the primary-file anchor
parsed from the bead's BLAST RADIUS line; attended driver planning establishes the
actual scope before a run starts.

Lib only (no CLI): consumed by queue_drain.py's select + decide round and
queue_status.py's advisory. Raises NotMaintainer (callers map to exit 4) and
ToolError (exit 2).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from _evolve_common import (
    NotMaintainer,
    active_evolve_keys,
    backpressure_budget,
    fleet_live_keys,
    gather_refs,
    is_inflight,
    key_from_ref,
    live_run_keys,
    loads,
    model_per_key,
    ok,
    primary_anchor,
    read_cap_concurrency,
)
from _evolve_common import read_worker_model as _worker_model
from _runner import CwdRunner as Runner
from _runner import cwd_default_runner as _default_runner
from maintainer import resolve_maintainer_repo

DEFAULT_CAP = 5
DEFAULT_CONCURRENCY = 3
_EXCLUDED_LABELS = {"evolve", "proposal", "hot", "hitl"}


def _day_job(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        c
        for c in candidates
        if c.get("id")
        and c.get("issue_type") != "epic"
        and not (_EXCLUDED_LABELS & set(c.get("labels") or []))
    ]


def partition(
    candidates: list[dict[str, Any]],
    inflight_keys: set[str],
    open_pr_count: int,
    cap: int = DEFAULT_CAP,
    concurrency: int = DEFAULT_CONCURRENCY,
    inflight_count: int = 0,
) -> dict[str, Any]:
    """Pure core: decide the candidate batch from already-extracted inputs.

    candidates: parsed `bd ready --json` items (id, priority, labels,
    issue_type, description). The day-job filter applies BEFORE the in-flight
    split, so an in-flight bead from another queue (e.g. an evolve bead the
    evolve drain is running) never surfaces in `skipped_in_flight`. The
    queue-drain's liveness wait stays scoped to its own queue.
    """
    day_job = _day_job(candidates)
    skipped_in_flight = [c["id"] for c in day_job if c["id"] in inflight_keys]

    active = [c for c in day_job if c["id"] not in inflight_keys]
    active.sort(key=lambda c: (c.get("priority", 99), str(c.get("id"))))

    if open_pr_count >= cap:
        return {
            "launch": [],
            "skipped_in_flight": skipped_in_flight,
            "held_backpressure": True,
            "held_anchor": [],
        }

    budget = backpressure_budget(cap, open_pr_count, concurrency, inflight_count)
    launch: list[str] = []
    held_anchor: list[str] = []
    used_anchors: set[str] = set()

    for c in active:
        key = c["id"]
        anchor = primary_anchor(c.get("description", ""))
        if anchor and anchor in used_anchors:
            held_anchor.append(key)
            continue
        if len(launch) >= budget:
            break
        launch.append(key)
        if anchor:
            used_anchors.add(anchor)

    return {
        "launch": launch,
        "skipped_in_flight": skipped_in_flight,
        "held_backpressure": False,
        "held_anchor": held_anchor,
    }


def select(
    workspace_root: Path,
    *,
    cap: int,
    concurrency: int,
    runner: Runner | None = None,
) -> dict[str, Any]:
    repo = resolve_maintainer_repo(workspace_root)
    if repo is None:
        raise NotMaintainer("not a flow maintainer setup; nothing to select")
    run = runner or _default_runner(repo)

    candidates = loads(ok(run(["bd", "ready", "--json"]), "bd ready"))
    refs, pr_refs = gather_refs(run)
    # live_keys is LEASE-ONLY (-> result["live_runs"], which the queue-drain uses to
    # decide a launched run has "registered" a lease); fleet must NOT leak into it or
    # a still-booting pre-lease run gets evicted from launched_pending a turn early
    # (flow-d4s). The reconciled lease|fleet read is for the in-flight suppression
    # set only.
    live_keys = live_run_keys(repo)
    fleet_keys = fleet_live_keys(repo)
    launched_keys = fleet_keys - live_keys  # pre-lease fleet entries -> result["launched_pending"]
    sessions = fleet_keys
    pr_keys = {k for r in pr_refs if (k := key_from_ref(r))}
    # The PR set, worktree pool, and fleet ledger are all repo-global (shared with
    # the evolve drain), so the active-evolve keys leave BOTH backpressure terms: an
    # evolve PR belongs to the evolve queue's cap, and an evolve session must not
    # consume this queue's concurrency budget (a saturated evolve drain would
    # otherwise zero it and starve the day-job queue). One query serves both;
    # skipped when there is nothing to subtract from. A key whose evolve bead is
    # already closed/deferred still counts here (transient under-selection only).
    active_evolve = active_evolve_keys(run) if (pr_keys or sessions) else set()
    open_pr_keys = sorted(pr_keys - active_evolve)
    inflight_keys = {
        c["id"] for c in candidates if c.get("id") and is_inflight(c["id"], refs)
    } | fleet_keys

    result = partition(
        candidates,
        inflight_keys,
        len(open_pr_keys),
        cap=cap,
        concurrency=concurrency,
        inflight_count=len(sessions - active_evolve),
    )
    result["cap"] = cap
    result["concurrency"] = concurrency
    result["open_pr_count"] = len(open_pr_keys)
    # day-job-scoped, so the queue-drain loop reuses this gather instead of
    # re-running `gh pr list`
    result["open_pr_keys"] = open_pr_keys
    result["live_runs"] = sorted(live_keys)
    result["launched_pending"] = sorted(launched_keys)
    labels_by_id = {c["id"]: (c.get("labels") or []) for c in candidates if c.get("id")}
    result["model_per_key"] = model_per_key(
        result["launch"], labels_by_id, _worker_model(workspace_root)
    )
    return result


def _config_defaults(workspace_root: Path) -> tuple[int, int]:
    return read_cap_concurrency(workspace_root, "queue", DEFAULT_CAP, DEFAULT_CONCURRENCY)
