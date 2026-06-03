"""Contract tests for ticket_frontmatter.py — TOML frontmatter r/w."""

from __future__ import annotations

import datetime
import json
import multiprocessing
import re
from pathlib import Path

import pytest

import ticket_frontmatter

# ─── _split_frontmatter ──────────────────────────────────────────────────────


def test_split_simple() -> None:
    text = '+++\nticket = "FT-1"\n+++\n\nbody here\n'
    fm, body = ticket_frontmatter._split_frontmatter(text)
    assert fm == 'ticket = "FT-1"\n'
    assert body == "body here\n"


def test_split_no_frontmatter() -> None:
    text = "no delimiter\n"
    fm, body = ticket_frontmatter._split_frontmatter(text)
    assert fm is None
    assert body == text


def test_split_unterminated() -> None:
    text = '+++\nticket = "FT-1"\nbody but no close delim\n'
    fm, body = ticket_frontmatter._split_frontmatter(text)
    assert fm is None
    assert body == text


def test_split_empty_body() -> None:
    text = '+++\nticket = "FT-1"\n+++\n'
    fm, body = ticket_frontmatter._split_frontmatter(text)
    assert fm == 'ticket = "FT-1"\n'
    assert body == ""


# ─── read() ──────────────────────────────────────────────────────────────────


def test_read_happy(tmp_path: Path) -> None:
    p = tmp_path / "FT-1.md"
    p.write_text(
        '+++\nticket = "FT-1"\nstatus = "in_progress"\nlabels = ["a", "b"]\n+++\n\n# body\n',
        encoding="utf-8",
    )
    data = ticket_frontmatter.read(p)
    assert data == {"ticket": "FT-1", "status": "in_progress", "labels": ["a", "b"]}


def test_read_missing_file_returns_empty(tmp_path: Path) -> None:
    p = tmp_path / "missing.md"
    assert ticket_frontmatter.read(p) == {}


def test_read_no_frontmatter_returns_empty(tmp_path: Path) -> None:
    p = tmp_path / "no_fm.md"
    p.write_text("just markdown\n", encoding="utf-8")
    assert ticket_frontmatter.read(p) == {}


