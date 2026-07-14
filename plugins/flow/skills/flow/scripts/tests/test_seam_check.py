"""Tests for the prose<->CLI seam checker.

The headline test is `test_live_docs_are_green`: it runs the checker over the
real SKILL.md + references/ so pytest fails the moment prose names a flag or
subcommand a script does not define. That is the regression gate the restructure
relies on.
"""

from __future__ import annotations

import seam_check


def test_logical_lines_join_backslash_continuations() -> None:
    text = "a \\\n  b \\\n  c\nd"
    joined = seam_check._logical_lines(text)
    assert joined[0] == (1, "a b c")
    assert joined[1] == (4, "d")


def test_find_invocations_strips_command_substitution() -> None:
    # `--abbrev-ref` lives inside $(...) and must NOT be attributed to this script.
    text = (
        "python3 ${CLAUDE_SKILL_DIR}/scripts/flow_worktree.py create \\\n"
        '  --base "$(git rev-parse --abbrev-ref HEAD)" --branch x'
    )
    invs = seam_check.find_invocations("t.md", text)
    assert len(invs) == 1
    inv = invs[0]
    assert inv.script == "flow_worktree.py"
    assert "--base" in inv.flags
    assert "--branch" in inv.flags
    assert "--abbrev-ref" not in inv.flags


def test_find_invocations_quoted_value_with_sequencing_char() -> None:
    # A `;` inside a quoted --text value must not truncate the span: the inner
    # `--auto` is part of the value, not a flag of this command.
    text = (
        "${CLAUDE_SKILL_DIR}/scripts/tracker_cli.py comment --key X "
        '--text "Judgment --auto settled; this is a safety hold"'
    )
    invs = seam_check.find_invocations("t.md", text)
    assert len(invs) == 1
    inv = invs[0]
    assert inv.script == "tracker_cli.py"
    assert "--key" in inv.flags
    assert "--text" in inv.flags
    assert "--auto" not in inv.flags


def test_find_invocations_matches_digit_bearing_script() -> None:
    # Guards the [a-z_]+ regression: without the digit in _SCRIPT_RE, the `2`
    # in embedder_model2vec.py breaks the match and the invocation goes unlinted.
    text = "${CLAUDE_SKILL_DIR}/scripts/embedder_model2vec.py --texts-file X"
    invs = seam_check.find_invocations("t.md", text)
    assert len(invs) == 1
    assert invs[0].script == "embedder_model2vec.py"


def test_find_invocations_two_commands_on_one_line() -> None:
    # Both commands of a &&-joined recipe must lint, each with its own flags.
    text = (
        "${CLAUDE_SKILL_DIR}/scripts/state.py read --key X && "
        "${CLAUDE_SKILL_DIR}/scripts/tracker_cli.py get --key Y"
    )
    invs = seam_check.find_invocations("t.md", text)
    assert len(invs) == 2
    assert invs[0].script == "state.py"
    assert invs[0].flags == ["--key"]
    assert invs[1].script == "tracker_cli.py"
    assert invs[1].flags == ["--key"]


def test_find_invocations_handles_bare_form() -> None:
    text = "   ${CLAUDE_SKILL_DIR}/scripts/tracker_cli.py --workspace-root . get --key X"
    invs = seam_check.find_invocations("t.md", text)
    assert len(invs) == 1
    assert invs[0].script == "tracker_cli.py"
    assert "--workspace-root" in invs[0].flags
    assert "--key" in invs[0].flags


def test_find_invocations_handles_harness_neutral_skill_root_placeholder() -> None:
    text = 'python3 "<skill-root>/scripts/init.py" --config "$ANSWERS"'
    invs = seam_check.find_invocations("t.md", text)
    assert len(invs) == 1
    assert invs[0].script == "init.py"
    assert invs[0].flags == ["--config"]


def test_find_facade_invocation_resolves_allowlisted_command() -> None:
    text = ".flow/runtime/flow dispatch advance --ticket X --status completed"
    invs = seam_check.find_facade_invocations("t.md", text)
    assert len(invs) == 1
    assert invs[0].facade_command == "dispatch"
    assert invs[0].script == "dispatch_stage.py"
    assert [p for p in seam_check.validate(invs[0]) if p.level == "ERROR"] == []


def test_facade_context_requires_absolute_binding_and_call_local_harness() -> None:
    relative = seam_check.find_facade_invocations(
        "t.md", ".flow/runtime/flow dispatch next --ticket X"
    )
    problems = seam_check.facade_context_problems(relative)
    assert len(problems) == 2
    assert any("workspace-relative" in problem.msg for problem in problems)
    assert any("FLOW_HARNESS" in problem.msg for problem in problems)


def test_facade_context_accepts_bound_placeholder_with_call_local_harness() -> None:
    invocations = seam_check.find_facade_invocations(
        "t.md",
        'FLOW_HARNESS="<harness>" "<facade>" dispatch next --ticket X',
    )
    assert len(invocations) == 1
    assert invocations[0].facade_path == "<facade>"
    assert seam_check.facade_context_problems(invocations) == []


def test_facade_context_accepts_absolute_literal_with_concrete_harness() -> None:
    invocations = seam_check.find_facade_invocations(
        "t.md",
        'FLOW_HARNESS=codex "/tmp/work tree/.flow/runtime/flow" dispatch next --ticket X',
    )
    assert seam_check.facade_context_problems(invocations) == []


def test_find_facade_invocation_rejects_command_outside_allowlist() -> None:
    inv = seam_check.find_facade_invocations("t.md", ".flow/runtime/flow arbitrary-script --foo")[0]
    problems = seam_check.validate(inv)
    assert len(problems) == 1
    assert problems[0].level == "ERROR"
    assert "not allowlisted" in problems[0].msg


def test_facade_missing_command_is_error() -> None:
    invocations = seam_check.find_facade_invocations("t.md", ".flow/runtime/flow")
    assert len(invocations) == 1
    errors = [
        problem for problem in seam_check.validate(invocations[0]) if problem.level == "ERROR"
    ]
    assert len(errors) == 1
    assert "missing a command" in errors[0].msg


