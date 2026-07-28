"""Deterministic half of manager seating (`FLOW manager`).

Library + thin CLI behind the `manager-seat` facade command. `references/manager.md` §Seating runs
it first: the judgment half of seating (charter, memory, ledger, queue) starts from the JSON posture
this script emits, so every seated manager begins from the same fresh mechanical picture instead of
re-deriving it by hand.

Sequence (live mode):
  1. Resolve the primary checkout from `git worktree list`; seating may be invoked from the
     bench itself, and the posture always describes the primary checkout.
  2. `git fetch origin`, so every judgment that follows sees the current remote.
  3. Sync `refs/remotes/origin/HEAD` via `remote set-head origin --auto` (a plain fetch never
     rewrites an existing symref) and resolve the remote default branch from it; a dangling name
     counts as unset.
  4. Resolve the integration branch: the primary checkout's `[create_pr] base`, tried as
     `origin/<base>`, when the workspace declares one; the remote default otherwise. A declared base
     that fails to resolve falls back to the remote default and is named in
     `integration_unresolved`.
  5. Ensure the standing bench worktree `.claude/worktrees/flow-manager`: created detached at
     the integration branch when absent. An existing bench is NEVER mutated, whatever its state;
     a bench parked mid-task holds in-flight inline work that only judgment may resume or park.
  6. Emit the posture: primary-checkout branch/cleanliness/distance-from-integration-branch, bench
     state, fetch result, and both branch names (`default_branch`, `integration_branch`).

`--dry-run` performs no ref update and no filesystem write (no fetch, no set-head, no worktree add);
the posture reports `would_fetch` / `would_create`, computed from the refs as they are.

CLI:
  manager_seat.py --workspace-root <root> [--dry-run]

Exit codes:
  0 = posture emitted, every action succeeded
  2 = probe error (stderr), or a fetch / bench failure (posture still printed)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from _runner import Runner, default_runner
from _workspace import WorkspaceConfigError, load_workspace_toml
from worktree_janitor import _enumerate_worktrees

BENCH_RELPATH = Path(".claude/worktrees/flow-manager")

EXIT_OK = 0
EXIT_ERROR = 2


class SeatError(Exception):
    """A probe failed before any posture could be assembled."""


def _run(runner: Runner, args: list[str], cwd: Path, what: str) -> str:
    result = runner(args, cwd)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown error").strip()
        raise SeatError(f"{what} failed: {detail}")
    return result.stdout.strip()


def _primary_checkout(runner: Runner, invoking: Path) -> tuple[Path, list[dict[str, str | None]]]:
    entries = _enumerate_worktrees(
        _run(runner, ["git", "worktree", "list", "--porcelain"], invoking, "git worktree list")
    )
    if not entries or not entries[0].get("worktree"):
        raise SeatError("git worktree list returned no primary checkout")
    return Path(str(entries[0]["worktree"])).expanduser().resolve(), entries


def _default_branch(runner: Runner, main_root: Path, *, allow_set_head: bool) -> str | None:
    """The remote default ref (`origin/<HEAD>`) as the remote reports it, or None.

    Live mode always runs `remote set-head origin --auto` first: a plain fetch never rewrites an
    existing `origin/HEAD`, so after the remote renames its default the local symref keeps naming
    the dead branch forever (dangling once pruned, a stale snapshot otherwise). Dry-run reads the
    local symref only, because the repair writes a ref; either way a name whose target ref no longer
    exists counts as unset rather than being returned as a revision that cannot resolve.
    """
    if allow_set_head:
        runner(["git", "remote", "set-head", "origin", "--auto"], main_root)
    probe = ["git", "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"]
    head = runner(probe, main_root)
    name = head.stdout.strip() if head.returncode == 0 else ""
    if (
        name
        and runner(["git", "rev-parse", "--verify", "--quiet", name], main_root).returncode != 0
    ):
        name = ""
    return name or None


def _integration_base_config(main_root: Path) -> str | None:
    """`[create_pr] base` from the primary checkout's workspace.toml (non-empty str); None when
    unset, unreadable, or the workspace declares no base."""
    try:
        config = load_workspace_toml(main_root)
    except WorkspaceConfigError:
        return None
    section = config.get("create_pr")
    if not isinstance(section, dict):
        return None
    value = section.get("base")
    return value if isinstance(value, str) and value else None


def _integration_branch(
    runner: Runner, main_root: Path, default: str | None
) -> tuple[str | None, str | None]:
    """The branch this repository's work lands on: `[create_pr] base` as a remote-tracking ref when
    the workspace declares one, the remote default otherwise. Returns `(integration_branch,
    unresolved_reason)`; the reason is set only when a declared base failed to resolve, so a typo'd
    base is visible in the posture instead of silently falling back.

    Each candidate is checked as an exact ref under `refs/remotes/`, never resolved as a revision:
    `git rev-parse --verify origin/main~1` answers with a commit, and the manager compares
    `integration_branch` against a branch name, so an accepted revision expression would read as a
    permanent, unfixable divergence. Exact-ref matching also keeps a local branch literally named
    `origin/nodev` from shadowing the remote-tracking lookup, and excludes tags and bare SHAs.
    """
    base = _integration_base_config(main_root)
    if base is None:
        return default, None
    candidates = [f"origin/{base}"]
    if base.startswith("origin/"):
        candidates.append(base)
    for candidate in candidates:
        probe = runner(
            ["git", "show-ref", "--verify", "--quiet", f"refs/remotes/{candidate}"], main_root
        )
        if probe.returncode == 0:
            return candidate, None
    return default, f"[create_pr] base {base!r} did not resolve to a remote branch"


def _tree_posture(runner: Runner, root: Path, integration: str | None) -> dict[str, Any]:
    """One checkout's branch (None = detached), head, cleanliness, and distance from the integration
    branch."""
    branch_probe = runner(["git", "symbolic-ref", "--quiet", "--short", "HEAD"], root)
    branch = branch_probe.stdout.strip() if branch_probe.returncode == 0 else None
    head = _run(runner, ["git", "rev-parse", "HEAD"], root, "git rev-parse HEAD")
    clean = _run(runner, ["git", "status", "--porcelain"], root, "git status") == ""
    posture: dict[str, Any] = {"branch": branch, "head": head, "clean": clean}
    if integration is not None:
        counts = runner(
            ["git", "rev-list", "--left-right", "--count", f"{integration}...HEAD"], root
        )
        if counts.returncode == 0 and counts.stdout.split():
            behind, ahead = counts.stdout.split()
            posture["behind_integration"] = int(behind)
            posture["ahead_integration"] = int(ahead)
    return posture


def _bench_posture(
    runner: Runner,
    main_root: Path,
    integration: str | None,
    registered: set[Path],
    *,
    dry_run: bool,
) -> dict[str, Any]:
    bench = main_root / BENCH_RELPATH
    posture: dict[str, Any] = {"path": str(bench)}
    if bench.exists() and not bench.is_dir():
        posture.update({"action": "unrecognized", "reason": "path exists but is not a directory"})
        return posture
    if bench.is_dir():
        # A plain directory at the bench path sits INSIDE the primary work tree, so
        # `--is-inside-work-tree` cannot distinguish it; a linked worktree's toplevel is itself.
        top = runner(["git", "rev-parse", "--show-toplevel"], bench)
        if top.returncode != 0 or Path(top.stdout.strip()).resolve() != bench.resolve():
            posture.update(
                {"action": "unrecognized", "reason": "path exists but is not a git worktree"}
            )
            return posture
        try:
            tree = _tree_posture(runner, bench, integration)
        except SeatError as exc:
            posture.update({"action": "failed", "reason": str(exc)})
            return posture
        posture["action"] = "present"
        posture.update(tree)
        return posture
    if bench.resolve() in registered:
        # `worktree add` refuses a registered-but-deleted bench, so dry-run must predict that
        # refusal instead of promising a creation the live run cannot perform.
        posture.update(
            {
                "action": "failed",
                "reason": "bench directory is missing but still registered as a worktree; "
                "run `git worktree prune` in the primary checkout",
            }
        )
        return posture
    if dry_run:
        posture["action"] = "would_create"
        return posture
    if integration is None:
        posture.update(
            {"action": "failed", "reason": "no integration branch to create the bench from"}
        )
        return posture
    bench.parent.mkdir(parents=True, exist_ok=True)
    result = runner(["git", "worktree", "add", "--detach", str(bench), integration], main_root)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown error").strip()
        posture.update({"action": "failed", "reason": detail})
        return posture
    posture["action"] = "created"
    posture.update(_tree_posture(runner, bench, integration))
    return posture


def seat(workspace_root: Path, *, dry_run: bool = False) -> tuple[int, dict[str, Any]]:
    """Fetch, resolve the default and integration branches, ensure the bench, and return (exit_code,
    posture)."""
    invoking = workspace_root.expanduser().resolve()
    runner = default_runner()
    main_root, entries = _primary_checkout(runner, invoking)
    registered = {
        Path(str(e["worktree"])).expanduser().resolve() for e in entries[1:] if e.get("worktree")
    }
    posture: dict[str, Any] = {"target_root": str(main_root), "dry_run": dry_run}
    failed = False

    if dry_run:
        posture["fetch"] = {"action": "would_fetch"}
    else:
        fetched = runner(["git", "fetch", "--quiet", "origin"], main_root)
        if fetched.returncode == 0:
            posture["fetch"] = {"action": "fetched"}
        else:
            detail = (fetched.stderr or fetched.stdout or "unknown error").strip()
            posture["fetch"] = {"action": "failed", "reason": detail}
            failed = True

    default = _default_branch(runner, main_root, allow_set_head=not dry_run)
    posture["default_branch"] = default
    integration, unresolved = _integration_branch(runner, main_root, default)
    posture["integration_branch"] = integration
    if unresolved is not None:
        posture["integration_unresolved"] = unresolved
    posture["workspace_root"] = _tree_posture(runner, main_root, integration)
    bench = _bench_posture(runner, main_root, integration, registered, dry_run=dry_run)
    posture["bench"] = bench
    if bench.get("action") in ("failed", "unrecognized"):
        failed = True
    return (EXIT_ERROR if failed else EXIT_OK), posture


def cli_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Deterministic half of manager seating.")
    parser.add_argument("--workspace-root", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        code, posture = seat(Path(args.workspace_root), dry_run=bool(args.dry_run))
    except SeatError as exc:
        print(f"manager-seat: {exc}", file=sys.stderr)
        return EXIT_ERROR
    print(json.dumps(posture, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = [
    "BENCH_RELPATH",
    "EXIT_ERROR",
    "EXIT_OK",
    "SeatError",
    "cli_main",
    "seat",
]
