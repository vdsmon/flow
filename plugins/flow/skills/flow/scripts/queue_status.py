"""Read-only day-job queue status (the `/flow queue` verb's core).

Wraps `queue_select.select()` (the canonical partition) with the full day-job
ready backlog, per-key lease liveness (`evolve_drain.liveness_map`), and the
ADVISORY next action a queue drain would take (`evolve_drain.decide`): the
`action` field reports what a drain would do next, it is never acted on here.
The advisory mirrors `queue_drain.cli_main`'s scoping: the active-evolve set is
subtracted from `live_runs`/`launched_pending` (this loop never waits on a live
evolve run) and STRANDED pre-PR day-job beads feed `decide` so it reads
`recover`, not a false `done`. One known approximation remains: the advisory
`launch` list is not reap-filtered, so a merged-PR key the real drain diverts
to the close path can still appear in it (the reap classification needs the
merged-PR + per-key `bd show` gather this status verb skips).

Also carries the parked-PR review enrichment (epic flow-kx17.5): each parked
key's open PR is probed for unresolved NATIVE Major+ review threads (a genuine
new human CHANGES_REQUESTED -> `/flow revise <pr#>`), surfaced as `reviews`.
Best-effort: no `[forge]` block, no parked keys, or any per-key forge error ->
`reviews: []`, never a failure. The `[revise] plain_comment_severity` floor is
deliberately NOT applied here (a leftover unresolved bot minor must never
produce a false human-review flag; the floor is a revise-time knob).

Read-only by construction: this script touches no file, ever. No launches, no
bd mutations, no fleet-ledger writes; the launched_pending-minus-registered set
is computed in memory only (the underlying fleet entry is left untouched -- it
ages out on its own staleness clock, `fleet.STALE_AFTER_S`).

CLI:
  queue_status.py --workspace-root <dir> [--cap N] [--concurrency N]

Exit codes:
  0 = ok (prints the status JSON)
  2 = tool error (bd/git/gh failed; stderr propagated)
  4 = not a maintainer setup (dormant; nothing to report)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import _evolve_common
import evolve_drain
import forge
import queue_drain
import queue_select
from _runner import CwdRunner as Runner
from _runner import cwd_default_runner
from maintainer import resolve_maintainer_repo

# minor/nit are intentionally excluded: a leftover unresolved bot minor must never
# produce a false human-review flag (the plain-comment floor is a revise-time knob).
_MAJOR_PLUS = {"major", "critical"}


def flag_parked_reviews(keys: list[str], pr_refs: list[str], adapter: Any) -> list[dict[str, Any]]:
    """For each parked key with a matching open-PR ref, count unresolved Major+ threads.

    Joins each parked key to its EXACT slugged head ref via `key_from_ref` (a
    reconstructed bare `feat/<key>` would not match the real `feat/<key>-<slug>`
    branch and silently flag nothing). Returns a result dict only for keys whose
    `unresolved_major > 0`. Best-effort: any per-key adapter error (a `ForgeError`
    incl. `NotSupported`, or an unexpected payload shape) is swallowed, that key
    is not flagged, the others still process.
    """
    ref_by_key: dict[str, str] = {}
    for ref in pr_refs:
        key = _evolve_common.key_from_ref(ref)
        if key and key not in ref_by_key:
            ref_by_key[key] = ref

    results: list[dict[str, Any]] = []
    for key in keys:
        ref = ref_by_key.get(key)
        if ref is None:
            continue
        try:
            pr = adapter.detect_pr(ref)
            if pr is None:
                continue
            pr_id = pr.get("id")
            if not pr_id:
                continue
            threads = adapter.review_threads(pr_id)
        except Exception:
            # not just ForgeError: an unexpected payload shape (KeyError/TypeError)
            # or a raw parse error surfacing from an adapter must also skip the
            # key, or the best-effort contract breaks
            continue

        flagged = [t for t in threads if t.get("severity") in _MAJOR_PLUS and not t.get("resolved")]
        if not flagged:
            continue
        results.append(
            {
                "key": key,
                "pr_id": pr_id,
                "pr_url": pr.get("url"),
                "unresolved_major": len(flagged),
                "threads": [
                    {"id": t.get("id"), "severity": t.get("severity"), "title": t.get("title")}
                    for t in flagged
                ],
            }
        )
    return results


def _parked_reviews(
    workspace_root: Path,
    keys: list[str],
    pr_refs: list[str],
    forge_factory: Any = None,
) -> list[dict[str, Any]]:
    """flag_parked_reviews behind the workspace forge config; [] on any miss."""
    if not keys:
        return []
    try:
        config = forge.read_forge_config(workspace_root)
    except forge.ForgeConfigError:
        config = None
    if config is None:
        return []
    factory = forge_factory or forge.make_forge
    try:
        adapter = factory(config)
    except Exception:
        return []
    return flag_parked_reviews(keys, pr_refs, adapter)


def status(
    workspace_root: Path,
    *,
    cap: int,
    concurrency: int,
    runner: Runner | None = None,
    forge_factory: Any = None,
) -> dict[str, Any]:
    repo = resolve_maintainer_repo(workspace_root)
    if repo is None:
        raise _evolve_common.NotMaintainer("not a flow maintainer setup; nothing to report")
    run = runner or cwd_default_runner(repo)

    sel = queue_select.select(workspace_root, cap=cap, concurrency=concurrency, runner=run)

    # select() hides the budget-overflow tail; a status verb's headline is the
    # total backlog depth, so re-read the full day-job candidate list
    candidates = queue_select.loads(queue_select.ok(run(["bd", "ready", "--json"]), "bd ready"))
    ready = [
        {
            "id": c["id"],
            "priority": c.get("priority"),
            "labels": c.get("labels") or [],
            "title": c.get("title"),
        }
        for c in sorted(
            queue_select._day_job(candidates),
            key=lambda c: (c.get("priority", 99), str(c.get("id"))),
        )
    ]

    # queue-scope the advisory exactly as queue_drain.cli_main does: live_runs and
    # launched_pending are repo-global (pool + ledger shared with the evolve
    # drain), so the advisory must not report `wait` on a live evolve run the
    # real drain ignores
    evolve_keys = queue_drain._active_evolve_keys(run)
    # in-memory only: a registered key leaves launched_pending in the report,
    # but its fleet entry stays on disk (read-only invariant)
    open_pr_keys, _live_runs, inflight = _evolve_common.reconcile_launched_pending(
        sel, exclude_keys=evolve_keys
    )
    live = evolve_drain.liveness_map(repo, inflight)

    # read-only stranded detection (same core the drain runs): without it the
    # advisory would report `done` where the drain reports `recover`
    stranded = evolve_drain.stranded_pre_pr(
        repo,
        run,
        launched_pending=set(sel["launched_pending"]),
        open_pr_keys=open_pr_keys,
        in_progress_keys=queue_drain._inprogress_dayjob_keys(run),
    )

    decision = evolve_drain.decide(sel, live, stranded=[e["key"] for e in stranded])

    # parked-PR review enrichment: probe only when something is parked (skips
    # the extra gh/git round-trip on the common empty case)
    reviews: list[dict[str, Any]] = []
    if decision["parked"]:
        _, pr_refs = _evolve_common.gather_refs(run)
        reviews = _parked_reviews(
            workspace_root, decision["parked"], sorted(pr_refs), forge_factory
        )

    return {
        "action": decision["action"],
        "launch": decision["launch"],
        "parked": decision["parked"],
        "reviews": reviews,
        "stranded_pre_pr": stranded,
        "liveness": live,
        "ready": ready,
        "select": sel,
    }


def cli_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Read-only day-job queue status.")
    parser.add_argument("--workspace-root", required=True)
    parser.add_argument("--cap", type=int, default=None)
    parser.add_argument("--concurrency", type=int, default=None)
    args = parser.parse_args(argv)

    ws = Path(args.workspace_root)
    cfg_cap, cfg_conc = queue_select._config_defaults(ws)
    cap = args.cap if args.cap is not None else cfg_cap
    concurrency = args.concurrency if args.concurrency is not None else cfg_conc

    try:
        result = status(ws, cap=cap, concurrency=concurrency)
    except _evolve_common.NotMaintainer as exc:
        print(str(exc), file=sys.stderr)
        return 4
    except _evolve_common.ToolError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(cli_main(sys.argv[1:]))
