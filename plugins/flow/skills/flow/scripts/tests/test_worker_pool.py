from __future__ import annotations

import json
import subprocess
from dataclasses import asdict
from pathlib import Path

import pytest

import worker_pool as wp


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


def test_effective_concurrency_reserves_one_driver_slot() -> None:
    assert wp.effective_concurrency(configured=8, capacity=4) == 3
    assert wp.effective_concurrency(configured=2, capacity=9) == 2
    assert wp.effective_concurrency(configured=3, capacity=1) == 0


@pytest.mark.parametrize(
    ("configured", "capacity", "message"),
    [(-1, 4, "configured concurrency"), (2, 0, "host capacity")],
)
def test_effective_concurrency_rejects_invalid_inputs(
    configured: int, capacity: int, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        wp.effective_concurrency(configured=configured, capacity=capacity)


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (wp.DurableRunState.ABSENT, wp.RecoveryAction.RELAUNCH),
        (wp.DurableRunState.BOOTSTRAPPING, wp.RecoveryAction.MONITOR),
        (wp.DurableRunState.RUNNING, wp.RecoveryAction.MONITOR),
        (wp.DurableRunState.SUCCEEDED, wp.RecoveryAction.SETTLED),
        (wp.DurableRunState.FAILED, wp.RecoveryAction.REPAIR),
        (wp.DurableRunState.CORRUPT, wp.RecoveryAction.REPAIR),
    ],
)
def test_driver_failure_recovery_uses_durable_run_evidence(
    state: wp.DurableRunState, expected: wp.RecoveryAction
) -> None:
    outcome = wp.driver_recovery_outcome(
        wp.DurableRunEvidence(key="FT-1", state=state, run_id="run-1")
    )

    assert outcome.action is expected
    assert outcome.key == "FT-1"
    assert outcome.run_id == "run-1"


def test_driver_recovery_does_not_accept_disposable_worker_handles_as_evidence() -> None:
    # A dead driver can leave a stale native handle or lose it entirely. The recovery
    # seam deliberately accepts only durable run evidence, so either situation maps
    # to the same action and a live durable run is never launched twice.
    evidence = wp.DurableRunEvidence(
        key="FT-1", state=wp.DurableRunState.RUNNING, run_id="run-durable"
    )

    first = wp.driver_recovery_outcome(evidence)
    second = wp.driver_recovery_outcome(evidence)

    assert first == second
    assert first.action is wp.RecoveryAction.MONITOR


def test_recovery_plan_defaults_missing_durable_evidence_to_relaunch() -> None:
    plan = wp.driver_recovery_plan(
        ["FT-1", "FT-2"],
        {
            "FT-2": wp.DurableRunEvidence(
                key="FT-2", state=wp.DurableRunState.SUCCEEDED, run_id="run-2"
            )
        },
    )

    assert [(outcome.key, outcome.action) for outcome in plan] == [
        ("FT-1", wp.RecoveryAction.RELAUNCH),
        ("FT-2", wp.RecoveryAction.SETTLED),
    ]


def test_capture_git_snapshot_is_stable_with_preexisting_dirt(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    (repo / "untracked.txt").write_text("untracked\n", encoding="utf-8")

    assert wp.capture_git_snapshot(repo) == wp.capture_git_snapshot(repo)


def test_capture_git_snapshot_detects_each_worktree_class(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    before = wp.capture_git_snapshot(repo)

    (repo / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    tracked = wp.capture_git_snapshot(repo)
    assert wp.changed_git_fields(before, tracked) == ("tracked_worktree",)

    _git(repo, "add", "tracked.txt")
    staged = wp.capture_git_snapshot(repo)
    assert wp.changed_git_fields(tracked, staged) == ("index_tree", "tracked_worktree")

    (repo / "new.txt").write_text("new\n", encoding="utf-8")
    untracked = wp.capture_git_snapshot(repo)
    assert wp.changed_git_fields(staged, untracked) == ("untracked_worktree",)


def test_cli_limit_reserves_driver_slot(capsys: pytest.CaptureFixture[str]) -> None:
    assert wp.cli_main(["limit", "--configured", "8", "--capacity", "4"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "configured": 8,
        "effective_concurrency": 3,
        "host_capacity": 4,
        "driver_slots": 1,
    }


def test_cli_snapshot_guard_refuses_discovery_mutation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(tmp_path)
    before = tmp_path / "before.json"
    before.write_text(json.dumps(asdict(wp.capture_git_snapshot(repo))), encoding="utf-8")
    (repo / "new.txt").write_text("mutated\n", encoding="utf-8")

    assert (
        wp.cli_main(
            [
                "guard",
                "--workspace-root",
                str(repo),
                "--before",
                str(before),
            ]
        )
        == 3
    )
    assert json.loads(capsys.readouterr().out) == {
        "changed_git_fields": ["untracked_worktree"],
        "unchanged": False,
    }


def test_cli_recover_reduces_only_durable_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    evidence = tmp_path / "evidence.json"
    evidence.write_text(
        json.dumps(
            [
                {"key": "FT-1", "state": "absent"},
                {"key": "FT-2", "state": "running", "run_id": "run-2"},
            ]
        ),
        encoding="utf-8",
    )

    assert wp.cli_main(["recover", "--evidence", str(evidence)]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "outcomes": [
            {"action": "relaunch", "key": "FT-1", "run_id": ""},
            {"action": "monitor", "key": "FT-2", "run_id": "run-2"},
        ]
    }
