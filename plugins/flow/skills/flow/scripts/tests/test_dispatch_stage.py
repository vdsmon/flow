"""Contract tests for dispatch_stage.py.

Covers init/next/finish/status lifecycle, blocked_by surfacing, handler-type
routing JSON, and validate-workspace HARD GATE. git rev-parse HEAD is stubbed
via monkeypatch.setattr(subprocess, "run", ...). No real git repo needed.
"""

from __future__ import annotations

import hashlib
import json
import socket
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import agent_routes
import cognitive_workers as cw
import dispatch_stage as ds
import lease
import snapshot
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
        handlers = dict.fromkeys(stages, "inline")

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
    # Second init that resumes a LIVE lease must present the session_nonce the
    # first init minted (the same session re-entering): same run_id, completed
    # stage stays completed (no replay of a finished commit stage).
    _write_workspace(tmp_path, stages=["ticket", "plan"], compounding=False)
    _stub_git_head(monkeypatch)
    rc, first = ds.cmd_init(tmp_path, "FT-1")
    assert rc == 0
    assert first["resumed"] is False
    assert first["session_nonce"]
    nonce = first["session_nonce"]
    ds.cmd_next(tmp_path, "FT-1", nonce)
    ds.cmd_finish(tmp_path, "FT-1", "ticket", "completed", session_nonce=nonce)

    rc, second = ds.cmd_init(tmp_path, "FT-1", session_nonce=nonce)
    assert rc == 0
    assert second["resumed"] is True
    assert second["run_id"] == first["run_id"]

    state_path = tmp_path / ".flow" / "runs" / "FT-1" / "state.json"
    state_data = json.loads(state_path.read_text(encoding="utf-8"))
    assert state_data["stages"]["ticket"]["status"] == "completed"


def test_init_resumes_bak_recovered_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # flow-k6l6: a completed run releases its lease, its state.json later
    # corrupts on disk, and an operator runs `FLOW FT-1` (the "none present"
    # re-init path: no nonce, no force). state.read quarantines the corrupt file
    # and restores the newest .bak (exit 1). cmd_init MUST resume that recovered
    # run, NOT mint a fresh run_id + state.init wipe it to all-pending (which
    # would replay a shipped ticket = duplicate branch/PR).
    _write_workspace(tmp_path, stages=["ticket", "plan"], compounding=False)
    _stub_git_head(monkeypatch)
    rc, first = ds.cmd_init(tmp_path, "FT-1")
    assert rc == 0
    nonce = first["session_nonce"]
    ds.cmd_next(tmp_path, "FT-1", nonce)
    ds.cmd_finish(tmp_path, "FT-1", "ticket", "completed", session_nonce=nonce)

    td = tmp_path / ".flow" / "runs" / "FT-1"
    state_path = td / "state.json"
    # Snapshot the good state into a GUARANTEED-NEWEST .bak (the bak glob sorts
    # by name reverse; real tokens are like 20260612T...Z, so a far-future token
    # is deterministically picked first).
    good = state_path.read_text(encoding="utf-8")
    (td / "state.json.99999999T999999Z.bak").write_text(good, encoding="utf-8")

    # Release the lease, then corrupt state.json (the "completed run, lease
    # released" path the ticket describes).
    ds.cmd_release(tmp_path, "FT-1", nonce)
    state_path.write_text("{ this is not valid json ]", encoding="utf-8")

    # Re-init with NO nonce and no force (a fresh `FLOW FT-1`).
    rc2, payload = ds.cmd_init(tmp_path, "FT-1")
    assert rc2 == 0
    assert payload["resumed"] is True
    assert payload["run_id"] == first["run_id"]
    assert payload.get("state_recovered_from_backup") is True

    # The on-disk state was healed and the resumed run kept its progress: NOT
    # wiped to a fresh all-pending run.
    healed = json.loads(state_path.read_text(encoding="utf-8"))
    assert healed["stages"]["ticket"]["status"] == "completed"
    assert healed["run_id"] == first["run_id"]


def _corrupt_state_and_baks(td: Path) -> None:
    """Make state.json unrecoverable: corrupt it AND every rotated .bak."""
    for bak in td.glob("state.json.*.bak"):
        bak.write_text("{ corrupt bak ]", encoding="utf-8")
    (td / "state.json").write_text("{ this is not valid json ]", encoding="utf-8")


def test_init_refuses_unrecoverable_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # flow-k6l6, exit-2 flavor: a completed run releases its lease, state.json
    # corrupts AND no .bak parses (state.read returns (None, 2)). cmd_init must
    # fail closed like cmd_next/cmd_finish/cmd_status, NOT mint a fresh
    # all-pending run that replays the shipped ticket.
    _write_workspace(tmp_path, stages=["ticket", "plan"], compounding=False)
    _stub_git_head(monkeypatch)
    rc, first = ds.cmd_init(tmp_path, "FT-1")
    assert rc == 0
    nonce = first["session_nonce"]
    ds.cmd_next(tmp_path, "FT-1", nonce)
    ds.cmd_finish(tmp_path, "FT-1", "ticket", "completed", session_nonce=nonce)

    td = tmp_path / ".flow" / "runs" / "FT-1"
    ds.cmd_release(tmp_path, "FT-1", nonce)
    assert list(td.glob("state.json.*.bak"))
    _corrupt_state_and_baks(td)

    rc2, payload = ds.cmd_init(tmp_path, "FT-1")
    assert rc2 == 1
    assert payload["error"] == f"unrecoverable state.json at {td}"
    assert payload["hint"] == "FLOW workspace repair FT-1"
    # fail closed: no fresh run minted, no lease acquired; the corrupt file was
    # quarantined by the read (forensics for FLOW workspace repair).
    assert not (td / "state.json").exists()
    assert not (td / "run.lock").exists()
    assert list(td.glob("state.json.quarantine.*"))


def test_init_force_replaces_unrecoverable_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # --force stays the operator-explicit reset over an unrecoverable state: a
    # fresh all-pending run is minted and the payload carries the marker.
    _write_workspace(tmp_path, stages=["ticket", "plan"], compounding=False)
    _stub_git_head(monkeypatch)
    rc, first = ds.cmd_init(tmp_path, "FT-1")
    assert rc == 0
    nonce = first["session_nonce"]
    ds.cmd_next(tmp_path, "FT-1", nonce)
    ds.cmd_finish(tmp_path, "FT-1", "ticket", "completed", session_nonce=nonce)

    td = tmp_path / ".flow" / "runs" / "FT-1"
    ds.cmd_release(tmp_path, "FT-1", nonce)
    _corrupt_state_and_baks(td)

    rc2, payload = ds.cmd_init(tmp_path, "FT-1", force=True)
    assert rc2 == 0
    assert payload["resumed"] is False
    assert payload["run_id"] != first["run_id"]
    assert payload["state_unrecoverable_replaced"] is True
    state_data = json.loads((td / "state.json").read_text(encoding="utf-8"))
    assert state_data["stages"]["ticket"]["status"] == "pending"


def test_init_second_run_on_live_lease_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # flow-8i6l: a second delivery reuses run_id from state.json but cannot present
    # the live owner's nonce, so its init must be blocked rather than silently
    # re-acquiring the live lease.
    _write_workspace(tmp_path, stages=["ticket", "plan"], compounding=False)
    _stub_git_head(monkeypatch)
    rc, first = ds.cmd_init(tmp_path, "FT-1")
    assert rc == 0

    rc, blocked = ds.cmd_init(tmp_path, "FT-1")  # no nonce: a fresh session
    assert rc == 1
    assert blocked["error"] == "ticket locked by another live run"
    assert blocked["holder"]["run_id"] == first["run_id"]


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
    _, first = ds.cmd_init(tmp_path, "FT-1")

    calls: list[str] = []

    def fake_promote(workspace_root: Path, **kwargs: Any) -> list[dict[str, Any]]:
        del workspace_root
        calls.append(kwargs["ticket"])
        return []

    monkeypatch.setattr(ds.recall_pending, "promote_matching", fake_promote)
    rc, payload = ds.cmd_init(tmp_path, "FT-1", session_nonce=first["session_nonce"])
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
    ds.cmd_next(tmp_path, "FT-1")
    ds.cmd_finish(tmp_path, "FT-1", "ticket", "completed")
    rc, payload = ds.cmd_next(tmp_path, "FT-1")
    assert rc == 0
    assert payload["stage"] == "implement"
    # implement carries the diff-baseline pre-hook and explicit route marker.
    assert payload["roles"] == ["records_diff_baseline", "agent_routed"]


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


