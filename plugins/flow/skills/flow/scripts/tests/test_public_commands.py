from __future__ import annotations

from pathlib import Path

import pytest

from public_commands import (
    GeneratedContentDrift,
    RegistryError,
    TargetKind,
    check_generated_block,
    classify_root_token,
    load_registry,
    render_grammar_block,
    render_help,
    render_router_block,
    route_tokens,
)

REGISTRY = Path(__file__).resolve().parents[2] / "public-commands.toml"
MAINTAIN_REFERENCE = Path(__file__).resolve().parents[2] / "references" / "command-maintain.md"
TRACKER_PATTERNS = (r"FT-\d+", r"flow-[a-z0-9]+")


def test_real_registry_is_complete_and_has_no_legacy_root_verbs() -> None:
    registry = load_registry(REGISTRY)

    assert registry.static_namespaces == (
        "ticket",
        "memory",
        "measure",
        "workspace",
        "maintain",
        "help",
    )


def test_worktree_cleanup_is_documented_as_workspace_local_two_pass() -> None:
    registry = load_registry(REGISTRY)
    command = registry.by_id["maintain.worktrees.clean"]
    reference = MAINTAIN_REFERENCE.read_text(encoding="utf-8")

    assert "invoking workspace" in command.summary.lower()
    assert "maintainer --workspace-root . --require-current" in reference
    assert "worktree-janitor sweep --workspace-root . --dry-run" in reference
    assert "absolute `target_root`" in reference
    assert '--confirmed-target "<target_root>"' in reference
    assert '--confirmed-candidate "<confirmation_id>"' in reference
    assert {command.id for command in registry.commands} >= {
        "cockpit",
        "target",
        "ticket.create",
        "ticket.group",
        "ticket.split",
        "memory.search",
        "memory.prune",
        "memory.rebuild",
        "workspace.setup",
        "workspace.inspect",
        "workspace.repair",
        "workspace.sync",
        "maintain.backlog.status",
        "maintain.backlog.drain",
        "maintain.evolution.expand",
        "maintain.worktrees.clean",
    }
    assert (
        not {
            "spec",
            "do",
            "resume",
            "revise",
            "new",
            "group",
            "slice",
            "status",
            "recall",
            "triage",
            "recover",
            "sync",
            "init",
            "queue",
            "evolve",
        }
        & registry.root_tokens
    )


def test_every_command_declares_effect_workspace_reference_and_both_harnesses() -> None:
    registry = load_registry(REGISTRY)

    for command in registry.commands:
        assert command.effect in {"read", "confirm", "write"}
        assert command.workspace in {"none", "optional", "required"}
        assert command.reference.startswith("references/")
        assert command.harnesses == frozenset({"claude-code", "codex"})


def test_target_options_encode_conflicts_and_cardinality() -> None:
    target = load_registry(REGISTRY).by_id["target"]
    options = {option.name: option for option in target.options}

    assert target.arguments[0].name == "target"
    assert target.arguments[0].cardinality == "one_or_more"
    assert options["--unattended"].conflicts == frozenset({"--verify"})
    assert options["--verify"].conflicts == frozenset({"--unattended"})
    assert options["--verify"].choices == ("express", "light", "full")
    assert options["--request"].value_type == "text"
    assert options["--route"].value_type == "agent_route"


@pytest.mark.parametrize(
    ("token", "kind", "value"),
    [
        ("FT-42", TargetKind.TICKET, "FT-42"),
        ("flow-a0d", TargetKind.TICKET, "flow-a0d"),
        ("ticket:memory", TargetKind.TICKET, "memory"),
        ("pr:17", TargetKind.PR, "17"),
        ("https://github.com/vdsmon/flow/pull/478", TargetKind.PR_URL, "478"),
        (
            "https://bitbucket.example/projects/F/repos/R/pull-requests/91/overview",
            TargetKind.PR_URL,
            "91",
        ),
    ],
)
def test_target_classification(token: str, kind: TargetKind, value: str) -> None:
    classified = classify_root_token(token, TRACKER_PATTERNS)
    assert classified.kind is kind
    assert classified.value == value