def test_facade_path_traversal_command_is_error() -> None:
    invocations = seam_check.find_facade_invocations(
        "t.md", ".flow/runtime/flow ../../arbitrary.py"
    )
    assert len(invocations) == 1
    errors = [
        problem for problem in seam_check.validate(invocations[0]) if problem.level == "ERROR"
    ]
    assert len(errors) == 1
    assert "not allowlisted" in errors[0].msg
    assert "../../arbitrary.py" in errors[0].msg


def test_facade_uppercase_command_is_error() -> None:
    invocations = seam_check.find_facade_invocations("t.md", ".flow/runtime/flow Dispatch next")
    assert len(invocations) == 1
    errors = [
        problem for problem in seam_check.validate(invocations[0]) if problem.level == "ERROR"
    ]
    assert len(errors) == 1
    assert "not allowlisted" in errors[0].msg
    assert "Dispatch" in errors[0].msg


def test_facade_narrative_name_is_not_a_missing_command() -> None:
    text = "The `.flow/runtime/flow` executable is the post-init command seam."
    assert seam_check.find_facade_invocations("t.md", text) == []


def test_facade_inline_span_does_not_absorb_later_command_flags() -> None:
    text = (
        "Run `.flow/runtime/flow diff capture-implement-diff --ticket FT-1 --cwd .` "
        "then `git apply --cached --check patch.diff`."
    )
    invocation = seam_check.find_facade_invocations("t.md", text)[0]
    assert "--ticket" in invocation.flags
    assert "--cwd" in invocation.flags
    assert "--cached" not in invocation.flags
    assert "--check" not in invocation.flags


def test_facade_unknown_nested_subcommand_is_error() -> None:
    inv = seam_check.find_facade_invocations(
        "t.md", ".flow/runtime/flow dispatch nxt --workspace-root . --ticket FT-1"
    )[0]
    errors = [problem for problem in seam_check.validate(inv) if problem.level == "ERROR"]
    assert len(errors) == 1
    assert "unknown subcommand nxt" in errors[0].msg


def test_facade_flag_valid_on_another_subcommand_is_error() -> None:
    inv = seam_check.find_facade_invocations(
        "t.md", ".flow/runtime/flow dispatch next --ticket FT-1 --status completed"
    )[0]
    errors = [problem for problem in seam_check.validate(inv) if problem.level == "ERROR"]
    assert len(errors) == 1
    assert "--status" in errors[0].msg
    assert "not for this subcommand" in errors[0].msg


def test_facade_valid_subcommand_token_in_argument_value_cannot_mask_typo() -> None:
    inv = seam_check.find_facade_invocations(
        "t.md", ".flow/runtime/flow dispatch nxt --stage next --ticket FT-1"
    )[0]
    errors = [problem for problem in seam_check.validate(inv) if problem.level == "ERROR"]
    assert len(errors) == 1
    assert "unknown subcommand nxt" in errors[0].msg
    assert inv.subcommand is None


def test_facade_subcommand_flag_before_subcommand_is_error() -> None:
    inv = seam_check.find_facade_invocations(
        "t.md", ".flow/runtime/flow dispatch --workspace-root . next --ticket FT-1"
    )[0]
    errors = [problem for problem in seam_check.validate(inv) if problem.level == "ERROR"]
    assert any(
        "--workspace-root" in problem.msg and "before subcommand" in problem.msg
        for problem in errors
    )


def test_find_facade_invocation_recognizes_quoted_absolute_path() -> None:
    text = '"/tmp/work tree/.flow/runtime/flow" dispatch next --ticket FT-1'
    invs = seam_check.find_facade_invocations("t.md", text)
    assert len(invs) == 1
    assert invs[0].facade_command == "dispatch"
    assert invs[0].script == "dispatch_stage.py"
    assert [problem for problem in seam_check.validate(invs[0]) if problem.level == "ERROR"] == []


def test_facade_global_flag_before_subcommand_remains_valid() -> None:
    inv = seam_check.find_facade_invocations(
        "t.md", ".flow/runtime/flow tracker --workspace-root . get --key FT-1"
    )[0]
    assert [problem for problem in seam_check.validate(inv) if problem.level == "ERROR"] == []
    assert inv.subcommand == "get"


def test_direct_launcher_repair_is_parsed_from_flow_skill_variable() -> None:
    text = 'python3 "${FLOW_SKILL_DIR}/scripts/flow_launcher.py" --workspace-root .'
    invs = seam_check.find_invocations("t.md", text)
    assert len(invs) == 1
    assert invs[0].script == "flow_launcher.py"


def test_stale_direct_invocation_rejected_outside_bootstrap_allowlist() -> None:
    stale = seam_check.find_invocations(
        "t.md", "${CLAUDE_SKILL_DIR}/scripts/dispatch_stage.py next --ticket X"
    )
    allowed = seam_check.find_invocations(
        "t.md",
        "${CLAUDE_SKILL_DIR}/scripts/init.py --reconfigure\n"
        "${FLOW_SKILL_DIR}/scripts/flow_launcher.py --workspace-root .",
    )
    assert len(seam_check.stale_direct_invocation_problems(stale)) == 1
    assert seam_check.stale_direct_invocation_problems(allowed) == []


def test_bare_script_invocation_is_rejected_as_a_facade_escape() -> None:
    invocations = seam_check.find_bare_script_invocations(
        "t.md",
        "Retry with `recover.py retry --stage implement --ticket FT-1`.",
    )
    assert len(invocations) == 1
    assert invocations[0].script == "recover.py"
    problems = seam_check.stale_direct_invocation_problems(invocations)
    assert len(problems) == 1
    assert "absolute <facade>" in problems[0].msg


def test_bare_script_filename_without_an_executable_surface_is_ignored() -> None:
    text = "The dispatcher (`dispatch_stage.py`) owns state; `recover.py` is its recovery peer."
    assert seam_check.find_bare_script_invocations("t.md", text) == []


def test_surface_of_real_script_has_subcommands_and_flags() -> None:
    surface = seam_check.surface_of("dispatch_stage.py")
    assert surface is not None
    assert {"init", "next", "advance", "release"} <= surface.subcommands
    assert "--ticket" in surface.all_sub_flags()


def test_surface_of_global_flag_before_subcommand() -> None:
    # tracker_cli puts --workspace-root before the subcommand: it must still be
    # discovered as a subcommand-bearing script with --key under `get`.
    surface = seam_check.surface_of("tracker_cli.py")
    assert surface is not None
    assert "get" in surface.subcommands
    assert "--key" in surface.all_sub_flags()


