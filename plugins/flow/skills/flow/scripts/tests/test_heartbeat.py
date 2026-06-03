"""Contract tests for heartbeat.py.

Pure detection logic + round-trip IO. Every test injects now/wrote_at explicitly
so nothing depends on real time. Real temp dirs exercise the atomic write and the
quarantine rename.
"""

from __future__ import annotations

from pathlib import Path

import heartbeat

RUN_ID = "run-1"
STAGE = "implement"
TICKET = "FT-100"

# stage start at 12:00; a fresh heartbeat lands at/after this.
STARTED = "2026-05-28T12:00:00Z"


def _write(
    ticket_dir: Path,
    *,
    run_id: str = RUN_ID,
    stage: str = STAGE,
    ticket: str = TICKET,
    seq: int = 1,
    current_op: str = "edit file.py",
    last_artifact: dict | None = None,
    now: str = "2026-05-28T12:00:30Z",
) -> heartbeat.Progress:
    return heartbeat.write_progress(
        ticket_dir,
        run_id=run_id,
        stage=stage,
        ticket=ticket,
        seq=seq,
        current_op=current_op,
        last_artifact=last_artifact,
        now_iso=now,
    )


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


# ─── write / read round-trip ──────────────────────────────────────────────────


def test_write_then_read_round_trips(tmp_path: Path) -> None:
    artifact = {"path": "out.txt", "size": 42, "mtime_ns": 123}
    written = _write(tmp_path, seq=7, current_op="run tests", last_artifact=artifact)
    assert heartbeat.progress_path(tmp_path, STAGE).exists()

    loaded = heartbeat.read_progress(tmp_path, STAGE)
    assert loaded == written
    assert loaded is not None
    assert loaded.seq == 7
    assert loaded.current_op == "run tests"
    assert loaded.last_artifact == artifact
    assert loaded.wrote_at == "2026-05-28T12:00:30Z"


def test_write_then_read_with_null_artifact(tmp_path: Path) -> None:
    written = _write(tmp_path, last_artifact=None)
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


# ─── identity_ok ──────────────────────────────────────────────────────────────


def _identity_ok(progress: heartbeat.Progress) -> bool:
    return heartbeat.identity_ok(
        progress,
        run_id=RUN_ID,
        stage=STAGE,
        ticket=TICKET,
        stage_started_at_iso=STARTED,
    )


def test_identity_ok_true_when_all_match_and_not_older(tmp_path: Path) -> None:
    assert _identity_ok(_progress(wrote_at="2026-05-28T12:00:00Z")) is True
    assert _identity_ok(_progress(wrote_at="2026-05-28T12:05:00Z")) is True


def test_identity_false_on_run_id_mismatch() -> None:
    assert _identity_ok(_progress(run_id="other")) is False


def test_identity_false_on_stage_mismatch() -> None:
    assert _identity_ok(_progress(stage="plan")) is False


def test_identity_false_on_ticket_mismatch() -> None:
    assert _identity_ok(_progress(ticket="FT-999")) is False


def test_identity_false_when_wrote_at_older_than_start() -> None:
    assert _identity_ok(_progress(wrote_at="2026-05-28T11:59:59Z")) is False


# ─── quarantine_stale ─────────────────────────────────────────────────────────


def test_quarantine_moves_mismatched_file(tmp_path: Path) -> None:
    # wrong run_id -> identity fails -> file is moved aside.
    _write(tmp_path, run_id="foreign", now="2026-05-28T12:00:30Z")
    moved = heartbeat.quarantine_stale(
        tmp_path,
        STAGE,
        run_id=RUN_ID,
        ticket=TICKET,
        stage_started_at_iso=STARTED,
    )
    assert moved is True
    assert not heartbeat.progress_path(tmp_path, STAGE).exists()
    stale = list(tmp_path.glob(f"{STAGE}.progress.stale.*"))
    assert len(stale) == 1
    assert stale[0].name == f"{STAGE}.progress.stale.0"


def test_quarantine_leaves_matching_file(tmp_path: Path) -> None:
    _write(tmp_path, now="2026-05-28T12:00:30Z")
    moved = heartbeat.quarantine_stale(
        tmp_path,
        STAGE,
        run_id=RUN_ID,
        ticket=TICKET,
        stage_started_at_iso=STARTED,
    )
    assert moved is False
    assert heartbeat.progress_path(tmp_path, STAGE).exists()
    assert list(tmp_path.glob(f"{STAGE}.progress.stale.*")) == []


def test_quarantine_absent_returns_false(tmp_path: Path) -> None:
    assert (
        heartbeat.quarantine_stale(
            tmp_path,
            STAGE,
            run_id=RUN_ID,
            ticket=TICKET,
            stage_started_at_iso=STARTED,
        )
        is False
    )


def test_quarantine_picks_next_free_index(tmp_path: Path) -> None:
    base = heartbeat.progress_path(tmp_path, STAGE)
    base.with_name(f"{base.name}.stale.0").write_text("old", encoding="utf-8")
    _write(tmp_path, run_id="foreign")
    moved = heartbeat.quarantine_stale(
        tmp_path,
        STAGE,
        run_id=RUN_ID,
        ticket=TICKET,
        stage_started_at_iso=STARTED,
    )
    assert moved is True
    assert base.with_name(f"{base.name}.stale.1").exists()


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
