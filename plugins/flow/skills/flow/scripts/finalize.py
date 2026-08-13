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
     `feat/<key>*` branch. Read `covers` from the worktree's ticket frontmatter here, while it
     still exists: step (d) reaps the worktree that holds it.
  3. Require merged-PR proof through the forge seam. An open PR, or no PR, is "not merged yet" and
     exits 3 with zero writes, so a host-owned watch can simply re-invoke until exit 0.
  4. Refuse on a live or corrupt run lease, and on a merged-head/worktree-tip mismatch (the
     worktree may hold a newer generation than the PR that merged).

Finalize (order mirrors the evolve-drain reap):
  a. transition the ticket to done through the tracker seam (skip when already terminal), then
     fan out the same transition to the `covers` siblings read during the probe;
  b. freeze the ship event (`observe_at_close`) before the run state it reads is destroyed;
  c. delete the remote branch through the forge seam;
  d. reap the local worktree + branch (`reap_worktree`: lease-gated, checkpoints dirty work to a
     rescue ref before removal).
Steps a, c, and d are best-effort, but c and d run only after b actually froze the event (or had
nothing to freeze: an ad-hoc worktree with no run state, or an event already on disk). Any other
observe outcome refuses before the destructive steps, because the reap destroys the only state
observe reads; a finalize invoked without the tracker's credentials must land in "refused", never
silently discard the ship event and report "finalized" (witnessed FT-1499, 2026-08-04).

Idempotent: every step skips when its outcome already holds, so re-running converges and a
finalized ticket exits 0 as a no-op.

