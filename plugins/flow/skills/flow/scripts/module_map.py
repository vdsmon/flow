"""Render and check the generated derived surfaces: MODULE.md's script map appendix,
MODULE.md's reference-docs index, and stage-reflect.md's guard-file enumeration.

The import graph, argparse subcommand names, and ``triage._GUARD_FILES`` are facts
the code already states; hand-writing them in prose made every import or subcommand
change a two-file edit policed by parser gates. This module renders each fact into a
marker-delimited block instead: ``write`` regenerates the blocks in place, ``check``
(the default, folded into ``seam_check.py``) fails when a block is stale.

Subcommand names come from AST ``add_parser`` string constants, not ``--help``
subprocesses; ``tests/test_module_map.py`` pins AST-vs-argparse equivalence so a
dynamically built subparser fails the suite rather than drifting silently.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from collections.abc import Callable
from pathlib import Path

from public_commands import (
    GeneratedContentDrift,
    RegistryError,
    check_generated_block,
    replace_generated_block,
)

SCRIPTS_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPTS_DIR.parent

MODULE_MD = SCRIPTS_DIR / "MODULE.md"
STAGE_REFLECT_MD = SKILL_ROOT / "references" / "stage-reflect.md"
REFERENCES_DIR = SKILL_ROOT / "references"

MODULE_BEGIN = "<!-- flow:module-map:begin -->"
MODULE_END = "<!-- flow:module-map:end -->"
GUARD_BEGIN = "<!-- flow:guard-files:begin -->"
GUARD_END = "<!-- flow:guard-files:end -->"
DOCS_INDEX_BEGIN = "<!-- flow:docs-index:begin -->"
DOCS_INDEX_END = "<!-- flow:docs-index:end -->"


def local_stems(scripts_dir: Path = SCRIPTS_DIR) -> set[str]:
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
    stems = local_stems(scripts_dir)
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


def subcommands(scripts_dir: Path = SCRIPTS_DIR) -> dict[str, list[str]]:
    """{script basename: sorted argparse subcommand names}, from AST.

    A subcommand is the first positional argument of an ``.add_parser(...)`` call
    when it is a string constant. Every add_parser first argument in the engine is
    a literal today; test_module_map pins that equivalence against argparse.
    """
    out: dict[str, list[str]] = {}
    for path in sorted(scripts_dir.glob("*.py")):
        if path.name.startswith("test") or path.name == "conftest.py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        names: set[str] = set()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_parser"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                names.add(node.args[0].value)
        if names:
            out[path.name] = sorted(names)
    return out


def triage_guard_files(scripts_dir: Path = SCRIPTS_DIR) -> frozenset[str]:
    """triage._GUARD_FILES parsed from source (AST, no import side effects)."""
    tree = ast.parse((scripts_dir / "triage.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for tgt in node.targets:
            if isinstance(tgt, ast.Name) and tgt.id == "_GUARD_FILES":
                return frozenset(
                    c.value
                    for c in ast.walk(node.value)
                    if isinstance(c, ast.Constant) and isinstance(c.value, str)
                )
    return frozenset()


def render_module_block(scripts_dir: Path = SCRIPTS_DIR) -> str:
    """The MODULE.md derived-surfaces table, markers included."""
    importers = true_importers(scripts_dir)
    subs = subcommands(scripts_dir)
    names = sorted(
        p.name
        for p in scripts_dir.glob("*.py")
        if not p.name.startswith("test") and p.name != "conftest.py"
    )
    lines = [
        MODULE_BEGIN,
        "| Script | Subcommands | Imported by |",
        "|--------|-------------|-------------|",
    ]
    for name in names:
        sub_cell = " ".join(f"`{s}`" for s in subs.get(name, [])) or "—"
        imp_cell = ", ".join(f"`{s}`" for s in sorted(importers.get(name[:-3], set()))) or "—"
        lines.append(f"| `{name}` | {sub_cell} | {imp_cell} |")
    lines.append(MODULE_END)
    return "\n".join(lines) + "\n"


def render_guard_span(scripts_dir: Path = SCRIPTS_DIR) -> str:
    """The stage-reflect.md guard-file enumeration span, markers included.

    Single-line so the span can sit inside an indented list item without the
    regeneration disturbing the surrounding markdown structure.
    """
    members = sorted(n for n in triage_guard_files(scripts_dir) if n.endswith(".py"))
    listed = ", ".join(f"`{n}`" for n in members)
    return f"{GUARD_BEGIN}({listed}),{GUARD_END}"


_SENTENCE_RE = re.compile(r"(.+?[.!?])(?:\s|$)", re.DOTALL)


def doc_one_liner(path: Path) -> str:
    """First purpose sentence of a reference doc, normalized for a table cell.

    Skips the H1 (and a `## Purpose` heading when the doc uses the stage shape),
    joins the first paragraph's wrapped lines, and takes its first sentence.
    Backticks are stripped so the cell never forms an inline recipe span; a
    trailing colon (an intro to a list) is dropped; long sentences truncate at a
    word boundary.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    i = 0
    while i < len(lines) and not lines[i].startswith("# "):
        i += 1
    i += 1
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i < len(lines) and lines[i].strip() == "## Purpose":
        i += 1
        while i < len(lines) and not lines[i].strip():
            i += 1
    para: list[str] = []
    while i < len(lines):
        text = lines[i].strip()
        if not text or text.startswith("#"):
            break
        para.append(text)
        i += 1
    joined = " ".join(para)
    match = _SENTENCE_RE.match(joined)
    sentence = (match.group(1) if match else joined).replace("`", "").strip().rstrip(":")
    if len(sentence) > 140:
        sentence = sentence[:140].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return sentence


