"""Deterministic half of the scrutinize seating (`FLOW scrutinize`).

Library + thin CLI behind the `scrutinize-seat` facade command. The Seating section of
`references/scrutinize.md` runs it first so every seat begins from the same bounded local picture
instead of re-deriving it by hand or eagerly reading remote work queues.

Sequence (live mode):
  0. Refuse outside the self-target: scrutiny is flow's own maintenance verb, so the primary
     checkout must contain the engine source tree. A delivery workspace runs plain driver sessions
     instead of seating a scrutiny.
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
  5. Read configured tracker/forge names without constructing either adapter, and scan registered
     worktrees for unfinished, failed, stale, or corrupt base and revision runs.
  6. Ensure the standing bench worktree `.claude/worktrees/flow-bench`: created detached at the
     integration branch when absent. A clean detached bench is fast-forward re-parked only when no
     unfinished local run makes that unsafe; all other existing bench state is preserved.
  7. Emit the posture: Git state, configured integrations, and unfinished local run evidence.

`--dry-run` performs no ref update and no filesystem write (no fetch, no set-head, no worktree add);
the posture reports `would_fetch` / `would_create`, computed from the refs as they are.

CLI:
  scrutinize_seat.py --workspace-root <root> [--dry-run]

Exit codes:
  0 = posture emitted, every action succeeded
  2 = probe error (stderr), or a fetch / bench failure (posture still printed)
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import lease
from _runner import Runner, default_runner
from _timeutil import utcnow_iso
from _workspace import WorkspaceConfigError, load_workspace_toml
from worktree_janitor import _enumerate_worktrees

BENCH_RELPATH = Path(".claude/worktrees/flow-bench")

# The engine source tree only the self-target contains; delivery workspaces install the engine
# rather than carrying its source, so this path is what tells the two apart.
SELF_TARGET_MARKER = Path("plugins/flow/skills/flow/scripts")

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


def _configured_integrations(main_root: Path) -> dict[str, str | None]:
    """Configured adapter names without importing or constructing either adapter."""
    try:
        config = load_workspace_toml(main_root)
    except WorkspaceConfigError:
        return {"tracker": None, "forge": None}
    tracker = config.get("tracker")
    forge = config.get("forge")
    tracker_name = tracker.get("backend") if isinstance(tracker, dict) else None
    forge_name = forge.get("backend") if isinstance(forge, dict) else None
    return {
        "tracker": tracker_name if tracker_name in ("jira", "beads") else None,
        "forge": forge_name if forge_name in ("github", "bitbucket") else None,
    }


def _run_dirs(worktree: Path) -> list[tuple[str, str | None, Path]]:
    def has_evidence(path: Path) -> bool:
        return bool(
            (path / "state.json").exists()
            or (path / "run.lock").exists()
            or list(path.glob("state.json.*.bak"))
            or list(path.glob("state.json.quarantine.*"))
        )

    runs_root = worktree / ".flow" / "runs"
    if not runs_root.is_dir():
        return []
    found: list[tuple[str, str | None, Path]] = []
    for base in sorted(path for path in runs_root.iterdir() if path.is_dir()):
        if has_evidence(base):
            found.append((base.name, None, base))
        revisions = base / "revisions"
        if revisions.is_dir():
            revision_dirs = sorted(path for path in revisions.iterdir() if path.is_dir())
            found.extend(
                (base.name, revision.name, revision)
                for revision in revision_dirs
                if has_evidence(revision)
            )
    return found


def _classify_run(
    ticket: str,
    revision: str | None,
    run_dir: Path,
    worktree: Path,
    *,
    now: str,
    current_boot: str,
    hostname: str,
) -> dict[str, Any] | None:
    row: dict[str, Any] = {
        "ticket": ticket,
        "revision": revision,
        "kind": "revision" if revision is not None else "base",
        "worktree": str(worktree),
        "path": str(run_dir),
    }
    try:
        raw = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
        stages = raw["stages"]
        if (
            not isinstance(raw.get("run_id"), str)
            or not isinstance(stages, dict)
            or any(not isinstance(record, dict) for record in stages.values())
        ):
            raise ValueError("invalid state shape")
        statuses = [record.get("status") for record in stages.values()]
        if any(
            status not in ("pending", "in_progress", "completed", "failed") for status in statuses
        ):
            raise ValueError("invalid stage status")
        row["run_id"] = raw["run_id"]
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        row["status"] = "corrupt"
        row["reason"] = "state.json is missing or invalid"
        return row

    lease_info = lease.classify(
        run_dir,
        now,
        current_boot=current_boot,
        hostname=hostname,
    )
    lease_state = str(lease_info.get("state"))
    row["lease"] = lease_state
    if lease_state == "corrupt":
        row["status"] = "corrupt"
        row["reason"] = "run.lock is invalid"
    elif "failed" in statuses:
        row["status"] = "failed"
    elif lease_state.startswith("expired_"):
        row["status"] = "stale"
    elif statuses and all(status == "completed" for status in statuses) and lease_state == "free":
        return None
    else:
        row["status"] = "unfinished"
    return row


def _local_runs(entries: list[dict[str, str | None]]) -> list[dict[str, Any]]:
    """Return non-terminal local run evidence from every registered worktree."""
    now = utcnow_iso()
    current_boot = lease.boot_id()
    hostname = lease.hostname()
    rows: list[dict[str, Any]] = []
    for entry in entries:
        raw_path = entry.get("worktree")
        if not raw_path:
            continue
        worktree = Path(str(raw_path)).expanduser().resolve()
        if not worktree.is_dir():
            continue
        for ticket, revision, run_dir in _run_dirs(worktree):
            row = _classify_run(
                ticket,
                revision,
                run_dir,
                worktree,
                now=now,
                current_boot=current_boot,
                hostname=hostname,
            )
            if row is not None:
                rows.append(row)

    identities: dict[tuple[str, str | None], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        identities[(str(row["ticket"]), row["revision"])].append(row)
    for duplicates in identities.values():
        signatures = {(row.get("run_id"), row["status"]) for row in duplicates}
        if len(duplicates) > 1 and len(signatures) > 1:
            for row in duplicates:
                row["contradictory"] = True
    severity = {"corrupt": 0, "failed": 1, "stale": 2, "unfinished": 3}
    return sorted(
        rows,
        key=lambda row: (
            severity[str(row["status"])],
            str(row["ticket"]),
            str(row["revision"] or ""),
            str(row["worktree"]),
        ),
    )


def _integration_branch(
    runner: Runner, main_root: Path, default: str | None
) -> tuple[str | None, str | None]:
    """The branch this repository's work lands on: `[create_pr] base` as a remote-tracking ref when
    the workspace declares one, the remote default otherwise. Returns `(integration_branch,
    unresolved_reason)`; the reason is set only when a declared base failed to resolve, so a typo'd
    base is visible in the posture instead of silently falling back.

    Each candidate is checked as an exact ref under `refs/remotes/`, never resolved as a revision:
    `git rev-parse --verify origin/main~1` answers with a commit, and the seat compares
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


