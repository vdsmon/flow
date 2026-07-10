"""Decide the next action for the day-job `queue drain` loop (pure core + thin CLI).

Day-job sibling of `evolve_drain.py`. The loop itself (reap merged-and-exited runs, fan out
`claude --bg "/flow <key> --auto"`, Monitor-wait) is prose in `references/verb-queue.md`
(§drain); this module supplies the decision it consumes. The `launch | recover | wait | done`
core and the lease-liveness annotation are `evolve_drain.decide` / `evolve_drain.liveness_map`,
imported (both pure), not duplicated; the selection is `queue_select.select`. New here is
`classify_reap`, the merged-PR reap classification.

STRANDED pre-PR parity: a day-job `/flow <key> --auto` run that dies pre-PR strands its bead
in_progress with no lease and no PR, invisible to every channel (the loop would false-positive
to `done`). `cli_main` detects it with the SAME `evolve_drain.stranded_pre_pr` core, fed a
DAY-JOB-scoped in_progress set (all in_progress beads minus epics minus the `{evolve, proposal,
hot}` labels, the inverse of evolve's per-label union), and threads the keys into `decide` as
`stranded` so it returns `recover` instead of `done`. The recover recipe (reap the dirty
worktree + reopen, bounded by the prose `STRANDED-RECOVERY:` ladder) lives in
`references/verb-queue.md` §Recover.

Unlike the evolve drain this loop NEVER merges PRs: a day-job run's merge stage skips on a
non-evolve bead, so every green PR parks for the maintainer's review. Parked open PRs are this
queue's normal success terminal, not leftovers.

The wait gate is queue-scoped: `live_runs` and `launched_pending` from select are repo-global
(the worktree pool and the fleet ledger are SHARED with the evolve drain), so the
active-evolve key set is subtracted from both before liveness. The day-job loop never blocks
waiting on a live evolve run. Conservative direction preserved: anything not provably evolve's
is waited on.

Reap classification: a merged flow PR whose key still has a registered worktree (run exited,
teardown pending) or sits in this turn's launch batch (merged PR but bead never closed) is a
reap candidate; `bead_active` says whether the loop must still `bd close` it. Any launch key in
the reap set is dropped from `launch`. A merged-but-unclosed bead diverts to the close path,
never relaunches.

Exit codes: 0 ok; 2 = a `bd`/`git`/`gh` call failed; 4 = not a maintainer setup.
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path
from typing import Any

from _evolve_common import (
    ACTIVE_STATUSES,
    WORKTREE_BASES,
    WORKTREE_PREFIXES,
    NotMaintainer,
    ToolError,
    bead_status,
    key_from_ref,
    loads,
    merged_flow_prs,
    ok,
    reconcile_launched_pending,
)
from _evolve_common import active_evolve_keys as _active_evolve_keys
from _runner import CwdRunner as Runner
from _runner import cwd_default_runner as _default_runner
from evolve_drain import decide, liveness_map, stranded_pre_pr
from maintainer import resolve_maintainer_repo
from queue_select import _EXCLUDED_LABELS, _config_defaults, select

_ACTIVE = frozenset(ACTIVE_STATUSES.split(","))


def classify_reap(
    merged_prs: list[Any],
    candidate_keys: set[str],
    bead_status: dict[str, str | None],
    worktree_keys: set[str] | frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """Pure: the merged flow PRs whose runs the loop must reap.

    merged_prs: parsed `gh pr list --state merged` items (number, headRefName).
    candidate_keys: keys with a registered worktree UNION this turn's launch keys. Anything else
    merged within the window is long-since torn down.
    bead_status: key -> bd status for the candidates (a `deferred`/`closed` bead reads
    bead_active=False; deferred stays the human's triage call).
    One entry per key (first PR in list order wins).
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for pr in merged_prs:
        if not isinstance(pr, dict):
            continue
        branch = str(pr.get("headRefName") or "")
        key = key_from_ref(branch)
        if key is None or key not in candidate_keys or key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "key": key,
                "branch": branch,
                "pr": pr.get("number"),
                "bead_active": (bead_status.get(key) or "") in _ACTIVE,
                "has_worktree": key in worktree_keys,
            }
        )
    return out


