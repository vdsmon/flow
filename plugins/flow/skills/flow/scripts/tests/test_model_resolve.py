"""Tests for optional stage/role agent hints."""

from __future__ import annotations

from pathlib import Path

import pytest

import _registry
import model_resolve

_STAGES = ("implement", "code_review", "e2e", "review_loop", "review_brief", "reflect", "plan")


def _workspace(
    tmp_path: Path,
    model_lines: list[str] | None = None,
    *,
    handlers: dict[str, str] | None = None,
) -> Path:
    """A workspace with a real [pipeline.handlers] block.

    The handlers are load-bearing for HANDLER-kind sites: their model vocabulary is
    derived from the handler, so a fixture without them resolves every handler site to
    "unclassifiable" and silently tests nothing.
    """
    flow = tmp_path / ".flow"
    flow.mkdir(parents=True)
    wired = dict.fromkeys(_STAGES, "inline")
    wired.update(handlers or {})
    lines = [
        "[tracker]",
        'backend = "beads"',
        "[tracker.beads]",
        'prefix = "test"',
        "[pipeline.handlers]",
        *(f'{k} = "{v}"' for k, v in wired.items()),
    ]
    if model_lines is not None:
        lines.extend(["[models]", *model_lines])
    (flow / "workspace.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return tmp_path


def test_absent_config_falls_through_to_the_registry_default(tmp_path: Path) -> None:
    # Step 3. An ABSENT [models] key is what lets the shipped default apply; contrast
    # test_disabled_hint_inherits, where an explicit "off" skips it. Collapsing those two
    # would either disable every default or ignore every opt-out.
    root = _workspace(tmp_path)
    assert model_resolve.resolve_agent_hint(root, "implement", role="implementer") == "sonnet"


def test_site_with_no_registry_default_still_inherits(tmp_path: Path) -> None:
    # Step 4, the floor: a stage that launches nothing has no default, so it inherits.
    root = _workspace(tmp_path)
    assert model_resolve.resolve_agent_hint(root, "reflect") == ""


def test_stage_hint_is_returned_verbatim(tmp_path: Path) -> None:
    root = _workspace(tmp_path, ['implement = "opus"', 'e2e = "sonnet"'])
    assert model_resolve.resolve_agent_hint(root, "implement") == "opus"
    assert model_resolve.resolve_agent_hint(root, "e2e") == "sonnet"
    assert model_resolve.resolve_agent_hint(root, "reflect") == ""


def test_stage_wide_string_is_a_model_hint_and_leaves_effort_to_the_default(
    tmp_path: Path,
) -> None:
    # Fields resolve INDEPENDENTLY: a bare model string says nothing about effort, so the
    # registry effort default still applies. Before defaults existed this returned "".
    root = _workspace(tmp_path, ['code_review = "opus"'])
    assert model_resolve.resolve_agent_hint(root, "code_review", role="reviewer") == "opus"
    assert model_resolve.resolve_agent_hint(root, "code_review", role="fixer") == "opus"
    assert (
        model_resolve.resolve_agent_hint(root, "code_review", role="reviewer", field="effort")
        == "medium"
    )


def test_role_table_string_and_inline_table(tmp_path: Path) -> None:
    root = _workspace(
        tmp_path,
        [
            "[models.code_review]",
            'reviewer = { model = "gpt-5.6-sol", effort = "high" }',
            'fixer = "sonnet"',
        ],
    )
    assert model_resolve.resolve_agent_hint(root, "code_review", role="reviewer") == "gpt-5.6-sol"
    assert (
        model_resolve.resolve_agent_hint(root, "code_review", role="reviewer", field="effort")
        == "high"
    )
    assert model_resolve.resolve_agent_hint(root, "code_review", role="fixer") == "sonnet"
    assert model_resolve.resolve_agent_hint(root, "code_review", role="fixer", field="effort") == ""


def test_single_role_table_applies_without_role(tmp_path: Path) -> None:
    # The generic launch recipe carries no role; a single-role table must not
    # silently resolve to nothing. A named-but-different role still inherits.
    root = _workspace(tmp_path, ["[models.code_review]", 'reviewer = "opus"'])
    assert model_resolve.resolve_agent_hint(root, "code_review") == "opus"
    # A role the table does not name is ABSENT, so it takes the registry default rather
    # than inheriting.
    assert model_resolve.resolve_agent_hint(root, "code_review", role="fixer") == "sonnet"


def test_multi_role_table_needs_an_explicit_role(tmp_path: Path) -> None:
    root = _workspace(tmp_path, ["[models.code_review]", 'reviewer = "opus"', 'fixer = "sonnet"'])
    assert model_resolve.resolve_agent_hint(root, "code_review") == ""


@pytest.mark.parametrize("value", ["off", "none", "false", ""])
def test_disabled_hint_inherits(tmp_path: Path, value: str) -> None:
    root = _workspace(tmp_path, [f'implement = "{value}"'])
    assert model_resolve.resolve_agent_hint(root, "implement") == ""


@pytest.mark.parametrize("value", ["off", "none", "false", ""])
def test_disabled_effort_inherits(tmp_path: Path, value: str) -> None:
    root = _workspace(
        tmp_path,
        ["[models.code_review]", f'reviewer = {{ model = "opus", effort = "{value}" }}'],
    )
    assert (
        model_resolve.resolve_agent_hint(root, "code_review", role="reviewer", field="effort") == ""
    )


def test_missing_or_malformed_workspace_fails_open(tmp_path: Path) -> None:
    assert model_resolve.resolve_agent_hint(tmp_path, "implement") == ""
    root = _workspace(tmp_path, ["implement = 3"])
    assert model_resolve.resolve_agent_hint(root, "implement") == ""


def test_cli_prints_hint(tmp_path: Path, capsys) -> None:
    root = _workspace(tmp_path, ['code_review = "opus"'])
    rc = model_resolve.cli_main(["--workspace-root", str(root), "--stage", "code_review"])
    assert rc == 0
    assert capsys.readouterr().out == "opus\n"


def test_cli_role_and_field(tmp_path: Path, capsys) -> None:
    root = _workspace(
        tmp_path, ["[models.code_review]", 'reviewer = { model = "m", effort = "high" }']
    )
    rc = model_resolve.cli_main(
        [
            "--workspace-root",
            str(root),
            "--stage",
            "code_review",
            "--role",
            "reviewer",
            "--field",
            "effort",
        ]
    )
    assert rc == 0
    assert capsys.readouterr().out == "high\n"


def test_cli_prints_nothing_when_inheriting(tmp_path: Path, capsys) -> None:
    root = _workspace(tmp_path)  # reflect launches nothing, so it has no default
    rc = model_resolve.cli_main(["--workspace-root", str(root), "--stage", "reflect"])
    assert rc == 0
    assert capsys.readouterr().out == ""


# ─── launcher-derived vocabulary (flow-0nnm) ──────────────────────────────────

_CODEX_REVIEWER = "subagent:flow:codex-reviewer"


def test_handler_decides_the_reviewer_vocabulary_not_the_parent_harness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # THE test the first design draft would have failed. Same registry tier, two
    # workspaces: the vocabulary follows the HANDLER, because that is what launches the
    # agent. A statically declared harness cannot express this.
    monkeypatch.setenv("FLOW_HARNESS", "claude-code")
    codex = _workspace(tmp_path / "a", handlers={"code_review": _CODEX_REVIEWER})
    native = _workspace(tmp_path / "b", handlers={"code_review": "inline"})
    assert model_resolve.resolve_agent_hint(codex, "code_review", role="reviewer") == "gpt-5.6-sol"
    assert model_resolve.resolve_agent_hint(native, "code_review", role="reviewer") == "opus"


def test_native_role_ignores_a_codex_stage_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The fixer is native even when the stage's handler is the Codex reviewer, so a
    # per-STAGE scheme passes every other test here and fails this one.
    monkeypatch.setenv("FLOW_HARNESS", "claude-code")
    root = _workspace(tmp_path, handlers={"code_review": _CODEX_REVIEWER})
    assert model_resolve.resolve_agent_hint(root, "code_review", role="fixer") == "sonnet"


@pytest.mark.parametrize(
    ("harness", "expected"), [("claude-code", "sonnet"), ("codex", "gpt-5.6-terra")]
)
def test_native_sites_follow_the_parent_harness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harness: str, expected: str
) -> None:
    # Forced divergence: the two tier maps must differ at `standard`, or this passes on
    # code that ignores the parent entirely.
    monkeypatch.setenv("FLOW_HARNESS", harness)
    root = _workspace(tmp_path, handlers={"code_review": "inline"})
    assert model_resolve.resolve_agent_hint(root, "code_review", role="fixer") == expected


