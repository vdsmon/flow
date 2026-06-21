"""flow_worktree.py — post-approval bootstrap for the ticket pipeline.

After `/flow spec` approves a plan (ExitPlanMode), this seeds a git worktree so the
pipeline resumes directly at the implement stage. The spec session then enters this
worktree (EnterWorktree) and continues the `do` pipeline in the SAME conversation;
running it unattended is a separate, harness-level choice (`/bg`), not this script's
concern.

  1. git worktree add -b <branch> <worktree> <base>
  2. copy gitignored dev config main->worktree; ensure .flow/.initialized +
     workspace.toml exist (a git worktree only materializes committed files)
  3. mise trust the worktree (toolchain) unless --no-mise-trust
  4. redirect the worktree's memory store to the main checkout's .flow via the
     gitignored .flow/memory-root sibling (shared store, so per-ticket worktrees
     don't fragment the compounding-knowledge layer; tracked workspace.toml untouched)
  5. seed state.json: plan marked completed with its output_path; plan.out written
     from --plan-from; ticket left pending so the pipeline self-fetches ticket.json
     and stamps frontmatter (keeps the bootstrap offline; tracker auth stays live)
  6. stamp commit_type/commit_summary (and e2e_recipe when e2e is opted in, and the
     verification lane when express/light) into the worktree frontmatter so the
     commit + e2e + lane-gated stages do not block on a prompt
  7. print the worktree path (the spec session enters it via EnterWorktree)

The bootstrap holds NO run lease; the pipeline's cmd_init acquires it under the
run_id seeded here (it sees that run_id as the owner, so resume is clean). It
DOES transiently hold the canonical per-ticket bootstrap CLAIM — a flock on
<main_root>/.flow/tickets/<ticket>.claim, held across worktree-add → state-seed
→ frontmatter stamp, released at bootstrap exit — under which it refuses
(exit 4) when a live sibling run already holds this ticket. The .claim file
persists after release by design (deleting a flock target would race a waiter).

Exit codes:
  0 = ok (may carry warnings on stderr)
  1 = git / worktree error
  2 = bad args / missing main workspace config
  3 = I/O error
  4 = duplicate claim (a live sibling run already holds this ticket)
  6 = bead is terminal (closed/done/cancelled) — nothing to bootstrap
  7 = bead is an epic (a container, not a single-PR unit) — refuse to bootstrap
"""

from __future__ import annotations

import argparse
import secrets
import shutil
import subprocess
import sys
from pathlib import Path
from typing import cast

import _atomicio
import _locking
import _workspace
import lease
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


def _porcelain_paths(main_root: Path, runner: Runner) -> dict[str, bool]:
    """Map each uncommitted path in `main_root` -> is_untracked.

    `git status --porcelain` lines are `XY <path>` (or `XY <orig> -> <path>` for a
    rename); `??` is untracked. Paths are repo-relative, matching planned_files'
    convention (`_gitignored` uses `cwd / f`). Renames take the post-`->` name.
    Quoted paths (core.quotePath on exotic filenames) are left as-is — a rare miss,
    not a fault, for this backstop.
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
    leaves no diff to conflict (the worktree just takes the agent's version), and
    main is cleaned ONLY after the copy verifiably landed — so the work is never in
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
                    # Worktree has the work; main just wasn't reverted (e.g. a staged
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


def _copy_config(main_root: Path, worktree: Path, extra: list[str]) -> list[str]:
    """Copy gitignored dev config main->worktree. Returns the list copied."""
    copied: list[str] = []
    for rel in [*_DEFAULT_COPY, *extra]:
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


def _ensure_flow_config(main_root: Path, worktree: Path, shared_flow: Path) -> None:
    """Ensure the worktree has .flow/.initialized + workspace.toml (copying from
    main when absent — the gitignored case), then redirect the memory store to the
    shared (main) .flow via the gitignored `.flow/memory-root` sibling.

    The redirect lives in the sibling, NOT in workspace.toml: the tracked
    workspace.toml stays byte-identical to main's copy so a per-machine absolute
    path can never ride into a commit. `resolve_memory_base` reads the sibling
    first (see _memory_paths)."""
    wt_flow = worktree / ".flow"
    wt_ws = wt_flow / "workspace.toml"
    if not wt_ws.exists():
        main_ws = main_root / ".flow" / "workspace.toml"
        if not main_ws.exists():
            raise _ConfigError(
                f"no workspace.toml at {main_ws}; run /flow init in the main checkout first"
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
    (wt_flow / "memory-root").write_text(str(shared_flow) + "\n", encoding="utf-8")


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


def _e2e_enabled(main_root: Path) -> bool:
    """True when the workspace wires e2e to a real handler (not 'none').

    A 'none' handler short-circuits the stage before its reference doc loads, so
    no recipe is needed there. Only an opted-in e2e demands a recipe.
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
    if override:
        return Path(override).expanduser().resolve()
    main = main_root.resolve()
    return main / ".flow" / "worktrees" / branch.replace("/", "-")


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


