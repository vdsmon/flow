"""Contract tests for pending_mutations.py — durable failed-mutation queue."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import pending_mutations as pm

_INTENT = "2026-05-28T12:00:00Z"


def _read_lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


# ─── compute_key ─────────────────────────────────────────────────────────────


def test_compute_key_deterministic() -> None:
    a = pm.compute_key("FT-1", "edit", {"b": 2, "a": 1})
    b = pm.compute_key("FT-1", "edit", {"a": 1, "b": 2})
    assert a == b
    assert len(a) == 16


def test_compute_key_distinct_for_different_op() -> None:
    a = pm.compute_key("FT-1", "edit", {"a": 1})
    b = pm.compute_key("FT-1", "comment", {"a": 1})
    assert a != b


def test_compute_key_distinct_for_different_args() -> None:
    a = pm.compute_key("FT-1", "edit", {"a": 1})
    b = pm.compute_key("FT-1", "edit", {"a": 2})
    assert a != b


def test_key_independent_of_run_id(tmp_path: Path) -> None:
    e1 = pm.append_mutation(
        tmp_path, ticket="FT-1", op="edit", args={"x": 1}, first_run_id="aaaa", intent_at=_INTENT
    )
    # Second call from a "recovered run" with a different run_id must collide.
    e2 = pm.append_mutation(
        tmp_path, ticket="FT-1", op="edit", args={"x": 1}, first_run_id="bbbb", intent_at=_INTENT
    )
    assert e1["idempotency_key"] == e2["idempotency_key"]
    # No-op returns the original entry, so first_run_id stays aaaa.
    assert e2["first_run_id"] == "aaaa"
    assert len(_read_lines(pm.pending_mutations_path(tmp_path))) == 1


# ─── append_mutation ─────────────────────────────────────────────────────────


def test_append_writes_one_line(tmp_path: Path) -> None:
    entry = pm.append_mutation(
        tmp_path,
        ticket="FT-2",
        op="transition",
        args={"to": "Done"},
        expected_pre_state={"status": "In Progress"},
        expected_postcondition={"status": "Done"},
        first_run_id="run0",
        intent_at=_INTENT,
    )
    path = pm.pending_mutations_path(tmp_path)
    lines = _read_lines(path)
    assert len(lines) == 1
    on_disk = lines[0]
    assert on_disk == entry
    assert on_disk["ticket"] == "FT-2"
    assert on_disk["op"] == "transition"
    assert on_disk["args"] == {"to": "Done"}
    assert on_disk["expected_pre_state"] == {"status": "In Progress"}
    assert on_disk["expected_postcondition"] == {"status": "Done"}
    assert on_disk["intent_at"] == _INTENT
    assert on_disk["first_run_id"] == "run0"
    assert on_disk["attempts"] == []
    assert on_disk["idempotency_key"] == pm.compute_key("FT-2", "transition", {"to": "Done"})
    assert len(on_disk["args_fingerprint"]) == 16


def test_append_idempotent(tmp_path: Path) -> None:
    pm.append_mutation(
        tmp_path, ticket="FT-3", op="comment", args={"body": "hi"}, intent_at=_INTENT
    )
    pm.append_mutation(
        tmp_path, ticket="FT-3", op="comment", args={"body": "hi"}, intent_at=_INTENT
    )
    assert len(_read_lines(pm.pending_mutations_path(tmp_path))) == 1


def test_append_distinct_entries_both_written(tmp_path: Path) -> None:
    pm.append_mutation(tmp_path, ticket="FT-4", op="edit", args={"a": 1}, intent_at=_INTENT)
    pm.append_mutation(tmp_path, ticket="FT-4", op="edit", args={"a": 2}, intent_at=_INTENT)
    assert len(_read_lines(pm.pending_mutations_path(tmp_path))) == 2


def test_invalid_op_rejected(tmp_path: Path) -> None:
    with pytest.raises(pm._InvalidArgs):
        pm.append_mutation(tmp_path, ticket="FT-5", op="bogus", args={}, intent_at=_INTENT)
    assert not pm.pending_mutations_path(tmp_path).exists()


# ─── list_mutations ──────────────────────────────────────────────────────────


def test_list_returns_entries(tmp_path: Path) -> None:
    pm.append_mutation(tmp_path, ticket="FT-6", op="link", args={"to": "FT-7"}, intent_at=_INTENT)
    pm.append_mutation(tmp_path, ticket="FT-6", op="create", args={"k": "v"}, intent_at=_INTENT)
    entries = pm.list_mutations(tmp_path)
    assert {e["op"] for e in entries} == {"link", "create"}


def test_list_quarantines_malformed(tmp_path: Path) -> None:
    pm.append_mutation(tmp_path, ticket="FT-8", op="edit", args={"a": 1}, intent_at=_INTENT)
    path = pm.pending_mutations_path(tmp_path)
    with path.open("a", encoding="utf-8") as fh:
        fh.write("this is not json\n")
        fh.write("[1, 2, 3]\n")  # valid json but not an object
    entries = pm.list_mutations(tmp_path)
    assert len(entries) == 1
    quarantine = path.with_name(path.name + ".quarantine")
    assert quarantine.exists()
    reasons = quarantine.read_text(encoding="utf-8")
    assert "this is not json" in reasons
    assert "not an object" in reasons


# ─── compact ─────────────────────────────────────────────────────────────────


def test_compact_removes_named_keys_keeps_others(tmp_path: Path) -> None:
    e1 = pm.append_mutation(tmp_path, ticket="FT-9", op="edit", args={"a": 1}, intent_at=_INTENT)
    e2 = pm.append_mutation(tmp_path, ticket="FT-9", op="edit", args={"a": 2}, intent_at=_INTENT)
    e3 = pm.append_mutation(tmp_path, ticket="FT-9", op="edit", args={"a": 3}, intent_at=_INTENT)
    removed = pm.compact(tmp_path, {e1["idempotency_key"], e3["idempotency_key"]})
    assert removed == 2
    remaining = pm.list_mutations(tmp_path)
    assert [e["idempotency_key"] for e in remaining] == [e2["idempotency_key"]]


def test_compact_missing_file_is_noop(tmp_path: Path) -> None:
    removed = pm.compact(tmp_path, {"deadbeef"})
    assert removed == 0
    assert not pm.pending_mutations_path(tmp_path).exists()


def test_compact_unknown_keys_remove_nothing(tmp_path: Path) -> None:
    pm.append_mutation(tmp_path, ticket="FT-10", op="edit", args={"a": 1}, intent_at=_INTENT)
    removed = pm.compact(tmp_path, {"notarealkey0000"})
    assert removed == 0
    assert len(pm.list_mutations(tmp_path)) == 1


# ─── CLI ─────────────────────────────────────────────────────────────────────


def test_cli_append_then_duplicate_exit_codes(tmp_path: Path) -> None:
    base = ["--workspace-root", str(tmp_path), "append", "--ticket", "FT-11", "--op", "edit"]
    rc1 = pm.cli_main([*base, "--args-json", '{"a": 1}'], clock=lambda: _INTENT)
    rc2 = pm.cli_main([*base, "--args-json", '{"a": 1}'], clock=lambda: _INTENT)
    assert rc1 == 0
    assert rc2 == 1
    assert len(_read_lines(pm.pending_mutations_path(tmp_path))) == 1


def test_cli_append_invalid_op_exit_3(tmp_path: Path) -> None:
    rc = pm.cli_main(
        [
            "--workspace-root",
            str(tmp_path),
            "append",
            "--ticket",
            "FT-12",
            "--op",
            "nope",
            "--args-json",
            "{}",
        ],
        clock=lambda: _INTENT,
    )
    assert rc == 3


def test_cli_append_bad_json_exit_3(tmp_path: Path) -> None:
    rc = pm.cli_main(
        [
            "--workspace-root",
            str(tmp_path),
            "append",
            "--ticket",
            "FT-13",
            "--op",
            "edit",
            "--args-json",
            "{not json}",
        ],
        clock=lambda: _INTENT,
    )
    assert rc == 3


def test_cli_compact_drop_keys(tmp_path: Path) -> None:
    e = pm.append_mutation(tmp_path, ticket="FT-14", op="edit", args={"a": 1}, intent_at=_INTENT)
    rc = pm.cli_main(
        ["--workspace-root", str(tmp_path), "compact", "--drop-keys", e["idempotency_key"]]
    )
    assert rc == 0
    assert pm.list_mutations(tmp_path) == []


def test_cli_clock_supplies_intent_at(tmp_path: Path) -> None:
    rc = pm.cli_main(
        [
            "--workspace-root",
            str(tmp_path),
            "append",
            "--ticket",
            "FT-15",
            "--op",
            "edit",
            "--args-json",
            '{"a": 1}',
        ],
        clock=lambda: "2099-01-01T00:00:00Z",
    )
    assert rc == 0
    entries = pm.list_mutations(tmp_path)
    assert entries[0]["intent_at"] == "2099-01-01T00:00:00Z"
