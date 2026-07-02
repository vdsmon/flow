"""Neutralize GitHub CI-skip tokens in a commit-message file.

Library + thin CLI. Stdlib-only.

GitHub honors a bracketed CI-skip token (`[skip ci]`, `[ci skip]`, `[no ci]`,
`[skip actions]`, `[actions skip]`, case-insensitive) ANYWHERE in a commit
message and suppresses all CI for the push. It also honors an unbracketed
trailer form: a commit message ending with a `skip-checks: true` (or
`skip-checks:true`) line. The commit body is free text, so a stray token there
would silently skip CI. This strips the brackets (keeping the inner words
verbatim) and drops the colon from a whole-line skip-checks trailer, so the
markers no longer trigger.

Exit codes:
  0 = ok (always, whether or not tokens were neutralized)
  1 = path missing/unreadable
"""

from __future__ import annotations

import argparse
import re
import sys

_TOKEN = re.compile(r"\[(skip ci|ci skip|no ci|skip actions|actions skip)\]", re.IGNORECASE)
# GitHub only honors the trailer at the end of the message, but any line that is
# solely `skip-checks: true` is the dangerous form; neutralizing it anywhere is
# safe-side and cannot mangle prose that mentions it mid-sentence.
_SKIP_CHECKS = re.compile(r"^(skip-checks):[ \t]*(true)[ \t]*$", re.IGNORECASE | re.MULTILINE)


def scrub(text: str) -> tuple[str, int]:
    scrubbed, n = _TOKEN.subn(lambda m: m.group(1), text)
    scrubbed, n_trailer = _SKIP_CHECKS.subn(r"\1 \2", scrubbed)
    return scrubbed, n + n_trailer


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