def test_validate_flags_unknown_flag_as_error() -> None:
    inv = seam_check.Invocation(
        doc="t.md",
        line=1,
        script="dispatch_stage.py",
        subcommand=None,
        flags=["--ticket", "--definitely-not-a-flag"],
        raw="dispatch_stage.py advance --ticket X --definitely-not-a-flag Y",
    )
    problems = seam_check.validate(inv)
    errors = [p for p in problems if p.level == "ERROR"]
    assert len(errors) == 1
    assert "--definitely-not-a-flag" in errors[0].msg
    assert inv.subcommand == "advance"


def test_validate_known_flags_pass() -> None:
    inv = seam_check.Invocation(
        doc="t.md",
        line=1,
        script="dispatch_stage.py",
        subcommand=None,
        flags=["--ticket", "--workspace-root", "--stage", "--status"],
        raw="dispatch_stage.py advance --ticket X --workspace-root . --stage s --status completed",
    )
    assert [p for p in seam_check.validate(inv) if p.level == "ERROR"] == []


def test_forwarder_folds_metric_surface() -> None:
    # recall.py --metric forwards to metric.cli_main, so metric's flags resolve.
    inv = seam_check.Invocation(
        doc="t.md",
        line=1,
        script="recall.py",
        subcommand=None,
        flags=["--metric", "--namespace", "--workspace-root"],
        raw="recall.py --metric tickets-per-week --namespace ns --workspace-root .",
    )
    assert [p for p in seam_check.validate(inv) if p.level == "ERROR"] == []


def test_host_specific_public_recipe_is_rejected_from_reusable_prose() -> None:
    text = "Use `/flow workspace repair FT-1` or:\n```\n$flow:flow resume FT-1\n```\n"
    problems = seam_check.host_specific_invocation_problems("t.md", text)
    assert len(problems) == 2
    assert all("logical FLOW" in problem.msg for problem in problems)


def test_bare_host_trigger_mapping_is_not_a_recipe_error() -> None:
    text = "Claude Code renders FLOW as `/flow`; Codex renders it as `$flow:flow`."
    assert seam_check.host_specific_invocation_problems("t.md", text) == []


def test_live_docs_are_green() -> None:
    """The real SKILL.md + references/ must have zero prose<->CLI seam errors."""
    assert seam_check.main([]) == 0