def _maybe_fast_forward_primary(
    runner: Runner,
    main_root: Path,
    integration: str | None,
    tree: dict[str, Any],
    *,
    dry_run: bool,
    safe: bool,
) -> dict[str, Any]:
    expected_branch = integration.removeprefix("origin/") if integration is not None else None
    can_fast_forward = (
        safe
        and integration is not None
        and tree.get("branch") == expected_branch
        and tree.get("clean") is True
        and tree.get("ahead_integration", 0) == 0
        and tree.get("behind_integration", 0) > 0
    )
    if not can_fast_forward:
        return tree
    if dry_run:
        return {**tree, "action": "would_fast_forward"}
    result = runner(["git", "merge", "--ff-only", integration], main_root)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown error").strip()
        return {**tree, "action": "fast_forward_failed", "reason": detail}
    return {**_tree_posture(runner, main_root, integration), "action": "fast_forwarded"}


def _bench_posture(
    runner: Runner,
    main_root: Path,
    integration: str | None,
    registered: set[Path],
    *,
    dry_run: bool,
    allow_repark: bool,
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
        can_repark = (
            allow_repark
            and integration is not None
            and tree.get("branch") is None
            and tree.get("clean") is True
            and tree.get("ahead_integration", 0) == 0
            and tree.get("behind_integration", 0) > 0
        )
        if can_repark and dry_run:
            posture["action"] = "would_repark"
            posture.update(tree)
            return posture
        if can_repark:
            result = runner(["git", "checkout", "--detach", integration], bench)
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "unknown error").strip()
                posture.update({"action": "failed", "reason": detail})
                return posture
            posture["action"] = "reparked"
            posture.update(_tree_posture(runner, bench, integration))
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


def _existing_bench_blocks_fast_forward(
    runner: Runner, main_root: Path, integration: str | None
) -> bool:
    """Whether an existing bench has work or invalid posture that must be preserved."""
    bench = main_root / BENCH_RELPATH
    if not bench.exists():
        return False
    if not bench.is_dir():
        return True
    top = runner(["git", "rev-parse", "--show-toplevel"], bench)
    if top.returncode != 0 or Path(top.stdout.strip()).resolve() != bench.resolve():
        return True
    try:
        tree = _tree_posture(runner, bench, integration)
    except SeatError:
        return True
    return bool(
        tree.get("branch") is not None
        or tree.get("clean") is not True
        or tree.get("ahead_integration", 0) > 0
    )


def seat(workspace_root: Path, *, dry_run: bool = False) -> tuple[int, dict[str, Any]]:
    """Fetch, resolve the default and integration branches, ensure the bench, and return (exit_code,
    posture)."""
    invoking = workspace_root.expanduser().resolve()
    runner = default_runner()
    main_root, entries = _primary_checkout(runner, invoking)
    if not (main_root / SELF_TARGET_MARKER).is_dir():
        raise SeatError(
            "scrutinize seats only in flow's self-target repository; "
            "a delivery workspace runs plain driver sessions instead"
        )
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
    posture["integrations"] = _configured_integrations(main_root)
    local_runs = _local_runs(entries)
    posture["local_runs"] = local_runs
    bench_has_work = _existing_bench_blocks_fast_forward(runner, main_root, integration)
    root_tree = _tree_posture(runner, main_root, integration)
    posture["workspace_root"] = _maybe_fast_forward_primary(
        runner,
        main_root,
        integration,
        root_tree,
        dry_run=dry_run,
        safe=not local_runs and not bench_has_work,
    )
    bench = _bench_posture(
        runner,
        main_root,
        integration,
        registered,
        dry_run=dry_run,
        allow_repark=not local_runs,
    )
    posture["bench"] = bench
    if posture["workspace_root"].get("action") == "fast_forward_failed":
        failed = True
    if bench.get("action") in ("failed", "unrecognized"):
        failed = True
    return (EXIT_ERROR if failed else EXIT_OK), posture


def cli_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Deterministic half of the scrutinize seating.")
    parser.add_argument("--workspace-root", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        code, posture = seat(Path(args.workspace_root), dry_run=bool(args.dry_run))
    except SeatError as exc:
        print(f"scrutinize-seat: {exc}", file=sys.stderr)
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