def test_static_namespace_wins_even_when_tracker_pattern_matches_everything() -> None:
    classified = classify_root_token("memory", (r".*",))
    assert classified.kind is TargetKind.NAMESPACE
    assert classified.value == "memory"


@pytest.mark.parametrize("token", ["do", "resume", "recall", "status", "not-a-ticket", "pr:0"])
def test_old_or_invalid_root_token_is_unknown(token: str) -> None:
    assert classify_root_token(token, TRACKER_PATTERNS).kind is TargetKind.UNKNOWN


def test_removed_root_token_stays_unknown_under_permissive_tracker_pattern() -> None:
    registry = load_registry(REGISTRY)
    classified = classify_root_token(
        "resume",
        (r".*",),
        static_namespaces=registry.static_namespaces,
        forbidden_root_tokens=registry.forbidden_root_tokens,
    )
    assert classified.kind is TargetKind.UNKNOWN


def test_routing_distinguishes_cockpit_explicit_help_static_command_and_targets() -> None:
    registry = load_registry(REGISTRY)

    cockpit = route_tokens([], registry, TRACKER_PATTERNS)
    assert cockpit.command is not None
    assert cockpit.command.id == "cockpit"

    scoped = route_tokens(["help", "memory"], registry, TRACKER_PATTERNS)
    assert scoped.kind == "help"
    assert scoped.topic == "memory"

    search = route_tokens(["memory", "search", "lease", "--semantic"], registry, TRACKER_PATTERNS)
    assert search.command is not None
    assert search.command.id == "memory.search"
    assert search.positionals == ("lease",)
    assert search.options == ("--semantic",)

    target = route_tokens(["FT-1", "flow-a0d", "--together"], registry, TRACKER_PATTERNS)
    assert target.command is not None
    assert target.command.id == "target"
    assert target.positionals == ("FT-1", "flow-a0d")
    assert target.options == ("--together",)


@pytest.mark.parametrize(
    "tokens",
    [
        ["ticket"],
        ["memory"],
        ["workspace"],
        ["maintain"],
        ["maintain", "evolution"],
        ["maintain", "backlog"],
        ["maintain", "worktrees"],
    ],
)
def test_incomplete_namespace_is_unknown_not_implicit_help(tokens: list[str]) -> None:
    with pytest.raises(RegistryError, match="unknown"):
        route_tokens(tokens, load_registry(REGISTRY), TRACKER_PATTERNS)


def test_static_namespace_never_falls_through_to_ticket_target() -> None:
    registry = load_registry(REGISTRY)
    with pytest.raises(RegistryError, match="unknown memory command"):
        route_tokens(["memory", "recall"], registry, (r".*",))


def test_removed_root_command_is_rejected_instead_of_treated_as_target() -> None:
    registry = load_registry(REGISTRY)
    with pytest.raises(RegistryError, match="unknown command or target"):
        route_tokens(["resume"], registry, (r".*",))

    with pytest.raises(RegistryError, match="invalid target"):
        route_tokens(["FT-1", "resume", "--together"], registry, (r".*",))


def test_route_rejects_unknown_options_and_conflicting_options() -> None:
    registry = load_registry(REGISTRY)
    with pytest.raises(RegistryError, match="unknown option"):
        route_tokens(["FT-1", "--auto"], registry, TRACKER_PATTERNS)
    with pytest.raises(RegistryError, match="conflicts"):
        route_tokens(["FT-1", "--unattended", "--verify", "full"], registry, TRACKER_PATTERNS)


def test_route_keeps_repeated_route_values_without_losing_option_names() -> None:
    route = route_tokens(
        [
            "FT-1",
            "--route",
            "planner=codex,gpt-5.6-sol,xhigh",
            "--route=implementer=claude_code,sonnet,high",
        ],
        load_registry(REGISTRY),
        TRACKER_PATTERNS,
    )
    assert route.options == ("--route", "--route")
    assert route.option_values == (
        ("--route", "planner=codex,gpt-5.6-sol,xhigh"),
        ("--route", "implementer=claude_code,sonnet,high"),
    )


