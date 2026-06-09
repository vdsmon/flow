"""Merge-time version-conflict recovery (Option B): auto-resolve the version-only
conflict a sibling-merge race leaves behind, re-push, leave the merge to the caller.

In a multi-bead evolve drain every PR bumps the two version files
(`plugins/flow/.claude-plugin/plugin.json` + `.claude-plugin/marketplace.json`).
As siblings merge, main's version walks forward, so a later-merging PR goes DIRTY
on the version line ONLY — the code merges clean. This helper merges the default
branch into the feature branch (HEAD) and recovers ONLY when the git-reported
conflict set is EXACTLY the two version files. Any other conflicting file → it
aborts the merge and recovers nothing ("leave for human").

It RE-PUSHES the recovered branch but NEVER merges the PR. The caller (prose:
stage-merge §3 / verb-evolve drain reap) re-waits CI on the new SHA, THEN merges —
preserving stage-merge §3's "merge ONLY the commit CI validated" invariant. The
new SHA was never CI'd, so the re-wait is non-negotiable: it catches a textually-
clean-but-semantically-wrong merge that a path-only conflict detector cannot see.

Resolution keeps OURS (the PR's content) and CONTENT-VERIFIES the conflict is
version-only: the branch and main blobs must be identical modulo the version line
(`_strip_version`). A non-version content difference inside a version file (e.g. the
PR added a manifest field main lacks) → abort, never auto-resolve — a `--theirs`
take would have silently discarded that legitimate PR change.

CLI:
  version_remerge.py recover --branch <feature/...> --workspace-root . [--cwd <path>]

Operates on a checkout whose current branch (HEAD) is the feature branch; `--cwd`
is that checkout (default ".").

Exit codes:
  0 = ok (remerged / remerged_clean; prints JSON, pushed)
  2 = tool error (a git command failed)
  3 = non-version conflict (merge aborted; leave for human)
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from _runner import CwdRunner as Runner
from _runner import cwd_default_runner as _default_runner

PLUGIN_JSON = "plugins/flow/.claude-plugin/plugin.json"
MARKETPLACE_JSON = ".claude-plugin/marketplace.json"
VERSION_FILES = frozenset({PLUGIN_JSON, MARKETPLACE_JSON})

_VERSION_RE = re.compile(r'"version"\s*:\s*"(\d+)\.(\d+)\.(\d+)"')


class ToolError(Exception):
    """A git command failed (or no version found). Exit 2."""


class NonVersionConflict(Exception):
    """The conflict set was not exactly the two version files. Exit 3."""

    def __init__(self, files: list[str]) -> None:
        super().__init__("non-version conflict; left for human")
        self.files = files


def parse_version(text: str) -> tuple[int, int, int]:
    """First `"version": "X.Y.Z"` in the text → (X, Y, Z)."""
    m = _VERSION_RE.search(text)
    if not m:
        raise ToolError("no semantic version found")
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def next_version(version: tuple[int, int, int]) -> str:
    """Deterministic patch bump: X.Y.Z → X.Y.(Z+1)."""
    major, minor, patch = version
    return f"{major}.{minor}.{patch + 1}"


def is_version_only_conflict(conflicts: set[str]) -> bool:
    """True iff the conflict set is EXACTLY the two version files (strict detector)."""
    return conflicts == set(VERSION_FILES)


def _ok(result: subprocess.CompletedProcess[str], what: str) -> str:
    if result.returncode != 0:
        raise ToolError(f"{what} failed: {result.stderr.strip()}")
    return result.stdout or ""


def _conflict_set(run: Runner) -> set[str]:
    raw = _ok(run(["git", "diff", "--name-only", "--diff-filter=U"]), "git diff")
    return {line.strip() for line in raw.splitlines() if line.strip()}


def _strip_version(text: str) -> str:
    """Normalize every version string to a placeholder, so two blobs can be compared
    modulo the version line."""
    # count=1: normalize only the FIRST (plugin/marketplace) version; a second
    # "version" field is compared literally, so a difference there trips the
    # equality check and aborts (safe) rather than being silently rewritten.
    return _VERSION_RE.sub('"version": "0.0.0"', text, count=1)


def _set_version_in_file(path: Path, next_ver: str) -> None:
    """Rewrite every `"version": "X.Y.Z"` in the file to next_ver; assert it changed."""
    text = path.read_text(encoding="utf-8")
    new_text = _VERSION_RE.sub(f'"version": "{next_ver}"', text, count=1)
    if new_text == text:
        raise ToolError(f"version replacement made no change in {path}")
    path.write_text(new_text, encoding="utf-8")


def recover(branch: str, *, cwd: Path, runner: Runner | None = None) -> dict:
    """Merge default into HEAD (the feature branch), auto-resolve a version-only
    conflict, push. Returns the JSON-able result dict. Raises NonVersionConflict
    (exit 3) on any other conflict and ToolError (exit 2) on git failure.
    """
    run = runner or _default_runner(cwd)

    default_ref = _ok(
        run(["git", "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"]),
        "git symbolic-ref",
    ).strip()
    if not default_ref:
        raise ToolError("could not resolve origin default branch")
    _ok(run(["git", "fetch", "--quiet", "origin"]), "git fetch")

    main_plugin = _ok(run(["git", "show", f"{default_ref}:{PLUGIN_JSON}"]), "git show plugin.json")
    next_ver = next_version(parse_version(main_plugin))

    merge = run(["git", "merge", default_ref, "--no-edit"])
    if merge.returncode == 0:
        head = _ok(run(["git", "rev-parse", "HEAD"]), "git rev-parse").strip()
        _ok(run(["git", "push"]), "git push")
        return {"status": "remerged_clean", "sha": head, "version": None}

    conflicts = _conflict_set(run)
    if not is_version_only_conflict(conflicts):
        _ok(run(["git", "merge", "--abort"]), "git merge --abort")
        raise NonVersionConflict(sorted(conflicts))

    for rel in (PLUGIN_JSON, MARKETPLACE_JSON):
        ours = _ok(run(["git", "show", f":2:{rel}"]), f"git show :2:{rel}")  # branch (PR) blob
        theirs = _ok(
            run(["git", "show", f":3:{rel}"]), f"git show :3:{rel}"
        )  # incoming (main) blob
        # the ONLY allowed difference is the version line; otherwise it is a real
        # (non-version) conflict INSIDE a version file -> abort, never auto-resolve.
        if _strip_version(ours) != _strip_version(theirs):
            _ok(run(["git", "merge", "--abort"]), "git merge --abort")
            raise NonVersionConflict([rel])
        (cwd / rel).write_text(ours, encoding="utf-8")  # keep the PR's content...
        _set_version_in_file(cwd / rel, next_ver)  # ...with the bumped version
        _ok(run(["git", "add", rel]), f"git add {rel}")

    remaining = _conflict_set(run)
    if remaining:
        _ok(run(["git", "merge", "--abort"]), "git merge --abort")
        raise NonVersionConflict(sorted(remaining))

    _ok(run(["git", "commit", "--no-edit"]), "git commit")
    head = _ok(run(["git", "rev-parse", "HEAD"]), "git rev-parse").strip()
    _ok(run(["git", "push"]), "git push")
    return {"status": "remerged", "sha": head, "version": next_ver}


def cli_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Merge-time version-conflict recovery.")
    sub = parser.add_subparsers(dest="command", required=True)
    rec = sub.add_parser("recover", help="re-merge default + auto-resolve a version-only conflict")
    rec.add_argument("--branch", required=True, help="the feature branch (HEAD; context only)")
    rec.add_argument("--workspace-root", default=".", help="workspace root (context only)")
    rec.add_argument("--cwd", default=".", help="the feature-branch checkout to operate on")
    args = parser.parse_args(argv)

    cwd = Path(args.cwd).resolve()
    try:
        result = recover(args.branch, cwd=cwd)
    except NonVersionConflict as exc:
        print(json.dumps({"status": "non_version_conflict", "files": exc.files}))
        return 3
    except ToolError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(cli_main(sys.argv[1:]))
