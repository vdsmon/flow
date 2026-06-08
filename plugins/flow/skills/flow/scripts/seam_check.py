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
import ast
import re
import subprocess
import sys
import tomllib
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

# A bare script name as it appears in MODULE.md backticks/prose (no path prefix).
_MODULE_NAME_RE = re.compile(r"[a-z_]+\.py")

# A script basename inside a stage-registry.toml [[stage]].description. Allows
# hyphens and uppercase so a stale hyphenated reference (compose-commit.py for
# the real compose_commit.py) is matched literally and flagged, not normalized
# away. Do NOT reuse `[a-z_]+\.py` here — it cannot match a hyphenated drift.
_REGISTRY_SCRIPT_RE = re.compile(r"[A-Za-z0-9_-]+\.py")

# An inline-code span: text between a pair of backticks on one line.
_INLINE_SPAN_RE = re.compile(r"`([^`]*)`")
# A fenced-code block delimiter (``` or ~~~), ignoring leading whitespace.
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
# A user-facing slash command in prose, e.g. `/flow recover --ticket X`. The verb
# is the first word after `/flow `; cross-checked against scripts/<verb>.py.
_SLASH_RE = re.compile(r"^/flow\s+([a-z][a-z-]*)\b(.*)$")


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


def _slash_spans(text: str) -> list[tuple[int, str]]:
    """Yield (1-based line, span-content) for each inline-code span and each
    fenced-code line. Spans are extracted independently so two adjacent backtick
    spans on one line never merge (e.g. `/flow recover <KEY>` then `retry --stage
    ticket` stay separate)."""
    spans: list[tuple[int, str]] = []
    in_fence = False
    for lineno, line in enumerate(text.splitlines(), start=1):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            spans.append((lineno, line.strip()))
        else:
            for m in _INLINE_SPAN_RE.finditer(line):
                spans.append((lineno, m.group(1).strip()))
    return spans


