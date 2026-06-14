"""Select + partition the next batch of day-job beads to launch (queue-drain's select core).

Day-job sibling of `evolve_select.py`: pure selection over the project's
NON-evolve backlog, no side effects. Given the ready beads plus the in-flight
branches/PRs, decide which keys to fan out as `/flow <key> --auto` runs. The
day-job queue-drain loop (flow-hw1.3) consumes this and does the launching.

Day-job = `bd ready --json` (unlabelled) minus epics and minus beads labelled
`evolve` (the evolve drain's queue), `proposal` (judgment work never
auto-launches; no opt-in exists here), or `hot` (a hot non-evolve bead would be
invisible to evolve's one-hot gate, so it never auto-launches either). No
hot-serialization layer — hotness is evolve-machinery-only.

Backpressure is queue-scoped: only open `feature/flow-*` PRs whose key is NOT
an active evolve bead count toward the `[queue]` cap, so a busy evolve drain
never starves this queue. Conservative edge: a flow-key PR whose evolve bead is
already closed/deferred counts toward the day-job cap (transient
under-launching, never over-launching).

Partition is best-effort coarse, NOT a disjointness guarantee — planning is
post-launch, so the selector never knows a bead's real file set. It serializes
on the primary-file anchor parsed from the bead's BLAST RADIUS line and relies
on the keystone gate: each run is worktree/lease-isolated, so any residual file
overlap surfaces as a merge conflict at human review — friction, never
corruption.

CLI:
  queue_select.py --workspace-root <dir> [--cap N] [--concurrency N]

Exit codes:
  0 = ok (prints the selection JSON)
  2 = tool error (bd/git/gh failed; stderr propagated)
  4 = not a maintainer setup (dormant; nothing selected)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import launch_ledger
from _evolve_common import (
    ACTIVE_STATUSES,
    NotMaintainer,
    ToolError,
    fleet_live_keys,
    gather_refs,
    is_inflight,
    key_from_ref,
    live_run_keys,
    loads,
    ok,
    primary_anchor,
)
from _runner import CwdRunner as Runner
from _runner import cwd_default_runner as _default_runner
from _workspace import WorkspaceConfigError, load_workspace_toml
from maintainer import resolve_maintainer_repo

DEFAULT_CAP = 5
DEFAULT_CONCURRENCY = 3
_EXCLUDED_LABELS = {"evolve", "proposal", "hot"}


def _day_job(candidates: list[dict]) -> list[dict]:
    return [
        c
        for c in candidates
        if c.get("id")
        and c.get("issue_type") != "epic"
        and not (_EXCLUDED_LABELS & set(c.get("labels") or []))
    ]


def partition(
    candidates: list[dict],
    inflight_keys: set[str],
    open_pr_count: int,
    cap: int = DEFAULT_CAP,
    concurrency: int = DEFAULT_CONCURRENCY,
    inflight_count: int = 0,
) -> dict:
    """Pure core: decide the launch batch from already-extracted inputs.

    candidates: parsed `bd ready --json` items (id, priority, labels,
    issue_type, description). The day-job filter applies BEFORE the in-flight
    split, so an in-flight bead from another queue (e.g. an evolve bead the
    evolve drain is running) never surfaces in `skipped_in_flight` — the
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

    # inflight_count = active sessions (launched_pending UNION live_runs), subtracted
    # from the concurrency simultaneity bound (open PRs are bounded separately by cap)
    budget = min(cap - open_pr_count, max(0, concurrency - inflight_count))
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


def _day_job_open_prs(runner: Runner, pr_refs: set[str]) -> list[str]:
    """The day-job keys behind the open flow-* PRs (evolve PRs excluded).

    A PR key is evolve's iff `bd list -l evolve` over the active statuses knows
    it; everything else (incl. a key whose evolve bead is already closed)
    counts toward THIS queue's cap.
    """
    pr_keys = {k for r in pr_refs if (k := key_from_ref(r))}
    if not pr_keys:
        return []
    raw = ok(
        runner(["bd", "list", "-l", "evolve", "--status", ACTIVE_STATUSES, "--json"]),
        "bd list",
    )
    active_evolve = {str(b["id"]) for b in loads(raw) if isinstance(b, dict) and b.get("id")}
    return sorted(pr_keys - active_evolve)


