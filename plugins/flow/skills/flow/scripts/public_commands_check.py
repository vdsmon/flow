"""Check generated public-command artifacts without modifying the workspace."""

from __future__ import annotations

import sys
from pathlib import Path

from public_commands import (
    DEFAULT_REGISTRY,
    GeneratedContentDrift,
    RegistryError,
    check_generated_block,
    load_registry,
    render_grammar_block,
    render_router_block,
    render_trigger_description,
)

SKILL_ROOT = Path(__file__).resolve().parent.parent
_ROUTER_BEGIN = "<!-- flow:public-router:begin -->"
_ROUTER_END = "<!-- flow:public-router:end -->"
_GRAMMAR_BEGIN = "<!-- flow:public-grammar:begin -->"
_GRAMMAR_END = "<!-- flow:public-grammar:end -->"


def _frontmatter_description(document: str) -> str | None:
    lines = document.splitlines()
    if not lines or lines[0] != "---":
        return None
    for line in lines[1:]:
        if line == "---":
            break
        name, separator, value = line.partition(":")
        if separator and name.strip() == "description":
            return value.strip()
    return None


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

    skill_path = skill_root / "SKILL.md"
    try:
        skill_document = skill_path.read_text(encoding="utf-8")
    except OSError as exc:
        problems.append(f"cannot read {skill_path}: {exc}")
    else:
        expected_description = render_trigger_description(registry)
        if _frontmatter_description(skill_document) != expected_description:
            problems.append("SKILL.md description is stale relative to public-commands.toml")
        try:
            check_generated_block(
                skill_document,
                begin_marker=_ROUTER_BEGIN,
                end_marker=_ROUTER_END,
                rendered=render_router_block(registry),
            )
        except (GeneratedContentDrift, RegistryError):
            problems.append(
                "SKILL.md public router block is stale relative to public-commands.toml"
            )
        try:
            check_generated_block(
                skill_document,
                begin_marker=_GRAMMAR_BEGIN,
                end_marker=_GRAMMAR_END,
                rendered=render_grammar_block(registry),
            )
        except (GeneratedContentDrift, RegistryError):
            problems.append(
                "SKILL.md public grammar block is stale relative to public-commands.toml"
            )

    if require_references:
        for command in registry.commands:
            reference = skill_root / command.reference
            if not reference.is_file():
                problems.append(f"{command.id}: missing reference {command.reference}")

    return problems


def cli_main() -> int:
    problems = check()
    if problems:
        for problem in problems:
            sys.stderr.write(f"public-commands: {problem}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main())


__all__ = ["check", "cli_main"]
