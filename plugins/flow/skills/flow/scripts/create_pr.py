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
  create_pr.py --workspace-root <dir> [--base BRANCH] [--ticket KEY] [--draft] [--body-file PATH]

The base branch resolves as: explicit `--base`, else `[create_pr] base` in
`workspace.toml`, else `main`.

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

import pr_body
from _runner import CwdRunner as Runner
from _runner import cwd_default_runner as _default_runner
from _workspace import WorkspaceConfigError, load_workspace_toml
from forge import Forge, ForgeError, NotSupported, make_forge, read_forge_config

_PROTECTED = {"main", "master", "dev", "develop"}


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


class ToolError(Exception):
    """git/gh failed. Exit 2."""


class RefusedBranch(Exception):
    """Current branch is protected; never open a PR from it. Exit 3."""


def _ok(result: subprocess.CompletedProcess[str], what: str) -> str:
    if result.returncode != 0:
        raise ToolError(f"{what} failed: {result.stderr.strip()}")
    return result.stdout or ""


def _compose_body(raw: str, subject: str, body_file: Path | None) -> str:
    """The PR body passed to open_pr.

    With `body_file`: the authored markdown (de-AI scrubbed as a floor) plus the
    deterministic `Closes` footer from the commit trailer. Without it: the
    commit-derived fallback (build_body + scrub). Empty prose falls back to the
    commit subject. Both real-body paths pass through `enforce_cap`, the
    deterministic size net so an oversized `## Evidence` body cannot fail open_pr.
    """
    if body_file is None:
        return pr_body.enforce_cap(pr_body.scrub(pr_body.build_body(raw)).strip() or subject)
    try:
        authored = body_file.read_text()
    except OSError as exc:
        raise ToolError(f"--body-file {body_file} unreadable: {exc}") from exc
    body = pr_body.scrub(authored).strip()
    if not body:
        return subject
    footer = pr_body.closes_footer(raw)
    return pr_body.enforce_cap(f"{body}\n\n{footer}" if footer else body)


def open_or_get_pr(
    workspace_root: Path,
    *,
    base: str = "main",
    draft: bool = True,
    body_file: Path | None = None,
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
        body = _compose_body(raw, subject, body_file)
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
        "--body-file",
        default=None,
        help=(
            "path to an authored PR body (markdown); the Closes footer is appended "
            "and a de-AI scrub applied. Absent = derive the body from the commit."
        ),
    )
    args = parser.parse_args(argv)
    ws = Path(args.workspace_root)
    draft = args.draft if args.draft is not None else _draft_config(ws)
    base = args.base if args.base is not None else (_base_config(ws) or "main")
    body_file = Path(args.body_file) if args.body_file else None
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