@pytest.mark.parametrize(
    "value",
    [
        "unknown=codex,gpt-5.6-sol,high",
        "reflector=codex,gpt-5.6-sol",
        "reflector=generic,gpt-5.6-sol,high",
        "reflector=codex,gpt-5.6-sol,extreme",
        "reflector-codex,gpt-5.6-sol,high",
    ],
)
def test_route_rejects_invalid_atomic_agent_route_values(value: str) -> None:
    with pytest.raises(RegistryError, match="--route"):
        route_tokens(["FT-1", "--route", value], load_registry(REGISTRY), TRACKER_PATTERNS)


def test_route_rejects_duplicate_profiles_across_repeated_overrides() -> None:
    with pytest.raises(RegistryError, match="duplicate --route"):
        route_tokens(
            [
                "FT-1",
                "--route",
                "reflector=codex,gpt-5.6-sol,high",
                "--route",
                "reflector=claude_code,opus,high",
            ],
            load_registry(REGISTRY),
            TRACKER_PATTERNS,
        )


@pytest.mark.parametrize(
    "tokens",
    [
        ["memory", "search", "x", "--limit", "many"],
        ["memory", "search", "x", "--threshold", "nan"],
        ["memory", "search", "x", "--ticket", "not-a-key"],
        ["measure", "throughput", "--since", "next-week"],
        ["measure", "throughput", "--manifest", ""],
        ["FT-1", "--request="],
    ],
)
def test_route_rejects_invalid_typed_or_empty_option_values(tokens: list[str]) -> None:
    with pytest.raises(RegistryError):
        route_tokens(tokens, load_registry(REGISTRY), TRACKER_PATTERNS)


def test_renderers_are_deterministic_and_expose_logical_flow_not_host_syntax() -> None:
    registry = load_registry(REGISTRY)

    first = render_help(registry)
    assert first == render_help(registry)
    assert "FLOW <target> [<target> ...]" in first
    assert "FLOW memory search [<query>]" in first
    assert "/flow" not in first
    assert "$flow:flow" not in first

    grammar = render_grammar_block(registry)
    assert "--verify express|light|full" in grammar
    assert "--threshold <float>" in grammar
    assert "throughput --checkpoint <personal|work>" in grammar

    router = render_router_block(registry)
    assert router == render_router_block(registry)
    assert "Static namespaces win over target parsing." in router
    assert "ticket | memory | measure | workspace | maintain | help" in router


def test_generated_block_checker_detects_documentation_drift() -> None:
    registry = load_registry(REGISTRY)
    rendered = render_router_block(registry)
    document = f"before\n{rendered}after\n"

    check_generated_block(
        document,
        begin_marker="<!-- flow:public-router:begin -->",
        end_marker="<!-- flow:public-router:end -->",
        rendered=rendered,
    )
    with pytest.raises(GeneratedContentDrift, match="is stale"):
        check_generated_block(
            document.replace("Static namespaces win", "Static namespaces sometimes win"),
            begin_marker="<!-- flow:public-router:begin -->",
            end_marker="<!-- flow:public-router:end -->",
            rendered=rendered,
        )


def test_registry_validation_rejects_duplicate_routes(tmp_path: Path) -> None:
    registry = tmp_path / "public-commands.toml"
    registry.write_text(
        """
schema_version = 1
static_namespaces = ["help"]

[[command]]
id = "one"
path = ["help"]
summary = "one"
effect = "read"
workspace = "none"
reference = "references/help.md"
harnesses = ["claude-code", "codex"]

[[command]]
id = "two"
path = ["help"]
summary = "two"
effect = "read"
workspace = "none"
reference = "references/help.md"
harnesses = ["claude-code", "codex"]
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(RegistryError, match="duplicate command path"):
        load_registry(registry)


def test_registry_validation_requires_cockpit_and_target_routes(tmp_path: Path) -> None:
    registry = tmp_path / "public-commands.toml"
    registry.write_text(
        """
schema_version = 1
static_namespaces = ["help"]

[[command]]
id = "help"
path = ["help"]
summary = "help"
effect = "read"
workspace = "none"
reference = "references/help.md"
harnesses = ["claude-code", "codex"]
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(RegistryError, match=r"required commands.*cockpit.*target"):
        load_registry(registry)
