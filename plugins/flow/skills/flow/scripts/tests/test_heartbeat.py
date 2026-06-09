"""Contract tests for heartbeat.py.

Consumer-only inspection library: pure hung-detection logic + read_progress IO.
Every test injects now/wrote_at explicitly so nothing depends on real time. The
producer side (write_progress / quarantine_stale / identity_ok / the `write` CLI)
was deleted as dead (flow-dwd); .progress fixtures are built directly here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import heartbeat

RUN_ID = "run-1"
STAGE = "implement"
TICKET = "FT-100"


def _progress(
    *,
    run_id: str = RUN_ID,
    stage: str = STAGE,
    ticket: str = TICKET,
    seq: int = 1,
    current_op: str = "edit file.py",
    last_artifact: dict | None = None,
    wrote_at: str = "2026-05-28T12:00:30Z",
) -> heartbeat.Progress:
    return heartbeat.Progress(
        run_id=run_id,
        stage=stage,
        ticket=ticket,
        seq=seq,
        current_op=current_op,
        last_artifact=last_artifact,
        wrote_at=wrote_at,
    )


def _put(ticket_dir: Path, progress: heartbeat.Progress) -> None:
    heartbeat.progress_path(ticket_dir, progress.stage).write_text(
        heartbeat._serialize(progress), encoding="utf-8"
    )


# ─── read_progress ────────────────────────────────────────────────────────────


def test_read_round_trips_a_serialized_progress(tmp_path: Path) -> None:
    artifact = {"path": "out.txt", "size": 42, "mtime_ns": 123}
    written = _progress(seq=7, current_op="run tests", last_artifact=artifact)
    _put(tmp_path, written)
    assert heartbeat.progress_path(tmp_path, STAGE).exists()

    loaded = heartbeat.read_progress(tmp_path, STAGE)
    assert loaded == written
    assert loaded is not None
    assert loaded.seq == 7
    assert loaded.current_op == "run tests"
    assert loaded.last_artifact == artifact
    assert loaded.wrote_at == "2026-05-28T12:00:30Z"


def test_read_with_null_artifact(tmp_path: Path) -> None:
    written = _progress(last_artifact=None)
    _put(tmp_path, written)
    loaded = heartbeat.read_progress(tmp_path, STAGE)
    assert loaded == written
    assert loaded is not None
    assert loaded.last_artifact is None


def test_read_absent_returns_none(tmp_path: Path) -> None:
    assert heartbeat.read_progress(tmp_path, STAGE) is None


def test_read_malformed_returns_none(tmp_path: Path) -> None:
    heartbeat.progress_path(tmp_path, STAGE).write_text("{ not json", encoding="utf-8")
    assert heartbeat.read_progress(tmp_path, STAGE) is None


def test_read_structurally_wrong_returns_none(tmp_path: Path) -> None:
    # valid JSON but missing required keys -> None, not a crash.
    heartbeat.progress_path(tmp_path, STAGE).write_text('{"run_id": "x"}', encoding="utf-8")
    assert heartbeat.read_progress(tmp_path, STAGE) is None


# ─── dead producer is gone (flow-dwd) ─────────────────────────────────────────


def test_producer_symbols_absent() -> None:
    assert not hasattr(heartbeat, "write_progress")
    assert not hasattr(heartbeat, "quarantine_stale")
    assert not hasattr(heartbeat, "identity_ok")


def test_write_subcommand_rejected() -> None:
    with pytest.raises(SystemExit):
        heartbeat.cli_main(
            [
                "write",
                "--ticket-dir",
                ".",
                "--stage",
                STAGE,
                "--run-id",
                RUN_ID,
                "--ticket",
                TICKET,
                "--seq",
                "1",
                "--current-op",
                "x",
            ]
        )


# ─── detect_hung ──────────────────────────────────────────────────────────────


def test_detect_hung_on_old_wrote_at() -> None:
    # interval 60s -> hung threshold is 180s. wrote_at 12:00:00, now 12:05:00 (300s old).
    progress = _progress(wrote_at="2026-05-28T12:00:00Z")
    assert (
        heartbeat.detect_hung(progress, "2026-05-28T12:05:00Z", heartbeat_interval_s=60)
        == heartbeat.HUNG
    )


def test_detect_ok_when_fresh_no_prev() -> None:
    progress = _progress(wrote_at="2026-05-28T12:05:00Z")
    # 30s old, well under the 180s threshold, no prev to compare.
    assert heartbeat.detect_hung(progress, "2026-05-28T12:05:30Z") == heartbeat.OK


def test_detect_wedged_on_equal_seq() -> None:
    # both reads recent (not hung), seq did not advance -> wedged.
    prev = _progress(seq=5, wrote_at="2026-05-28T12:05:00Z")
    cur = _progress(seq=5, wrote_at="2026-05-28T12:05:40Z")
    assert heartbeat.detect_hung(cur, "2026-05-28T12:06:00Z", prev=prev) == heartbeat.WEDGED


def test_detect_no_progress_unchanged_artifact_op_past_window() -> None:
    # seq advanced (not wedged), but artifact + op frozen across an 11-min gap.
    # now is only 30s after cur.wrote_at so the hung check does not preempt.
    artifact = {"path": "out.txt", "size": 10, "mtime_ns": 1}
    prev = _progress(
        seq=5, current_op="compile", last_artifact=artifact, wrote_at="2026-05-28T12:00:00Z"
    )
    cur = _progress(
        seq=6, current_op="compile", last_artifact=artifact, wrote_at="2026-05-28T12:11:00Z"
    )
    assert (
        heartbeat.detect_hung(cur, "2026-05-28T12:11:30Z", prev=prev, max_no_progress_min=10)
        == heartbeat.NO_PROGRESS
    )


def test_detect_ok_when_artifact_advances() -> None:
    # seq advanced and the artifact changed -> genuine progress.
    prev = _progress(
        seq=5,
        current_op="compile",
        last_artifact={"path": "a", "size": 1, "mtime_ns": 1},
        wrote_at="2026-05-28T12:00:00Z",
    )
    cur = _progress(
        seq=6,
        current_op="compile",
        last_artifact={"path": "a", "size": 2, "mtime_ns": 2},
        wrote_at="2026-05-28T12:11:00Z",
    )
    assert heartbeat.detect_hung(cur, "2026-05-28T12:11:30Z", prev=prev) == heartbeat.OK


def test_detect_ok_when_gap_within_window() -> None:
    # frozen artifact + op but only 5-min gap, under the 10-min window.
    artifact = {"path": "out.txt", "size": 10, "mtime_ns": 1}
    prev = _progress(
        seq=5, current_op="compile", last_artifact=artifact, wrote_at="2026-05-28T12:00:00Z"
    )
    cur = _progress(
        seq=6, current_op="compile", last_artifact=artifact, wrote_at="2026-05-28T12:05:00Z"
    )
    assert heartbeat.detect_hung(cur, "2026-05-28T12:05:30Z", prev=prev) == heartbeat.OK


def test_hung_precedes_wedged() -> None:
    # equal seq would be wedged, but an old wrote_at makes it hung first.
    prev = _progress(seq=5, wrote_at="2026-05-28T12:00:00Z")
    cur = _progress(seq=5, wrote_at="2026-05-28T12:00:00Z")
    assert heartbeat.detect_hung(cur, "2026-05-28T12:05:00Z", prev=prev) == heartbeat.HUNG