def test_finish_rejects_missing_output_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_workspace(tmp_path, stages=["ticket"], compounding=False)
    _stub_git_head(monkeypatch)
    ds.cmd_init(tmp_path, "FT-1")
    ds.cmd_next(tmp_path, "FT-1")
    missing = tmp_path / ".flow" / "runs" / "FT-1" / "stages" / "ticket.out"
    rc, payload = ds.cmd_finish(tmp_path, "FT-1", "ticket", "completed", output_path=str(missing))
    assert rc == 1
    assert str(missing) in payload["error"]
    state_path = tmp_path / ".flow" / "runs" / "FT-1" / "state.json"
    state_data = json.loads(state_path.read_text(encoding="utf-8"))
    assert state_data["stages"]["ticket"]["status"] == "in_progress"
    assert state_data["stages"]["ticket"]["output_path"] is None


def test_finish_records_existing_output_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_workspace(tmp_path, stages=["ticket"], compounding=False)
    _stub_git_head(monkeypatch)
    ds.cmd_init(tmp_path, "FT-1")
    ds.cmd_next(tmp_path, "FT-1")
    out = tmp_path / ".flow" / "runs" / "FT-1" / "stages" / "ticket.out"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("report", encoding="utf-8")
    rc, _ = ds.cmd_finish(tmp_path, "FT-1", "ticket", "completed", output_path=str(out))
    assert rc == 0
    state_path = tmp_path / ".flow" / "runs" / "FT-1" / "state.json"
    state_data = json.loads(state_path.read_text(encoding="utf-8"))
    assert state_data["stages"]["ticket"]["output_path"] == str(out)


def test_advance_missing_output_path_does_not_advance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_workspace(tmp_path, stages=["ticket", "plan"], compounding=False)
    _stub_git_head(monkeypatch)
    ds.cmd_init(tmp_path, "FT-1")
    ds.cmd_next(tmp_path, "FT-1")
    out = tmp_path / ".flow" / "runs" / "FT-1" / "stages" / "ticket.out"
    rc, payload = ds.cmd_advance(tmp_path, "FT-1", "ticket", "completed", output_path=str(out))
    assert rc == 1
    assert "finished" not in payload
    assert "stage" not in payload
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("report", encoding="utf-8")
    rc, payload = ds.cmd_advance(tmp_path, "FT-1", "ticket", "completed", output_path=str(out))
    assert rc == 0
    assert payload["finished"] == {"stage": "ticket", "status": "completed"}
    assert payload["stage"] == "plan"


def test_finish_output_path_relative_resolves_against_workspace_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_workspace(tmp_path, stages=["ticket", "plan"], compounding=False)
    _stub_git_head(monkeypatch)
    ds.cmd_init(tmp_path, "FT-1")
    ds.cmd_next(tmp_path, "FT-1")
    rel = ".flow/runs/FT-1/stages/ticket.out"
    rc, _ = ds.cmd_finish(tmp_path, "FT-1", "ticket", "completed", output_path=rel)
    assert rc == 1
    out = tmp_path / rel
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("report", encoding="utf-8")
    rc, _ = ds.cmd_finish(tmp_path, "FT-1", "ticket", "completed", output_path=rel)
    assert rc == 0
    state_path = tmp_path / ".flow" / "runs" / "FT-1" / "state.json"
    state_data = json.loads(state_path.read_text(encoding="utf-8"))
    assert state_data["stages"]["ticket"]["output_path"] == rel


def test_finish_rejects_output_path_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_workspace(tmp_path, stages=["ticket"], compounding=False)
    _stub_git_head(monkeypatch)
    ds.cmd_init(tmp_path, "FT-1")
    ds.cmd_next(tmp_path, "FT-1")
    d = tmp_path / ".flow" / "runs" / "FT-1" / "stages"
    d.mkdir(parents=True, exist_ok=True)
    rc, payload = ds.cmd_finish(tmp_path, "FT-1", "ticket", "completed", output_path=str(d))
    assert rc == 1
    assert str(d) in payload["error"]


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


def test_release_on_missing_flow_creates_no_tree(tmp_path: Path) -> None:
    # drifted-cwd reproduction: release/status against a workspace whose .flow
    # does not exist must not materialize a phantom .flow tree + state.json.lock.
    rc, _ = ds.cmd_release(tmp_path, "FT-1")
    assert rc == 0
    assert not (tmp_path / ".flow").exists()


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


