"""Contract tests for state.py.

Covers atomic write semantics, flock contention (via multiprocessing, not
threads — GIL hides POSIX flock from threading), quarantine load-from-bak,
all-bak-corrupt exit 2, rolling backup trim to 5, schema valid/invalid,
lifecycle transitions, and pick_next/find_failed helpers.
"""

from __future__ import annotations

import json
import multiprocessing
from pathlib import Path

import pytest

import state

# ─── Helpers ─────────────────────────────────────────────────────────────────


def _seed(tmp_path: Path) -> state.TicketState:
    return state.init(
        tmp_path,
        ticket="FT-1234",
        backend="jira",
        stages=["ticket", "plan", "implement", "commit", "reflect"],
        run_id="0123456789abcdef",
    )


# ─── init / read happy path ──────────────────────────────────────────────────


def test_init_writes_state_with_pending_stages(tmp_path: Path) -> None:
    ts = _seed(tmp_path)
    assert ts.ticket == "FT-1234"
    assert ts.run_id == "0123456789abcdef"
    assert ts.backend == "jira"
    assert len(ts.stages) == 5
    for record in ts.stages.values():
        assert record.status == "pending"
        assert record.started_at_iso is None


def test_read_returns_state_after_init(tmp_path: Path) -> None:
    _seed(tmp_path)
    loaded, exit_code = state.read(tmp_path)
    assert exit_code == 0
    assert loaded is not None
    assert loaded.ticket == "FT-1234"


def test_read_absent_returns_none_exit_zero(tmp_path: Path) -> None:
    loaded, exit_code = state.read(tmp_path)
    assert loaded is None
    assert exit_code == 0


def test_init_auto_generates_run_id(tmp_path: Path) -> None:
    ts = state.init(tmp_path, "FT-9", "beads", ["ticket"])
    assert len(ts.run_id) == 16


# ─── Lifecycle ───────────────────────────────────────────────────────────────


def test_begin_stage_transitions_pending_to_in_progress(tmp_path: Path) -> None:
    _seed(tmp_path)
    ts = state.begin_stage(tmp_path, "plan", "deadbeef")
    record = ts.stages["plan"]
    assert record.status == "in_progress"
    assert record.started_at_sha == "deadbeef"
    assert record.started_at_iso is not None


def test_begin_stage_idempotent_when_already_in_progress(tmp_path: Path) -> None:
    _seed(tmp_path)
    state.begin_stage(tmp_path, "plan", "deadbeef")
    # Second call should not raise; should keep the original started_at_iso.
    ts = state.begin_stage(tmp_path, "plan", "newhead")
    assert ts.stages["plan"].started_at_sha == "deadbeef"  # original sha preserved


def test_begin_stage_rejects_completed_stage(tmp_path: Path) -> None:
    _seed(tmp_path)
    state.begin_stage(tmp_path, "plan", "h1")
    state.finish_stage(tmp_path, "plan", "completed", "h2")
    with pytest.raises(ValueError, match="cannot begin"):
        state.begin_stage(tmp_path, "plan", "h3")


def test_begin_stage_unknown_stage_raises(tmp_path: Path) -> None:
    _seed(tmp_path)
    with pytest.raises(ValueError, match=r"not in state\.stages"):
        state.begin_stage(tmp_path, "nonexistent", "h")


def test_finish_stage_marks_completed(tmp_path: Path) -> None:
    _seed(tmp_path)
    state.begin_stage(tmp_path, "ticket", "h1")
    ts = state.finish_stage(tmp_path, "ticket", "completed", "h2")
    record = ts.stages["ticket"]
    assert record.status == "completed"
    assert record.finished_at_sha == "h2"
    assert record.finished_at_iso is not None


def test_finish_stage_marks_failed_with_detail(tmp_path: Path) -> None:
    _seed(tmp_path)
    state.begin_stage(tmp_path, "implement", "h1")
    ts = state.finish_stage(
        tmp_path, "implement", "failed", "h2", failure_detail="subagent crashed"
    )
    record = ts.stages["implement"]
    assert record.status == "failed"
    assert record.failure_detail == "subagent crashed"


