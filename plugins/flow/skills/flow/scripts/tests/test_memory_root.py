"""Tests for the shared external memory store (`[memory].root`).

A git-worktree run has cwd = the worktree, so without a shared root every per-ticket
worktree would get its own `.flow/<ns>/` store and the compounding-knowledge layer
would fragment. `[memory].root` redirects the store to one stable absolute path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import _memory_paths
import init as init_mod


def _write_workspace(
    root: Path, *, namespace: str = "demo", memory_root: str | None = None
) -> None:
    flow = root / ".flow"
    flow.mkdir(parents=True, exist_ok=True)
    lines = [
        "[tracker]",
        'backend = "jira"',
        "[tracker.jira]",
        'cloud_id = "x"',
        'project_key = "FT"',
        "[memory]",
        f'namespace = "{namespace}"',
    ]
    if memory_root is not None:
        lines.append(f'root = "{memory_root}"')
    (flow / "workspace.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_sibling(root: Path, text: str) -> None:
    flow = root / ".flow"
    flow.mkdir(parents=True, exist_ok=True)
    (flow / "memory-root").write_text(text, encoding="utf-8")


def _stamp_v2(root: Path, memory_base: Path) -> None:
    runtime = root / ".flow" / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    memory_base.mkdir(parents=True, exist_ok=True)
    (runtime / "layout-version").write_text("2\n", encoding="utf-8")
    (runtime / "memory-root").write_text(str(memory_base) + "\n", encoding="utf-8")


def test_v2_namespace_is_below_dedicated_memory_directory(tmp_path: Path) -> None:
    _write_workspace(tmp_path)
    memory_base = tmp_path / ".flow" / "memory"
    _stamp_v2(tmp_path, memory_base)

    assert _memory_paths.resolve_memory_base(tmp_path) == memory_base
    assert _memory_paths.namespace_root(tmp_path, "demo") == memory_base / "demo"
    assert _memory_paths.knowledge_path(tmp_path, "demo") == (
        memory_base / "demo" / "knowledge.jsonl"
    )


def test_v2_local_pointer_is_workspace_relative(tmp_path: Path) -> None:
    _write_workspace(tmp_path)
    runtime = tmp_path / ".flow" / "runtime"
    runtime.mkdir(parents=True)
    (tmp_path / ".flow" / "memory").mkdir()
    (runtime / "layout-version").write_text("2\n", encoding="utf-8")
    (runtime / "memory-root").write_text(".flow/memory\n", encoding="utf-8")

    assert _memory_paths.resolve_memory_base(tmp_path) == tmp_path / ".flow" / "memory"


def test_v2_missing_pointer_does_not_fall_back_to_empty_local_store(tmp_path: Path) -> None:
    _write_workspace(tmp_path)
    runtime = tmp_path / ".flow" / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "layout-version").write_text("2\n", encoding="utf-8")

    with pytest.raises(_memory_paths._MemoryConfigError, match="cannot read layout-v2"):
        _memory_paths.resolve_memory_base(tmp_path)


@pytest.mark.parametrize("local", [True, False])
def test_v2_missing_selected_root_does_not_fall_through_or_recreate(
    tmp_path: Path, local: bool
) -> None:
    _write_workspace(tmp_path)
    runtime = tmp_path / ".flow" / "runtime"
    runtime.mkdir(parents=True)
    selected = tmp_path / ".flow" / "memory" if local else tmp_path / "external-memory"
    pointer = ".flow/memory" if local else str(selected)
    (runtime / "layout-version").write_text("2\n", encoding="utf-8")
    (runtime / "memory-root").write_text(pointer + "\n", encoding="utf-8")

    with pytest.raises(_memory_paths._MemoryConfigError, match="missing or not a directory"):
        _memory_paths.resolve_memory_base(tmp_path)

    assert not selected.exists()


def test_v2_runtime_pointer_wins_over_legacy_sources(tmp_path: Path) -> None:
    legacy_toml = tmp_path / "toml-store" / ".flow"
    legacy_sibling = tmp_path / "sibling-store" / ".flow"
    v2_base = tmp_path / "main" / ".flow" / "memory"
    _write_workspace(tmp_path, memory_root=str(legacy_toml))
    _write_sibling(tmp_path, str(legacy_sibling) + "\n")
    _stamp_v2(tmp_path, v2_base)

    assert _memory_paths.resolve_memory_base(tmp_path) == v2_base


def _memory_data(root: object) -> dict:
    mem: dict[str, object] = {
        "namespace": "demo",
        "compounding": True,
    }
    if root is not None:
        mem["root"] = root
    return {"memory": mem}


def _init_config(tmp_path: Path):
    return init_mod.InitConfig(
        backend="jira",
        bundle="bare",
        workspace_root=tmp_path,
        jira=init_mod.JiraConfig(cloud_id="x", project_key="FT", assignee_account_id=None),
    )


def test_render_never_writes_root(tmp_path: Path) -> None:
    # init never writes [memory].root; the worktree share rides the gitignored
    # .flow/memory-root sibling. The read path (resolve_memory_base) still
    # honors a hand-set root, covered above.
    toml = init_mod._render_workspace_toml(
        _init_config(tmp_path), "demo", ["ticket"], {"ticket": "inline"}
    )
    assert "root =" not in toml


def test_unstamped_workspace_refuses(tmp_path: Path) -> None:
    (tmp_path / ".flow").mkdir(parents=True)
    with pytest.raises(_memory_paths._MemoryConfigError, match="no layout-v2 stamp"):
        _memory_paths.resolve_memory_base(tmp_path)
