"""Cross-queue memory dedup: individual vs drain-batch runs converge on one store.

A ticket run individually (main checkout) and the same ticket run as one of N
concurrent drain-fleet worktrees must hit the SAME knowledge store with the
SAME idempotency id (path/branch/ts excluded from the formula) and the SAME
lock file. These tests append through two distinct workspace roots — main and
a bootstrap-shaped worktree (byte-copied workspace.toml + `.flow/memory-root`
sibling, mirroring flow_worktree._ensure_flow_config) — into one shared store.
"""

from __future__ import annotations

import contextlib
import json
import multiprocessing
import shutil
from pathlib import Path

import pytest

import _memory_paths
import memory_append
import observe_ship_event
import recall_pending

_NS = "demo"


def _make_main(tmp_path: Path) -> Path:
    main = tmp_path / "main"
    flow = main / ".flow"
    flow.mkdir(parents=True)
    (flow / "workspace.toml").write_text(
        "\n".join(
            [
                "[tracker]",
                'backend = "jira"',
                "[tracker.jira]",
                'cloud_id = "x"',
                'project_key = "FT"',
                "[memory]",
                f'namespace = "{_NS}"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return main


def _make_worktree(tmp_path: Path, main: Path, name: str) -> Path:
    worktree = tmp_path / name
    flow = worktree / ".flow"
    flow.mkdir(parents=True)
    shutil.copy2(main / ".flow" / "workspace.toml", flow / "workspace.toml")
    (flow / "memory-root").write_text(str(main / ".flow") + "\n", encoding="utf-8")
    return worktree


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def test_same_entry_from_main_and_worktree_dedups(tmp_path: Path) -> None:
    main = _make_main(tmp_path)
    worktree = _make_worktree(tmp_path, main, "wt-1")
    body = "flock serializes the append path"
    first = memory_append.append(main, "LEARNED", body, "main", "FT-1")
    with pytest.raises(memory_append._DuplicateId):
        memory_append.append(worktree, "LEARNED", body, "feature/FT-1", "FT-1")
    shared = _memory_paths.knowledge_path(main, _NS)
    entries = _read_jsonl(shared)
    assert len(entries) == 1
    assert entries[0]["id"] == first["id"]
    assert not (worktree / ".flow" / _NS).exists()


def test_different_branch_still_dedups(tmp_path: Path) -> None:
    main = _make_main(tmp_path)
    worktree = _make_worktree(tmp_path, main, "wt-1")
    memory_append.append(main, "FACT", "branch is excluded from the id", "main", "FT-2")
    with pytest.raises(memory_append._DuplicateId):
        memory_append.append(
            worktree, "FACT", "branch is excluded from the id", "feature/FT-2-retry", "FT-2"
        )
    assert len(_read_jsonl(_memory_paths.knowledge_path(main, _NS))) == 1


def test_distinct_tickets_same_body_no_false_dedup(tmp_path: Path) -> None:
    main = _make_main(tmp_path)
    worktree = _make_worktree(tmp_path, main, "wt-1")
    memory_append.append(main, "LEARNED", "identical finding", "main", "FT-1")
    memory_append.append(worktree, "LEARNED", "identical finding", "main", "FT-2")
    entries = _read_jsonl(_memory_paths.knowledge_path(main, _NS))
    assert {e["ticket"] for e in entries} == {"FT-1", "FT-2"}
    assert len({e["id"] for e in entries}) == 2


def test_lock_path_identical_across_roots(tmp_path: Path) -> None:
    main = _make_main(tmp_path)
    wt1 = _make_worktree(tmp_path, main, "wt-1")
    wt2 = _make_worktree(tmp_path, main, "wt-2")
    lock_main = _memory_paths.knowledge_lock_path(main, _NS)
    assert lock_main == _memory_paths.knowledge_lock_path(wt1, _NS)
    assert lock_main == _memory_paths.knowledge_lock_path(wt2, _NS)
    assert lock_main == main / ".flow" / _NS / "knowledge.jsonl.lock"


def _wt_appender(root_str: str, body: str) -> None:
    memory_append.append(Path(root_str), "LEARNED", body, "main", "FT-1")


def _wt_dup_appender(root_str: str) -> None:
    with contextlib.suppress(memory_append._DuplicateId):
        memory_append.append(Path(root_str), "LEARNED", "same body", "main", "FT-1")


def test_concurrent_appends_from_two_worktrees_distinct_bodies(tmp_path: Path) -> None:
    main = _make_main(tmp_path)
    wt1 = _make_worktree(tmp_path, main, "wt-1")
    wt2 = _make_worktree(tmp_path, main, "wt-2")
    ctx = multiprocessing.get_context("spawn")
    p1 = ctx.Process(target=_wt_appender, args=(str(wt1), "first"))
    p2 = ctx.Process(target=_wt_appender, args=(str(wt2), "second"))
    p1.start()
    p2.start()
    p1.join(timeout=10)
    p2.join(timeout=10)
    assert p1.exitcode == 0
    assert p2.exitcode == 0
    entries = _read_jsonl(_memory_paths.knowledge_path(main, _NS))
    assert {e["body"] for e in entries} == {"first", "second"}


def test_concurrent_appends_from_two_worktrees_identical_body(tmp_path: Path) -> None:
    main = _make_main(tmp_path)
    wt1 = _make_worktree(tmp_path, main, "wt-1")
    wt2 = _make_worktree(tmp_path, main, "wt-2")
    ctx = multiprocessing.get_context("spawn")
    p1 = ctx.Process(target=_wt_dup_appender, args=(str(wt1),))
    p2 = ctx.Process(target=_wt_dup_appender, args=(str(wt2),))
    p1.start()
    p2.start()
    p1.join(timeout=10)
    p2.join(timeout=10)
    assert p1.exitcode == 0
    assert p2.exitcode == 0
    entries = _read_jsonl(_memory_paths.knowledge_path(main, _NS))
    assert len(entries) == 1
    assert entries[0]["body"] == "same body"


def test_ship_event_rerun_across_roots_writes_dupe(tmp_path: Path) -> None:
    main = _make_main(tmp_path)
    worktree = _make_worktree(tmp_path, main, "wt-1")
    payload = {
        "ticket": "FT-9",
        "shipped_at": "2026-06-09T00:00:00Z",
        "evidence": {"pr": "https://example.test/pr/1"},
    }
    primary, is_dupe = observe_ship_event.observe(main, "FT-9", dict(payload), "a" * 16)
    assert not is_dupe
    assert primary == _memory_paths.ship_event_path(main, _NS, "FT-9")
    before = primary.read_bytes()
    dupe, is_dupe = observe_ship_event.observe(worktree, "FT-9", dict(payload), "b" * 16)
    assert is_dupe
    assert dupe == primary.parent / "FT-9.json.dupe.1.json"
    assert primary.read_bytes() == before
    assert not (worktree / ".flow" / _NS).exists()


def test_recall_pending_is_workspace_local(tmp_path: Path) -> None:
    main = _make_main(tmp_path)
    worktree = _make_worktree(tmp_path, main, "wt-1")
    main_path = recall_pending.recall_pending_path(main)
    wt_path = recall_pending.recall_pending_path(worktree)
    assert main_path != wt_path
    assert main_path == main / ".flow" / "recall-pending.jsonl"
    assert wt_path == worktree / ".flow" / "recall-pending.jsonl"
