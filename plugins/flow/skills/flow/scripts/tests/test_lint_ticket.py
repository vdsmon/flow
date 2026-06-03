"""Tests for lint_ticket.py — HARD GATE per-stage required-field validator."""

from __future__ import annotations

from pathlib import Path

import pytest

import lint_ticket
import ticket_frontmatter

# ─── Helpers ─────────────────────────────────────────────────────────────────


def _write_registry(root: Path, stage_blocks: list[str]) -> Path:
    body = "schema_version = 1\n\n" + "\n".join(stage_blocks)
    p = root / "stage-registry.toml"
    p.write_text(body, encoding="utf-8")
    return p


def _stage(
    name: str,
    required_fields: list[str] | None = None,
    predecessors: list[str] | None = None,
) -> str:
    lines = [
        "[[stage]]",
        f'name = "{name}"',
        'description = "test stage"',
        'default_handler = "inline"',
        "default_timeout_min = 5",
        "default_heartbeat_required = false",
        "default_max_no_progress_min = 5",
        "required_capabilities = []",
        f"required_predecessors = {predecessors or []}",
    ]
    if required_fields is not None:
        lines.append(f"required_fields = {required_fields}")
    lines.extend(
        [
            "required = false",
            "required_when_compounding = false",
            f'reference_doc = "references/stage-{name}.md"',
            "roles = []",
        ]
    )
    return "\n".join(lines) + "\n"


def _write_ticket(path: Path, fm: dict[str, object]) -> None:
    ticket_frontmatter.update(path, {k: str(v) for k, v in fm.items()})


# ─── validate() ──────────────────────────────────────────────────────────────


def test_universal_fields_present_returns_no_violations(tmp_path: Path) -> None:
    reg = _write_registry(tmp_path, [_stage("ticket")])
    tp = tmp_path / "FT-1.md"
    _write_ticket(tp, {"ticket": "FT-1", "status": "in_progress"})
    assert lint_ticket.validate("ticket", tp, reg) == []


def test_missing_ticket_field_returns_violation(tmp_path: Path) -> None:
    reg = _write_registry(tmp_path, [_stage("ticket")])
    tp = tmp_path / "FT-1.md"
    _write_ticket(tp, {"status": "in_progress"})
    violations = lint_ticket.validate("ticket", tp, reg)
    assert any("ticket:" in v for v in violations)


def test_missing_status_field_returns_violation(tmp_path: Path) -> None:
    reg = _write_registry(tmp_path, [_stage("ticket")])
    tp = tmp_path / "FT-1.md"
    _write_ticket(tp, {"ticket": "FT-1"})
    violations = lint_ticket.validate("ticket", tp, reg)
    assert any("status:" in v for v in violations)


def test_empty_string_value_counts_as_violation(tmp_path: Path) -> None:
    reg = _write_registry(tmp_path, [_stage("ticket")])
    tp = tmp_path / "FT-1.md"
    _write_ticket(tp, {"ticket": "FT-1", "status": "null"})
    violations = lint_ticket.validate("ticket", tp, reg)
    assert any("status:" in v and "empty" in v for v in violations)


def test_implement_requires_planned_files(tmp_path: Path) -> None:
    reg = _write_registry(tmp_path, [_stage("implement", required_fields=["planned_files"])])
    tp = tmp_path / "FT-1.md"
    _write_ticket(tp, {"ticket": "FT-1", "status": "in_progress"})
    violations = lint_ticket.validate("implement", tp, reg)
    assert any("planned_files:" in v for v in violations)


def test_implement_with_planned_files_passes(tmp_path: Path) -> None:
    reg = _write_registry(tmp_path, [_stage("implement", required_fields=["planned_files"])])
    tp = tmp_path / "FT-1.md"
    _write_ticket(
        tp,
        {"ticket": "FT-1", "status": "in_progress", "planned_files": "[src/a.py, src/b.py]"},
    )
    assert lint_ticket.validate("implement", tp, reg) == []


def test_empty_list_required_field_is_violation(tmp_path: Path) -> None:
    reg = _write_registry(tmp_path, [_stage("implement", required_fields=["planned_files"])])
    tp = tmp_path / "FT-1.md"
    _write_ticket(tp, {"ticket": "FT-1", "status": "in_progress", "planned_files": "[]"})
    violations = lint_ticket.validate("implement", tp, reg)
    assert any("planned_files:" in v and "empty" in v for v in violations)


def test_commit_requires_commit_type_and_summary(tmp_path: Path) -> None:
    reg = _write_registry(
        tmp_path, [_stage("commit", required_fields=["commit_type", "commit_summary"])]
    )
    tp = tmp_path / "FT-1.md"
    _write_ticket(tp, {"ticket": "FT-1", "status": "in_progress"})
    violations = lint_ticket.validate("commit", tp, reg)
    assert any("commit_type:" in v for v in violations)
    assert any("commit_summary:" in v for v in violations)


