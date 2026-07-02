"""Contract tests for trace_mine.py, the read-only transcript-extract miner."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import trace_mine


def _slug(path: Path) -> str:
    """Independent re-derivation of the `~/.claude/projects/` slug convention
    (flatten `/` and `.` to `-`), so fixtures don't lean on trace_mine's own
    `_slugify` to build the paths trace_mine then re-derives and checks.
    """
    return str(path).replace("/", "-").replace(".", "-")


def _write_jsonl(path: Path, lines: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")


def _project_dir(tmp_path: Path, workspace_root: Path) -> Path:
    return tmp_path / "projects" / _slug(workspace_root)


def _run_extract(
    capsys: pytest.CaptureFixture[str], *extra_args: str
) -> tuple[int, dict[str, Any] | None]:
    rc = trace_mine.cli_main(["extract", *extra_args])
    out = capsys.readouterr().out
    payload = json.loads(out) if out.strip() else None
    return rc, payload


def _tool_use_line(tool_id: str, name: str, ts: str, session: str = "sess1") -> dict[str, Any]:
    return {
        "type": "assistant",
        "timestamp": ts,
        "gitBranch": "main",
        "sessionId": session,
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": {}}],
        },
    }


def _tool_error_line(tool_id: str, ts: str, body: str, session: str = "sess1") -> dict[str, Any]:
    return {
        "type": "user",
        "timestamp": ts,
        "gitBranch": "main",
        "sessionId": session,
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "is_error": True,
                    "content": f"<tool_use_error>{body}</tool_use_error>",
                }
            ],
        },
    }


def _descriptor_line(
    ts: str,
    stage: str,
    *,
    finished_stage: str | None = None,
    ticket_dir: str = "/x/.flow/runs/flow-eia3",
    session: str = "sess1",
    **markers: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "done": False,
        "stage": stage,
        "head_sha": "abc123",
        "handler_type": "inline",
        "ticket_dir": ticket_dir,
        "timeout_min": 10,
        **markers,
    }
    if finished_stage is not None:
        payload["finished"] = {"stage": finished_stage, "status": "completed"}
    return {
        "type": "user",
        "timestamp": ts,
        "gitBranch": "main",
        "sessionId": session,
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_dispatch",
                    "is_error": False,
                    "content": json.dumps(payload, indent=2, sort_keys=True),
                }
            ],
        },
    }


def _retry_line(ts: str, session: str = "sess1", subtype: str = "api_error") -> dict[str, Any]:
    return {
        "type": "system",
        "subtype": subtype,
        "level": "error",
        "error": {"message": "Connection error."},
        "retryInMs": 522.3,
        "retryAttempt": 1,
        "maxRetries": 10,
        "timestamp": ts,
        "sessionId": session,
        "gitBranch": "main",
    }


# ─── slug convention (hardcoded, independent of _slugify's own logic) ──────


def test_slugify_matches_claude_projects_convention() -> None:
    assert trace_mine._slugify(Path("/a/b/.flow/worktrees/c")) == "-a-b--flow-worktrees-c"
    assert trace_mine._slugify(Path("/a/b")) == "-a-b"


# ─── the four extractors ────────────────────────────────────────────────────


def test_extract_tool_error_event(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    workspace_root = tmp_path / "repo"
    transcript = _project_dir(tmp_path, workspace_root) / "t1.jsonl"
    _write_jsonl(
        transcript,
        [
            _tool_use_line("toolu_1", "Bash", "2026-06-01T00:00:00.000Z"),
            _tool_error_line("toolu_1", "2026-06-01T00:00:01.000Z", "boom"),
        ],
    )
    rc, payload = _run_extract(
        capsys,
        "--transcript",
        str(transcript),
        "--workspace-root",
        str(workspace_root),
        "--projects-root",
        str(tmp_path / "projects"),
    )
    assert rc == 0
    assert payload is not None
    events = payload["events"]
    assert len(events) == 1
    ev = events[0]
    assert ev["kind"] == "tool_error"
    assert ev["body"] == "boom"
    assert ev["detail"] == "Bash"
    assert ev["tool_use_id"] == "toolu_1"
    assert ev["stage"] == "<pre-dispatch>"


def test_extract_silent_retry_event(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    workspace_root = tmp_path / "repo"
    transcript = _project_dir(tmp_path, workspace_root) / "t1.jsonl"
    # C1: subtype differs from the confirmed-live "api_error" -- predicate must
    # still fire off retryAttempt presence alone.
    _write_jsonl(transcript, [_retry_line("2026-06-01T00:00:00.000Z", subtype="something_else")])
    rc, payload = _run_extract(
        capsys,
        "--transcript",
        str(transcript),
        "--workspace-root",
        str(workspace_root),
        "--projects-root",
        str(tmp_path / "projects"),
    )
    assert rc == 0
    assert payload is not None
    events = payload["events"]
    assert len(events) == 1
    ev = events[0]
    assert ev["kind"] == "silent_retry"
    assert ev["body"] == "Connection error."
    detail = json.loads(ev["detail"])
    assert detail["subtype"] == "something_else"
    assert detail["max_retries"] == 10
    assert detail["message"] == "Connection error."


def test_extract_drift_marker_event(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    workspace_root = tmp_path / "repo"
    transcript = _project_dir(tmp_path, workspace_root) / "t1.jsonl"
    _write_jsonl(
        transcript,
        [
            _descriptor_line(
                "2026-06-01T00:00:00.000Z", "code_review", reconciled_drift="workspace_toml"
            ),
            _descriptor_line("2026-06-01T00:00:05.000Z", "e2e", state_recovered_from_backup=True),
        ],
    )
    rc, payload = _run_extract(
        capsys,
        "--transcript",
        str(transcript),
        "--workspace-root",
        str(workspace_root),
        "--projects-root",
        str(tmp_path / "projects"),
    )
    assert rc == 0
    assert payload is not None
    events = payload["events"]
    assert [e["kind"] for e in events] == ["drift", "drift"]
    assert events[0]["detail"] == "reconciled_drift"
    assert events[0]["body"] == "reconciled_drift=workspace_toml"
    assert events[1]["detail"] == "state_recovered_from_backup"
    # never opens friction.jsonl (the "eaten" cross-check is child-2's job): no
    # import of the modules that would read/locate it.
    source = Path(trace_mine.__file__).read_text(encoding="utf-8")
    assert "import flow_friction" not in source
    assert "import _memory_paths" not in source
    assert not list(tmp_path.rglob("friction.jsonl"))


def test_extract_stall_gap_event(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    workspace_root = tmp_path / "repo"
    transcript = _project_dir(tmp_path, workspace_root) / "t1.jsonl"
    _write_jsonl(
        transcript,
        [
            _tool_use_line("toolu_1", "Bash", "2026-06-01T00:00:00.000Z"),
            _tool_use_line("toolu_2", "Bash", "2026-06-01T00:10:00.000Z"),  # 600s gap
        ],
    )
    rc, payload = _run_extract(
        capsys,
        "--transcript",
        str(transcript),
        "--workspace-root",
        str(workspace_root),
        "--projects-root",
        str(tmp_path / "projects"),
    )
    assert rc == 0
    assert payload is not None
    gaps = [e for e in payload["events"] if e["kind"] == "stall_gap"]
    assert len(gaps) == 1
    assert gaps[0]["gap_secs"] == pytest.approx(600.0)
    assert gaps[0]["gap_start_ts"] == "2026-06-01T00:00:00.000Z"
    assert gaps[0]["gap_end_ts"] == "2026-06-01T00:10:00.000Z"


def test_stall_gap_below_threshold_emits_none(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace_root = tmp_path / "repo"
    transcript = _project_dir(tmp_path, workspace_root) / "t1.jsonl"
    _write_jsonl(
        transcript,
        [
            _tool_use_line("toolu_1", "Bash", "2026-06-01T00:00:00.000Z"),
            _tool_use_line("toolu_2", "Bash", "2026-06-01T00:01:40.000Z"),  # 100s gap
        ],
    )
    rc, payload = _run_extract(
        capsys,
        "--transcript",
        str(transcript),
        "--workspace-root",
        str(workspace_root),
        "--projects-root",
        str(tmp_path / "projects"),
    )
    assert rc == 0
    assert payload is not None
    assert [e for e in payload["events"] if e["kind"] == "stall_gap"] == []


# ─── stage bucketing ────────────────────────────────────────────────────────


def test_stage_bucketing_from_dispatch_descriptors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace_root = tmp_path / "repo"
    transcript = _project_dir(tmp_path, workspace_root) / "t1.jsonl"
    _write_jsonl(
        transcript,
        [
            _tool_use_line("toolu_pre", "Bash", "2026-06-01T00:00:00.000Z"),
            _tool_error_line("toolu_pre", "2026-06-01T00:00:01.000Z", "pre-dispatch failure"),
            _descriptor_line("2026-06-01T00:00:02.000Z", "implement"),
            _tool_use_line("toolu_in", "Bash", "2026-06-01T00:00:03.000Z"),
            _tool_error_line("toolu_in", "2026-06-01T00:00:04.000Z", "implement failure"),
            _descriptor_line("2026-06-01T00:00:05.000Z", "code_review", finished_stage="implement"),
        ],
    )
    rc, payload = _run_extract(
        capsys,
        "--transcript",
        str(transcript),
        "--workspace-root",
        str(workspace_root),
        "--projects-root",
        str(tmp_path / "projects"),
    )
    assert rc == 0
    assert payload is not None
    errors = [e for e in payload["events"] if e["kind"] == "tool_error"]
    assert len(errors) == 2
    assert errors[0]["stage"] == "<pre-dispatch>"
    assert errors[1]["stage"] == "implement"
    assert payload["stage_order"] == ["implement", "code_review"]


def test_tool_use_id_links_to_tool_name(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    workspace_root = tmp_path / "repo"
    transcript = _project_dir(tmp_path, workspace_root) / "t1.jsonl"
    _write_jsonl(
        transcript,
        [
            _tool_use_line("toolu_a", "Bash", "2026-06-01T00:00:00.000Z"),
            _tool_use_line("toolu_b", "Read", "2026-06-01T00:00:01.000Z"),
            _tool_error_line("toolu_a", "2026-06-01T00:00:02.000Z", "bash boom"),
            _tool_error_line("toolu_b", "2026-06-01T00:00:03.000Z", "read boom"),
        ],
    )
    rc, payload = _run_extract(
        capsys,
        "--transcript",
        str(transcript),
        "--workspace-root",
        str(workspace_root),
        "--projects-root",
        str(tmp_path / "projects"),
    )
    assert rc == 0
    assert payload is not None
    by_id = {e["tool_use_id"]: e["detail"] for e in payload["events"] if e["kind"] == "tool_error"}
    assert by_id == {"toolu_a": "Bash", "toolu_b": "Read"}


# ─── resilience ─────────────────────────────────────────────────────────────


def test_malformed_line_resilience(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    workspace_root = tmp_path / "repo"
    transcript = _project_dir(tmp_path, workspace_root) / "t1.jsonl"
    valid_use = json.dumps(_tool_use_line("toolu_1", "Bash", "2026-06-01T00:00:00.000Z"))
    valid_err = json.dumps(_tool_error_line("toolu_1", "2026-06-01T00:00:01.000Z", "boom"))
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text(
        "\n".join(
            [
                "",
                "{not json at all",
                valid_use,
                "plain text, not even braces",
                '{"truncated": ',
                valid_err,
                "",
            ]
        ),
        encoding="utf-8",
    )
    rc, payload = _run_extract(
        capsys,
        "--transcript",
        str(transcript),
        "--workspace-root",
        str(workspace_root),
        "--projects-root",
        str(tmp_path / "projects"),
    )
    assert rc == 0
    assert payload is not None
    events = payload["events"]
    assert len(events) == 1
    assert events[0]["kind"] == "tool_error"
    assert events[0]["body"] == "boom"


def test_empty_vs_missing_transcript(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    workspace_root = tmp_path / "repo"
    project_dir = _project_dir(tmp_path, workspace_root)

    empty_transcript = project_dir / "empty.jsonl"
    empty_transcript.parent.mkdir(parents=True, exist_ok=True)
    empty_transcript.write_text("", encoding="utf-8")
    rc, payload = _run_extract(
        capsys,
        "--transcript",
        str(empty_transcript),
        "--workspace-root",
        str(workspace_root),
        "--projects-root",
        str(tmp_path / "projects"),
    )
    assert rc == 0
    assert payload is not None
    assert payload["events"] == []

    missing_transcript = project_dir / "missing.jsonl"
    rc, payload = _run_extract(
        capsys,
        "--transcript",
        str(missing_transcript),
        "--workspace-root",
        str(workspace_root),
        "--projects-root",
        str(tmp_path / "projects"),
    )
    assert rc == 3
    assert payload is None


# ─── self-target guard ──────────────────────────────────────────────────────


def test_self_target_rejection(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    workspace_root = tmp_path / "repo"
    outside_transcript = tmp_path / "projects" / "-some-other-project" / "x.jsonl"
    rc, payload = _run_extract(
        capsys,
        "--transcript",
        str(outside_transcript),
        "--workspace-root",
        str(workspace_root),
        "--projects-root",
        str(tmp_path / "projects"),
    )
    assert rc == 4
    assert payload is None


def test_self_target_accepts_parent_repo_dir_for_worktree_workspace_root(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A worktree workspace-root's own /flow session often files its transcript
    # under the main repo's project dir, not the worktree's own slug dir.
    repo_root = tmp_path / "repo"
    workspace_root = repo_root / ".flow" / "worktrees" / "feat-x"
    transcript = _project_dir(tmp_path, repo_root) / "t1.jsonl"
    _write_jsonl(transcript, [_tool_use_line("toolu_1", "Bash", "2026-06-01T00:00:00.000Z")])
    rc, payload = _run_extract(
        capsys,
        "--transcript",
        str(transcript),
        "--workspace-root",
        str(workspace_root),
        "--projects-root",
        str(tmp_path / "projects"),
    )
    assert rc == 0
    assert payload is not None


def test_bad_args_requires_exactly_one_of_transcript_or_session(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, payload = _run_extract(capsys, "--workspace-root", str(tmp_path / "repo"))
    assert rc == 1
    assert payload is None
