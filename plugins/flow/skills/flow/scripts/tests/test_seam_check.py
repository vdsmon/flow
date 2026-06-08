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


def test_find_invocations_handles_bare_form() -> None:
    text = "   ${CLAUDE_SKILL_DIR}/scripts/tracker_cli.py --workspace-root . get --key X"
    invs = seam_check.find_invocations("t.md", text)
    assert len(invs) == 1
    assert invs[0].script == "tracker_cli.py"
    assert "--workspace-root" in invs[0].flags
    assert "--key" in invs[0].flags


def test_surface_of_real_script_has_subcommands_and_flags() -> None:
    surface = seam_check.surface_of("dispatch_stage.py")
    assert surface is not None
    assert {"init", "next", "advance", "finish", "release"} <= surface.subcommands
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


def test_slash_phantom_flag_is_error() -> None:
    # The headline regression: `/flow recover --reset-foo` names a flag recover.py
    # has nowhere on its surface, so it must surface as an ERROR.
    text = "Run `/flow recover --reset-foo` to wipe state."
    invs = seam_check.find_slash_invocations("t.md", text)
    assert len(invs) == 1
    assert invs[0].script == "recover.py"
    errors = [p for inv in invs for p in seam_check.validate(inv) if p.level == "ERROR"]
    assert len(errors) == 1
    assert "--reset-foo" in errors[0].msg


def test_slash_real_flags_pass() -> None:
    text = "Run `/flow init --reconfigure --resume` to redo setup."
    invs = seam_check.find_slash_invocations("t.md", text)
    assert len(invs) == 1
    assert [p for inv in invs for p in seam_check.validate(inv) if p.level == "ERROR"] == []


def test_slash_skips_verbs_without_script() -> None:
    # evolve and do have no scripts/<verb>.py, so their slash-prose is not linted.
    text = "Use `/flow evolve drain --include-proposals` or `/flow do --stage implement`."
    assert seam_check.find_slash_invocations("t.md", text) == []


def test_slash_subcommand_recover_passes() -> None:
    text = "Then `/flow recover reload-snapshot <ticket>` accepts the config."
    invs = seam_check.find_slash_invocations("t.md", text)
    assert len(invs) == 1
    problems = [p for inv in invs for p in seam_check.validate(inv)]
    assert [p for p in problems if p.level == "ERROR"] == []
    assert invs[0].subcommand == "reload-snapshot"


def test_slash_recall_metric_forwarder_passes() -> None:
    text = "`/flow recall --metric tickets-per-week` forwards to metric.py."
    invs = seam_check.find_slash_invocations("t.md", text)
    assert len(invs) == 1
    assert [p for inv in invs for p in seam_check.validate(inv) if p.level == "ERROR"] == []


def test_slash_adjacent_backtick_spans_do_not_merge() -> None:
    # Two inline spans on one line must stay separate: the bare recover span must
    # not absorb the `--stage` that belongs to the second `retry --stage ticket` span.
    text = "Once fixed, `/flow recover <KEY>` -> `retry --stage ticket`."
    invs = seam_check.find_slash_invocations("t.md", text)
    assert len(invs) == 1
    assert invs[0].script == "recover.py"
    assert invs[0].flags == []


def test_slash_phantom_flag_in_fenced_block_is_error() -> None:
    # The phantom-flag catch must also fire when the slash command sits on a
    # fenced-code line, not just an inline backtick span.
    text = "Example:\n\n```\n/flow recover --reset-foo\n```\n"
    invs = seam_check.find_slash_invocations("t.md", text)
    assert len(invs) == 1
    assert invs[0].script == "recover.py"
    errors = [p for inv in invs for p in seam_check.validate(inv) if p.level == "ERROR"]
    assert len(errors) == 1
    assert "--reset-foo" in errors[0].msg


def test_live_docs_are_green() -> None:
    """The real SKILL.md + references/ must have zero prose<->CLI seam errors."""
    assert seam_check.main([]) == 0


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
    """No live /flow doc statically re-enumerates the stage->reference_doc map."""
    assert seam_check.docs_over_stage_doc_citation_limit() == {}