def test_finish_stage_rejects_non_terminal_status(tmp_path: Path) -> None:
    _seed(tmp_path)
    with pytest.raises(ValueError, match=r"completed\|failed"):
        state.finish_stage(tmp_path, "ticket", "in_progress", "h")  # type: ignore[arg-type]


def test_finish_stage_persists_skill_output(tmp_path: Path) -> None:
    _seed(tmp_path)
    state.begin_stage(tmp_path, "ticket", "h")
    ts = state.finish_stage(
        tmp_path, "ticket", "completed", "h", skill_output={"pr_url": "https://x/1"}
    )
    assert ts.stages["ticket"].skill_output == {"pr_url": "https://x/1"}


# ─── pick_next + find_failed ────────────────────────────────────────────────


def test_pick_next_returns_first_pending_in_order(tmp_path: Path) -> None:
    ts = _seed(tmp_path)
    order = ["ticket", "plan", "implement", "commit", "reflect"]
    assert state.pick_next_pending(ts, order) == "ticket"
    state.begin_stage(tmp_path, "ticket", "h1")
    state.finish_stage(tmp_path, "ticket", "completed", "h1")
    ts2, _ = state.read(tmp_path)
    assert ts2 is not None
    assert state.pick_next_pending(ts2, order) == "plan"


def test_pick_next_resumes_in_progress_stage(tmp_path: Path) -> None:
    # A stage left in_progress by a crashed run must be resumed, not skipped.
    _seed(tmp_path)
    order = ["ticket", "plan", "implement", "commit", "reflect"]
    state.begin_stage(tmp_path, "ticket", "h1")
    state.finish_stage(tmp_path, "ticket", "completed", "h1")
    state.begin_stage(tmp_path, "plan", "h2")  # crashes here, left in_progress
    ts, _ = state.read(tmp_path)
    assert ts is not None
    assert ts.stages["plan"].status == "in_progress"
    assert state.pick_next_pending(ts, order) == "plan"


def test_pick_next_returns_none_when_all_done(tmp_path: Path) -> None:
    _seed(tmp_path)
    for s in ["ticket", "plan", "implement", "commit", "reflect"]:
        state.begin_stage(tmp_path, s, "h")
        state.finish_stage(tmp_path, s, "completed", "h")
    ts, _ = state.read(tmp_path)
    assert ts is not None
    assert state.pick_next_pending(ts, list(ts.stages)) is None


def test_find_failed_returns_first_failed(tmp_path: Path) -> None:
    _seed(tmp_path)
    state.begin_stage(tmp_path, "plan", "h")
    state.finish_stage(tmp_path, "plan", "failed", "h", failure_detail="oops")
    ts, _ = state.read(tmp_path)
    assert ts is not None
    assert state.find_failed(ts) == "plan"


def test_find_failed_returns_none_when_no_failure(tmp_path: Path) -> None:
    ts = _seed(tmp_path)
    assert state.find_failed(ts) is None


# ─── Atomic write semantics ──────────────────────────────────────────────────


def test_atomic_write_replaces_in_place(tmp_path: Path) -> None:
    _seed(tmp_path)
    path = tmp_path / "state.json"
    inode_before = path.stat().st_ino
    state.begin_stage(tmp_path, "ticket", "h")
    # Atomic rename SHOULD produce a different inode (rename target replaces).
    inode_after = path.stat().st_ino
    # We can't always assert different inode (depends on FS); only assert file
    # parses cleanly after the write.
    del inode_before, inode_after
    assert json.loads(path.read_text(encoding="utf-8"))["ticket"] == "FT-1234"


