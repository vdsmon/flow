"""Read-only day-job queue status (the `/flow queue` verb's core).

Wraps `queue_select.select()` (the canonical partition) with the full day-job
ready backlog, per-key lease liveness (`evolve_drain.liveness_map`), and the
ADVISORY next action a queue drain would take (`evolve_drain.decide`) — the
`action` field reports what a drain would do next, it is never acted on here.

Read-only by construction: this script touches no file, ever. No launches, no
bd mutations, no launch-ledger marker removal — the launched_pending-minus-
registered set is computed in memory only (evolve_drain's cli_main owns the
physical marker removal).

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

import evolve_drain
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
) -> dict:
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

    inflight = sorted(
        set(sel.get("skipped_in_flight") or [])
        | set(sel.get("open_pr_keys") or [])
        | set(sel.get("live_runs") or [])
    )
    live = evolve_drain.liveness_map(repo, inflight)

    # in-memory only: a registered key leaves launched_pending in the report,
    # but its marker stays on disk (read-only invariant)
    pending = set(sel.get("launched_pending") or [])
    registered = set(sel.get("live_runs") or []) | set(sel.get("open_pr_keys") or [])
    sel["launched_pending"] = sorted(pending - registered)

    decision = evolve_drain.decide(sel, live)
    return {
        "action": decision["action"],
        "launch": decision["launch"],
        "parked": decision["parked"],
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