def render_docs_index(scripts_dir: Path = SCRIPTS_DIR, refs_dir: Path | None = None) -> str:
    """The MODULE.md reference-docs index table, markers included.

    ``scripts_dir`` is unused but kept so every ``_targets()`` render callable
    shares one call shape; the docs live under ``REFERENCES_DIR`` (module-level
    so tests can monkeypatch it, same pattern as ``MODULE_MD``).
    """
    del scripts_dir
    refs = refs_dir if refs_dir is not None else REFERENCES_DIR
    lines = [
        DOCS_INDEX_BEGIN,
        "| Doc | Covers |",
        "|-----|--------|",
    ]
    lines.extend(
        f"| `references/{path.name}` | {doc_one_liner(path)} |"
        for path in sorted(refs.glob("*.md"))
    )
    lines.append(DOCS_INDEX_END)
    return "\n".join(lines) + "\n"


def _targets() -> tuple[tuple[Path, str, str, Callable[[Path], str]], ...]:
    """(path, begin, end, render) per managed block; read at call time so tests
    can point the module-level path constants at temporary copies."""
    return (
        (MODULE_MD, MODULE_BEGIN, MODULE_END, render_module_block),
        (MODULE_MD, DOCS_INDEX_BEGIN, DOCS_INDEX_END, render_docs_index),
        (STAGE_REFLECT_MD, GUARD_BEGIN, GUARD_END, render_guard_span),
    )


def check(scripts_dir: Path = SCRIPTS_DIR) -> list[str]:
    """Return one problem string per stale or unreadable generated block."""
    problems: list[str] = []
    for path, begin, end, render in _targets():
        try:
            document = path.read_text(encoding="utf-8")
        except OSError as exc:
            problems.append(f"cannot read {path.name}: {exc}")
            continue
        try:
            check_generated_block(
                document, begin_marker=begin, end_marker=end, rendered=render(scripts_dir)
            )
        except GeneratedContentDrift:
            problems.append(
                f"{path.name}: generated block {begin} is stale — run: python3 module_map.py write"
            )
        except RegistryError as exc:
            problems.append(f"{path.name}: {exc}")
    return problems


def write(scripts_dir: Path = SCRIPTS_DIR) -> list[Path]:
    """Regenerate every managed block in place; return the paths that changed."""
    changed: list[Path] = []
    for path, begin, end, render in _targets():
        document = path.read_text(encoding="utf-8")
        updated = replace_generated_block(
            document, begin_marker=begin, end_marker=end, rendered=render(scripts_dir)
        )
        if updated != document:
            path.write_text(updated, encoding="utf-8")
            changed.append(path)
    return changed


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Render and check MODULE.md's generated derived surfaces."
    )
    parser.add_argument(
        "mode",
        nargs="?",
        choices=("check", "write"),
        default="check",
        help="check (default): exit 1 when a block is stale; write: regenerate in place",
    )
    args = parser.parse_args(argv)
    if args.mode == "write":
        for path in write():
            sys.stdout.write(f"rewrote {path}\n")
        return 0
    problems = check()
    for problem in problems:
        sys.stderr.write(problem + "\n")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