def test_live_router_carries_rooted_cross_harness_context() -> None:
    skill = (seam_check.SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    harness = (seam_check.SKILL_ROOT / "references" / "harness.md").read_text(encoding="utf-8")
    spec = (seam_check.SKILL_ROOT / "references" / "delivery-plan.md").read_text(encoding="utf-8")

    for field in ("arguments", "skill_root", "task_root", "run_root", "facade", "capabilities"):
        assert field in skill
    for adapter in ("Claude Code", "Codex", "Generic fallback"):
        assert adapter in harness
    for prompt_field in (
        "Workspace root:",
        "Skill root:",
        "Facade:",
        "Harness:",
        "Ticket dir:",
        "Reference path:",
        "Artifact path:",
    ):
        assert prompt_field in skill
    assert "result.worktree" in spec
    assert "binding, not the convenience switch" in spec.lower()


def test_live_portable_references_use_adapter_capabilities_not_claude_tool_calls() -> None:
    portable = [
        seam_check.SKILL_ROOT / "references" / "command-ticket.md",
        seam_check.SKILL_ROOT / "references" / "command-memory.md",
        seam_check.SKILL_ROOT / "references" / "command-ticket.md",
        seam_check.SKILL_ROOT / "references" / "delivery-plan.md",
        seam_check.SKILL_ROOT / "references" / "stage-plan.md",
    ]
    text = "\n".join(path.read_text(encoding="utf-8") for path in portable)
    for claude_only_instruction in (
        "Use `AskUserQuestion`",
        "one `AskUserQuestion`",
        "via `AskUserQuestion`",
        "then call `advisor()`",
        "a `ToolSearch` for `advisor`",
        "a `general-purpose` `Agent`",
    ):
        assert claude_only_instruction not in text
    assert "adapter's" in text
    assert "user-input capability" in text
    assert "fresh independent agent" in text


def test_live_post_init_prose_has_no_bare_script_invocation() -> None:
    escaped = []
    for doc in seam_check.docs_to_check():
        text = doc.read_text(encoding="utf-8")
        escaped.extend(seam_check.find_bare_script_invocations(doc.name, text))
    assert escaped == []


def test_live_codex_route_requires_structured_activation() -> None:
    skill = (seam_check.SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    do_ref = (seam_check.SKILL_ROOT / "references" / "delivery-loop.md").read_text(encoding="utf-8")
    assert "structured native launch acceptance" in skill
    assert "Only an `active` receipt proves exact execution" in do_ref
    assert "Codex" in do_ref
    assert "does not retry" in do_ref


def test_live_init_carries_an_absolute_answers_path_across_calls() -> None:
    init_ref = (seam_check.SKILL_ROOT / "references" / "command-workspace.md").read_text(
        encoding="utf-8"
    )
    assert "answers_path" in init_ref
    assert "<absolute task_root>" in init_ref
    assert "ANSWERS=$(mktemp" not in init_ref
    assert '"$(pwd)"' not in init_ref


def test_live_harness_selector_is_call_local_and_explicit() -> None:
    paths = [
        seam_check.SKILL_ROOT / "SKILL.md",
        seam_check.SKILL_ROOT / "references" / "harness.md",
        seam_check.SKILL_ROOT / "references" / "command-workspace.md",
    ]
    text = "\n".join(path.read_text(encoding="utf-8") for path in paths)
    assert "FLOW_HARNESS" in text
    for value in ("codex", "claude-code", "generic"):
        assert value in text
    assert "export FLOW_HARNESS" not in text
    assert "same command" in text or "call-local" in text


def test_live_portable_path_never_depends_on_persistent_cd_or_automatic_spill_recovery() -> None:
    portable_docs = [
        seam_check.SKILL_ROOT / "SKILL.md",
        seam_check.SKILL_ROOT / "references" / "harness.md",
        seam_check.SKILL_ROOT / "references" / "delivery-plan.md",
        seam_check.SKILL_ROOT / "references" / "delivery-loop.md",
        seam_check.SKILL_ROOT / "references" / "delivery-revision.md",
    ]
    text = "\n".join(path.read_text(encoding="utf-8") for path in portable_docs)
    assert "cd the persistent Bash cwd" not in text
    assert "cd into the worktree dir in the persistent shell" not in text
    assert "Only the off-CC AGENTS.md entry point passes `--recover-spill`" not in text
    assert "Do not pass `--recover-spill` automatically" in text


# --- generated managed AGENTS guidance --------------------------------------


_VALID_AGENTS_STANZA = '''
_AGENTS_STANZA = """<!-- flow:begin -->
A generic adapter supplies the absolute `FLOW_SKILL_DIR`; do not search for it.
Read `$FLOW_SKILL_DIR/SKILL.md` and `$FLOW_SKILL_DIR/references/harness.md`.
Route with `public-commands.toml`. Static namespaces win; unknown or removed forms stop.
Select `codex`, `claude-code`, or `generic`; set `FLOW_HARNESS=<identity>` in the same
call as each Flow command, never as an export.
Perform read-only planning, then stop until the user approves.
After approval, adopt the absolute worktree as the run root and its `.flow/runtime/flow` facade.
Harness calls need an explicit workdir because a prior `cd` is never persistent state.
Never relocate dirty main-checkout files; recovery requires proven agent provenance.
<!-- flow:end -->
"""
'''


def test_managed_agents_guidance_accepts_semantic_contract(tmp_path) -> None:
    init_path = tmp_path / "init.py"
    init_path.write_text(_VALID_AGENTS_STANZA, encoding="utf-8")
    assert seam_check.managed_agents_guidance_drift(init_path) == []


def test_managed_agents_guidance_is_not_satisfied_by_any_agents_mention(tmp_path) -> None:
    init_path = tmp_path / "init.py"
    init_path.write_text(
        "# AGENTS.md should mention FLOW_SKILL_DIR, SKILL.md, "
        "references/harness.md, and .flow/runtime/flow\n",
        encoding="utf-8",
    )
    drift = seam_check.managed_agents_guidance_drift(init_path)
    assert any("_AGENTS_STANZA" in detail for detail in drift)


def test_managed_agents_guidance_requires_stable_markers(tmp_path) -> None:
    init_path = tmp_path / "init.py"
    init_path.write_text(
        _VALID_AGENTS_STANZA.replace("<!-- flow:end -->", "<!-- flow:done -->"),
        encoding="utf-8",
    )
    drift = seam_check.managed_agents_guidance_drift(init_path)
    assert any("managed markers" in detail for detail in drift)


def test_managed_agents_guidance_reports_missing_contract(tmp_path) -> None:
    init_path = tmp_path / "init.py"
    init_path.write_text(
        _VALID_AGENTS_STANZA.replace(
            "Read `$FLOW_SKILL_DIR/SKILL.md` and `$FLOW_SKILL_DIR/references/harness.md`.\n", ""
        ),
        encoding="utf-8",
    )
    drift = seam_check.managed_agents_guidance_drift(init_path)
    assert any("router and harness guidance" in detail for detail in drift)


def test_managed_agents_guidance_requires_call_local_harness_selection(tmp_path) -> None:
    init_path = tmp_path / "init.py"
    init_path.write_text(
        _VALID_AGENTS_STANZA.replace(
            "Select `codex`, `claude-code`, or `generic`; set `FLOW_HARNESS=<identity>` "
            "in the same\ncall as each Flow command, never as an export.\n",
            "",
        ),
        encoding="utf-8",
    )
    drift = seam_check.managed_agents_guidance_drift(init_path)
    assert any("harness selector" in detail for detail in drift)


def test_main_fails_on_managed_agents_guidance_drift(monkeypatch) -> None:
    monkeypatch.setattr(
        seam_check,
        "managed_agents_guidance_drift",
        lambda *args, **kwargs: ["missing facade guidance"],
    )
    assert seam_check.main([]) == 1


def test_module_md_covers_all_live_scripts() -> None:
    """Every non-test script on disk must be named in the real MODULE.md."""
    assert seam_check.scripts_missing_from_module_md() == set()


def test_main_fails_on_module_md_gap(monkeypatch) -> None:
    monkeypatch.setattr(seam_check, "scripts_missing_from_module_md", lambda *a, **k: {"foo.py"})
    assert seam_check.main([]) == 1


def test_flags_script_missing_from_module_md(tmp_path) -> None:
    (tmp_path / "foo.py").write_text("")
    missing = seam_check.scripts_missing_from_module_md(
        scripts_dir=tmp_path, module_text="nothing here"
    )
    assert missing == {"foo.py"}


def test_underscore_libs_are_required(tmp_path) -> None:
    (tmp_path / "_bar.py").write_text("")
    missing = seam_check.scripts_missing_from_module_md(
        scripts_dir=tmp_path, module_text="some other text"
    )
    assert "_bar.py" in missing


def test_excludes_test_and_conftest(tmp_path) -> None:
    (tmp_path / "test_x.py").write_text("")
    (tmp_path / "conftest.py").write_text("")
    missing = seam_check.scripts_missing_from_module_md(scripts_dir=tmp_path, module_text="")
    assert missing == set()


def _write_registry(path, description: str) -> None:
    path.write_text(
        '[[stage]]\nname = "commit"\ndescription = "' + description + '"\n',
        encoding="utf-8",
    )


def test_registry_description_drift_is_flagged(tmp_path) -> None:
    # A hyphenated reference (compose-commit.py) for the real compose_commit.py
    # must be flagged literally, not normalized away.
    registry = tmp_path / "stage-registry.toml"
    _write_registry(registry, "Compose commit (compose-commit.py skeleton).")
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "compose_commit.py").write_text("")
    missing = seam_check.scripts_missing_from_registry_descriptions(
        registry_path=registry, scripts_dir=scripts_dir
    )
    assert missing == {"compose-commit.py"}


def test_registry_description_real_underscore_names_pass(tmp_path) -> None:
    registry = tmp_path / "stage-registry.toml"
    _write_registry(registry, "Open the PR via create_pr.py.")
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "create_pr.py").write_text("")
    missing = seam_check.scripts_missing_from_registry_descriptions(
        registry_path=registry, scripts_dir=scripts_dir
    )
    assert missing == set()


