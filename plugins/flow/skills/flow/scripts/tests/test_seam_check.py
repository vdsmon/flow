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


def test_live_docs_are_green() -> None:
    """The real SKILL.md + references/ must have zero prose<->CLI seam errors."""
    assert seam_check.main([]) == 0


def test_module_md_covers_all_live_scripts() -> None:
    """Every non-test script on disk must be named in the real MODULE.md."""
    assert seam_check.scripts_missing_from_module_md() == set()


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
