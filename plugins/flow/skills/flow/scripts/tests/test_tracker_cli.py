"""Tests for tracker_cli.py — CLI wrapper around the Tracker Protocol."""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
from typing import Any

import pytest

import pending_mutations
import tracker_cli
from tracker import TrackerError


def _seed_workspace(root: Path, backend: str = "jira") -> None:
    flow = root / ".flow"
    flow.mkdir(parents=True, exist_ok=True)
    if backend == "jira":
        body = (
            '[tracker]\nbackend = "jira"\n\n'
            '[tracker.jira]\ncloud_id = "x"\nproject_key = "FT"\n\n'
            '[memory]\nnamespace = "demo"\n'
        )
    else:
        body = (
            '[tracker]\nbackend = "beads"\n\n'
            '[tracker.beads]\nprefix = "bd"\n\n'
            '[memory]\nnamespace = "demo"\n'
        )
    (flow / "workspace.toml").write_text(body, encoding="utf-8")


class _FakeTracker:
    """Records calls + returns scripted responses."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def _record(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))

    def get(self, key: str) -> dict[str, Any]:
        self._record("get", key)
        return {"key": key, "summary": "test ticket", "status": "Open"}

    def list_assigned(self, filter: str = "open") -> list[dict[str, Any]]:
        self._record("list_assigned", filter)
        return [{"key": "FT-1"}, {"key": "FT-2"}]

    def state(self, key: str) -> dict[str, Any]:
        self._record("state", key)
        return {"normalized": "in_progress", "native_status": "In Progress"}

    def list_transitions(self, key: str) -> list[dict[str, Any]]:
        self._record("list_transitions", key)
        return [
            {
                "id": "31",
                "name": "Start Progress",
                "to_state": "In Progress",
                "to_normalized_state": "in_progress",
            },
            {
                "id": "41",
                "name": "Close",
                "to_state": "Done",
                "to_normalized_state": "done",
            },
        ]

    def transition(
        self, key: str, transition_id: str, fields: dict | None = None
    ) -> dict[str, Any]:
        self._record("transition", key, transition_id, fields)
        return {"success": True, "new_state": {"normalized": "in_progress"}}

    def create(
        self,
        summary: dict,
        description: dict,
        type: str,
        parent: str | None = None,
        labels: list[str] | None = None,
        assignee: str | None = None,
    ) -> str:
        self._record("create", summary, description, type, parent, labels, assignee)
        return "FT-99"

    def comment(self, key: str, body: dict) -> None:
        self._record("comment", key, body)

    def is_shipped(self, key: str) -> dict[str, Any]:
        self._record("is_shipped", key)
        return {"state": "not_shipped", "shipped_at": None, "evidence": None, "source": "none"}


class _FailingTracker(_FakeTracker):
    def get(self, key: str) -> dict[str, Any]:
        raise TrackerError(f"network failed for {key}")


# ─── Workspace config ─────────────────────────────────────────────────────────


def test_read_tracker_config_flattens_jira(tmp_path: Path) -> None:
    _seed_workspace(tmp_path, backend="jira")
    config = tracker_cli._read_tracker_config(tmp_path)
    assert config["backend"] == "jira"
    assert config["cloud_id"] == "x"
    assert config["project_key"] == "FT"


def test_read_tracker_config_flattens_beads(tmp_path: Path) -> None:
    _seed_workspace(tmp_path, backend="beads")
    config = tracker_cli._read_tracker_config(tmp_path)
    assert config["backend"] == "beads"
    assert config["prefix"] == "bd"
    assert "workspace_root" in config


def test_read_tracker_config_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(tracker_cli._WorkspaceConfigError, match=r"no workspace\.toml"):
        tracker_cli._read_tracker_config(tmp_path)


def test_read_tracker_config_unknown_backend_raises(tmp_path: Path) -> None:
    (tmp_path / ".flow").mkdir()
    (tmp_path / ".flow" / "workspace.toml").write_text(
        '[tracker]\nbackend = "garbage"\n', encoding="utf-8"
    )
    with pytest.raises(tracker_cli._WorkspaceConfigError, match=r"unknown tracker\.backend"):
        tracker_cli._read_tracker_config(tmp_path)


# ─── Subcommand dispatch ─────────────────────────────────────────────────────


def _factory(tracker_obj: _FakeTracker):
    def make(_config):
        return tracker_obj

    return make


def test_get_emits_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed_workspace(tmp_path)
    tk = _FakeTracker()
    rc = tracker_cli.cli_main(
        ["--workspace-root", str(tmp_path), "get", "--key", "FT-1"],
        tracker_factory=_factory(tk),
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["key"] == "FT-1"
    assert tk.calls[0] == ("get", ("FT-1",), {})


def test_list_assigned_default_filter(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed_workspace(tmp_path)
    tk = _FakeTracker()
    rc = tracker_cli.cli_main(
        ["--workspace-root", str(tmp_path), "list-assigned"],
        tracker_factory=_factory(tk),
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload) == 2
    assert tk.calls[0] == ("list_assigned", ("open",), {})


def test_list_assigned_custom_filter(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed_workspace(tmp_path)
    tk = _FakeTracker()
    tracker_cli.cli_main(
        ["--workspace-root", str(tmp_path), "list-assigned", "--filter", "all"],
        tracker_factory=_factory(tk),
    )
    assert tk.calls[0] == ("list_assigned", ("all",), {})


def test_state_emits_normalized(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed_workspace(tmp_path)
    tk = _FakeTracker()
    rc = tracker_cli.cli_main(
        ["--workspace-root", str(tmp_path), "state", "--key", "FT-1"],
        tracker_factory=_factory(tk),
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["normalized"] == "in_progress"


def test_transition_finds_by_normalized(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed_workspace(tmp_path)
    tk = _FakeTracker()
    rc = tracker_cli.cli_main(
        [
            "--workspace-root",
            str(tmp_path),
            "transition",
            "--key",
            "FT-1",
            "--to-state",
            "in_progress",
        ],
        tracker_factory=_factory(tk),
    )
    assert rc == 0
    # Two calls: list_transitions then transition.
    assert tk.calls[0][0] == "list_transitions"
    assert tk.calls[1] == ("transition", ("FT-1", "31", None), {})


def test_transition_finds_by_native_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_workspace(tmp_path)
    tk = _FakeTracker()
    rc = tracker_cli.cli_main(
        [
            "--workspace-root",
            str(tmp_path),
            "transition",
            "--key",
            "FT-1",
            "--to-state",
            "Close",
        ],
        tracker_factory=_factory(tk),
    )
    assert rc == 0
    assert tk.calls[1][1] == ("FT-1", "41", None)


def test_transition_unknown_state_returns_3(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_workspace(tmp_path)
    tk = _FakeTracker()
    rc = tracker_cli.cli_main(
        [
            "--workspace-root",
            str(tmp_path),
            "transition",
            "--key",
            "FT-1",
            "--to-state",
            "no-such-state",
        ],
        tracker_factory=_factory(tk),
    )
    assert rc == 3
    assert "no transition" in capsys.readouterr().err


def test_transition_with_fields(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    tk = _FakeTracker()
    rc = tracker_cli.cli_main(
        [
            "--workspace-root",
            str(tmp_path),
            "transition",
            "--key",
            "FT-1",
            "--to-state",
            "in_progress",
            "--field",
            "resolution=Done",
            "--field",
            "comment=ok",
        ],
        tracker_factory=_factory(tk),
    )
    assert rc == 0
    assert tk.calls[1] == (
        "transition",
        ("FT-1", "31", {"resolution": "Done", "comment": "ok"}),
        {},
    )


def test_transition_bad_field_returns_3(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed_workspace(tmp_path)
    tk = _FakeTracker()
    rc = tracker_cli.cli_main(
        [
            "--workspace-root",
            str(tmp_path),
            "transition",
            "--key",
            "FT-1",
            "--to-state",
            "in_progress",
            "--field",
            "noeq",
        ],
        tracker_factory=_factory(tk),
    )
    assert rc == 3
    assert "missing '='" in capsys.readouterr().err


def test_comment_invokes_tracker(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    tk = _FakeTracker()
    rc = tracker_cli.cli_main(
        [
            "--workspace-root",
            str(tmp_path),
            "comment",
            "--key",
            "FT-1",
            "--text",
            "looks good",
        ],
        tracker_factory=_factory(tk),
    )
    assert rc == 0
    # Content TypedDict shape is {body, fmt}; JiraAdapter._content_to_adf reads content[fmt].
    name, call_args, _ = tk.calls[0]
    assert name == "comment"
    assert call_args[0] == "FT-1"
    body = call_args[1]
    assert body == {"body": "looks good", "fmt": "md"}
    assert body["fmt"] == "md"


def test_create_minimal_emits_key(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed_workspace(tmp_path)
    tk = _FakeTracker()
    rc = tracker_cli.cli_main(
        [
            "--workspace-root",
            str(tmp_path),
            "create",
            "--summary",
            "New thing",
            "--type",
            "task",
        ],
        tracker_factory=_factory(tk),
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"key": "FT-99"}
    name, call_args, _ = tk.calls[0]
    assert name == "create"
    summary, description, ttype, parent, labels, assignee = call_args
    assert summary == {"body": "New thing", "fmt": "md"}
    assert description == {"body": "", "fmt": "md"}
    assert summary["fmt"] == "md"
    assert description["fmt"] == "md"
    assert ttype == "task"
    assert parent is None
    assert labels is None
    assert assignee is None


def test_create_repeatable_labels(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    tk = _FakeTracker()
    rc = tracker_cli.cli_main(
        [
            "--workspace-root",
            str(tmp_path),
            "create",
            "--summary",
            "X",
            "--type",
            "task",
            "--label",
            "a",
            "--label",
            "b",
        ],
        tracker_factory=_factory(tk),
    )
    assert rc == 0
    _, call_args, _ = tk.calls[0]
    labels = call_args[4]
    assert labels == ["a", "b"]


def test_create_parent_and_assignee_passthrough(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    tk = _FakeTracker()
    rc = tracker_cli.cli_main(
        [
            "--workspace-root",
            str(tmp_path),
            "create",
            "--summary",
            "X",
            "--description",
            "details",
            "--type",
            "subtask",
            "--parent",
            "FT-1",
            "--assignee",
            "acct-123",
        ],
        tracker_factory=_factory(tk),
    )
    assert rc == 0
    _, call_args, _ = tk.calls[0]
    _summary, description, _ttype, parent, _labels, assignee = call_args
    assert description == {"body": "details", "fmt": "md"}
    assert parent == "FT-1"
    assert assignee == "acct-123"


def test_is_shipped_emits_state(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed_workspace(tmp_path)
    tk = _FakeTracker()
    rc = tracker_cli.cli_main(
        ["--workspace-root", str(tmp_path), "is-shipped", "--key", "FT-1"],
        tracker_factory=_factory(tk),
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["state"] == "not_shipped"


# ─── Error paths ─────────────────────────────────────────────────────────────


def test_missing_workspace_returns_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = tracker_cli.cli_main(
        ["--workspace-root", str(tmp_path), "get", "--key", "FT-1"],
        tracker_factory=_factory(_FakeTracker()),
    )
    assert rc == 2
    assert "workspace.toml" in capsys.readouterr().err


def test_factory_error_returns_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed_workspace(tmp_path)

    def boom(_config):
        raise RuntimeError("factory exploded")

    rc = tracker_cli.cli_main(
        ["--workspace-root", str(tmp_path), "get", "--key", "FT-1"],
        tracker_factory=boom,
    )
    assert rc == 2
    assert "factory error" in capsys.readouterr().err


def test_tracker_error_returns_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed_workspace(tmp_path)
    tk = _FailingTracker()
    rc = tracker_cli.cli_main(
        ["--workspace-root", str(tmp_path), "get", "--key", "FT-1"],
        tracker_factory=_factory(tk),
    )
    assert rc == 1
    assert "tracker error" in capsys.readouterr().err


def _run_transition(
    tmp_path: Path,
    result: dict[str, Any],
    *,
    enqueue: bool = False,
    fields: list[str] | None = None,
) -> tuple[int, str]:
    """Drive `transition in_progress` with a tracker scripted to return `result`."""
    _seed_workspace(tmp_path)

    class _ScriptedTransition(_FakeTracker):
        def transition(self, key, transition_id, fields=None):
            self._record("transition", key, transition_id, fields)
            return result

    tk = _ScriptedTransition()
    argv = [
        "--workspace-root",
        str(tmp_path),
        "transition",
        "--key",
        "FT-1",
        "--to-state",
        "in_progress",
    ]
    if enqueue:
        argv.append("--enqueue-on-transient")
    for f in fields or []:
        argv += ["--field", f]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = tracker_cli.cli_main(argv, tracker_factory=_factory(tk))
    return rc, buf.getvalue()


def test_transition_success_returns_0(tmp_path: Path) -> None:
    rc, _ = _run_transition(
        tmp_path,
        {"success": True, "failure_kind": None, "failure_detail": None},
    )
    assert rc == 0


@pytest.mark.parametrize(
    ("failure_kind", "expected_rc"),
    [
        ("permission_denied", 4),
        ("validator_failed", 4),
        ("missing_required_field", 4),
        ("wrong_source_state", 5),
        ("ambiguous_transition", 5),
    ],
)
def test_transition_failure_kind_maps_to_exit(
    tmp_path: Path, failure_kind: str, expected_rc: int
) -> None:
    rc, out = _run_transition(
        tmp_path,
        {
            "success": False,
            "failure_kind": failure_kind,
            "failure_detail": f"detail for {failure_kind}",
        },
    )
    assert rc == expected_rc
    # Full TransitionResult JSON (including failure_kind + failure_detail) is printed.
    payload = json.loads(out)
    assert payload["failure_kind"] == failure_kind
    assert payload["failure_detail"] == f"detail for {failure_kind}"


def test_transition_unknown_failure_kind_returns_1(tmp_path: Path) -> None:
    rc, out = _run_transition(
        tmp_path,
        {"success": False, "failure_kind": "validation_error", "failure_detail": "x"},
    )
    assert rc == 1
    assert json.loads(out)["failure_kind"] == "validation_error"


def test_transition_failure_without_kind_returns_1(tmp_path: Path) -> None:
    rc, _ = _run_transition(tmp_path, {"success": False})
    assert rc == 1


# ─── Pending-mutations enqueue on transient transition failure ────────────────


def test_transition_transient_with_flag_enqueues(tmp_path: Path) -> None:
    rc, _ = _run_transition(tmp_path, {"success": False, "failure_kind": None}, enqueue=True)
    assert rc == 1
    entries = pending_mutations.list_mutations(tmp_path)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["op"] == "transition"
    assert entry["ticket"] == "FT-1"
    assert entry["args"]["transition_id"] == "31"
    assert entry["args"]["fields"] is None
    assert entry["expected_postcondition"]["normalized"] == "in_progress"


def test_transition_transient_without_flag_no_enqueue(tmp_path: Path) -> None:
    rc, _ = _run_transition(tmp_path, {"success": False, "failure_kind": None})
    assert rc == 1
    assert pending_mutations.list_mutations(tmp_path) == []


def test_transition_raised_trackererror_with_flag_enqueues(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)

    class _RaisingTransition(_FakeTracker):
        def transition(self, key, transition_id, fields=None):
            raise TrackerError("network down")

    tk = _RaisingTransition()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = tracker_cli.cli_main(
            [
                "--workspace-root",
                str(tmp_path),
                "transition",
                "--key",
                "FT-1",
                "--to-state",
                "in_progress",
                "--enqueue-on-transient",
            ],
            tracker_factory=_factory(tk),
        )
    assert rc == 1
    assert len(pending_mutations.list_mutations(tmp_path)) == 1


@pytest.mark.parametrize(
    ("failure_kind", "expected_rc"),
    [("permission_denied", 4), ("wrong_source_state", 5)],
)
def test_transition_hard_failure_with_flag_no_enqueue(
    tmp_path: Path, failure_kind: str, expected_rc: int
) -> None:
    rc, _ = _run_transition(
        tmp_path, {"success": False, "failure_kind": failure_kind}, enqueue=True
    )
    assert rc == expected_rc
    assert pending_mutations.list_mutations(tmp_path) == []


def test_transition_success_with_flag_no_enqueue(tmp_path: Path) -> None:
    rc, _ = _run_transition(tmp_path, {"success": True}, enqueue=True)
    assert rc == 0
    assert pending_mutations.list_mutations(tmp_path) == []


def test_transition_enqueue_exception_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(pending_mutations, "append_mutation", boom)
    rc, _ = _run_transition(tmp_path, {"success": False, "failure_kind": None}, enqueue=True)
    assert rc == 1


def test_transition_enqueue_idempotent(tmp_path: Path) -> None:
    result = {"success": False, "failure_kind": None}
    rc1, _ = _run_transition(tmp_path, result, enqueue=True)
    rc2, _ = _run_transition(tmp_path, result, enqueue=True)
    assert rc1 == 1
    assert rc2 == 1
    assert len(pending_mutations.list_mutations(tmp_path)) == 1


def test_transition_with_fields_enqueues_fields(tmp_path: Path) -> None:
    rc, _ = _run_transition(
        tmp_path,
        {"success": False, "failure_kind": None},
        enqueue=True,
        fields=["comment=ok"],
    )
    assert rc == 1
    entries = pending_mutations.list_mutations(tmp_path)
    assert len(entries) == 1
    assert entries[0]["args"]["fields"] == {"comment": "ok"}
