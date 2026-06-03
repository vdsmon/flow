from __future__ import annotations

from pathlib import Path
from typing import Any

import pending_mutations
import sync


class _FakeTracker:
    def __init__(self, states: dict[str, dict[str, Any]]) -> None:
        self._states = states
        self.transitions: list[tuple[str, str]] = []

    def state(self, key: str) -> dict[str, Any]:
        return self._states.get(key, {"normalized": "open", "native_status": "Open"})

    def transition(
        self, key: str, transition_id: str, fields: dict | None = None
    ) -> dict[str, Any]:
        self.transitions.append((key, transition_id))
        self._states[key] = {"normalized": "done", "native_status": "Done"}
        return {"success": True}


def _seed(workspace_root: Path, **kw: Any) -> None:
    pending_mutations.append_mutation(workspace_root, intent_at="2026-05-01T00:00:00Z", **kw)


def test_reconcile_applies_pending_transition(tmp_path: Path) -> None:
    _seed(
        tmp_path,
        ticket="FT-1",
        op="transition",
        args={"transition_id": "31"},
        expected_postcondition={"normalized": "done"},
    )
    tracker = _FakeTracker({"FT-1": {"normalized": "in_progress", "native_status": "In Progress"}})
    report = sync.reconcile(tmp_path, tracker)
    assert len(report["applied"]) == 1
    assert tracker.transitions == [("FT-1", "31")]
    assert report["removed"] == 1
    assert pending_mutations.list_mutations(tmp_path) == []


def test_reconcile_skips_already_satisfied(tmp_path: Path) -> None:
    _seed(
        tmp_path,
        ticket="FT-2",
        op="transition",
        args={"transition_id": "31"},
        expected_postcondition={"normalized": "done"},
    )
    tracker = _FakeTracker({"FT-2": {"normalized": "done", "native_status": "Done"}})
    report = sync.reconcile(tmp_path, tracker)
    assert len(report["applied_externally"]) == 1
    assert tracker.transitions == []
    assert pending_mutations.list_mutations(tmp_path) == []


def test_reconcile_superseded_when_pre_state_gone(tmp_path: Path) -> None:
    _seed(
        tmp_path,
        ticket="FT-3",
        op="transition",
        args={"transition_id": "31"},
        expected_pre_state={"tracker_status": "in_progress"},
        expected_postcondition={"normalized": "done"},
    )
    # current state is neither the target nor the expected pre-state -> superseded.
    tracker = _FakeTracker({"FT-3": {"normalized": "blocked", "native_status": "Blocked"}})
    report = sync.reconcile(tmp_path, tracker)
    assert len(report["superseded"]) == 1
    assert tracker.transitions == []
