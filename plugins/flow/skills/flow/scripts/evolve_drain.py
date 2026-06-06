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

Termination: `action == "done"` iff `launch` is empty AND no in-flight run is
live. A withheld hot bead (the in-run reviewer raised `held_guard`) leaves a ready
PR + a branch but its session has ended, so its lease is non-live: it never reads
as "wait," so the loop cannot spin on it — it terminates and reports it `parked`
for the human. A still-running run reads "live" → the loop waits → it self-merges →
the next turn's reap clears the cap / `hot_inflight` → the next batch launches.

Exit codes: 0 ok; 2 = a `bd`/`git`/`gh` call failed; 4 = not a maintainer setup.
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path

import lease
from evolve_select import (
    NotMaintainer,
    ToolError,
    _config_defaults,
    _default_runner,
    _key_from_ref,
    _loads,
    _ok,
    select,
)
from maintainer import resolve_maintainer_repo


def decide(select_result: dict, liveness: dict[str, str]) -> dict:
    """Pure: map a select result + in-flight liveness to the loop's next action.

    launch non-empty            -> launch that batch.
    launch empty, a live run    -> wait (it will free serialization / the PR cap).
    launch empty, none live     -> done (drained, or only parked-for-human remains).

    `liveness` is the complete in-flight picture (open PRs + in-flight ready beads);
    `parked` is the keys whose run is not live — what the loop hands the human
    (withheld hot PRs, non-green drafts, orphaned branches).
    """
    launch = list(select_result.get("launch") or [])
    if launch:
        return {"action": "launch", "launch": launch, "parked": []}
    live = sorted(k for k, state in liveness.items() if state == "live")
    parked = sorted(k for k, state in liveness.items() if state != "live")
    if live:
        return {"action": "wait", "launch": [], "parked": parked}
    return {"action": "done", "launch": [], "parked": parked}


def _open_pr_keys(repo: Path) -> list[str]:
    """The evolve bead keys behind currently-open PRs (the cap-occupying runs)."""
    run = _default_runner(repo)
    raw = _ok(
        run(["gh", "pr", "list", "--state", "open", "--json", "headRefName", "--limit", "200"]),
        "gh pr list",
    )
    keys: set[str] = set()
    for pr in _loads(raw):
        if isinstance(pr, dict) and pr.get("headRefName"):
            key = _key_from_ref(str(pr["headRefName"]))
            if key:
                keys.add(key)
    return sorted(keys)


def _run_dir_for(repo: Path, key: str) -> Path | None:
    """The in-flight run's ticket dir, under the sibling worktree for `key`.

    Worktrees live at `<repo>.worktrees/feature-<key>-<slug>/` (see
    flow_worktree._worktree_path); the run state is `.flow/runs/<key>/`. Absent =
    no live lease to read (a leaked branch with no worktree), so the caller treats
    it as non-live rather than waiting on it forever.
    """
    base = repo.parent / f"{repo.name}.worktrees"
    for wt in sorted(glob.glob(str(base / f"feature-{key}*"))):
        run_dir = Path(wt) / ".flow" / "runs" / key
        if run_dir.exists():
            return run_dir
    return None


def liveness_map(repo: Path, keys: list[str]) -> dict[str, str]:
    """For each in-flight key, the lease state of its run ("live" = still working)."""
    now = lease._utcnow_iso()
    out: dict[str, str] = {}
    for key in keys:
        run_dir = _run_dir_for(repo, key)
        out[key] = "absent" if run_dir is None else str(lease.classify(run_dir, now).get("state"))
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
        inflight = sorted(set(sel.get("skipped_in_flight") or []) | set(_open_pr_keys(repo)))
        live = liveness_map(repo, inflight)
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
