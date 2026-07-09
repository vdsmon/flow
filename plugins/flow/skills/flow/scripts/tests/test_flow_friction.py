"""Contract tests for flow_friction.py, append-only friction log."""

from __future__ import annotations

import fcntl
import json
import multiprocessing
import os
from pathlib import Path
from typing import Any

import _memory_paths
import flow_friction


def _seed_workspace(root: Path, namespace: str = "demo") -> None:
    flow = root / ".flow"
    flow.mkdir(parents=True, exist_ok=True)
    (flow / "workspace.toml").write_text(
        '[tracker]\nbackend = "jira"\n[tracker.jira]\ncloud_id = "x"\nproject_key = "FT"\n'
        f'\n[memory]\nnamespace = "{namespace}"\n',
        encoding="utf-8",
    )


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def test_append_returns_entry(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    entry = flow_friction.append(
        tmp_path, "FT-1", "run0001", "implement", "RECONCILE", "expanded planned_files"
    )
    assert entry["type"] == "RECONCILE"
    assert entry["ticket"] == "FT-1"
    assert entry["run_id"] == "run0001"
    assert entry["stage"] == "implement"
    assert entry["severity"] == "major"
    assert entry["id"]
    assert "detail" not in entry  # omitted when not provided


def test_append_writes_jsonl_line(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    flow_friction.append(tmp_path, "FT-1", "r", "ticket", "DRIFT", "config changed", detail="x")
    fpath = _memory_paths.friction_path(tmp_path, "demo")
    rows = _read_jsonl(fpath)
    assert len(rows) == 1
    assert rows[0]["type"] == "DRIFT"
    assert rows[0]["detail"] == "x"


def test_append_accumulates_no_dedup(tmp_path: Path) -> None:
    # identical events are distinct entries (no dedup): both land.
    _seed_workspace(tmp_path)
    flow_friction.append(tmp_path, "FT-1", "r", "implement", "RETRY", "same")
    flow_friction.append(tmp_path, "FT-1", "r", "implement", "RETRY", "same")
    rows = _read_jsonl(_memory_paths.friction_path(tmp_path, "demo"))
    assert len(rows) == 2
    assert rows[0]["id"] != rows[1]["id"]


def test_invalid_type_raises(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    try:
        flow_friction.append(tmp_path, "FT-1", "r", "implement", "BOGUS", "x")
    except flow_friction._InvalidType:
        pass
    else:
        raise AssertionError("expected _InvalidType")


def test_invalid_severity_raises(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    try:
        flow_friction.append(tmp_path, "FT-1", "r", "implement", "RETRY", "x", severity="loud")
    except flow_friction._InvalidType:
        pass
    else:
        raise AssertionError("expected _InvalidType")


def test_cli_happy_path(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    rc = flow_friction.cli_main(
        [
            "--ticket",
            "FT-1",
            "--run-id",
            "r",
            "--stage",
            "create_pr",
            "--type",
            "MISSING_TOOL",
            "--body",
            "skill ship-it not installed",
            "--workspace-root",
            str(tmp_path),
        ]
    )
    assert rc == 0
    rows = _read_jsonl(_memory_paths.friction_path(tmp_path, "demo"))
    assert rows[0]["type"] == "MISSING_TOOL"


def test_cli_invalid_type_returns_3(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    rc = flow_friction.cli_main(
        [
            "--ticket",
            "FT-1",
            "--run-id",
            "r",
            "--stage",
            "x",
            "--type",
            "NOPE",
            "--body",
            "b",
            "--workspace-root",
            str(tmp_path),
        ]
    )
    assert rc == 3


# ─── Concurrency: friction flock contention → exit 2 ───────────────────────────


def _hold_friction_lock(lock_path_str: str, acquired_evt: Any, release_evt: Any) -> None:
    """Top-level so multiprocessing can pickle it on macOS spawn-start.

    Holds an exclusive flock on the friction lock file, signals once held, and
    waits for release. While held, cli_main's flock_retry exhausts and returns 2.
    """
    fd = os.open(lock_path_str, os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    acquired_evt.set()
    release_evt.wait(timeout=30)
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)


def test_cli_lock_contention_returns_2(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    ns = _memory_paths.resolve_namespace(tmp_path)
    lock_path = _memory_paths.friction_lock_path(tmp_path, ns)
    # O_CREAT does not create parent dirs; the holder runs before append()'s mkdir.
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    ctx = multiprocessing.get_context("spawn")
    acquired_evt = ctx.Event()
    release_evt = ctx.Event()
    proc = ctx.Process(target=_hold_friction_lock, args=(str(lock_path), acquired_evt, release_evt))
    proc.start()
    try:
        assert acquired_evt.wait(timeout=10)
        rc = flow_friction.cli_main(
            [
                "--ticket",
                "FT-1",
                "--run-id",
                "r",
                "--stage",
                "implement",
                "--type",
                "RETRY",
                "--body",
                "b",
                "--workspace-root",
                str(tmp_path),
            ]
        )
        assert rc == 2
    finally:
        release_evt.set()
        proc.join(timeout=10)


def test_cli_missing_config_returns_4(tmp_path: Path) -> None:
    # no .flow/workspace.toml seeded
    rc = flow_friction.cli_main(
        [
            "--ticket",
            "FT-1",
            "--run-id",
            "r",
            "--stage",
            "x",
            "--type",
            "RETRY",
            "--body",
            "b",
            "--workspace-root",
            str(tmp_path),
        ]
    )
    assert rc == 4


# ─── plugin_version (self-read, fully guarded) ───────────────────────────────


def _live_plugin_version() -> str:
    path = Path(flow_friction.__file__).resolve().parents[3] / ".claude-plugin" / "plugin.json"
    return json.loads(path.read_text(encoding="utf-8"))["version"]


def test_append_stamps_plugin_version(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    entry = flow_friction.append(tmp_path, "FT-1", "r", "implement", "RETRY", "x")
    live = _live_plugin_version()
    assert isinstance(entry["plugin_version"], str)
    assert entry["plugin_version"]
    assert entry["plugin_version"] == live


def test_plugin_version_in_jsonl_line(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    flow_friction.append(tmp_path, "FT-1", "r", "implement", "RETRY", "x")
    rows = _read_jsonl(_memory_paths.friction_path(tmp_path, "demo"))
    assert rows[0]["plugin_version"] == _live_plugin_version()


def test_append_succeeds_when_plugin_version_guarded_empty(
    tmp_path: Path, monkeypatch: Any
) -> None:
    _seed_workspace(tmp_path)
    monkeypatch.setattr(flow_friction, "plugin_version", lambda: "")
    entry = flow_friction.append(tmp_path, "FT-1", "r", "implement", "RETRY", "x")
    assert entry["plugin_version"] == ""
