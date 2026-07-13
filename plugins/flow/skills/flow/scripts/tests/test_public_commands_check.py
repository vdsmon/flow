from __future__ import annotations

from pathlib import Path

import public_commands_check
from public_commands import (
    load_registry,
    render_grammar_block,
    render_help,
    render_router_block,
    render_trigger_description,
)

SKILL_ROOT = Path(__file__).resolve().parents[2]


def test_live_public_command_artifacts_are_generated_and_references_exist() -> None:
    assert public_commands_check.check(SKILL_ROOT) == []


def test_checker_reports_each_managed_surface_without_writing(tmp_path: Path) -> None:
    registry = load_registry(SKILL_ROOT / "public-commands.toml")
    skill = tmp_path / "SKILL.md"
    help_doc = tmp_path / "references" / "public-help.md"
    help_doc.parent.mkdir()
    skill.write_text(
        "---\nname: flow\ndescription: stale\n---\n\n"
        + render_router_block(registry).replace("Static namespaces", "Old namespaces")
        + render_grammar_block(registry).replace("FLOW ticket", "FLOW old-ticket"),
        encoding="utf-8",
    )
    help_doc.write_text("stale help\n", encoding="utf-8")
    before = {skill: skill.read_bytes(), help_doc: help_doc.read_bytes()}

    problems = public_commands_check.check(
        tmp_path,
        registry_path=SKILL_ROOT / "public-commands.toml",
        require_references=False,
    )

    assert any("description" in problem for problem in problems)
    assert any("router" in problem for problem in problems)
    assert any("grammar" in problem for problem in problems)
    assert any("public-help.md" in problem for problem in problems)
    assert {path: path.read_bytes() for path in before} == before


def test_expected_artifacts_are_registry_renderings(tmp_path: Path) -> None:
    registry = load_registry(SKILL_ROOT / "public-commands.toml")
    skill = tmp_path / "SKILL.md"
    references = tmp_path / "references"
    references.mkdir()
    skill.write_text(
        "---\nname: flow\ndescription: "
        + render_trigger_description(registry)
        + "\n---\n\n"
        + render_router_block(registry)
        + render_grammar_block(registry),
        encoding="utf-8",
    )
    (references / "public-help.md").write_text(render_help(registry), encoding="utf-8")

    assert (
        public_commands_check.check(
            tmp_path,
            registry_path=SKILL_ROOT / "public-commands.toml",
            require_references=False,
        )
        == []
    )