def _is_ticket_branch(short_branch: str, ticket: str) -> bool:
    """True when `short_branch` is this ticket's feature branch (exact or slugged)."""
    return short_branch == f"feature/{ticket}" or short_branch.startswith(f"feature/{ticket}-")


def _ticket_siblings(ticket: str, main_root: Path, runner: Runner) -> list[tuple[Path, str]]:
    """All registered worktrees whose checked-out branch belongs to `ticket`."""
    listing = _git(["worktree", "list", "--porcelain"], main_root, runner)
    return [
        (Path(path), sb)
        for path, sb in _parse_worktree_list(listing)
        if sb is not None and _is_ticket_branch(sb, ticket)
    ]


def _claim_path(main_root: Path, ticket: str) -> Path:
    return main_root / ".flow" / "tickets" / f"{ticket}.claim"


def _assert_no_live_sibling(ticket: str, main_root: Path, runner: Runner) -> None:
    """Refuse (under the held bootstrap claim) when a sibling run is live.

    Per sibling worktree, the run's <wt>/.flow/runs/<ticket> is classified via
    lease.classify: a live or corrupt run.lock refuses; an expired lease is a
    dead sibling (reap owns its teardown) and proceeds. A free lease with a
    seeded NON-TERMINAL state.json (any stage pending/in_progress — which
    includes a failed-mid-pipeline run, since /flow recover can resume it) also
    refuses: that is the bootstrap→cmd_init window where the winner has seeded
    state but not yet acquired its run lease.
    """
    unstick = (
        f"resume/inspect it via `/flow recover {ticket}`, or tear down a dead "
        f"sibling via `flow_worktree.py reap --ticket {ticket}`"
    )
    now = utcnow_iso()
    boot = lease.boot_id()
    host = lease.hostname()
    for wt_path, _sb in _ticket_siblings(ticket, main_root, runner):
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


