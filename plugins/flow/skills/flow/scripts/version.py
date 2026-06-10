"""Plugin version-derivation seam: compute the next semantic version from a git ref.

Derivation basis (maintainer-decided): read the current plugin version on a ref
(default `origin/main`), bump MINOR on a `feat` commit type (X.(Y+1).0) and PATCH
otherwise. The type comes from an explicit `--commit-type` flag (the merge prose
feeds it from the ticket frontmatter), falling back to the HEAD commit subject's
conventional-commit prefix, falling back to patch. Single source so the epic can
lift the same number to merge time instead of hand-bumping it per PR.

Keystone seam of epic flow-6gx: the per-PR version bump is gone, and `stamp` writes
the derived version into both version files at merge time (`references/stage-merge.md`
§3). `write_version` does the surgical line-replace that preserves JSON formatting.

CLI:
  version.py next [--ref origin/main] [--cwd .] [--commit-type <type>]
  prints JSON {"ref", "current", "next", "bump", "commit_type"} to stdout.

  version.py stamp [--ref origin/main] [--cwd .] [--commit-type <type>]
  computes the next version, writes it into both version files, prints the same JSON.

Exit codes:
  0 = ok
  2 = tool error (a git command failed, or no version field)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from _runner import CwdRunner as Runner
from _runner import cwd_default_runner as _default_runner

PLUGIN_JSON = "plugins/flow/.claude-plugin/plugin.json"
MARKETPLACE_JSON = ".claude-plugin/marketplace.json"

VERSION_RE = re.compile(r'"version"\s*:\s*"(\d+)\.(\d+)\.(\d+)"')

COMMIT_TYPE_RE = re.compile(r"^([a-z]+)(?:\([^)]*\))?!?:")

__all__ = [
    "COMMIT_TYPE_RE",
    "VERSION_RE",
    "ToolError",
    "bump_for_type",
    "bump_minor",
    "bump_patch",
    "cli_main",
    "compute",
    "head_commit_type",
    "parse_commit_type",
    "read_version",
    "stamp",
    "write_version",
]


class ToolError(Exception):
    """A git command failed, or the version field was missing. Exit 2."""


def _ok(result, what: str) -> str:
    if result.returncode != 0:
        raise ToolError(f"{what} failed: {result.stderr.strip()}")
    return result.stdout or ""


def bump_patch(version: str) -> str:
    """Parse MAJOR.MINOR.PATCH and return MAJOR.MINOR.(PATCH+1)."""
    parts = version.split(".")
    if len(parts) != 3:
        raise ValueError(f"not a MAJOR.MINOR.PATCH version: {version!r}")
    major, minor, patch = (int(p) for p in parts)
    return f"{major}.{minor}.{patch + 1}"


def bump_minor(version: str) -> str:
    """Parse MAJOR.MINOR.PATCH and return MAJOR.(MINOR+1).0."""
    parts = version.split(".")
    if len(parts) != 3:
        raise ValueError(f"not a MAJOR.MINOR.PATCH version: {version!r}")
    major, minor, _patch = (int(p) for p in parts)
    return f"{major}.{minor + 1}.0"


def bump_for_type(version: str, commit_type: str | None) -> str:
    """MINOR bump on a `feat` commit type, PATCH otherwise."""
    return bump_minor(version) if commit_type == "feat" else bump_patch(version)


def parse_commit_type(subject: str) -> str | None:
    """Conventional-commit type token of a commit subject, or None when the subject
    has no `type(scope)?: ` prefix. `feat(queue): x` and `feat!: x` both parse to
    `feat`; a major/BREAKING-CHANGE bump is deliberately out of scope, so `feat!`
    still drives a minor bump."""
    m = COMMIT_TYPE_RE.match(subject)
    return m.group(1) if m else None


def head_commit_type(*, cwd: Path, runner: Runner | None = None) -> str | None:
    """The conventional-commit type of the HEAD commit subject, or None."""
    run = runner or _default_runner(cwd)
    subject = _ok(run(["git", "log", "-1", "--format=%s", "HEAD"]), "git log").strip()
    return parse_commit_type(subject)


def read_version(
    *, cwd: Path, ref: str | None = "origin/main", runner: Runner | None = None
) -> str:
    """The plugin.json `version` field at a git ref (or the working tree when ref is None)."""
    if ref is None:
        text = (cwd / PLUGIN_JSON).read_text(encoding="utf-8")
    else:
        run = runner or _default_runner(cwd)
        text = _ok(run(["git", "show", f"{ref}:{PLUGIN_JSON}"]), "git show plugin.json")
    version = json.loads(text).get("version")
    if not isinstance(version, str):
        raise ToolError("plugin.json has no string version field")
    return version


def compute(
    *,
    cwd: Path,
    ref: str | None = "origin/main",
    runner: Runner | None = None,
    commit_type: str | None = None,
) -> dict:
    """{"ref", "current", "next", "bump", "commit_type"} for the version on `ref`.
    An empty/None `commit_type` falls back to the HEAD commit subject's type."""
    resolved = commit_type or head_commit_type(cwd=cwd, runner=runner)
    current = read_version(cwd=cwd, ref=ref, runner=runner)
    return {
        "ref": ref,
        "current": current,
        "next": bump_for_type(current, resolved),
        "bump": "minor" if resolved == "feat" else "patch",
        "commit_type": resolved,
    }


