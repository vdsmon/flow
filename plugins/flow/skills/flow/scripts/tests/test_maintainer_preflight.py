"""Tests for the host-neutral maintainer preflight deadman."""

from __future__ import annotations

import json
import socket
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import lease
import maintainer_preflight as preflight

# ─── evolve-loop staleness (deadman) ───────────────────────────────────────────


def _now() -> datetime:
    return datetime(2026, 6, 11, 12, 0, 0, tzinfo=UTC)


def _write_record(path: Path, *rows: dict[str, Any]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "flow@example.invalid")
    _git(repo, "config", "user.name", "Flow Test")
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "base")
    return repo


def _ts(now: datetime, **delta: float) -> str:
    return (now - timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_staleness_absent_file_is_silent(tmp_path: Path) -> None:
    path = tmp_path / "missing.jsonl"
    assert preflight.render_preflight(preflight.evaluate_run_records(path, _now())) == ""


def test_staleness_unreadable_ledger_is_unavailable_not_healthy(tmp_path: Path) -> None:
    report = preflight.evaluate_run_records(tmp_path, _now())

    assert report.configured is True
    assert report.attention_required is True
    assert report.issues[0].state == "unavailable"


def test_staleness_fresh_runs_are_silent(tmp_path: Path) -> None:
    now = _now()
    rec = tmp_path / "run-record.jsonl"
    _write_record(
        rec,
        {"schedule": "nightly", "phase": "end", "ts": _ts(now, hours=10), "outcome": "ok"},
        {"schedule": "weekly", "phase": "end", "ts": _ts(now, days=3), "outcome": "ok"},
    )
    assert preflight.render_preflight(preflight.evaluate_run_records(rec, now)) == ""


def test_staleness_nightly_stale_warns(tmp_path: Path) -> None:
    now = _now()
    rec = tmp_path / "run-record.jsonl"
    _write_record(
        rec, {"schedule": "nightly", "phase": "end", "ts": _ts(now, hours=40), "outcome": "ok"}
    )
    block = preflight.render_preflight(preflight.evaluate_run_records(rec, now))
    assert block.startswith("Flow maintainer preflight")
    assert "nightly evolve loop stale" in block
    assert ">36h" in block


def test_staleness_weekly_stale_warns(tmp_path: Path) -> None:
    now = _now()
    rec = tmp_path / "run-record.jsonl"
    _write_record(
        rec, {"schedule": "weekly", "phase": "end", "ts": _ts(now, days=9), "outcome": "ok"}
    )
    block = preflight.render_preflight(preflight.evaluate_run_records(rec, now))
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
    assert preflight.render_preflight(preflight.evaluate_run_records(rec, now)) == ""


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
    block = preflight.render_preflight(preflight.evaluate_run_records(rec, now))
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
    block = preflight.render_preflight(preflight.evaluate_run_records(rec, now))
    assert block.startswith("Flow maintainer preflight")
    assert "nightly evolve" in block
    assert "fail" in block


def test_staleness_hung_start_no_end_warns(tmp_path: Path) -> None:
    """A start with no end past the nightly 3h grace reads as hung."""
    now = _now()
    rec = tmp_path / "run-record.jsonl"
    _write_record(
        rec, {"schedule": "nightly", "phase": "start", "ts": _ts(now, hours=4), "outcome": ""}
    )
    block = preflight.render_preflight(preflight.evaluate_run_records(rec, now))
    assert block.startswith("Flow maintainer preflight")
    assert "nightly evolve" in block
    assert "hung" in block


def test_staleness_hung_within_grace_is_silent(tmp_path: Path) -> None:
    """A start within the nightly 3h grace is an in-flight run, not a warning."""
    now = _now()
    rec = tmp_path / "run-record.jsonl"
    _write_record(
        rec, {"schedule": "nightly", "phase": "start", "ts": _ts(now, hours=1), "outcome": ""}
    )
    assert preflight.render_preflight(preflight.evaluate_run_records(rec, now)) == ""


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
    block = preflight.render_preflight(preflight.evaluate_run_records(rec, now))
    assert "hung" in block
    assert "stale" not in block


def test_staleness_weekly_hung_grace_is_separate(tmp_path: Path) -> None:
    """Weekly uses a 6h zombie grace, distinct from nightly's 3h."""
    now = _now()
    rec = tmp_path / "run-record.jsonl"
    _write_record(
        rec, {"schedule": "weekly", "phase": "start", "ts": _ts(now, hours=7), "outcome": ""}
    )
    block = preflight.render_preflight(preflight.evaluate_run_records(rec, now))
    assert "weekly epic" in block
    assert "hung" in block


def test_staleness_weekly_hung_within_grace_is_silent(tmp_path: Path) -> None:
    now = _now()
    rec = tmp_path / "run-record.jsonl"
    _write_record(
        rec, {"schedule": "weekly", "phase": "start", "ts": _ts(now, hours=5), "outcome": ""}
    )
    assert preflight.render_preflight(preflight.evaluate_run_records(rec, now)) == ""


def test_staleness_disarmed_suppresses_stale(tmp_path: Path) -> None:
    now = _now()
    rec = tmp_path / "run-record.jsonl"
    _write_record(
        rec, {"schedule": "nightly", "phase": "end", "ts": _ts(now, hours=40), "outcome": "ok"}
    )
    (tmp_path / "disarmed-nightly").touch()
    block = preflight.render_preflight(preflight.evaluate_run_records(rec, now))
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
    block = preflight.render_preflight(preflight.evaluate_run_records(rec, now))
    assert "nightly evolve loop disarmed" in block
    assert "⚠️" not in block


def test_staleness_disarmed_suppresses_fail(tmp_path: Path) -> None:
    now = _now()
    rec = tmp_path / "run-record.jsonl"
    _write_record(
        rec, {"schedule": "nightly", "phase": "end", "ts": _ts(now, hours=5), "outcome": "fail"}
    )
    (tmp_path / "disarmed-nightly").touch()
    block = preflight.render_preflight(preflight.evaluate_run_records(rec, now))
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
    block = preflight.render_preflight(preflight.evaluate_run_records(rec, now))
    assert "nightly evolve loop disarmed" in block
    assert "weekly epic loop stale" in block
    assert "nightly evolve loop stale" not in block


def test_staleness_disarmed_no_record_silent(tmp_path: Path) -> None:
    now = _now()
    rec = tmp_path / "run-record.jsonl"
    rec.write_text("", encoding="utf-8")
    (tmp_path / "disarmed-nightly").touch()
    block = preflight.render_preflight(preflight.evaluate_run_records(rec, now))
    assert "nightly evolve loop disarmed" in block


# ─── cli_main ─────────────────────────────────────────────────────────────────


def test_cli_main_silent_with_no_record(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    # Inject a nonexistent run-record so the assertion is deterministic on an
    # armed maintainer machine (without injection cli_main reads the real
    # ~/.flow-evolve/run-record.jsonl and would flake).
    missing = tmp_path / "missing.jsonl"
    assert preflight.cli_main(["--run-record", str(missing)]) == 0
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

    assert preflight.cli_main(["--run-record", str(stale_record)]) == 0
    out = capsys.readouterr().out
    assert "Flow maintainer preflight" in out
    assert "nightly evolve loop stale" in out


def test_cli_main_json_is_structured_for_cockpit(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    stale_record = tmp_path / "run-record.jsonl"
    _write_record(
        stale_record,
        {"schedule": "nightly", "phase": "end", "ts": "2020-01-01T00:00:00Z", "outcome": "ok"},
    )

    assert (
        preflight.cli_main(
            [
                "--run-record",
                str(stale_record),
                "--now",
                "2026-06-11T12:00:00Z",
                "--json",
            ]
        )
        == 0
    )
    data = json.loads(capsys.readouterr().out)
    assert data["configured"] is True
    assert data["attention_required"] is True
    assert data["issues"][0]["state"] == "stale"


def test_absent_record_json_is_unconfigured(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    missing = tmp_path / "missing.jsonl"
    assert preflight.cli_main(["--run-record", str(missing), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data == {
        "attention_required": False,
        "configured": False,
        "issues": [],
        "record_path": str(missing),
    }


def test_maintenance_boundary_accepts_clean_repo_without_live_leases(tmp_path: Path) -> None:
    repo = _repo(tmp_path)

    report = preflight.evaluate_maintenance_boundary(repo, _now())

    assert report.clear is True
    assert report.checkout_clean is True
    assert report.live_leases == ()


def test_maintenance_boundary_rejects_dirt_and_live_revision_lease(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    revision = repo / ".flow" / "runs" / "FT-1" / "revisions" / "rev-1"
    lease.acquire(
        revision,
        "run-1",
        3600,
        _now().isoformat(),
        current_boot=lease.boot_id(),
        hostname=socket.gethostname(),
        cwd=str(repo),
    )
    (repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")

    report = preflight.evaluate_maintenance_boundary(repo, _now())

    assert report.clear is False
    assert report.checkout_clean is False
    assert report.live_leases == ("FT-1/revisions/rev-1",)


def test_cli_requires_clean_boundary_before_mutation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(tmp_path)
    (repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    missing_record = tmp_path / "missing.jsonl"

    assert (
        preflight.cli_main(
            [
                "--run-record",
                str(missing_record),
                "--workspace-root",
                str(repo),
                "--require-clean-boundary",
                "--json",
            ]
        )
        == 3
    )
    assert json.loads(capsys.readouterr().out)["boundary"]["clear"] is False
