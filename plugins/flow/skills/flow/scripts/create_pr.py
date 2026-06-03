"""Open (or resolve) a GitHub PR for the run's feature branch.

The `create_pr` stage handler for GitHub workspaces. The bare flow plugin ships no
inline create_pr (PR mechanics are platform-specific); this is flow's own GitHub
handler, wired as `create_pr = "inline"` in the dogfood workspace. Other workspaces
keep `create_pr = "none"` and never invoke it. PRs open ready for review (not draft).

Idempotent on resume: if a PR already exists for the branch it returns that URL
instead of erroring, so a re-run after a crash does not double-open. The title comes
from the HEAD (work) commit subject, which the commit stage built from
`commit_summary`, so there is no `pr_title` field to populate — do NOT add a
lint_ticket gate for it.

Prints `PR_URL=<url>` on stdout; the do-loop captures that into
`.flow/runs/<KEY>/stages/create_pr.out`, where the final summary + the review_loop
notification read the `PR_URL=` token.

CLI:
  create_pr.py --workspace-root <dir> [--base main] [--ticket KEY]

Exit codes:
  0 = ok (prints PR_URL=<url>)
  2 = tool error (git/gh failed; stderr propagated)
  3 = refused (current branch is a protected/integration branch — never PR from it)
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]

_PROTECTED = {"main", "master", "dev", "develop"}


class ToolError(Exception):
    """git/gh failed. Exit 2."""


class RefusedBranch(Exception):
    """Current branch is protected; never open a PR from it. Exit 3."""


def _default_runner(repo: Path) -> Runner:
    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(args, cwd=str(repo), capture_output=True, text=True, check=False)

    return run


def _ok(result: subprocess.CompletedProcess[str], what: str) -> str:
    if result.returncode != 0:
        raise ToolError(f"{what} failed: {result.stderr.strip()}")
    return result.stdout or ""


def _existing_pr_url(branch: str, runner: Runner) -> str | None:
    """URL of an open PR already targeting this branch, or None."""
    raw = _ok(
        runner(
            [
                "gh",
                "pr",
                "list",
                "--head",
                branch,
                "--state",
                "open",
                "--json",
                "url",
                "--limit",
                "1",
            ]
        ),
        "gh pr list",
    )
    try:
        items = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return None
    if isinstance(items, list) and items and isinstance(items[0], dict):
        url = items[0].get("url")
        return str(url) if url else None
    return None


def open_or_get_pr(
    workspace_root: Path, *, base: str = "main", runner: Runner | None = None
) -> str:
    """Push the run's branch and return its PR URL, opening one (ready) if absent."""
    run = runner or _default_runner(workspace_root)
    branch = _ok(run(["git", "rev-parse", "--abbrev-ref", "HEAD"]), "git rev-parse").strip()
    if not branch or branch in _PROTECTED:
        raise RefusedBranch(f"refusing to open a PR from protected branch {branch!r}")

    _ok(run(["git", "push", "-u", "origin", branch]), "git push")

    existing = _existing_pr_url(branch, run)
    if existing:
        return existing

    # title from the HEAD (work) commit, which the commit stage built from
    # commit_summary. Not `gh --fill`: a branch cut off a non-main base carries
    # already-merged commits, and --fill then mistitles from the branch name.
    subject = _ok(run(["git", "log", "-1", "--format=%s"]), "git log").strip()
    body = _ok(run(["git", "log", "-1", "--format=%b"]), "git log").strip()
    out = _ok(
        run(
            [
                "gh",
                "pr",
                "create",
                "--base",
                base,
                "--head",
                branch,
                "--title",
                subject,
                "--body",
                body or subject,
            ],
        ),
        "gh pr create",
    )
    # gh prints the PR URL as the last non-empty stdout line
    url = next((ln.strip() for ln in reversed(out.splitlines()) if ln.strip()), "")
    if not url:
        # re-resolve rather than fail: the PR may have been created despite no URL echo
        url = _existing_pr_url(branch, run) or ""
    if not url:
        raise ToolError("gh pr create returned no URL and none is resolvable")
    return url


def cli_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Open or resolve a PR for the run branch.")
    parser.add_argument("--workspace-root", required=True)
    parser.add_argument("--base", default="main")
    parser.add_argument("--ticket", default=None)  # context only
    args = parser.parse_args(argv)
    try:
        url = open_or_get_pr(Path(args.workspace_root), base=args.base)
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
