"""Tests for optional native-agent model hints."""

from __future__ import annotations

from pathlib import Path

import pytest

import model_resolve


def _workspace(tmp_path: Path, model_lines: list[str] | None = None) -> Path:
    flow = tmp_path / ".flow"
    flow.mkdir()
    lines = [
        "[tracker]",
        'backend = "beads"',
        "[tracker.beads]",
        'prefix = "test"',
    ]
    if model_lines is not None:
        lines.extend(["[models]", *model_lines])
    (flow / "workspace.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return tmp_path


def test_missing_models_inherits_owner_session(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    assert model_resolve.resolve_stage_model(root, "implement") == ""


def test_stage_hint_is_returned_verbatim(tmp_path: Path) -> None:
    root = _workspace(tmp_path, ['implement = "opus"', 'e2e = "sonnet"'])
    assert model_resolve.resolve_stage_model(root, "implement") == "opus"
    assert model_resolve.resolve_stage_model(root, "e2e") == "sonnet"
    assert model_resolve.resolve_stage_model(root, "reflect") == ""


@pytest.mark.parametrize("value", ["off", "none", "false", ""])
def test_disabled_hint_inherits(tmp_path: Path, value: str) -> None:
    root = _workspace(tmp_path, [f'implement = "{value}"'])
    assert model_resolve.resolve_stage_model(root, "implement") == ""


def test_missing_or_malformed_workspace_fails_open(tmp_path: Path) -> None:
    assert model_resolve.resolve_stage_model(tmp_path, "implement") == ""
    root = _workspace(tmp_path, ["implement = 3"])
    assert model_resolve.resolve_stage_model(root, "implement") == ""


def test_cli_prints_hint(tmp_path: Path, capsys) -> None:
    root = _workspace(tmp_path, ['code_review = "opus"'])
    rc = model_resolve.cli_main(["--workspace-root", str(root), "--stage", "code_review"])
    assert rc == 0
    assert capsys.readouterr().out == "opus\n"


def test_cli_prints_nothing_when_inheriting(tmp_path: Path, capsys) -> None:
    root = _workspace(tmp_path)
    rc = model_resolve.cli_main(["--workspace-root", str(root), "--stage", "implement"])
    assert rc == 0
    assert capsys.readouterr().out == ""
