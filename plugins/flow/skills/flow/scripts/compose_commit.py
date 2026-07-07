"""Skeleton conventional-commit message emitter.

Library + thin CLI. Stdlib-only.

Header is deterministic. Body section is a template for the LLM (or human) to
fill in.

Exit codes:
  0 = ok
  1 = empty/whitespace --summary or --ticket
  2 = invalid --type or missing required flag (argparse usage error)
"""

from __future__ import annotations

import argparse
import sys

VALID_TYPES: tuple[str, ...] = (
    "feat",
    "fix",
    "chore",
    "docs",
    "refactor",
    "test",
    "perf",
    "style",
    "build",
    "ci",
    "revert",
)


def compose(
    ticket: str,
    type_: str,
    summary: str,
    scope: str | None = None,
    files: list[str] | None = None,
    covers: list[str] | None = None,
) -> str:
    if type_ not in VALID_TYPES:
        raise ValueError(f"invalid commit type {type_!r}; valid: {VALID_TYPES}")
    if not summary.strip():
        raise ValueError("summary must be non-empty")
    if not ticket.strip():
        raise ValueError("ticket must be non-empty")
    header = f"{type_}({scope}): {summary}" if scope else f"{type_}: {summary}"
    lines: list[str] = [header, "", f"ticket: {ticket}"]
    # covers: sibling tickets co-delivered by this one run. One `Closes <KEY>`
    # footer per cover so the PR body lists every ticket the run satisfies.
    lines.extend(f"Closes {cover.strip()}" for cover in covers or [] if cover.strip())
    if files:
        lines.append("files:")
        lines.extend(f"  - {f}" for f in files)
    lines.extend(["", "# body — fill in below this line"])
    return "\n".join(lines) + "\n"


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Skeleton conventional-commit message emitter.")
    parser.add_argument("--ticket", required=True)
    parser.add_argument("--type", dest="type_", required=True, choices=VALID_TYPES)
    parser.add_argument("--summary", required=True)
    parser.add_argument("--scope", default=None)
    parser.add_argument(
        "--files",
        default=None,
        help="comma-separated list of files.",
    )
    parser.add_argument(
        "--covers",
        default=None,
        help="comma-separated sibling ticket keys co-delivered by this run (one Closes line each).",
    )
    return parser.parse_args(argv)


def cli_main(argv: list[str]) -> int:
    try:
        args = _parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 1
    files = [f.strip() for f in args.files.split(",") if f.strip()] if args.files else None
    covers = [c.strip() for c in args.covers.split(",") if c.strip()] if args.covers else None
    try:
        out = compose(
            ticket=args.ticket,
            type_=args.type_,
            summary=args.summary,
            scope=args.scope,
            files=files,
            covers=covers,
        )
    except ValueError as exc:
        sys.stderr.write(f"compose-commit: {exc}\n")
        return 1
    sys.stdout.write(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = ["VALID_TYPES", "cli_main", "compose"]
