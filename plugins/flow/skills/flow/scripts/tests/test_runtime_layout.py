"""Runtime layout v2 migration contracts."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

import runtime_layout

_FLOW_OWNED_V1_ROOTS = {
    "config.toml",
    "e2e-recipes.md",
    "fleet",
    "launch-ledger",
    "memory",
    "memory-root",
    "pending-mutations.jsonl",
    "recall-pending.jsonl",
    "runs",
    "runtime",
    "skill_dir",
    "tickets",
    "workspace.toml",
    "worktrees",
}


def _workspace(root: Path, namespace: str = "flow") -> Path:
    flow = root / ".flow"
    flow.mkdir(parents=True)
    (flow / "workspace.toml").write_text(f'[memory]\nnamespace = "{namespace}"\n', encoding="utf-8")
    return flow


def _manifest(root: Path) -> dict[str, tuple[int, str]]:
    return {
        str(path.relative_to(root)): (
            path.stat().st_size,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_namespace_named_flow_migrates_without_launcher_collision(tmp_path: Path) -> None:
    flow = _workspace(tmp_path, "flow")
    legacy = flow / "flow"
    legacy.mkdir()
    (legacy / "knowledge.jsonl").write_bytes(b'{"fact":"preserved"}\n')
    before = _manifest(legacy)

    layout = runtime_layout.ensure_layout(tmp_path)

    assert layout.memory_base == flow / "memory"
    assert _manifest(flow / "memory" / "flow") == before
    assert not legacy.exists()
    assert (flow / "runtime" / "layout-version").read_text(encoding="utf-8") == "2\n"


def test_nonempty_source_and_destination_conflict_preserves_both(tmp_path: Path) -> None:
    flow = _workspace(tmp_path, "demo")
    source = flow / "demo"
    destination = flow / "memory" / "demo"
    source.mkdir()
    destination.mkdir(parents=True)
    (source / "knowledge.jsonl").write_text("old\n", encoding="utf-8")
    (destination / "knowledge.jsonl").write_text("new\n", encoding="utf-8")

    with pytest.raises(runtime_layout.MemoryConflictError, match="both contain data"):
        runtime_layout.ensure_layout(tmp_path)

    assert (source / "knowledge.jsonl").read_text(encoding="utf-8") == "old\n"
    assert (destination / "knowledge.jsonl").read_text(encoding="utf-8") == "new\n"
    assert not (flow / "runtime" / "layout-version").exists()


def test_live_base_or_revision_lease_refuses_migration(tmp_path: Path) -> None:
    flow = _workspace(tmp_path, "demo")
    source = flow / "demo"
    source.mkdir()
    (source / "knowledge.jsonl").write_text("old\n", encoding="utf-8")
    revision = flow / "runs" / "FT-1" / "revisions" / "001"
    revision.mkdir(parents=True)
    (revision / "run.lock").write_text(
        json.dumps(
            {
                "run_id": "run-1",
                "boot_id": "boot",
                "hostname": "host",
                "cwd": str(tmp_path),
                "acquired_at": "2098-01-01T00:00:00Z",
                "lease_expires_at": "2099-01-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(runtime_layout.LiveLeaseError, match="FT-1/revisions/001"):
        runtime_layout.ensure_layout(tmp_path)

    assert source.is_dir()
    assert not (flow / "memory" / "demo").exists()


def test_interrupted_move_resumes_forward_from_journal(tmp_path: Path) -> None:
    flow = _workspace(tmp_path, "demo")
    source = flow / "demo"
    source.mkdir()
    (source / "knowledge.jsonl").write_text("one\n", encoding="utf-8")
    before = _manifest(source)

    def interrupt(stage: str) -> None:
        if stage == "moved":
            raise RuntimeError("simulated process death")

    with pytest.raises(RuntimeError, match="simulated process death"):
        runtime_layout.ensure_layout(tmp_path, stage_hook=interrupt)

    assert not source.exists()
    assert _manifest(flow / "memory" / "demo") == before
    assert not (flow / "runtime" / "layout-version").exists()

    layout = runtime_layout.ensure_layout(tmp_path)

    assert layout.version == 2
    assert _manifest(flow / "memory" / "demo") == before
    journal = json.loads((flow / "runtime" / "migration-journal.json").read_text(encoding="utf-8"))
    assert journal["stage"] == "complete"
    assert not (flow / "runtime" / "migration-backup").exists()


def test_interrupted_after_version_publish_finishes_cleanup_forward(tmp_path: Path) -> None:
    flow = _workspace(tmp_path, "demo")
    source = flow / "demo"
    source.mkdir()
    (source / "knowledge.jsonl").write_text("one\n", encoding="utf-8")

    def interrupt(stage: str) -> None:
        if stage == "published":
            raise RuntimeError("simulated death after stamp")

    with pytest.raises(RuntimeError, match="after stamp"):
        runtime_layout.ensure_layout(tmp_path, stage_hook=interrupt)

    runtime = flow / "runtime"
    assert (runtime / "layout-version").read_text(encoding="utf-8") == "2\n"
    assert (runtime / "migration-backup" / "demo").is_dir()

    runtime_layout.ensure_layout(tmp_path)

    assert not (runtime / "migration-backup").exists()
    journal = json.loads((runtime / "migration-journal.json").read_text(encoding="utf-8"))
    assert journal["stage"] == "complete"
    assert (flow / "memory" / "demo" / "knowledge.jsonl").read_text() == "one\n"


def test_empty_destination_is_replaced_by_legacy_store(tmp_path: Path) -> None:
    flow = _workspace(tmp_path, "demo")
    source = flow / "demo"
    destination = flow / "memory" / "demo"
    source.mkdir()
    destination.mkdir(parents=True)
    (source / "friction.jsonl").write_text("entry\n", encoding="utf-8")

    runtime_layout.ensure_layout(tmp_path)

    assert not source.exists()
    assert (destination / "friction.jsonl").read_text(encoding="utf-8") == "entry\n"


def test_fresh_layout_has_no_legacy_metadata(tmp_path: Path) -> None:
    flow = _workspace(tmp_path, "demo")

    runtime_layout.ensure_layout(tmp_path)

    runtime = flow / "runtime"
    assert (runtime / "memory-root").read_text(encoding="utf-8").strip() == ".flow/memory"
    assert not (flow / "memory-root").exists()
    assert not (flow / "skill_dir").exists()


def test_workspace_local_memory_pointer_survives_checkout_relocation(tmp_path: Path) -> None:
    old = tmp_path / "old"
    old.mkdir()
    flow = _workspace(old, "demo")
    legacy = flow / "demo"
    legacy.mkdir()
    (legacy / "knowledge.jsonl").write_text("preserved\n", encoding="utf-8")
    runtime_layout.ensure_layout(old)

    new = tmp_path / "new"
    old.rename(new)
    layout = runtime_layout.ensure_layout(new)

    assert layout.memory_base == (new / ".flow" / "memory").resolve()
    assert (layout.memory_base / "demo" / "knowledge.jsonl").read_text() == "preserved\n"
    assert not (old / ".flow" / "memory").exists()


def test_v2_missing_memory_pointer_fails_closed(tmp_path: Path) -> None:
    flow = _workspace(tmp_path, "demo")
    runtime_layout.ensure_layout(tmp_path)
    pointer = flow / "runtime" / "memory-root"
    pointer.unlink()

    with pytest.raises(runtime_layout.RuntimeLayoutError, match="cannot read layout-v2"):
        runtime_layout.ensure_layout(tmp_path)

    assert not pointer.exists()


def test_v2_missing_external_memory_root_is_not_recreated(tmp_path: Path) -> None:
    _workspace(tmp_path, "demo")
    external = tmp_path / "external" / "memory"
    runtime_layout.ensure_layout(tmp_path, memory_base=external)
    external.rmdir()

    with pytest.raises(runtime_layout.RuntimeLayoutError, match="refusing to create"):
        runtime_layout.ensure_layout(tmp_path)

    assert not external.exists()


def test_v2_rebind_refuses_to_hide_existing_namespace(tmp_path: Path) -> None:
    flow = _workspace(tmp_path, "demo")
    runtime_layout.ensure_layout(tmp_path)
    (flow / "memory" / "demo").mkdir()
    (flow / "memory" / "demo" / "knowledge.jsonl").write_text("fact\n", encoding="utf-8")
    replacement = tmp_path / "replacement"
    replacement.mkdir()

    with pytest.raises(runtime_layout.MemoryConflictError, match="refusing to rebind"):
        runtime_layout.ensure_layout(tmp_path, memory_base=replacement)


def test_linked_worktree_lease_on_shared_legacy_base_blocks_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main = tmp_path / "main"
    worktree = tmp_path / "worktree"
    main.mkdir()
    worktree.mkdir()
    flow = _workspace(main, "demo")
    source = flow / "demo"
    source.mkdir()
    (source / "knowledge.jsonl").write_text("old\n", encoding="utf-8")
    _workspace(worktree, "demo")
    (worktree / ".flow" / "memory-root").write_text(str(flow) + "\n", encoding="utf-8")
    revision = worktree / ".flow" / "runs" / "FT-1" / "revisions" / "002"
    revision.mkdir(parents=True)
    (revision / "run.lock").write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(runtime_layout, "_linked_worktree_roots", lambda _root: [main, worktree])

    with pytest.raises(runtime_layout.LiveLeaseError, match=r"worktree.*FT-1/revisions/002"):
        runtime_layout.ensure_layout(main)

    assert source.is_dir()
    assert not (flow / "memory" / "demo").exists()


def test_linked_worktree_discovery_reads_git_porcelain_paths(tmp_path: Path) -> None:
    main = tmp_path / "main"
    sibling = tmp_path / "sibling with spaces"
    main.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=main, check=True)
    (main / "tracked").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked"], cwd=main, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Flow Test",
            "-c",
            "user.email=flow@example.invalid",
            "commit",
            "-qm",
            "seed",
        ],
        cwd=main,
        check=True,
    )
    subprocess.run(["git", "worktree", "add", "-qb", "sibling", str(sibling)], cwd=main, check=True)

    assert set(runtime_layout._linked_worktree_roots(main.resolve())) == {
        main.resolve(),
        sibling.resolve(),
    }


def test_corrupt_linked_worktree_memory_pointer_blocks_migration_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main = tmp_path / "main"
    worktree = tmp_path / "worktree"
    main.mkdir()
    worktree.mkdir()
    flow = _workspace(main, "demo")
    (flow / "demo").mkdir()
    _workspace(worktree, "demo")
    (worktree / ".flow" / "memory-root").write_text("relative/path\n", encoding="utf-8")
    monkeypatch.setattr(runtime_layout, "_linked_worktree_roots", lambda _root: [main, worktree])

    with pytest.raises(runtime_layout.LiveLeaseError, match="invalid pointer"):
        runtime_layout.ensure_layout(main)


def test_path_corrupt_journal_cannot_delete_outside_runtime(tmp_path: Path) -> None:
    flow = _workspace(tmp_path, "demo")
    runtime = flow / "runtime"
    runtime.mkdir()
    memory = flow / "memory"
    (memory / "demo").mkdir(parents=True)
    (runtime / "layout-version").write_text("2\n", encoding="utf-8")
    (runtime / "memory-root").write_text(".flow/memory\n", encoding="utf-8")
    victim = tmp_path / "must-survive"
    victim.mkdir()
    (victim / "data").write_text("safe\n", encoding="utf-8")
    journal = {
        "version": 2,
        "stage": "published",
        "workspace_root": str(tmp_path.resolve()),
        "namespace": "demo",
        "source": str((flow / "demo").resolve()),
        "destination": str((memory / "demo").resolve()),
        "destination_base": str(memory.resolve()),
        "backup": str(victim.resolve()),
        "manifest": {"files": [], "directories": []},
    }
    (runtime / "migration-journal.json").write_text(json.dumps(journal), encoding="utf-8")

    with pytest.raises(runtime_layout.RuntimeLayoutError, match="backup escapes"):
        runtime_layout.ensure_layout(tmp_path)

    assert (victim / "data").read_text(encoding="utf-8") == "safe\n"


def test_reserved_namespaces_cover_every_flow_owned_v1_root() -> None:
    assert runtime_layout._RESERVED_NAMESPACES == _FLOW_OWNED_V1_ROOTS


@pytest.mark.parametrize(
    "namespace",
    sorted(
        _FLOW_OWNED_V1_ROOTS
        | {
            "MEMORY",
            "pending-mutations.jsonl.lock",
            "pending-mutations.jsonl.quarantine",
            "recall-pending.jsonl.lock",
            "recall-pending.jsonl.quarantine",
            "recall-pending.jsonl.stale",
        }
    ),
)
def test_reserved_namespace_is_rejected_before_layout_mutation(
    tmp_path: Path, namespace: str
) -> None:
    _workspace(tmp_path, namespace)

    with pytest.raises(runtime_layout.RuntimeLayoutError, match="reserved memory namespace"):
        runtime_layout.ensure_layout(tmp_path)

    assert not (tmp_path / ".flow" / "memory").exists()
    assert not (tmp_path / ".flow" / "runtime").exists()


@pytest.mark.parametrize("namespace", ["tickets", "fleet"])
def test_flow_owned_v1_state_is_never_migrated_as_memory(tmp_path: Path, namespace: str) -> None:
    flow = _workspace(tmp_path, namespace)
    owned = flow / namespace
    owned.mkdir()
    sentinel = owned / "flow-owned-state.json"
    sentinel.write_text('{"preserve":true}\n', encoding="utf-8")

    with pytest.raises(runtime_layout.RuntimeLayoutError, match="reserved memory namespace"):
        runtime_layout.ensure_layout(tmp_path)

    assert sentinel.read_text(encoding="utf-8") == '{"preserve":true}\n'
    assert not (flow / "memory" / namespace).exists()
