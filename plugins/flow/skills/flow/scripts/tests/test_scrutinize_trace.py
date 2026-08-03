from __future__ import annotations

import json
from pathlib import Path

import scrutinize_trace as st


def _event(ts: str, typ: str, content) -> str:
    return json.dumps({"timestamp": ts, "type": typ, "message": {"content": content}})


def _tool_use(tool_id: str, name: str, tool_input: dict) -> dict:
    return {"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}


def _write_session(tmp_path: Path, name: str, lines: list[str]) -> Path:
    path = tmp_path / f"{name}.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _sample_session(tmp_path: Path, name: str = "sess-1") -> Path:
    lines = [
        _event("2026-08-03T10:00:00Z", "user", [{"type": "text", "text": "please fix the thing"}]),
        _event(
            "2026-08-03T10:00:05Z",
            "user",
            [{"type": "text", "text": "<command-message>flow:flow</command-message> args"}],
        ),
        _event(
            "2026-08-03T10:01:00Z",
            "assistant",
            [
                _tool_use(
                    "t1",
                    "Bash",
                    {
                        "command": "FLOW_HARNESS=x ./.flow/runtime/flow "
                        "--workspace-root . status --json"
                    },
                )
            ],
        ),
        _event(
            "2026-08-03T10:02:00Z",
            "assistant",
            [
                _tool_use(
                    "t2",
                    "Task",
                    {"description": "Implement the fix", "subagent_type": "general-purpose"},
                )
            ],
        ),
        _event(
            "2026-08-03T10:03:00Z",
            "assistant",
            [_tool_use("t3", "Bash", {"command": "false"})],
        ),
        _event(
            "2026-08-03T10:03:10Z",
            "user",
            [
                {
                    "type": "tool_result",
                    "tool_use_id": "t3",
                    "is_error": True,
                    "content": [{"type": "text", "text": "Exit code 1  boom"}],
                }
            ],
        ),
    ]
    return _write_session(tmp_path, name, lines)


def test_mine_session_extracts_all_signal_families(tmp_path):
    path = _sample_session(tmp_path)
    report = st.mine_session(path)
    assert report["first"] == "2026-08-03T10:00:00Z"
    assert report["last"] == "2026-08-03T10:03:10Z"
    plain, skill = report["user_messages"]
    assert plain["is_skill"] is False
    assert skill["is_skill"] is True
    assert report["flow_calls"] == [
        {
            "ts": "2026-08-03T10:01:00Z",
            "sub": "status",
            "command": "FLOW_HARNESS=x ./.flow/runtime/flow --workspace-root . status --json",
        }
    ]
    assert report["agent_spawns"][0]["type"] == "general-purpose"
    (error,) = report["tool_errors"]
    assert error["tool"] == "Bash"
    assert error["command"] == "false"
    assert "boom" in error["error"]


def test_mine_session_reads_string_content_user_messages(tmp_path):
    path = _write_session(
        tmp_path,
        "plain",
        [
            json.dumps(
                {
                    "timestamp": "2026-08-03T19:03:36Z",
                    "type": "user",
                    "message": {"content": "merged"},
                }
            )
        ],
    )
    (msg,) = st.mine_session(path)["user_messages"]
    assert msg["text"] == "merged"
    assert msg["is_skill"] is False


def test_mine_session_since_filters_signals_but_keeps_span(tmp_path):
    path = _sample_session(tmp_path)
    report = st.mine_session(path, since="2026-08-03T10:02:30Z")
    assert report["first"] == "2026-08-03T10:00:00Z"
    assert report["user_messages"] == []
    assert report["flow_calls"] == []
    assert report["agent_spawns"] == []
    assert len(report["tool_errors"]) == 1


def test_mine_session_joins_subagent_spans(tmp_path):
    path = _sample_session(tmp_path)
    sub_dir = tmp_path / path.stem / "subagents"
    sub_dir.mkdir(parents=True)
    (sub_dir / "agent-a1.jsonl").write_text(
        _event("2026-08-03T10:02:01Z", "assistant", [])
        + "\n"
        + _event("2026-08-03T10:09:00Z", "assistant", [])
        + "\n",
        encoding="utf-8",
    )
    (sub_dir / "agent-a1.meta.json").write_text(
        json.dumps({"description": "Implement the fix"}), encoding="utf-8"
    )
    (span,) = st.mine_session(path)["subagents"]
    assert span["first"] == "2026-08-03T10:02:01Z"
    assert span["last"] == "2026-08-03T10:09:00Z"
    assert span["description"] == "Implement the fix"


def test_mine_dir_drops_sessions_entirely_before_window(tmp_path):
    _sample_session(tmp_path, "old")
    fresh = _write_session(
        tmp_path,
        "fresh",
        [_event("2026-08-04T09:00:00Z", "user", [{"type": "text", "text": "hi"}])],
    )
    reports = st.mine_dir(tmp_path, since="2026-08-04T00:00:00Z")
    assert [r["session"] for r in reports] == ["fresh"]
    assert fresh.exists()


def test_mine_dir_session_filter(tmp_path):
    _sample_session(tmp_path, "one")
    _sample_session(tmp_path, "two")
    reports = st.mine_dir(tmp_path, sessions=["two"])
    assert [r["session"] for r in reports] == ["two"]


def test_cli_missing_dir_exits_2_and_json_round_trips(tmp_path, capsys):
    assert st.cli_main(["--transcript-dir", str(tmp_path / "nope")]) == 2
    capsys.readouterr()
    _sample_session(tmp_path)
    assert st.cli_main(["--transcript-dir", str(tmp_path), "--json"]) == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed[0]["flow_calls"][0]["sub"] == "status"
