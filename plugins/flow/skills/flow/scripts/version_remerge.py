"""Merge-time version-conflict recovery (Option B): auto-resolve the version-only
conflict a sibling-merge race leaves behind, re-push, leave the merge to the caller.

The maintainer stamps the derived version into the two version files
(`plugins/flow/.claude-plugin/plugin.json` + `.claude-plugin/marketplace.json`) at
merge time (stage-merge §3), not per-PR. As siblings merge, their merge-time stamps
walk main's version forward, so a later-merging PR can go DIRTY on the version line
ONLY — the code merges clean. This helper merges the default branch into the feature
branch (HEAD) and recovers ONLY when the git-reported conflict set is EXACTLY the two
version files. Any other conflicting file → it aborts the merge and recovers nothing
("leave for human").

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

The CLEAN-merge path carries the mirror-image duplicate-stamp check (flow-5fp / live
PR #213): a sibling that walks main to the SAME version this branch stamped merges
CLEAN, invisible to the DIRTY recovery, and would land with no version walk. After a
clean merge the working tree's version is compared against main's — equal → restamp
to next-from-main, commit, push (`restamped`); different → push as-is
(`remerged_clean`). A branch that never stamped but whose tree sits at main's
version restamps too: intended, the restamp is idempotent-correct.

CLI:
  version_remerge.py recover --branch <feature/...> --workspace-root . [--cwd <path>]
    [--commit-type <type>]

Operates on a checkout whose current branch (HEAD) is the feature branch; `--cwd`
is that checkout (default "."). The re-stamp is type-aware (minor on feat, patch
otherwise); without `--commit-type` the type is resolved by scanning the branch-only
commit subjects (`origin/<default>..HEAD --no-merges`) for a feat prefix.

Exit codes:
  0 = ok (remerged / remerged_clean / restamped; prints JSON, pushed)
  2 = tool error (a git command failed; a best-effort `git merge --abort` runs
      first, so exit 2 does not leave a conflicted/mid-merge index behind — a
      post-commit push failure leaves a committed-but-unpushed merge, which is
      clean, not mid-merge)
  3 = non-version conflict (merge aborted; leave for human)
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import version
from _runner import CwdRunner as Runner
from _runner import cwd_default_runner as _default_runner

PLUGIN_JSON = "plugins/flow/.claude-plugin/plugin.json"
MARKETPLACE_JSON = ".claude-plugin/marketplace.json"
VERSION_FILES = frozenset({PLUGIN_JSON, MARKETPLACE_JSON})


class ToolError(Exception):
    """A git command failed (or no version found). Exit 2."""


class NonVersionConflict(Exception):
    """The conflict set was not exactly the two version files. Exit 3."""

    def __init__(self, files: list[str]) -> None:
        super().__init__("non-version conflict; left for human")
        self.files = files


def parse_version(text: str) -> tuple[int, int, int]:
    """First `"version": "X.Y.Z"` in the text → (X, Y, Z)."""
    m = version.VERSION_RE.search(text)
    if not m:
        raise ToolError("no semantic version found")
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def next_version(parsed: tuple[int, int, int], commit_type: str | None = None) -> str:
    """Type-aware bump (delegates to version.bump_for_type): feat → X.(Y+1).0,
    otherwise X.Y.(Z+1)."""
    return version.bump_for_type(".".join(str(p) for p in parsed), commit_type)


def _branch_commit_type(run: Runner, default_ref: str) -> str | None:
    """Flagless fallback: scan the branch-only commit subjects for a feat. The branch
    HEAD at remerge time is usually the `chore: stamp plugin version` commit, so a
    HEAD-subject read would misclassify every feat branch; any feat among
    `{default_ref}..HEAD --no-merges` resolves to feat."""
    raw = _ok(
        run(["git", "log", "--no-merges", "--format=%s", f"{default_ref}..HEAD"]),
        "git log",
    )
    subjects = [line for line in raw.splitlines() if line.strip()]
    if any(version.parse_commit_type(s) == "feat" for s in subjects):
        return "feat"
    return None


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
    return version.VERSION_RE.sub('"version": "0.0.0"', text, count=1)


def recover(
    branch: str, *, cwd: Path, commit_type: str | None = None, runner: Runner | None = None
) -> dict:
    """Merge default into HEAD (the feature branch), auto-resolve a version-only
    conflict, push. Returns the JSON-able result dict. Raises NonVersionConflict
    (exit 3) on any other conflict and ToolError (exit 2) on git failure.
    An empty/None `commit_type` falls back to the branch-only commit-subject scan.
    """
    run = runner or _default_runner(cwd)

    default_ref = _ok(
        run(["git", "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"]),
        "git symbolic-ref",
    ).strip()
    if not default_ref:
        raise ToolError("could not resolve origin default branch")
    _ok(run(["git", "fetch", "--quiet", "origin"]), "git fetch")
    if not commit_type:
        commit_type = _branch_commit_type(run, default_ref)

    main_plugin = _ok(run(["git", "show", f"{default_ref}:{PLUGIN_JSON}"]), "git show plugin.json")
    next_ver = next_version(parse_version(main_plugin), commit_type)

    merge = run(["git", "merge", default_ref, "--no-edit"])
    if merge.returncode == 0:
        # duplicate-stamp check (flow-5fp): a sibling that walked main to the SAME
        # version this branch stamped merges CLEAN (identical content on both sides
        # of the version line), so the conflict path below never sees it. Equal
        # versions -> restamp to next-from-main. A never-stamped branch whose tree
        # sits at main's version restamps too: intended, the restamp is
        # idempotent-correct. No merge --abort on this path: the clean merge
        # already committed, a later failure never leaves a mid-merge index.
        try:
            tree_plugin = (cwd / PLUGIN_JSON).read_text(encoding="utf-8")
        except OSError as exc:
            raise ToolError(f"read {PLUGIN_JSON} failed: {exc}") from exc
        if parse_version(tree_plugin) == parse_version(main_plugin):
            try:
                version.write_version(cwd=cwd, version=next_ver)
            except OSError as exc:
                raise ToolError(f"write version files failed: {exc}") from exc
            for rel in (PLUGIN_JSON, MARKETPLACE_JSON):
                _ok(run(["git", "add", rel]), f"git add {rel}")
            _ok(
                run(
                    [
                        "git",
                        "commit",
                        "-m",
                        "chore: stamp plugin version",
                        "--",
                        PLUGIN_JSON,
                        MARKETPLACE_JSON,
                    ]
                ),
                "git commit",
            )
            head = _ok(run(["git", "rev-parse", "HEAD"]), "git rev-parse").strip()
            _ok(run(["git", "push"]), "git push")
            return {
                "status": "restamped",
                "sha": head,
                "version": next_ver,
                "bump": "minor" if commit_type == "feat" else "patch",
                "commit_type": commit_type,
            }
        head = _ok(run(["git", "rev-parse", "HEAD"]), "git rev-parse").strip()
        _ok(run(["git", "push"]), "git push")
        return {"status": "remerged_clean", "sha": head, "version": None}

    conflicts = _conflict_set(run)
    if not is_version_only_conflict(conflicts):
        _ok(run(["git", "merge", "--abort"]), "git merge --abort")
        raise NonVersionConflict(sorted(conflicts))

    try:
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
            (cwd / rel).write_text(ours, encoding="utf-8")  # keep the PR's content

        version.write_version(cwd=cwd, version=next_ver)  # then bump the version in both files
        for rel in (PLUGIN_JSON, MARKETPLACE_JSON):
            _ok(run(["git", "add", rel]), f"git add {rel}")

        remaining = _conflict_set(run)
        if remaining:
            _ok(run(["git", "merge", "--abort"]), "git merge --abort")
            raise NonVersionConflict(sorted(remaining))

        _ok(run(["git", "commit", "--no-edit"]), "git commit")
    except (ToolError, version.ToolError):
        # never leave the worktree mid-merge (flow-wkn): best-effort abort, then
        # propagate the original error. Bare run (not _ok) so a failing abort
        # cannot mask it.
        run(["git", "merge", "--abort"])
        raise
    except OSError as exc:
        # a raw working-tree read/write failure (e.g. disk full) gets the same
        # abort, wrapped so cli_main maps it to exit 2 instead of a traceback.
        run(["git", "merge", "--abort"])
        raise ToolError(f"working-tree write failed: {exc}") from exc
    head = _ok(run(["git", "rev-parse", "HEAD"]), "git rev-parse").strip()
    _ok(run(["git", "push"]), "git push")
    return {
        "status": "remerged",
        "sha": head,
        "version": next_ver,
        "bump": "minor" if commit_type == "feat" else "patch",
        "commit_type": commit_type,
    }


def cli_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Merge-time version-conflict recovery.")
    sub = parser.add_subparsers(dest="command", required=True)
    rec = sub.add_parser("recover", help="re-merge default + auto-resolve a version-only conflict")
    rec.add_argument("--branch", required=True, help="the feature branch (HEAD; context only)")
    rec.add_argument("--workspace-root", default=".", help="workspace root (context only)")
    rec.add_argument("--cwd", default=".", help="the feature-branch checkout to operate on")
    rec.add_argument(
        "--commit-type",
        default="",
        help="conventional-commit type (feat → minor bump); empty → branch commit scan",
    )
    args = parser.parse_args(argv)

    cwd = Path(args.cwd).resolve()
    try:
        result = recover(args.branch, cwd=cwd, commit_type=args.commit_type)
    except NonVersionConflict as exc:
        print(json.dumps({"status": "non_version_conflict", "files": exc.files}))
        return 3
    except (ToolError, version.ToolError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(cli_main(sys.argv[1:]))
