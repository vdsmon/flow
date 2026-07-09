"""lint_comments.py: the deterministic comment-quality floor under the stage-implement bar."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import lint_comments
from lint_comments import Finding, cli_main, discover_line_length, lint_file

EM = "—"


def _lint(tmp_path: Path, name: str, content: str, limit: int = 100) -> list[Finding]:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return lint_file(path, line_length=limit)


def _categories(findings: list[Finding]) -> set[str]:
    return {f.category for f in findings}


def test_em_dash_in_comment_flagged(tmp_path):
    findings = _lint(tmp_path, "a.py", f"# a claim {EM} with a dash\nx = 1\n")
    assert _categories(findings) == {"em-dash"}
    assert findings[0].line == 1


def test_em_dash_in_docstring_flagged(tmp_path):
    findings = _lint(tmp_path, "a.py", f'"""Summary {EM} with a dash."""\n')
    assert _categories(findings) == {"em-dash"}


@pytest.mark.parametrize(
    "word",
    ["simply", "note that", "leverages", "robustly", "seamlessly", "comprehensive", "powerful"],
)
def test_banned_words_flagged(tmp_path, word):
    findings = _lint(tmp_path, "a.py", f"# this {word} retries the write\nx = 1\n")
    assert _categories(findings) == {"banned-word"}


@pytest.mark.parametrize(
    "text",
    [
        "# a just-merged squash lands here",
        "# the block ending just before the first marker",
    ],
)
def test_just_compound_and_temporal_pass(tmp_path, text):
    assert _lint(tmp_path, "a.py", text + "\nx = 1\n") == []


def test_just_filler_flagged(tmp_path):
    findings = _lint(tmp_path, "a.py", "# it will just retry the write here\nx = 1\n")
    assert _categories(findings) == {"banned-word"}
    assert findings[0].message == '"just" as filler'


def test_narration_flagged(tmp_path):
    findings = _lint(tmp_path, "a.py", "# here we monkeypatch the tracker call\nx = 1\n")
    assert _categories(findings) == {"narration"}


def test_trailing_comment_wording_checked(tmp_path):
    findings = _lint(tmp_path, "a.py", f"x = 1  # trailing claim {EM} with a dash\n")
    assert _categories(findings) == {"em-dash"}


def test_underfill_flagged_once_per_block(tmp_path):
    block = (
        "# the lease holder writes the state file under the flock and every\n"
        "# reader retries on contention until the holder releases it, with the\n"
        "# quarantine path taken on a corrupt read so the run never dies there\n"
        "x = 1\n"
    )
    findings = _lint(tmp_path, "a.py", block)
    assert [f.category for f in findings] == ["under-fill"]
    assert findings[0].line == 1


def test_sentence_final_breaks_pass(tmp_path):
    block = (
        "# the lease holder writes the state file under the configured flock.\n"
        "# every reader retries on contention until the holder releases it.\n"
        "x = 1\n"
    )
    assert _lint(tmp_path, "a.py", block) == []


def test_list_block_passes(tmp_path):
    block = (
        "# - first: the lease holder writes the state file entry\n"
        "# - second: every reader retries on contention until release\n"
        "x = 1\n"
    )
    assert _lint(tmp_path, "a.py", block) == []


def test_filled_block_passes(tmp_path):
    first = "# " + "word " * 18 + "tail"
    assert len(first) > 90
    block = first + "\n# continuation prose line that is long enough to count\nx = 1\n"
    assert _lint(tmp_path, "a.py", block) == []


def test_long_line_flagged(tmp_path):
    long_comment = "# " + "x" * 120
    findings = _lint(tmp_path, "a.py", long_comment + "\n")
    assert _categories(findings) == {"long-line"}


def test_string_literal_not_flagged(tmp_path):
    assert _lint(tmp_path, "a.py", f'msg = "simply {EM} here we go"\n') == []


def test_pragma_lines_skipped(tmp_path):
    content = (
        "#!/usr/bin/env python3\n"
        "# -*- coding: utf-8 -*-\n"
        "x = 1  # noqa: E501\n"
        "y = 2  # type: ignore[assignment]\n"
    )
    assert _lint(tmp_path, "a.py", content) == []


def test_generic_line_comment_flagged(tmp_path):
    findings = _lint(tmp_path, "a.ts", "// this simply wraps the call\nconst x = 1\n")
    assert _categories(findings) == {"banned-word"}


def test_generic_marker_inside_string_not_flagged(tmp_path):
    assert _lint(tmp_path, "a.ts", 'const s = "// simply a string"\n') == []


def test_unknown_extension_skipped(tmp_path):
    assert _lint(tmp_path, "a.xyz", f"weird {EM} content, simply ignored\n") == []


def test_discovery_reads_pyproject_ruff(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\nline-length = 60\n", encoding="utf-8")
    target = tmp_path / "pkg" / "a.py"
    target.parent.mkdir()
    target.write_text("x = 1\n", encoding="utf-8")
    assert discover_line_length(target) == 60


def test_discovery_defaults_when_nothing_declares(tmp_path):
    (tmp_path / ".git").mkdir()
    target = tmp_path / "a.py"
    target.write_text("x = 1\n", encoding="utf-8")
    assert discover_line_length(target) == lint_comments._DEFAULT_LIMIT


def test_cli_clean_exit_zero(tmp_path, capsys):
    path = tmp_path / "a.py"
    path.write_text("x = 1\n", encoding="utf-8")
    assert cli_main([str(path), "--line-length", "100"]) == 0
    assert capsys.readouterr().out == ""


def test_cli_findings_exit_one_and_json(tmp_path, capsys):
    path = tmp_path / "a.py"
    path.write_text(f"# a claim {EM} with a dash\nx = 1\n", encoding="utf-8")
    assert cli_main([str(path), "--line-length", "100", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["category"] == "em-dash"
    assert payload[0]["line"] == 1


def test_cli_missing_file_skipped(tmp_path, capsys):
    ok = tmp_path / "a.py"
    ok.write_text("x = 1\n", encoding="utf-8")
    missing = tmp_path / "gone.py"
    assert cli_main([str(ok), str(missing), "--line-length", "100"]) == 0
    assert "skipped" in capsys.readouterr().err


def test_docstrings_of_class_function_and_async_linted(tmp_path):
    content = (
        f'class C:\n    """Class doc {EM} dash."""\n\n'
        f'def f():\n    """Function doc {EM} dash."""\n\n'
        f'async def g():\n    """Async doc {EM} dash."""\n'
    )
    findings = _lint(tmp_path, "a.py", content)
    assert [f.category for f in findings] == ["em-dash"] * 3
    assert [f.line for f in findings] == [2, 5, 8]


def test_multiline_docstring_continuation_lines_linted(tmp_path):
    content = f'def f():\n    """Summary line.\n\n    Body claim {EM} with a dash.\n    """\n'
    findings = _lint(tmp_path, "a.py", content)
    assert _categories(findings) == {"em-dash"}
    assert findings[0].line == 4


def test_docstring_sharing_line_with_code_not_scanned(tmp_path):
    content = f'def f(sep="{EM}"): "join parts with sep"\n\nclass Simply: "marker for retries"\n'
    assert _lint(tmp_path, "a.py", content) == []


def test_docstring_trailing_comment_not_double_counted(tmp_path):
    content = f'"""Short doc."""  # trailing {EM} dash\n'
    findings = _lint(tmp_path, "a.py", content)
    assert [f.category for f in findings] == ["em-dash"]


def test_underfill_short_orphan_continuation_flagged(tmp_path):
    block = (
        "# the lease holder writes the state file under the flock and every reader\n"
        "# retries\n"
        "x = 1\n"
    )
    findings = _lint(tmp_path, "a.py", block, limit=88)
    assert [f.category for f in findings] == ["under-fill"]


def test_underfill_skips_field_list_rows(tmp_path):
    block = (
        "# Args:\n"
        "#   ticket: the key resolved from the branch and used for the lease\n"
        "#   workspace_root: where the workspace.toml and state live for the run\n"
        "x = 1\n"
    )
    assert _lint(tmp_path, "a.py", block) == []


def test_underfill_skips_fenced_lines(tmp_path):
    block = (
        "# run the drain loop by hand when the janitor parks a worktree entry\n"
        "# ```\n"
        "# python3 evolve_drain.py --workspace-root . --cap 2 --concurrency 2\n"
        "# ```\n"
        "x = 1\n"
    )
    assert _lint(tmp_path, "a.py", block) == []


def test_underfill_skips_star_bullets_and_numbered_rows(tmp_path):
    block = (
        "# * first the lease holder writes the state entry under the flock\n"
        "# * then every reader retries on contention until the flock releases\n"
        "# 1. numbered rows are structured text and never merge upward here\n"
        "x = 1\n"
    )
    assert _lint(tmp_path, "a.py", block) == []


def test_underfill_colon_ended_intro_line_passes(tmp_path):
    block = (
        "# the drain loop needs these two knobs before the first launch turn:\n"
        "# cap bounds the batch and concurrency bounds the in-flight run count\n"
        "x = 1\n"
    )
    assert _lint(tmp_path, "a.py", block) == []


def test_python_blocks_split_on_column_change(tmp_path):
    content = (
        "def f():\n"
        "    # inner comment about the retry path and its flock contention story\n"
        "# outer comment at module level continuing a totally unrelated thought\n"
        "    pass\n"
    )
    assert _lint(tmp_path, "a.py", content) == []


def test_pragma_prefix_needs_boundary(tmp_path):
    findings = _lint(tmp_path, "a.py", f"# pragmatic compromise {EM} retried here\nx = 1\n")
    assert _categories(findings) == {"em-dash"}
    findings = _lint(tmp_path, "a.py", f"# coding style {EM} follows the guide\nx = 1\n")
    assert _categories(findings) == {"em-dash"}


def test_generic_long_line_and_block(tmp_path):
    content = "// " + "x" * 120 + "\nconst a = 1\n"
    findings = _lint(tmp_path, "a.ts", content)
    assert _categories(findings) == {"long-line"}
    block = (
        "// the lease holder writes the state file under the flock and every\n"
        "// reader retries on contention until the flock holder releases it\n"
        "const b = 1\n"
    )
    assert _categories(_lint(tmp_path, "b.ts", block, limit=100)) == {"under-fill"}


def test_generic_blocks_split_on_indent_change(tmp_path):
    content = (
        "// outer comment about the retry path and its flock contention story\n"
        "    // indented comment continuing a totally unrelated inner thought\n"
        "const a = 1\n"
    )
    assert _lint(tmp_path, "a.ts", content) == []


def test_discovery_reads_ruff_toml_black_and_editorconfig(tmp_path):
    ruff_dir = tmp_path / "r"
    ruff_dir.mkdir()
    (ruff_dir / "ruff.toml").write_text("line-length = 70\n", encoding="utf-8")
    (ruff_dir / "a.py").write_text("x = 1\n", encoding="utf-8")
    assert discover_line_length(ruff_dir / "a.py") == 70

    black_dir = tmp_path / "b"
    black_dir.mkdir()
    (black_dir / "pyproject.toml").write_text("[tool.black]\nline-length = 79\n", encoding="utf-8")
    (black_dir / "a.py").write_text("x = 1\n", encoding="utf-8")
    assert discover_line_length(black_dir / "a.py") == 79

    ec_dir = tmp_path / "e"
    ec_dir.mkdir()
    (ec_dir / ".editorconfig").write_text("[*]\nmax_line_length = 120\n", encoding="utf-8")
    (ec_dir / "a.py").write_text("x = 1\n", encoding="utf-8")
    assert discover_line_length(ec_dir / "a.py") == 120


def test_discovery_stops_at_git_file_worktree_layout(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\nline-length = 60\n", encoding="utf-8")
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
    target = wt / "a.py"
    target.write_text("x = 1\n", encoding="utf-8")
    assert discover_line_length(target) == lint_comments._DEFAULT_LIMIT


def test_discovery_survives_undecodable_config(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "pyproject.toml").write_bytes(b"[tool.ruff]\nline-length = 100 # caf\xe9\n")
    target = tmp_path / "a.py"
    target.write_text(f"# a claim {EM} with a dash\nx = 1\n", encoding="utf-8")
    findings = lint_file(target)
    assert _categories(findings) == {"em-dash"}


def test_cli_line_length_flag_applies(tmp_path, capsys):
    path = tmp_path / "a.py"
    path.write_text("# " + "x" * 70 + "\nx = 1\n", encoding="utf-8")
    assert cli_main([str(path), "--line-length", "100"]) == 0
    assert cli_main([str(path), "--line-length", "40"]) == 1
    assert "long-line" in capsys.readouterr().out


def test_cli_broken_python_does_not_crash(tmp_path):
    path = tmp_path / "a.py"
    path.write_text("def broken(:\n", encoding="utf-8")
    assert cli_main([str(path), "--line-length", "100"]) == 0


def test_diff_base_scopes_to_changed_lines(tmp_path):
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    path = tmp_path / "a.py"
    path.write_text("# old claim, simply retried forever\nx = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "seed"],
        cwd=tmp_path,
        check=True,
    )
    path.write_text(
        f"# old claim, simply retried forever\nx = 1\n# new claim {EM} with a dash\ny = 2\n",
        encoding="utf-8",
    )
    unscoped = cli_main([str(path), "--line-length", "100"])
    assert unscoped == 1
    findings = lint_file(path, line_length=100)
    assert _categories(findings) == {"banned-word", "em-dash"}
    changed = lint_comments._changed_lines(path, "HEAD")
    assert changed is not None
    assert changed == {3, 4}
    scoped = [f for f in findings if f.line in changed]
    assert _categories(scoped) == {"em-dash"}


def test_dogfood_engine_and_test_file_clean():
    scripts_dir = Path(lint_comments.__file__).resolve().parent
    for target in (scripts_dir / "lint_comments.py", Path(__file__).resolve()):
        assert lint_file(target, line_length=100) == [], str(target)
