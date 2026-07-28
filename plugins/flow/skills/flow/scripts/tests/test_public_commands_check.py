from __future__ import annotations

from pathlib import Path

import pytest

import public_commands_check
from public_commands import (
    RegistryError,
    load_registry,
    render_grammar_block,
    render_router_block,
    render_trigger_description,
)

SKILL_ROOT = Path(__file__).resolve().parents[2]
REGISTRY_PATH = SKILL_ROOT / "public-commands.toml"


def test_live_public_command_artifacts_are_generated_and_references_exist() -> None:
    assert public_commands_check.check(SKILL_ROOT) == []


def test_checker_reports_each_managed_surface_without_writing(tmp_path: Path) -> None:
    registry = load_registry(SKILL_ROOT / "public-commands.toml")
    skill = tmp_path / "SKILL.md"
    skill.write_text(
        "---\nname: flow\ndescription: stale\n---\n\n"
        + render_router_block(registry).replace("Static namespaces", "Old namespaces")
        + render_grammar_block(registry).replace("FLOW ticket", "FLOW old-ticket"),
        encoding="utf-8",
    )
    before = {skill: skill.read_bytes()}

    problems = public_commands_check.check(
        tmp_path,
        registry_path=SKILL_ROOT / "public-commands.toml",
        require_references=False,
    )

    assert any("description" in problem for problem in problems)
    assert any("router" in problem for problem in problems)
    assert any("grammar" in problem for problem in problems)
    assert {path: path.read_bytes() for path in before} == before


def test_expected_artifacts_are_registry_renderings(tmp_path: Path) -> None:
    registry = load_registry(SKILL_ROOT / "public-commands.toml")
    skill = tmp_path / "SKILL.md"
    skill.write_text(
        "---\nname: flow\ndescription: "
        + render_trigger_description(registry)
        + "\n---\n\n"
        + render_router_block(registry)
        + render_grammar_block(registry),
        encoding="utf-8",
    )

    assert (
        public_commands_check.check(
            tmp_path,
            registry_path=SKILL_ROOT / "public-commands.toml",
            require_references=False,
        )
        == []
    )


def _stale_skill_document(registry) -> str:
    return (
        "---\nname: flow\ndescription: stale\n---\n\n"
        + render_router_block(registry).replace("Static namespaces", "Old namespaces")
        + render_grammar_block(registry).replace("FLOW ticket", "FLOW old-ticket")
    )


def test_check_flags_all_three_stale_surfaces_and_write_repairs_them_once(
    tmp_path: Path,
) -> None:
    registry = load_registry(REGISTRY_PATH)
    skill = tmp_path / "SKILL.md"
    skill.write_text(_stale_skill_document(registry), encoding="utf-8")

    problems = public_commands_check.check(
        tmp_path, registry_path=REGISTRY_PATH, require_references=False
    )
    assert len(problems) == 3
    assert all("public_commands_check.py write" in problem for problem in problems)

    assert public_commands_check.write(tmp_path, registry_path=REGISTRY_PATH) == [skill]
    assert (
        public_commands_check.check(tmp_path, registry_path=REGISTRY_PATH, require_references=False)
        == []
    )
    assert public_commands_check.write(tmp_path, registry_path=REGISTRY_PATH) == []


def test_write_rewrites_description_line_leaving_rest_of_frontmatter_untouched(
    tmp_path: Path,
) -> None:
    registry = load_registry(REGISTRY_PATH)
    skill = tmp_path / "SKILL.md"
    skill.write_text(
        "---\nname: flow\ndescription: stale\nallowed-tools: Read, Write\n---\n\n"
        + render_router_block(registry)
        + render_grammar_block(registry),
        encoding="utf-8",
    )

    public_commands_check.write(tmp_path, registry_path=REGISTRY_PATH)

    lines = skill.read_text(encoding="utf-8").splitlines()
    assert lines[1] == "name: flow"
    assert lines[2] == f"description: {render_trigger_description(registry)}"
    assert lines[3] == "allowed-tools: Read, Write"


def test_missing_markers_report_problem_without_write_remediation(tmp_path: Path) -> None:
    registry = load_registry(REGISTRY_PATH)
    skill = tmp_path / "SKILL.md"
    skill.write_text(
        "---\nname: flow\ndescription: "
        + render_trigger_description(registry)
        + "\n---\n\nno markers here\n",
        encoding="utf-8",
    )

    problems = public_commands_check.check(
        tmp_path, registry_path=REGISTRY_PATH, require_references=False
    )

    assert len(problems) == 2
    assert all("managed markers not found" in problem for problem in problems)
    assert not any("public_commands_check.py write" in problem for problem in problems)


@pytest.mark.parametrize(
    "description_lines",
    [
        "description: >\n  folded text\n  continues here\n",
        "description: |2-\n    literal text\n    continues here\n",
        # No continuation line, so the folded-plain guard cannot fire and only the block-scalar
        # guard can catch this. The indentation indicator also pins the prefix test: an enumerated
        # {">", "|", ">-", ">+", "|-", "|+"} would let "|2-" through.
        "description: |2-\n",
    ],
)
def test_block_scalar_description_is_a_structural_problem_and_write_refuses_it(
    tmp_path: Path, description_lines: str
) -> None:
    registry = load_registry(REGISTRY_PATH)
    skill = tmp_path / "SKILL.md"
    document = (
        "---\nname: flow\n"
        + description_lines
        + "---\n\n"
        + render_router_block(registry)
        + render_grammar_block(registry)
    )
    skill.write_text(document, encoding="utf-8")

    problems = public_commands_check.check(
        tmp_path, registry_path=REGISTRY_PATH, require_references=False
    )
    assert len(problems) == 1
    assert "description" in problems[0]
    assert "public_commands_check.py write" not in problems[0]

    with pytest.raises(RegistryError):
        public_commands_check.write(tmp_path, registry_path=REGISTRY_PATH)
    assert skill.read_text(encoding="utf-8") == document


