"""module_map: the generated derived-surfaces renderer/checker.

The live-tree green test plus the AST-vs-argparse equivalence pin are the load-bearing
pair: the first keeps the committed generated block honest, the second keeps AST
derivation legal (a dynamically named subparser must fail here, not drift silently).
"""

from __future__ import annotations

import module_map
import seam_check


def test_live_generated_blocks_are_green() -> None:
    assert module_map.check() == []


def test_true_importers_captures_lazy_in_function_import(tmp_path) -> None:
    (tmp_path / "a.py").write_text("")
    (tmp_path / "b.py").write_text("def f():\n    from a import X\n    return X\n")
    importers = module_map.true_importers(scripts_dir=tmp_path)
    assert importers.get("a") == {"b"}


def test_subcommands_from_ast(tmp_path) -> None:
    (tmp_path / "c.py").write_text(
        "import argparse\n"
        "p = argparse.ArgumentParser()\n"
        "sub = p.add_subparsers(dest='cmd')\n"
        "sub.add_parser('beta')\n"
        "sub.add_parser('alpha', help='x')\n"
    )
    assert module_map.subcommands(tmp_path) == {"c.py": ["alpha", "beta"]}


def test_ast_subcommands_match_argparse_help() -> None:
    """Ground-truth pin: for every script the AST credits with subparsers, the
    `--help`-derived surface must agree exactly. A future subparser whose name is
    not a string literal breaks this test the day it lands."""
    ast_subs = module_map.subcommands()
    assert ast_subs, "no subparser scripts found — the AST walk is broken"
    for name, subs in sorted(ast_subs.items()):
        surface = seam_check.surface_of(name)
        assert surface is not None, f"{name}: --help probe failed but AST found subparsers"
        assert surface.subcommands == frozenset(subs), (
            f"{name}: AST {sorted(subs)} != argparse {sorted(surface.subcommands)}"
        )


def test_dynamic_subparsers_have_no_silent_escape() -> None:
    """Reverse direction, targeted: a script that calls add_subparsers but yields
    zero AST-derived names is building names dynamically — fail it here."""
    ast_subs = module_map.subcommands()
    for path in module_map.SCRIPTS_DIR.glob("*.py"):
        if path.name.startswith("test") or path.name == "conftest.py":
            continue
        if "add_subparsers" in path.read_text(encoding="utf-8"):
            assert path.name in ast_subs, (
                f"{path.name}: add_subparsers present but no literal add_parser names"
            )


def test_render_module_block_lists_every_script() -> None:
    block = module_map.render_module_block()
    for path in module_map.SCRIPTS_DIR.glob("*.py"):
        if path.name.startswith("test") or path.name == "conftest.py":
            continue
        assert f"| `{path.name}` |" in block


def test_check_flags_stale_block_and_write_repairs_it(tmp_path, monkeypatch) -> None:
    # The docs-index block is embedded green so exactly one block (module-map) is stale.
    doc = tmp_path / "MODULE.md"
    doc.write_text(
        f"# map\n\n{module_map.MODULE_BEGIN}\n| stale |\n{module_map.MODULE_END}\n\n"
        f"{module_map.render_docs_index()}",
        encoding="utf-8",
    )
    monkeypatch.setattr(module_map, "MODULE_MD", doc)
    problems = module_map.check()
    assert len(problems) == 1
    assert "module_map.py write" in problems[0]
    assert module_map.write() == [doc]
    assert module_map.check() == []
    assert module_map.write() == []


def test_check_reports_missing_markers(tmp_path, monkeypatch) -> None:
    doc = tmp_path / "MODULE.md"
    doc.write_text("# map with no markers\n", encoding="utf-8")
    monkeypatch.setattr(module_map, "MODULE_MD", doc)
    problems = module_map.check()
    # Both MODULE.md-hosted blocks (module-map and docs-index) report their pair.
    assert len(problems) == 2
    assert all("markers not found" in p for p in problems)


def test_docs_index_round_trip(tmp_path, monkeypatch) -> None:
    refs = tmp_path / "references"
    refs.mkdir()
    (refs / "alpha.md").write_text("# Alpha\n\nOwns the alpha protocol.\n", encoding="utf-8")
    doc = tmp_path / "MODULE.md"
    doc.write_text(
        f"# map\n\n{module_map.render_module_block()}\n"
        f"{module_map.DOCS_INDEX_BEGIN}\n| stale |\n{module_map.DOCS_INDEX_END}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(module_map, "MODULE_MD", doc)
    monkeypatch.setattr(module_map, "REFERENCES_DIR", refs)
    problems = module_map.check()
    assert len(problems) == 1
    assert "module_map.py write" in problems[0]
    assert module_map.write() == [doc]
    assert "| `references/alpha.md` | Owns the alpha protocol. |" in doc.read_text(encoding="utf-8")
    assert module_map.check() == []
    assert module_map.write() == []


def test_render_docs_index_lists_every_reference_doc() -> None:
    block = module_map.render_docs_index()
    for path in module_map.REFERENCES_DIR.glob("*.md"):
        assert f"| `references/{path.name}` |" in block


def test_doc_one_liner_purpose_branch(tmp_path) -> None:
    doc = tmp_path / "stage-x.md"
    doc.write_text(
        "# Stage: x\n\n## Purpose\n\nDo the x thing. Ignore this second sentence.\n",
        encoding="utf-8",
    )
    assert module_map.doc_one_liner(doc) == "Do the x thing."


def test_doc_one_liner_joins_wrapped_lines(tmp_path) -> None:
    doc = tmp_path / "d.md"
    doc.write_text(
        "# D\n\nThe first sentence wraps across\ntwo hard-wrapped lines. Second sentence.\n",
        encoding="utf-8",
    )
    assert (
        module_map.doc_one_liner(doc) == "The first sentence wraps across two hard-wrapped lines."
    )


def test_doc_one_liner_strips_backticks_and_trailing_colon(tmp_path) -> None:
    doc = tmp_path / "d.md"
    doc.write_text("# D\n\nFlow has three `nested` loops:\n\n- one\n", encoding="utf-8")
    assert module_map.doc_one_liner(doc) == "Flow has three nested loops"


def test_doc_one_liner_truncates_at_word_boundary(tmp_path) -> None:
    doc = tmp_path / "d.md"
    doc.write_text("# D\n\n" + "word " * 60 + "end.\n", encoding="utf-8")
    cell = module_map.doc_one_liner(doc)
    assert len(cell) <= 141
    assert cell.endswith("…")
    assert not cell.removesuffix("…").endswith(" ")


def test_triage_guard_files_parsed_from_source() -> None:
    members = module_map.triage_guard_files()
    assert "lease.py" in members
    assert "dispatch_stage.py" in members
    assert "SKILL.md" in members


def test_render_guard_span_lists_py_members_only() -> None:
    span = module_map.render_guard_span()
    assert span.startswith(module_map.GUARD_BEGIN)
    assert span.endswith(module_map.GUARD_END)
    assert "`lease.py`" in span
    # Non-.py members (SKILL.md, AGENTS.md, ...) are enumerated separately by
    # the surrounding authored sentence, never by the generated span.
    assert "SKILL.md" not in span
    assert "\n" not in span
