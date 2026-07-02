"""Revision-stage config reader: the `[revise]` block of workspace.toml.

Library + thin CLI. Stdlib-only.

The only knob today is `plain_comment_severity` (default `"minor"`): in revision
mode the original run's review_loop already resolved the bot threads before
delivery, so the UNRESOLVED threads `review-threads` returns ARE the maintainer's.
A plain human comment maps to `minor` and is dropped by the Major+ fix loop; this
floor lets a maintainer opt those unresolved minor threads up to `major` so they
enter the fix set. Default `minor` keeps today's behavior.

The bump is applied LOOP-SIDE via `apply_floor`, not in the forge adapter, so the
adapter (`forge_github._severity_from_state`) stays pure of `[revise]` config.

Exit codes:
  0 = ok (including the bad-value/missing-config fallback to the default; the
      review_loop bash captures the JSON, so this never exits nonzero on config).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import get_args

import _workspace
import forge

_DEFAULT_SEVERITY = "minor"
# THREAD_SEVERITY is ordered high -> low; a floor below minor (nit/unknown) would
# DEMOTE unresolved minors out of the Major+ fix set, so only minor+ is accepted.
_SEVERITY_ORDER = get_args(forge.THREAD_SEVERITY)
_VALID_FLOORS = frozenset(_SEVERITY_ORDER[: _SEVERITY_ORDER.index(_DEFAULT_SEVERITY) + 1])


def plain_comment_severity(workspace_root: Path) -> str:
    """Return the configured `[revise] plain_comment_severity` floor.

    Falls back to `"minor"` (and warns to stderr) on a missing/unparseable
    workspace.toml or an invalid/below-minor severity value, so a caller capturing
    the CLI JSON always gets a floor that can only raise.
    """
    try:
        block = _workspace.load_workspace_toml(workspace_root).get("revise", {})
    except _workspace.WorkspaceConfigError:
        return _DEFAULT_SEVERITY
    if not isinstance(block, dict):
        # a non-table `revise = "..."` at top level; treat as unconfigured
        return _DEFAULT_SEVERITY
    value = block.get("plain_comment_severity", _DEFAULT_SEVERITY)
    if value not in _VALID_FLOORS:
        sys.stderr.write(
            f"revise-config: invalid [revise] plain_comment_severity {value!r}; "
            f"falling back to {_DEFAULT_SEVERITY!r}\n"
        )
        return _DEFAULT_SEVERITY
    return value


def apply_floor(threads: list[dict], severity: str) -> list[dict]:
    """Bump every UNRESOLVED `minor` thread up to `severity`. Returns NEW dicts;
    the input is never mutated. No-op when `severity == "minor"`.

    Only unresolved minor is bumped; resolved/major/critical/nit threads pass
    through unchanged (a `nit` is below minor in the enum but the floor only
    promotes the maintainer's plain minor comments into the Major+ fix set).
    """
    if severity == _DEFAULT_SEVERITY:
        return [dict(t) for t in threads]
    out: list[dict] = []
    for t in threads:
        nt = dict(t)
        if not nt.get("resolved") and nt.get("severity") == _DEFAULT_SEVERITY:
            nt["severity"] = severity
        out.append(nt)
    return out


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read the [revise] block of workspace.toml.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("severity", help="print the configured plain_comment_severity floor")
    p.add_argument("--workspace-root", default=".")
    pa = sub.add_parser(
        "apply-floor",
        help="read a threads JSON array on stdin, bump unresolved minor to the floor, print",
    )
    pa.add_argument("--workspace-root", default=".")
    return parser.parse_args(argv)


def cli_main(argv: list[str]) -> int:
    args = _parse_args(argv)
    if args.cmd == "severity":
        value = plain_comment_severity(Path(args.workspace_root).resolve())
        sys.stdout.write(json.dumps({"plain_comment_severity": value}) + "\n")
        return 0
    if args.cmd == "apply-floor":
        try:
            threads = json.loads(sys.stdin.read() or "[]")
        except json.JSONDecodeError:
            sys.stderr.write("revise-config apply-floor: invalid threads JSON on stdin\n")
            return 1
        if not isinstance(threads, list):
            sys.stderr.write("revise-config apply-floor: expected a JSON array of threads\n")
            return 1
        floor = plain_comment_severity(Path(args.workspace_root).resolve())
        sys.stdout.write(json.dumps(apply_floor(threads, floor)) + "\n")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = ["apply_floor", "cli_main", "plain_comment_severity"]
