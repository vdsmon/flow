"""Post-merge worktree janitor: reap orphaned local worktrees whose PR already merged.

A standalone maintainer-only sweep, complementing the evolve drain's reap
(references/verb-evolve.md, drain step A). That reap is keyed on in-flight /
open-PR state; a run whose merge stage closed its bead and whose PR merged drops
out of every drain channel, and an attended hand-merge never had a local teardown
path at all. This sweep enumerates registered worktrees, joins each `feat/<key>`
branch to its merged PR, and tears down ONLY when the worktree's local tip sha
equals the merged PR head sha (never ancestry, because the repo squash-merges) AND
the bead is terminal AND the run is not live.

Every destructive act funnels through `flow_worktree.reap_worktree` (lease-flock +
checkpoint teardown); the only novel logic here is enumerate -> per-branch
merged-PR + tip join -> terminal-bead + is-live pre-skip -> dispatch reap.

Exit codes: 0 ok; 2 = a bd/git/gh call failed; 4 = not a maintainer setup.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import fleet
from _evolve_common import ACTIVE_STATUSES, ToolError, key_from_ref, loads, ok
from _runner import CwdRunner as Runner
from _runner import cwd_default_runner as _default_runner
from flow_worktree import reap_worktree
from maintainer import resolve_maintainer_repo

_ACTIVE = frozenset(ACTIVE_STATUSES.split(","))


def _enumerate_worktrees(porcelain: str) -> list[dict[str, Any]]:
    """Parse `git worktree list --porcelain` into flow-branch worktree records.

    Porcelain emits `worktree <path>` / `HEAD <sha>` / `branch refs/heads/<name>`
    per entry, blank-line separated. Keeps only entries whose branch resolves to a
    flow key (drops main, non-flow, and detached). Re-parsed here rather than
    importing flow_worktree's private `_parse_worktree_list`, which drops the HEAD
    sha this sweep gates on.
    """
    entries: list[dict[str, Any]] = []
    cur: dict[str, str] = {}
    for line in porcelain.splitlines():
        if line.startswith("worktree "):
            cur = {"worktree": line[len("worktree ") :].strip()}
        elif line.startswith("HEAD "):
            cur["tip"] = line[len("HEAD ") :].strip()
        elif line.startswith("branch "):
            cur["branch"] = line[len("branch ") :].strip().removeprefix("refs/heads/")
        elif not line.strip():
            _emit(entries, cur)
            cur = {}
    _emit(entries, cur)
    return entries


def _emit(entries: list[dict[str, Any]], cur: dict[str, Any]) -> None:
    branch = cur.get("branch")
    if not branch:
        return
    key = key_from_ref(branch)
    if key is None:
        return
    entries.append(
        {"key": key, "branch": branch, "worktree": cur.get("worktree"), "tip": cur.get("tip")}
    )


def classify_orphans(
    worktrees: list[dict[str, Any]], merged: dict[str, dict[str, Any]]
) -> dict[str, list[dict[str, Any]]]:
    """Pure: bucket each flow worktree by its merged-PR + tip-sha join.

    worktrees: `{key, branch, worktree, tip}` records (the flow-branch entries).
    merged: branch -> `{pr, head_oid}`, the merged PR per branch.

    A worktree is `reapable` only when its branch has a merged PR AND the local tip
    equals that PR's head sha. A tip AHEAD of the merged head (local commits after
    the merge, e.g. an unpushed reflect machinery commit) is NEVER reaped
    (`skipped_ahead`): reap's checkpoint reads a committed-but-unpushed tip as clean
    and would `branch -D` it, losing the work. A branch with no merged PR is
    `no_merged_pr`.
    """
    out: dict[str, list[dict[str, Any]]] = {"reapable": [], "skipped_ahead": [], "no_merged_pr": []}
    for wt in worktrees:
        branch = wt.get("branch")
        m = merged.get(branch) if branch else None
        if m is None:
            out["no_merged_pr"].append(wt)
        elif wt.get("tip") == m.get("head_oid"):
            out["reapable"].append({**wt, "pr": m.get("pr")})
        else:
            out["skipped_ahead"].append({**wt, "pr": m.get("pr")})
    return out


def _bead_is_active(runner: Runner, key: str) -> bool:
    """True when the bead's RAW bd status is active (open/in_progress/blocked) or
    unreadable. Fail-safe: any read or parse error reads active, so a reap never
    fires on a bead we could not confirm terminal (mirrors fleet.is_live's toward-
    live default). The check is membership in ACTIVE_STATUSES, not a
    `{done,cancelled}` set: beads emits `closed` for a closed bead, so a
    positive-terminal check would no-op on the primary target.
    """
    try:
        raw = ok(runner(["bd", "show", key, "--json"]), f"bd show {key}")
        data = json.loads(raw or "{}")
    except (ToolError, json.JSONDecodeError):
        return True
    if isinstance(data, list):
        data = data[0] if data else {}
    status = data.get("status") if isinstance(data, dict) else None
    if not status:
        return True
    return str(status) in _ACTIVE


def _merged_pr(runner: Runner, branch: str) -> dict[str, Any] | None:
    """The single merged PR for `branch`, `{pr, head_oid}`, else None.

    `headRefOid` resolves even after the remote head branch was deleted at
    squash-merge.
    """
    prs = loads(
        ok(
            runner(
                [
                    "gh",
                    "pr",
                    "list",
                    "--head",
                    branch,
                    "--state",
                    "merged",
                    "--json",
                    "number,headRefOid",
                    "--limit",
                    "1",
                ]
            ),
            f"gh pr list --head {branch}",
        )
    )
    if prs and isinstance(prs[0], dict):
        return {"pr": prs[0].get("number"), "head_oid": prs[0].get("headRefOid")}
    return None


def cli_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Reap orphaned local worktrees whose PR already merged."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sweep = sub.add_parser("sweep", help="reap merged-PR orphan worktrees")
    sweep.add_argument("--workspace-root", required=True)
    sweep.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    ws = Path(args.workspace_root)
    repo = resolve_maintainer_repo(ws)
    if repo is None:
        print("not a flow maintainer setup; worktree janitor is dormant", file=sys.stderr)
        return 4

    run = _default_runner(repo)
    result: dict[str, object] = {
        "reaped": [],
        "reap_failed": [],
        "skipped_live": [],
        "skipped_active_bead": [],
        "skipped_ahead": [],
        "no_merged_pr": [],
        "dry_run": bool(args.dry_run),
    }
    try:
        worktrees = _enumerate_worktrees(
            ok(run(["git", "worktree", "list", "--porcelain"]), "git worktree list")
        )
        merged = {wt["branch"]: m for wt in worktrees if (m := _merged_pr(run, wt["branch"]))}
        classification = classify_orphans(worktrees, merged)
        result["skipped_ahead"] = classification["skipped_ahead"]
        result["no_merged_pr"] = classification["no_merged_pr"]
        for entry in classification["reapable"]:
            key, branch = entry["key"], entry["branch"]
            # Terminal-bead READ gate: an active bead defers to queue_drain's
            # close-then-reap. Read-only, NEVER `bd close` here.
            if _bead_is_active(run, key):
                result["skipped_active_bead"].append(entry)
                continue
            # is_live takes the RAW ws (resolves the pool via resolve_memory_base),
            # not repo; lease-only, fail-safe toward live.
            if fleet.is_live(ws, key):
                result["skipped_live"].append(entry)
                continue
            if args.dry_run:
                result["reaped"].append({**entry, "receipt": None})
                continue
            # Isolate each teardown: a mid-sweep reap failure must not abort the
            # loop and lose the JSON audit trail of the reaps already done.
            # reap_worktree raises _GitError, not ToolError, so the outer handler
            # would miss it.
            try:
                receipt = reap_worktree(ticket=key, main_root=repo, branch=branch)
            except Exception as exc:  # one bad worktree must not sink the whole sweep
                result["reap_failed"].append({**entry, "reap_error": str(exc)})
                continue
            result["reaped"].append({**entry, "receipt": receipt})
    except ToolError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(cli_main(sys.argv[1:]))
