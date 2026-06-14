"""Neutralize GitHub CI-skip tokens in a commit-message file.

Library + thin CLI. Stdlib-only.

GitHub honors a bracketed CI-skip token (`[skip ci]`, `[ci skip]`, `[no ci]`,
`[skip actions]`, `[actions skip]`, case-insensitive) ANYWHERE in a commit
message and suppresses all CI for the push. The commit body is free text, so a
stray token there would silently skip CI. This strips the brackets, keeping the
inner words verbatim, so the marker no longer triggers.

Exit codes:
  0 = ok (always, whether or not tokens were neutralized)
  1 = path missing/unreadable
"""

from __future__ import annotations

import argparse
import re
import sys

_TOKEN = re.compile(r"\[(skip ci|ci skip|no ci|skip actions|actions skip)\]", re.IGNORECASE)


def scrub(text: str) -> tuple[str, int]:
    scrubbed, n = _TOKEN.subn(lambda m: m.group(1), text)
    return scrubbed, n


def cli_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Neutralize CI-skip tokens in a commit-message file."
    )
    parser.add_argument("path", help="commit-message file to scrub in place.")
    args = parser.parse_args(argv)
    try:
        with open(args.path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        sys.stderr.write(f"scrub-ci-skip: cannot read {args.path}: {exc}\n")
        return 1
    scrubbed, n = scrub(text)
    if n > 0:
        with open(args.path, "w", encoding="utf-8") as fh:
            fh.write(scrubbed)
        sys.stderr.write(f"scrub-ci-skip: neutralized {n} CI-skip token(s)\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = ["cli_main", "scrub"]
