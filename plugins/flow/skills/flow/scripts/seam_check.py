#!/usr/bin/env python3
"""Prose<->CLI seam checker for /flow.

Parses every `${CLAUDE_SKILL_DIR}/scripts/<x>.py` invocation out of SKILL.md +
references/*.md and validates each against the script's REAL argparse surface
(subcommands + flags), discovered by running each script with `--help`. Catches
the #1 drift bug class: prose naming a flag or subcommand the script does not
define. Unit tests bypass argparse, so they never catch this; the seam checker
is the net that lets the prose be restructured without silently breaking the
executable contract.

Run from anywhere:
    python3 seam_check.py            # check the live SKILL.md + references/
    python3 seam_check.py --verbose  # also print every invocation it resolved

Exit 0 = every invocation resolves. Exit 1 = at least one ERROR (unknown flag,
unknown subcommand, or missing script). WARN lines never fail the build.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from functools import cache
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPTS_DIR.parent

# A script reference inside prose, e.g. `${CLAUDE_SKILL_DIR}/scripts/init.py`.
_SCRIPT_RE = re.compile(r"\$\{CLAUDE_SKILL_DIR\}/scripts/([a-z_]+\.py)")
# A long-option token. Matches the flag name and stops before `=`.
_FLAG_RE = re.compile(r"--[a-zA-Z][a-zA-Z0-9-]*")
# Quoted strings and command substitutions hold VALUES, not flags of this
# command (e.g. `--base "$(git rev-parse --abbrev-ref HEAD)"`). Strip them
# before pulling flag names so a nested command's flag is not misattributed.
_VALUE_SPAN_RE = re.compile(r"\"[^\"]*\"|'[^']*'|`[^`]*`|\$\([^)]*\)")
# argparse renders the subparser positional as `{a,b,c} ...` (trailing `...`),
# which distinguishes it from a flag's value choices like `--status {a,b}`.
# The subparser group may sit after top-level optionals, so anchor on the `...`.
_USAGE_SUBCMD_RE = re.compile(r"\{([^}]+)\}\s*\.\.\.")
# Tokens that are obviously argument VALUES / placeholders, never subcommands.
_PLACEHOLDER_RE = re.compile(r"""^[<"'$.]|^-""")

# Sentinel flags that one script detects in raw argv and forwards (minus the
# sentinel) to another script's CLI. recall.py --metric <...> dispatches to
# metric.cli_main, so the trailing flags are metric.py's surface, not recall's.
_FORWARDERS = {("recall.py", "--metric"): "metric.py"}


@dataclass(frozen=True)
class Surface:
    """The real CLI surface of one script, read from its `--help` output."""

    subcommands: frozenset[str]
    global_flags: frozenset[str]
    sub_flags: dict[str, frozenset[str]]

    def all_sub_flags(self) -> frozenset[str]:
        out: set[str] = set()
        for fl in self.sub_flags.values():
            out |= fl
        return frozenset(out)


@dataclass
class Invocation:
    doc: str
    line: int
    script: str
    subcommand: str | None
    flags: list[str]
    raw: str


@dataclass
class Problem:
    doc: str
    line: int
    level: str  # "ERROR" | "WARN"
    msg: str
    raw: str


# --- prose parsing -----------------------------------------------------------


def _logical_lines(text: str) -> list[tuple[int, str]]:
    """Join backslash-continued lines. Returns (1-based start line, joined text)."""
    out: list[tuple[int, str]] = []
    buf: list[str] = []
    start = 0
    for i, line in enumerate(text.splitlines(), start=1):
        if not buf:
            start = i
        stripped = line.rstrip()
        if stripped.endswith("\\"):
            buf.append(stripped[:-1])
            continue
        buf.append(line)
        out.append((start, " ".join(p.strip() for p in buf)))
        buf = []
    if buf:
        out.append((start, " ".join(p.strip() for p in buf)))
    return out


def _clean(token: str) -> str:
    return token.strip().strip("[](){}\"'`,")


def find_invocations(doc_name: str, text: str) -> list[Invocation]:
    invs: list[Invocation] = []
    for lineno, logical in _logical_lines(text):
        m = _SCRIPT_RE.search(logical)
        if not m:
            continue
        script = m.group(1)
        # Args are everything after the script reference on this logical line,
        # truncated at a shell sequencing operator so a second command's flags
        # are not attributed to this script.
        args = logical[m.end() :]
        for sep in ("&&", "||", "|", ";"):
            idx = args.find(sep)
            if idx != -1:
                args = args[:idx]
        flags = _FLAG_RE.findall(_VALUE_SPAN_RE.sub(" ", args))
        invs.append(
            Invocation(
                doc=doc_name,
                line=lineno,
                script=script,
                subcommand=None,  # resolved later, once the surface is known
                flags=flags,
                raw=logical.strip(),
            )
        )
    return invs


# --- script introspection ----------------------------------------------------


