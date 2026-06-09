"""Decide the next action for the `evolve drain` loop (pure core + thin CLI).

The drain loop reaps finished orphans, then asks this module: given the current
`evolve_select` result plus the liveness of every in-flight run, should the loop
LAUNCH the next batch, WAIT for a live run to settle, or is it DONE (nothing
startable)? The loop itself — reap, fan out `claude --bg`, Monitor-wait — is prose
in `references/verb-evolve.md` (§drain); this is the pure decision it consumes.

The in-flight set is derived from the actual OPEN evolve PRs (plus any ready bead
that is in-flight), NOT from `evolve_select`'s `skipped_in_flight` alone: a run
that occupies the open-PR cap may have left `bd ready` (its bead is claimed), so
`skipped_in_flight` can be empty even while runs are in flight — relying on it
would make the loop quit the moment backpressure hits. Liveness over the open PRs
is the authoritative picture.

Termination: `action == "done"` iff `launch` is empty AND `launched_pending` is
empty AND no in-flight run is BLOCKING. A run is blocking when its lease reads
"live" (still working) OR "corrupt" (run.lock unparseable, ownership cannot be
confirmed). The third blocking reason is a non-empty `launched_pending`: a run
fanned out on a prior turn that has not yet registered a branch/lease/PR is still
in the launch→init blind window (its run dir reads "absent", which would
otherwise be non-blocking), so it blocks termination until it registers (cli_main
removes its marker then) or its marker TTL-expires. Corrupt is treated
live-equivalent because this decision gates a self-merge: an in-flight run we
cannot confirm dead must never let the loop drain to done. A withheld hot bead
(the in-run reviewer raised `held_guard`) leaves a ready PR + a branch but its
session has ended, so its lease is non-blocking (expired/absent): it never reads
as "wait," so the loop cannot spin on it — it terminates and reports it `parked`
for the human. A still-running run reads "live" → the loop waits → it self-merges
→ the next turn's reap clears the cap / `hot_inflight` → the next batch launches.
A corrupt lease blocks until a human runs `recover takeover`.

Exit codes: 0 ok; 2 = a `bd`/`git`/`gh` call failed; 4 = not a maintainer setup.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import launch_ledger
import lease
from _evolve_common import run_dir_for as _run_dir_for
from _timeutil import utcnow_iso
from evolve_select import (
    NotMaintainer,
    ToolError,
    _config_defaults,
    select,
)
from maintainer import resolve_maintainer_repo


def decide(select_result: dict, liveness: dict[str, str]) -> dict:
    """Pure: map a select result + in-flight liveness to the loop's next action.

    launch non-empty            -> launch that batch.
    launch empty, a blocking run -> wait (live OR corrupt: corrupt cannot be
                                   confirmed dead, so it blocks a self-merge).
    launch empty, launched_pending non-empty -> wait (a launched-but-pre-lease run
                                   is still in the launch->init window; it blocks
                                   until it registers or its marker TTL-expires).
    launch empty, none blocking -> done (drained, or only parked-for-human remains).

    `liveness` is the complete in-flight picture (open PRs + in-flight ready beads);
    `launched_pending` (from the select result) is the still-pre-lease launched keys;
    `parked` is the keys whose run is not live and not still bootstrapping — what the
    loop hands the human (withheld hot PRs, non-green drafts, orphaned branches).
    """
    launch = list(select_result.get("launch") or [])
    if launch:
        return {"action": "launch", "launch": launch, "parked": []}
    launched_pending = set(select_result.get("launched_pending") or [])
    blocking = sorted(k for k, state in liveness.items() if state in ("live", "corrupt"))
    parked = sorted(
        k
        for k, state in liveness.items()
        if state not in ("live", "corrupt") and k not in launched_pending
    )
    if blocking or launched_pending:
        return {"action": "wait", "launch": [], "parked": parked}
    return {"action": "done", "launch": [], "parked": parked}


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


def cli_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Decide the evolve drain loop's next action.")
    parser.add_argument("--workspace-root", required=True)
    parser.add_argument("--cap", type=int, default=None)
    parser.add_argument("--concurrency", type=int, default=None)
    parser.add_argument(
        "--include-proposals",
        action="store_true",
        help="DANGEROUS: also auto-launch `proposal` (judgment) beads, bypassing the "
        "human spec-plan accept gate. Default off; evolve/audit work only.",
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
            "WARNING: --include-proposals auto-launches judgment `proposal` beads "
            "without the human spec-plan accept gate.",
            file=sys.stderr,
        )

    try:
        sel = select(ws, cap=cap, concurrency=concurrency, include_proposals=args.include_proposals)
        open_pr_keys = set(sel.get("open_pr_keys") or [])
        live_runs = set(sel.get("live_runs") or [])
        inflight = sorted(set(sel.get("skipped_in_flight") or []) | open_pr_keys | live_runs)
        live = liveness_map(repo, inflight)
        # a launched key that has registered (live lease OR open PR) leaves the blind
        # window; physically drop its marker so it stays out of launched_pending past
        # any later merge/teardown. NOT skipped_in_flight: select folds launched_pending
        # into it, which would falsely mark an unregistered key registered.
        pending = set(sel.get("launched_pending") or [])
        registered = live_runs | open_pr_keys
        for key in sorted(pending & registered):
            launch_ledger.remove(repo, key)
        sel["launched_pending"] = sorted(pending - registered)
    except NotMaintainer as exc:
        print(str(exc), file=sys.stderr)
        return 4
    except ToolError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    result = decide(sel, live)
    result["liveness"] = live
    result["select"] = sel
    result["include_proposals"] = args.include_proposals
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(cli_main(sys.argv[1:]))
