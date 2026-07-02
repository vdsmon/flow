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
(the worktree pool and the launch ledger are SHARED with the evolve drain), so the
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

import launch_ledger
from _evolve_common import (
    ACTIVE_STATUSES,
    WORKTREE_PREFIXES,
    NotMaintainer,
    ToolError,
    key_from_ref,
    loads,
    ok,
)
from _runner import CwdRunner as Runner
from _runner import cwd_default_runner as _default_runner
from evolve_drain import decide, liveness_map, stranded_pre_pr
from maintainer import resolve_maintainer_repo
from queue_select import _EXCLUDED_LABELS, _config_defaults, select

_ACTIVE = frozenset(ACTIVE_STATUSES.split(","))


def classify_reap(
    merged_prs: list,
    candidate_keys: set[str],
    bead_status: dict[str, str | None],
    worktree_keys: set[str] | frozenset[str] = frozenset(),
) -> list[dict]:
    """Pure: the merged flow PRs whose runs the loop must reap.

    merged_prs: parsed `gh pr list --state merged` items (number, headRefName).
    candidate_keys: keys with a registered worktree UNION this turn's launch keys. Anything else
    merged within the window is long-since torn down.
    bead_status: key -> bd status for the candidates (a `deferred`/`closed` bead reads
    bead_active=False; deferred stays the human's triage call).
    One entry per key (first PR in list order wins).
    """
    out: list[dict] = []
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
    base = repo / ".flow" / "worktrees"
    return {
        Path(p).name
        for prefix in WORKTREE_PREFIXES
        for p in glob.glob(str(base / f"{prefix}*" / ".flow" / "runs" / "*"))
    }


def _active_evolve_keys(runner: Runner) -> set[str]:
    """Keys of the active evolve beads (the evolve drain's queue, not ours)."""
    raw = ok(
        runner(["bd", "list", "-l", "evolve", "--status", ACTIVE_STATUSES, "--json"]),
        "bd list",
    )
    return {str(b["id"]) for b in loads(raw) if isinstance(b, dict) and b.get("id")}


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


def _bead_status(runner: Runner, key: str) -> str | None:
    """`bd show <key> --json` status; bd show sees closed beads, bd list hides them."""
    raw = ok(runner(["bd", "show", key, "--json"]), f"bd show {key}")
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return None
    if isinstance(data, list):
        data = data[0] if data else {}
    status = data.get("status") if isinstance(data, dict) else None
    return str(status) if status else None


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
        open_pr_keys = set(sel.get("open_pr_keys") or [])
        live_runs = set(sel.get("live_runs") or []) - evolve_keys
        inflight = sorted(set(sel.get("skipped_in_flight") or []) | open_pr_keys | live_runs)
        live = liveness_map(repo, inflight)
        # a launched key that has registered (live lease OR open PR) leaves the blind
        # window; physically drop its marker so it stays out of launched_pending past
        # any later merge/teardown. NOT skipped_in_flight: select folds launched_pending
        # into it, which would falsely mark an unregistered key registered.
        pending = set(sel.get("launched_pending") or []) - evolve_keys
        registered = live_runs | open_pr_keys
        for key in sorted(pending & registered):
            launch_ledger.remove(repo, key)
        sel["launched_pending"] = sorted(pending - registered)

        merged = loads(
            ok(
                run(
                    [
                        "gh",
                        "pr",
                        "list",
                        "--state",
                        "merged",
                        "--json",
                        "number,headRefName",
                        "--limit",
                        "200",
                    ]
                ),
                "gh pr list",
            )
        )
        wt_keys = _worktree_keys(repo)
        launch_keys = set(sel.get("launch") or [])
        candidates = wt_keys | launch_keys
        merged_keys = {
            k
            for p in merged
            if isinstance(p, dict) and (k := key_from_ref(str(p.get("headRefName") or "")))
        }
        bead_status = {k: _bead_status(run, k) for k in sorted(merged_keys & candidates)}
        reap = classify_reap(merged, candidates, bead_status, worktree_keys=wt_keys)
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
