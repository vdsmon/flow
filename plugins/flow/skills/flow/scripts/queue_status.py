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

import evolve_drain
import queue_drain
import queue_select
from _runner import CwdRunner as Runner
from _runner import cwd_default_runner
from maintainer import resolve_maintainer_repo


def status(
    workspace_root: Path,
    *,
    cap: int,
    concurrency: int,
    runner: Runner | None = None,
) -> dict[str, Any]:
    repo = resolve_maintainer_repo(workspace_root)
    if repo is None:
        raise queue_select.NotMaintainer("not a flow maintainer setup; nothing to report")
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
    open_pr_keys = set(sel.get("open_pr_keys") or [])
    live_runs = set(sel.get("live_runs") or []) - evolve_keys
    inflight = sorted(set(sel.get("skipped_in_flight") or []) | open_pr_keys | live_runs)
    live = evolve_drain.liveness_map(repo, inflight)

    # in-memory only: a registered key leaves launched_pending in the report,
    # but its fleet entry stays on disk (read-only invariant)
    pending = set(sel.get("launched_pending") or []) - evolve_keys
    registered = live_runs | open_pr_keys
    sel["launched_pending"] = sorted(pending - registered)

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
    return {
        "action": decision["action"],
        "launch": decision["launch"],
        "parked": decision["parked"],
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
    except queue_select.NotMaintainer as exc:
        print(str(exc), file=sys.stderr)
        return 4
    except queue_select.ToolError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(cli_main(sys.argv[1:]))
