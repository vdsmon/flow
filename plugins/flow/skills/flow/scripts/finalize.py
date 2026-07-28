"""Post-merge finalizer for one delivered ticket.

Library + thin CLI behind the `finalize` facade command (`FLOW ticket finalize <ticket>`).

A delivery workspace parks a green PR for the human; the human merges it on the
forge. Nothing on that path closes the ticket, freezes the ship event, deletes the remote branch,
or reaps the worktree: the merge stage handler is `none` outside the self-target workspace, and the
`worktree_janitor` sweep requires the tracker to ALREADY read done/cancelled, so a merged-but-open
ticket preserves its worktree forever. This module is the delivery-workspace close-out
that sequences
the existing primitives once, gated on merged-PR proof.

Probe (no writes):
  1. Enumerate worktrees; the primary checkout is the target root. Refuse when invoked FROM the
     ticket's own worktree (the driver must re-invoke from the primary checkout before the reap).
  2. Locate the ticket's branch: the managed worktree's checked-out branch, else the unique local
     `feat/<key>*` branch.
  3. Require merged-PR proof through the forge seam. An open PR, or no PR, is "not merged yet" and
     exits 3 with zero writes, so a host-owned watch can simply re-invoke until exit 0.
  4. Refuse on a live or corrupt run lease, and on a merged-head/worktree-tip mismatch (the
     worktree may hold a newer generation than the PR that merged).

Finalize (each step best-effort once the probe passes; order mirrors the evolve-drain reap):
  a. transition the ticket to done through the tracker seam (skip when already terminal);
  b. freeze the ship event (`observe_at_close`) before the run state it reads is destroyed;
  c. delete the remote branch through the forge seam;
  d. reap the local worktree + branch (`reap_worktree`: lease-gated, checkpoints dirty work to a
     rescue ref before removal).

Idempotent: every step skips when its outcome already holds, so re-running converges and a
finalized ticket exits 0 as a no-op.

CLI:
  finalize.py --workspace-root <root> --key <key> [--dry-run]

Exit codes:
  0 = finalized (or already finalized)
  2 = workspace/config/probe error
  3 = not merged yet (open PR or no PR; no writes)
  4 = refused (invoked from the doomed worktree, live/corrupt lease, or head mismatch)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import branch_ticket
import observe_at_close
from _runner import CwdRunner as Runner
from _runner import cwd_default_runner as _default_runner
from flow_worktree import is_ticket_branch, reap_worktree
from forge import make_forge, read_forge_config
from tracker import make_tracker
from tracker_cli import _read_tracker_config, _select_transition_id
from worktree_janitor import (
    _candidate_lease_blocker,
    _enumerate_worktrees,
    _managed,
    _same_commit,
)

_TERMINAL_STATES = frozenset({"done", "cancelled"})

EXIT_OK = 0
EXIT_ERROR = 2
EXIT_NOT_MERGED = 3
EXIT_REFUSED = 4


class FinalizeError(Exception):
    """A probe failed before any write decision could be made."""


class FinalizeRefused(Exception):
    """The probe found evidence that makes the write sequence unsafe."""


def _run(runner: Runner, args: list[str], what: str) -> str:
    result = runner(args)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown error").strip()
        raise FinalizeError(f"{what} failed: {detail}")
    return result.stdout.strip()


def _locate_worktree(
    entries: list[dict[str, str | None]], main_root: Path, key: str
) -> tuple[Path, str, str] | None:
    """The ticket's managed worktree as `(path, branch, tip)`, or None."""
    for entry in entries[1:]:
        raw_path, branch, tip = entry.get("worktree"), entry.get("branch"), entry.get("tip")
        if not raw_path or not branch or not tip:
            continue
        path = Path(raw_path).expanduser().resolve()
        if not _managed(path, main_root):
            continue
        try:
            resolved = branch_ticket.resolve(main_root, path, branch=branch)
        except Exception:
            continue
        if resolved == key and is_ticket_branch(branch, key):
            return path, branch, tip
    return None


def _local_ticket_branch(runner: Runner, key: str) -> str | None:
    """The unique local branch belonging to `key`, or None; ambiguity refuses."""
    raw = _run(
        runner,
        ["git", "for-each-ref", "refs/heads", "--format=%(refname:short)"],
        "list local branches",
    )
    matches = [b for b in raw.splitlines() if is_ticket_branch(b.strip(), key)]
    if len(matches) > 1:
        raise FinalizeRefused(f"multiple local branches belong to {key}: {', '.join(matches)}")
    return matches[0].strip() if matches else None