def reap_worktree(
    *,
    ticket: str,
    main_root: Path,
    branch: str | None = None,
    runner: Runner | None = None,
) -> dict:
    """Tear down the local worktree + branch left behind after a squash-merge.

    The squash-merge (`gh pr merge --squash`) deletes no branch (gh's
    branch-delete is skipped), and the separate `git push origin --delete
    <branch>` touches only the remote ref; so the local `feature/<key>-*`
    branch and its still-registered worktree survive regardless (the worktree
    holds that branch checked out, which also blocks any local-branch delete).
    This reaps them, gated on the per-ticket lease: when the worktree's run is
    still live (the bg session is, typically, in reflect) NOTHING is touched
    and a later pass reaps it.

    Idempotent: a second call (worktree + branch already gone) is a clean no-op.
    """
    run = runner or _default_runner()
    main_root = main_root.expanduser().resolve()

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
        elif _is_ticket_branch(sb, ticket):
            target_path = Path(path)
            resolved_branch = sb
            break

    receipt = {
        "ticket": ticket,
        "branch": resolved_branch,
        "worktree": str(target_path) if target_path is not None else None,
        "worktree_removed": False,
        "branch_deleted": False,
        "skipped": None,
    }

    if target_path is not None:
        ticket_dir = target_path / ".flow" / "runs" / ticket

        # Hold the lease flock ACROSS classify + the worktree-remove so a
        # concurrent acquire cannot go live between the decision and the
        # destructive mutation (the flow-72d9 incident family). classify_then's
        # teardown runs only when the lease is non-live/non-corrupt; it runs a
        # git subprocess only (no lease re-entry — flock is non-reentrant).
        outcome = lease.classify_then(
            ticket_dir,
            utcnow_iso(),
            lambda: run(["git", "worktree", "remove", "--force", str(target_path)], main_root),
            current_boot=lease.boot_id(),
            hostname=lease.hostname(),
        )
        if not outcome["torn_down"]:
            if outcome["state"] == "live":
                receipt["skipped"] = "lease live (run still in progress)"
            else:
                receipt["skipped"] = "lease corrupt (run.lock unparseable; possibly live)"
            return receipt
        result = cast(subprocess.CompletedProcess[str], outcome["result"])
        if result.returncode != 0:
            receipt["skipped"] = f"worktree remove failed: {result.stderr.strip()}"
            return receipt
        receipt["worktree_removed"] = True

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
) -> dict:
    """Locate the ticket's worktree, or re-materialize it from the PR branch (flow-kx17.2).

    A revision (/flow revise) needs the worktree the original run left behind. The
    norm (PR-open ⇒ worktree-present) is a LOCATE: a registered worktree on a
    `feature/<ticket>*` branch is returned as-is (reseeded:false). When the worktree
    was externally reaped, RESEED: fetch the existing remote branch and `git worktree
    add <path> <branch>` (checkout, NOT -b), then re-copy gitignored config + redirect
    memory + mise trust via the same helpers bootstrap uses (reseeded:true).
    """
    run = runner or _default_runner()
    main_root = main_root.expanduser().resolve()

    siblings = _ticket_siblings(ticket, main_root, run)
    if siblings:
        return {"worktree": str(siblings[0][0]), "reseeded": False}

    worktree = _worktree_path(main_root, branch, None)
    _git(["fetch", "origin", branch], main_root, run)
    _git(["worktree", "add", str(worktree), branch], main_root, run)
    _copy_config(main_root, worktree, [])
    _ensure_flow_config(main_root, worktree, main_root / ".flow")
    if (worktree / "mise.toml").exists() or (worktree / ".mise.toml").exists():
        run(["mise", "trust"], worktree)
    return {"worktree": str(worktree), "reseeded": True}


_DEFAULT_BASE = "@default"


def _resolve_base(base: str, main_root: Path, runner: Runner) -> str:
    """Resolve the worktree base ref.

    `@default` resolves to the freshly-fetched default branch (`origin/<HEAD>`),
    so an autonomous (`--auto`) run never inherits the launcher's current branch
    or a stale local `main` — branching off either pollutes the PR with
    already-merged commits. Any other value passes through unchanged (interactive
    runs branch off their integration branch on purpose).
    """
    if base != _DEFAULT_BASE:
        return base
    _git(["fetch", "--quiet", "origin"], main_root, runner)
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
    return name or "origin/main"


def _enforce_hot_floor(
    *,
    ticket: str,
    base: str,
    auto: bool,
    planned_files: list[str] | None,
    main_root: Path,
) -> None:
    """Code-enforced hot hard-floor (flow-aen).

    An autonomous run — signaled by `--auto` OR a `@default` base (the load-bearing
    autonomous base; the drain launches from the main checkout, so `--base` alone is
    not a sufficient signal, hence both) — may NOT self-ship a hot change (a
    guard/safety file, or a `hot`-labelled bead) with no maintainer decision on file.
    This lives at the single shared bootstrap every self-approve path funnels through,
    so it holds for the clean >=90% path too — verb-spec.md step 5 only carried the
    floor in the adjudication/decided sub-branches, so a clean re-plan could slip a
    hot change past it. Beads-only: `triage.decided` reads a `DECISION:`/
    `TRIAGE-DECISION:` comment, a beads-native seam (a non-beads tracker has no such
    record, so gating it would permanently block). Caller invokes this BEFORE
    `git worktree add`, so a refusal leaves no orphan. The `[evolve] adjudicate_hot`
    flag (default off) skips this floor for a maintainer self-target workspace.
    """
    if not (planned_files and (auto or base.strip() == "@default")):
        return
    import triage

    config, _code = triage._resolve_config(main_root)
    if config is None or config.get("backend") != "beads":
        return
    # adjudicate_hot lifts the floor for this (maintainer self-target) workspace:
    # the advisor's proceed ruling stands for hot changes too, gated by the
    # merge-time guard-property review + CI instead of this pre-bootstrap refusal.
    if triage.adjudicate_hot(main_root):
        return
    # No runner threaded: BeadsAdapter (via decided) needs the keyword-only
    # KwRunner protocol, not flow_worktree's positional Runner — passing `run`
    # here throws inside decided's try/except and silently returns block-by-default,
    # which would make the gate unable to read a recorded decision (the triage
    # bypass would never clear). Let decided build its own kw_default_runner.
    probe = triage.decided(config, ticket, planned_files)
    if probe.get("is_hot") and not probe.get("decided"):
        raise _ConfigError(
            "autonomous run refuses to bootstrap a HOT change with no recorded "
            "decision: " + ", ".join(planned_files) + " trips the is_hot_change "
            "floor (a guard/safety file or a 'hot'-labelled bead) and carries no "
            "DECISION:/TRIAGE-DECISION: comment. A hot change never self-approves "
            f'unattended. Triage it (/flow triage {ticket} "<answer>") then re-run, '
            "or run WITHOUT --auto so a human gates it at ExitPlanMode."
        )


