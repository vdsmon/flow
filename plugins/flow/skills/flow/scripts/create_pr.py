"""Open (or resolve) a PR for the run's feature branch, via the forge seam.

The `create_pr` stage handler. Git mechanics (push, protected-branch refusal, title
from the HEAD commit) live here; the host calls (detect/open PR) go through the
pluggable forge seam (`forge.py`), so this same handler serves GitHub (`gh`) and
Bitbucket (`bkt`) workspaces. Wired as `create_pr = "inline"` in the dogfood
workspace and requires a `[forge]` block; other workspaces keep `create_pr = "none"`.
PRs open as drafts by default; set `[create_pr] draft = false` in
`workspace.toml` to open ready for review (`--draft` forces a draft).

Idempotent on resume: if a PR already exists for the branch it returns that URL
instead of erroring, so a re-run after a crash does not double-open. The title comes
from the HEAD (work) commit subject, which the commit stage built from
`commit_summary`, so there is no `pr_title` field to populate. Do NOT add a
lint_ticket gate for it.

Prints `PR_URL=<url>` on stdout; the do-loop captures that into
`.flow/runs/<KEY>/stages/create_pr.out`, where the final summary + the review_loop
notification read the `PR_URL=` token.

CLI:
  create_pr.py --workspace-root <dir> [--base BRANCH] [--ticket KEY] [--draft] [--hotfix]
               [--body-file PATH]

The base branch resolves as: explicit `--base`, else `[create_pr] base` in
`workspace.toml`, else `main`. `--hotfix` (hotfix-lane run) instead opens ready for
review against the remote default branch, ignoring the `[create_pr]` base and draft
settings: a hotfix always targets what production builds from, even in a workspace
whose ordinary PRs stack on an integration branch. `--base` with `--hotfix` is
refused outright (a hotfix base is not negotiable; witnessed 2026-08-14, a driver
"corrected" a hotfix PR onto the workspace integration branch and the fix had to be
re-cut). An explicit `--draft` still wins over `--hotfix`.

Exit codes:
  0 = ok (prints PR_URL=<url>)
  2 = tool error (git/gh failed; stderr propagated)
  3 = refused (current branch is a protected/integration branch, never PR from it)
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import _runner
import _workspace
import pr_body
from _runner import CwdRunner as Runner
from _runner import cwd_default_runner as _default_runner
from _workspace import WorkspaceConfigError, load_workspace_toml
from forge import Forge, ForgeError, NotSupported, make_forge, read_forge_config

_PROTECTED = _workspace.PROTECTED_BRANCHES


def _draft_config(workspace_root: Path) -> bool:
    """`[create_pr] draft` from workspace.toml (bool); default True (open as draft)."""
    try:
        config = load_workspace_toml(workspace_root)
    except WorkspaceConfigError:
        return True
    section = config.get("create_pr")
    if not isinstance(section, dict):
        return True
    value = section.get("draft")
    return value if isinstance(value, bool) else True


def _base_config(workspace_root: Path) -> str | None:
    """`[create_pr] base` from workspace.toml (non-empty str); None falls back to main."""
    try:
        config = load_workspace_toml(workspace_root)
    except WorkspaceConfigError:
        return None
    section = config.get("create_pr")
    if not isinstance(section, dict):
        return None
    value = section.get("base")
    return value if isinstance(value, str) and value else None


def _remote_default_branch(workspace_root: Path, runner: Runner | None = None) -> str:
    """The short remote default branch name (`origin/HEAD` minus the prefix), else `main`.

    The hotfix base: what production builds from. No fetch here; the run's worktree
    was cut off a freshly-fetched `@default`, so the local `origin/HEAD` ref is at
    most minutes old, and a wrong-but-plausible fallback of `main` fails loudly at
    PR open on repos that use another name.
    """
    run = runner or _default_runner(workspace_root)
    result = run(["git", "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"])
    name = result.stdout.strip() if result.returncode == 0 else ""
    return name.removeprefix("origin/") or "main"


class ToolError(Exception):
    """git/gh failed. Exit 2."""


class RefusedBranch(Exception):
    """Current branch is protected; never open a PR from it. Exit 3."""


def _ok(result: subprocess.CompletedProcess[str], what: str) -> str:
    return _runner.checked(result, what, ToolError, stderr_only=True)


def _compose_body(raw: str, subject: str, body_file: Path, *, flatten: bool = False) -> str:
    """The PR body passed to open_pr.

    Delegates to `pr_body.compose`, the one compose path shared with `forge update-body`: scrub
    floor, `<details>` flatten on a bitbucket forge, the deterministic `Closes` footer from the
    commit trailer, and the `enforce_cap` size net. Empty prose falls back to the commit subject.
    """
    try:
        authored = body_file.read_text()
    except OSError as exc:
        raise ToolError(f"--body-file {body_file} unreadable: {exc}") from exc
    return pr_body.compose(authored, raw, flatten=flatten) or subject


def open_or_get_pr(
    workspace_root: Path,
    *,
    base: str = "main",
    draft: bool = True,
    body_file: Path,
    runner: Runner | None = None,
    forge: Forge | None = None,
) -> str:
    """Push the run's branch and return its PR URL, opening one if absent.

    Git mechanics (rev-parse, protected-branch refusal, push, title from the HEAD
    commit) stay here; the host calls (detect/open PR) go through the forge seam, so
    this same handler serves GitHub and Bitbucket. Opens a draft by default;
    `draft=False` opens ready for review. `forge` is injectable for tests.
    """
    run = runner or _default_runner(workspace_root)
    branch = _ok(run(["git", "rev-parse", "--abbrev-ref", "HEAD"]), "git rev-parse").strip()
    if branch == "HEAD":
        # a detached HEAD rev-parses to the literal "HEAD", which would push
        # refs/heads/HEAD and PR from a remote branch named HEAD.
        raise RefusedBranch("refusing to open a PR from a detached HEAD (no run branch)")
    if not branch or branch in _PROTECTED:
        raise RefusedBranch(f"refusing to open a PR from protected branch {branch!r}")

    _ok(run(["git", "push", "-u", "origin", f"{branch}:refs/heads/{branch}"]), "git push")

    fg = forge if forge is not None else _resolve_forge(workspace_root)

    try:
        existing = fg.detect_pr(branch)
        if existing:
            return str(existing["url"])

        # title from the HEAD (work) commit, which the commit stage built from
        # commit_summary. Not `gh --fill`: a branch cut off a non-main base carries
        # already-merged commits, and --fill then mistitles from the branch name.
        subject = _ok(run(["git", "log", "-1", "--format=%s"]), "git log").strip()
        raw = _ok(run(["git", "log", "-1", "--format=%b"]), "git log")
        body = _compose_body(raw, subject, body_file, flatten=fg.backend == "bitbucket")
        pr = fg.open_pr(base, branch, subject, body, draft)
    except ForgeError as exc:
        raise ToolError(str(exc)) from exc
    # Set-on-open only: open_or_get_pr early-returns on an existing PR, so reviewers
    # apply on the first open. A reviewer-API failure must NEVER fail an open PR.
    _set_reviewers(fg, pr["id"])
    return str(pr["url"])


def _set_reviewers(fg: Forge, pr_id: str) -> None:
    """Attach default reviewers; swallow NotSupported (host degrade) AND any other
    ForgeError (a reviewer-API hiccup never fails an otherwise-open PR)."""
    try:
        fg.set_default_reviewers(pr_id)
    except NotSupported:
        print(
            f"create_pr: forge does not set default reviewers; skipping ({pr_id})", file=sys.stderr
        )
    except ForgeError as exc:
        print(f"create_pr: set default reviewers failed for {pr_id}: {exc}", file=sys.stderr)


def _resolve_forge(workspace_root: Path) -> Forge:
    """Build the workspace's forge adapter; an inline create_pr requires `[forge]`."""
    try:
        config = read_forge_config(workspace_root)
        if config is None:
            raise ToolError("inline create_pr requires a [forge] block in workspace.toml")
        return make_forge(config)
    except ForgeError as exc:
        raise ToolError(str(exc)) from exc