def test_registry_description_hyphen_basename_matched() -> None:
    # Guards against accidentally reusing `[a-z_]+\.py`, which cannot match a
    # hyphenated basename.
    assert seam_check._REGISTRY_SCRIPT_RE.findall("see compose-commit.py here") == [
        "compose-commit.py"
    ]


def test_main_fails_on_registry_description_gap(monkeypatch) -> None:
    monkeypatch.setattr(seam_check, "scripts_missing_from_module_md", lambda *a, **k: set())
    monkeypatch.setattr(
        seam_check, "scripts_missing_from_registry_descriptions", lambda *a, **k: {"foo.py"}
    )
    assert seam_check.main([]) == 1


# --- MODULE.md 'imported by' row drift ---------------------------------------


def test_importer_drift_clean_row_matches(tmp_path) -> None:
    (tmp_path / "a.py").write_text("")
    (tmp_path / "b.py").write_text("import a\n")
    text = "| `a.py` (lib) | x | imported by b |\n"
    assert seam_check.module_md_importer_drift(scripts_dir=tmp_path, module_text=text) == []


def test_importer_drift_phantom_importer(tmp_path) -> None:
    (tmp_path / "a.py").write_text("")
    (tmp_path / "b.py").write_text("import a\n")
    (tmp_path / "c.py").write_text("")  # real stem, but does not import a
    text = "| `a.py` (lib) | x | imported by b, c |\n"
    drifts = seam_check.module_md_importer_drift(scripts_dir=tmp_path, module_text=text)
    assert len(drifts) == 1
    assert "c" in drifts[0].phantom
    assert drifts[0].missing == frozenset()


def test_importer_drift_missing_importer(tmp_path) -> None:
    (tmp_path / "a.py").write_text("")
    (tmp_path / "b.py").write_text("import a\n")
    (tmp_path / "c.py").write_text("import a\n")
    text = "| `a.py` (lib) | x | imported by b |\n"
    drifts = seam_check.module_md_importer_drift(scripts_dir=tmp_path, module_text=text)
    assert len(drifts) == 1
    assert "c" in drifts[0].missing
    assert drifts[0].phantom == frozenset()


def test_importer_drift_prose_row_skipped_per_row(tmp_path) -> None:
    (tmp_path / "a.py").write_text("")
    (tmp_path / "x.py").write_text("import a\n")
    (tmp_path / "foo.py").write_text("")
    # `adapters` is not a real stem -> the whole row is skipped (not flagged).
    # The second, enumerable row IS still checked: x imports a, so it is clean.
    text = (
        "| `a.py` (lib) | y | imported by the adapters + foo |\n"
        "| `a.py` (lib) | z | imported by x |\n"
    )
    assert seam_check.module_md_importer_drift(scripts_dir=tmp_path, module_text=text) == []


def test_importer_drift_reverse_direction_guard(tmp_path) -> None:
    # `vp.py` declares `imports a, b, c` (real stems) but NOT `imported by`.
    # The anchor must skip it so it is never inverted into a phantom row.
    (tmp_path / "vp.py").write_text("")
    (tmp_path / "a.py").write_text("")
    (tmp_path / "b.py").write_text("")
    (tmp_path / "c.py").write_text("")
    text = "| `vp.py` (lib) | imports a, b, c |\n"
    assert seam_check.module_md_importer_drift(scripts_dir=tmp_path, module_text=text) == []


def test_true_importers_captures_lazy_in_function_import(tmp_path) -> None:
    (tmp_path / "a.py").write_text("")
    (tmp_path / "b.py").write_text("def f():\n    from a import X\n    return X\n")
    importers = seam_check.true_importers(scripts_dir=tmp_path)
    assert importers.get("a") == {"b"}


def test_main_fails_on_importer_drift(monkeypatch) -> None:
    monkeypatch.setattr(
        seam_check,
        "module_md_importer_drift",
        lambda *a, **k: [
            seam_check.ImporterDrift(module="a", missing=frozenset({"c"}), phantom=frozenset())
        ],
    )
    assert seam_check.main([]) == 1


def test_module_md_importer_rows_match_imports() -> None:
    """Every enumerable MODULE.md 'imported by' row must match the AST truth."""
    assert seam_check.module_md_importer_drift() == []


# --- MODULE.md phantom rows ---------------------------------------------------


def test_phantom_row_flagged(tmp_path) -> None:
    (tmp_path / "a.py").write_text("")
    text = "| `a.py` | live |\n| `gone.py` | deleted script |\n"
    assert seam_check.phantom_module_md_rows(scripts_dir=tmp_path, module_text=text) == {"gone.py"}


def test_phantom_check_ignores_role_cell_mentions(tmp_path) -> None:
    # A historical mention inside a Role cell is prose, not a row.
    (tmp_path / "a.py").write_text("")
    text = "| `a.py` | absorbed from queue_reviews.py (epic) |\n"
    assert seam_check.phantom_module_md_rows(scripts_dir=tmp_path, module_text=text) == set()