def _run_help(script: Path, sub: str | None) -> str | None:
    argv = [sys.executable, str(script)]
    if sub:
        argv.append(sub)
    argv.append("--help")
    try:
        cp = subprocess.run(
            argv,
            cwd=str(SCRIPTS_DIR),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return cp.stdout or None


@cache
def surface_of(script_name: str) -> Surface | None:
    script = SCRIPTS_DIR / script_name
    if not script.is_file():
        return None
    top = _run_help(script, None)
    if top is None:
        return None
    subs: frozenset[str] = frozenset()
    usage_oneline = " ".join(top.splitlines()[:3])
    um = _USAGE_SUBCMD_RE.search(usage_oneline)
    if um:
        subs = frozenset(s.strip() for s in um.group(1).split(",") if s.strip())
    global_flags = frozenset(_FLAG_RE.findall(top))
    sub_flags: dict[str, frozenset[str]] = {}
    for s in subs:
        sh = _run_help(script, s)
        sub_flags[s] = frozenset(_FLAG_RE.findall(sh)) if sh else frozenset()
    return Surface(subcommands=subs, global_flags=global_flags, sub_flags=sub_flags)


def _resolve_subcommand(inv: Invocation, surface: Surface) -> str | None:
    if not surface.subcommands:
        return None
    args = inv.raw.split(inv.script, 1)[-1]
    for tok in (_clean(t) for t in args.split()):
        if tok in surface.subcommands:
            return tok
    return None


# --- validation --------------------------------------------------------------


def validate(inv: Invocation) -> list[Problem]:
    surface = surface_of(inv.script)
    if surface is None:
        return [
            Problem(
                inv.doc,
                inv.line,
                "ERROR",
                f"script not found / not runnable: {inv.script}",
                inv.raw,
            )
        ]
    problems: list[Problem] = []
    sub = _resolve_subcommand(inv, surface)
    inv.subcommand = sub

    # A script with subcommands invoked without one we recognize: WARN only
    # (prose sometimes shows a partial / illustrative call).
    if surface.subcommands and sub is None:
        barewords = [
            _clean(t)
            for t in inv.raw.split(inv.script, 1)[-1].split()
            if _clean(t) and not _PLACEHOLDER_RE.match(_clean(t))
        ]
        unknown = [b for b in barewords if b not in surface.subcommands]
        if unknown:
            problems.append(
                Problem(
                    inv.doc,
                    inv.line,
                    "WARN",
                    f"{inv.script}: no recognized subcommand "
                    f"(saw {unknown[:3]}; known: {sorted(surface.subcommands)})",
                    inv.raw,
                )
            )

    known_any = surface.global_flags | surface.all_sub_flags() | {"--help"}
    known_strict = surface.global_flags | {"--help"}
    if sub is not None:
        known_strict |= surface.sub_flags.get(sub, frozenset())

    # Fold in a forwarded script's surface when its sentinel flag is present.
    for (fscript, sentinel), target in _FORWARDERS.items():
        if inv.script == fscript and sentinel in inv.flags:
            tsurface = surface_of(target)
            extra = {sentinel}
            if tsurface is not None:
                extra |= tsurface.global_flags | tsurface.all_sub_flags()
            known_any |= extra
            known_strict |= extra

    for flag in inv.flags:
        if flag not in known_any:
            problems.append(
                Problem(
                    inv.doc,
                    inv.line,
                    "ERROR",
                    f"{inv.script}{' ' + sub if sub else ''}: unknown flag {flag}",
                    inv.raw,
                )
            )
        elif sub is not None and flag not in known_strict:
            problems.append(
                Problem(
                    inv.doc,
                    inv.line,
                    "WARN",
                    f"{inv.script} {sub}: flag {flag} valid elsewhere but not for this subcommand",
                    inv.raw,
                )
            )
    return problems


# --- driver ------------------------------------------------------------------


def docs_to_check() -> list[Path]:
    docs = [SKILL_ROOT / "SKILL.md"]
    refs = SKILL_ROOT / "references"
    if refs.is_dir():
        docs.extend(sorted(refs.glob("*.md")))
    return [d for d in docs if d.is_file()]


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description="Validate /flow prose script invocations against real CLIs."
    )
    ap.add_argument("--verbose", action="store_true", help="print every resolved invocation")
    args = ap.parse_args(argv)

    docs = docs_to_check()
    all_invs: list[Invocation] = []
    for doc in docs:
        all_invs.extend(find_invocations(doc.name, doc.read_text(encoding="utf-8")))

    problems: list[Problem] = []
    for inv in all_invs:
        problems.extend(validate(inv))

    if args.verbose:
        for inv in all_invs:
            sub = f" {inv.subcommand}" if inv.subcommand else ""
            print(f"  {inv.doc}:{inv.line}  {inv.script}{sub}  {inv.flags}")

    errors = [p for p in problems if p.level == "ERROR"]
    warns = [p for p in problems if p.level == "WARN"]
    for p in errors + warns:
        print(f"{p.doc}:{p.line}: {p.level}: {p.msg}")
        if p.level == "ERROR":
            print(f"    {p.raw}")

    n_scripts = len({inv.script for inv in all_invs})
    print(
        f"\nseam_check: {len(all_invs)} invocations across {len(docs)} docs, "
        f"{n_scripts} scripts -> {len(errors)} error(s), {len(warns)} warning(s)"
    )
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