_TERMINAL_STATES = frozenset({"done", "cancelled"})


def _refuse_terminal_bead(*, ticket: str, main_root: Path) -> None:
    """Refuse (exit 6) to bootstrap a bead whose authoritative status is terminal.

    Witnessed (flow-d6gq): a `/flow <key> --auto` run bootstrapped a CLOSED bead and
    ran it to implement. The spec `get` ran pre-worktree from the main checkout and
    reflected the bead as open at that instant; the close (its parent epic's merge)
    landed during the run. This re-reads the bead's authoritative status at the
    bootstrap chokepoint — seconds-to-minutes after the spec fetch, so it catches a
    bead that closed during planning — and refuses before `git worktree add` (a
    refusal leaves no orphan). Tracker-agnostic and unconditional (interactive +
    `--auto`): bootstrapping a done/cancelled bead is wrong either way.

    Fail-open is narrow: a genuine read *exception* (tracker construction / subprocess
    failure) proceeds, so a flaky tracker read never strands a legitimate run. A read
    that SUCCEEDS but yields no usable status is NOT fail-open — it refuses, since an
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
    """Refuse (exit 7) to bootstrap an epic — a container, not a single-PR unit.

    Witnessed (flow-jvxj, parent flow-8by2): `/flow <epic> --auto` reached this
    chokepoint on an epic bead. `evolve_select.py` filters `issue_type != "epic"`
    unconditionally so drain never launches one, but a manual or misrouted
    `/flow <epic> --auto` had no structural floor — and bootstrapping an epic
    cram-ships fragments of an unaccepted empire as a single PR (the ouroboros
    verb-evolve.md §epic names). This mirrors the select-side filter at the
    bootstrap chokepoint. Tracker-agnostic ("epic"/"Epic") and unconditional
    (interactive + `--auto`): an epic is decomposed via the §E expand recipe, not
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
            "recipe (verb-evolve.md §E), then each child runs at its own spec gate — "
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


def _stamp_lane(
    *,
    ticket: str,
    main_root: Path,
    explicit_lane: str | None = None,
    planned_files: list[str] | None = None,
) -> str | None:
    """Lane to stamp at bootstrap: express/light only. `full` is the default the
    stages already assume for an absent field, so it is left unstamped (a normal
    run's frontmatter is unchanged).

    An explicit `--lane` (the interactive phase-2 proposal the user approved at the
    spec gate, or the `--lane` flag passed directly) takes precedence over the bead's
    tier labels (the drain path's label derivation). Either way a HOT change (a guard
    file in planned_files) is clamped to full: the hot floor overrides any requested
    downshift, mirroring tier_policy's `hot` precedence on the drain side. The
    interactive vetting is the user at the spec gate, the same role the Opus producer's
    audit plays for a drain-stamped tier label."""
    import triage

    candidate = explicit_lane or _lane_for_bead(ticket=ticket, main_root=main_root)
    if candidate not in ("express", "light"):
        return None
    if triage.is_hot_change(planned_files or []):
        return None  # hot floor: a guard-file change is never expressed/lightened
    return candidate