def test_cli_advance_skill_output_invalid_json(
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
            "advance",
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


def test_cli_advance_persists_skill_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_workspace(
        tmp_path,
        handlers={"ticket": "skill:ship-it:create"},
        stages=["ticket"],
        compounding=False,
    )
    _stub_git_head(monkeypatch)
    ds.cmd_init(tmp_path, "FT-1")
    ds.cmd_next(tmp_path, "FT-1")
    rc = ds.cli_main(
        [
            "advance",
            "--ticket",
            "FT-1",
            "--workspace-root",
            str(tmp_path),
            "--stage",
            "ticket",
            "--status",
            "completed",
            "--skill-output",
            json.dumps({"pr_url": "https://x/1"}),
        ]
    )
    assert rc == 0
    state_path = tmp_path / ".flow" / "runs" / "FT-1" / "state.json"
    state_data = json.loads(state_path.read_text())
    assert state_data["stages"]["ticket"]["skill_output"] == {"pr_url": "https://x/1"}


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


def _boom_write(*args: Any, **kwargs: Any) -> Any:
    del args, kwargs
    raise OSError("disk full")


def test_init_snapshot_write_failure_fail_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # fresh init, no pre-existing sha: write_snapshot raises -> drift guard OFF.
    _write_workspace(tmp_path)
    _stub_git_head(monkeypatch)
    monkeypatch.setattr(ds, "write_snapshot", _boom_write)
    rc, payload = ds.cmd_init(tmp_path, "FT-1")
    assert rc == 0
    assert payload["snapshot_write_failed"] is True
    assert payload["snapshot_guard_active"] is False
    sha_path = tmp_path / ".flow" / "runs" / "FT-1" / "snapshot.sha"
    assert not sha_path.exists()
    err = capsys.readouterr().err
    assert "fail-open" in err
    assert "FLOW workspace repair FT-1" in err


def test_init_resume_skips_snapshot_write_preserving_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # flow-qwf3: a resume with an existing snapshot does NOT re-baseline. The write
    # is skipped entirely (not merely retried-on-failure), so the original sha
    # survives and the drift guard stays armed. A spy proves write_snapshot is not
    # called on the resume path. (Replaces the old fail-closed test: a write
    # failure on resume is now unreachable because the write itself is skipped.)
    _write_workspace(tmp_path)
    _stub_git_head(monkeypatch)
    rc, first = ds.cmd_init(tmp_path, "FT-1")
    assert rc == 0
    sha_path = tmp_path / ".flow" / "runs" / "FT-1" / "snapshot.sha"
    assert sha_path.exists()
    sha_before = sha_path.read_bytes()
    capsys.readouterr()

    calls = {"n": 0}
    real_write = ds.write_snapshot

    def counting_write(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        return real_write(*args, **kwargs)

    monkeypatch.setattr(ds, "write_snapshot", counting_write)
    rc, payload = ds.cmd_init(tmp_path, "FT-1", session_nonce=first["session_nonce"])
    assert rc == 0
    assert payload["resumed"] is True
    assert calls["n"] == 0  # resume preserved S0 without recomputing it
    assert "snapshot_write_failed" not in payload
    assert sha_path.read_bytes() == sha_before
    assert capsys.readouterr().err == ""


def test_init_resume_preserves_snapshot_does_not_launder_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # flow-qwf3: a resume must NOT re-baseline the canonical snapshot. The original
    # S0 is the run's TOCTOU baseline; recomputing it on resume would launder
    # unowned drift that landed while the run was suspended (a swapped engine, a
    # rewritten workspace.toml), silently defeating the next-stage drift guard.
    _write_workspace(tmp_path)
    _stub_git_head(monkeypatch)
    rc, first = ds.cmd_init(tmp_path, "FT-1")
    assert rc == 0
    sha_path = tmp_path / ".flow" / "runs" / "FT-1" / "snapshot.sha"
    sha_before = sha_path.read_bytes()

    # unowned drift lands while suspended: no baseline.json -> empty planned set,
    # so the owned-reconcile path cannot absorb it.
    wt = tmp_path / ".flow" / "workspace.toml"
    wt.write_text(wt.read_text(encoding="utf-8") + "\n# drift\n", encoding="utf-8")

    rc, second = ds.cmd_init(tmp_path, "FT-1", session_nonce=first["session_nonce"])
    assert rc == 0
    assert second["resumed"] is True
    # snapshot preserved: not re-baselined to the drifted content.
    assert sha_path.read_bytes() == sha_before
    # so the next call still catches the unowned drift and aborts.
    rc, payload = ds.cmd_next(tmp_path, "FT-1", second["session_nonce"])
    assert rc == 1
    assert "drift" in payload["error"]


def test_init_exits_zero_on_snapshot_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the broad catch was not hardened into a block: a write failure still exits 0.
    _write_workspace(tmp_path)
    _stub_git_head(monkeypatch)
    monkeypatch.setattr(ds, "write_snapshot", _boom_write)
    rc, _ = ds.cmd_init(tmp_path, "FT-1")
    assert rc == 0


def test_init_snapshot_success_emits_no_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # happy path stays byte-identical: no marker keys, no stderr.
    _write_workspace(tmp_path)
    _stub_git_head(monkeypatch)
    rc, payload = ds.cmd_init(tmp_path, "FT-1")
    assert rc == 0
    assert "snapshot_write_failed" not in payload
    assert "snapshot_guard_active" not in payload
    assert capsys.readouterr().err == ""


def test_init_refuses_foreign_live_lease(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_workspace(tmp_path)
    _stub_git_head(monkeypatch)
    td = tmp_path / ".flow" / "runs" / "FT-1"
    boot, host = _identity()
    lease.acquire(td, "other-run", 600, _now_iso(), current_boot=boot, hostname=host, cwd=str(td))
    rc, payload = ds.cmd_init(tmp_path, "FT-1")
    assert rc == 1
    assert payload["holder"]["run_id"] == "other-run"
    assert payload["hint"] == "FLOW workspace repair FT-1"


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


def test_cli_init_stale_foreign_lease_exits_5(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # recovery prose routes on the process exit code: 5 must survive cli_main.
    _write_workspace(tmp_path)
    _stub_git_head(monkeypatch)
    td = tmp_path / ".flow" / "runs" / "FT-1"
    boot, host = _identity()
    lease.acquire(
        td, "old-run", 1, "2020-01-01T00:00:00Z", current_boot=boot, hostname=host, cwd=str(td)
    )
    rc = ds.cli_main(["init", "--ticket", "FT-1", "--workspace-root", str(tmp_path)])
    assert rc == 5
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["holder"]["run_id"] == "old-run"
    assert payload["hint"] == "FLOW workspace repair FT-1"
    assert "stale lease" in captured.err


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


def test_next_refresh_lease_lost_returns_7(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # the refresh-time LeaseLost branch is shadowed by _guard_lease_ownership for
    # any file-forgeable condition; reach it by failing refresh itself.
    _write_workspace(tmp_path, stages=["ticket"], compounding=False)
    _stub_git_head(monkeypatch)
    ds.cmd_init(tmp_path, "FT-1")

    def lost(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise lease.LeaseLost("taken over mid-refresh")

    monkeypatch.setattr(ds.lease, "refresh", lost)
    rc, payload = ds.cmd_next(tmp_path, "FT-1")
    assert rc == lease.EXIT_LEASE_LOST
    assert payload["error"] == "lost lease"
    assert payload["detail"] == "taken over mid-refresh"
    # refresh guard fires before begin_stage: ticket stays pending.
    state_path = tmp_path / ".flow" / "runs" / "FT-1" / "state.json"
    data = json.loads(state_path.read_text(encoding="utf-8"))
    assert data["stages"]["ticket"]["status"] == "pending"


def test_next_probes_boot_id_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # boot_id spawns a sysctl subprocess on macOS; cmd_next passes its probe
    # into the lease guard instead of probing a second time.
    _write_workspace(tmp_path, stages=["ticket"], compounding=False)
    _stub_git_head(monkeypatch)
    ds.cmd_init(tmp_path, "FT-1")

    calls = {"n": 0}
    real_boot = lease.boot_id

    def counting(*args: Any, **kwargs: Any) -> str:
        calls["n"] += 1
        return real_boot(*args, **kwargs)

    monkeypatch.setattr(ds.lease, "boot_id", counting)
    rc, _ = ds.cmd_next(tmp_path, "FT-1")
    assert rc == 0
    assert calls["n"] == 1


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


def test_advance_cli_flag_contract_matches_skill_prose() -> None:
    # Guards the prose<->CLI seam: SKILL.md's do-loop advance call must parse.
    # head_sha is derived internally by cmd_finish, NOT a flag: prose passing
    # --head-sha would die "unrecognized arguments" (a bug the unit tests, which
    # call cmd_advance directly, cannot see).
    args = ds._parse_args(
        [
            "advance",
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
    assert args.cmd == "advance"
    assert args.status_value == "completed"
    with pytest.raises(SystemExit):
        ds._parse_args(
            [
                "advance",
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


# ─── owned workspace.toml drift auto-reconcile (flow-u3s) ──────────────────────


def _write_baseline(tmp_path: Path, ticket: str, planned_files: list[str]) -> None:
    bpath = tmp_path / ".flow" / "runs" / ticket / "baseline.json"
    bpath.parent.mkdir(parents=True, exist_ok=True)
    bpath.write_text(
        json.dumps({"head_sha": "x", "planned_files": planned_files, "blobs": {}}),
        encoding="utf-8",
    )


def test_next_auto_reconciles_owned_workspace_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_workspace(tmp_path, stages=["ticket", "plan"], compounding=False)
    _stub_git_head(monkeypatch)
    ds.cmd_init(tmp_path, "FT-1")
    _write_baseline(tmp_path, "FT-1", [".flow/workspace.toml"])
    wt = tmp_path / ".flow" / "workspace.toml"
    wt.write_text(wt.read_text(encoding="utf-8") + "\n# owned edit\n", encoding="utf-8")

    sha_path = tmp_path / ".flow" / "runs" / "FT-1" / "snapshot.sha"
    sha_before = sha_path.read_text(encoding="utf-8")

    rc, payload = ds.cmd_next(tmp_path, "FT-1")
    assert rc == 0, payload
    assert payload.get("stage") == "ticket"
    assert payload.get("reconciled_drift") == "workspace_toml"
    # snapshot refreshed to the new baseline
    assert sha_path.read_text(encoding="utf-8") != sha_before

    # a second next finds no residual drift and does not re-reconcile
    ds.cmd_finish(tmp_path, "FT-1", "ticket", "completed")
    rc2, payload2 = ds.cmd_next(tmp_path, "FT-1")
    assert rc2 == 0, payload2
    assert "reconciled_drift" not in payload2


def test_next_owned_reconcile_computes_snapshot_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the reconcile write reuses classify_drift's snapshot instead of recomputing.
    _write_workspace(tmp_path, stages=["ticket", "plan"], compounding=False)
    _stub_git_head(monkeypatch)
    ds.cmd_init(tmp_path, "FT-1")
    _write_baseline(tmp_path, "FT-1", [".flow/workspace.toml"])
    wt = tmp_path / ".flow" / "workspace.toml"
    wt.write_text(wt.read_text(encoding="utf-8") + "\n# owned edit\n", encoding="utf-8")

    calls = {"n": 0}
    real_compute = snapshot.compute_snapshot

    def counting(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        return real_compute(*args, **kwargs)

    monkeypatch.setattr(snapshot, "compute_snapshot", counting)
    rc, payload = ds.cmd_next(tmp_path, "FT-1")
    assert rc == 0, payload
    assert payload.get("reconciled_drift") == "workspace_toml"
    assert calls["n"] == 1

    # the reused snapshot must verify clean afterwards
    ok, detail = snapshot.verify_snapshot(tmp_path, "FT-1", skill_root=ds._skill_root_from_script())
    assert ok is True
    assert detail == "match"


def test_next_refuses_unowned_workspace_drift_without_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # no baseline.json → planned set empty → owned reconcile MUST NOT fire.
    _write_workspace(tmp_path, stages=["ticket", "plan"], compounding=False)
    _stub_git_head(monkeypatch)
    ds.cmd_init(tmp_path, "FT-1")
    wt = tmp_path / ".flow" / "workspace.toml"
    wt.write_text(wt.read_text(encoding="utf-8") + "\n# edit\n", encoding="utf-8")
    rc, payload = ds.cmd_next(tmp_path, "FT-1")
    assert rc == 1
    assert "drift" in payload["error"]


def test_next_engine_drift_dirty_aborts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # flow-p9sc GUARD (replaces test_next_refuses_engine_drift_never_owned): a
    # persistent engine-only drift whose engine working tree is DIRTY
    # (engine_tree_clean False) still fail-closes (rc 1), the raw-Edit-on-
    # machinery threat. An engine-mapped path seeded in planned_files must NOT
    # flip it to a reconcile: component_files(["engine"], ...) -> {"engine":
    # None}, so engine is never OWNED via planned_files (the re-anchor is a
    # distinct cleanliness-gated path, not ownership).
    _write_workspace(tmp_path, stages=["ticket", "plan"], compounding=False)
    _stub_git_head(monkeypatch)
    ds.cmd_init(tmp_path, "FT-1")
    _write_baseline(tmp_path, "FT-1", ["plugins/flow/skills/flow/scripts/dispatch_stage.py"])

    def stub_classify(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        return (False, "drift: engine", ["engine"], {"master_hash": "x"})

    monkeypatch.setattr(ds, "classify_drift", stub_classify)
    monkeypatch.setattr(ds, "engine_tree_clean", lambda *a, **k: False)
    rc, payload = ds.cmd_next(tmp_path, "FT-1")
    assert rc == 1
    assert "reconciled_drift" not in payload
    assert "engine_reanchored" not in payload
    assert "engine" in payload["detail"]


def test_next_engine_drift_transient_race_reverifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # flow-p9sc: a transient concurrent-read race shows engine drift on the
    # first classify pass and clean on the re-verify (second pass). cmd_next
    # proceeds (rc 0) with marker engine_drift_reverified and NO snapshot
    # mutation (the sha file is byte-unchanged).
    _write_workspace(tmp_path, stages=["ticket", "plan"], compounding=False)
    _stub_git_head(monkeypatch)
    ds.cmd_init(tmp_path, "FT-1")

    calls = {"n": 0}

    def stub_classify(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        calls["n"] += 1
        if calls["n"] == 1:
            return (False, "drift: engine", ["engine"], {"master_hash": "x"})
        return (True, "match", [], {"master_hash": "y"})

    monkeypatch.setattr(ds, "classify_drift", stub_classify)

    sha_path = tmp_path / ".flow" / "runs" / "FT-1" / "snapshot.sha"
    sha_before = sha_path.read_bytes()

    rc, payload = ds.cmd_next(tmp_path, "FT-1")
    assert rc == 0, payload
    assert payload.get("stage") == "ticket"
    assert payload.get("engine_drift_reverified") is True
    assert "engine_reanchored" not in payload
    assert sha_path.read_bytes() == sha_before


def test_next_engine_drift_clean_advance_reanchors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # flow-p9sc: a committed lagging-main / marketplace advance leaves the
    # engine working tree clean vs HEAD. Drift is persistent across both
    # classify passes, engine_tree_clean True -> RE-ANCHOR: cmd_next proceeds
    # (rc 0) with marker engine_reanchored and the snapshot.sha is rewritten to
    # the recomputed master_hash.
    _write_workspace(tmp_path, stages=["ticket", "plan"], compounding=False)
    _stub_git_head(monkeypatch)
    ds.cmd_init(tmp_path, "FT-1")

    def stub_classify(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        return (False, "drift: engine", ["engine"], {"master_hash": "reanchored-hash"})

    monkeypatch.setattr(ds, "classify_drift", stub_classify)
    monkeypatch.setattr(ds, "engine_tree_clean", lambda *a, **k: True)

    sha_path = tmp_path / ".flow" / "runs" / "FT-1" / "snapshot.sha"
    sha_before = sha_path.read_bytes()

    rc, payload = ds.cmd_next(tmp_path, "FT-1")
    assert rc == 0, payload
    assert payload.get("stage") == "ticket"
    assert payload.get("engine_reanchored") is True
    assert "reconciled_drift" not in payload
    after = sha_path.read_bytes()
    assert after != sha_before
    assert after == b"reanchored-hash\n"


def test_next_engine_drift_lost_lease_returns_7_without_reanchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # flow-p9sc lease-before-mutation invariant: an engine-only drift abort is
    # DEFERRED past the lease guard, so a lost lease wins (rc 7) and the snapshot
    # is never re-anchored. classify_drift would re-verify clean on the second
    # call, but the lease guard returns first; engine_tree_clean must NOT be
    # consulted and the sha must be byte-unchanged.
    _write_workspace(tmp_path, stages=["ticket", "plan"], compounding=False)
    _stub_git_head(monkeypatch)
    ds.cmd_init(tmp_path, "FT-1")

    def stub_classify(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        return (False, "drift: engine", ["engine"], {"master_hash": "x"})

    def boom_clean(*args: Any, **kwargs: Any) -> bool:
        raise AssertionError("engine_tree_clean must not run before the lease guard")

    monkeypatch.setattr(ds, "classify_drift", stub_classify)
    monkeypatch.setattr(ds, "engine_tree_clean", boom_clean)

    lock = tmp_path / ".flow" / "runs" / "FT-1" / "run.lock"
    data = json.loads(lock.read_text(encoding="utf-8"))
    data["run_id"] = "someone-else"
    lock.write_text(json.dumps(data), encoding="utf-8")

    sha_path = tmp_path / ".flow" / "runs" / "FT-1" / "snapshot.sha"
    sha_before = sha_path.read_bytes()

    rc, payload = ds.cmd_next(tmp_path, "FT-1")
    assert rc == 7
    assert payload["error"] == "lost lease"
    assert "engine_reanchored" not in payload
    assert sha_path.read_bytes() == sha_before


def test_next_mixed_engine_drift_aborts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # flow-p9sc: a mixed drift (engine + a second component) is NOT engine-only,
    # so the re-verify branch is never entered and engine_tree_clean must NOT be
    # consulted. Abort rc 1 exactly as today.
    _write_workspace(tmp_path, stages=["ticket", "plan"], compounding=False)
    _stub_git_head(monkeypatch)
    ds.cmd_init(tmp_path, "FT-1")

    def stub_classify(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        return (
            False,
            "drift: engine, workspace_toml",
            ["engine", "workspace_toml"],
            {"master_hash": "x"},
        )

    def boom_clean(*args: Any, **kwargs: Any) -> bool:
        raise AssertionError("engine_tree_clean must not be consulted for mixed drift")

    monkeypatch.setattr(ds, "classify_drift", stub_classify)
    monkeypatch.setattr(ds, "engine_tree_clean", boom_clean)
    rc, payload = ds.cmd_next(tmp_path, "FT-1")
    assert rc == 1
    assert "engine_reanchored" not in payload
    assert "reconciled_drift" not in payload
    assert "engine" in payload["detail"]


def test_next_owned_drift_reload_failure_returns_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_workspace(tmp_path, stages=["ticket", "plan"], compounding=False)
    _stub_git_head(monkeypatch)
    ds.cmd_init(tmp_path, "FT-1")
    _write_baseline(tmp_path, "FT-1", [".flow/workspace.toml"])
    wt = tmp_path / ".flow" / "workspace.toml"
    wt.write_text(wt.read_text(encoding="utf-8") + "\n# owned edit\n", encoding="utf-8")

    def boom(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise OSError("disk full")

    monkeypatch.setattr(ds, "write_snapshot", boom)
    rc, payload = ds.cmd_next(tmp_path, "FT-1")
    assert rc == 1
    # `detail` present keeps the do-loop's exit-1 classification on the drift
    # branch (not run-state-corruption).
    assert payload.get("detail") == "drift: workspace_toml"


def test_next_owned_drift_lost_lease_returns_7_without_reconcile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # owned drift AND a lost lease: the lease guard must win (rc 7) and the
    # snapshot must NOT be refreshed (reconcile deferred past the lease check).
    _write_workspace(tmp_path, stages=["ticket", "plan"], compounding=False)
    _stub_git_head(monkeypatch)
    ds.cmd_init(tmp_path, "FT-1")
    _write_baseline(tmp_path, "FT-1", [".flow/workspace.toml"])
    wt = tmp_path / ".flow" / "workspace.toml"
    wt.write_text(wt.read_text(encoding="utf-8") + "\n# owned edit\n", encoding="utf-8")

    lock = tmp_path / ".flow" / "runs" / "FT-1" / "run.lock"
    data = json.loads(lock.read_text(encoding="utf-8"))
    data["run_id"] = "someone-else"
    lock.write_text(json.dumps(data), encoding="utf-8")

    sha_path = tmp_path / ".flow" / "runs" / "FT-1" / "snapshot.sha"
    sha_before = sha_path.read_bytes()

    rc, payload = ds.cmd_next(tmp_path, "FT-1")
    assert rc == 7
    assert payload["error"] == "lost lease"
    assert sha_path.read_bytes() == sha_before


# ─── owned stage_registry drift auto-reconcile (flow-56s) ──────────────────────


def _redirect_skill_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point dispatch's skill_root under tmp_path with a parseable stage-registry.

    Returns the skill_root. The registry file must exist BEFORE cmd_init so the
    init snapshot baseline includes the stage_registry component; otherwise the
    suppressed write_snapshot would skip and the drift gate would see no
    baseline.
    """
    skill_root = tmp_path / "skill"
    skill_root.mkdir(parents=True, exist_ok=True)
    (skill_root / "stage-registry.toml").write_text(
        '[[stage]]\nname = "create_pr"\ndefault_handler = "none"\n', encoding="utf-8"
    )
    monkeypatch.setattr(ds, "_skill_root_from_script", lambda: skill_root)
    return skill_root


def test_next_auto_reconciles_owned_stage_registry_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill_root = _redirect_skill_root(monkeypatch, tmp_path)
    _write_workspace(tmp_path, stages=["ticket", "plan"], compounding=False)
    _stub_git_head(monkeypatch)
    ds.cmd_init(tmp_path, "FT-1")
    _write_baseline(tmp_path, "FT-1", ["skill/stage-registry.toml"])
    reg = skill_root / "stage-registry.toml"
    reg.write_text(reg.read_text(encoding="utf-8") + "\n# owned edit\n", encoding="utf-8")

    sha_path = tmp_path / ".flow" / "runs" / "FT-1" / "snapshot.sha"
    sha_before = sha_path.read_text(encoding="utf-8")

    rc, payload = ds.cmd_next(tmp_path, "FT-1")
    assert rc == 0, payload
    assert payload.get("reconciled_drift") == "stage_registry"
    assert sha_path.read_text(encoding="utf-8") != sha_before

    ds.cmd_finish(tmp_path, "FT-1", "ticket", "completed")
    rc2, payload2 = ds.cmd_next(tmp_path, "FT-1")
    assert rc2 == 0, payload2
    assert "reconciled_drift" not in payload2


def test_next_auto_reconciles_owned_co_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill_root = _redirect_skill_root(monkeypatch, tmp_path)
    _write_workspace(tmp_path, stages=["ticket", "plan"], compounding=False)
    _stub_git_head(monkeypatch)
    ds.cmd_init(tmp_path, "FT-1")
    _write_baseline(tmp_path, "FT-1", [".flow/workspace.toml", "skill/stage-registry.toml"])
    wt = tmp_path / ".flow" / "workspace.toml"
    wt.write_text(wt.read_text(encoding="utf-8") + "\n# owned edit\n", encoding="utf-8")
    reg = skill_root / "stage-registry.toml"
    reg.write_text(reg.read_text(encoding="utf-8") + "\n# owned edit\n", encoding="utf-8")

    rc, payload = ds.cmd_next(tmp_path, "FT-1")
    assert rc == 0, payload
    assert payload.get("reconciled_drift") == "workspace_toml, stage_registry"


def test_next_refuses_unowned_stage_registry_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # stage-registry.toml drifts but is NOT in planned_files → halt at exit 1.
    skill_root = _redirect_skill_root(monkeypatch, tmp_path)
    _write_workspace(tmp_path, stages=["ticket", "plan"], compounding=False)
    _stub_git_head(monkeypatch)
    ds.cmd_init(tmp_path, "FT-1")
    _write_baseline(tmp_path, "FT-1", [".flow/workspace.toml"])
    reg = skill_root / "stage-registry.toml"
    reg.write_text(reg.read_text(encoding="utf-8") + "\n# foreign edit\n", encoding="utf-8")

    rc, payload = ds.cmd_next(tmp_path, "FT-1")
    assert rc == 1
    assert "drift" in payload["error"]


# ─── corrupt run.lock ────────────────────────────────────────────────────────


def _corrupt_lock(tmp_path: Path, ticket: str = "FT-1") -> Path:
    lock = tmp_path / ".flow" / "runs" / ticket / "run.lock"
    lock.write_text("{not json", encoding="utf-8")
    return lock


def test_init_corrupt_lock_returns_clean_error_no_clear(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_workspace(tmp_path)
    _stub_git_head(monkeypatch)
    ds.cmd_init(tmp_path, "FT-1")
    lock = _corrupt_lock(tmp_path)
    rc, payload = ds.cmd_init(tmp_path, "FT-1")
    assert rc == 1
    assert payload["error"] == "corrupt run.lock"
    assert payload["hint"] == "FLOW workspace repair FT-1"
    # NOT auto-cleared: the corrupt lock survives for human-driven takeover.
    assert lock.exists()
    assert lock.read_text(encoding="utf-8") == "{not json"


def test_next_corrupt_lock_returns_lease_lost_no_advance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_workspace(tmp_path, stages=["ticket"], handlers={"ticket": "inline"}, compounding=False)
    _stub_git_head(monkeypatch)
    ds.cmd_init(tmp_path, "FT-1")
    _corrupt_lock(tmp_path)
    rc, payload = ds.cmd_next(tmp_path, "FT-1")
    assert rc == lease.EXIT_LEASE_LOST
    assert payload["error"] == "corrupt run.lock"
    assert payload["hint"] == "FLOW workspace repair <target>"
    # state did not advance: ticket stage stays pending (never begun).
    state_path = tmp_path / ".flow" / "runs" / "FT-1" / "state.json"
    data = json.loads(state_path.read_text(encoding="utf-8"))
    assert data["stages"]["ticket"]["status"] == "pending"


def test_finish_corrupt_lock_returns_lease_lost_no_advance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_workspace(tmp_path, stages=["ticket"], handlers={"ticket": "inline"}, compounding=False)
    _stub_git_head(monkeypatch)
    ds.cmd_init(tmp_path, "FT-1")
    ds.cmd_next(tmp_path, "FT-1")
    _corrupt_lock(tmp_path)
    rc, payload = ds.cmd_finish(tmp_path, "FT-1", "ticket", "completed")
    assert rc == lease.EXIT_LEASE_LOST
    assert payload["error"] == "corrupt run.lock"
    assert payload["hint"] == "FLOW workspace repair <target>"
    # finish did not run: ticket stays in_progress (begun by cmd_next, not closed).
    state_path = tmp_path / ".flow" / "runs" / "FT-1" / "state.json"
    data = json.loads(state_path.read_text(encoding="utf-8"))
    assert data["stages"]["ticket"]["status"] == "in_progress"


def test_release_corrupt_lock_returns_released_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # SKILL.md step 5 calls release unconditionally on every exit path (incl.
    # the exit-7 corrupt-lock break), so a corrupt run.lock must return a clean
    # released=false, never an uncaught LeaseError traceback. The corrupt lock
    # survives for the human-driven takeover to quarantine.
    _write_workspace(tmp_path, stages=["ticket"], handlers={"ticket": "inline"}, compounding=False)
    _stub_git_head(monkeypatch)
    ds.cmd_init(tmp_path, "FT-1")
    lock = _corrupt_lock(tmp_path)
    rc, payload = ds.cmd_release(tmp_path, "FT-1")
    assert rc == 0
    assert payload["released"] is False
    assert "corrupt run.lock" in payload["detail"]
    assert lock.exists()
    assert lock.read_text(encoding="utf-8") == "{not json"


def test_cli_next_corrupt_lock_exits_7(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # recovery prose routes on the process exit code: 7 must survive cli_main.
    _write_workspace(tmp_path, stages=["ticket"], handlers={"ticket": "inline"}, compounding=False)
    _stub_git_head(monkeypatch)
    ds.cmd_init(tmp_path, "FT-1")
    _corrupt_lock(tmp_path)
    capsys.readouterr()
    rc = ds.cli_main(["next", "--ticket", "FT-1", "--workspace-root", str(tmp_path)])
    assert rc == lease.EXIT_LEASE_LOST
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "corrupt run.lock"


def test_cli_advance_corrupt_lock_exits_7(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_workspace(tmp_path, stages=["ticket"], handlers={"ticket": "inline"}, compounding=False)
    _stub_git_head(monkeypatch)
    ds.cmd_init(tmp_path, "FT-1")
    ds.cmd_next(tmp_path, "FT-1")
    _corrupt_lock(tmp_path)
    capsys.readouterr()
    rc = ds.cli_main(
        [
            "advance",
            "--ticket",
            "FT-1",
            "--workspace-root",
            str(tmp_path),
            "--stage",
            "ticket",
            "--status",
            "completed",
        ]
    )
    assert rc == lease.EXIT_LEASE_LOST
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "corrupt run.lock"
    # finish never ran: ticket stays in_progress.
    state_path = tmp_path / ".flow" / "runs" / "FT-1" / "state.json"
    data = json.loads(state_path.read_text(encoding="utf-8"))
    assert data["stages"]["ticket"]["status"] == "in_progress"


# ─── state rollback marker (flow-6hn2) ───────────────────────────────────────


def _corrupt_state(td: Path) -> None:
    """Overwrite state.json with unparseable bytes, leaving any .bak intact."""
    state._state_path(td).write_text("{ not json", encoding="utf-8")


def test_next_surfaces_recovery_marker_after_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_workspace(tmp_path, stages=["ticket", "plan"], compounding=False)
    _stub_git_head(monkeypatch)
    ds.cmd_init(tmp_path, "FT-1")
    ds.cmd_next(tmp_path, "FT-1")
    ds.cmd_finish(tmp_path, "FT-1", "ticket", "completed")
    td = tmp_path / ".flow" / "runs" / "FT-1"
    assert list(td.glob("state.json.*.bak"))  # a recoverable .bak exists
    _corrupt_state(td)
    rc, payload = ds.cmd_next(tmp_path, "FT-1")
    assert rc == 0
    assert payload["state_recovered_from_backup"] is True


def test_advance_surfaces_recovery_marker_after_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_workspace(tmp_path, stages=["ticket", "plan"], compounding=False)
    _stub_git_head(monkeypatch)
    ds.cmd_init(tmp_path, "FT-1")
    ds.cmd_next(tmp_path, "FT-1")
    ds.cmd_finish(tmp_path, "FT-1", "ticket", "completed")
    td = tmp_path / ".flow" / "runs" / "FT-1"
    assert list(td.glob("state.json.*.bak"))
    _corrupt_state(td)
    _, payload = ds.cmd_advance(tmp_path, "FT-1", "plan", "completed")
    assert payload["state_recovered_from_backup"] is True


def test_finish_surfaces_recovery_marker_after_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_workspace(tmp_path, stages=["ticket", "plan"], compounding=False)
    _stub_git_head(monkeypatch)
    ds.cmd_init(tmp_path, "FT-1")
    ds.cmd_next(tmp_path, "FT-1")
    ds.cmd_finish(tmp_path, "FT-1", "ticket", "completed")
    td = tmp_path / ".flow" / "runs" / "FT-1"
    assert list(td.glob("state.json.*.bak"))
    _corrupt_state(td)
    _, payload = ds.cmd_finish(tmp_path, "FT-1", "plan", "completed")
    assert payload["state_recovered_from_backup"] is True


def test_next_clean_read_has_no_recovery_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_workspace(tmp_path, stages=["ticket", "plan"], compounding=False)
    _stub_git_head(monkeypatch)
    ds.cmd_init(tmp_path, "FT-1")
    ds.cmd_next(tmp_path, "FT-1")
    rc, payload = ds.cmd_advance(tmp_path, "FT-1", "ticket", "completed")
    assert rc == 0
    assert "state_recovered_from_backup" not in payload


# ─── fleet ledger shadow-write (epic flow-8by2.2) ──────────────────────────────


def _make_maintainer(tmp_path: Path) -> None:
    # mark the workspace as a maintainer self-target so register_run is armed.
    wt = tmp_path / ".flow" / "workspace.toml"
    wt.write_text(
        wt.read_text(encoding="utf-8") + "\n[maintainer]\nself_target = true\n",
        encoding="utf-8",
    )


def test_next_shadow_writes_fleet_entry_in_maintainer_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_workspace(tmp_path, stages=["ticket", "plan"], compounding=False)
    _make_maintainer(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "no-home"))
    _stub_git_head(monkeypatch)
    rc, first = ds.cmd_init(tmp_path, "FT-1")
    assert rc == 0
    ds.cmd_next(tmp_path, "FT-1", first["session_nonce"])
    entry_path = tmp_path / ".flow" / "fleet" / "FT-1.json"
    assert entry_path.exists()
    entry = json.loads(entry_path.read_text(encoding="utf-8"))
    assert entry["key"] == "FT-1"
    assert entry["run_id"] == first["run_id"]


def test_next_no_fleet_write_when_not_maintainer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # default workspace has no [maintainer] marker -> register_run is a no-op and
    # cmd_next must still succeed (fail-open: a shadow ledger never breaks dispatch).
    _write_workspace(tmp_path, stages=["ticket", "plan"], compounding=False)
    monkeypatch.setenv("HOME", str(tmp_path / "no-home"))
    _stub_git_head(monkeypatch)
    ds.cmd_init(tmp_path, "FT-1")
    rc, _ = ds.cmd_next(tmp_path, "FT-1")
    assert rc == 0
    assert not (tmp_path / ".flow" / "fleet").exists()


def test_finish_clean_deregisters_fleet_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # epic flow-8by2.3: a cleanly-finished run positively deregisters from the fleet
    # ledger (no 30-min staleness lingering in the reconciled liveness read).
    _write_workspace(tmp_path, stages=["ticket"], compounding=False)
    _make_maintainer(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "no-home"))
    _stub_git_head(monkeypatch)
    rc, first = ds.cmd_init(tmp_path, "FT-1")
    assert rc == 0
    nonce = first["session_nonce"]
    ds.cmd_next(tmp_path, "FT-1", nonce)  # heartbeat registers the fleet entry
    entry = tmp_path / ".flow" / "fleet" / "FT-1.json"
    assert entry.exists()
    # finishing the only stage = clean completion -> lease release + fleet dereg
    ds.cmd_finish(tmp_path, "FT-1", "ticket", "completed", session_nonce=nonce)
    assert not entry.exists()


# ─── lease TTL multiplier (flow-0xex) ──────────────────────────────────────────


def test_stage_ttl_seconds_proportional_to_timeout() -> None:
    # Pure helper, built from the REAL registry so it tracks future stage edits.
    from _registry import registry_by_name

    reg = registry_by_name(ds._skill_root_from_script() / ds._STAGE_REGISTRY_RELATIVE)

    # implement (30min) regressed under the old +300 buffer: 2100 < 38*60.
    assert ds._stage_ttl_seconds(reg["implement"]) == 3600
    assert ds._stage_ttl_seconds(reg["implement"]) > 38 * 60
    # review_loop (60min) pins the upper bound the K=2 choice trades against.
    assert ds._stage_ttl_seconds(reg["review_loop"]) == 7200
    # None meta falls back to the 10min default.
    assert ds._stage_ttl_seconds(None) == 1200

    # proportional-headroom invariant across every registered stage.
    for meta in reg.values():
        ttl = ds._stage_ttl_seconds(meta)
        assert ttl == meta.default_timeout_min * 60 * 2
        assert ttl > meta.default_timeout_min * 60


def test_next_refreshes_lease_with_multiplied_ttl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Wiring pin: cmd_next's refresh at the TTL site runs the multiplier path,
    # not the old +300. implement is 30min: old 30*60+300=2100 < 3000, new 3600.
    _write_workspace(
        tmp_path,
        stages=["ticket", "implement"],
        handlers={"ticket": "inline", "implement": "inline"},
        compounding=False,
    )
    _stub_git_head(monkeypatch)
    td = tmp_path / ".flow" / "runs" / "FT-1"

    rc, _ = ds.cmd_init(tmp_path, "FT-1")
    assert rc == 0
    # advance past ticket so the next landing is implement (the 30min stage).
    rc, _ = ds.cmd_next(tmp_path, "FT-1")
    assert rc == 0
    rc, _ = ds.cmd_finish(tmp_path, "FT-1", "ticket", "completed")
    assert rc == 0
    rc, nxt = ds.cmd_next(tmp_path, "FT-1")
    assert rc == 0, nxt
    assert nxt["stage"] == "implement"

    lse = lease.read_lease(td)
    assert lse is not None
    expires = lease.parse_iso(lse.lease_expires_at)
    assert expires is not None
    remaining = (expires - datetime.now(UTC)).total_seconds()
    assert remaining > 50 * 60, remaining


# ─── cognitive substeps ──────────────────────────────────────────────────────


def _cognitive_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Init a run whose plan stage carries a frozen exact route snapshot."""
    _write_workspace(
        tmp_path,
        handlers={"ticket": "inline", "plan": "inline"},
        stages=["ticket", "plan"],
        compounding=False,
    )
    _stub_git_head(monkeypatch, "a" * 40)
    ds.cmd_init(tmp_path, "FT-1")
    td = tmp_path / ".flow" / "runs" / "FT-1"
    agent_routes.snapshot(tmp_path, "codex", output_path=td / "route-snapshot.json")
    ds.cmd_next(tmp_path, "FT-1")
    ds.cmd_finish(tmp_path, "FT-1", "ticket", "completed")
    return td


def _sealed(td: Path, stage: str = "plan") -> dict[str, Any]:
    ts, _ = state.read(td)
    assert ts is not None
    return ts.stages[stage].cognitive_substeps or {}


def test_next_seals_each_cognitive_substep_to_its_stage_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    td = _cognitive_workspace(tmp_path, monkeypatch)
    rc, payload = ds.cmd_next(tmp_path, "FT-1")
    assert rc == 0
    assert payload["stage"] == "plan"
    assert payload["generation"] == 1

    sealed = payload["cognitive_substeps"]
    assert set(sealed) == {"planning", "assessment"}
    assert sealed["planning"]["profile"] == "planner"
    assert sealed["planning"]["activation"] == "pending"
    assert sealed["planning"]["source_sha"] == "a" * 40
    assert sealed["planning"]["logical_invocation_id"].endswith(":plan:planning:1")
    assert sealed == _sealed(td)
    assert json.loads(Path(payload["descriptor_path"]).read_text(encoding="utf-8")) == payload


def test_stage_cannot_complete_without_a_matching_cognitive_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cognitive_workspace(tmp_path, monkeypatch)
    ds.cmd_next(tmp_path, "FT-1")

    rc, payload = ds.cmd_finish(tmp_path, "FT-1", "plan", "completed")
    assert rc == 1
    assert "no successful outcome" in payload["error"]


def test_stale_generation_outcome_cannot_complete_the_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    td = _cognitive_workspace(tmp_path, monkeypatch)
    ds.cmd_next(tmp_path, "FT-1")
    for facts in _sealed(td).values():
        _publish_outcome(facts, generation=facts["stage_generation"] + 1)

    rc, payload = ds.cmd_finish(tmp_path, "FT-1", "plan", "completed")
    assert rc == 1
    assert "does not match the sealed stage generation" in payload["error"]


def test_matching_outcomes_complete_the_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    td = _cognitive_workspace(tmp_path, monkeypatch)
    ds.cmd_next(tmp_path, "FT-1")
    for facts in _sealed(td).values():
        _publish_outcome(facts)

    rc, payload = ds.cmd_finish(tmp_path, "FT-1", "plan", "completed")
    assert rc == 0, payload
    ts, _ = state.read(td)
    assert ts is not None
    assert ts.stages["plan"].status == "completed"


def test_a_fabricated_outcome_in_the_stage_output_cannot_complete_the_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fence reads the worker's own receipt, never the agent's structured output."""
    td = _cognitive_workspace(tmp_path, monkeypatch)
    ds.cmd_next(tmp_path, "FT-1")
    forged = {name: _outcome(facts) for name, facts in _sealed(td).items()}

    rc, payload = ds.cmd_finish(
        tmp_path,
        "FT-1",
        "plan",
        "completed",
        skill_output={"cognitive_outcomes": forged},
    )
    assert rc == 1
    assert "no successful outcome" in payload["error"]


def test_tampered_outcome_digest_cannot_complete_the_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    td = _cognitive_workspace(tmp_path, monkeypatch)
    ds.cmd_next(tmp_path, "FT-1")
    sealed = _sealed(td)
    for facts in sealed.values():
        _publish_outcome(facts)
    planning = sealed["planning"]
    token = hashlib.sha256(str(planning["logical_invocation_id"]).encode()).hexdigest()
    path = Path(planning["artifact_root"]) / "invocations" / token / "outcome.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    value["result"] = {"tampered": True}
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    rc, payload = ds.cmd_finish(tmp_path, "FT-1", "plan", "completed")
    assert rc == 1
    assert "does not match the sealed stage generation" in payload["error"]


def test_a_resumed_cognitive_stage_keeps_its_seal_when_head_moves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-entering next after a mid-stage commit must not crash or reseal."""
    td = _cognitive_workspace(tmp_path, monkeypatch)
    rc, first = ds.cmd_next(tmp_path, "FT-1")
    assert rc == 0
    sealed_before = _sealed(td)

    monkeypatch.setattr(ds, "_git_head_sha", lambda root: "b" * 40)
    rc, second = ds.cmd_next(tmp_path, "FT-1")

    assert rc == 0, second
    assert second["stage"] == "plan"
    assert second["generation"] == first["generation"]
    assert _sealed(td) == sealed_before
    assert second["cognitive_substeps"] == sealed_before


def _review_loop_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, dict[str, Any]]:
    """Drive real dispatch to a sealed review_loop with the fixers active.

    Returns (ticket_dir, review_loop descriptor).
    """
    _write_workspace(
        tmp_path,
        handlers={"ticket": "inline", "review_loop": "inline"},
        stages=["ticket", "review_loop"],
        compounding=False,
    )
    _stub_git_head(monkeypatch, "a" * 40)
    ds.cmd_init(tmp_path, "FT-1")
    td = tmp_path / ".flow" / "runs" / "FT-1"
    agent_routes.snapshot(tmp_path, "codex", output_path=td / "route-snapshot.json")
    ds.cmd_next(tmp_path, "FT-1")
    ds.cmd_finish(tmp_path, "FT-1", "ticket", "completed")
    rc, descriptor = ds.cmd_next(tmp_path, "FT-1")
    assert rc == 0, descriptor
    assert descriptor["stage"] == "review_loop", descriptor
    return td, descriptor


def test_review_loop_green_first_poll_completes_via_reasoned_fixer_skips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A CI-green-first-poll review_loop must complete though no fixer ran.

    Activating review_fixer/revision_fixer seals review_fix/revision_fix as pending
    conditional substeps, so the terminal advance needs an outcome-or-skip for each on
    every path — the green-first-poll included, where §2 never runs and neither fixer
    launches. Without §5's reasoned skips the stage wedges (the closed defect); with them
    it completes. Reverting §5 drops the skills, which is the no-skips wedge branch here.
    """
    td, descriptor = _review_loop_workspace(tmp_path, monkeypatch)

    sealed = _sealed(td, "review_loop")
    assert set(sealed) == {"review_fix", "revision_fix"}
    for name in ("review_fix", "revision_fix"):
        assert sealed[name]["activation"] == "pending"
        assert sealed[name]["conditional"] is True

    # No fixer ran and no reasoned skip emitted: the fail-closed cognitive fence (the wedge).
    rc, wedged = ds.cmd_finish(tmp_path, "FT-1", "review_loop", "completed")
    assert rc == 1
    assert wedged["error"] == (
        "activated cognitive substep 'review_fix' has no successful outcome or valid skip"
    )

    # §5 emits a reasoned skip for each un-run fixer substep through the REAL executor.
    body = cw.run_stage(
        descriptor,
        {
            "review_fix": {"skip": {"reason": "CI green on the first poll; no fix needed"}},
            "revision_fix": {"skip": {"reason": "not a revision sub-run"}},
        },
        source_root=tmp_path,
        artifact_root=td / "cognitive" / "review_loop",
        capsule_root=td / "cognitive" / "capsules",
        owner_id="owner",
        owner_harness="codex",
    )
    assert set(body["cognitive_skips"]) == {"review_fix", "revision_fix"}

    rc, done = ds.cmd_finish(tmp_path, "FT-1", "review_loop", "completed", skill_output=body)
    assert rc == 0, done
    ts, _ = state.read(td)
    assert ts is not None
    assert ts.stages["review_loop"].status == "completed"


def _code_review_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, dict[str, Any]]:
    """Drive real dispatch to a sealed code_review with its three conditional substeps.

    Returns (ticket_dir, code_review descriptor).
    """
    _write_workspace(
        tmp_path,
        handlers={"ticket": "inline", "code_review": "inline"},
        stages=["ticket", "code_review"],
        compounding=False,
    )
    _stub_git_head(monkeypatch, "a" * 40)
    ds.cmd_init(tmp_path, "FT-1")
    td = tmp_path / ".flow" / "runs" / "FT-1"
    agent_routes.snapshot(tmp_path, "codex", output_path=td / "route-snapshot.json")
    ds.cmd_next(tmp_path, "FT-1")
    ds.cmd_finish(tmp_path, "FT-1", "ticket", "completed")
    rc, descriptor = ds.cmd_next(tmp_path, "FT-1")
    assert rc == 0, descriptor
    assert descriptor["stage"] == "code_review", descriptor
    return td, descriptor


def test_code_review_completes_via_reasoned_skips_for_the_native_readers_and_no_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """code_review must complete though no capsule ran: native readers + no auto-fix.

    Every code_review substep is a pending conditional (the two reader passes run natively
    and launch no capsule; review_fix launches only when a finding is auto-fixable), so the
    terminal advance needs an outcome-or-skip for each. Without the step-9 reasoned skips the
    stage wedges on the first sealed reader substep (the green-first-poll wedge class); with
    them it completes. Reverting the step-9 terminal-skip wiring drops the skips, which is the
    no-skips wedge branch asserted first here.
    """
    td, descriptor = _code_review_workspace(tmp_path, monkeypatch)

    sealed = _sealed(td, "code_review")
    assert set(sealed) == {"primary_review", "plan_blind_review", "review_fix"}
    for name in ("primary_review", "plan_blind_review", "review_fix"):
        assert sealed[name]["activation"] == "pending"
        assert sealed[name]["conditional"] is True, name

    # No capsule ran and no reasoned skip emitted: the fail-closed cognitive fence (the wedge).
    rc, wedged = ds.cmd_finish(tmp_path, "FT-1", "code_review", "completed")
    assert rc == 1
    assert wedged["error"] == (
        "activated cognitive substep 'plan_blind_review' has no successful outcome or valid skip"
    )

    # Step 9 emits a reasoned skip for every un-run substep through the REAL executor: both
    # native readers always, and review_fix because no finding was auto-fixable this run.
    body = cw.run_stage(
        descriptor,
        {
            "primary_review": {"skip": {"reason": "primary review ran natively"}},
            "plan_blind_review": {"skip": {"reason": "plan-blind reader ran natively"}},
            "review_fix": {"skip": {"reason": "no auto-fixable findings"}},
        },
        source_root=tmp_path,
        artifact_root=td / "cognitive" / "code_review",
        capsule_root=td / "cognitive" / "capsules",
        owner_id="owner",
        owner_harness="codex",
    )
    assert set(body["cognitive_skips"]) == {"primary_review", "plan_blind_review", "review_fix"}

    rc, done = ds.cmd_finish(tmp_path, "FT-1", "code_review", "completed", skill_output=body)
    assert rc == 0, done
    ts, _ = state.read(td)
    assert ts is not None
    assert ts.stages["code_review"].status == "completed"


def _publish_outcome(facts: dict[str, Any], *, generation: int | None = None) -> dict[str, Any]:
    """Write the worker's receipt where the dispatcher seals it, as the engine would."""
    outcome = _outcome(facts, generation=generation)
    token = hashlib.sha256(str(facts["logical_invocation_id"]).encode()).hexdigest()
    path = Path(facts["artifact_root"]) / "invocations" / token / "outcome.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(outcome, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return outcome


def _outcome(facts: dict[str, Any], *, generation: int | None = None) -> dict[str, Any]:
    generation = facts["stage_generation"] if generation is None else generation
    body = {
        "logical_invocation_id": facts["logical_invocation_id"],
        "generation": generation,
        "profile": facts["profile"],
        "status": "succeeded",
        "result": {"ok": True},
        "receipts": {
            "route": {
                "activation": "active",
                "desired": facts["desired_route"],
                "effective": facts["desired_route"],
            },
            "process": {
                "child_reaped": True,
                "process_group_absent": True,
                "stdout_eof": True,
                "stderr_eof": True,
            },
            "disposal": {"absent": True, "quarantined": False},
        },
        "failure": None,
        "run_id": facts["run_id"],
        "stage": facts["stage"],
        "substep": facts["substep"],
        "stage_generation": generation,
        "route_snapshot_digest": facts["route_snapshot_digest"],
        "source_sha": facts["source_sha"],
        "lease_fence": facts["lease_fence"],
        "schema": "flow.cognitive-work-outcome/v1",
    }
    return {**body, "digest": agent_routes.canonical_digest(body)}