def test_atomic_write_durable_with_parent_dir_fsync(tmp_path: Path) -> None:
    # _atomic_write fsyncs the parent dir after os.replace for crash durability.
    # The suppressed-OSError fsync must not break the write on any platform.
    _seed(tmp_path)
    state.begin_stage(tmp_path, "ticket", "h")
    path = tmp_path / "state.json"
    assert json.loads(path.read_text(encoding="utf-8"))["stages"]["ticket"]["status"] == (
        "in_progress"
    )


def test_no_temp_files_leak_after_write(tmp_path: Path) -> None:
    _seed(tmp_path)
    state.begin_stage(tmp_path, "ticket", "h")
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".state.json.")]
    assert leftovers == []


# ─── Rolling backups ─────────────────────────────────────────────────────────


def test_backup_files_created_on_overwrite(tmp_path: Path) -> None:
    _seed(tmp_path)
    state.begin_stage(tmp_path, "ticket", "h")
    backups = list(tmp_path.glob("state.json.*.bak"))
    assert len(backups) >= 1


def test_backup_trim_keeps_only_last_five(tmp_path: Path) -> None:
    _seed(tmp_path)
    # 7 writes total → 6 backups → trim to 5.
    for i, name in enumerate(["plan", "implement", "commit", "reflect"]):
        # Each begin_stage call writes once.
        state.begin_stage(tmp_path, name, f"h{i}")
        # And finish_stage writes again.
        state.finish_stage(tmp_path, name, "completed", f"h{i}")
    backups = list(tmp_path.glob("state.json.*.bak"))
    assert len(backups) <= state.BACKUP_RETENTION


# ─── Quarantine + recovery ───────────────────────────────────────────────────


def test_quarantine_when_state_json_corrupt_loads_from_bak(tmp_path: Path) -> None:
    _seed(tmp_path)
    state.begin_stage(tmp_path, "ticket", "h1")
    # Corrupt the live state.json.
    (tmp_path / "state.json").write_text("not json at all", encoding="utf-8")
    loaded, exit_code = state.read(tmp_path)
    assert exit_code == 1  # quarantine triggered, loaded from .bak
    assert loaded is not None
    # The recovered backup is the pre-begin_stage snapshot.
    quarantines = list(tmp_path.glob("state.json.quarantine.*"))
    assert len(quarantines) == 1


def test_quarantine_on_unexpected_stage_record_key(tmp_path: Path) -> None:
    # Schema drift: an extra/renamed key in a stage record makes
    # StageRecord(**entry) raise TypeError. read() must treat that as corrupt
    # (quarantine + recover from .bak), not crash with an uncaught TypeError.
    _seed(tmp_path)
    state.begin_stage(tmp_path, "ticket", "h1")  # produces a parseable .bak
    (tmp_path / "state.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "ticket": "FT-1234",
                "run_id": "0123456789abcdef",
                "backend": "jira",
                "started_at": "x",
                "stages": {"ticket": {"status": "pending", "bogus_extra_key": 1}},
            }
        ),
        encoding="utf-8",
    )
    loaded, exit_code = state.read(tmp_path)
    assert exit_code == 1
    assert loaded is not None
    quarantines = list(tmp_path.glob("state.json.quarantine.*"))
    assert len(quarantines) == 1


def test_unrecoverable_when_state_json_corrupt_and_no_bak(tmp_path: Path) -> None:
    _seed(tmp_path)
    # Remove all backups, then corrupt state.json.
    for bak in tmp_path.glob("state.json.*.bak"):
        bak.unlink()
    (tmp_path / "state.json").write_text("not json", encoding="utf-8")
    loaded, exit_code = state.read(tmp_path)
    assert exit_code == 2
    assert loaded is None


def test_unrecoverable_when_all_baks_also_corrupt(tmp_path: Path) -> None:
    _seed(tmp_path)
    state.begin_stage(tmp_path, "ticket", "h1")
    # Corrupt every backup AND the live file.
    for bak in tmp_path.glob("state.json.*.bak"):
        bak.write_text("nope", encoding="utf-8")
    (tmp_path / "state.json").write_text("nope", encoding="utf-8")
    loaded, exit_code = state.read(tmp_path)
    assert exit_code == 2
    assert loaded is None