def _transition_to_done(main_root: Path, key: str) -> dict[str, Any]:
    """Transition the ticket to its done state; skip when already terminal."""
    tracker = make_tracker(_read_tracker_config(main_root))
    normalized = tracker.state(key).get("normalized")
    if normalized in _TERMINAL_STATES:
        return {"action": "skipped", "reason": f"already_{normalized}"}
    transitions = [dict(t) for t in tracker.list_transitions(key)]
    selected = _select_transition_id(transitions, "done") or _select_transition_id(
        transitions, "closed"
    )
    if selected is None:
        return {"action": "failed", "reason": "no done/closed transition available"}
    result = tracker.transition(key, selected)
    failure = (result or {}).get("failure_kind", "none")
    if failure not in ("", "none", None):
        return {"action": "failed", "reason": str(failure)}
    return {"action": "transitioned", "from": normalized}


def finalize(
    workspace_root: Path, key: str, *, dry_run: bool = False
) -> tuple[int, dict[str, Any]]:
    """Probe merged-PR proof, then run the close-out sequence. Returns (exit_code, report)."""
    invoking = workspace_root.expanduser().resolve()
    runner = _default_runner(invoking)
    entries = _enumerate_worktrees(
        _run(runner, ["git", "worktree", "list", "--porcelain"], "git worktree list")
    )
    if not entries or not entries[0].get("worktree"):
        raise FinalizeError("git worktree list returned no primary checkout")
    main_root = Path(str(entries[0]["worktree"])).expanduser().resolve()
    report: dict[str, Any] = {"key": key, "target_root": str(main_root), "dry_run": dry_run}

    located = _locate_worktree(entries, main_root, key)
    if located is not None:
        wt_path, branch, tip = located
        if wt_path == invoking:
            raise FinalizeRefused(
                f"invoked from the ticket's own worktree {wt_path}; "
                "re-invoke from the primary checkout so the reap does not remove the cwd"
            )
        report.update({"worktree": str(wt_path), "branch": branch, "tip": tip})
    else:
        wt_path, tip = None, None
        branch = _local_ticket_branch(runner, key)
        report.update({"worktree": None, "branch": branch, "tip": None})
        if branch is None:
            raise FinalizeError(f"no worktree or local branch belongs to {key}; nothing to prove")

    forge_config = read_forge_config(main_root)
    if forge_config is None:
        raise FinalizeError("workspace.toml has no [forge] block")
    forge = make_forge(forge_config)
    merged_pr = forge.detect_pr(branch, state="merged")
    if merged_pr is None:
        open_pr = forge.detect_pr(branch, state="open")
        report["pr"] = open_pr
        report["reason"] = "pr_open" if open_pr is not None else "no_pr"
        return EXIT_NOT_MERGED, report
    report["pr"] = merged_pr

    if wt_path is not None:
        blocker = _candidate_lease_blocker(wt_path, key)
        if blocker is not None:
            raise FinalizeRefused(f"{blocker[0]} lease at {blocker[1]}; a run may still own this")
        head_sha = merged_pr.get("head_sha")
        # Skipping when the merged PR reports no head is deliberate: the janitor sweep treats a
        # missing head as a mismatch, and making the two symmetric would change a reachable path.
        if head_sha and not _same_commit(tip, head_sha):
            raise FinalizeRefused(
                f"worktree tip {tip} does not match merged PR head {head_sha}; "
                "the worktree may hold newer work"
            )

    steps: dict[str, Any] = {}
    report["steps"] = steps
    if dry_run:
        steps["transition"] = steps["observe"] = steps["delete_remote_branch"] = steps["reap"] = {
            "action": "would_run"
        }
        return EXIT_OK, report

    try:
        steps["transition"] = _transition_to_done(main_root, key)
    except Exception as exc:
        steps["transition"] = {"action": "failed", "reason": str(exc)}

    steps["observe"] = observe_at_close.observe_at_close(main_root, key, wt_path)

    try:
        forge.delete_branch(branch)
        steps["delete_remote_branch"] = {"action": "deleted"}
    except Exception as exc:
        steps["delete_remote_branch"] = {"action": "failed_or_absent", "reason": str(exc)}

    try:
        receipt = reap_worktree(ticket=key, main_root=main_root, branch=branch, expected_tip=tip)
        steps["reap"] = receipt
    except Exception as exc:
        steps["reap"] = {"action": "failed", "reason": str(exc)}

    return EXIT_OK, report


def cli_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Close out one merged delivery.")
    parser.add_argument("--workspace-root", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        code, report = finalize(Path(args.workspace_root), args.key, dry_run=bool(args.dry_run))
    except FinalizeRefused as exc:
        print(json.dumps({"key": args.key, "refused": str(exc)}, indent=2))
        return EXIT_REFUSED
    except FinalizeError as exc:
        print(f"finalize: {exc}", file=sys.stderr)
        return EXIT_ERROR
    print(json.dumps(report, indent=2, default=str))
    return code


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = [
    "EXIT_NOT_MERGED",
    "EXIT_OK",
    "EXIT_REFUSED",
    "FinalizeError",
    "FinalizeRefused",
    "cli_main",
    "finalize",
]
