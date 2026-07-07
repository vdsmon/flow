"""Tests for the /flow SessionStart hook (staleness deadman only).

The hook file is hyphenated (`session-start.py`), not an importable module name,
so it is loaded via importlib from its path. Recall moved to the plan phase; the
hook is now staleness-only.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

HOOK_PATH = Path(__file__).resolve().parent.parent / "session-start.py"


def _load_hook() -> ModuleType:
    spec = importlib.util.spec_from_file_location("flow_session_start", HOOK_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hook = _load_hook()


# ─── evolve-loop staleness (deadman) ───────────────────────────────────────────


def _now() -> datetime:
    return datetime(2026, 6, 11, 12, 0, 0, tzinfo=UTC)


def _write_record(path: Path, *rows: dict[str, Any]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _ts(now: datetime, **delta: float) -> str:
    return (now - timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_staleness_absent_file_is_silent(tmp_path: Path) -> None:
    assert hook.staleness_block(tmp_path / "missing.jsonl", _now()) == ""


def test_staleness_fresh_runs_are_silent(tmp_path: Path) -> None:
    now = _now()
    rec = tmp_path / "run-record.jsonl"
    _write_record(
        rec,
        {"schedule": "nightly", "phase": "end", "ts": _ts(now, hours=10), "outcome": "ok"},
        {"schedule": "weekly", "phase": "end", "ts": _ts(now, days=3), "outcome": "ok"},
    )
    assert hook.staleness_block(rec, now) == ""


def test_staleness_nightly_stale_warns(tmp_path: Path) -> None:
    now = _now()
    rec = tmp_path / "run-record.jsonl"
    _write_record(
        rec, {"schedule": "nightly", "phase": "end", "ts": _ts(now, hours=40), "outcome": "ok"}
    )
    block = hook.staleness_block(rec, now)
    assert block.startswith("## /flow ops")
    assert "nightly evolve loop stale" in block
    assert ">36h" in block


def test_staleness_weekly_stale_warns(tmp_path: Path) -> None:
    now = _now()
    rec = tmp_path / "run-record.jsonl"
    _write_record(
        rec, {"schedule": "weekly", "phase": "end", "ts": _ts(now, days=9), "outcome": "ok"}
    )
    block = hook.staleness_block(rec, now)
    assert "weekly epic loop stale" in block
    assert ">8d" in block


def test_staleness_uses_latest_record_per_schedule(tmp_path: Path) -> None:
    """A fresh end record after an old start record clears the warning."""
    now = _now()
    rec = tmp_path / "run-record.jsonl"
    _write_record(
        rec,
        {"schedule": "nightly", "phase": "start", "ts": _ts(now, hours=50), "outcome": ""},
        {"schedule": "nightly", "phase": "end", "ts": _ts(now, hours=2), "outcome": "ok"},
    )
    assert hook.staleness_block(rec, now) == ""


def test_staleness_tolerates_garbage_lines(tmp_path: Path) -> None:
    now = _now()
    rec = tmp_path / "run-record.jsonl"
    rec.write_text(
        "not json\n"
        + json.dumps({"schedule": "nightly", "ts": "garbage-ts"})
        + "\n"
        + json.dumps({"schedule": "nightly", "phase": "end", "ts": _ts(now, hours=40)})
        + "\n",
        encoding="utf-8",
    )
    block = hook.staleness_block(rec, now)
    assert "nightly evolve loop stale" in block


def test_staleness_fail_outcome_warns(tmp_path: Path) -> None:
    """A latest `end` with outcome=fail (trap-EXIT crash-capture) fires a warning."""
    now = _now()
    rec = tmp_path / "run-record.jsonl"
    _write_record(
        rec,
        {"schedule": "nightly", "phase": "start", "ts": _ts(now, hours=2), "outcome": ""},
        {"schedule": "nightly", "phase": "end", "ts": _ts(now, hours=1), "outcome": "fail"},
    )
    block = hook.staleness_block(rec, now)
    assert block.startswith("## /flow ops")
    assert "nightly evolve" in block
    assert "fail" in block


def test_staleness_hung_start_no_end_warns(tmp_path: Path) -> None:
    """A start with no end past the nightly 3h grace reads as hung."""
    now = _now()
    rec = tmp_path / "run-record.jsonl"
    _write_record(
        rec, {"schedule": "nightly", "phase": "start", "ts": _ts(now, hours=4), "outcome": ""}
    )
    block = hook.staleness_block(rec, now)
    assert block.startswith("## /flow ops")
    assert "nightly evolve" in block
    assert "hung" in block


def test_staleness_hung_within_grace_is_silent(tmp_path: Path) -> None:
    """A start within the nightly 3h grace is an in-flight run, not a warning."""
    now = _now()
    rec = tmp_path / "run-record.jsonl"
    _write_record(
        rec, {"schedule": "nightly", "phase": "start", "ts": _ts(now, hours=1), "outcome": ""}
    )
    assert hook.staleness_block(rec, now) == ""


def test_staleness_hung_discriminates_from_pr266_dead_branch(tmp_path: Path) -> None:
    """A new pending start AFTER a prior completed run reads hung, not stale.

    This is the harvest's improvement over the closed PR #266: its hung branch
    keyed on `last_end is None`, so an accumulating record with any prior `end`
    never fired hung and would mis-report the old `end` as stale. The fix keys on
    `last_start > last_end`, so a fresh hung start is caught even with old ends present.
    """
    now = _now()
    rec = tmp_path / "run-record.jsonl"
    _write_record(
        rec,
        {"schedule": "nightly", "phase": "start", "ts": _ts(now, hours=50), "outcome": ""},
        {"schedule": "nightly", "phase": "end", "ts": _ts(now, hours=49), "outcome": "ok"},
        {"schedule": "nightly", "phase": "start", "ts": _ts(now, hours=4), "outcome": ""},
    )
    block = hook.staleness_block(rec, now)
    assert "hung" in block
    assert "stale" not in block


def test_staleness_weekly_hung_grace_is_separate(tmp_path: Path) -> None:
    """Weekly uses a 6h zombie grace, distinct from nightly's 3h."""
    now = _now()
    rec = tmp_path / "run-record.jsonl"
    _write_record(
        rec, {"schedule": "weekly", "phase": "start", "ts": _ts(now, hours=7), "outcome": ""}
    )
    block = hook.staleness_block(rec, now)
    assert "weekly epic" in block
    assert "hung" in block


