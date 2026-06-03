"""Contract tests for memory_append.py — single-writer JSONL append."""

from __future__ import annotations

import json
import multiprocessing
from pathlib import Path

import pytest

import _memory_paths
import memory_append


def _seed_workspace(root: Path, namespace: str = "demo") -> None:
    flow = root / ".flow"
    flow.mkdir(parents=True, exist_ok=True)
    (flow / "workspace.toml").write_text(
        f'[tracker]\nbackend = "jira"\n[tracker.jira]\ncloud_id = "x"\nproject_key = "FT"\n\n[memory]\nnamespace = "{namespace}"\n',
        encoding="utf-8",
    )


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


# ─── compute_id ──────────────────────────────────────────────────────────────


def test_compute_id_deterministic() -> None:
    a = memory_append.compute_id("ns", "FT-1", "LEARNED", "hello world")
    b = memory_append.compute_id("ns", "FT-1", "LEARNED", "hello world")
    assert a == b
    assert len(a) == 16


def test_compute_id_collapses_whitespace_and_case() -> None:
    a = memory_append.compute_id("ns", "FT-1", "LEARNED", "Hello   World")
    b = memory_append.compute_id("ns", "FT-1", "LEARNED", "hello world")
    assert a == b


def test_compute_id_strips_trailing_punctuation() -> None:
    a = memory_append.compute_id("ns", "FT-1", "LEARNED", "foo")
    b = memory_append.compute_id("ns", "FT-1", "LEARNED", "foo.")
    c = memory_append.compute_id("ns", "FT-1", "LEARNED", "foo!!")
    assert a == b == c


def test_compute_id_distinct_for_different_types() -> None:
    a = memory_append.compute_id("ns", "FT-1", "LEARNED", "x")
    b = memory_append.compute_id("ns", "FT-1", "DECISION", "x")
    assert a != b


def test_compute_id_distinct_for_different_namespaces() -> None:
    a = memory_append.compute_id("ns1", "FT-1", "LEARNED", "x")
    b = memory_append.compute_id("ns2", "FT-1", "LEARNED", "x")
    assert a != b


# ─── append() ────────────────────────────────────────────────────────────────


