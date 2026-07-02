"""Tests for scrub_ci_skip.py: CI-skip token neutralizer."""

from __future__ import annotations

import pytest

import scrub_ci_skip

FORMS = ["skip ci", "ci skip", "no ci", "skip actions", "actions skip"]


@pytest.mark.parametrize("inner", FORMS)
def test_each_form_neutralized(inner: str) -> None:
    out, n = scrub_ci_skip.scrub(f"fix: thing\n\nbody [{inner}] more")
    assert n == 1
    assert f"[{inner}]" not in out
    assert inner in out


@pytest.mark.parametrize("raw", ["[Skip CI]", "[SKIP CI]", "[Ci Skip]"])
def test_case_insensitive_match_preserves_inner_case(raw: str) -> None:
    out, n = scrub_ci_skip.scrub(f"body {raw} tail")
    assert n == 1
    inner = raw[1:-1]
    assert inner in out
    assert raw not in out


def test_mid_prose_strip_leaves_surrounding_text() -> None:
    out, n = scrub_ci_skip.scrub("before [skip ci] after")
    assert n == 1
    assert out == "before skip ci after"


def test_multiple_occurrences_all_neutralized() -> None:
    out, n = scrub_ci_skip.scrub("[skip ci] and [ci skip] and [no ci]")
    assert n == 3
    assert "[" not in out
    assert out == "skip ci and ci skip and no ci"


def test_token_in_header_neutralized() -> None:
    out, n = scrub_ci_skip.scrub("fix: thing [skip ci]\n\nbody")
    assert n == 1
    assert out == "fix: thing skip ci\n\nbody"


@pytest.mark.parametrize(
    "text",
    [
        "skip ci",
        "[skipping ci]",
        "[skip continuous integration]",
        "[skip-ci]",
        "[skip  ci]",
    ],
)
def test_near_misses_untouched(text: str) -> None:
    out, n = scrub_ci_skip.scrub(text)
    assert n == 0
    assert out == text


def test_idempotent() -> None:
    once, _ = scrub_ci_skip.scrub("body [skip ci] tail")
    twice, n = scrub_ci_skip.scrub(once)
    assert n == 0
    assert twice == once


def test_clean_input_unchanged() -> None:
    text = "fix: a real commit\n\nwhy this matters"
    out, n = scrub_ci_skip.scrub(text)
    assert n == 0
    assert out == text


# ─── skip-checks trailer ─────────────────────────────────────────────────────


@pytest.mark.parametrize("raw", ["skip-checks:true", "skip-checks: true", "Skip-Checks: True"])
def test_skip_checks_trailer_neutralized(raw: str) -> None:
    out, n = scrub_ci_skip.scrub(f"fix: thing\n\nbody\n\n\n{raw}\n")
    assert n == 1
    assert raw not in out
    assert "skip-checks:" not in out.lower()
    assert "skip-checks " in out.lower()


def test_skip_checks_mid_sentence_untouched() -> None:
    text = "the docs mention skip-checks: true as a trailer form\n"
    out, n = scrub_ci_skip.scrub(text)
    assert n == 0
    assert out == text


@pytest.mark.parametrize(
    "text",
    [
        "skip-checks: false\n",
        "  skip-checks: true\n",
        "skip-checks true\n",
    ],
)
def test_skip_checks_near_misses_untouched(text: str) -> None:
    out, n = scrub_ci_skip.scrub(text)
    assert n == 0
    assert out == text


def test_skip_checks_idempotent() -> None:
    once, _ = scrub_ci_skip.scrub("fix: x\n\n\nskip-checks: true\n")
    twice, n = scrub_ci_skip.scrub(once)
    assert n == 0
    assert twice == once


def test_bracketed_and_trailer_both_counted() -> None:
    out, n = scrub_ci_skip.scrub("fix: x [skip ci]\n\nbody\n\n\nskip-checks:true\n")
    assert n == 2
    assert "[skip ci]" not in out
    assert "skip-checks:true" not in out


# ─── CLI ─────────────────────────────────────────────────────────────────────


def test_cli_rewrites_in_place(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    p = tmp_path / "msg.txt"
    p.write_text("fix: x\n\nbody [skip ci] tail\n")
    rc = scrub_ci_skip.cli_main([str(p)])
    assert rc == 0
    assert p.read_text() == "fix: x\n\nbody skip ci tail\n"
    err = capsys.readouterr().err
    assert "neutralized 1" in err


def test_cli_clean_file_byte_identical(tmp_path) -> None:
    p = tmp_path / "msg.txt"
    original = "fix: x\n\nclean body\n"
    p.write_text(original)
    before = p.read_bytes()
    rc = scrub_ci_skip.cli_main([str(p)])
    assert rc == 0
    assert p.read_bytes() == before


def test_cli_missing_path_returns_1(capsys: pytest.CaptureFixture[str]) -> None:
    rc = scrub_ci_skip.cli_main(["/nonexistent/path/to/nothing.txt"])
    assert rc == 1
    assert capsys.readouterr().err