def _worktree_keys(repo: Path) -> set[str]:
    """Every key with a registered worktree run dir, live or not (reap candidates).

    Same pool layout `_evolve_common.run_dir_for` documents; unlike `live_run_keys` this keeps
    expired/exited runs. They are exactly the teardown targets.
    """
    return {
        Path(p).name
        for base in WORKTREE_BASES
        for prefix in WORKTREE_PREFIXES
        for p in glob.glob(str(repo / base / f"{prefix}*" / ".flow" / "runs" / "*"))
    }


def _inprogress_dayjob_keys(runner: Runner) -> set[str]:
    """Keys of IN_PROGRESS day-job beads (the inverse of the evolve scope).

    Day-job = all in_progress beads minus epics minus the `{evolve, proposal, hot}` labels, the same
    filter `queue_select._day_job` applies to `bd ready`, re-run over `--status in_progress`
    (stranded beads have left `bd ready`).
    Unscoped on purpose (NO `-l`): the day-job queue is everything NOT evolve's, so it cannot be
    expressed as a label union. `--limit 0` because bd list defaults to 50 and would silently
    truncate.
    """
    raw = ok(
        runner(["bd", "list", "--status", "in_progress", "--json", "--limit", "0"]),
        "bd list",
    )
    out: set[str] = set()
    for b in loads(raw):
        if not isinstance(b, dict) or not b.get("id"):
            continue
        if b.get("issue_type") == "epic":
            continue
        if _EXCLUDED_LABELS & set(b.get("labels") or []):
            continue
        out.add(str(b["id"]))
    return out


def cli_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Decide the day-job queue drain's next action.")
    parser.add_argument("--workspace-root", required=True)
    parser.add_argument("--cap", type=int, default=None)
    parser.add_argument("--concurrency", type=int, default=None)
    args = parser.parse_args(argv)

    ws = Path(args.workspace_root)
    repo = resolve_maintainer_repo(ws)
    if repo is None:
        print("not a flow maintainer setup; drain is dormant", file=sys.stderr)
        return 4

    cfg_cap, cfg_conc = _config_defaults(ws)
    cap = args.cap if args.cap is not None else cfg_cap
    concurrency = args.concurrency if args.concurrency is not None else cfg_conc

    try:
        sel = select(ws, cap=cap, concurrency=concurrency)
        run = _default_runner(repo)
        # live_runs and launched_pending are repo-global; subtract the active
        # evolve set so this loop never waits on (or unmarks) an evolve run.
        evolve_keys = _active_evolve_keys(run)
        open_pr_keys, _live_runs, inflight = reconcile_launched_pending(
            sel, exclude_keys=evolve_keys
        )
        live = liveness_map(repo, inflight)

        merged = merged_flow_prs(run)
        wt_keys = _worktree_keys(repo)
        launch_keys = set(sel.get("launch") or [])
        candidates = wt_keys | launch_keys
        merged_keys = {
            k
            for p in merged
            if isinstance(p, dict) and (k := key_from_ref(str(p.get("headRefName") or "")))
        }
        statuses = {k: bead_status(run, k) for k in sorted(merged_keys & candidates)}
        reap = classify_reap(merged, candidates, statuses, worktree_keys=wt_keys)
        # a launch key with a merged PR diverts to the close path, never relaunches
        reap_keys = {entry["key"] for entry in reap}
        if reap_keys & launch_keys:
            sel["launch"] = [k for k in sel.get("launch") or [] if k not in reap_keys]
        # STRANDED pre-PR detection (parity with evolve_drain): a day-job run that
        # died before opening a PR strands its bead in_progress, invisible to every
        # other channel. Gate done-termination on it + emit a recover list. Runs
        # AFTER the launched_pending reconciliation (it consumes the final set).
        stranded = stranded_pre_pr(
            repo,
            run,
            launched_pending=set(sel["launched_pending"]),
            open_pr_keys=open_pr_keys,
            in_progress_keys=_inprogress_dayjob_keys(run),
        )
    except NotMaintainer as exc:
        print(str(exc), file=sys.stderr)
        return 4
    except ToolError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    result = decide(sel, live, stranded=[e["key"] for e in stranded])
    result["stranded_pre_pr"] = stranded
    result["reap"] = reap
    result["liveness"] = live
    result["select"] = sel
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(cli_main(sys.argv[1:]))