def test_append_creates_file_and_entry(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    entry = memory_append.append(tmp_path, "LEARNED", "fsync matters", "feature/FT-1", "FT-1")
    kpath = _memory_paths.knowledge_path(tmp_path, "demo")
    assert kpath.exists()
    entries = _read_jsonl(kpath)
    assert len(entries) == 1
    assert entries[0]["body"] == "fsync matters"
    assert entries[0]["id"] == entry["id"]
    assert entries[0]["namespace"] == "demo"


@pytest.mark.parametrize("type_", list(memory_append.VALID_TYPES))
def test_each_valid_type_accepted(tmp_path: Path, type_: str) -> None:
    _seed_workspace(tmp_path)
    memory_append.append(tmp_path, type_, f"body {type_}", "main", "FT-1")
    entries = _read_jsonl(_memory_paths.knowledge_path(tmp_path, "demo"))
    assert entries[0]["type"] == type_


def test_invalid_type_raises(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    with pytest.raises(memory_append._InvalidType, match="not in"):
        memory_append.append(tmp_path, "GARBAGE", "x", "main", "FT-1")


def test_duplicate_id_raises(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    memory_append.append(tmp_path, "LEARNED", "atomic write", "main", "FT-1")
    with pytest.raises(memory_append._DuplicateId):
        memory_append.append(tmp_path, "LEARNED", "atomic write", "main", "FT-1")


def test_id_override_used_when_provided(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    entry = memory_append.append(
        tmp_path,
        "LEARNED",
        "x",
        "main",
        "FT-1",
        id_override="1234567890abcdef",
    )
    assert entry["id"] == "1234567890abcdef"


def test_id_override_also_dedupes(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    memory_append.append(tmp_path, "LEARNED", "x", "main", "FT-1", id_override="aaaa1111bbbb2222")
    with pytest.raises(memory_append._DuplicateId):
        memory_append.append(
            tmp_path, "LEARNED", "different body", "main", "FT-1", id_override="aaaa1111bbbb2222"
        )


def test_distinct_entries_both_appended(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    memory_append.append(tmp_path, "LEARNED", "first", "main", "FT-1")
    memory_append.append(tmp_path, "LEARNED", "second", "main", "FT-1")
    entries = _read_jsonl(_memory_paths.knowledge_path(tmp_path, "demo"))
    assert len(entries) == 2


def test_ts_field_ms_precision(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    entry = memory_append.append(tmp_path, "LEARNED", "x", "main", "FT-1")
    assert "." in entry["ts"]
    ms_part = entry["ts"].split(".")[1].rstrip("Z")
    assert len(ms_part) == 3


def test_no_workspace_toml_raises(tmp_path: Path) -> None:
    with pytest.raises(_memory_paths._MemoryConfigError, match=r"no workspace\.toml"):
        memory_append.append(tmp_path, "LEARNED", "x", "main", "FT-1")


def test_no_memory_block_raises(tmp_path: Path) -> None:
    (tmp_path / ".flow").mkdir()
    (tmp_path / ".flow" / "workspace.toml").write_text(
        '[tracker]\nbackend = "jira"\n', encoding="utf-8"
    )
    with pytest.raises(_memory_paths._MemoryConfigError, match=r"\[memory\]"):
        memory_append.append(tmp_path, "LEARNED", "x", "main", "FT-1")


# ─── Quarantine on malformed scan ────────────────────────────────────────────


def test_malformed_line_quarantined(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    kpath = _memory_paths.knowledge_path(tmp_path, "demo")
    kpath.parent.mkdir(parents=True, exist_ok=True)
    kpath.write_text(
        "not json\n"
        + json.dumps({"id": "deadbeefcafebabe", "body": "real"}, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    memory_append.append(tmp_path, "LEARNED", "new entry", "main", "FT-1")
    quarantines = list(kpath.parent.glob("knowledge.jsonl.quarantine.*"))
    assert len(quarantines) >= 1
    q_lines = quarantines[0].read_text(encoding="utf-8").splitlines()
    assert any("not json" in line for line in q_lines)


def test_quarantine_does_not_rewrite_main_file(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    kpath = _memory_paths.knowledge_path(tmp_path, "demo")
    kpath.parent.mkdir(parents=True, exist_ok=True)
    kpath.write_text("not json\n", encoding="utf-8")
    memory_append.append(tmp_path, "LEARNED", "new entry", "main", "FT-1")
    main = kpath.read_text(encoding="utf-8").splitlines()
    assert main[0] == "not json"
    assert any("new entry" in line for line in main)


# ─── Concurrency: multiprocessing flock contention ───────────────────────────


def _appender_proc(root_str: str, body: str) -> None:
    memory_append.append(Path(root_str), "LEARNED", body, "main", "FT-1")


def test_concurrent_distinct_appenders_both_succeed(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    ctx = multiprocessing.get_context("spawn")
    p1 = ctx.Process(target=_appender_proc, args=(str(tmp_path), "first"))
    p2 = ctx.Process(target=_appender_proc, args=(str(tmp_path), "second"))
    p1.start()
    p2.start()
    p1.join(timeout=10)
    p2.join(timeout=10)
    assert p1.exitcode == 0
    assert p2.exitcode == 0
    entries = _read_jsonl(_memory_paths.knowledge_path(tmp_path, "demo"))
    bodies = {e["body"] for e in entries}
    assert bodies == {"first", "second"}


def _dup_appender(root_str: str) -> int:
    try:
        memory_append.append(Path(root_str), "LEARNED", "same body", "main", "FT-1")
        return 0
    except memory_append._DuplicateId:
        return 1


def test_concurrent_duplicate_only_one_wins(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    ctx = multiprocessing.get_context("spawn")
    p1 = ctx.Process(target=_dup_appender, args=(str(tmp_path),))
    p2 = ctx.Process(target=_dup_appender, args=(str(tmp_path),))
    p1.start()
    p2.start()
    p1.join(timeout=10)
    p2.join(timeout=10)
    assert p1.exitcode in (0, 1)
    assert p2.exitcode in (0, 1)
    entries = _read_jsonl(_memory_paths.knowledge_path(tmp_path, "demo"))
    assert len(entries) == 1


# ─── CLI ─────────────────────────────────────────────────────────────────────


def test_cli_happy_path(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed_workspace(tmp_path)
    rc = memory_append.cli_main(
        [
            "--type",
            "LEARNED",
            "--text",
            "atomic write",
            "--branch",
            "main",
            "--ticket",
            "FT-1",
            "--workspace-root",
            str(tmp_path),
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["type"] == "LEARNED"


def test_cli_duplicate_returns_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed_workspace(tmp_path)
    memory_append.append(tmp_path, "LEARNED", "x", "main", "FT-1")
    rc = memory_append.cli_main(
        [
            "--type",
            "LEARNED",
            "--text",
            "x",
            "--branch",
            "main",
            "--ticket",
            "FT-1",
            "--workspace-root",
            str(tmp_path),
        ]
    )
    assert rc == 1
    assert "duplicate" in capsys.readouterr().err


def test_cli_invalid_type_returns_3(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed_workspace(tmp_path)
    rc = memory_append.cli_main(
        [
            "--type",
            "GARBAGE",
            "--text",
            "x",
            "--branch",
            "main",
            "--ticket",
            "FT-1",
            "--workspace-root",
            str(tmp_path),
        ]
    )
    assert rc == 3


def test_cli_missing_workspace_returns_4(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = memory_append.cli_main(
        [
            "--type",
            "LEARNED",
            "--text",
            "x",
            "--branch",
            "main",
            "--ticket",
            "FT-1",
            "--workspace-root",
            str(tmp_path),
        ]
    )
    assert rc == 4