def select(
    workspace_root: Path,
    *,
    cap: int,
    concurrency: int,
    runner: Runner | None = None,
) -> dict:
    repo = resolve_maintainer_repo(workspace_root)
    if repo is None:
        raise NotMaintainer("not a flow maintainer setup; nothing to select")
    run = runner or _default_runner(repo)

    candidates = loads(ok(run(["bd", "ready", "--json"]), "bd ready"))
    refs, pr_refs = gather_refs(run)
    open_pr_keys = _day_job_open_prs(run, pr_refs)
    # live_keys is LEASE-ONLY (-> result["live_runs"], which the queue-drain uses for
    # the launch-marker registered-check); fleet must NOT leak into it or a still-
    # booting pre-lease run gets evicted from launched_pending a turn early (flow-d4s).
    # The reconciled lease|fleet read is for the in-flight suppression set only.
    live_keys = live_run_keys(repo)  # lease-only -> result["live_runs"]
    fleet_keys = fleet_live_keys(repo)  # lease | fleet (reconciled in-flight authority)
    launched_keys = launch_ledger.live_keys(repo)  # pre-init launch->init window
    inflight_keys = (
        {c["id"] for c in candidates if c.get("id") and is_inflight(c["id"], refs)}
        | fleet_keys
        | launched_keys
    )

    result = partition(
        candidates,
        inflight_keys,
        len(open_pr_keys),
        cap=cap,
        concurrency=concurrency,
        inflight_count=len(fleet_keys | launched_keys),
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
    worker_model = _worker_model(workspace_root)
    model_per_key: dict[str, str] = {}
    for key in result["launch"]:
        labels = labels_by_id.get(key, [])
        if "hot" in labels:
            continue
        if "tier:trivial" in labels or "tier:light" in labels:
            model_per_key[key] = "sonnet"
        elif worker_model:
            model_per_key[key] = worker_model
    result["model_per_key"] = model_per_key
    return result


def _config_defaults(workspace_root: Path) -> tuple[int, int]:
    try:
        config = load_workspace_toml(workspace_root)
    except WorkspaceConfigError:
        return DEFAULT_CAP, DEFAULT_CONCURRENCY
    section = config.get("queue")
    if not isinstance(section, dict):
        return DEFAULT_CAP, DEFAULT_CONCURRENCY
    cap = section.get("cap")
    conc = section.get("concurrency")
    return (
        cap if isinstance(cap, int) and cap > 0 else DEFAULT_CAP,
        conc if isinstance(conc, int) and conc > 0 else DEFAULT_CONCURRENCY,
    )


def _worker_model(workspace_root: Path) -> str | None:
    # worker_model is a shared [evolve] knob; cap/concurrency stay [queue]-scoped (see _config_defaults).
    try:
        config = load_workspace_toml(workspace_root)
    except WorkspaceConfigError:
        return None
    section = config.get("evolve")
    if not isinstance(section, dict):
        return None
    val = section.get("worker_model")
    return val if isinstance(val, str) and val else None


def cli_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Select the next batch of day-job beads to launch."
    )
    parser.add_argument("--workspace-root", required=True)
    parser.add_argument("--cap", type=int, default=None)
    parser.add_argument("--concurrency", type=int, default=None)
    args = parser.parse_args(argv)

    ws = Path(args.workspace_root)
    cfg_cap, cfg_conc = _config_defaults(ws)
    cap = args.cap if args.cap is not None else cfg_cap
    concurrency = args.concurrency if args.concurrency is not None else cfg_conc

    try:
        result = select(ws, cap=cap, concurrency=concurrency)
    except NotMaintainer as exc:
        print(str(exc), file=sys.stderr)
        return 4
    except ToolError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(cli_main(sys.argv[1:]))
