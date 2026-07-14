from __future__ import annotations

import json
from pathlib import Path

import pytest

import bootstrap_journal as bj


def _receipt(digest: str = "a" * 64) -> dict[str, str]:
    return {
        "schema": "flow.plan-approval/v1",
        "digest": digest,
        "attempt_id": "attempt-1",
        "approved_base_sha": "b" * 40,
        "route_digest": "c" * 64,
        "plan_file_sha256": "d" * 64,
    }


def test_journal_advances_atomically_and_is_idempotent(tmp_path: Path) -> None:
    journal = bj.BootstrapJournal(tmp_path / "journal.json")
    prepared = journal.prepare(ticket="FT-1", approval=_receipt())
    assert prepared.phase == "prepared"
    assert journal.prepare(ticket="FT-1", approval=_receipt()).digest == prepared.digest
    journal.advance("worktree_intended", worktree=str(tmp_path / "wt"), branch="feat/FT-1-x")
    journal.advance("worktree_created")
    journal.advance("run_seeded", run_id="run-1")
    committed = journal.advance("committed")
    assert committed.phase == "committed"
    assert json.loads((tmp_path / "journal.json").read_text())["phase"] == "committed"


def test_conflicting_tuple_and_phase_skip_fail_closed(tmp_path: Path) -> None:
    journal = bj.BootstrapJournal(tmp_path / "journal.json")
    journal.prepare(ticket="FT-1", approval=_receipt())
    with pytest.raises(bj.JournalError, match="approved tuple"):
        journal.prepare(ticket="FT-1", approval=_receipt("f" * 64))
    with pytest.raises(bj.JournalError, match="phase"):
        journal.advance("run_seeded", run_id="run-1")


def test_incomplete_same_tuple_can_restart_without_becoming_live(tmp_path: Path) -> None:
    journal = bj.BootstrapJournal(tmp_path / "journal.json")
    journal.prepare(ticket="FT-1", approval=_receipt())
    journal.advance("worktree_intended", worktree=str(tmp_path / "wt"), branch="feat/FT-1-x")
    journal.advance("worktree_created")
    recovery = journal.recovery()
    assert recovery.action == "rollback_then_retry"
    restarted = journal.restart_after_rollback()
    assert restarted.phase == "prepared"
    assert restarted.approval_digest == "a" * 64


def test_committed_tuple_recovers_as_existing_run(tmp_path: Path) -> None:
    journal = bj.BootstrapJournal(tmp_path / "journal.json")
    journal.prepare(ticket="FT-1", approval=_receipt())
    journal.advance("worktree_intended", worktree=str(tmp_path / "wt"), branch="feat/FT-1-x")
    journal.advance("worktree_created")
    journal.advance("run_seeded", run_id="run-1")
    journal.advance("committed")
    recovery = journal.recovery()
    assert recovery.action == "return_committed"
    assert recovery.run_id == "run-1"


@pytest.mark.parametrize(
    ("phase", "action"),
    [
        ("prepared", "rollback_then_retry"),
        ("worktree_intended", "rollback_then_retry"),
        ("worktree_created", "rollback_then_retry"),
        ("run_seeded", "rollback_then_retry"),
        ("committed", "return_committed"),
    ],
)
def test_every_durable_phase_has_an_explicit_recovery_action(
    tmp_path: Path, phase: str, action: str
) -> None:
    journal = bj.BootstrapJournal(tmp_path / "journal.json")
    journal.prepare(ticket="FT-1", approval=_receipt())
    if phase != "prepared":
        journal.advance(
            "worktree_intended",
            worktree=str(tmp_path / "wt"),
            branch="feat/FT-1-x",
        )
    if phase in {"worktree_created", "run_seeded", "committed"}:
        journal.advance("worktree_created")
    if phase in {"run_seeded", "committed"}:
        journal.advance("run_seeded", run_id="run-1")
    if phase == "committed":
        journal.advance("committed")
    recovery = journal.recovery()
    assert recovery.phase == phase
    assert recovery.action == action
