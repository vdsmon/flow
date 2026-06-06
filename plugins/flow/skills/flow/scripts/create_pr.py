"""Open (or resolve) a PR for the run's feature branch, via the forge seam.

The `create_pr` stage handler. Git mechanics (push, protected-branch refusal, title
from the HEAD commit) live here; the host calls (detect/open PR) go through the
pluggable forge seam (`forge.py`), so this same handler serves GitHub (`gh`) and
Bitbucket (`bkt`) workspaces. Wired as `create_pr = "inline"` in the dogfood
workspace and requires a `[forge]` block; other workspaces keep `create_pr = "none"`.
PRs open ready for review by default; set `[create_pr] draft = true` in
`workspace.toml` (or pass `--draft`) to open drafts.

Idempotent on resume: if a PR already exists for the branch it returns that URL
instead of erroring, so a re-run after a crash does not double-open. The title comes
from the HEAD (work) commit subject, which the commit stage built from
`commit_summary`, so there is no `pr_title` field to populate — do NOT add a
lint_ticket gate for it.

Prints `PR_URL=<url>` on stdout; the do-loop captures that into
`.flow/runs/<KEY>/stages/create_pr.out`, where the final summary + the review_loop
notification read the `PR_URL=` token.

CLI:
  create_pr.py --workspace-root <dir> [--base main] [--ticket KEY] [--draft]

Exit codes:
  0 = ok (prints PR_URL=<url>)
  2 = tool error (git/gh failed; stderr propagated)
  3 = refused (current branch is a protected/integration branch — never PR from it)
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from _runner import CwdRunner as Runner
from _runner import cwd_default_runner as _default_runner
from _workspace import WorkspaceConfigError, load_workspace_toml
from forge import Forge, ForgeError, make_forge, read_forge_config

_PROTECTED = {"main", "master", "dev", "develop"}


def _draft_config(workspace_root: Path) -> bool:
    """`[create_pr] draft` from workspace.toml (bool); default False (open, ready)."""
    try:
        config = load_workspace_toml(workspace_root)
    except WorkspaceConfigError:
        return False
    section = config.get("create_pr")
    if not isinstance(section, dict):
        return False
    value = section.get("draft")
    return value if isinstance(value, bool) else False


class ToolError(Exception):
    """git/gh failed. Exit 2."""


class RefusedBranch(Exception):
    """Current branch is protected; never open a PR from it. Exit 3."""


def _ok(result: subprocess.CompletedProcess[str], what: str) -> str:
    if result.returncode != 0:
        raise ToolError(f"{what} failed: {result.stderr.strip()}")
    return result.stdout or ""


def open_or_get_pr(
    workspace_root: Path,
    *,
    base: str = "main",
    draft: bool = False,
    runner: Runner | None = None,
    forge: Forge | None = None,
) -> str:
    """Push the run's branch and return its PR URL, opening one if absent.

    Git mechanics (rev-parse, protected-branch refusal, push, title from the HEAD
    commit) stay here; the host calls (detect/open PR) go through the forge seam, so
    this same handler serves GitHub and Bitbucket. Opens ready-for-review by default;
    `draft=True` opens a draft PR. `forge` is injectable for tests.
    """
    run = runner or _default_runner(workspace_root)
    branch = _ok(run(["git", "rev-parse", "--abbrev-ref", "HEAD"]), "git rev-parse").strip()
    if not branch or branch in _PROTECTED:
        raise RefusedBranch(f"refusing to open a PR from protected branch {branch!r}")

    _ok(run(["git", "push", "-u", "origin", branch]), "git push")

    fg = forge if forge is not None else _resolve_forge(workspace_root)

    try:
        existing = fg.detect_pr(branch)
        if existing:
            return str(existing["url"])

        # title from the HEAD (work) commit, which the commit stage built from
        # commit_summary. Not `gh --fill`: a branch cut off a non-main base carries
        # already-merged commits, and --fill then mistitles from the branch name.
        subject = _ok(run(["git", "log", "-1", "--format=%s"]), "git log").strip()
        body = _ok(run(["git", "log", "-1", "--format=%b"]), "git log").strip()
        pr = fg.open_pr(base, branch, subject, body or subject, draft)
    except ForgeError as exc:
        raise ToolError(str(exc)) from exc
    return str(pr["url"])


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
    parser.add_argument("--base", default="main")
    parser.add_argument("--ticket", default=None)  # context only
    parser.add_argument(
        "--draft",
        action="store_true",
        default=None,
        help="open a draft PR (overrides the [create_pr] draft workspace setting).",
    )
    args = parser.parse_args(argv)
    ws = Path(args.workspace_root)
    draft = args.draft if args.draft is not None else _draft_config(ws)
    try:
        url = open_or_get_pr(ws, base=args.base, draft=draft)
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