def test_read_malformed_quarantines(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    p = tmp_path / "bad.md"
    p.write_text("+++\nthis is not = valid = toml\n+++\nbody\n", encoding="utf-8")
    data = ticket_frontmatter.read(p)
    assert data == {}
    assert not p.exists()
    quarantined = list(tmp_path.glob("bad.md.quarantine.*"))
    assert len(quarantined) == 1
    captured = capsys.readouterr()
    assert "quarantined" in captured.err


# ─── update() ────────────────────────────────────────────────────────────────


def test_update_creates_file_if_missing(tmp_path: Path) -> None:
    p = tmp_path / "new.md"
    ticket_frontmatter.update(p, {"ticket": "FT-2", "status": "pending"})
    data = ticket_frontmatter.read(p)
    assert data == {"ticket": "FT-2", "status": "pending"}


def test_update_preserves_body(tmp_path: Path) -> None:
    p = tmp_path / "FT-3.md"
    p.write_text(
        '+++\nticket = "FT-3"\n+++\n\n# original body\nwith multiple lines\n', encoding="utf-8"
    )
    ticket_frontmatter.update(p, {"status": "in_progress"})
    text = p.read_text(encoding="utf-8")
    assert "# original body" in text
    assert "with multiple lines" in text


def test_update_appends_new_keys_after_existing(tmp_path: Path) -> None:
    p = tmp_path / "FT-4.md"
    p.write_text('+++\nticket = "FT-4"\nstatus = "pending"\n+++\nbody\n', encoding="utf-8")
    ticket_frontmatter.update(p, {"agent_id": "abc"})
    text = p.read_text(encoding="utf-8")
    ticket_idx = text.index("ticket")
    status_idx = text.index("status")
    agent_idx = text.index("agent_id")
    assert ticket_idx < status_idx < agent_idx


def test_update_overwrites_existing_key(tmp_path: Path) -> None:
    p = tmp_path / "FT-5.md"
    p.write_text('+++\nticket = "FT-5"\nstatus = "pending"\n+++\n', encoding="utf-8")
    ticket_frontmatter.update(p, {"status": "in_progress"})
    data = ticket_frontmatter.read(p)
    assert data["status"] == "in_progress"


def test_update_null_substitution(tmp_path: Path) -> None:
    p = tmp_path / "FT-6.md"
    ticket_frontmatter.update(p, {"agent_id": "null"})
    data = ticket_frontmatter.read(p)
    assert data == {"agent_id": ""}


def test_update_now_substitution(tmp_path: Path) -> None:
    p = tmp_path / "FT-7.md"
    ticket_frontmatter.update(p, {"started_at": "NOW"})
    data = ticket_frontmatter.read(p)
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", data["started_at"])


def test_update_bool_substitution(tmp_path: Path) -> None:
    p = tmp_path / "FT-8.md"
    ticket_frontmatter.update(p, {"draft": "true", "merged": "false"})
    data = ticket_frontmatter.read(p)
    assert data == {"draft": True, "merged": False}


def test_update_int_substitution(tmp_path: Path) -> None:
    p = tmp_path / "FT-9.md"
    ticket_frontmatter.update(p, {"version": "42", "neg": "-7"})
    data = ticket_frontmatter.read(p)
    assert data == {"version": 42, "neg": -7}


def test_update_list_substitution(tmp_path: Path) -> None:
    p = tmp_path / "FT-10.md"
    ticket_frontmatter.update(p, {"labels": "[a, b, c]"})
    data = ticket_frontmatter.read(p)
    assert data == {"labels": ["a", "b", "c"]}


def test_update_empty_list(tmp_path: Path) -> None:
    p = tmp_path / "FT-11.md"
    ticket_frontmatter.update(p, {"labels": "[]"})
    data = ticket_frontmatter.read(p)
    assert data == {"labels": []}


def test_update_list_with_quoted_commas(tmp_path: Path) -> None:
    p = tmp_path / "FT-23.md"
    ticket_frontmatter.update(p, {"labels": '["a,b", "c"]'})
    data = ticket_frontmatter.read(p)
    # naive comma-split would tear "a,b" apart and keep the quote chars; the
    # tomllib-backed parse yields exactly two elements with the comma intact.
    assert data == {"labels": ["a,b", "c"]}


def test_update_quotes_strings_with_special_chars(tmp_path: Path) -> None:
    p = tmp_path / "FT-12.md"
    ticket_frontmatter.update(p, {"summary": 'has "quotes" inside'})
    data = ticket_frontmatter.read(p)
    assert data == {"summary": 'has "quotes" inside'}


# ─── [O] key quoting on emit ─────────────────────────────────────────────────


def test_update_key_with_space_round_trips_flat(tmp_path: Path) -> None:
    p = tmp_path / "FT-20.md"
    ticket_frontmatter.update(p, {"a key": "v"})
    # without quoting, the emitted bare key `a key = ...` is invalid TOML and
    # read() would quarantine + return {}, losing ALL frontmatter.
    assert not list(tmp_path.glob("FT-20.md.quarantine.*"))
    data = ticket_frontmatter.read(p)
    assert data == {"a key": "v"}


def test_update_dotted_key_stays_flat(tmp_path: Path) -> None:
    p = tmp_path / "FT-21.md"
    ticket_frontmatter.update(p, {"a.b": "v"})
    data = ticket_frontmatter.read(p)
    # quoted so it round-trips as a flat key, not nested {"a": {"b": ...}}.
    assert data == {"a.b": "v"}


# ─── [P] native datetime preserved across unrelated update ───────────────────


def test_update_preserves_native_datetime(tmp_path: Path) -> None:
    p = tmp_path / "FT-22.md"
    p.write_text("+++\nstarted_at = 2026-05-28T14:32:00Z\n+++\n", encoding="utf-8")
    first = ticket_frontmatter.read(p)
    assert isinstance(first["started_at"], datetime.datetime)
    ticket_frontmatter.update(p, {"status": "done"})
    again = ticket_frontmatter.read(p)
    # an unrelated --set must not rewrite the datetime as a quoted string.
    assert isinstance(again["started_at"], datetime.datetime)
    assert again["started_at"] == first["started_at"]


def test_update_malformed_existing_raises(tmp_path: Path) -> None:
    p = tmp_path / "FT-13.md"
    p.write_text("+++\nbad = = toml\n+++\n", encoding="utf-8")
    with pytest.raises(ticket_frontmatter._SchemaInvalid, match="does not parse"):
        ticket_frontmatter.update(p, {"status": "x"})


def test_update_file_without_frontmatter_raises(tmp_path: Path) -> None:
    p = tmp_path / "FT-14.md"
    p.write_text("just markdown content\n", encoding="utf-8")
    with pytest.raises(ticket_frontmatter._SchemaInvalid, match="no frontmatter block"):
        ticket_frontmatter.update(p, {"status": "x"})


# ─── Concurrency: multiprocessing flock contention ───────────────────────────


def _updater_proc(path_str: str, key: str, value: str) -> None:
    ticket_frontmatter.update(Path(path_str), {key: value})


def test_concurrent_updates_serialize_via_flock(tmp_path: Path) -> None:
    p = tmp_path / "FT-15.md"
    ticket_frontmatter.update(p, {"ticket": "FT-15"})
    ctx = multiprocessing.get_context("spawn")
    p1 = ctx.Process(target=_updater_proc, args=(str(p), "alpha", "1"))
    p2 = ctx.Process(target=_updater_proc, args=(str(p), "beta", "2"))
    p1.start()
    p2.start()
    p1.join(timeout=10)
    p2.join(timeout=10)
    assert p1.exitcode == 0
    assert p2.exitcode == 0
    data = ticket_frontmatter.read(p)
    assert data["alpha"] == 1
    assert data["beta"] == 2
    assert data["ticket"] == "FT-15"


# ─── CLI ─────────────────────────────────────────────────────────────────────


def test_cli_read_emits_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    p = tmp_path / "FT-16.md"
    p.write_text('+++\nticket = "FT-16"\nstatus = "pending"\n+++\n', encoding="utf-8")
    rc = ticket_frontmatter.cli_main(["read", str(p)])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"ticket": "FT-16", "status": "pending"}


def test_cli_update_persists(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    p = tmp_path / "FT-17.md"
    rc = ticket_frontmatter.cli_main(
        ["update", str(p), "--set", "ticket=FT-17", "--set", "status=in_progress"]
    )
    assert rc == 0
    data = ticket_frontmatter.read(p)
    assert data == {"ticket": "FT-17", "status": "in_progress"}


def test_cli_update_malformed_returns_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    p = tmp_path / "FT-18.md"
    p.write_text("+++\nbad = = toml\n+++\n", encoding="utf-8")
    rc = ticket_frontmatter.cli_main(["update", str(p), "--set", "status=in_progress"])
    assert rc == 2
    assert "does not parse" in capsys.readouterr().err


def test_cli_update_set_without_eq_returns_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    p = tmp_path / "FT-19.md"
    rc = ticket_frontmatter.cli_main(["update", str(p), "--set", "noeq"])
    assert rc == 2
    assert "missing '='" in capsys.readouterr().err
