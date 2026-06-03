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
  4. point the worktree's [memory].root at the main checkout's .flow (shared store,
     so per-ticket worktrees don't fragment the compounding-knowledge layer)
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
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import _atomicio
import _workspace
import state
import ticket_frontmatter

Runner = Callable[[list[str], Path], subprocess.CompletedProcess[str]]

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


def _default_runner() -> Runner:
    def run(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, check=False)

    return run


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


def _table_name(line: str) -> str | None:
    """Return the table name for a `[table]` header line, else None. Tolerates a
    trailing inline comment (`[memory] # note`) and ignores `[[array]]` headers —
    so a user's hand-edited workspace.toml doesn't slip past the [memory] match
    and get a duplicate table appended (which would not parse)."""
    s = line.split("#", 1)[0].strip()
    if s.startswith("[[") or not (s.startswith("[") and s.endswith("]")):
        return None
    return s[1:-1].strip()


def _set_memory_root(toml_text: str, root: str) -> str:
    """Insert/replace `root = "<root>"` under the [memory] table, preserving the
    rest of the file (comments, ordering). Assumes a [memory] section exists (a
    valid flow workspace always has one)."""
    lines = toml_text.splitlines()
    out: list[str] = []
    in_memory = False
    replaced = False
    for line in lines:
        name = _table_name(line)
        if name is not None:
            # leaving the [memory] table without having seen a root key -> inject now
            if in_memory and not replaced:
                out.append(f'root = "{root}"')
                replaced = True
            in_memory = name == "memory"
            out.append(line)
            continue
        key = line.split("=", 1)[0].strip() if "=" in line else ""
        if in_memory and key == "root":
            out.append(f'root = "{root}"')
            replaced = True
            continue
        out.append(line)
    if in_memory and not replaced:  # [memory] was the last table in the file
        out.append(f'root = "{root}"')
        replaced = True
    if not replaced:  # no [memory] table at all
        out.append("[memory]")
        out.append(f'root = "{root}"')
    return "\n".join(out) + "\n"


def _copy_config(main_root: Path, worktree: Path, extra: list[str]) -> list[str]:
    """Copy gitignored dev config main->worktree. Returns the list copied."""
    copied: list[str] = []
    for rel in [*_DEFAULT_COPY, *extra]:
        src = main_root / rel
        if not src.exists():
            continue
        dst = worktree / rel
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        copied.append(rel)
    return copied


def _ensure_flow_config(main_root: Path, worktree: Path, shared_flow: Path) -> None:
    """Ensure the worktree has .flow/.initialized + workspace.toml (copying from
    main when absent — the gitignored case), then point [memory].root at the
    shared store."""
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
    # redirect the memory store to the shared (main) .flow
    wt_ws.write_text(
        _set_memory_root(wt_ws.read_text(encoding="utf-8"), str(shared_flow)), encoding="utf-8"
    )


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
    state.init(ticket_dir, ticket, backend, list(stages), run_id=run_id)

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
    return main.parent / f"{main.name}.worktrees" / branch.replace("/", "-")


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

    plan_text = plan_from.read_text(encoding="utf-8")
    worktree = _worktree_path(main_root, branch, worktree_override)
    warnings: list[str] = []

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
    p.add_argument("--base", required=True, help="base branch/ref for the new worktree")
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
    return parser.parse_args(argv)


def cli_main(argv: list[str]) -> int:
    import json

    args = _parse_args(argv)
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
