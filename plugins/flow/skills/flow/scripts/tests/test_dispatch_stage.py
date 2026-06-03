"""Contract tests for dispatch_stage.py.

Covers init/next/finish/status lifecycle, blocked_by surfacing, handler-type
routing JSON, and validate-workspace HARD GATE. git rev-parse HEAD is stubbed
via monkeypatch.setattr(subprocess, "run", ...) — no real git repo needed.
"""

from __future__ import annotations

import json
import socket
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import dispatch_stage as ds
import lease
import state

# ─── Fixtures ────────────────────────────────────────────────────────────────


def _write_workspace(
    root: Path,
    *,
    handlers: dict[str, str] | None = None,
    backend: str = "jira",
    stages: list[str] | None = None,
    compounding: bool = True,
) -> None:
    if stages is None:
        stages = ["ticket", "plan", "implement", "commit", "reflect"]
    if handlers is None:
        handlers = {s: "inline" for s in stages}

    flow = root / ".flow"
    flow.mkdir()
    (flow / ".initialized").touch()

    lines: list[str] = []
    lines.append("[tracker]")
    lines.append(f'backend = "{backend}"')
    if backend == "jira":
        lines.append("[tracker.jira]")
        lines.append('cloud_id = "x"')
        lines.append('project_key = "FT"')
    else:
        lines.append("[tracker.beads]")
        lines.append('prefix = "testpkg"')
    lines.append("[pipeline]")
    lines.append("stages = [" + ", ".join(f'"{s}"' for s in stages) + "]")
    lines.append("[pipeline.handlers]")
    for stage, handler in handlers.items():
        lines.append(f'{stage} = "{handler}"')
    lines.append("[memory]")
    lines.append('namespace = "FT"')
    lines.append("auto_recall = true")
    lines.append(f"compounding = {str(compounding).lower()}")
    lines.append('recall_by = ["branch"]')
    lines.append("recall_top_n = 5")
    (flow / "workspace.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _stub_git_head(monkeypatch: pytest.MonkeyPatch, sha: str = "deadbeef") -> None:
    def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del args, kwargs
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=sha + "\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)


# ─── init ────────────────────────────────────────────────────────────────────


def test_init_creates_state_with_pending_stages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_workspace(tmp_path)
    _stub_git_head(monkeypatch)
    rc, payload = ds.cmd_init(tmp_path, "FT-1234")
    assert rc == 0
    assert payload["ticket"] == "FT-1234"
    assert payload["stages"] == ["ticket", "plan", "implement", "commit", "reflect"]
    state_path = tmp_path / ".flow" / "runs" / "FT-1234" / "state.json"
    assert state_path.exists()


def test_init_fails_when_workspace_invalid(tmp_path: Path) -> None:
    # No .flow/.initialized marker.
    rc, payload = ds.cmd_init(tmp_path, "FT-1234")
    assert rc == 1
    assert "violations" in payload


def test_init_is_idempotent_preserves_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Second init without --force must resume: same run_id, completed stage
    # stays completed (no replay of a finished commit stage).
    _write_workspace(tmp_path, stages=["ticket", "plan"], compounding=False)
    _stub_git_head(monkeypatch)
    rc, first = ds.cmd_init(tmp_path, "FT-1")
    assert rc == 0
    assert first["resumed"] is False
    ds.cmd_next(tmp_path, "FT-1")
    ds.cmd_finish(tmp_path, "FT-1", "ticket", "completed")

    rc, second = ds.cmd_init(tmp_path, "FT-1")
    assert rc == 0
    assert second["resumed"] is True
    assert second["run_id"] == first["run_id"]

    state_path = tmp_path / ".flow" / "runs" / "FT-1" / "state.json"
    state_data = json.loads(state_path.read_text(encoding="utf-8"))
    assert state_data["stages"]["ticket"]["status"] == "completed"


def test_init_force_resets_to_all_pending(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_workspace(tmp_path, stages=["ticket", "plan"], compounding=False)
    _stub_git_head(monkeypatch)
    rc, first = ds.cmd_init(tmp_path, "FT-1")
    assert rc == 0
    ds.cmd_next(tmp_path, "FT-1")
    ds.cmd_finish(tmp_path, "FT-1", "ticket", "completed")

    rc, forced = ds.cmd_init(tmp_path, "FT-1", force=True)
    assert rc == 0
    assert forced["resumed"] is False
    # --force resets state to all-pending but keeps the same run_id so the run
    # stays the lease owner; a fresh run_id would make the still-live lease
    # foreign and force could not reset it.
    assert forced["run_id"] == first["run_id"]

    state_path = tmp_path / ".flow" / "runs" / "FT-1" / "state.json"
    state_data = json.loads(state_path.read_text(encoding="utf-8"))
    assert state_data["stages"]["ticket"]["status"] == "pending"
    assert state_data["stages"]["plan"]["status"] == "pending"


def test_cli_init_force_flag_resets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_workspace(tmp_path, stages=["ticket"], compounding=False)
    _stub_git_head(monkeypatch)
    ds.cmd_init(tmp_path, "FT-1")
    ds.cmd_next(tmp_path, "FT-1")
    ds.cmd_finish(tmp_path, "FT-1", "ticket", "completed")
    rc = ds.cli_main(["init", "--ticket", "FT-1", "--workspace-root", str(tmp_path), "--force"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["resumed"] is False
    state_path = tmp_path / ".flow" / "runs" / "FT-1" / "state.json"
    state_data = json.loads(state_path.read_text(encoding="utf-8"))
    assert state_data["stages"]["ticket"]["status"] == "pending"


def test_init_triggers_recall_promotion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_workspace(tmp_path, stages=["ticket"], compounding=False)
    # _stub_git_head stubs subprocess.run globally, so both head_sha and branch
    # resolve to the stub sha.
    _stub_git_head(monkeypatch, "abc123")

    calls: list[dict[str, Any]] = []

    def fake_promote(workspace_root: Path, **kwargs: Any) -> list[dict[str, Any]]:
        calls.append({"workspace_root": workspace_root, **kwargs})
        return []

    monkeypatch.setattr(ds.recall_pending, "promote_matching", fake_promote)
    rc, payload = ds.cmd_init(tmp_path, "FT-1")
    assert rc == 0
    assert payload["resumed"] is False
    assert len(calls) == 1
    assert calls[0]["ticket"] == "FT-1"
    assert calls[0]["branch"] == "abc123"
    assert calls[0]["cwd"] == str(tmp_path)


def test_init_resume_triggers_recall_promotion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Promotion must fire on the resume path too, not only on fresh init.
    _write_workspace(tmp_path, stages=["ticket", "plan"], compounding=False)
    _stub_git_head(monkeypatch, "abc123")
    ds.cmd_init(tmp_path, "FT-1")

    calls: list[str] = []

    def fake_promote(workspace_root: Path, **kwargs: Any) -> list[dict[str, Any]]:
        del workspace_root
        calls.append(kwargs["ticket"])
        return []

    monkeypatch.setattr(ds.recall_pending, "promote_matching", fake_promote)
    rc, payload = ds.cmd_init(tmp_path, "FT-1")
    assert rc == 0
    assert payload["resumed"] is True
    assert calls == ["FT-1"]


def test_init_succeeds_when_recall_promotion_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Promotion is best-effort; a raised exception must not abort init.
    _write_workspace(tmp_path, stages=["ticket"], compounding=False)
    _stub_git_head(monkeypatch)

    def boom(workspace_root: Path, **kwargs: Any) -> list[dict[str, Any]]:
        del workspace_root, kwargs
        raise RuntimeError("promotion exploded")

    monkeypatch.setattr(ds.recall_pending, "promote_matching", boom)
    rc, payload = ds.cmd_init(tmp_path, "FT-1")
    assert rc == 0
    assert payload["resumed"] is False
    state_path = tmp_path / ".flow" / "runs" / "FT-1" / "state.json"
    assert state_path.exists()


# ─── next: handler routing ───────────────────────────────────────────────────


def test_next_routes_inline_handler(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_workspace(tmp_path, handlers={"ticket": "inline"}, stages=["ticket"], compounding=False)
    _stub_git_head(monkeypatch, "abc123")
    ds.cmd_init(tmp_path, "FT-1")
    rc, payload = ds.cmd_next(tmp_path, "FT-1")
    assert rc == 0
    assert payload["done"] is False
    assert payload["stage"] == "ticket"
    assert payload["handler_type"] == "inline"
    assert payload["reference_doc"] == "references/stage-ticket.md"
    assert payload["head_sha"] == "abc123"


def test_next_surfaces_roles_for_stage_with_roles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_workspace(
        tmp_path,
        handlers={"ticket": "inline", "implement": "subagent:general-purpose"},
        stages=["ticket", "implement"],
        compounding=False,
    )
    _stub_git_head(monkeypatch, "abc123")
    ds.cmd_init(tmp_path, "FT-1")
    # advance past ticket stage
    ds.cmd_finish(tmp_path, "FT-1", "ticket", "completed")
    rc, payload = ds.cmd_next(tmp_path, "FT-1")
    assert rc == 0
    assert payload["stage"] == "implement"
    assert payload["roles"] == ["records_diff_baseline"]


def test_next_surfaces_empty_roles_for_stage_without(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_workspace(tmp_path, handlers={"ticket": "inline"}, stages=["ticket"], compounding=False)
    _stub_git_head(monkeypatch, "abc123")
    ds.cmd_init(tmp_path, "FT-1")
    rc, payload = ds.cmd_next(tmp_path, "FT-1")
    assert rc == 0
    assert payload["roles"] == []


def test_next_routes_subagent_handler(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_workspace(
        tmp_path,
        handlers={"ticket": "inline", "plan": "subagent:Plan"},
        stages=["ticket", "plan"],
        compounding=False,
    )
    _stub_git_head(monkeypatch)
    ds.cmd_init(tmp_path, "FT-1")
    # First next picks ticket.
    rc, payload = ds.cmd_next(tmp_path, "FT-1")
    assert payload["stage"] == "ticket"
    ds.cmd_finish(tmp_path, "FT-1", "ticket", "completed")
    rc, payload = ds.cmd_next(tmp_path, "FT-1")
    assert rc == 0
    assert payload["stage"] == "plan"
    assert payload["handler_type"] == "subagent"
    assert payload["subagent_type"] == "Plan"
    # reference_doc attaches to subagent stages too, not only inline.
    assert payload["reference_doc"] == "references/stage-plan.md"


def test_next_routes_skill_handler(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_workspace(
        tmp_path,
        handlers={"ticket": "skill:ship-it:create"},
        stages=["ticket"],
        compounding=False,
    )
    _stub_git_head(monkeypatch)
    ds.cmd_init(tmp_path, "FT-1")
    rc, payload = ds.cmd_next(tmp_path, "FT-1")
    assert rc == 0
    assert payload["handler_type"] == "skill"
    assert payload["skill_name"] == "ship-it"
    assert payload["skill_args"] == "create"


def test_next_routes_skill_handler_without_args(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_workspace(
        tmp_path,
        handlers={"ticket": "skill:my-skill"},
        stages=["ticket"],
        compounding=False,
    )
    _stub_git_head(monkeypatch)
    ds.cmd_init(tmp_path, "FT-1")
    rc, payload = ds.cmd_next(tmp_path, "FT-1")
    assert rc == 0
    assert payload["handler_type"] == "skill"
    assert payload["skill_name"] == "my-skill"
    assert payload["skill_args"] is None


def test_next_routes_none_handler(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_workspace(
        tmp_path,
        handlers={"ticket": "none"},
        stages=["ticket"],
        compounding=False,
    )
    _stub_git_head(monkeypatch)
    ds.cmd_init(tmp_path, "FT-1")
    rc, payload = ds.cmd_next(tmp_path, "FT-1")
    assert rc == 0
    assert payload["handler_type"] == "none"


def test_next_keeps_stage_pending_when_descriptor_assembly_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # If descriptor assembly raises (handler parse here), begin_stage must NOT
    # have run, so the stage stays pending rather than stuck in_progress.
    _write_workspace(tmp_path, stages=["ticket"], compounding=False)
    _stub_git_head(monkeypatch)
    ds.cmd_init(tmp_path, "FT-1")

    def boom(value: str) -> dict[str, Any]:
        del value
        raise RuntimeError("handler parse exploded")

    monkeypatch.setattr(ds, "_parse_handler", boom)
    with pytest.raises(RuntimeError):
        ds.cmd_next(tmp_path, "FT-1")

    state_path = tmp_path / ".flow" / "runs" / "FT-1" / "state.json"
    state_data = json.loads(state_path.read_text(encoding="utf-8"))
    assert state_data["stages"]["ticket"]["status"] == "pending"


def test_next_writes_in_progress_to_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_workspace(tmp_path, stages=["ticket"], compounding=False)
    _stub_git_head(monkeypatch, "abc123")
    ds.cmd_init(tmp_path, "FT-1")
    ds.cmd_next(tmp_path, "FT-1")
    state_path = tmp_path / ".flow" / "runs" / "FT-1" / "state.json"
    state_data = json.loads(state_path.read_text(encoding="utf-8"))
    assert state_data["stages"]["ticket"]["status"] == "in_progress"
    assert state_data["stages"]["ticket"]["started_at_sha"] == "abc123"


# ─── next: terminal cases ────────────────────────────────────────────────────


def test_next_done_when_all_stages_completed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_workspace(tmp_path, stages=["ticket"], compounding=False)
    _stub_git_head(monkeypatch)
    ds.cmd_init(tmp_path, "FT-1")
    ds.cmd_next(tmp_path, "FT-1")
    ds.cmd_finish(tmp_path, "FT-1", "ticket", "completed")
    rc, payload = ds.cmd_next(tmp_path, "FT-1")
    assert rc == 0
    assert payload == {"done": True}


def test_next_returns_blocked_by_when_stage_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_workspace(tmp_path, stages=["ticket", "plan"], compounding=False)
    _stub_git_head(monkeypatch)
    ds.cmd_init(tmp_path, "FT-1")
    ds.cmd_next(tmp_path, "FT-1")
    ds.cmd_finish(tmp_path, "FT-1", "ticket", "failed", failure_detail="bd not reachable")
    rc, payload = ds.cmd_next(tmp_path, "FT-1")
    assert rc == 0
    assert payload["done"] is False
    assert payload["blocked_by"] == "ticket"
    assert payload["reason"] == "bd not reachable"


def test_next_before_init_returns_exit_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_workspace(tmp_path)
    _stub_git_head(monkeypatch)
    rc, payload = ds.cmd_next(tmp_path, "FT-1234")
    assert rc == 2
    assert "no state.json" in payload["error"]


def test_next_with_invalid_workspace_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_workspace(tmp_path)
    _stub_git_head(monkeypatch)
    ds.cmd_init(tmp_path, "FT-1")
    # Now corrupt the workspace.toml.
    (tmp_path / ".flow" / "workspace.toml").write_text("garbage", encoding="utf-8")
    rc, payload = ds.cmd_next(tmp_path, "FT-1")
    assert rc == 1
    assert "violations" in payload


# ─── finish ──────────────────────────────────────────────────────────────────


def test_finish_records_completed_and_next_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_workspace(tmp_path, stages=["ticket", "plan"], compounding=False)
    _stub_git_head(monkeypatch)
    ds.cmd_init(tmp_path, "FT-1")
    ds.cmd_next(tmp_path, "FT-1")
    rc, payload = ds.cmd_finish(tmp_path, "FT-1", "ticket", "completed")
    assert rc == 0
    assert payload["status"] == "completed"
    assert payload["next_pending"] == "plan"


def test_advance_finishes_and_returns_next_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_workspace(tmp_path, stages=["ticket", "plan"], compounding=False)
    _stub_git_head(monkeypatch)
    ds.cmd_init(tmp_path, "FT-1")
    ds.cmd_next(tmp_path, "FT-1")
    rc, payload = ds.cmd_advance(tmp_path, "FT-1", "ticket", "completed")
    assert rc == 0
    # finish confirmation nested; next descriptor spread at top level.
    assert payload["finished"] == {"stage": "ticket", "status": "completed"}
    assert payload["stage"] == "plan"
    assert payload["done"] is False


def test_advance_returns_done_on_last_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_workspace(tmp_path, stages=["ticket"], compounding=False)
    _stub_git_head(monkeypatch)
    ds.cmd_init(tmp_path, "FT-1")
    ds.cmd_next(tmp_path, "FT-1")
    rc, payload = ds.cmd_advance(tmp_path, "FT-1", "ticket", "completed")
    assert rc == 0
    assert payload["done"] is True
    assert payload["finished"] == {"stage": "ticket", "status": "completed"}


def test_advance_surfaces_finish_error_without_advancing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_workspace(tmp_path, stages=["ticket", "plan"], compounding=False)
    _stub_git_head(monkeypatch)
    ds.cmd_init(tmp_path, "FT-1")
    ds.cmd_next(tmp_path, "FT-1")
    rc, payload = ds.cmd_advance(tmp_path, "FT-1", "ticket", "weirdo")
    assert rc == 1
    assert "completed|failed" in payload["error"]
    # finish errored -> never advanced, so no next descriptor merged in.
    assert "finished" not in payload
    assert "done" not in payload


def test_finish_records_failed_with_detail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_workspace(tmp_path, stages=["ticket"], compounding=False)
    _stub_git_head(monkeypatch)
    ds.cmd_init(tmp_path, "FT-1")
    ds.cmd_next(tmp_path, "FT-1")
    rc, payload = ds.cmd_finish(tmp_path, "FT-1", "ticket", "failed", failure_detail="oops")
    assert rc == 0
    assert payload["status"] == "failed"
    # next_pending None when a stage failed (blocked_by takes over).
    assert payload["next_pending"] is None


def test_finish_rejects_unknown_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_workspace(tmp_path, stages=["ticket"], compounding=False)
    _stub_git_head(monkeypatch)
    ds.cmd_init(tmp_path, "FT-1")
    ds.cmd_next(tmp_path, "FT-1")
    rc, payload = ds.cmd_finish(tmp_path, "FT-1", "ticket", "weirdo")
    assert rc == 1
    assert "completed|failed" in payload["error"]


def test_finish_persists_skill_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_workspace(
        tmp_path,
        handlers={"ticket": "skill:ship-it:create"},
        stages=["ticket"],
        compounding=False,
    )
    _stub_git_head(monkeypatch)
    ds.cmd_init(tmp_path, "FT-1")
    ds.cmd_next(tmp_path, "FT-1")
    rc, _ = ds.cmd_finish(
        tmp_path,
        "FT-1",
        "ticket",
        "completed",
        skill_output={"pr_url": "https://x/1"},
    )
    assert rc == 0
    state_path = tmp_path / ".flow" / "runs" / "FT-1" / "state.json"
    state_data = json.loads(state_path.read_text(encoding="utf-8"))
    assert state_data["stages"]["ticket"]["skill_output"] == {"pr_url": "https://x/1"}


def test_finish_before_init_returns_exit_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_workspace(tmp_path)
    _stub_git_head(monkeypatch)
    rc, _ = ds.cmd_finish(tmp_path, "FT-1", "ticket", "completed")
    assert rc == 2


# ─── status ──────────────────────────────────────────────────────────────────


def test_status_emits_full_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_workspace(tmp_path, stages=["ticket"], compounding=False)
    _stub_git_head(monkeypatch)
    ds.cmd_init(tmp_path, "FT-1")
    rc, payload = ds.cmd_status(tmp_path, "FT-1")
    assert rc == 0
    assert payload["ticket"] == "FT-1"
    assert "stages" in payload


def test_status_before_init_returns_exit_2(tmp_path: Path) -> None:
    rc, _ = ds.cmd_status(tmp_path, "FT-1")
    assert rc == 2


# ─── End-to-end walk ─────────────────────────────────────────────────────────


def test_end_to_end_walks_every_stage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_workspace(
        tmp_path,
        stages=["ticket", "plan", "implement", "commit", "reflect"],
        compounding=True,
    )
    _stub_git_head(monkeypatch)
    ds.cmd_init(tmp_path, "FT-XYZ")
    visited: list[str] = []
    for _ in range(10):
        rc, payload = ds.cmd_next(tmp_path, "FT-XYZ")
        assert rc == 0
        if payload.get("done"):
            break
        visited.append(payload["stage"])
        ds.cmd_finish(tmp_path, "FT-XYZ", payload["stage"], "completed")
    assert visited == ["ticket", "plan", "implement", "commit", "reflect"]


# ─── CLI ─────────────────────────────────────────────────────────────────────


def test_cli_init_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_workspace(tmp_path, stages=["ticket"], compounding=False)
    _stub_git_head(monkeypatch)
    rc = ds.cli_main(["init", "--ticket", "FT-1", "--workspace-root", str(tmp_path)])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ticket"] == "FT-1"


def test_cli_finish_skill_output_invalid_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_workspace(tmp_path, stages=["ticket"], compounding=False)
    _stub_git_head(monkeypatch)
    ds.cmd_init(tmp_path, "FT-1")
    ds.cmd_next(tmp_path, "FT-1")
    rc = ds.cli_main(
        [
            "finish",
            "--ticket",
            "FT-1",
            "--workspace-root",
            str(tmp_path),
            "--stage",
            "ticket",
            "--status",
            "completed",
            "--skill-output",
            "{not json",
        ]
    )
    assert rc == 1
    assert "not JSON" in capsys.readouterr().err


# ─── Phase 7-full: lease (mutex) + canonical snapshot ────────────────────────


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _identity() -> tuple[str, str]:
    return lease.boot_id(), socket.gethostname()


def test_init_acquires_lease(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_workspace(tmp_path)
    _stub_git_head(monkeypatch)
    rc, payload = ds.cmd_init(tmp_path, "FT-1")
    assert rc == 0
    td = tmp_path / ".flow" / "runs" / "FT-1"
    assert (td / "run.lock").exists()
    held = lease.read_lease(td)
    assert held is not None
    assert held.run_id == payload["run_id"]


def test_init_writes_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_workspace(tmp_path)
    _stub_git_head(monkeypatch)
    rc, _ = ds.cmd_init(tmp_path, "FT-1")
    assert rc == 0
    td = tmp_path / ".flow" / "runs" / "FT-1"
    assert (td / "snapshot.json").exists()
    assert (td / "snapshot.sha").exists()


def test_init_refuses_foreign_live_lease(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_workspace(tmp_path)
    _stub_git_head(monkeypatch)
    td = tmp_path / ".flow" / "runs" / "FT-1"
    boot, host = _identity()
    lease.acquire(td, "other-run", 600, _now_iso(), current_boot=boot, hostname=host, cwd=str(td))
    rc, payload = ds.cmd_init(tmp_path, "FT-1")
    assert rc == 1
    assert payload["holder"]["run_id"] == "other-run"
    assert "recover --takeover" in payload["hint"]


def test_init_stale_foreign_lease_returns_5(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_workspace(tmp_path)
    _stub_git_head(monkeypatch)
    td = tmp_path / ".flow" / "runs" / "FT-1"
    boot, host = _identity()
    # expired foreign lease with the current boot id -> not reboot-clearable.
    lease.acquire(
        td, "old-run", 1, "2020-01-01T00:00:00Z", current_boot=boot, hostname=host, cwd=str(td)
    )
    rc, payload = ds.cmd_init(tmp_path, "FT-1")
    assert rc == 5
    assert payload["holder"]["run_id"] == "old-run"


def test_next_refuses_on_snapshot_drift(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_workspace(tmp_path)
    _stub_git_head(monkeypatch)
    ds.cmd_init(tmp_path, "FT-1")
    wt = tmp_path / ".flow" / "workspace.toml"
    wt.write_text(wt.read_text(encoding="utf-8") + "\n# drift\n", encoding="utf-8")
    rc, payload = ds.cmd_next(tmp_path, "FT-1")
    assert rc == 1
    assert "drift" in payload["error"]


def test_next_lost_lease_returns_7(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_workspace(tmp_path)
    _stub_git_head(monkeypatch)
    ds.cmd_init(tmp_path, "FT-1")
    lock = tmp_path / ".flow" / "runs" / "FT-1" / "run.lock"
    data = json.loads(lock.read_text(encoding="utf-8"))
    data["run_id"] = "someone-else"
    lock.write_text(json.dumps(data), encoding="utf-8")
    rc, payload = ds.cmd_next(tmp_path, "FT-1")
    assert rc == 7
    assert payload["error"] == "lost lease"


def test_finish_releases_lease_on_terminal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_workspace(tmp_path, stages=["ticket"], handlers={"ticket": "inline"}, compounding=False)
    _stub_git_head(monkeypatch)
    ds.cmd_init(tmp_path, "FT-1")
    ds.cmd_next(tmp_path, "FT-1")
    rc, payload = ds.cmd_finish(tmp_path, "FT-1", "ticket", "completed")
    assert rc == 0
    assert payload["next_pending"] is None
    assert not (tmp_path / ".flow" / "runs" / "FT-1" / "run.lock").exists()


def test_release_subcommand_removes_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_workspace(tmp_path)
    _stub_git_head(monkeypatch)
    ds.cmd_init(tmp_path, "FT-1")
    td = tmp_path / ".flow" / "runs" / "FT-1"
    assert (td / "run.lock").exists()
    rc, payload = ds.cmd_release(tmp_path, "FT-1")
    assert rc == 0
    assert payload["released"] is True
    assert not (td / "run.lock").exists()


def test_full_loop_init_to_done_releases_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # End-to-end dispatcher drive: init -> (next -> finish)* -> done -> lease gone.
    # Exercises lease acquire/refresh/assert/release + snapshot write/verify + state
    # transitions interacting across the whole sequence (no tracker/subagents).
    _write_workspace(
        tmp_path,
        stages=["ticket", "plan", "commit"],
        handlers={"ticket": "inline", "plan": "none", "commit": "inline"},
        compounding=False,
    )
    _stub_git_head(monkeypatch)
    td = tmp_path / ".flow" / "runs" / "FT-1"

    rc, _ = ds.cmd_init(tmp_path, "FT-1")
    assert rc == 0
    assert (td / "run.lock").exists()

    seen: list[str] = []
    guard = 0
    while True:
        guard += 1
        assert guard < 20, "dispatcher loop did not terminate"
        rc, nxt = ds.cmd_next(tmp_path, "FT-1")
        assert rc == 0, nxt
        if nxt.get("done"):
            break
        stage = nxt["stage"]
        seen.append(stage)
        rc, fin = ds.cmd_finish(tmp_path, "FT-1", stage, "completed")
        assert rc == 0, fin

    assert seen == ["ticket", "plan", "commit"]
    ts, _ = state.read(td)
    assert ts is not None
    assert all(r.status == "completed" for r in ts.stages.values())
    # lease released on terminal completion
    assert not (td / "run.lock").exists()


def test_finish_cli_flag_contract_matches_skill_prose() -> None:
    # Guards the prose<->CLI seam: SKILL.md's do-loop finish call must parse.
    # head_sha is derived internally by cmd_finish, NOT a flag — prose passing
    # --head-sha would die "unrecognized arguments" (a bug the unit tests, which
    # call cmd_finish directly, cannot see).
    args = ds._parse_args(
        [
            "finish",
            "--workspace-root",
            ".",
            "--ticket",
            "FT-1",
            "--stage",
            "commit",
            "--status",
            "completed",
            "--output-path",
            "x.out",
        ]
    )
    assert args.cmd == "finish"
    assert args.status_value == "completed"
    with pytest.raises(SystemExit):
        ds._parse_args(
            [
                "finish",
                "--workspace-root",
                ".",
                "--ticket",
                "FT-1",
                "--stage",
                "commit",
                "--status",
                "completed",
                "--head-sha",
                "deadbeef",
            ]
        )