def test_commit_with_type_and_summary_passes(tmp_path: Path) -> None:
    reg = _write_registry(
        tmp_path, [_stage("commit", required_fields=["commit_type", "commit_summary"])]
    )
    tp = tmp_path / "FT-1.md"
    _write_ticket(
        tp,
        {
            "ticket": "FT-1",
            "status": "in_progress",
            "commit_type": "feat",
            "commit_summary": "add thing",
        },
    )
    assert lint_ticket.validate("commit", tp, reg) == []


def test_create_pr_requires_pr_title(tmp_path: Path) -> None:
    reg = _write_registry(tmp_path, [_stage("create_pr", required_fields=["pr_title"])])
    tp = tmp_path / "FT-1.md"
    _write_ticket(tp, {"ticket": "FT-1", "status": "in_progress"})
    violations = lint_ticket.validate("create_pr", tp, reg)
    assert any("pr_title:" in v for v in violations)


def test_stage_with_no_required_fields_only_checks_universal(tmp_path: Path) -> None:
    reg = _write_registry(tmp_path, [_stage("plan")])
    tp = tmp_path / "FT-1.md"
    _write_ticket(tp, {"ticket": "FT-1", "status": "in_progress"})
    assert lint_ticket.validate("plan", tp, reg) == []


def test_multiple_violations_returned_together(tmp_path: Path) -> None:
    reg = _write_registry(tmp_path, [_stage("implement", required_fields=["planned_files"])])
    tp = tmp_path / "FT-1.md"
    _write_ticket(tp, {"ticket": "FT-1"})
    violations = lint_ticket.validate("implement", tp, reg)
    assert any("status:" in v for v in violations)
    assert any("planned_files:" in v for v in violations)


def test_unknown_stage_raises(tmp_path: Path) -> None:
    reg = _write_registry(tmp_path, [_stage("ticket")])
    tp = tmp_path / "FT-1.md"
    _write_ticket(tp, {"ticket": "FT-1", "status": "x"})
    with pytest.raises(ValueError, match="not in stage-registry"):
        lint_ticket.validate("nonexistent", tp, reg)


def test_missing_registry_raises(tmp_path: Path) -> None:
    tp = tmp_path / "FT-1.md"
    _write_ticket(tp, {"ticket": "FT-1", "status": "x"})
    with pytest.raises(FileNotFoundError, match="stage-registry"):
        lint_ticket.validate("ticket", tp, tmp_path / "missing.toml")


def test_missing_ticket_file_treated_as_empty_frontmatter(tmp_path: Path) -> None:
    reg = _write_registry(tmp_path, [_stage("ticket")])
    violations = lint_ticket.validate("ticket", tmp_path / "no-file.md", reg)
    assert any("ticket:" in v for v in violations)
    assert any("status:" in v for v in violations)


# ─── CLI ─────────────────────────────────────────────────────────────────────


def test_cli_violations_return_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    reg = _write_registry(tmp_path, [_stage("implement", required_fields=["planned_files"])])
    tp = tmp_path / "FT-1.md"
    _write_ticket(tp, {"ticket": "FT-1", "status": "in_progress"})
    rc = lint_ticket.cli_main(
        [
            "--stage",
            "implement",
            "--ticket-path",
            str(tp),
            "--workspace-root",
            str(reg.parent),
        ]
    )
    assert rc == 1
    captured = capsys.readouterr()
    assert "planned_files:" in captured.err


def test_cli_clean_returns_0(tmp_path: Path) -> None:
    reg = _write_registry(tmp_path, [_stage("ticket")])
    tp = tmp_path / "FT-1.md"
    _write_ticket(tp, {"ticket": "FT-1", "status": "in_progress"})
    rc = lint_ticket.cli_main(
        ["--stage", "ticket", "--ticket-path", str(tp), "--workspace-root", str(reg.parent)]
    )
    assert rc == 0


def test_cli_unknown_stage_returns_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    reg = _write_registry(tmp_path, [_stage("ticket")])
    tp = tmp_path / "FT-1.md"
    _write_ticket(tp, {"ticket": "FT-1", "status": "x"})
    rc = lint_ticket.cli_main(
        [
            "--stage",
            "nonexistent",
            "--ticket-path",
            str(tp),
            "--workspace-root",
            str(reg.parent),
        ]
    )
    assert rc == 1
    assert "not in stage-registry" in capsys.readouterr().err


def test_cli_default_registry_path_resolves(tmp_path: Path) -> None:
    """Without --workspace-root, uses plugin's stage-registry.toml. ticket stage
    has no required_fields beyond universal."""
    tp = tmp_path / "FT-1.md"
    _write_ticket(tp, {"ticket": "FT-1", "status": "in_progress"})
    rc = lint_ticket.cli_main(["--stage", "ticket", "--ticket-path", str(tp)])
    assert rc == 0


def test_real_registry_commit_fields_match_composer() -> None:
    """The shipped registry must require what compose_commit.py consumes
    (commit_type + commit_summary), not the unused commit_message field."""
    fields = lint_ticket._load_required_fields(lint_ticket._default_registry_path(), "commit")
    assert fields == ["commit_type", "commit_summary"]