Sweep (`--all`): enumerate every managed worktree that carries a `.flow/runs/<key>` run dir and
run the same per-key probe/close-out on each, isolating failures so one refused ticket never
stops the rest. Worktrees without a run dir (a human's ad-hoc worktree) are never swept; they
stay explicit-key-only. Exit 3 keys are normal sweep output ("still parked"), not failures.

CLI:
  finalize.py --workspace-root <root> (--key <key> | --all) [--dry-run]

Exit codes:
  0 = finalized (or already finalized); for --all, sweep completed with no probe errors
  2 = workspace/config/probe error
  3 = not merged yet (open PR or no PR; no writes; single-key form only)
  4 = refused (invoked from the doomed worktree, live/corrupt lease, head mismatch, or ship
      event not frozen; single-key form only)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import _runner
import branch_ticket
import observe_at_close
import ticket_frontmatter
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
    return _runner.checked(runner(args), what, FinalizeError, strip=True)


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


def _read_covers(wt_path: Path | None, key: str) -> list[str]:
    """The lead's co-delivered siblings, read from the run's own ticket frontmatter.

    Read during the probe, never later: the frontmatter lives inside the worktree that
    step (d) reaps, so after the reap there is no local evidence the siblings were ever
    co-delivered and no sweep can recover them (FT-1602/FT-1605, 2026-08-11, the run's
    own MAJOR friction entry). A branch-only finalize has no frontmatter to read and
    reports no covers rather than guessing from commit trailers.
    """
    if wt_path is None:
        return []
    fm = ticket_frontmatter.read(wt_path / ".flow" / "tickets" / f"{key}.md")
    covers = fm.get("covers") if isinstance(fm, dict) else None
    if isinstance(covers, str):
        covers = [covers]
    return [str(c).strip() for c in covers or [] if str(c).strip()]


def _transition_covers(main_root: Path, covers: list[str]) -> dict[str, Any]:
    """Close the lead's co-delivered siblings, one recorded outcome per key.

    Best-effort like the lead's own transition, but never silent: a miss here is
    unrecoverable once the reap removes the frontmatter that named these keys, so the
    report is the only place the failure can still be seen.
    """
    results: dict[str, Any] = {}
    for cover in covers:
        try:
            results[cover] = _transition_to_done(main_root, cover)
        except Exception as exc:
            results[cover] = {"action": "failed", "reason": str(exc)}
    return results


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
        report["covers"] = _read_covers(wt_path, key)
    else:
        wt_path, tip = None, None
        branch = _local_ticket_branch(runner, key)
        report.update({"worktree": None, "branch": branch, "tip": None, "covers": []})
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
    covers: list[str] = report["covers"]
    if dry_run:
        steps["transition"] = steps["observe"] = steps["delete_remote_branch"] = steps["reap"] = {
            "action": "would_run"
        }
        steps["transition_covers"] = {"action": "would_run", "keys": covers}
        return EXIT_OK, report

    try:
        steps["transition"] = _transition_to_done(main_root, key)
    except Exception as exc:
        steps["transition"] = {"action": "failed", "reason": str(exc)}

    steps["transition_covers"] = _transition_covers(main_root, covers)

    steps["observe"] = observe_at_close.observe_at_close(main_root, key, wt_path)
    action = steps["observe"].get("action")
    reason = str(steps["observe"].get("reason") or "")
    frozen = action == "observed" or (
        action == "skipped" and reason in ("already_observed", "no_run_state")
    )
    if not frozen:
        raise FinalizeRefused(
            f"ship event not frozen ({action}: {reason or 'no reason'}); "
            "worktree and run state preserved so a re-run can still observe it"
        )

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


def _sweep_candidates(workspace_root: Path) -> list[str]:
    """Ticket keys of managed worktrees that carry a `.flow/runs/<key>` run dir.

    Only run-carrying worktrees qualify: a worktree without one belongs to a human's
    ad-hoc work and is finalized only by explicit key.
    """
    invoking = workspace_root.expanduser().resolve()
    runner = _default_runner(invoking)
    entries = _enumerate_worktrees(
        _run(runner, ["git", "worktree", "list", "--porcelain"], "git worktree list")
    )
    if not entries or not entries[0].get("worktree"):
        raise FinalizeError("git worktree list returned no primary checkout")
    main_root = Path(str(entries[0]["worktree"])).expanduser().resolve()
    keys: list[str] = []
    for entry in entries[1:]:
        raw_path, branch = entry.get("worktree"), entry.get("branch")
        if not raw_path or not branch:
            continue
        path = Path(raw_path).expanduser().resolve()
        if not _managed(path, main_root):
            continue
        try:
            resolved = branch_ticket.resolve(main_root, path, branch=branch)
        except Exception:
            continue
        if not resolved or not is_ticket_branch(branch, resolved):
            continue
        if not (path / ".flow" / "runs" / resolved).is_dir():
            continue
        if resolved not in keys:
            keys.append(resolved)
    return keys


def finalize_all(workspace_root: Path, *, dry_run: bool = False) -> tuple[int, dict[str, Any]]:
    """Run the single-key close-out over every sweep candidate, isolating failures."""
    report: dict[str, Any] = {
        "dry_run": dry_run,
        "candidates": [],
        "finalized": [],
        "not_merged": [],
        "refused": [],
        "errors": [],
        "per_key": {},
    }
    candidates = _sweep_candidates(workspace_root)
    report["candidates"] = candidates
    for key in candidates:
        try:
            code, key_report = finalize(workspace_root, key, dry_run=dry_run)
        except FinalizeRefused as exc:
            report["refused"].append(key)
            report["per_key"][key] = {"refused": str(exc)}
            continue
        except FinalizeError as exc:
            report["errors"].append(key)
            report["per_key"][key] = {"error": str(exc)}
            continue
        report["per_key"][key] = key_report
        if code == EXIT_NOT_MERGED:
            report["not_merged"].append(key)
        else:
            report["finalized"].append(key)
    return (EXIT_ERROR if report["errors"] else EXIT_OK), report


def cli_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Close out one merged delivery.")
    parser.add_argument("--workspace-root", required=True)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--key")
    target.add_argument(
        "--all", action="store_true", help="sweep every run-carrying managed worktree"
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.all:
            code, report = finalize_all(Path(args.workspace_root), dry_run=bool(args.dry_run))
        else:
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
    "finalize_all",
]