def test_caller_site_resolves_both_fallback_branches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The assessor prefers the bundled Codex assessor but falls back to a host-native
    # agent, and only the caller knows which branch it took.
    monkeypatch.setenv("FLOW_HARNESS", "claude-code")
    root = _workspace(tmp_path)
    bundled = model_resolve.resolve_agent_hint(
        root, "plan", role="assessor", launcher_harness="codex"
    )
    fallback = model_resolve.resolve_agent_hint(root, "plan", role="assessor")
    assert (bundled, fallback) == ("gpt-5.6-sol", "opus")


def test_flow_harness_alone_never_selects_the_launcher_vocabulary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # FLOW_HARNESS names the host this process runs under, never the engine about to be
    # launched. An implementation that overloads it passes everything except this.
    monkeypatch.setenv("FLOW_HARNESS", "claude-code")
    root = _workspace(tmp_path)
    assert (
        model_resolve.resolve_agent_hint(
            root, "plan", role="assessor", launcher_harness="codex", field="model"
        )
        == "gpt-5.6-sol"
    )


def test_explicit_launcher_harness_survives_a_handler_rewrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # TOCTOU: the dispatcher binds the launcher harness into the descriptor, and the stage
    # already in flight must keep it even if [pipeline.handlers] is rewritten underneath.
    # A re-read implementation passes every other test and fails this one.
    monkeypatch.setenv("FLOW_HARNESS", "claude-code")
    root = _workspace(tmp_path, handlers={"code_review": _CODEX_REVIEWER})
    bound = "codex"
    (root / ".flow" / "workspace.toml").write_text(
        (root / ".flow" / "workspace.toml")
        .read_text(encoding="utf-8")
        .replace(_CODEX_REVIEWER, "inline"),
        encoding="utf-8",
    )
    assert model_resolve.resolve_agent_hint(root, "code_review", role="reviewer") == "opus"
    assert (
        model_resolve.resolve_agent_hint(
            root, "code_review", role="reviewer", launcher_harness=bound
        )
        == "gpt-5.6-sol"
    )


