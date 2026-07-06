from __future__ import annotations

import json
from pathlib import Path
from typing import Any, override

import pytest

import pending_mutations
import sync


class _FakeTracker:
    def __init__(self, states: dict[str, dict[str, Any]]) -> None:
        self._states = states
        self.transitions: list[tuple[str, str]] = []
        self.comments: list[tuple[str, Any]] = []
        self.links: list[tuple[str, str, str]] = []
        self.creates: list[tuple] = []

    def state(self, key: str) -> dict[str, Any]:
        return self._states.get(key, {"normalized": "open", "native_status": "Open"})

    def transition(
        self, key: str, transition_id: str, fields: dict | None = None
    ) -> dict[str, Any]:
        self.transitions.append((key, transition_id))
        self._states[key] = {"normalized": "done", "native_status": "Done"}
        return {"success": True}

    def comment(self, key: str, body: Any) -> None:
        self.comments.append((key, body))

    def link(self, from_key: str, to_key: str, kind: str) -> None:
        self.links.append((from_key, to_key, kind))

    def create(
        self,
        summary: Any,
        description: Any,
        type: str,
        parent: str | None = None,
        labels: list[str] | None = None,
        assignee: str | None = None,
    ) -> str:
        self.creates.append((summary, description, type, parent, labels, assignee))
        return "FT-NEW"


class _RaisingTracker(_FakeTracker):
    @override
    def comment(self, key: str, body: Any) -> None:
        raise RuntimeError("network error mid-drain")


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


def test_reconcile_applies_pending_comment(tmp_path: Path) -> None:
    _seed(tmp_path, ticket="FT-4", op="comment", args={"body": "hi"})
    tracker = _FakeTracker({})
    report = sync.reconcile(tmp_path, tracker)
    assert len(report["applied"]) == 1
    assert report["removed"] == 1
    assert tracker.comments == [("FT-4", "hi")]
    assert pending_mutations.list_mutations(tmp_path) == []


def test_reconcile_applies_pending_link(tmp_path: Path) -> None:
    _seed(tmp_path, ticket="FT-5", op="link", args={"to_key": "FT-9", "kind": "blocks"})
    tracker = _FakeTracker({})
    report = sync.reconcile(tmp_path, tracker)
    assert len(report["applied"]) == 1
    assert report["removed"] == 1
    assert tracker.links == [("FT-5", "FT-9", "blocks")]
    assert pending_mutations.list_mutations(tmp_path) == []


def test_reconcile_parks_legacy_edit_entry(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # op=edit predates its removal from VALID_OPS; a queued entry must not wedge
    # sync at exit 1 (parked, kept on disk, not counted as failed).
    path = pending_mutations.pending_mutations_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "idempotency_key": "k-edit",
        "ticket": "FT-6",
        "op": "edit",
        "args": {"fields": {"summary": "x"}},
    }
    path.write_text(json.dumps(entry) + "\n")
    tracker = _FakeTracker({})
    report = sync.reconcile(tmp_path, tracker)
    assert report["parked"] == ["k-edit"]
    assert report["failed"] == []
    assert report["removed"] == 0
    assert len(pending_mutations.list_mutations(tmp_path)) == 1
    assert "op=edit is not replayable" in capsys.readouterr().err


def test_reconcile_applies_pending_create(tmp_path: Path) -> None:
    _seed(
        tmp_path,
        ticket="FT-8",
        op="create",
        args={"summary": "new ticket", "description": "body", "type": "task"},
    )
    tracker = _FakeTracker({})
    report = sync.reconcile(tmp_path, tracker)
    assert len(report["applied"]) == 1
    assert report["removed"] == 1
    assert tracker.creates == [("new ticket", "body", "task", None, None, None)]
    assert pending_mutations.list_mutations(tmp_path) == []