def _set_version_in_file(path: Path, version: str) -> None:
    """Replace the first `"version": "X.Y.Z"` in the file, preserving the rest byte-for-byte.
    Already at the target version is a benign no-op (flow-wkn), not an error."""
    text = path.read_text(encoding="utf-8")
    if not VERSION_RE.search(text):
        raise ToolError(f"no version line to replace in {path}")
    new_text = VERSION_RE.sub(f'"version": "{version}"', text, count=1)
    if new_text != text:
        path.write_text(new_text, encoding="utf-8")


def write_version(*, cwd: Path, version: str, runner: Runner | None = None) -> None:
    """Surgically set `version` in both version files (plugin.json top-level + the
    marketplace flow entry), preserving surrounding JSON formatting. Each file has
    exactly one `"version":` line; a regex line-replace keeps the rest intact."""
    _set_version_in_file(cwd / PLUGIN_JSON, version)
    _set_version_in_file(cwd / MARKETPLACE_JSON, version)


def stamp(
    *,
    cwd: Path,
    ref: str = "origin/main",
    runner: Runner | None = None,
    commit_type: str | None = None,
) -> dict:
    """Compute the next version from `ref` and write it into both version files.
    Returns the compute dict {"ref", "current", "next", "bump", "commit_type"}."""
    result = compute(cwd=cwd, ref=ref, runner=runner, commit_type=commit_type)
    write_version(cwd=cwd, version=result["next"], runner=runner)
    return result


def cli_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Derive (and optionally stamp) the plugin version."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    nxt = sub.add_parser("next", help="print the next version from a ref")
    nxt.add_argument(
        "--ref", default="origin/main", help="git ref to read the current version from"
    )
    nxt.add_argument("--cwd", default=".", help="repo checkout to read in")
    nxt.add_argument(
        "--commit-type",
        default="",
        help="conventional-commit type (feat → minor bump); empty → HEAD subject fallback",
    )
    stp = sub.add_parser("stamp", help="write the next version into both version files")
    stp.add_argument(
        "--ref", default="origin/main", help="git ref to read the current version from"
    )
    stp.add_argument("--cwd", default=".", help="repo checkout to write in")
    stp.add_argument(
        "--commit-type",
        default="",
        help="conventional-commit type (feat → minor bump); empty → HEAD subject fallback",
    )
    args = parser.parse_args(argv)

    cwd = Path(args.cwd).resolve()
    try:
        if args.command == "stamp":
            result = stamp(cwd=cwd, ref=args.ref, commit_type=args.commit_type)
        else:
            result = compute(cwd=cwd, ref=args.ref, commit_type=args.commit_type)
    except ToolError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(cli_main(sys.argv[1:]))
