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
  6. stamp commit_type/commit_summary (and e2e_recipe when e2e is opted in) into
     the worktree frontmatter so the commit + e2e stages do not block on a prompt
  7. print the worktree path (the spec session enters it via EnterWorktree)

The bootstrap holds NO lease; the pipeline's cmd_init acquires it under the
run_id seeded here (it sees that run_id as the owner, so resume is clean).

Exit codes:
  0 = ok (may carry warnings on stderr)
  1 = git / worktree error
  2 = bad args / missing main workspace config
  3 = I/O error
"""

from __future__ import annotations

import argparse
import secrets
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

import _atomicio
import _workspace
import lease
import maintainer
import state
import ticket_frontmatter
from _runner import Runner
from _runner import default_runner as _default_runner

# The two files the version-bump invariant requires on any plugin-code change.
_VERSION_BUMP_FILES = (
    "plugins/flow/.claude-plugin/plugin.json",
    ".claude-plugin/marketplace.json",
)


def _with_version_bump_files(planned: list[str], main_root: Path) -> list[str]:
    """Append the version-bump files to planned_files for maintainer plugin-code changes.

    The implement stage always bumps these two files on a plugin-code change (the
    version-bump invariant), so a maintainer ticket that touches `plugins/flow/`
    otherwise pays a post-implement reconcile to add them. No-op for user projects
    and non-plugin changes.
    """
    if not maintainer.is_maintainer(main_root):
        return planned
    if not any(f.startswith("plugins/flow/") for f in planned):
        return planned
    return planned + [f for f in _VERSION_BUMP_FILES if f not in planned]


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


def _utcnow_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


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
        elif sb == f"feature/{ticket}" or sb.startswith(f"feature/{ticket}-"):
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
        info = lease.classify(ticket_dir, _utcnow_iso())
        if info.get("state") == "live":
            receipt["skipped"] = "lease live (run still in progress)"
            return receipt
        if info.get("state") == "corrupt":
            receipt["skipped"] = "lease corrupt (run.lock unparseable; possibly live)"
            return receipt
        result = run(["git", "worktree", "remove", "--force", str(target_path)], main_root)
        if result.returncode != 0:
            receipt["skipped"] = f"worktree remove failed: {result.stderr.strip()}"
            return receipt
        receipt["worktree_removed"] = True

    if resolved_branch:
        result = run(["git", "branch", "-D", resolved_branch], main_root)
        receipt["branch_deleted"] = result.returncode == 0

    return receipt


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
    commit_type: str | None = None,
    commit_summary: str | None = None,
    e2e_recipe: str | None = None,
    mise_trust: bool = True,
    auto: bool = False,
    runner: Runner | None = None,
) -> dict:
    run = runner or _default_runner()
    main_root = main_root.expanduser().resolve()

    if planned_files:
        planned_files = _with_version_bump_files(planned_files, main_root)

    # e2e is opt-in; when a workspace enables it the approved plan must declare
    # what the e2e stage runs. Refuse here, while the user is still present at the
    # spec gate, rather than let the unattended tail block at the e2e lint gate.
    if _e2e_enabled(main_root) and not (e2e_recipe and e2e_recipe.strip()):
        raise _ConfigError(
            "e2e handler is enabled in workspace.toml; pass --e2e-recipe "
            "(the approved plan must declare the e2e recipe/fixture, or 'skip: <reason>')"
        )

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

    base = _resolve_base(base, main_root, run)
    _git(["worktree", "add", "-b", branch, str(worktree), base], main_root, run)

    # A gitignored planned file is silently dropped from the commit and hard-fails
    # capture-implement-diff's `git add --intent-to-add` four stages later in the
    # unattended tail. Catch it here, at the spec gate, while the user is present.
    # Checked in the WORKTREE, not main_root: the worktree is checked out from
    # `base`, which may carry .gitignore negations (e.g. a stacked PR off a feature
    # branch) that main_root's current branch lacks; checking main_root would
    # false-refuse a file `base` legitimately un-ignores. On a real ignore we remove
    # the just-created worktree so refusing leaves no orphan.
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
                run(["git", "worktree", "remove", "--force", str(worktree)], main_root)
                raise _ConfigError(
                    "planned files are gitignored and would be silently dropped from "
                    "the commit: "
                    + ", ".join(ignored)
                    + " (add a .gitignore negation to the plan's files, or fix the planned paths)"
                )

    copied = _copy_config(main_root, worktree, extra_copy or [])
    _ensure_flow_config(main_root, worktree, main_root / ".flow")

    if mise_trust and ((worktree / "mise.toml").exists() or (worktree / ".mise.toml").exists()):
        result = run(["mise", "trust"], worktree)
        if result.returncode != 0:
            warnings.append(
                f"mise trust failed: {result.stderr.strip()} (the tail may die on first `mise run`)"
            )

    head_sha = _git(["rev-parse", "HEAD"], worktree, run)
    run_id = _seed_state(worktree, ticket, plan_text, head_sha)

    fm_updates: dict[str, str] = {}
    if planned_files:
        # the implement pre-handler hook (records_diff_baseline) reads frontmatter
        # `planned_files`; seeding it here keeps the tail from pausing to ask.
        # Pass a TOML-array literal so ticket_frontmatter coerces it to a list.
        fm_updates["planned_files"] = "[" + ", ".join(f'"{f}"' for f in planned_files) + "]"
    if commit_type:
        fm_updates["commit_type"] = commit_type
    if commit_summary:
        fm_updates["commit_summary"] = commit_summary
    if e2e_recipe:
        # the e2e stage reads frontmatter `e2e_recipe` (lint_ticket HARD GATE +
        # the recipe-executor doc); seeding it here is what lets the opted-in
        # e2e stage run unattended without pausing to ask.
        fm_updates["e2e_recipe"] = e2e_recipe
    if fm_updates:
        ticket_frontmatter.update(worktree / ".flow" / "tickets" / f"{ticket}.md", fm_updates)

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
    p.add_argument("--commit-type", default=None)
    p.add_argument("--commit-summary", default=None)
    p.add_argument(
        "--e2e-recipe",
        default=None,
        help="the e2e recipe the plan declared (runner + fixture + command + expected, "
        "or 'skip: <reason>' / 'test-ci-only'); required when the workspace enables e2e. "
        "Seeds frontmatter e2e_recipe so the opted-in e2e stage runs unattended",
    )
    p.add_argument("--no-mise-trust", action="store_true")
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


def cli_main(argv: list[str]) -> int:
    import json

    args = _parse_args(argv)
    if args.cmd == "reap":
        return _run_reap(args)
    extra = [s.strip() for s in args.copy.split(",")] if args.copy else []
    planned = (
        [s.strip() for s in args.planned_files.split(",") if s.strip()]
        if args.planned_files
        else []
    )
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
            commit_type=args.commit_type,
            commit_summary=args.commit_summary,
            e2e_recipe=args.e2e_recipe,
            mise_trust=not args.no_mise_trust,
            auto=args.auto,
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

    for w in result["warnings"]:
        sys.stderr.write(f"flow-worktree: WARN {w}\n")
    sys.stdout.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    sys.stderr.write(f"\nworktree ready at {result['worktree']}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))