def cli_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Open or resolve a PR for the run branch.")
    parser.add_argument("--workspace-root", required=True)
    parser.add_argument(
        "--base",
        default=None,
        help="PR base branch (overrides the [create_pr] base workspace setting; default main).",
    )
    parser.add_argument("--ticket", default=None)  # context only
    parser.add_argument(
        "--draft",
        action="store_true",
        default=None,
        help="open a draft PR (overrides the [create_pr] draft workspace setting).",
    )
    parser.add_argument(
        "--hotfix",
        action="store_true",
        help=(
            "hotfix-lane run: open ready for review against the remote default branch, "
            "ignoring the [create_pr] base/draft settings (--base is refused, explicit "
            "--draft still wins)."
        ),
    )
    parser.add_argument(
        "--body-file",
        required=True,
        help=(
            "path to the authored PR body (markdown); the Closes footer is appended "
            "and a de-AI scrub applied."
        ),
    )
    args = parser.parse_args(argv)
    ws = Path(args.workspace_root)
    if args.hotfix:
        if args.base is not None:
            # A hotfix targets what production builds from, full stop. A workspace
            # "PRs target the integration branch" convention never applies to it.
            parser.error(
                "--base conflicts with --hotfix: a hotfix always targets the remote default branch"
            )
        draft = args.draft if args.draft is not None else False
        base = _remote_default_branch(ws)
    else:
        draft = args.draft if args.draft is not None else _draft_config(ws)
        base = args.base if args.base is not None else (_base_config(ws) or "main")
    body_file = Path(args.body_file)
    try:
        url = open_or_get_pr(ws, base=base, draft=draft, body_file=body_file)
    except RefusedBranch as exc:
        print(str(exc), file=sys.stderr)
        return 3
    except ToolError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"PR_URL={url}")
    return 0


if __name__ == "__main__":
    sys.exit(cli_main(sys.argv[1:]))
