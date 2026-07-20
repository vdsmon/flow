"""Decide the next action for the `evolve drain` loop (pure core + thin CLI).

The drain loop reaps finished orphans, then asks this module: given the current `evolve_select`
result plus the liveness of every in-flight run, should the driver RECOVER stranded work, WAIT for
a live run to settle, request ATTENDED PLANNING for fresh candidates, or report DONE? The facade
consumer is the
`evolve-drain` command in `FLOW maintain evolution drain` (`references/command-maintain.md`
§drain): the driver loop (reap, native collaboration, driver wait) is prose there;
the CLI here is its pure evolution-scoped decision, plus `decide()`/`liveness_map()`/
`stranded_pre_pr()` as a shared library `queue_drain.py`/`queue_status.py` reuse for the day-job
(non-evolve) backlog with their own scoped in-flight set.

The in-flight set is derived from the actual OPEN evolve PRs (plus any ready bead that is
in-flight), NOT from `evolve_select`'s `skipped_in_flight` alone: a run that occupies the open-PR
cap may have left `bd ready` (its bead is claimed), so `skipped_in_flight` can be empty even while
runs are in flight. Relying on it would make the loop quit the moment backpressure hits. Liveness
over the open PRs is the authoritative picture.

Termination: `action == "done"` iff the candidate list is empty, `launched_pending` is empty, and
no in-flight run is blocking or stranded. A candidate requests attended planning; it does not
authorize a worker launch.
A run is blocking when its lease reads "live" (still working) OR "corrupt" (run.lock unparseable,
ownership cannot be confirmed). The third blocking reason is a non-empty `launched_pending`: a run
fanned out on a prior turn that has not yet registered a branch/lease/PR is still in the launch→init
blind window (its run dir reads "absent", which would otherwise be non-blocking), so it blocks
termination until it registers (cli_main drops it from launched_pending then) or its fleet entry
ages past `STALE_AFTER_S`. Corrupt is treated live-equivalent because this decision gates a
self-merge: an in-flight run we cannot confirm dead must never let the loop drain to done. A
withheld hot bead (the in-run reviewer raised `held_guard`) leaves a ready PR + a branch but its
session has ended, so its lease is non-blocking (expired/absent): it never reads as "wait," so the
loop cannot spin on it. It terminates and reports it `parked` for the human. A still-running run
reads "live" → the loop waits → it self-merges → the next turn's reap clears the cap /
`hot_inflight` → the next candidate batch is reported for planning.
A corrupt lease blocks until a human runs `recover takeover`.

A fourth termination guard is the STRANDED gate: an already-approved unattended run that died
before its PR (crash/zombie/OOM during delivery) strands its bead in_progress with a dirty orphan
worktree
but no lease and no PR, so every other channel reads it as gone and the loop would false-positive to
"done". cli_main detects it (an in_progress evolve-scoped bead whose lease is non-live, that is not
in `launched_pending`, and has NO PR open or merged) and feeds the key list to decide() as
`stranded`; a non-empty `stranded` returns action "recover" (never "done"), and the loop reaps the
dirty worktree + reopens the bead so the next turn can return it for attended planning. See
references/command-maintain.md §drain (the recover branch).

Exit codes: 0 ok; 2 = a `bd`/`git`/`gh` call failed; 4 = not a maintainer setup.
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import lease
from _evolve_common import BRANCH_PREFIX as _BRANCH_PREFIX
from _evolve_common import WORKTREE_BASES as _WORKTREE_BASES
from _evolve_common import WORKTREE_PREFIXES as _WORKTREE_PREFIXES
from _evolve_common import NotMaintainer, ToolError, bead_labels
from _evolve_common import key_from_ref as _key_from_ref
from _evolve_common import loads as _loads
from _evolve_common import merged_flow_prs as _merged_flow_prs
from _evolve_common import ok as _ok
from _evolve_common import reconcile_launched_pending as _reconcile_launched_pending
from _evolve_common import run_dir_for as _run_dir_for
from _runner import CwdRunner as Runner
from _runner import cwd_default_runner as _default_runner
from _timeutil import utcnow_iso
from evolve_select import _config_defaults, select
from maintainer import resolve_maintainer_repo


def decide(
    select_result: dict[str, Any],
    liveness: dict[str, str],
    stranded: Sequence[str] = (),
) -> dict[str, Any]:
    """Pure: map a select result + in-flight liveness to the loop's next action.

    stranded non-empty          -> recover (a pre-PR dead run left its bead
                                   IN_PROGRESS + a dirty orphan worktree, invisible
                                   to every other channel; the loop reaps the
                                   worktree + reopens the bead so the next turn can
                                   offer it for attended planning). A non-empty `stranded` MUST
                                   never let the loop read `done`. That was the
                                   false-positive termination this gate closes.
    a blocking run              -> wait (live OR corrupt: corrupt cannot be
                                   confirmed dead, so it blocks a self-merge).
    launched_pending non-empty  -> wait (a launched-but-pre-lease run
                                   is still in the launch->init window; it blocks
                                   until it registers or its fleet entry ages out).
    candidates non-empty        -> plan_required (fresh work needs driver planning,
                                   independent assessment, and human approval).
    none of the above           -> done (drained, or only parked-for-human remains).

    `liveness` is the complete in-flight picture (open PRs + in-flight ready beads);
    `launched_pending` (from the select result) is the still-pre-lease launched keys;
    `stranded` is the pre-PR-dead in_progress keys the CLI detected (empty for the
    day-job `queue_drain` caller, which passes only the first two positionally);
    `parked` is the keys whose run is not live and not still bootstrapping (what the
    loop hands the human: withheld hot PRs, non-green drafts, orphaned branches).

    `launch` is retained only as an internal selector field. Every public decision
    returns `launch=[]`; `plan_required` carries candidates only for that action.
    Precedence is recover > wait > plan_required > done so fresh candidates cannot
    hide existing work that needs recovery or observation.
    """
    candidates = list(select_result.get("launch") or [])
    stranded_keys = sorted(set(stranded))
    launched_pending = set(select_result.get("launched_pending") or [])
    blocking = sorted(k for k, state in liveness.items() if state in ("live", "corrupt"))
    parked = sorted(
        k
        for k, state in liveness.items()
        if state not in ("live", "corrupt") and k not in launched_pending and k not in stranded_keys
    )
    if stranded_keys:
        return {
            "action": "recover",
            "launch": [],
            "plan_required": [],
            "stranded": stranded_keys,
            "parked": parked,
        }
    if blocking or launched_pending:
        return {"action": "wait", "launch": [], "plan_required": [], "parked": parked}
    if candidates:
        return {
            "action": "plan_required",
            "launch": [],
            "plan_required": candidates,
            "parked": parked,
        }
    return {"action": "done", "launch": [], "plan_required": [], "parked": parked}


def liveness_map(repo: Path, keys: list[str]) -> dict[str, str]:
    """For each in-flight key, the lease state of its run ("live" = still working)."""
    now = utcnow_iso()
    current_boot = lease.boot_id()
    host = lease.hostname()
    out: dict[str, str] = {}
    for key in keys:
        run_dir = _run_dir_for(repo, key)
        out[key] = (
            "absent"
            if run_dir is None
            else str(
                lease.classify(run_dir, now, current_boot=current_boot, hostname=host).get("state")
            )
        )
    return out


def _merged_pr_keys(runner: Runner) -> set[str]:
    """Flow keys with a MERGED PR.

    An in_progress bead with a merged PR is a different inconsistency (close, not
    renewed delivery), so it is excluded from the stranded set.
    """
    return {
        k
        for p in _merged_flow_prs(runner)
        if isinstance(p, dict) and (k := _key_from_ref(str(p.get("headRefName") or "")))
    }


def _inprogress_evolve_keys(runner: Runner, *, include_proposals: bool) -> set[str]:
    """Keys of IN_PROGRESS evolve beads (scoped to the evolve label set).

    Scoped, not a bare `bd list --status in_progress`, so the evolve drain never
    reaps a day-job run's worktree in the shared pool. `--limit 0` because bd list
    defaults to 50 and would silently truncate.
    """
    keys: set[str] = set()
    for label in bead_labels(include_proposals):
        raw = _ok(
            runner(
                ["bd", "list", "-l", label, "--status", "in_progress", "--json", "--limit", "0"]
            ),
            "bd list",
        )
        keys |= {str(b["id"]) for b in _loads(raw) if isinstance(b, dict) and b.get("id")}
    return keys


def _worktree_for(repo: Path, key: str) -> str | None:
    """Worktree dir `<base>/feat-<key>-*` for `key`, if present (both pool
    bases, legacy `feature-` prefix too)."""
    for b in _WORKTREE_BASES:
        for p in _WORKTREE_PREFIXES:
            for wt in sorted(glob.glob(str(repo / b / f"{p}{key}*"))):
                if (Path(wt) / ".flow" / "runs" / key).exists():
                    return wt
    return None


def stranded_pre_pr(
    repo: Path,
    runner: Runner,
    *,
    launched_pending: set[str],
    open_pr_keys: set[str],
    include_proposals: bool = False,
    in_progress_keys: set[str] | None = None,
) -> list[dict[str, Any]]:
    """In_progress beads whose run died PRE-PR, invisible to every channel.

    STRANDED iff ALL hold: the bead is in_progress, its lease is non-live (not
    `live`/`corrupt`), it is NOT in the post-reconciliation `launched_pending`
    (still-booting guard + TTL debounce), and it has NO PR in any state (neither an
    open PR nor a merged PR). `branch` is best-effort; the prose reaps by `--ticket`.

    The in_progress source is injectable: `in_progress_keys=None` (the evolve
    default) computes the evolve-label-scoped set via `_inprogress_evolve_keys`,
    while `queue_drain` injects its own day-job-scoped set (the inverse filter).
    Everything downstream (the merged/open-PR/launched_pending exclusions and the
    lease-liveness probe) is scope-agnostic.
    """
    in_progress = (
        _inprogress_evolve_keys(runner, include_proposals=include_proposals)
        if in_progress_keys is None
        else set(in_progress_keys)
    )
    if not in_progress:
        return []
    merged = _merged_pr_keys(runner)
    out: list[dict[str, Any]] = []
    for key in sorted(in_progress):
        if key in launched_pending or key in open_pr_keys or key in merged:
            continue
        if liveness_map(repo, [key]).get(key) in ("live", "corrupt"):
            continue
        out.append(
            {"key": key, "branch": f"{_BRANCH_PREFIX}{key}", "worktree": _worktree_for(repo, key)}
        )
    return out


def cli_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Decide the evolve drain loop's next action.")
    parser.add_argument("--workspace-root", required=True)
    parser.add_argument("--cap", type=int, default=None)
    parser.add_argument("--concurrency", type=int, default=None)
    parser.add_argument(
        "--include-proposals",
        action="store_true",
        help="also include `proposal` (judgment) beads as attended planning candidates",
    )
    args = parser.parse_args(argv)

    ws = Path(args.workspace_root)
    repo = resolve_maintainer_repo(ws)
    if repo is None:
        print("not a flow maintainer setup; drain is dormant", file=sys.stderr)
        return 4

    cfg_cap, cfg_conc = _config_defaults(ws)
    cap = args.cap if args.cap is not None else cfg_cap
    concurrency = args.concurrency if args.concurrency is not None else cfg_conc

    if args.include_proposals:
        print(
            "NOTICE: --include-proposals includes judgment beads as planning candidates",
            file=sys.stderr,
        )

    try:
        sel = select(ws, cap=cap, concurrency=concurrency, include_proposals=args.include_proposals)
        open_pr_keys, _live_runs, inflight = _reconcile_launched_pending(sel)
        live = liveness_map(repo, inflight)
        stranded = stranded_pre_pr(
            repo,
            _default_runner(repo),
            launched_pending=set(sel["launched_pending"]),
            open_pr_keys=open_pr_keys,
            include_proposals=args.include_proposals,
        )
    except NotMaintainer as exc:
        print(str(exc), file=sys.stderr)
        return 4
    except ToolError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    result = decide(sel, live, stranded=[e["key"] for e in stranded])
    result["stranded_pre_pr"] = stranded
    result["liveness"] = live
    result["select"] = sel
    result["include_proposals"] = args.include_proposals
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(cli_main(sys.argv[1:]))