def test_reconcile_unknown_op_falls_through(tmp_path: Path) -> None:
    path = pending_mutations.pending_mutations_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"idempotency_key": "k-bogus", "ticket": "FT-7", "op": "bogus", "args": {}}
    path.write_text(json.dumps(entry) + "\n")
    tracker = _FakeTracker({})
    report = sync.reconcile(tmp_path, tracker)
    assert len(report["failed"]) == 1
    assert report["removed"] == 0
    assert len(pending_mutations.list_mutations(tmp_path)) == 1
    assert tracker.comments == []
    assert tracker.links == []
    assert tracker.transitions == []
    assert tracker.creates == []


def test_reconcile_keeps_entry_when_tracker_raises(tmp_path: Path) -> None:
    _seed(tmp_path, ticket="FT-R", op="comment", args={"body": "hi"})
    key = pending_mutations.list_mutations(tmp_path)[0]["idempotency_key"]
    report = sync.reconcile(tmp_path, _RaisingTracker({}))
    assert report["failed"] == [key]
    assert report["applied"] == []
    assert report["removed"] == 0
    assert len(pending_mutations.list_mutations(tmp_path)) == 1


def test_reconcile_continues_drain_when_one_op_raises(tmp_path: Path) -> None:
    _seed(tmp_path, ticket="FT-R", op="comment", args={"body": "hi"})
    _seed(tmp_path, ticket="FT-OK", op="link", args={"to_key": "FT-9", "kind": "blocks"})
    keys = {m["ticket"]: m["idempotency_key"] for m in pending_mutations.list_mutations(tmp_path)}
    report = sync.reconcile(tmp_path, _RaisingTracker({}))
    assert keys["FT-OK"] in report["applied"]
    assert report["removed"] == 1
    assert keys["FT-R"] in report["failed"]
    survivors = {m["idempotency_key"] for m in pending_mutations.list_mutations(tmp_path)}
    assert keys["FT-R"] in survivors
    assert keys["FT-OK"] not in survivors


def test_reconcile_postcondition_matches_native_status_case_insensitively(
    tmp_path: Path,
) -> None:
    # tracker_cli enqueues the lowercased --to-state; a name-form target like
    # "To Do" must still satisfy its postcondition against the native status.
    _seed(
        tmp_path,
        ticket="FT-9",
        op="transition",
        args={"transition_id": "11"},
        expected_postcondition={"normalized": "to do"},
    )
    tracker = _FakeTracker({"FT-9": {"normalized": "open", "native_status": "To Do"}})
    report = sync.reconcile(tmp_path, tracker)
    assert len(report["applied_externally"]) == 1
    assert tracker.transitions == []
    assert pending_mutations.list_mutations(tmp_path) == []


# ─── _build_tracker / cli_main ────────────────────────────────────────────────


def _seed_workspace(root: Path) -> None:
    flow = root / ".flow"
    flow.mkdir(parents=True, exist_ok=True)
    (flow / "workspace.toml").write_text(
        '[tracker]\nbackend = "beads"\n\n[tracker.beads]\nprefix = "bd"\n',
        encoding="utf-8",
    )


def test_build_tracker_config_carries_workspace_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_workspace(tmp_path)
    captured: dict[str, Any] = {}

    def fake_make(cfg: dict[str, Any]) -> Any:
        captured.update(cfg)
        return object()

    monkeypatch.setattr(sync, "make_tracker", fake_make)
    sync._build_tracker(tmp_path)
    assert captured["backend"] == "beads"
    assert captured["prefix"] == "bd"
    assert captured["workspace_root"] == str(tmp_path)


def test_cli_main_builds_tracker_with_workspace_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_workspace(tmp_path)
    captured: dict[str, Any] = {}

    def fake_make(cfg: dict[str, Any]) -> Any:
        captured.update(cfg)
        return _FakeTracker({})

    monkeypatch.setattr(sync, "make_tracker", fake_make)
    rc = sync.cli_main(["--workspace-root", str(tmp_path)])
    assert rc == 0
    assert captured["workspace_root"] == str(tmp_path.resolve())
    report = json.loads(capsys.readouterr().out)
    assert report["failed"] == []


def test_cli_main_missing_workspace_returns_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = sync.cli_main(["--workspace-root", str(tmp_path)])
    assert rc == 2
    assert "workspace.toml" in capsys.readouterr().err