def test_nested_description_key_is_not_the_trigger_description(tmp_path: Path) -> None:
    registry = load_registry(REGISTRY_PATH)
    skill = tmp_path / "SKILL.md"
    document = (
        "---\nname: flow\nmeta:\n  description: nested value\n---\n\n"
        + render_router_block(registry)
        + render_grammar_block(registry)
    )
    skill.write_text(document, encoding="utf-8")

    problems = public_commands_check.check(
        tmp_path, registry_path=REGISTRY_PATH, require_references=False
    )
    assert len(problems) == 1
    assert "no description: line" in problems[0]
    assert "public_commands_check.py write" not in problems[0]

    with pytest.raises(RegistryError):
        public_commands_check.write(tmp_path, registry_path=REGISTRY_PATH)
    assert skill.read_text(encoding="utf-8") == document


def test_write_skips_a_nested_description_and_rewrites_the_top_level_one(tmp_path: Path) -> None:
    registry = load_registry(REGISTRY_PATH)
    skill = tmp_path / "SKILL.md"
    skill.write_text(
        "---\nname: flow\nmeta:\n  description: nested value\ndescription: stale\n---\n\n"
        + render_router_block(registry)
        + render_grammar_block(registry),
        encoding="utf-8",
    )

    public_commands_check.write(tmp_path, registry_path=REGISTRY_PATH)

    lines = skill.read_text(encoding="utf-8").splitlines()
    assert lines[2] == "meta:"
    assert lines[3] == "  description: nested value"
    assert lines[4] == f"description: {render_trigger_description(registry)}"


def test_folded_plain_description_is_a_structural_problem_and_write_refuses_it(
    tmp_path: Path,
) -> None:
    registry = load_registry(REGISTRY_PATH)
    skill = tmp_path / "SKILL.md"
    document = (
        "---\nname: flow\ndescription: State-aware ticket-to-PR delivery\n"
        "  and workspace operations.\n---\n\n"
        + render_router_block(registry)
        + render_grammar_block(registry)
    )
    skill.write_text(document, encoding="utf-8")

    problems = public_commands_check.check(
        tmp_path, registry_path=REGISTRY_PATH, require_references=False
    )
    assert len(problems) == 1
    assert "description" in problems[0]

    with pytest.raises(RegistryError):
        public_commands_check.write(tmp_path, registry_path=REGISTRY_PATH)
    assert skill.read_text(encoding="utf-8") == document


# ─── namespace drift (flow-glrn) ─────────────────────────────────────────────


def _namespace_fixture(tmp_path: Path, *, static_roots: str, manifest_list: str) -> Path:
    """A skill tree deep enough for both manifest ancestors, with drift planted."""
    skill_root = tmp_path / "plugins" / "flow" / "skills" / "flow"
    (skill_root / "references").mkdir(parents=True)
    (skill_root / "references" / "command-target.md").write_text(
        f"{static_roots} are always parsed\nas static roots first.\n", encoding="utf-8"
    )
    plugin_dir = tmp_path / "plugins" / "flow" / ".claude-plugin"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.json").write_text(
        f'{{"description": "the {manifest_list} namespaces share one engine"}}',
        encoding="utf-8",
    )
    market_dir = tmp_path / ".claude-plugin"
    market_dir.mkdir(parents=True)
    (market_dir / "marketplace.json").write_text(
        '{"description": "no list here"}', encoding="utf-8"
    )
    return skill_root


def _namespace_problems(problems: list[str]) -> list[str]:
    return [p for p in problems if "names a namespace" in p]


def test_retired_namespace_in_static_roots_sentence_is_drift(tmp_path: Path) -> None:
    skill_root = _namespace_fixture(
        tmp_path,
        static_roots="`ticket`, `memory`, `maintain`, and `help`",
        manifest_list="ticket, memory, measure, and workspace",
    )
    problems = _namespace_problems(
        public_commands_check.check(
            skill_root, registry_path=REGISTRY_PATH, require_references=False
        )
    )
    assert problems == [
        "command-target.md static roots names a namespace the registry does not have: maintain"
    ]


def test_retired_namespace_in_manifest_list_is_drift(tmp_path: Path) -> None:
    skill_root = _namespace_fixture(
        tmp_path,
        static_roots="`ticket`, `memory`, and `help`",
        manifest_list="ticket, memory, measure, and maintain",
    )
    problems = _namespace_problems(
        public_commands_check.check(
            skill_root, registry_path=REGISTRY_PATH, require_references=False
        )
    )
    assert problems == ["plugin.json names a namespace the registry does not have: maintain"]


def test_live_namespaces_are_clean(tmp_path: Path) -> None:
    skill_root = _namespace_fixture(
        tmp_path,
        static_roots="`ticket`, `memory`, `measure`, `workspace`, and `help`",
        manifest_list="ticket, memory, measure, and workspace",
    )
    problems = _namespace_problems(
        public_commands_check.check(
            skill_root, registry_path=REGISTRY_PATH, require_references=False
        )
    )
    assert problems == []