def test_unknown_handler_gets_no_default_rather_than_a_guessed_vocabulary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A third-party handler that may shell to Codex receives NO hint today. Injecting a
    # host model name would BREAK a supported configuration rather than fail to fix one,
    # so an unclassifiable launcher must fail safe to inherit.
    monkeypatch.setenv("FLOW_HARNESS", "claude-code")
    root = _workspace(tmp_path, handlers={"code_review": "none"})
    assert model_resolve.resolve_agent_hint(root, "code_review", role="reviewer") == ""


# ─── shipped-registry integrity (flow-0nnm) ───────────────────────────────────


def test_shipped_registry_parses_and_preserves_the_dispatch_roles() -> None:
    # Parse the COMPLETE shipped file, not a fragment: `agent_defaults` sits beside the
    # pre-existing `roles` ARRAY, and a fragment parses in isolation while colliding in
    # place. `records_diff_baseline` arms implement's owned-file baseline guard.
    entries = {e.name: e for e in _registry.load_registry(_registry.registry_path())}
    assert entries["implement"].roles == ["records_diff_baseline"]
    assert entries["reflect"].agent_defaults == {}


def test_every_launch_site_has_a_default_and_every_default_a_launch_site() -> None:
    # The consumer-coverage guard in data form: a default on a site nothing launches is
    # dead config, and a launch site with no default silently keeps the old behaviour.
    entries = {e.name: e for e in _registry.load_registry(_registry.registry_path())}
    declared = {(s, r) for s, e in entries.items() for r in e.agent_defaults}
    expected = {(s, r) for s, roles in _registry.LAUNCH_KINDS.items() for r in roles}
    assert declared == expected


def test_every_shipped_tier_value_is_in_the_closed_set_and_maps_on_every_harness() -> None:
    # A typo'd tier resolves to "" and silently disables the default, so pin the vocabulary
    # and require every harness map to cover every tier actually used.
    entries = _registry.load_registry(_registry.registry_path())
    used = {d["tier"] for e in entries for d in e.agent_defaults.values() if "tier" in d}
    assert used <= set(_registry.TIERS)
    tiers = _registry.load_tiers()
    assert set(tiers) == {"claude-code", "codex"}
    for harness, table in tiers.items():
        missing = used - set(table)
        assert not missing, f"{harness} tier map is missing {sorted(missing)}"


def test_effort_is_declared_only_where_a_codex_launcher_is_possible() -> None:
    # An effort default on an always-native site is dead config AND would fire the
    # unusable-effort warning against flow's own shipped registry.
    entries = _registry.load_registry(_registry.registry_path())
    with_effort = {
        (e.name, role) for e in entries for role, d in e.agent_defaults.items() if d.get("effort")
    }
    assert with_effort == {("plan", "assessor"), ("code_review", "reviewer")}
    for stage, role in with_effort:
        assert _registry.LAUNCH_KINDS[stage][role] != _registry.LAUNCH_NATIVE
