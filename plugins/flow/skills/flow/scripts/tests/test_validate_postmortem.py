"""Tests for validate_postmortem.py — 14-day checkpoint stop-path validation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import _memory_paths
import validate_postmortem

NAMESPACE = "demo"
NOW = "2026-05-28T00:00:00Z"


def _write_postmortem(root: Path, fm_lines: list[str]) -> Path:
    path = root / ".flow" / "postmortem.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = "\n".join(fm_lines)
    path.write_text(f"+++\n{fm}\n+++\n\nFreeform postmortem body.\n", encoding="utf-8")
    return path


def _ship_event(root: Path, ticket: str, shipped_at: str) -> None:
    events_dir = _memory_paths.ship_events_dir(root, NAMESPACE)
    events_dir.mkdir(parents=True, exist_ok=True)
    record = {"ticket": ticket, "shipped_at": shipped_at, "evidence": {}}
    (events_dir / f"{ticket}.json").write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _validate(path: Path, root: Path) -> list[str]:
    return validate_postmortem.validate(path, workspace_root=root, namespace=NAMESPACE, now_iso=NOW)


# ─── valid (rollback) ─────────────────────────────────────────────────────────


def test_valid_rollback_passes(tmp_path: Path) -> None:
    path = _write_postmortem(
        tmp_path,
        [
            'root_cause = "tracker_friction"',
            'evidence = ["e1", "e2"]',
            'next_action = "rollback_to_jira_workflow"',
        ],
    )
    assert _validate(path, tmp_path) == []
    assert validate_postmortem._exit_code(_validate(path, tmp_path)) == 0


# ─── schema violations → exit 1 ───────────────────────────────────────────────


def test_missing_root_cause(tmp_path: Path) -> None:
    path = _write_postmortem(
        tmp_path,
        [
            'evidence = ["e1", "e2"]',
            'next_action = "rollback_to_jira_workflow"',
        ],
    )
    violations = _validate(path, tmp_path)
    assert any(v.startswith("root_cause: missing") for v in violations)
    assert validate_postmortem._exit_code(violations) == 1


def test_evidence_fewer_than_two(tmp_path: Path) -> None:
    path = _write_postmortem(
        tmp_path,
        [
            'root_cause = "tracker_friction"',
            'evidence = ["only_one"]',
            'next_action = "rollback_to_jira_workflow"',
        ],
    )
    violations = _validate(path, tmp_path)
    assert any("evidence" in v and "fewer than 2" in v for v in violations)
    assert validate_postmortem._exit_code(violations) == 1


def test_bad_root_cause_enum(tmp_path: Path) -> None:
    path = _write_postmortem(
        tmp_path,
        [
            'root_cause = "aliens"',
            'evidence = ["e1", "e2"]',
            'next_action = "rollback_to_jira_workflow"',
        ],
    )
    violations = _validate(path, tmp_path)
    assert any(v.startswith("root_cause:") and "not in enum" in v for v in violations)
    assert validate_postmortem._exit_code(violations) == 1


def test_bad_next_action_enum(tmp_path: Path) -> None:
    path = _write_postmortem(
        tmp_path,
        [
            'root_cause = "tracker_friction"',
            'evidence = ["e1", "e2"]',
            'next_action = "give_up"',
        ],
    )
    violations = _validate(path, tmp_path)
    assert any(v.startswith("next_action:") and "not in enum" in v for v in violations)
    assert validate_postmortem._exit_code(violations) == 1


# ─── continue_as_is conditional ───────────────────────────────────────────────


def test_continue_as_is_without_trend_evidence(tmp_path: Path) -> None:
    # No ship-events: 0 vs 0 is non-decreasing, so the only failure is the
    # missing trend_evidence block. Must be exit 1, not 2.
    path = _write_postmortem(
        tmp_path,
        [
            'root_cause = "memory_underperformance"',
            'evidence = ["e1", "e2"]',
            'next_action = "continue_as_is"',
        ],
    )
    violations = _validate(path, tmp_path)
    assert any(v.startswith("trend_evidence:") for v in violations)
    assert validate_postmortem._exit_code(violations) == 1


def test_continue_as_is_decreasing_trend_exit_2(tmp_path: Path) -> None:
    # Prior window (now-14d, now-7d]: 2 ships. Current (now-7d, now]: 1 ship.
    _ship_event(tmp_path, "FT-1", "2026-05-16T00:00:00Z")  # prior
    _ship_event(tmp_path, "FT-2", "2026-05-17T00:00:00Z")  # prior
    _ship_event(tmp_path, "FT-3", "2026-05-24T00:00:00Z")  # current
    path = _write_postmortem(
        tmp_path,
        [
            'root_cause = "memory_underperformance"',
            'evidence = ["e1", "e2"]',
            'next_action = "continue_as_is"',
            'trend_evidence = { note = "see ship-events" }',
        ],
    )
    violations = _validate(path, tmp_path)
    trend = [v for v in violations if v.startswith(validate_postmortem.TREND_PREFIX)]
    schema = [v for v in violations if not v.startswith(validate_postmortem.TREND_PREFIX)]
    assert trend, f"expected a trend violation, got {violations}"
    assert schema == [], f"expected no schema violations, got {schema}"
    assert validate_postmortem._exit_code(violations) == 2


def test_continue_as_is_non_decreasing_trend_exit_0(tmp_path: Path) -> None:
    # Prior: 1 ship. Current: 2 ships. Non-decreasing -> valid.
    _ship_event(tmp_path, "FT-1", "2026-05-16T00:00:00Z")  # prior
    _ship_event(tmp_path, "FT-2", "2026-05-24T00:00:00Z")  # current
    _ship_event(tmp_path, "FT-3", "2026-05-25T00:00:00Z")  # current
    path = _write_postmortem(
        tmp_path,
        [
            'root_cause = "memory_underperformance"',
            'evidence = ["e1", "e2"]',
            'next_action = "continue_as_is"',
            'trend_evidence = { note = "see ship-events" }',
        ],
    )
    violations = _validate(path, tmp_path)
    assert violations == [], f"expected no violations, got {violations}"
    assert validate_postmortem._exit_code(violations) == 0


def test_continue_as_is_equal_trend_exit_0(tmp_path: Path) -> None:
    # Prior: 1, current: 1. Equal counts as non-decreasing.
    _ship_event(tmp_path, "FT-1", "2026-05-16T00:00:00Z")  # prior
    _ship_event(tmp_path, "FT-2", "2026-05-24T00:00:00Z")  # current
    path = _write_postmortem(
        tmp_path,
        [
            'root_cause = "memory_underperformance"',
            'evidence = ["e1", "e2"]',
            'next_action = "continue_as_is"',
            'trend_evidence = { note = "see ship-events" }',
        ],
    )
    violations = _validate(path, tmp_path)
    assert violations == []
    assert validate_postmortem._exit_code(violations) == 0


# ─── trend ignores non-primary ship-event files ───────────────────────────────


def test_trend_skips_dupe_files(tmp_path: Path) -> None:
    _ship_event(tmp_path, "FT-1", "2026-05-16T00:00:00Z")  # prior
    _ship_event(tmp_path, "FT-2", "2026-05-24T00:00:00Z")  # current
    # A dupe of the current ship must NOT inflate the current count.
    events_dir = _memory_paths.ship_events_dir(tmp_path, NAMESPACE)
    (events_dir / "FT-2.json.dupe.1.json").write_text(
        json.dumps({"ticket": "FT-2", "shipped_at": "2026-05-25T00:00:00Z"}) + "\n",
        encoding="utf-8",
    )
    path = _write_postmortem(
        tmp_path,
        [
            'root_cause = "memory_underperformance"',
            'evidence = ["e1", "e2"]',
            'next_action = "continue_as_is"',
            'trend_evidence = { note = "x" }',
        ],
    )
    # prior 1, current 1 (dupe ignored) -> non-decreasing -> exit 0.
    assert _validate(path, tmp_path) == []


# ─── extend_dogfood conditional ───────────────────────────────────────────────


def test_extend_dogfood_missing_blocker_evidence(tmp_path: Path) -> None:
    path = _write_postmortem(
        tmp_path,
        [
            'root_cause = "external_dependency_issue"',
            'evidence = ["e1", "e2"]',
            'next_action = "extend_dogfood_one_more_week"',
        ],
    )
    violations = _validate(path, tmp_path)
    assert any(v.startswith("blocker_fixed_evidence:") for v in violations)
    assert validate_postmortem._exit_code(violations) == 1


def test_extend_dogfood_with_blocker_evidence_passes(tmp_path: Path) -> None:
    path = _write_postmortem(
        tmp_path,
        [
            'root_cause = "external_dependency_issue"',
            'evidence = ["e1", "e2"]',
            'next_action = "extend_dogfood_one_more_week"',
            'blocker_fixed_evidence = { commit_sha = "abc123", summary = "fixed it" }',
        ],
    )
    assert _validate(path, tmp_path) == []


# ─── CLI ─────────────────────────────────────────────────────────────────────


def test_cli_valid_exit_0(tmp_path: Path) -> None:
    path = _write_postmortem(
        tmp_path,
        [
            'root_cause = "tracker_friction"',
            'evidence = ["e1", "e2"]',
            'next_action = "rollback_to_jira_workflow"',
        ],
    )
    rc = validate_postmortem.cli_main(
        [
            "--path",
            str(path),
            "--workspace-root",
            str(tmp_path),
            "--namespace",
            NAMESPACE,
            "--now",
            NOW,
        ]
    )
    assert rc == 0


def test_cli_schema_violation_exit_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _write_postmortem(
        tmp_path,
        [
            'evidence = ["e1", "e2"]',
            'next_action = "rollback_to_jira_workflow"',
        ],
    )
    rc = validate_postmortem.cli_main(
        [
            "--path",
            str(path),
            "--workspace-root",
            str(tmp_path),
            "--namespace",
            NAMESPACE,
            "--now",
            NOW,
        ]
    )
    assert rc == 1
    assert "root_cause" in capsys.readouterr().err


def test_cli_trend_failure_exit_2(tmp_path: Path) -> None:
    _ship_event(tmp_path, "FT-1", "2026-05-16T00:00:00Z")
    _ship_event(tmp_path, "FT-2", "2026-05-17T00:00:00Z")
    _ship_event(tmp_path, "FT-3", "2026-05-24T00:00:00Z")
    path = _write_postmortem(
        tmp_path,
        [
            'root_cause = "memory_underperformance"',
            'evidence = ["e1", "e2"]',
            'next_action = "continue_as_is"',
            'trend_evidence = { note = "x" }',
        ],
    )
    rc = validate_postmortem.cli_main(
        [
            "--path",
            str(path),
            "--workspace-root",
            str(tmp_path),
            "--namespace",
            NAMESPACE,
            "--now",
            NOW,
        ]
    )
    assert rc == 2
