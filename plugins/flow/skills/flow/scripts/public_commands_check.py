"""Check generated public-command artifacts, or regenerate them in place.

``check`` (the default) reports drift without touching the workspace; the prek hook and CI both call
it. ``write`` regenerates SKILL.md's frontmatter description, public router block, and public
grammar block from ``public-commands.toml``, mirroring ``module_map.py``'s check/write split for its
own generated blocks.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path

from public_commands import (
    DEFAULT_REGISTRY,
    Registry,
    RegistryError,
    load_registry,
    render_grammar_block,
    render_router_block,
    render_trigger_description,
    replace_generated_block,
)

SKILL_ROOT = Path(__file__).resolve().parent.parent
_ROUTER_BEGIN = "<!-- flow:public-router:begin -->"
_ROUTER_END = "<!-- flow:public-router:end -->"
_GRAMMAR_BEGIN = "<!-- flow:public-grammar:begin -->"
_GRAMMAR_END = "<!-- flow:public-grammar:end -->"

Rewrite = Callable[[str, Registry], str]


def _frontmatter_description_line(document: str) -> int | None:
    """Index into ``document.splitlines()`` of the frontmatter description: line.

    Only a top-level key counts. An indented ``description:`` is nested under some other key and is
    not the trigger description this script manages, so rewriting it would corrupt a document the
    script does not own.
    """
    lines = document.splitlines()
    if not lines or lines[0] != "---":
        return None
    for index, line in enumerate(lines[1:], start=1):
        if line == "---":
            break
        if line.startswith((" ", "\t")):
            continue
        name, separator, _ = line.partition(":")
        if separator and name.strip() == "description":
            return index
    return None


def _rewrite_description(document: str, registry: Registry) -> str:
    """Replace the frontmatter description: line with the current rendering.

    Raises RegistryError instead of replacing when the value is not a plain single-line scalar: a
    YAML block scalar (a value starting with ``>`` or ``|``, with or without an
    indentation-indicator digit) or a folded plain multi-line value would leave orphaned
    continuation lines after a one-line replace.

    The replacement carries over the original line's trailing newline, or its absence on a truncated
    document. It does not preserve CRLF: ``read_text`` decodes in universal-newline mode and
    ``write_text`` emits LF, so ``write`` normalizes the whole file exactly as ``module_map.write``
    does.
    """
    index = _frontmatter_description_line(document)
    if index is None:
        raise RegistryError("SKILL.md frontmatter has no description: line")

    lines = document.splitlines(keepends=True)
    line = lines[index]
    stripped = line.rstrip("\r\n")
    terminator = line[len(stripped) :]
    _, _, value = stripped.partition(":")
    value = value.strip()
    if value.startswith((">", "|")):
        raise RegistryError(
            "SKILL.md description is a YAML block scalar; write cannot rewrite it in place"
        )

    this_indent = len(stripped) - len(stripped.lstrip(" "))
    next_raw = lines[index + 1] if index + 1 < len(lines) else ""
    next_stripped = next_raw.rstrip("\r\n")
    next_indent = len(next_stripped) - len(next_stripped.lstrip(" "))
    if next_stripped.strip() and next_indent > this_indent:
        raise RegistryError(
            "SKILL.md description folds onto a continuation line; write cannot rewrite it in place"
        )

    lines[index] = f"description: {render_trigger_description(registry)}{terminator}"
    return "".join(lines)


def _rewrite_router(document: str, registry: Registry) -> str:
    return replace_generated_block(
        document,
        begin_marker=_ROUTER_BEGIN,
        end_marker=_ROUTER_END,
        rendered=render_router_block(registry),
    )


def _rewrite_grammar(document: str, registry: Registry) -> str:
    return replace_generated_block(
        document,
        begin_marker=_GRAMMAR_BEGIN,
        end_marker=_GRAMMAR_END,
        rendered=render_grammar_block(registry),
    )


def _targets(skill_root: Path) -> tuple[tuple[Path, str, Rewrite], ...]:
    """(path, label, rewrite) per managed surface; all three live in SKILL.md today.

    Takes skill_root as a parameter, read at call time, so tests can point it at a temporary copy;
    mirrors module_map._targets()'s per-call table.
    """
    skill_path = skill_root / "SKILL.md"
    return (
        (skill_path, "description", _rewrite_description),
        (skill_path, "public router block", _rewrite_router),
        (skill_path, "public grammar block", _rewrite_grammar),
    )


def check(
    skill_root: Path = SKILL_ROOT,
    *,
    registry_path: Path | None = None,
    require_references: bool = True,
) -> list[str]:
    """Return every drift problem; never write generated content."""

    problems: list[str] = []
    registry_source = registry_path or skill_root / DEFAULT_REGISTRY.name
    try:
        registry = load_registry(registry_source)
    except RegistryError as exc:
        return [str(exc)]

    for path, label, rewrite in _targets(skill_root):
        try:
            document = path.read_text(encoding="utf-8")
        except OSError as exc:
            problems.append(f"cannot read {path}: {exc}")
            continue
        try:
            rewritten = rewrite(document, registry)
        except RegistryError as exc:
            problems.append(str(exc))
            continue
        if rewritten != document:
            problems.append(
                f"{path.name} {label} is stale relative to public-commands.toml — "
                "run: python3 public_commands_check.py write"
            )

    if require_references:
        for command in registry.commands:
            reference = skill_root / command.reference
            if not reference.is_file():
                problems.append(f"{command.id}: missing reference {command.reference}")

    return problems


def write(
    skill_root: Path = SKILL_ROOT,
    *,
    registry_path: Path | None = None,
) -> list[Path]:
    """Regenerate every managed surface in place; return each changed path once."""

    registry_source = registry_path or skill_root / DEFAULT_REGISTRY.name
    registry = load_registry(registry_source)

    changed: list[Path] = []
    for path, _label, rewrite in _targets(skill_root):
        document = path.read_text(encoding="utf-8")
        updated = rewrite(document, registry)
        if updated != document:
            path.write_text(updated, encoding="utf-8")
            if path not in changed:
                changed.append(path)
    return changed


def cli_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check or regenerate SKILL.md's generated public-command surfaces."
    )
    parser.add_argument(
        "mode",
        nargs="?",
        choices=("check", "write"),
        default="check",
        help="check (default): exit 1 when a surface is stale; write: regenerate in place",
    )
    args = parser.parse_args(argv)
    if args.mode == "write":
        for path in write():
            sys.stdout.write(f"rewrote {path}\n")
        return 0
    problems = check()
    if problems:
        for problem in problems:
            sys.stderr.write(f"public-commands: {problem}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = ["check", "cli_main", "write"]
