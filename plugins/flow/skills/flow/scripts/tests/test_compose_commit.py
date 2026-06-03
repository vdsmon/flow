"""Tests for compose_commit.py — conventional commit message emitter."""

from __future__ import annotations

import pytest

import compose_commit


@pytest.mark.parametrize("type_", list(compose_commit.VALID_TYPES))
def test_each_valid_type_produces_header(type_: str) -> None:
    out = compose_commit.compose(ticket="FT-1", type_=type_, summary="add thing", scope="auth")
    assert out.startswith(f"{type_}(auth): add thing\n")


def test_invalid_type_raises() -> None:
    with pytest.raises(ValueError, match="invalid commit type"):
        compose_commit.compose(ticket="FT-1", type_="nope", summary="x")


def test_scope_optional() -> None:
    out = compose_commit.compose(ticket="FT-1", type_="feat", summary="thing")
    assert out.startswith("feat: thing\n")


def test_includes_ticket() -> None:
    out = compose_commit.compose(ticket="FT-77", type_="fix", summary="x")
    assert "ticket: FT-77" in out


def test_files_list_rendered() -> None:
    out = compose_commit.compose(
        ticket="FT-1", type_="feat", summary="x", files=["src/a.py", "src/b.py"]
    )
    assert "files:" in out
    assert "  - src/a.py" in out
    assert "  - src/b.py" in out


def test_no_files_section_when_empty() -> None:
    out = compose_commit.compose(ticket="FT-1", type_="feat", summary="x")
    assert "files:" not in out


def test_body_template_present() -> None:
    out = compose_commit.compose(ticket="FT-1", type_="feat", summary="x")
    assert "# body" in out


def test_empty_summary_raises() -> None:
    with pytest.raises(ValueError, match="summary must be non-empty"):
        compose_commit.compose(ticket="FT-1", type_="feat", summary="   ")


def test_empty_ticket_raises() -> None:
    with pytest.raises(ValueError, match="ticket must be non-empty"):
        compose_commit.compose(ticket="", type_="feat", summary="x")


# ─── CLI ─────────────────────────────────────────────────────────────────────


def test_cli_happy_path(capsys: pytest.CaptureFixture[str]) -> None:
    rc = compose_commit.cli_main(
        [
            "--ticket",
            "FT-1",
            "--type",
            "feat",
            "--summary",
            "add auth cooldown",
            "--scope",
            "auth",
            "--files",
            "src/a.py,src/b.py",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert out.startswith("feat(auth): add auth cooldown\n")
    assert "  - src/a.py" in out


def test_cli_invalid_type_returns_nonzero(capsys: pytest.CaptureFixture[str]) -> None:
    rc = compose_commit.cli_main(["--ticket", "FT-1", "--type", "garbage", "--summary", "x"])
    assert rc != 0