def find_slash_invocations(doc_name: str, text: str) -> list[Invocation]:
    """Parse user-facing `/flow <verb> ...` slash-prose and normalize each to the
    same Invocation form `find_invocations` produces, so validate() runs unchanged.
    Only verbs with a matching scripts/<verb>.py on disk are linted; verbs without
    a script (do, evolve, new, spec, baseline) are intentionally skipped."""
    invs: list[Invocation] = []
    for lineno, span in _slash_spans(text):
        m = _SLASH_RE.match(span)
        if not m:
            continue
        verb, rest = m.group(1), m.group(2)
        script = f"{verb}.py"
        if not (SCRIPTS_DIR / script).is_file():
            continue
        raw = f"{script}{rest}"
        flags = _FLAG_RE.findall(_VALUE_SPAN_RE.sub(" ", rest))
        invs.append(
            Invocation(
                doc=doc_name,
                line=lineno,
                script=script,
                subcommand=None,
                flags=flags,
                raw=raw,
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


def scripts_missing_from_module_md(
    scripts_dir: Path = SCRIPTS_DIR, module_text: str | None = None
) -> set[str]:
    """Non-test *.py files on disk not named anywhere in MODULE.md."""
    if module_text is None:
        module_text = (scripts_dir / "MODULE.md").read_text(encoding="utf-8")
    on_disk = {
        p.name
        for p in scripts_dir.glob("*.py")
        if not p.name.startswith("test") and p.name != "conftest.py"
    }
    named = set(_MODULE_NAME_RE.findall(module_text))
    return on_disk - named


def scripts_missing_from_registry_descriptions(
    registry_path: Path = SKILL_ROOT / "stage-registry.toml",
    scripts_dir: Path = SCRIPTS_DIR,
) -> set[str]:
    """Script basenames named in a [[stage]].description but not on disk.

    Matches the LITERAL basename (hyphens preserved, no normalization) so a
    stale compose-commit.py reference for the real compose_commit.py is caught,
    not masked.
    """
    data = tomllib.loads(registry_path.read_text(encoding="utf-8"))
    named: set[str] = set()
    for stage in data.get("stage", []):
        named |= set(_REGISTRY_SCRIPT_RE.findall(stage.get("description", "")))
    return {name for name in named if not (scripts_dir / name).is_file()}


def _local_stems(scripts_dir: Path) -> set[str]:
    """Stems of every non-test *.py basename in scripts_dir (the resolvable modules)."""
    return {
        p.stem
        for p in scripts_dir.glob("*.py")
        if not p.name.startswith("test") and p.name != "conftest.py"
    }


def true_importers(scripts_dir: Path = SCRIPTS_DIR) -> dict[str, set[str]]:
    """AST-walk every non-test scripts/*.py and build {imported_stem: {importer_stem}}.

    Walks the whole module body so lazy/in-function imports are credited (e.g.
    tracker imports its adapters inside make_tracker). Only resolvable local
    stems are kept; an import of a module's own stem is dropped.
    """
    stems = _local_stems(scripts_dir)
    out: dict[str, set[str]] = {}
    for path in scripts_dir.glob("*.py"):
        if path.name.startswith("test") or path.name == "conftest.py":
            continue
        importer = path.stem
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                names = [node.module]
            for name in names:
                stem = name.split(".")[0]
                if stem in stems and stem != importer:
                    out.setdefault(stem, set()).add(importer)
    return out


@dataclass(frozen=True)
class ImporterDrift:
    module: str
    missing: frozenset[str]  # in true importers, absent from the row
    phantom: frozenset[str]  # in the row, not a true importer


def module_md_importer_drift(
    scripts_dir: Path = SCRIPTS_DIR, module_text: str | None = None
) -> list[ImporterDrift]:
    """Drift between MODULE.md 'imported by' rows and the AST-computed truth.

    For each line: take the module from the first backticked *.py token, require
    the literal `imported by` anchor (skips reverse-direction `imports x, y`
    rows), parse the importer list after it (split on , and +; strip
    parentheticals/backticks/the/.py). A row is enumerable iff every token
    resolves to a local stem; unresolvable rows (prose like `the adapters`) are
    skipped. Enumerable rows whose parsed set != true set yield one descriptor.
    """
    if module_text is None:
        module_text = (scripts_dir / "MODULE.md").read_text(encoding="utf-8")
    stems = _local_stems(scripts_dir)
    truth = true_importers(scripts_dir)
    drifts: list[ImporterDrift] = []
    for line in module_text.splitlines():
        module_stem: str | None = None
        for span in _INLINE_SPAN_RE.finditer(line):
            mm = _MODULE_NAME_RE.search(span.group(1))
            if mm:
                module_stem = mm.group(0)[: -len(".py")]
                break
        if module_stem is None:
            continue
        anchor = "imported by"
        idx = line.find(anchor)
        if idx == -1:
            continue
        cell = line[idx + len(anchor) :]
        cell = cell.split("|", 1)[0]
        tokens: list[str] = []
        for raw_tok in re.split(r"[,+]", cell):
            tok = raw_tok.split("(", 1)[0]
            tok = tok.strip().strip("`").strip()
            if tok.startswith("the "):
                tok = tok[len("the ") :]
            tok = tok.strip().strip(".").strip()
            if tok.endswith(".py"):
                tok = tok[: -len(".py")]
            tok = tok.strip()
            if tok:
                tokens.append(tok)
        if not tokens or any(tok not in stems for tok in tokens):
            continue
        parsed = set(tokens)
        true_set = truth.get(module_stem, set())
        if parsed != true_set:
            drifts.append(
                ImporterDrift(
                    module=module_stem,
                    missing=frozenset(true_set - parsed),
                    phantom=frozenset(parsed - true_set),
                )
            )
    return drifts


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
        text = doc.read_text(encoding="utf-8")
        all_invs.extend(find_invocations(doc.name, text))
        all_invs.extend(find_slash_invocations(doc.name, text))

    problems: list[Problem] = []
    for inv in all_invs:
        problems.extend(validate(inv))

    for name in sorted(scripts_missing_from_module_md()):
        problems.append(
            Problem(
                doc="MODULE.md",
                line=0,
                level="ERROR",
                msg=f"script not named in MODULE.md: {name}",
                raw="",
            )
        )

    for name in sorted(scripts_missing_from_registry_descriptions()):
        problems.append(
            Problem(
                doc="stage-registry.toml",
                line=0,
                level="ERROR",
                msg=f"description names a script not on disk: {name}",
                raw="",
            )
        )

    for drift in module_md_importer_drift():
        problems.append(
            Problem(
                doc="MODULE.md",
                line=0,
                level="ERROR",
                msg=(
                    f"MODULE.md 'imported by' row for {drift.module}: "
                    f"missing {sorted(drift.missing)}, phantom {sorted(drift.phantom)}"
                ),
                raw="",
            )
        )

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