def test_staleness_weekly_hung_within_grace_is_silent(tmp_path: Path) -> None:
    now = _now()
    rec = tmp_path / "run-record.jsonl"
    _write_record(
        rec, {"schedule": "weekly", "phase": "start", "ts": _ts(now, hours=5), "outcome": ""}
    )
    assert hook.staleness_block(rec, now) == ""


def test_staleness_disarmed_suppresses_stale(tmp_path: Path) -> None:
    now = _now()
    rec = tmp_path / "run-record.jsonl"
    _write_record(
        rec, {"schedule": "nightly", "phase": "end", "ts": _ts(now, hours=40), "outcome": "ok"}
    )
    (tmp_path / "disarmed-nightly").touch()
    block = hook.staleness_block(rec, now)
    assert "nightly evolve loop disarmed" in block
    assert "⚠️" not in block


def test_staleness_disarmed_suppresses_hung(tmp_path: Path) -> None:
    now = _now()
    rec = tmp_path / "run-record.jsonl"
    _write_record(
        rec,
        {"schedule": "nightly", "phase": "start", "ts": _ts(now, hours=5), "outcome": ""},
    )
    (tmp_path / "disarmed-nightly").touch()
    block = hook.staleness_block(rec, now)
    assert "nightly evolve loop disarmed" in block
    assert "⚠️" not in block


def test_staleness_disarmed_suppresses_fail(tmp_path: Path) -> None:
    now = _now()
    rec = tmp_path / "run-record.jsonl"
    _write_record(
        rec, {"schedule": "nightly", "phase": "end", "ts": _ts(now, hours=5), "outcome": "fail"}
    )
    (tmp_path / "disarmed-nightly").touch()
    block = hook.staleness_block(rec, now)
    assert "nightly evolve loop disarmed" in block
    assert "⚠️" not in block


def test_staleness_disarmed_per_schedule_independent(tmp_path: Path) -> None:
    now = _now()
    rec = tmp_path / "run-record.jsonl"
    _write_record(
        rec,
        {"schedule": "nightly", "phase": "end", "ts": _ts(now, hours=40), "outcome": "ok"},
        {"schedule": "weekly", "phase": "end", "ts": _ts(now, days=9), "outcome": "ok"},
    )
    (tmp_path / "disarmed-nightly").touch()
    block = hook.staleness_block(rec, now)
    assert "nightly evolve loop disarmed" in block
    assert "weekly epic loop stale" in block
    assert "nightly evolve loop stale" not in block


def test_staleness_disarmed_no_record_silent(tmp_path: Path) -> None:
    now = _now()
    rec = tmp_path / "run-record.jsonl"
    rec.write_text("", encoding="utf-8")
    (tmp_path / "disarmed-nightly").touch()
    block = hook.staleness_block(rec, now)
    assert "nightly evolve loop disarmed" in block


# ─── cli_main (staleness-only) ─────────────────────────────────────────────────


def test_cli_main_silent_with_no_record(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    # Inject a nonexistent run-record so the assertion is deterministic on an
    # armed maintainer machine (without injection cli_main reads the real
    # ~/.flow-evolve/run-record.jsonl and would flake).
    missing = tmp_path / "missing.jsonl"
    assert hook.cli_main(run_record_path=missing) == 0
    assert capsys.readouterr().out == ""


def test_cli_main_renders_staleness(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """The machine-level evolve deadman renders from any cwd.

    cli_main calls the real _now(), so the record's ts is a fixed far-past date
    that exceeds the nightly staleness threshold against actual wall-clock time.
    """
    stale_record = tmp_path / "run-record.jsonl"
    _write_record(
        stale_record,
        {"schedule": "nightly", "phase": "end", "ts": "2020-01-01T00:00:00Z", "outcome": "ok"},
    )

    assert hook.cli_main(run_record_path=stale_record) == 0
    out = capsys.readouterr().out
    assert "## /flow ops" in out
    assert "nightly evolve loop stale" in out


def test_cli_main_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a, **_k):
        raise RuntimeError("staleness blew up")

    monkeypatch.setattr(hook, "staleness_block", boom)
    assert hook.cli_main() == 0
