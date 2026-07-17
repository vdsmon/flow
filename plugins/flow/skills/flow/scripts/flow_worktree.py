"""flow_worktree.py: post-approval bootstrap for the ticket pipeline.

After Flow approves a target plan, this seeds a git worktree so delivery resumes
directly at the implement stage. The spec session then enters this worktree (EnterWorktree) and
continues the `do` pipeline in the SAME conversation; running it unattended is a separate,
harness-level choice (`/bg`), not this script's concern.

  1. validate an optional native-gate receipt, pin its approved SHA and route snapshot,
     then git worktree add -b <branch> <worktree> <base>
  2. copy gitignored dev config main->worktree; ensure .flow/.initialized + workspace.toml exist (a
     git worktree only materializes committed files)
  3. mise trust the worktree (toolchain) unless --no-mise-trust
  4. redirect the worktree's memory store to the main checkout's resolved memory base (its own
     `.flow/runtime/memory-root` / `[memory].root` honored) via runtime metadata
     (shared store, so per-ticket worktrees don't fragment the compounding-knowledge layer; tracked
     workspace.toml untouched)
  5. seed state.json: plan marked completed with its output_path; plan.out written from --plan-from;
     ticket left pending so the pipeline self-fetches ticket.json and stamps frontmatter (keeps the
     bootstrap offline; tracker auth stays live)
  6. freeze the normalized owner and desired/effective agent-route snapshot in the run
  7. stamp commit_type/commit_summary (and e2e_recipe unless e2e is explicitly disabled) into the
     worktree frontmatter so the commit + e2e stages do not block on a prompt
  8. persist the approval receipt and advance its crash journal when routed planning
     supplied one
  9. print the worktree path (the spec session enters it via EnterWorktree)

The bootstrap holds NO run lease; the pipeline's cmd_init acquires it under the run_id seeded here
(it sees that run_id as the owner, so resume is clean). It DOES transiently hold the canonical
per-ticket bootstrap CLAIM (a flock on <main_root>/.flow/tickets/<ticket>.claim, held across
worktree-add → state-seed → frontmatter stamp, released at bootstrap exit), under which it refuses
(exit 4) when a live sibling run already holds this ticket. The .claim file persists after release
by design (deleting a flock target would race a waiter). Also under the claim, past the live-sibling
check but before `git worktree add -b`: a DEAD sibling checked out on the exact colliding branch or
worktree path (the flow-vpg1 case, a manual relaunch after a spend-limit death) is auto-reaped so
the worktree-add no longer collides; a checkpoint failure during that auto-reap refuses (exit 2)
rather than destroy the sibling's uncommitted work, and a lease that goes live under the reap's own
flock (TOCTOU) refuses (exit 4) same as a live sibling.

Exit codes (create):
  0 = ok (may carry warnings on stderr)
  1 = git / worktree error
  2 = bad args / missing main workspace config (also: auto-reap of a dead colliding sibling
      could not checkpoint its uncommitted work, so the sibling is left intact)
  3 = I/O error
  4 = duplicate claim (a live sibling run already holds this ticket, or went live mid-auto-reap)
  6 = bead is terminal (closed/done/cancelled), nothing to bootstrap
  7 = bead is an epic (a container, not a single-PR unit), refuse to bootstrap

Exit codes (reap): 0 = ok (incl. a skipped/no-op reap: inspect the receipt's `skipped` field); 1 =
git error; 5 = checkpoint of uncommitted work failed before the destructive remove (the worktree was
left INTACT, failing toward preserving work; see reap_worktree).
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import _atomicio
import _locking
import _memory_paths
import _workspace
import agent_routes
import bootstrap_journal
import flow_launcher
import lease
import planning_attempt
import state
import ticket_frontmatter
from _runner import Runner
from _runner import default_runner as _default_runner
from _timeutil import utcnow_iso

# Gitignored dev config the autonomous tail needs but a fresh worktree won't have.
_DEFAULT_COPY = [
    ".env",
    ".envrc",
    ".claude",
    ".cursor",
    ".vscode",
    "mise.local.toml",
    ".mise.local.toml",
]


class _GitError(Exception):
    """git command failed. Exit code 1."""


class _ConfigError(Exception):
    """missing/invalid main workspace config. Exit code 2."""


class _DuplicateClaim(Exception):
    """a live sibling run already holds this ticket. Exit code 4."""


class _TerminalBead(Exception):
    """the bead is already closed/done/cancelled. Exit code 6."""


class _EpicBead(Exception):
    """the bead is an epic (a container, not a single-PR unit). Exit code 7."""


class _HitlBead(Exception):
    """the bead is marked hitl (human-in-the-loop) with no recorded decision. Exit code 8."""


def _git(args: list[str], cwd: Path, runner: Runner) -> str:
    result = runner(["git", *args], cwd)
    if result.returncode != 0:
        raise _GitError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _gitignored(files: list[str], cwd: Path, runner: Runner) -> list[str]:
    """Return the subset of `files` that git ignores in `cwd`.

    `git check-ignore` exits 0 when at least one path is ignored, 1 when none
    are, 128 on real error, so it cannot go through `_git` (which raises on any
    non-zero). check-ignore evaluates rules against the path string, so the
    files need not exist yet (planned files are usually about to be created).
    """
    if not files:
        return []
    result = runner(["git", "check-ignore", "--", *files], cwd)
    if result.returncode not in (0, 1):
        raise _GitError(f"git check-ignore failed: {result.stderr.strip()}")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _typo_planned(files: list[str], cwd: Path) -> list[str]:
    """Planned paths whose parent dir is also missing (a likely path typo).

    A new file in an existing dir is normal (TDD writes test files that do not
    exist yet). A planned path whose PARENT dir is also absent is the flow-kx17.1
    case: a stamped `.../scripts/references/...` where `references/` is a sibling
    of `scripts/`, so the whole parent chain is wrong.
    """
    return [f for f in files if not (cwd / f).exists() and not (cwd / f).parent.exists()]


def _mislocated_registry(files: list[str], cwd: Path) -> list[str]:
    """Planned stage-registry.toml paths that do not exist.

    The registry lives at the SKILL ROOT (plugins/flow/skills/flow/), never
    under scripts/. A `scripts/` prefix slips past _typo_planned because the
    parent dir exists, then reads as unowned drift at the dispatcher reconcile
    and aborts the run (flow-l014).
    """
    return [f for f in files if Path(f).name == "stage-registry.toml" and not (cwd / f).exists()]


def _porcelain_paths(main_root: Path, runner: Runner) -> dict[str, bool]:
    """Map each uncommitted path in `main_root` -> is_untracked.

    `git status --porcelain` lines are `XY <path>` (or `XY <orig> -> <path>` for a
    rename); `??` is untracked. Paths are repo-relative, matching planned_files'
    convention (`_gitignored` uses `cwd / f`). Renames take the post-`->` name.
    Quoted paths (core.quotePath on exotic filenames) are left as-is (a rare miss,
    not a fault, for this backstop).
    """
    result = runner(["git", "status", "--porcelain"], main_root)
    if result.returncode != 0:
        raise _GitError(f"git status --porcelain failed: {result.stderr.strip()}")
    out: dict[str, bool] = {}
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        code, rest = line[:2], line[3:]
        path = rest.split(" -> ", 1)[1] if " -> " in rest else rest
        out[path] = code == "??"
    return out


def _spilled_planned(
    planned_files: list[str], main_root: Path, runner: Runner
) -> list[tuple[str, bool]]:
    """planned_files that are uncommitted in the main checkout (the spill symptom).

    A harness without a plan-mode write-block (Cursor, Windsurf, a bare loop) can
    let the agent edit the plan's files on `main` BEFORE `create` runs. We cannot
    intercept that edit, but its fingerprint is exact: an uncommitted planned file
    on main. On Claude Code plan-mode keeps those files clean, and unrelated main
    WIP never overlaps planned_files, so both no-op here. Returns (path, untracked).
    """
    dirty = _porcelain_paths(main_root, runner)
    return [(f, dirty[f]) for f in planned_files if f in dirty]


def _relocate_spilled(
    spilled: list[tuple[str, bool]],
    main_root: Path,
    worktree: Path,
    runner: Runner,
    warnings: list[str],
) -> None:
    """Carry main-checkout spilled planned edits into the seeded worktree.

    Direct content copy, not `git stash`: copying the full working-tree content
    leaves no diff to conflict (the worktree takes the agent's version), and
    main is cleaned ONLY after the copy verifiably landed, so the work is never in
    neither place. Worst case it lives in both (harmless, recoverable). Best-effort:
    a relocation fault degrades to a warning, never fails the bootstrap.
    """
    carried: list[str] = []
    for rel, untracked in spilled:
        src, dst = main_root / rel, worktree / rel
        try:
            if not src.exists():
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            if not dst.is_file():
                continue
            # Clean main only now that the worktree copy is confirmed present.
            if untracked:
                src.unlink()
            else:
                res = runner(["git", "checkout", "--", rel], main_root)
                if res.returncode != 0:
                    # Worktree has the work; main wasn't reverted (e.g. a staged
                    # edit). No loss, so warn rather than fail.
                    warnings.append(
                        f"relocated {rel} into the worktree but could not revert it on "
                        f"main: {res.stderr.strip()}"
                    )
            carried.append(rel)
        except OSError as exc:
            warnings.append(f"could not relocate spilled edit {rel}: {exc}")
    if carried:
        warnings.append(
            "carried uncommitted edits to planned files from the main checkout into the "
            "worktree (and reverted them on main): "
            + ", ".join(carried)
            + " (a soft-gate harness let the agent edit before bootstrap; if some predate "
            "this run, that pre-existing work now lives in the worktree — review it)"
        )


def _copy_config(main_root: Path, worktree: Path) -> list[str]:
    """Copy gitignored dev config main->worktree. Returns the list copied."""
    copied: list[str] = []
    for rel in _DEFAULT_COPY:
        src = main_root / rel
        if not src.exists():
            continue
        dst = worktree / rel
        if src.is_dir():
            # skip the nested worktree pool (.claude/worktrees can be 10G+ of
            # other tickets' trees); the tail needs hooks/skills/settings, never
            # peer worktrees.
            shutil.copytree(
                src, dst, dirs_exist_ok=True, ignore=shutil.ignore_patterns("worktrees")
            )
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        copied.append(rel)
    return copied


def _ensure_flow_config(main_root: Path, worktree: Path, shared_memory: Path) -> None:
    """Ensure the worktree has .flow/.initialized + workspace.toml (copying from
    main when absent, the gitignored case), then bind layout v2 to
    `shared_memory` (main's resolved `.flow/memory` base, or the corresponding
    configured external base) through `.flow/runtime/memory-root`.

    The redirect lives in the sibling, NOT in workspace.toml: the tracked
    workspace.toml stays byte-identical to main's copy so a per-machine absolute
    path can never ride into a commit."""
    wt_flow = worktree / ".flow"
    wt_ws = wt_flow / "workspace.toml"
    if not wt_ws.exists():
        main_ws = main_root / ".flow" / "workspace.toml"
        if not main_ws.exists():
            raise _ConfigError(
                f"no workspace.toml at {main_ws}; run FLOW workspace setup in the main checkout"
            )
        wt_flow.mkdir(parents=True, exist_ok=True)
        shutil.copy2(main_ws, wt_ws)
    marker = wt_flow / ".initialized"
    if not marker.exists():
        main_marker = main_root / ".flow" / ".initialized"
        if main_marker.exists():
            shutil.copy2(main_marker, marker)
        else:
            marker.touch()
    # Stamp the currently executing Flow installation. Copying the main checkout's machine-local
    # skill path would make new worktrees stale.
    flow_launcher.install(
        worktree,
        skill_dir=Path(__file__).resolve().parent.parent,
        memory_base=shared_memory,
    )


def _shared_memory_base(main_root: Path) -> Path:
    """Migrate the main workspace first, then return its v2 memory base."""
    workspace_toml = main_root / ".flow" / "workspace.toml"
    if not workspace_toml.is_file():
        raise _ConfigError(f"no workspace.toml at {workspace_toml}; run Flow workspace setup first")
    flow_launcher.runtime_layout.ensure_layout(main_root)
    return _memory_paths.resolve_memory_base(main_root)


def _seed_state(worktree: Path, ticket: str, plan_text: str, head_sha: str) -> str:
    """Seed state.json: plan completed (with plan.out as its output_path); ticket
    left pending so the tail self-fetches it. Returns the run_id."""
    data = _workspace.load_workspace_toml(worktree)
    tracker = data.get("tracker")
    backend = tracker.get("backend") if isinstance(tracker, dict) else None
    pipeline = data.get("pipeline")
    stages = pipeline.get("stages") if isinstance(pipeline, dict) else None
    if not isinstance(backend, str) or not isinstance(stages, list):
        raise _ConfigError("worktree workspace.toml missing tracker.backend or pipeline.stages")

    ticket_dir = worktree / ".flow" / "runs" / ticket
    run_id = secrets.token_hex(8)
    state.init(ticket_dir, ticket, backend, [str(s) for s in stages], run_id=run_id)

    if "plan" in stages:
        state.begin_stage(ticket_dir, "plan", head_sha)
        plan_out = ticket_dir / "stages" / "plan.out"
        _atomicio.atomic_write_text(plan_out, plan_text)
        state.finish_stage(ticket_dir, "plan", "completed", head_sha, output_path=str(plan_out))
    return run_id


def _freeze_route_snapshot(
    worktree: Path,
    ticket: str,
    owner_harness: str | None,
    route_overrides: list[str] | None,
) -> dict[str, Any]:
    selected_owner = owner_harness or os.environ.get("FLOW_HARNESS") or "claude-code"
    route_path = worktree / ".flow" / "runs" / ticket / "route-snapshot.json"
    try:
        return agent_routes.snapshot(
            worktree,
            selected_owner,
            overrides=route_overrides or [],
            output_path=route_path,
        )
    except agent_routes.RouteError as exc:
        raise _ConfigError(f"cannot freeze agent routes: {exc}") from exc


def _seed_approval_receipt(
    worktree: Path,
    ticket: str,
    approval: planning_attempt.ApprovalReceipt,
) -> None:
    path = worktree / ".flow" / "runs" / ticket / "approval-receipt.json"
    _atomicio.atomic_write_text(
        path,
        json.dumps(approval.to_mapping(), indent=2, sort_keys=True) + "\n",
    )


def _approved_bootstrap_journal_path(main_root: Path, approval_digest: str) -> Path:
    """Keep planner-controlled attempt identifiers out of filesystem paths."""
    if len(approval_digest) != 64 or any(
        character not in "0123456789abcdef" for character in approval_digest
    ):
        raise _ConfigError("approved bootstrap journal requires a canonical approval digest")
    return main_root / ".flow" / "runtime" / "bootstrap" / f"approved-{approval_digest}.json"


def _verify_committed_approved_bootstrap(
    *,
    record: bootstrap_journal.JournalRecord,
    ticket: str,
    branch: str,
    worktree: Path,
    approval: planning_attempt.ApprovalReceipt,
) -> None:
    """Verify every durable artifact before treating a journal commit as recoverable."""
    if record.branch != branch or record.worktree != str(worktree) or not record.run_id:
        raise _ConfigError("committed approved bootstrap does not match the requested run location")
    if not worktree.is_dir():
        raise _ConfigError("committed approved bootstrap worktree is missing")
    ticket_dir = worktree / ".flow" / "runs" / ticket
    recovered_state, state_code = state.read(ticket_dir)
    if (
        state_code != 0
        or recovered_state is None
        or recovered_state.ticket != ticket
        or recovered_state.run_id != record.run_id
    ):
        raise _ConfigError("committed approved bootstrap state does not match its journal")
    try:
        seeded_approval = planning_attempt.load_approval_receipt(
            ticket_dir / "approval-receipt.json"
        )
        seeded_routes = agent_routes.load_snapshot(ticket_dir / "route-snapshot.json")
        approval.verify_plan_bytes((ticket_dir / "stages" / "plan.out").read_bytes())
    except (OSError, planning_attempt.AttemptError, agent_routes.RouteError) as exc:
        raise _ConfigError(f"committed approved bootstrap artifacts are invalid: {exc}") from exc
    if seeded_approval.digest != approval.digest:
        raise _ConfigError("committed approved bootstrap approval receipt does not match")
    if seeded_routes["digest"] != approval.route_digest:
        raise _ConfigError("committed approved bootstrap route snapshot does not match")


def _rollback_incomplete_approved_bootstrap(
    *,
    record: bootstrap_journal.JournalRecord,
    main_root: Path,
    run: Runner,
) -> None:
    """Prove an interrupted worktree and branch are gone before resetting the journal."""
    if not record.worktree or not record.branch:
        raise _ConfigError("incomplete approved bootstrap lacks its rollback coordinates")
    worktree = Path(record.worktree)
    removed = run(["git", "worktree", "remove", "--force", str(worktree)], main_root)
    if removed.returncode != 0 and worktree.exists():
        raise _ConfigError(
            "cannot roll back incomplete approved bootstrap worktree: "
            + (removed.stderr or removed.stdout).strip()
        )
    if worktree.exists():
        raise _ConfigError("incomplete approved bootstrap worktree still exists after rollback")
    deleted = run(["git", "branch", "-D", record.branch], main_root)
    if deleted.returncode != 0:
        exists = run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{record.branch}"],
            main_root,
        )
        if exists.returncode == 0:
            raise _ConfigError(
                "cannot roll back incomplete approved bootstrap branch: "
                + (deleted.stderr or deleted.stdout).strip()
            )
        if exists.returncode != 1:
            raise _ConfigError("cannot verify incomplete approved bootstrap branch removal")


def _e2e_enabled(main_root: Path) -> bool:
    """True when the workspace wires e2e to a real handler (not 'none').

    A 'none' handler short-circuits the stage before its reference doc loads, so
    no recipe is needed there. Only a disabled e2e (handler 'none') skips the
    recipe demand.
    """
    try:
        data = _workspace.load_workspace_toml(main_root)
    except _workspace.WorkspaceConfigError:
        return False
    pipeline = data.get("pipeline")
    handlers = pipeline.get("handlers") if isinstance(pipeline, dict) else None
    handler = handlers.get("e2e") if isinstance(handlers, dict) else None
    return isinstance(handler, str) and handler.strip().lower() != "none"


def _worktree_path(main_root: Path, branch: str, override: str | None) -> Path:
    """Mint the worktree path inside `.claude/worktrees/` (flow-gh1u).

    Claude Code >= 2.1.206 asks an interactive confirmation before EnterWorktree
    enters any worktree OUTSIDE `<repo>/.claude/worktrees/`, and the confirmation
    is not permission-mediated (no allow rule or headless bypass exists), so an
    unattended run seeded anywhere else blocks forever at the spec->do
    transition. Read sites glob both this base and the legacy
    `.flow/worktrees/` (`_evolve_common.WORKTREE_BASES`) so pre-relocation
    worktrees stay discoverable until reaped.
    """
    if override:
        return Path(override).expanduser().resolve()
    main = main_root.resolve()
    return main / ".claude" / "worktrees" / branch.replace("/", "-")


def _parse_worktree_list(porcelain: str) -> list[tuple[str, str | None]]:
    """Parse `git worktree list --porcelain` into (path, short_branch) pairs.

    Porcelain emits `worktree <path>`, then `HEAD <sha>`, then `branch
    refs/heads/<name>` (absent, or `detached`, for a detached worktree); entries
    are separated by blank lines. The short branch strips the `refs/heads/`
    prefix; a detached entry yields None.
    """
    pairs: list[tuple[str, str | None]] = []
    path: str | None = None
    branch: str | None = None
    for line in porcelain.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree ") :].strip()
            branch = None
        elif line.startswith("branch "):
            ref = line[len("branch ") :].strip()
            branch = ref.removeprefix("refs/heads/")
        elif not line.strip():
            if path is not None:
                pairs.append((path, branch))
            path = None
            branch = None
    if path is not None:
        pairs.append((path, branch))
    return pairs


def is_ticket_branch(short_branch: str, ticket: str) -> bool:
    """True when `short_branch` is this ticket's feature branch (exact or slugged).

    Accepts both the current `feat/` prefix and the legacy `feature/` so worktrees
    created before the rename still resolve.
    """
    return any(
        short_branch == f"{p}{ticket}" or short_branch.startswith(f"{p}{ticket}-")
        for p in ("feat/", "feature/")
    )


def _ticket_siblings(ticket: str, main_root: Path, runner: Runner) -> list[tuple[Path, str]]:
    """All registered worktrees whose checked-out branch belongs to `ticket`."""
    listing = _git(["worktree", "list", "--porcelain"], main_root, runner)
    return [
        (Path(path), sb)
        for path, sb in _parse_worktree_list(listing)
        if sb is not None and is_ticket_branch(sb, ticket)
    ]


def _claim_path(main_root: Path, ticket: str) -> Path:
    return main_root / ".flow" / "tickets" / f"{ticket}.claim"


def _assert_no_live_sibling(ticket: str, main_root: Path, runner: Runner) -> None:
    """Refuse (under the held bootstrap claim) when a sibling run is live.

    Per sibling worktree, the run's <wt>/.flow/runs/<ticket> is classified via
    lease.classify: a live or corrupt run.lock refuses; an expired lease is a
    dead sibling (reap owns its teardown) and proceeds. A free lease with a
    seeded NON-TERMINAL state.json (any stage pending/in_progress, which
    includes a failed-mid-pipeline run, since FLOW workspace repair can resume it) also
    refuses: that is the bootstrap→cmd_init window where the winner has seeded
    state but not yet acquired its run lease.
    """
    now = utcnow_iso()
    boot = lease.boot_id()
    host = lease.hostname()
    for wt_path, _sb in _ticket_siblings(ticket, main_root, runner):
        unstick = (
            f"resume/inspect it from the sibling (cd {wt_path} && "
            f"FLOW workspace repair {ticket}; "
            f"the run's state lives in that worktree, not this checkout), or tear down a "
            f"dead sibling via `flow_worktree.py reap --ticket {ticket}`"
        )
        ticket_dir = wt_path / ".flow" / "runs" / ticket
        info = lease.classify(ticket_dir, now, current_boot=boot, hostname=host)
        lease_state = info.get("state")
        if lease_state == "live":
            raise _DuplicateClaim(
                f"refusing to bootstrap {ticket}: sibling worktree {wt_path} holds a "
                f"live run lease (state: live); a second concurrent run would "
                f"double-ship the ticket. To unstick: {unstick}."
            )
        if lease_state == "corrupt":
            raise _DuplicateClaim(
                f"refusing to bootstrap {ticket}: sibling worktree {wt_path} has an "
                f"unparseable run.lock (state: corrupt, possibly live). "
                f"To unstick: {unstick}."
            )
        if lease_state == "free":
            ticket_state, _code = state.read(ticket_dir)
            if ticket_state is not None and any(
                rec.status in ("pending", "in_progress") for rec in ticket_state.stages.values()
            ):
                raise _DuplicateClaim(
                    f"refusing to bootstrap {ticket}: sibling worktree {wt_path} has a "
                    f"seeded non-terminal run (state.json with pending/in_progress "
                    f"stages, no run lease yet) — a just-bootstrapped or resumable "
                    f"run. To unstick: {unstick}."
                )
        # expired_reboot_clearable / expired_foreign / free+terminal-or-absent:
        # a dead sibling never blocks re-running the ticket.


def _detect_colliding_sibling(
    ticket: str, branch: str, worktree: Path, main_root: Path, runner: Runner
) -> Path | None:
    """A registered ticket sibling that would collide with `git worktree add -b
    <branch> <worktree>` (flow-vpg1): a DEAD sibling from a prior spend-limit
    death checked out on the exact same branch, or already registered at the
    exact derived path, makes `worktree add -b` fail outright (git refuses a
    duplicate branch checkout or a path already claimed by another worktree).
    `_assert_no_live_sibling` already refused a live/corrupt one; this only
    needs to spot a dead one worth auto-reaping. Returns the colliding
    worktree's path, or None when nothing collides.
    """
    worktree = worktree.resolve()
    for path, sb in _ticket_siblings(ticket, main_root, runner):
        if sb == branch or path.resolve() == worktree:
            return path
    return None


def _checkpoint_marker(ticket: str) -> str:
    """The exact WIP checkpoint commit subject for `ticket` (shared by the
    commit call and the crash-window recovery compare so they can never drift)."""
    return f"wip: flow checkpoint before reap ({ticket}) [skip ci]"


def _checkpoint_dirty_worktree(ticket: str, worktree: Path, run: Runner) -> dict[str, Any]:
    """Checkpoint uncommitted work in `worktree` as a WIP commit pushed to a
    `flow-rescue/<ticket>-<sha>` ref, before reap's destructive teardown.

    Runs inside `lease.classify_then`'s flock (capture is gated on non-live, so a concurrent acquire
    cannot go live mid-capture). `check=False` throughout (this is a Runner, not `_git`, which
    raises): every step's failure is reported back in the returned dict rather than raised, so the
    caller decides what "failed to checkpoint" means for the teardown.

    The pathspec excludes `.flow` (its `runs/` subtree is the only gitignored part;
    `tickets/<key>.md` is NOT, so a bare `git add -A` would re-commit it) and every `_copy_config`
    path (`.env` et al., also not gitignored in this repo). Without the exclude, a clean
    merged-orphan worktree would misfire as dirty (`.flow/tickets/<key>.md` always differs slightly
    from main's copy), and a dirty one could push a bootstrap-copied `.env` secret to a PUBLIC
    `flow-rescue/*` ref. `flow-rescue/*` is deliberately outside the `feat/`/`feature/`
    ticket-branch namespace (`is_ticket_branch`, `_evolve_common.FLOW_KEY_RE`, `is_inflight` all
    miss it), so it can never mark the ticket in-flight or block a fresh relaunch.

    DEVIATION from the literal maintainer decision text: the decision said push to
    `refs/heads/<run-branch>` verbatim. That target is unsafe here: `create_pr.py` pushes the run
    branch NON-force (`git push -u origin <branch>:refs/heads/<branch>`), and a pre-PR dead run
    never pushed its branch. Rescuing onto the exact run-branch name would leave a non-ancestor
    commit on that name, and a fresh relaunch reusing the same branch slug would then have ITS OWN
    create_pr push rejected non-fast-forward, regressing the drain recovery path this ticket exists
    to fix. The distinct `flow-rescue/<ticket>-<sha>` ref sidesteps that collision entirely.

    A clean tree can still be an ORPHANED checkpoint from a prior reap that died after this
    function's own `git commit` but before its rescue push landed (flow-81xn): HEAD reads clean
    either way, so the clean branch below probes HEAD's subject against `_checkpoint_marker` before
    declaring victory. An exact match (never a substring, since a feature commit merely mentioning
    the phrase must not misfire) re-attempts the same no-force push; anything else, including a
    squash-merged HEAD, is the ordinary clean/merged-orphan case.

    Returns {"status": "clean"} (nothing to capture, or a recovered ref already pushed), {"status":
    "captured", "rescue_branch", "sha"}, or {"status": "failed", "detail"} (leave the worktree
    untouched; fail toward preserving work).
    """
    pathspec = ["--", ".", ":(exclude).flow"] + [f":(exclude){p}" for p in _DEFAULT_COPY]

    status = run(["git", "status", "--porcelain", *pathspec], worktree)
    if status.returncode != 0:
        return {"status": "failed", "detail": f"git status failed: {status.stderr.strip()}"}
    if not status.stdout.strip():
        subject = run(["git", "log", "-1", "--format=%s", "HEAD"], worktree)
        if subject.returncode != 0:
            return {"status": "failed", "detail": f"git log failed: {subject.stderr.strip()}"}
        if subject.stdout.strip() != _checkpoint_marker(ticket):
            return {"status": "clean"}

        rev = run(["git", "rev-parse", "--short", "HEAD"], worktree)
        if rev.returncode != 0:
            return {"status": "failed", "detail": f"git rev-parse failed: {rev.stderr.strip()}"}
        sha = rev.stdout.strip()
        rescue_branch = f"flow-rescue/{ticket}-{sha}"

        # ls-remote is an optimization only, not the source of truth: a rc!=0
        # (transient network hiccup) still falls through to the idempotent
        # no-force push below rather than stranding the worktree.
        ls = run(["git", "ls-remote", "origin", f"refs/heads/{rescue_branch}"], worktree)
        if ls.returncode == 0 and ls.stdout.strip():
            return {"status": "clean"}

        push = run(["git", "push", "origin", f"HEAD:refs/heads/{rescue_branch}"], worktree)
        if push.returncode != 0:
            return {"status": "failed", "detail": f"git push failed: {push.stderr.strip()}"}
        return {"status": "captured", "rescue_branch": rescue_branch, "sha": sha}

    add = run(["git", "add", "-A", *pathspec], worktree)
    if add.returncode != 0:
        return {"status": "failed", "detail": f"git add failed: {add.stderr.strip()}"}

    commit = run(
        ["git", "commit", "--no-verify", "-m", _checkpoint_marker(ticket)],
        worktree,
    )
    if commit.returncode != 0:
        return {"status": "failed", "detail": f"git commit failed: {commit.stderr.strip()}"}

    rev = run(["git", "rev-parse", "--short", "HEAD"], worktree)
    if rev.returncode != 0:
        return {"status": "failed", "detail": f"git rev-parse failed: {rev.stderr.strip()}"}
    sha = rev.stdout.strip()
    rescue_branch = f"flow-rescue/{ticket}-{sha}"

    push = run(["git", "push", "origin", f"HEAD:refs/heads/{rescue_branch}"], worktree)
    if push.returncode != 0:
        return {"status": "failed", "detail": f"git push failed: {push.stderr.strip()}"}

    return {"status": "captured", "rescue_branch": rescue_branch, "sha": sha}


def _checkpoint_then_remove(
    ticket: str,
    worktree: Path,
    run: Runner,
    main_root: Path,
    *,
    expected_tip: str | None = None,
    before_remove: Callable[[Path], object] | None = None,
) -> dict[str, Any]:
    """Checkpoint, then remove `worktree` (the `lease.classify_then` teardown callback).

    On a failed checkpoint the worktree is left untouched (`removed=False`,
    fail-toward-preserving-work); a clean or captured checkpoint proceeds to `git worktree remove
    --force`. Runs a git subprocess only (no lease re-entry), matching classify_then's
    non-reentrant-flock contract.
    """
    if expected_tip is not None:
        current = run(["git", "rev-parse", "HEAD"], worktree)
        if current.returncode != 0:
            return {
                "removed": False,
                "remove_error": None,
                "checkpoint": {"status": "not_run"},
                "skipped": f"current tip probe failed: {current.stderr.strip()}",
            }
        current_tip = current.stdout.strip()
        if current_tip != expected_tip:
            return {
                "removed": False,
                "remove_error": None,
                "checkpoint": {"status": "not_run"},
                "skipped": f"worktree tip changed from {expected_tip} to {current_tip}",
            }

    checkpoint = _checkpoint_dirty_worktree(ticket, worktree, run)
    if checkpoint["status"] == "failed":
        return {"removed": False, "remove_error": None, "checkpoint": checkpoint}
    before_remove_result: object | None = None
    before_remove_error: str | None = None
    if before_remove is not None:
        try:
            before_remove_result = before_remove(worktree)
        except Exception as exc:
            before_remove_error = str(exc)
    result = run(["git", "worktree", "remove", "--force", str(worktree)], main_root)
    return {
        "removed": result.returncode == 0,
        "remove_error": result.stderr.strip() if result.returncode != 0 else None,
        "checkpoint": checkpoint,
        "before_remove_result": before_remove_result,
        "before_remove_error": before_remove_error,
    }


def _revision_reap_blocker(
    ticket_dir: Path, now: str, *, current_boot: str, hostname: str
) -> str | None:
    """Return why a base worktree's revision subtree cannot be reaped."""
    revisions = ticket_dir / "revisions"
    if not revisions.is_dir():
        return None
    for revision_dir in sorted(path for path in revisions.iterdir() if path.is_dir()):
        revision_state, state_code = state.read(revision_dir)
        if revision_state is None and state_code != 0:
            return f"revision state unreadable at {revision_dir}"
        if revision_state is not None and any(
            record.status in ("pending", "in_progress") for record in revision_state.stages.values()
        ):
            return f"revision run non-terminal at {revision_dir}"
        if not lease.run_lock_path(revision_dir).exists():
            continue
        info = lease.classify(
            revision_dir,
            now,
            current_boot=current_boot,
            hostname=hostname,
        )
        if info.get("state") in ("live", "corrupt"):
            return f"revision lease {info['state']} at {revision_dir}"
    return None


def reap_worktree(  # noqa: C901
    *,
    ticket: str,
    main_root: Path,
    branch: str | None = None,
    runner: Runner | None = None,
    expected_tip: str | None = None,
    before_remove: Callable[[Path], object] | None = None,
) -> dict[str, Any]:
    """Tear down the local worktree + branch left behind after a squash-merge.

    The squash-merge (`gh pr merge --squash`) deletes no branch (gh's branch-delete is skipped), and
    the separate `git push origin --delete <branch>` touches only the remote ref; so the local
    `feat/<key>-*` branch and its still-registered worktree survive regardless (the worktree holds
    that branch checked out, which also blocks any local-branch delete). This reaps them, gated on
    the per-ticket lease: when the worktree's run is still live (the bg session is, typically, in
    reflect) NOTHING is touched and a later pass reaps it.

    An explicit `branch` must belong to `ticket`: the lease gate classifies
    `<worktree>/.flow/runs/<ticket>`, so a mismatched pair would classify an ABSENT run dir as free
    and force-remove another ticket's live worktree. A mismatch refuses via the receipt, touching
    nothing.

    FALLIBLE (flow-vpg1): before the destructive remove, `_checkpoint_then_remove` checkpoints any
    uncommitted work as a WIP commit pushed to a `flow-rescue/*` ref (see
    `_checkpoint_dirty_worktree`). When that capture fails, the worktree is left INTACT
    (`worktree_removed=False`, `checkpoint_failed=True`) rather than destroyed (fail toward
    preserving work); the CLI (`_run_reap`) surfaces this as a non-zero exit so an `&&`-gated caller
    (the drain §Recover recipes) self-heals: the bead stays in_progress and re-strands next turn
    instead of silently losing the work. A successful capture ("clean" or "captured") still removes
    the worktree as before; `receipt["checkpoint"]` names the rescue branch only when work was
    actually captured.

    Idempotent: a second call (worktree + branch already gone) is a clean no-op.
    """
    run = runner or _default_runner()
    main_root = main_root.expanduser().resolve()

    if branch is not None and not is_ticket_branch(branch, ticket):
        return {
            "ticket": ticket,
            "branch": branch,
            "worktree": None,
            "worktree_removed": False,
            "branch_deleted": False,
            "skipped": f"branch {branch} does not belong to ticket {ticket}; refusing to reap",
        }

    listing = _git(["worktree", "list", "--porcelain"], main_root, run)
    pairs = _parse_worktree_list(listing)

    target_path: Path | None = None
    resolved_branch = branch
    for path, sb in pairs:
        if sb is None:
            continue
        if branch is not None:
            if sb == branch:
                target_path = Path(path)
                break
        elif is_ticket_branch(sb, ticket):
            target_path = Path(path)
            resolved_branch = sb
            break

    receipt: dict[str, object] = {
        "ticket": ticket,
        "branch": resolved_branch,
        "worktree": str(target_path) if target_path is not None else None,
        "worktree_removed": False,
        "branch_deleted": False,
        "skipped": None,
    }

    if target_path is not None:
        ticket_dir = target_path / ".flow" / "runs" / ticket
        now, boot, host = utcnow_iso(), lease.boot_id(), lease.hostname()

        # The revision claim blocks a new revision from opening while base and revision state is
        # checked. The base lease flock remains held through the expected-tip check, checkpoint,
        # close observation, and removal.
        with _locking.flock_blocking(ticket_dir / "revise.claim"):
            blocker = _revision_reap_blocker(
                ticket_dir,
                now,
                current_boot=boot,
                hostname=host,
            )
            if blocker is not None:
                receipt["skipped"] = blocker
                return receipt
            outcome = lease.classify_then(
                ticket_dir,
                now,
                lambda: _checkpoint_then_remove(
                    ticket,
                    target_path,
                    run,
                    main_root,
                    expected_tip=expected_tip,
                    before_remove=before_remove,
                ),
                current_boot=boot,
                hostname=host,
            )
        if not outcome["torn_down"]:
            if outcome["state"] == "live":
                receipt["skipped"] = "lease live (run still in progress)"
            else:
                receipt["skipped"] = "lease corrupt (run.lock unparseable; possibly live)"
            return receipt
        result = cast(dict[str, Any], outcome["result"])
        if result.get("skipped"):
            receipt["skipped"] = result["skipped"]
            return receipt
        checkpoint = result["checkpoint"]
        if checkpoint["status"] == "failed":
            receipt["checkpoint_failed"] = True
            receipt["skipped"] = f"checkpoint failed: {checkpoint['detail']}"
            return receipt
        if not result["removed"]:
            receipt["skipped"] = f"worktree remove failed: {result['remove_error']}"
            return receipt
        receipt["worktree_removed"] = True
        if before_remove is not None:
            receipt["before_remove_result"] = result.get("before_remove_result")
            receipt["before_remove_error"] = result.get("before_remove_error")
        if checkpoint["status"] == "captured":
            receipt["checkpoint"] = {
                "rescue_branch": checkpoint["rescue_branch"],
                "sha": checkpoint["sha"],
            }

    if target_path is None and resolved_branch and expected_tip is not None:
        current = run(["git", "rev-parse", resolved_branch], main_root)
        if current.returncode != 0:
            receipt["skipped"] = f"branch tip probe failed: {current.stderr.strip()}"
            return receipt
        current_tip = current.stdout.strip()
        if current_tip != expected_tip:
            receipt["skipped"] = (
                f"branch tip changed from {expected_tip} to {current_tip} after worktree removal"
            )
            return receipt

    if resolved_branch:
        result = run(["git", "branch", "-D", resolved_branch], main_root)
        receipt["branch_deleted"] = result.returncode == 0

    return receipt


def locate_or_reseed(
    *,
    ticket: str,
    branch: str,
    main_root: Path,
    runner: Runner | None = None,
) -> dict[str, Any]:
    """Locate the ticket's worktree, or re-materialize it from the PR branch (flow-kx17.2).

    A delivery revision needs the worktree the original run left behind. The
    norm (PR-open ⇒ worktree-present) is a LOCATE: a registered worktree on a
    `feat/<ticket>*` branch is returned as-is (reseeded:false). When the worktree
    was externally reaped, RESEED: fetch the existing remote branch and `git worktree
    add <path> <branch>` (checkout, NOT -b), then re-copy gitignored config + redirect
    memory + mise trust via the same helpers bootstrap uses (reseeded:true).
    """
    run = runner or _default_runner()
    main_root = main_root.expanduser().resolve()

    siblings = _ticket_siblings(ticket, main_root, run)
    if siblings:
        _ensure_flow_config(main_root, siblings[0][0], _shared_memory_base(main_root))
        return {"worktree": str(siblings[0][0]), "reseeded": False}

    worktree = _worktree_path(main_root, branch, None)
    _git(["fetch", "origin", branch], main_root, run)
    _git(["worktree", "add", str(worktree), branch], main_root, run)
    try:
        _copy_config(main_root, worktree)
        _ensure_flow_config(main_root, worktree, _shared_memory_base(main_root))
        if (worktree / "mise.toml").exists() or (worktree / ".mise.toml").exists():
            run(["mise", "trust"], worktree)
    except Exception:
        # This path checks out an existing PR branch, so remove only the new worktree registration.
        # The branch predates this operation and is kept.
        run(["git", "worktree", "remove", "--force", str(worktree)], main_root)
        raise
    return {"worktree": str(worktree), "reseeded": True}


_DEFAULT_BASE = "@default"


def _default_branch(main_root: Path, runner: Runner, *, strict: bool) -> str | None:
    """The freshly-fetched remote default branch ref (`origin/<HEAD>`), or None.

    Fetches origin first so the ref is current, then reads
    `refs/remotes/origin/HEAD`, populating it via `remote set-head` when unset.
    Returns None when no remote default resolves (no `origin` remote at all).

    `strict` hard-fails on a fetch error. The autonomous `@default` contract is
    "branch off a genuinely fresh remote default or do not start" (a clean abort
    leaves no orphan; the next drain retries). Non-strict swallows the fetch
    error so an offline/origin-less interactive run still bootstraps off its
    local base.
    """
    if strict:
        _git(["fetch", "--quiet", "origin"], main_root, runner)
    else:
        runner(["git", "fetch", "--quiet", "origin"], main_root)
    head = runner(
        ["git", "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"], main_root
    )
    name = head.stdout.strip() if head.returncode == 0 else ""
    if not name:
        # origin/HEAD not populated locally; ask the remote, then retry.
        runner(["git", "remote", "set-head", "origin", "--auto"], main_root)
        head = runner(
            ["git", "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"], main_root
        )
        name = head.stdout.strip() if head.returncode == 0 else ""
    return name or None


def _resolve_base(base: str, main_root: Path, runner: Runner) -> str:
    """Resolve the worktree base ref, always off a freshly-fetched origin.

    Every invocation fetches origin (best-effort), so a run never branches off a stale ref.
    `@default`, the local default branch, and a detached HEAD all resolve to the remote default
    (`origin/<HEAD>`): launching a Flow target from a lagging local `main` is the common stale-base
    error (a PR polluted with already-merged commits), so it branches off `origin/main` instead of
    the local tip. A feature branch passes through unchanged, an interactive run stacked on a
    parent `feat/` branch keeps stacking (the parent may be local-only), but its remote-tracking
    refs are now fresh. The interactive fetch is best-effort (an offline/origin-less repo still
    bootstraps off its local base); the autonomous `@default` fetch hard-fails instead, since that
    contract is a guaranteed-fresh remote base (a clean abort, not a stale run).
    """
    strict = base == _DEFAULT_BASE
    default = _default_branch(main_root, runner, strict=strict)
    if base == _DEFAULT_BASE:
        return default or "origin/main"
    if default is None:
        return base
    default_name = default.split("/", 1)[1] if "/" in default else default
    if base.strip() in ("", "HEAD", default_name):
        return default
    return base


def _enforce_autonomy_floors(
    *,
    ticket: str,
    base: str,
    auto: bool,
    planned_files: list[str] | None,
    main_root: Path,
) -> None:
    """Code-enforced autonomy floors at the shared bootstrap chokepoint.

    An autonomous run, signaled internally by ``auto=True`` OR a `@default` base (the load-bearing
    base; the drain launches from the main checkout, so `--base` alone is not a sufficient signal,
    hence both), must clear two refusals before `git worktree add`, both read off a SINGLE
    `triage.decided` probe (one `bd show`). Beads-only: `triage.decided` reads a
    `DECISION:`/`TRIAGE-DECISION:` comment, a beads-native seam (a non-beads tracker has no such
    record, so gating it would permanently block). A refusal here leaves no orphan.

    HITL floor (flow-blh2, exit 8 via `_HitlBead`): a bead marked `hitl` (human-in-the-loop,
    resolves only through a live exchange) with no recorded decision defers, never bootstraps
    unattended. Checked FIRST and NOT lifted by `[evolve] adjudicate_hot` (that flag lifts only the
    hot half): a decision-bound bead needs a person regardless of the maintainer's hot-ship
    preference. A recorded decision means the human already weighed in, so it clears the floor.

    HOT floor (flow-aen, exit 2 via `_ConfigError`): a hot change (a guard/safety file, or a
    `hot`-labelled bead) with no maintainer decision on file may NOT self-ship. This lives at the
    single shared bootstrap every self-approve path funnels through, so it holds for the clean
    >=90% path too. delivery-plan.md step 5 only carried the floor in the adjudication/decided
    sub-branches, so a clean re-plan could slip a hot change past it. The `[evolve] adjudicate_hot`
    flag (default off) lifts this floor for a maintainer self-target workspace. The floor runs even
    with an EMPTY planned set: the `hot` label is independent evidence of hotness (`triage.decided`
    reads it), so omitting `--planned-files` must not disable the label half of the floor.
    """
    if not (auto or base.strip() == "@default"):
        return
    import triage

    config, _code = triage._resolve_config(main_root)
    if config is None or config.get("backend") != "beads":
        return
    # No runner threaded: BeadsAdapter (via decided) needs the keyword-only
    # KwRunner protocol, not flow_worktree's positional Runner. Passing `run`
    # here throws inside decided's try/except and silently returns block-by-default,
    # which would make the gate unable to read a recorded decision (the triage
    # bypass would never clear). Let decided build its own kw_default_runner.
    # Computed before the adjudicate_hot early-return so the hitl half always reads it.
    probe = triage.decided(config, ticket, planned_files or [])

    if probe.get("hitl") and not probe.get("decided"):
        raise _HitlBead(
            f"autonomous run refuses to bootstrap {ticket}: it is marked hitl "
            "(human-in-the-loop) and resolves only through a live exchange, with no "
            "recorded DECISION:/TRIAGE-DECISION: comment. Run it interactively WITHOUT "
            f'--unattended, or FLOW {ticket} --request "<answer>" to record the decision and '
            "clear the label, then re-run."
        )

    # adjudicate_hot lifts the HOT floor for this (maintainer self-target) workspace: the advisor's
    # proceed ruling stands for hot changes too, gated by the merge-time guard-property review + CI
    # instead of this pre-bootstrap refusal. It does not lift the hitl floor above.
    if triage.adjudicate_hot(main_root):
        return
    if probe.get("is_hot") and not probe.get("decided"):
        tripped = ", ".join(planned_files) if planned_files else "the bead's 'hot' label"
        raise _ConfigError(
            "autonomous run refuses to bootstrap a HOT change with no recorded "
            "decision: " + tripped + " trips the is_hot_change "
            "floor (a guard/safety file or a 'hot'-labelled bead) and carries no "
            "DECISION:/TRIAGE-DECISION: comment. A hot change never self-approves "
            f'unattended. Answer it (FLOW {ticket} --request "<answer>") then re-run, '
            "or run attended so a human gates it at plan approval."
        )


_TERMINAL_STATES = frozenset({"done", "cancelled"})


def _refuse_terminal_bead(*, ticket: str, main_root: Path) -> None:
    """Refuse (exit 6) to bootstrap a bead whose authoritative status is terminal.

    Witnessed (flow-d6gq): an unattended Flow target bootstrapped a CLOSED bead and
    ran it to implement. The spec `get` ran pre-worktree from the main checkout and
    reflected the bead as open at that instant; the close (its parent epic's merge)
    landed during the run. This re-reads the bead's authoritative status at the
    bootstrap chokepoint (seconds-to-minutes after the spec fetch, so it catches a
    bead that closed during planning), and refuses before `git worktree add` (a
    refusal leaves no orphan). Tracker-agnostic and unconditional (interactive +
    unattended mode): bootstrapping a done/cancelled bead is wrong either way.

    Fail-open is narrow: a genuine read *exception* (tracker construction / subprocess
    failure) proceeds, so a flaky tracker read never strands a legitimate run. A read
    that SUCCEEDS but yields no usable status is NOT fail-open. It refuses, since an
    affirmatively-incoherent tracker answer is suspicious, not transient. The do-loop
    ticket stage re-checks downstream (stage-ticket.md step 3b) and is the backstop for
    a close that lands after this gate.
    """
    import triage
    from tracker import make_tracker

    config, _code = triage._resolve_config(main_root)
    if config is None:
        return
    try:
        st = make_tracker(config).state(ticket)
    except Exception:
        # Read mechanism failed (network / subprocess / construction). Fail-open:
        # do not block a legitimate run on a transient tracker read failure.
        return
    normalized = st.get("normalized") if isinstance(st, dict) else None
    if normalized in _TERMINAL_STATES:
        raise _TerminalBead(
            f"refusing to bootstrap {ticket}: the bead's authoritative status is "
            f"terminal (normalized={normalized!r}). It is already closed/done — there "
            "is nothing to implement. If this is wrong, reopen the bead (status->open) "
            "and re-run."
        )
    if not normalized:
        raise _TerminalBead(
            f"refusing to bootstrap {ticket}: could not confirm the bead is live "
            f"(empty/indeterminate status read: {st!r}). Refusing rather than "
            "bootstrapping on an unconfirmed status. Re-run once the tracker is healthy."
        )


def _refuse_epic_bead(*, ticket: str, main_root: Path) -> None:
    """Refuse (exit 7) to bootstrap an epic (a container, not a single-PR unit).

    Witnessed (flow-jvxj, parent flow-8by2): an unattended epic target reached this
    chokepoint on an epic bead. `evolve_select.py` filters `issue_type != "epic"`
    unconditionally so drain never launches one, but a manual or misrouted
    unattended epic delivery had no structural floor, and bootstrapping an epic
    cram-ships fragments of an unaccepted empire as a single PR (the ouroboros
    command-maintain.md §epic names). This mirrors the select-side filter at the
    bootstrap chokepoint. Tracker-agnostic ("epic"/"Epic") and unconditional
    (attended and unattended): an epic is decomposed before delivery, not
    implemented directly, either way.

    Fail-open matches `_refuse_terminal_bead`: a read *exception* proceeds so a
    flaky tracker never strands a real run; a successful read of a non-epic type
    proceeds normally.
    """
    import triage
    from tracker import make_tracker

    config, _code = triage._resolve_config(main_root)
    if config is None:
        return
    try:
        ticket_type = make_tracker(config).get(ticket).get("type", "")
    except Exception:
        # Read mechanism failed (network / subprocess / construction). Fail-open.
        return
    if str(ticket_type).strip().lower() == "epic":
        raise _EpicBead(
            f"refusing to bootstrap {ticket}: it is an EPIC (a container, not a "
            "single-PR unit). An epic is decomposed into child beads via the expand "
            "recipe (command-maintain.md §E), then each child runs at its own spec gate — "
            "bootstrapping the epic directly would cram-ship fragments of an "
            "unaccepted epic as one PR. Expand it, or run a child key instead."
        )


def _lane_for_bead(*, ticket: str, main_root: Path) -> str:
    """Resolve the verification lane (express|light|full) from the bead's tier labels.

    Same labels evolve_select reads for model selection (tier:trivial -> sonnet) now
    also pick how much verification the run does (tier_policy.lane_for). Fail-open to
    "full" matches the terminal/epic reads: a flaky tracker never silently downshifts a
    run's gating. A non-beads tracker (no tier labels) resolves to "full" too.
    """
    import tier_policy
    import triage
    from tracker import make_tracker

    config, _code = triage._resolve_config(main_root)
    if config is None:
        return "full"
    try:
        labels = make_tracker(config).get(ticket).get("labels", [])
    except Exception:
        return "full"
    return tier_policy.lane_for(labels)


def _run_is_hot(*, ticket: str, planned_files: list[str] | None, main_root: Path) -> bool:
    """A change is hot if a guard/safety file is in planned_files, or the bead carries
    a `hot` label (mirrors triage.decided's is_hot). Fail-safe: a tracker read failure
    reads as hot so the lane clamps to full, matching _lane_for_bead's fail-open."""
    import triage
    from tracker import make_tracker

    if triage.is_hot_change(planned_files or []):
        return True
    config, _code = triage._resolve_config(main_root)
    if config is None:
        return False
    try:
        return "hot" in (make_tracker(config).get(ticket).get("labels") or [])
    except Exception:
        return True


def _effective_lane(
    *, explicit: str | None, ticket: str, planned_files: list[str] | None, main_root: Path
) -> str | None:
    """Lane to stamp at bootstrap; express/light only (`full` -> None, the stages'
    absent-field default, so a normal run's frontmatter is unchanged).

    An explicit `--lane` (interactive override) wins over the bead's tier labels, but a
    hot change clamps to `full` either way: the label-derived path gets the hot-LABEL
    clamp inside _lane_for_bead; this re-checks the guard-file planned set (both paths)
    and the hot label (the explicit path bypasses _lane_for_bead)."""
    base = explicit if explicit is not None else _lane_for_bead(ticket=ticket, main_root=main_root)
    if base in ("express", "light") and _run_is_hot(
        ticket=ticket, planned_files=planned_files, main_root=main_root
    ):
        base = "full"
    return base if base in ("express", "light") else None


def _refuse_invalid_covers(*, ticket: str, covers: list[str], main_root: Path) -> None:
    """Each cover must be a distinct, live, non-epic ticket (the lead's floors, looped).

    The epic/terminal reads fail-open (a flaky tracker never strands the run); the
    self-check (a cover that is the lead itself) is deterministic and always refuses.
    """
    for cover in covers:
        if cover == ticket:
            raise _ConfigError(
                f"cover {cover!r} is the lead ticket itself; covers must be distinct siblings"
            )
        _refuse_terminal_bead(ticket=cover, main_root=main_root)
        _refuse_epic_bead(ticket=cover, main_root=main_root)


def _stamp_run_frontmatter(
    worktree: Path,
    ticket: str,
    *,
    planned_files: list[str] | None,
    covers: list[str],
    commit_type: str | None,
    commit_summary: str | None,
    e2e_recipe: str | None,
    unattended: bool,
    lane: str | None = None,
) -> None:
    """Seed the run frontmatter the unattended tail reads so it never pauses to ask.

    planned_files -> records_diff_baseline pre-hook; covers -> the delivery fan-out
    (transition / PR comment / reflect); commit_type/commit_summary -> the commit
    stage; e2e_recipe -> the e2e lint gate + recipe executor; unattended -> the sole
    signal review_brief.freshness() cross-checks against a canonical skip receipt
    (stamped unconditionally, unlike the other optional fields below, so a canonical
    skip can never fail open on an absent key); lane -> the verification depth the
    spec/implement/reflect stages read (tier_policy). List fields go in as TOML-array
    literals so ticket_frontmatter coerces them back to lists.
    """
    fm_updates: dict[str, str] = {"unattended": "true" if unattended else "false"}
    if planned_files:
        fm_updates["planned_files"] = "[" + ", ".join(f'"{f}"' for f in planned_files) + "]"
    if covers:
        fm_updates["covers"] = "[" + ", ".join(f'"{c}"' for c in covers) + "]"
    if commit_type:
        fm_updates["commit_type"] = commit_type
    if commit_summary:
        fm_updates["commit_summary"] = commit_summary
    if e2e_recipe:
        fm_updates["e2e_recipe"] = e2e_recipe
    if lane:
        fm_updates["lane"] = lane
    ticket_frontmatter.update(worktree / ".flow" / "tickets" / f"{ticket}.md", fm_updates)


def _refuse_offcontract_branch(*, ticket: str, branch: str) -> None:
    """Keep every downstream matcher on the shared `feat/<key>-<slug>` branch contract.

    The matchers include `is_ticket_branch`, the pool prefixes in `_evolve_common`, in-flight refs,
    reap eligibility, janitor PR joins, and `branch_ticket` parsing. A run that minted
    `fix/<key>-...` produced a worktree invisible to reap and drain (witnessed 2026-07-09). Refuse
    the deviation at the one mint site instead of widening every parser.
    """
    if not branch.startswith(f"feat/{ticket}"):
        raise _ConfigError(
            f"branch {branch!r} violates the feat/<ticket>-<slug> contract "
            f"(expected a 'feat/{ticket}' prefix); the reap/drain/select "
            f"machinery only tracks feat/ branches"
        )


def bootstrap(  # noqa: C901
    *,
    ticket: str,
    plan_from: Path,
    base: str,
    branch: str,
    main_root: Path,
    worktree_override: str | None = None,
    planned_files: list[str] | None = None,
    covers: list[str] | None = None,
    commit_type: str | None = None,
    commit_summary: str | None = None,
    e2e_recipe: str | None = None,
    lane: str | None = None,
    mise_trust: bool = True,
    auto: bool = False,
    recover_spill: bool = False,
    owner_harness: str | None = None,
    route_overrides: list[str] | None = None,
    approval_receipt: Path | None = None,
    runner: Runner | None = None,
) -> dict[str, Any]:
    run = runner or _default_runner()
    main_root = main_root.expanduser().resolve()

    _refuse_offcontract_branch(ticket=ticket, branch=branch)

    # e2e is default-on; unless a workspace explicitly disabled it, the approved
    # plan must declare what the e2e stage runs. Refuse here, while the user is
    # still present at the spec gate, rather than let the unattended tail block
    # at the e2e lint gate.
    if _e2e_enabled(main_root) and not (e2e_recipe and e2e_recipe.strip()):
        raise _ConfigError(
            "e2e handler is enabled in workspace.toml; pass --e2e-recipe "
            "(the approved plan must declare the e2e recipe/fixture, or 'skip: <reason>')"
        )

    _refuse_terminal_bead(ticket=ticket, main_root=main_root)

    _refuse_epic_bead(ticket=ticket, main_root=main_root)

    # covers: sibling tickets this one run co-delivers. They ride the lead's
    # identity (lease / state / branch / memory stay lead-keyed); only the
    # delivery steps fan out over them.
    covers = [c for c in (covers or []) if c.strip()]
    _refuse_invalid_covers(ticket=ticket, covers=covers, main_root=main_root)

    _enforce_autonomy_floors(
        ticket=ticket,
        base=base,
        auto=auto,
        planned_files=planned_files,
        main_root=main_root,
    )

    # Matches _enforce_autonomy_floors' own signal exactly, computed off the caller's raw
    # base before _resolve_base or an approval receipt replaces it below: either mutation would
    # lose the `@default` drain-launch signal review_brief.freshness() later authorizes against.
    unattended = auto or base.strip() == "@default"

    plan_text = plan_from.read_text(encoding="utf-8")
    approval: planning_attempt.ApprovalReceipt | None = None
    journal: bootstrap_journal.BootstrapJournal | None = None
    journal_recovery: bootstrap_journal.JournalRecord | None = None
    worktree = _worktree_path(main_root, branch, worktree_override)
    if approval_receipt is not None:
        try:
            approval = planning_attempt.load_approval_receipt(approval_receipt)
            approval.verify_plan_bytes(plan_from.read_bytes())
            selected_owner = owner_harness or os.environ.get("FLOW_HARNESS") or "claude-code"
            config_result = run(
                ["git", "show", f"{approval.approved_base_sha}:.flow/workspace.toml"],
                main_root,
            )
            if config_result.returncode != 0:
                raise _GitError(
                    "git show of approved workspace configuration failed: "
                    + config_result.stderr.strip()
                )
            fetched_config = config_result.stdout.encode()
            pre_gate_routes = agent_routes.snapshot_config(
                fetched_config,
                selected_owner,
                overrides=route_overrides or [],
            )
        except (planning_attempt.AttemptError, agent_routes.RouteError, _GitError) as exc:
            raise _ConfigError(f"invalid approval receipt: {exc}") from exc
        if pre_gate_routes["digest"] != approval.route_digest:
            raise _ConfigError(
                "current route snapshot does not match the exact native-gate receipt"
            )
        base = approval.approved_base_sha
        journal = bootstrap_journal.BootstrapJournal(
            _approved_bootstrap_journal_path(main_root, approval.digest)
        )
        try:
            prepared = journal.prepare(ticket=ticket, approval=approval.to_mapping())
            if prepared.phase == "committed":
                _verify_committed_approved_bootstrap(
                    record=prepared,
                    ticket=ticket,
                    branch=branch,
                    worktree=worktree,
                    approval=approval,
                )
                return {
                    "ticket": ticket,
                    "branch": prepared.branch,
                    "worktree": prepared.worktree,
                    "run_id": prepared.run_id,
                    "copied": [],
                    "warnings": ["recovered committed approved bootstrap"],
                    "route_digest": approval.route_digest,
                    "approval_digest": approval.digest,
                }
            if prepared.phase != "prepared":
                journal_recovery = prepared
        except bootstrap_journal.JournalError as exc:
            raise _ConfigError(f"cannot prepare approved bootstrap journal: {exc}") from exc
    warnings: list[str] = []

    # Spill recovery is an explicit operator action after the caller confirms that these paths were
    # created by this Flow attempt and did not predate it. Dirty state alone cannot distinguish an
    # agent spill from user-owned WIP. Relocation happens only after the run is fully seeded, so a
    # refusal or crash never removes work from main first. See references/harness.md.
    spilled = (
        _spilled_planned(planned_files, main_root, run) if recover_spill and planned_files else []
    )

    # The fetch inside _resolve_base stays OUTSIDE the claim, so a second launch
    # never blocks on the winner's network round-trip; the claim window below is
    # local-only ops, seconds.
    if approval is None:
        base = _resolve_base(base, main_root, run)

    # Canonical per-ticket bootstrap claim: two simultaneous bootstraps of the
    # same ticket serialize here; the loser then sees the winner's seeded state
    # via _assert_no_live_sibling and refuses (exit 4) before any git mutation.
    # Held across worktree-add → state-seed → frontmatter stamp, so a sibling's
    # check never observes a half-seeded run.
    with _locking.flock_blocking(_claim_path(main_root, ticket)):
        if journal is not None and journal_recovery is not None:
            if journal_recovery.worktree != str(worktree) or journal_recovery.branch != branch:
                raise _ConfigError(
                    "incomplete approved bootstrap journal names a different worktree or branch"
                )
            _rollback_incomplete_approved_bootstrap(
                record=journal_recovery,
                main_root=main_root,
                run=run,
            )
            try:
                journal.restart_after_rollback()
            except bootstrap_journal.JournalError as exc:
                raise _ConfigError(f"cannot restart approved bootstrap: {exc}") from exc
        _assert_no_live_sibling(ticket, main_root, run)

        # flow-vpg1: a DEAD sibling (already ruled out live/corrupt above) on the exact colliding
        # branch/path would make `worktree add -b` below fail outright (the
        # manual-relaunch-after-spend-limit-death case). Auto-reap it first (checkpoint-then-remove,
        # on the sibling's OWN distinct run.lock.lock, never this claim flock, so no deadlock). A
        # checkpoint failure refuses rather than destroy the sibling's uncommitted work; a lease
        # that goes live under the reap's own flock (TOCTOU) refuses the same as a live sibling
        # above.
        colliding = _detect_colliding_sibling(ticket, branch, worktree, main_root, run)
        if colliding is not None:
            reap_receipt = reap_worktree(
                ticket=ticket, main_root=main_root, branch=branch, runner=run
            )
            if reap_receipt.get("checkpoint_failed"):
                raise _ConfigError(
                    f"refusing to bootstrap {ticket}: the dead colliding sibling at "
                    f"{colliding} could not be auto-reaped because checkpointing its "
                    f"uncommitted work failed ({reap_receipt.get('skipped')}) — it is "
                    "left intact, failing toward preserving the work. Rescue it by hand "
                    "(inspect the worktree, push its WIP yourself), then retry."
                )
            if not reap_receipt.get("worktree_removed"):
                raise _DuplicateClaim(
                    f"refusing to bootstrap {ticket}: the dead colliding sibling at "
                    f"{colliding} could not be auto-reaped ({reap_receipt.get('skipped')}); "
                    "it may have gone live since classification. To unstick: resume/inspect "
                    f"it, or tear it down by hand (`flow_worktree.py reap --ticket {ticket}`)."
                )

        if journal is not None:
            try:
                journal.advance("worktree_intended", worktree=str(worktree), branch=branch)
            except bootstrap_journal.JournalError as exc:
                raise _ConfigError(f"cannot journal approved worktree intent: {exc}") from exc
        _git(["worktree", "add", "-b", branch, str(worktree), base], main_root, run)
        if journal is not None:
            try:
                journal.advance("worktree_created", worktree=str(worktree), branch=branch)
            except bootstrap_journal.JournalError as exc:
                run(["git", "worktree", "remove", "--force", str(worktree)], main_root)
                run(["git", "branch", "-D", branch], main_root)
                raise _ConfigError(f"cannot journal approved worktree creation: {exc}") from exc

        # Past the worktree+branch creation, ANY exception (a deliberate refusal
        # below, or a non-deliberate raise from _copy_config / mise / _seed_state /
        # the frontmatter write) would otherwise strand the worktree dir AND the
        # -b-created branch. Clean both before propagating so a crash or refusal
        # leaves no orphan (flow-fh05, broadening flow-n2a6's single-site cleanup).
        # Cleanup runs inside the flock so a sibling never sees a half-state. Remove
        # the worktree before the branch because a checked-out branch refuses -D.
        # Receipt-free callers retain best-effort cleanup. Approved bootstraps reset
        # their journal only after both removals are proven.
        try:
            # A gitignored planned file is silently dropped from the commit and hard-fails
            # capture-implement-diff's `git add --intent-to-add` four stages later in the
            # unattended tail. Catch it here, at the spec gate, while the user is present.
            # Checked in the WORKTREE, not main_root: the worktree is checked out from
            # `base`, which may carry .gitignore negations (e.g. a stacked PR off a feature
            # branch) that main_root's current branch lacks; checking main_root would
            # false-refuse a file `base` legitimately un-ignores.
            if planned_files:
                ignored = _gitignored(planned_files, worktree, run)
                if ignored:
                    ignore_file_planned = any(
                        f == ".gitignore" or f.endswith("/.gitignore") for f in planned_files
                    )
                    if ignore_file_planned:
                        # The plan touches .gitignore, but that change is not committed yet,
                        # so check-ignore still flags these. Warn, do not refuse: the planned
                        # negation may legitimately un-ignore them.
                        warnings.append(
                            "planned files are currently gitignored: "
                            + ", ".join(ignored)
                            + " (plan also touches .gitignore; "
                            "ensure your negation un-ignores them)"
                        )
                    else:
                        raise _ConfigError(
                            "planned files are gitignored and would be silently dropped from "
                            "the commit: "
                            + ", ".join(ignored)
                            + " (add a .gitignore negation to the plan's files, "
                            "or fix the planned paths)"
                        )

            if planned_files:
                typo = _typo_planned(planned_files, worktree)
                if typo:
                    warnings.append(
                        "planned files in a non-existent directory (likely a path typo): "
                        + ", ".join(typo)
                        + " (a new file in an existing dir is fine; check the parent path)"
                    )
                misreg = _mislocated_registry(planned_files, worktree)
                if misreg:
                    warnings.append(
                        "planned stage-registry.toml path does not exist: "
                        + ", ".join(misreg)
                        + " (the registry lives at the skill root, never scripts/; "
                        "a wrong prefix reads as unowned drift and aborts the run)"
                    )

            copied = _copy_config(main_root, worktree)
            _ensure_flow_config(main_root, worktree, _shared_memory_base(main_root))

            if mise_trust and (
                (worktree / "mise.toml").exists() or (worktree / ".mise.toml").exists()
            ):
                result = run(["mise", "trust"], worktree)
                if result.returncode != 0:
                    warnings.append(
                        f"mise trust failed: {result.stderr.strip()} "
                        "(the tail may die on first `mise run`)"
                    )

            head_sha = _git(["rev-parse", "HEAD"], worktree, run)
            run_id = _seed_state(worktree, ticket, plan_text, head_sha)
            route_snapshot = _freeze_route_snapshot(
                worktree, ticket, owner_harness, route_overrides
            )
            if approval is not None and route_snapshot["digest"] != approval.route_digest:
                raise _ConfigError(
                    "seeded route snapshot does not match the exact native-gate receipt"
                )
            if approval is not None:
                _seed_approval_receipt(worktree, ticket, approval)
            if journal is not None:
                journal.advance("run_seeded", run_id=run_id)

            _stamp_run_frontmatter(
                worktree,
                ticket,
                planned_files=planned_files,
                covers=covers,
                commit_type=commit_type,
                commit_summary=commit_summary,
                e2e_recipe=e2e_recipe,
                unattended=unattended,
                # An unattended run derives its lane
                # from the bead's tier labels (per the CLI help + delivery-plan.md).
                lane=_effective_lane(
                    explicit=None if auto else lane,
                    ticket=ticket,
                    planned_files=planned_files,
                    main_root=main_root,
                ),
            )

            # Last step: the run is fully seeded, so carrying spilled edits in (and
            # cleaning them off main) can no longer be undone by the except-cleanup.
            if spilled:
                _relocate_spilled(spilled, main_root, worktree, run, warnings)
            if journal is not None:
                journal.advance("committed")
        except Exception as original:
            if journal is None:
                run(["git", "worktree", "remove", "--force", str(worktree)], main_root)
                run(["git", "branch", "-D", branch], main_root)
                raise
            try:
                recovery_record = journal.prepare(
                    ticket=ticket,
                    approval=approval.to_mapping() if approval is not None else {},
                )
                _rollback_incomplete_approved_bootstrap(
                    record=recovery_record,
                    main_root=main_root,
                    run=run,
                )
                journal.restart_after_rollback()
            except (bootstrap_journal.JournalError, _ConfigError) as cleanup_error:
                raise _ConfigError(
                    "approved bootstrap failed and cleanup could not be proven; "
                    "rollback coordinates remain in the journal: "
                    f"{cleanup_error}"
                ) from original
            raise

    return {
        "ticket": ticket,
        "branch": branch,
        "worktree": str(worktree),
        "run_id": run_id,
        "copied": copied,
        "warnings": warnings,
        "route_digest": route_snapshot["digest"],
        "approval_digest": approval.digest if approval is not None else None,
    }


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Flow worktree bootstrap for delivery.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("create", help="Create a worktree + seed state for the tail.")
    p.add_argument("--ticket", required=True)
    p.add_argument("--plan-from", required=True, help="path to the approved plan file")
    p.add_argument(
        "--base",
        required=True,
        help="base branch/ref for the new worktree; '@default' = the freshly-fetched "
        "default branch (use for --auto/autonomous runs so the launcher's branch never leaks in)",
    )
    p.add_argument("--branch", required=True, help="new branch name (e.g. feat/FT-1-thing)")
    p.add_argument("--main-root", default=".", help="path to the main checkout (default cwd)")
    p.add_argument("--worktree-path", default=None, help="override the derived worktree path")
    p.add_argument(
        "--planned-files",
        default=None,
        help="comma-separated files the plan will touch; seeds frontmatter planned_files "
        "so the implement pre-hook + commit stage don't pause to ask",
    )
    p.add_argument(
        "--covers",
        default=None,
        help="comma-separated sibling ticket keys this one run co-delivers; seeds frontmatter "
        "covers so the delivery fan-out (transition / PR comment / reflect) closes each one. "
        "Lead owns identity (lease/state/branch); covers must be distinct, live, non-epic",
    )
    p.add_argument("--commit-type", default=None)
    p.add_argument("--commit-summary", default=None)
    p.add_argument(
        "--route",
        action="append",
        default=[],
        help="profile=harness,model,effort; repeatable and frozen into run provenance",
    )
    p.add_argument(
        "--approval-receipt",
        default=None,
        help="exact native-gate receipt for routed planning; legacy host-native callers omit it",
    )
    p.add_argument(
        "--lane",
        default=None,
        choices=["express", "light", "full"],
        help="explicit verification lane (interactive override): precedence over the "
        "bead's tier labels; a hot change (guard file in planned_files, or a hot-labelled "
        "bead) clamps to full regardless. Interactive-only; --auto derives from labels.",
    )
    p.add_argument(
        "--e2e-recipe",
        default=None,
        help="the e2e recipe the plan declared (runner + fixture + command + expected, "
        "or 'skip: <reason>' / 'test-ci-only'); required unless the workspace explicitly "
        "disabled e2e (handler 'none'). Seeds frontmatter e2e_recipe so the e2e stage "
        "runs unattended",
    )
    p.add_argument("--no-mise-trust", action="store_true")
    p.add_argument(
        "--recover-spill",
        action="store_true",
        help="recover edits a soft-gate harness (no plan-mode write-block) spilled onto "
        "the main checkout before bootstrap: a planned file left uncommitted on main is "
        "carried into the seeded worktree. The cross-harness AGENTS.md entry point passes "
        "this; Claude Code omits it (plan mode keeps main clean), so the CC path is unchanged",
    )
    p.add_argument(
        "--auto",
        action="store_true",
        help="autonomous run: code-enforce the is_hot_change floor (refuse to bootstrap "
        "a hot change with no recorded DECISION:/TRIAGE-DECISION:). A `@default` base "
        "implies this too. Omit for interactive runs (ExitPlanMode is the human gate)",
    )

    r = sub.add_parser("reap", help="Remove the local worktree + branch after a merge.")
    r.add_argument("--ticket", required=True)
    r.add_argument("--branch", default=None, help="branch to reap (else derived from --ticket)")
    r.add_argument("--main-root", default=".", help="path to the main checkout (default cwd)")

    lr = sub.add_parser(
        "locate-or-reseed",
        help="Locate the ticket's worktree, or re-materialize it from the PR branch (revise).",
    )
    lr.add_argument("--ticket", required=True)
    lr.add_argument(
        "--branch", required=True, help="the PR's feature branch to check out on reseed"
    )
    lr.add_argument("--main-root", default=".", help="path to the main checkout (default cwd)")

    return parser.parse_args(argv)


def _run_reap(args: argparse.Namespace) -> int:
    import json

    try:
        receipt = reap_worktree(
            ticket=args.ticket,
            main_root=Path(args.main_root),
            branch=args.branch,
        )
    except _GitError as exc:
        sys.stderr.write(f"flow-worktree: {exc}\n")
        return 1
    sys.stdout.write(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    if receipt.get("checkpoint_failed"):
        # Checkpoint capture failed; the worktree was left intact (fail toward
        # preserving work). Non-zero so an `&&`-gated caller (drain §Recover)
        # never advances past a reap that did not actually tear anything down.
        return 5
    return 0


def _run_locate_or_reseed(args: argparse.Namespace) -> int:
    import json

    try:
        result = locate_or_reseed(
            ticket=args.ticket,
            branch=args.branch,
            main_root=Path(args.main_root),
        )
    except _ConfigError as exc:
        sys.stderr.write(f"flow-worktree: {exc}\n")
        return 2
    except _GitError as exc:
        sys.stderr.write(f"flow-worktree: {exc}\n")
        return 1
    except OSError as exc:
        sys.stderr.write(f"flow-worktree: I/O error: {exc}\n")
        return 3
    sys.stdout.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return 0


def cli_main(argv: list[str]) -> int:
    import json

    args = _parse_args(argv)
    if args.cmd == "reap":
        return _run_reap(args)
    if args.cmd == "locate-or-reseed":
        return _run_locate_or_reseed(args)
    planned = (
        [s.strip() for s in args.planned_files.split(",") if s.strip()]
        if args.planned_files
        else []
    )
    covers = [s.strip() for s in args.covers.split(",") if s.strip()] if args.covers else []
    try:
        result = bootstrap(
            ticket=args.ticket,
            plan_from=Path(args.plan_from).expanduser(),
            base=args.base,
            branch=args.branch,
            main_root=Path(args.main_root),
            worktree_override=args.worktree_path,
            planned_files=planned,
            covers=covers,
            commit_type=args.commit_type,
            commit_summary=args.commit_summary,
            e2e_recipe=args.e2e_recipe,
            lane=args.lane,
            mise_trust=not args.no_mise_trust,
            auto=args.auto,
            recover_spill=args.recover_spill,
            route_overrides=args.route,
            approval_receipt=(
                Path(args.approval_receipt).expanduser() if args.approval_receipt else None
            ),
        )
    except _ConfigError as exc:
        sys.stderr.write(f"flow-worktree: {exc}\n")
        return 2
    except _GitError as exc:
        sys.stderr.write(f"flow-worktree: {exc}\n")
        return 1
    except _DuplicateClaim as exc:
        sys.stderr.write(f"flow-worktree: {exc}\n")
        return 4
    except _TerminalBead as exc:
        sys.stderr.write(f"flow-worktree: {exc}\n")
        return 6
    except _EpicBead as exc:
        sys.stderr.write(f"flow-worktree: {exc}\n")
        return 7
    except _HitlBead as exc:
        sys.stderr.write(f"flow-worktree: {exc}\n")
        return 8
    except OSError as exc:
        sys.stderr.write(f"flow-worktree: I/O error: {exc}\n")
        return 3

    for w in result["warnings"]:
        sys.stderr.write(f"flow-worktree: WARN {w}\n")
    sys.stdout.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    sys.stderr.write(f"\nworktree ready at {result['worktree']}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))