# ─── Schema validation on read ───────────────────────────────────────────────


def test_read_rejects_wrong_schema_version(tmp_path: Path) -> None:
    (tmp_path / "state.json").write_text(
        json.dumps(
            {
                "schema_version": 999,
                "ticket": "FT-1",
                "run_id": "abc",
                "backend": "jira",
                "started_at": "x",
                "stages": {},
            }
        ),
        encoding="utf-8",
    )
    # No .bak available; quarantine + exit 2.
    loaded, exit_code = state.read(tmp_path)
    assert exit_code == 2
    assert loaded is None


def test_read_rejects_missing_top_level_keys(tmp_path: Path) -> None:
    (tmp_path / "state.json").write_text(
        json.dumps({"schema_version": 1, "ticket": "FT-1"}), encoding="utf-8"
    )
    loaded, exit_code = state.read(tmp_path)
    assert exit_code == 2
    assert loaded is None


# ─── Concurrency: multiprocessing flock contention ───────────────────────────


def _writer_proc(ticket_dir_str: str, stage: str, head: str) -> None:
    """Top-level so multiprocessing can pickle it on macOS spawn-start."""
    state.begin_stage(Path(ticket_dir_str), stage, head)


def test_concurrent_writers_serialize_via_flock(tmp_path: Path) -> None:
    _seed(tmp_path)
    # Two workers each call begin_stage on different stages. flock serializes
    # the writes; the resulting state.json must parse and reflect both writes.
    ctx = multiprocessing.get_context("spawn")
    p1 = ctx.Process(target=_writer_proc, args=(str(tmp_path), "plan", "ha"))
    p2 = ctx.Process(target=_writer_proc, args=(str(tmp_path), "implement", "hb"))
    p1.start()
    p2.start()
    p1.join(timeout=10)
    p2.join(timeout=10)
    assert p1.exitcode == 0
    assert p2.exitcode == 0

    ts, exit_code = state.read(tmp_path)
    assert exit_code == 0
    assert ts is not None
    assert ts.stages["plan"].status == "in_progress"
    assert ts.stages["implement"].status == "in_progress"


# ─── CLI ─────────────────────────────────────────────────────────────────────


def test_cli_init_emits_state_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = state.cli_main(
        [
            "init",
            str(tmp_path),
            "--ticket",
            "FT-1",
            "--backend",
            "jira",
            "--stage",
            "ticket",
            "--stage",
            "plan",
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ticket"] == "FT-1"
    assert "plan" in payload["stages"]


def test_cli_read_emits_state_after_init(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(tmp_path)
    rc = state.cli_main(["read", str(tmp_path)])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ticket"] == "FT-1234"


def test_cli_read_absent_emits_null(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = state.cli_main(["read", str(tmp_path)])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "null"


def test_cli_begin_and_finish_lifecycle(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed(tmp_path)
    rc = state.cli_main(
        ["begin", "--ticket-dir", str(tmp_path), "--stage", "ticket", "--head-sha", "h1"]
    )
    assert rc == 0
    capsys.readouterr()
    rc = state.cli_main(
        [
            "finish",
            "--ticket-dir",
            str(tmp_path),
            "--stage",
            "ticket",
            "--status",
            "completed",
            "--head-sha",
            "h2",
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["stages"]["ticket"]["status"] == "completed"


def test_cli_finish_rejects_invalid_skill_output_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(tmp_path)
    state.begin_stage(tmp_path, "ticket", "h")
    rc = state.cli_main(
        [
            "finish",
            "--ticket-dir",
            str(tmp_path),
            "--stage",
            "ticket",
            "--status",
            "completed",
            "--head-sha",
            "h",
            "--skill-output",
            "{not json",
        ]
    )
    assert rc == 1
    assert "not JSON" in capsys.readouterr().err