def test_phantom_check_resolves_test_files_against_tests_dir(tmp_path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("")
    text = "| `test_a.py` | frozen corpus gate |\n"
    assert seam_check.phantom_module_md_rows(scripts_dir=tmp_path, module_text=text) == set()


def test_module_md_has_no_phantom_rows() -> None:
    """Every row in the real MODULE.md must document a script that exists."""
    assert seam_check.phantom_module_md_rows() == set()


def test_main_fails_on_phantom_row(monkeypatch) -> None:
    monkeypatch.setattr(seam_check, "phantom_module_md_rows", lambda *a, **k: {"gone.py"})
    assert seam_check.main([]) == 1


# --- MODULE.md forward "imports x, y" claims ----------------------------------


def test_forward_import_claim_clean(tmp_path) -> None:
    (tmp_path / "a.py").write_text("")
    (tmp_path / "vp.py").write_text("import a\n")
    text = "| `vp.py` (lib) | x | imports a |\n"
    assert seam_check.module_md_forward_import_drift(scripts_dir=tmp_path, module_text=text) == []


def test_forward_import_claim_stale(tmp_path) -> None:
    (tmp_path / "a.py").write_text("")
    (tmp_path / "b.py").write_text("")
    (tmp_path / "vp.py").write_text("import a\n")
    text = "| `vp.py` (lib) | x | imports a, b |\n"
    drifts = seam_check.module_md_forward_import_drift(scripts_dir=tmp_path, module_text=text)
    assert drifts == [("vp", "b")]


def test_forward_import_prose_claim_skipped(tmp_path) -> None:
    (tmp_path / "vp.py").write_text("")
    # `nothing` is not a local stem -> the claim is prose, skipped.
    text = "| `vp.py` (lib) | x | imports nothing at dispatch time |\n"
    assert seam_check.module_md_forward_import_drift(scripts_dir=tmp_path, module_text=text) == []


def test_module_md_forward_import_rows_match_imports() -> None:
    """Every enumerable forward 'imports' claim in the real MODULE.md holds."""
    assert seam_check.module_md_forward_import_drift() == []


def test_main_fails_on_forward_import_drift(monkeypatch) -> None:
    monkeypatch.setattr(seam_check, "module_md_forward_import_drift", lambda *a, **k: [("vp", "b")])
    assert seam_check.main([]) == 1


# --- guard-file list <-> triage._GUARD_FILES ----------------------------------


def test_triage_guard_files_parsed_from_source() -> None:
    parsed = seam_check.triage_guard_files()
    assert "flow_worktree.py" in parsed
    assert "SKILL.md" in parsed


def test_guard_lists_match_triage() -> None:
    """The canonical prose guard-file enumeration must equal triage._GUARD_FILES."""
    assert seam_check.guard_file_list_drift() == []


def test_guard_list_divergence_flagged(tmp_path) -> None:
    guard = frozenset({"a.py", "b.py", "SKILL.md"})
    doc1 = tmp_path / "one.md"
    doc1.write_text("a safety-machinery guard file (`a.py`, `b.py`) is hot\n")
    doc2 = tmp_path / "two.md"
    doc2.write_text("a safety-machinery guard file (`a.py`) is hot\n")
    drifts = seam_check.guard_file_list_drift(docs=[doc1, doc2], guard_files=guard)
    assert len(drifts) == 1
    assert drifts[0][0] == "two.md"
    assert "b.py" in drifts[0][2]


def test_guard_list_extra_member_flagged(tmp_path) -> None:
    guard = frozenset({"a.py", "b.py"})
    doc1 = tmp_path / "one.md"
    doc1.write_text("a safety-machinery guard file (`a.py`, `b.py`) is hot\n")
    doc2 = tmp_path / "two.md"
    doc2.write_text("a safety-machinery guard file (`a.py`, `b.py`, `c.py`) is hot\n")
    drifts = seam_check.guard_file_list_drift(docs=[doc1, doc2], guard_files=guard)
    assert len(drifts) == 1
    assert "c.py" in drifts[0][2]


def test_guard_list_missing_anchors_is_a_drift(tmp_path) -> None:
    # The phrase moving out of the docs must not silently disarm the gate.
    doc = tmp_path / "one.md"
    doc.write_text("no anchor here\n")
    drifts = seam_check.guard_file_list_drift(docs=[doc], guard_files=frozenset({"a.py"}))
    assert len(drifts) == 1
    assert "expected >= 1" in drifts[0][2]


def test_main_fails_on_guard_list_drift(monkeypatch) -> None:
    monkeypatch.setattr(
        seam_check, "guard_file_list_drift", lambda *a, **k: [("one.md", 3, "missing ['b.py']")]
    )
    assert seam_check.main([]) == 1


# --- MODULE.md surface-cell completeness ------------------------------------


def _surface(*subs: str) -> seam_check.Surface:
    return seam_check.Surface(subcommands=frozenset(subs), global_flags=frozenset(), sub_flags={})


def test_surface_cell_clean_row_matches() -> None:
    text = "| `a.py` | x | `create` / `reap` |\n"
    lookup = lambda name: _surface("create", "reap")  # noqa: E731
    assert seam_check.module_md_surface_cell_drift(module_text=text, surface_lookup=lookup) == []


def test_surface_cell_under_enumerated_row() -> None:
    text = "| `a.py` | x | `create` |\n"
    lookup = lambda name: _surface("create", "reap")  # noqa: E731
    drifts = seam_check.module_md_surface_cell_drift(module_text=text, surface_lookup=lookup)
    assert len(drifts) == 1
    assert drifts[0].module == "a"
    assert drifts[0].missing == frozenset({"reap"})


def test_surface_cell_lib_row_skipped() -> None:
    # `(lib)` rows are documented by importer list, not a CLI surface -> skipped
    # even when the surface would be under-enumerated.
    text = "| `a.py` (lib) | x | imported by `create` |\n"
    lookup = lambda name: _surface("create", "reap")  # noqa: E731
    assert seam_check.module_md_surface_cell_drift(module_text=text, surface_lookup=lookup) == []


def test_surface_cell_zero_enumerated_row_skipped() -> None:
    # The cell names none of the real subs (e.g. metric.py's `(via recall.py
    # --metric)`) -> not a surface listing -> skipped.
    text = "| `a.py` | x | (via recall.py --metric) |\n"
    lookup = lambda name: _surface("create", "reap")  # noqa: E731
    assert seam_check.module_md_surface_cell_drift(module_text=text, surface_lookup=lookup) == []


def test_surface_cell_boundary_list_assigned() -> None:
    # `list-assigned` in the cell must NOT count as enumerating a `list` sub.
    text = "| `a.py` | x | `list-assigned` |\n"
    lookup = lambda name: _surface("list", "list-assigned")  # noqa: E731
    drifts = seam_check.module_md_surface_cell_drift(module_text=text, surface_lookup=lookup)
    assert len(drifts) == 1
    assert drifts[0].missing == frozenset({"list"})


def test_main_fails_on_surface_cell_drift(monkeypatch) -> None:
    monkeypatch.setattr(seam_check, "scripts_missing_from_module_md", lambda *a, **k: set())
    monkeypatch.setattr(
        seam_check, "scripts_missing_from_registry_descriptions", lambda *a, **k: set()
    )
    monkeypatch.setattr(seam_check, "module_md_importer_drift", lambda *a, **k: [])
    monkeypatch.setattr(
        seam_check,
        "module_md_surface_cell_drift",
        lambda *a, **k: [seam_check.SurfaceCellDrift(module="a", missing=frozenset({"reap"}))],
    )
    assert seam_check.main([]) == 1


def test_module_md_surface_cells_match_argparse() -> None:
    """Every live MODULE.md surface cell must fully enumerate the script's subcommands."""
    assert seam_check.module_md_surface_cell_drift() == []


# --- stage->reference_doc map re-enumeration drift ---------------------------


def _write_stage_registry(path, names: list[str]) -> None:
    body = "".join(
        f'[[stage]]\nname = "{n}"\nreference_doc = "references/stage-{n}.md"\n\n' for n in names
    )
    path.write_text(body, encoding="utf-8")


def test_stage_doc_re_matches_e2e_digit() -> None:
    # Guards the [a-z_]+ regression: without the digit, stage-e2e.md is missed.
    assert seam_check._STAGE_DOC_RE.findall("see references/stage-e2e.md") == ["stage-e2e.md"]


def test_live_registry_yields_ten_stage_docs() -> None:
    import tomllib

    registry = seam_check.SKILL_ROOT / "stage-registry.toml"
    data = tomllib.loads(registry.read_text(encoding="utf-8"))
    basenames: set[str] = set()
    for stage in data.get("stage", []):
        basenames |= set(seam_check._STAGE_DOC_RE.findall(stage.get("reference_doc", "")))
    assert len(basenames) == 10
    assert "stage-e2e.md" in basenames


def test_exact_three_distinct_citations_flagged(tmp_path) -> None:
    # Exactly 3 DISTINCT registry stage-docs -> flagged. Discriminates >=3 from >3.
    registry = tmp_path / "stage-registry.toml"
    _write_stage_registry(registry, ["plan", "implement", "commit", "merge"])
    doc = tmp_path / "verb-x.md"
    doc.write_text(
        "see references/stage-plan.md and references/stage-implement.md and "
        "references/stage-commit.md",
        encoding="utf-8",
    )
    over = seam_check.docs_over_stage_doc_citation_limit(registry_path=registry, docs=[doc])
    assert over == {"verb-x.md": 3}


def test_exact_two_distinct_citations_clean(tmp_path) -> None:
    registry = tmp_path / "stage-registry.toml"
    _write_stage_registry(registry, ["plan", "implement", "commit"])
    doc = tmp_path / "verb-x.md"
    doc.write_text(
        "see references/stage-plan.md and references/stage-implement.md",
        encoding="utf-8",
    )
    over = seam_check.docs_over_stage_doc_citation_limit(registry_path=registry, docs=[doc])
    assert over == {}


def test_non_registry_token_does_not_inflate(tmp_path) -> None:
    # 3 stage-*.md tokens cited, but only 2 live in the synthetic registry; the
    # intersection drops the foreign token, so count is 2 -> clean.
    registry = tmp_path / "stage-registry.toml"
    _write_stage_registry(registry, ["plan", "implement"])
    doc = tmp_path / "verb-x.md"
    doc.write_text(
        "references/stage-plan.md references/stage-implement.md references/stage-bogus.md",
        encoding="utf-8",
    )
    over = seam_check.docs_over_stage_doc_citation_limit(registry_path=registry, docs=[doc])
    assert over == {}


def test_main_fails_on_stage_doc_citation_offender(monkeypatch) -> None:
    monkeypatch.setattr(seam_check, "scripts_missing_from_module_md", lambda *a, **k: set())
    monkeypatch.setattr(
        seam_check, "scripts_missing_from_registry_descriptions", lambda *a, **k: set()
    )
    monkeypatch.setattr(seam_check, "module_md_importer_drift", lambda *a, **k: [])
    monkeypatch.setattr(
        seam_check, "docs_over_stage_doc_citation_limit", lambda *a, **k: {"SKILL.md": 4}
    )
    assert seam_check.main([]) == 1


def test_live_corpus_no_stage_doc_reenumeration() -> None:
    """No live Flow doc statically re-enumerates the stage-to-reference map."""
    assert seam_check.docs_over_stage_doc_citation_limit() == {}


# --- descriptor-key gate -----------------------------------------------------

_DISPATCH_SRC = """
def cmd_next(next_stage, sha, r, ref):
    payload = {"done": False, "stage": next_stage, "head_sha": sha, "roles": r}
    payload["reference_doc"] = ref
    return 0, payload


def blocked(failed, detail):
    return 0, {"done": False, "blocked_by": failed, "reason": detail}
"""


def test_emitted_keys_include_dict_and_subscript_assigns(tmp_path) -> None:
    src = tmp_path / "dispatch_stage.py"
    src.write_text(_DISPATCH_SRC, encoding="utf-8")
    emitted = seam_check.emitted_descriptor_keys(src)
    assert emitted is not None
    # dict-literal keys AND the `payload["reference_doc"] = ...` subscript assign.
    assert {"done", "stage", "head_sha", "roles", "blocked_by", "reason"} <= emitted
    assert "reference_doc" in emitted


def test_emitted_keys_none_on_unparseable(tmp_path) -> None:
    src = tmp_path / "dispatch_stage.py"
    src.write_text("def x( :\n", encoding="utf-8")
    assert seam_check.emitted_descriptor_keys(src) is None


def test_prose_descriptor_anchors_extract_keys() -> None:
    text = (
        "handler descriptor with `stage`, `handler_type`, optional `reference_doc`.\n"
        "if `descriptor.roles` includes something.\n"
        '`{"done": false, "blocked_by": "<s>", "reason": "<t>"}`\n'
    )
    keys = {k for _, k in seam_check.prose_descriptor_key_citations(text)}
    assert {
        "stage",
        "handler_type",
        "reference_doc",
        "roles",
        "done",
        "blocked_by",
        "reason",
    } <= keys


def test_prose_descriptor_ignores_foreign_json_without_done() -> None:
    # Another script's JSON object (no `"done"` key) must not leak its keys.
    text = '`{"backend": "beads", "prefix": "flow"}`\n'
    assert seam_check.prose_descriptor_key_citations(text) == []


def test_descriptor_key_drift_flags_renamed_citation(tmp_path) -> None:
    doc = tmp_path / "SKILL.md"
    doc.write_text("handler descriptor with `stage`, `head_commit`.\n", encoding="utf-8")
    # Truth emits `head_sha`, prose says the renamed-away `head_commit`.
    drift = seam_check.descriptor_key_drift(docs=[doc], emitted={"stage", "head_sha"})
    assert ("SKILL.md", 1, "head_commit") in drift
    assert all(d[2] != "stage" for d in drift)


def test_descriptor_key_drift_noop_without_emitted(tmp_path) -> None:
    doc = tmp_path / "SKILL.md"
    doc.write_text("handler descriptor with `whatever`.\n", encoding="utf-8")
    # An empty/None emitted set (unparseable dispatch) makes the gate no-op
    # rather than mass-flag every citation.
    assert seam_check.descriptor_key_drift(docs=[doc], emitted=set()) == []


def test_main_fails_on_descriptor_key_drift(monkeypatch) -> None:
    monkeypatch.setattr(seam_check, "scripts_missing_from_module_md", lambda *a, **k: set())
    monkeypatch.setattr(
        seam_check, "scripts_missing_from_registry_descriptions", lambda *a, **k: set()
    )
    monkeypatch.setattr(seam_check, "module_md_importer_drift", lambda *a, **k: [])
    monkeypatch.setattr(seam_check, "module_md_surface_cell_drift", lambda *a, **k: [])
    monkeypatch.setattr(seam_check, "docs_over_stage_doc_citation_limit", lambda *a, **k: {})
    monkeypatch.setattr(seam_check, "role_literal_drift", lambda *a, **k: [])
    monkeypatch.setattr(
        seam_check, "descriptor_key_drift", lambda *a, **k: [("SKILL.md", 7, "head_commit")]
    )
    assert seam_check.main([]) == 1


def test_live_descriptor_keys_all_emitted() -> None:
    """Every descriptor key cited in the live docs is emitted by dispatch_stage."""
    assert seam_check.descriptor_key_drift() == []


def test_live_skill_cites_the_do_loop_descriptor_keys() -> None:
    """The anchors fire on the real SKILL.md, not only synthetic input."""
    text = (seam_check.SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    cited = {k for _, k in seam_check.prose_descriptor_key_citations(text)}
    assert {"done", "blocked_by", "reason", "stage", "head_sha", "roles"} <= cited


# --- role-literal gate -------------------------------------------------------


def test_registry_roles_unions_arrays(tmp_path) -> None:
    registry = tmp_path / "stage-registry.toml"
    registry.write_text(
        '[[stage]]\nname = "a"\nroles = ["records_diff_baseline"]\n\n'
        '[[stage]]\nname = "b"\nroles = ["reflect_anchor", "ship_observer"]\n',
        encoding="utf-8",
    )
    assert seam_check.registry_roles(registry) == {
        "records_diff_baseline",
        "reflect_anchor",
        "ship_observer",
    }


def test_prose_role_citation_membership_idiom() -> None:
    text = 'if `descriptor.roles` includes `"records_diff_baseline"`:\n'
    assert seam_check.prose_role_citations(text) == [(1, "records_diff_baseline")]


def test_prose_role_citation_rejects_stage_name_as_role_membership() -> None:
    text = "If the stage is `agent_routed`, resolve the adapter route.\n"
    assert seam_check.prose_role_citations(text) == []


def test_prose_role_citation_ignores_non_membership_roles_mention() -> None:
    # A bare `roles` list-of-keys mention (no membership verb) yields nothing.
    text = "the descriptor with `stage`, `roles`, `reference_doc`.\n"
    assert seam_check.prose_role_citations(text) == []


def test_prose_role_citation_ignores_later_backticked_token() -> None:
    # a backticked lowercase token AFTER the role literal must not read as a role
    text = 'when `descriptor.roles` includes `"agent_routed"`, pass the route in the Agent call.\n'
    assert seam_check.prose_role_citations(text) == [(1, "agent_routed")]


def test_live_role_citations_recognized() -> None:
    # flow-rvrv: role_literal_drift()==[] passes on ZERO capture; assert the live
    # sentences are actually recognized, so the fix did not drop live coverage
    roles = {
        r
        for _, r in seam_check.prose_role_citations(
            (seam_check.SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        )
    }
    assert {"records_diff_baseline", "agent_routed"} <= roles


def test_role_literal_drift_flags_renamed_role(tmp_path) -> None:
    doc = tmp_path / "SKILL.md"
    doc.write_text('if `descriptor.roles` includes `"records_baseline"`:\n', encoding="utf-8")
    drift = seam_check.role_literal_drift(docs=[doc], roles={"records_diff_baseline"})
    assert ("SKILL.md", 1, "records_baseline") in drift


def test_role_literal_drift_clean(tmp_path) -> None:
    doc = tmp_path / "SKILL.md"
    doc.write_text('if `descriptor.roles` includes `"records_diff_baseline"`:\n', encoding="utf-8")
    assert seam_check.role_literal_drift(docs=[doc], roles={"records_diff_baseline"}) == []


def test_main_fails_on_role_literal_drift(monkeypatch) -> None:
    monkeypatch.setattr(seam_check, "scripts_missing_from_module_md", lambda *a, **k: set())
    monkeypatch.setattr(
        seam_check, "scripts_missing_from_registry_descriptions", lambda *a, **k: set()
    )
    monkeypatch.setattr(seam_check, "module_md_importer_drift", lambda *a, **k: [])
    monkeypatch.setattr(seam_check, "module_md_surface_cell_drift", lambda *a, **k: [])
    monkeypatch.setattr(seam_check, "docs_over_stage_doc_citation_limit", lambda *a, **k: {})
    monkeypatch.setattr(seam_check, "descriptor_key_drift", lambda *a, **k: [])
    monkeypatch.setattr(
        seam_check, "role_literal_drift", lambda *a, **k: [("SKILL.md", 116, "records_baseline")]
    )
    assert seam_check.main([]) == 1


def test_live_role_citations_all_in_registry() -> None:
    """Every role literal cited in the live docs exists in a registry roles array."""
    assert seam_check.role_literal_drift() == []
