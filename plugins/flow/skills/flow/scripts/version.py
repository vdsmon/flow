"""Plugin version-derivation seam: compute the next patch version from a git ref.

Derivation basis (maintainer-decided): read the current plugin version on a ref
(default `origin/main`), bump PATCH +1, preserve MAJOR.MINOR. Single source so the
epic can lift the same number to merge time instead of hand-bumping it per PR.

This is the keystone seam of epic flow-6gx and ships ahead of its callers: child
flow-6gx.2 drops the version files off the per-PR content path, child flow-6gx.4
stamps the derived version at merge time. Until then it has no callers by design.

CLI:
  version.py next [--ref origin/main] [--cwd .]
  prints JSON {"ref", "current", "next"} to stdout.

Exit codes:
  0 = ok
  2 = tool error (a git command failed, or no version field)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from _runner import CwdRunner as Runner
from _runner import cwd_default_runner as _default_runner

PLUGIN_JSON = "plugins/flow/.claude-plugin/plugin.json"
MARKETPLACE_JSON = ".claude-plugin/marketplace.json"

__all__ = ["ToolError", "bump_patch", "cli_main", "compute", "read_version"]


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


def compute(*, cwd: Path, ref: str | None = "origin/main", runner: Runner | None = None) -> dict:
    """{"ref", "current", "next"} for the version on `ref`."""
    current = read_version(cwd=cwd, ref=ref, runner=runner)
    return {"ref": ref, "current": current, "next": bump_patch(current)}


def cli_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Derive the next plugin version.")
    sub = parser.add_subparsers(dest="command", required=True)
    nxt = sub.add_parser("next", help="print the next patch version from a ref")
    nxt.add_argument(
        "--ref", default="origin/main", help="git ref to read the current version from"
    )
    nxt.add_argument("--cwd", default=".", help="repo checkout to read in")
    args = parser.parse_args(argv)

    cwd = Path(args.cwd).resolve()
    try:
        result = compute(cwd=cwd, ref=args.ref)
    except ToolError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(cli_main(sys.argv[1:]))