def _refuse_invalid_covers(*, ticket: str, covers: list[str], main_root: Path) -> None:
    """Each cover must be a distinct, live, non-epic ticket — the lead's floors, looped.

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
    lane: str | None = None,
) -> None:
    """Seed the run frontmatter the unattended tail reads so it never pauses to ask.

    planned_files -> records_diff_baseline pre-hook; covers -> the delivery fan-out
    (transition / PR comment / reflect); commit_type/commit_summary -> the commit
    stage; e2e_recipe -> the e2e lint gate + recipe executor; lane -> the verification
    depth the spec/implement/reflect stages read (tier_policy). List fields go in as
    TOML-array literals so ticket_frontmatter coerces them back to lists.
    """
    fm_updates: dict[str, str] = {}
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
    if fm_updates:
        ticket_frontmatter.update(worktree / ".flow" / "tickets" / f"{ticket}.md", fm_updates)


def bootstrap(
    *,
    ticket: str,
    plan_from: Path,
    base: str,
    branch: str,
    main_root: Path,
    worktree_override: str | None = None,
    extra_copy: list[str] | None = None,
    planned_files: list[str] | None = None,
    covers: list[str] | None = None,
    commit_type: str | None = None,
    commit_summary: str | None = None,
    e2e_recipe: str | None = None,
    lane: str | None = None,
    mise_trust: bool = True,
    auto: bool = False,
    recover_spill: bool = False,
    runner: Runner | None = None,
) -> dict:
    run = runner or _default_runner()
    main_root = main_root.expanduser().resolve()

    # e2e is opt-in; when a workspace enables it the approved plan must declare
    # what the e2e stage runs. Refuse here, while the user is still present at the
    # spec gate, rather than let the unattended tail block at the e2e lint gate.
    if _e2e_enabled(main_root) and not (e2e_recipe and e2e_recipe.strip()):
        raise _ConfigError(
            "e2e handler is enabled in workspace.toml; pass --e2e-recipe "
            "(the approved plan must declare the e2e recipe/fixture, or 'skip: <reason>')"
        )

    # Refuse a bead that is already closed/done before any git mutation (flow-d6gq).
    _refuse_terminal_bead(ticket=ticket, main_root=main_root)

    # Refuse an epic before any git mutation (flow-jvxj): mirrors the select-side
    # `issue_type != "epic"` filter at the bootstrap chokepoint.
    _refuse_epic_bead(ticket=ticket, main_root=main_root)

    # covers: sibling tickets this one run co-delivers. They ride the lead's
    # identity (lease / state / branch / memory stay lead-keyed); only the
    # delivery steps fan out over them.
    covers = [c for c in (covers or []) if c.strip()]
    _refuse_invalid_covers(ticket=ticket, covers=covers, main_root=main_root)

    _enforce_hot_floor(
        ticket=ticket,
        base=base,
        auto=auto,
        planned_files=planned_files,
        main_root=main_root,
    )

    plan_text = plan_from.read_text(encoding="utf-8")
    worktree = _worktree_path(main_root, branch, worktree_override)
    warnings: list[str] = []

    # Detect (read-only) edits a weaker harness spilled onto the main checkout
    # before bootstrap; relocated into the worktree at the end of the try, once the
    # run is fully seeded (so a refusal/crash never deletes work from main first).
    # Opt-in via recover_spill, which ONLY the non-CC AGENTS.md entry point passes:
    # on Claude Code plan mode already blocks the pre-bootstrap edit, so the CC path
    # never sets this and stays byte-identical (a dirty planned file there is the
    # user's own pre-existing WIP, which must not be touched). See references/harness.md.
    spilled = (
        _spilled_planned(planned_files, main_root, run) if recover_spill and planned_files else []
    )

    # The fetch inside _resolve_base stays OUTSIDE the claim, so a second launch
    # never blocks on the winner's network round-trip; the claim window below is
    # local-only ops, seconds.
    base = _resolve_base(base, main_root, run)

    # Canonical per-ticket bootstrap claim: two simultaneous bootstraps of the
    # same ticket serialize here; the loser then sees the winner's seeded state
    # via _assert_no_live_sibling and refuses (exit 4) before any git mutation.
    # Held across worktree-add → state-seed → frontmatter stamp, so a sibling's
    # check never observes a half-seeded run.
    with _locking.flock_blocking(_claim_path(main_root, ticket)):
        _assert_no_live_sibling(ticket, main_root, run)

        _git(["worktree", "add", "-b", branch, str(worktree), base], main_root, run)

        # Past the worktree+branch creation, ANY exception (a deliberate refusal
        # below, or a non-deliberate raise from _copy_config / mise / _seed_state /
        # the frontmatter write) would otherwise strand the worktree dir AND the
        # -b-created branch. Clean both before propagating so a crash or refusal
        # leaves no orphan (flow-fh05, broadening flow-n2a6's single-site cleanup).
        # Cleanup runs inside the flock so a sibling never sees a half-state; remove
        # the worktree BEFORE the branch (a checked-out branch refuses -D); best-effort
        # `run` (not `_git`) so a cleanup failure never masks the original exception.
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
                            + " (plan also touches .gitignore; ensure your negation un-ignores them)"
                        )
                    else:
                        raise _ConfigError(
                            "planned files are gitignored and would be silently dropped from "
                            "the commit: "
                            + ", ".join(ignored)
                            + " (add a .gitignore negation to the plan's files, or fix the planned paths)"
                        )

            if planned_files:
                typo = _typo_planned(planned_files, worktree)
                if typo:
                    warnings.append(
                        "planned files in a non-existent directory (likely a path typo): "
                        + ", ".join(typo)
                        + " (a new file in an existing dir is fine; check the parent path)"
                    )

            copied = _copy_config(main_root, worktree, extra_copy or [])
            _ensure_flow_config(main_root, worktree, main_root / ".flow")

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

            _stamp_run_frontmatter(
                worktree,
                ticket,
                planned_files=planned_files,
                covers=covers,
                commit_type=commit_type,
                commit_summary=commit_summary,
                e2e_recipe=e2e_recipe,
                lane=_stamp_lane(
                    ticket=ticket,
                    main_root=main_root,
                    explicit_lane=lane,
                    planned_files=planned_files,
                ),
            )

            # Last step: the run is fully seeded, so carrying spilled edits in (and
            # cleaning them off main) can no longer be undone by the except-cleanup.
            if spilled:
                _relocate_spilled(spilled, main_root, worktree, run, warnings)
        except Exception:
            run(["git", "worktree", "remove", "--force", str(worktree)], main_root)
            run(["git", "branch", "-D", branch], main_root)
            raise

    return {
        "ticket": ticket,
        "branch": branch,
        "worktree": str(worktree),
        "run_id": run_id,
        "copied": copied,
        "warnings": warnings,
    }


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="/flow worktree bootstrap for the background tail."
    )
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
    p.add_argument("--branch", required=True, help="new branch name (e.g. feature/FT-1-thing)")
    p.add_argument("--main-root", default=".", help="path to the main checkout (default cwd)")
    p.add_argument("--worktree-path", default=None, help="override the derived worktree path")
    p.add_argument("--copy", default=None, help="extra comma-separated gitignored paths to copy")
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
        "--lane",
        default=None,
        choices=["express", "light", "full"],
        help="verification lane the user approved at the spec gate (interactive phase 2) "
        "or passed directly; takes precedence over the bead's tier labels. A hot change "
        "(guard file in --planned-files) is clamped to full regardless. Omit to derive "
        "from tier labels (the drain path)",
    )
    p.add_argument(
        "--e2e-recipe",
        default=None,
        help="the e2e recipe the plan declared (runner + fixture + command + expected, "
        "or 'skip: <reason>' / 'test-ci-only'); required when the workspace enables e2e. "
        "Seeds frontmatter e2e_recipe so the opted-in e2e stage runs unattended",
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
    extra = [s.strip() for s in args.copy.split(",")] if args.copy else []
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
            extra_copy=extra,
            planned_files=planned,
            covers=covers,
            commit_type=args.commit_type,
            commit_summary=args.commit_summary,
            e2e_recipe=args.e2e_recipe,
            lane=args.lane,
            mise_trust=not args.no_mise_trust,
            auto=args.auto,
            recover_spill=args.recover_spill,
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
