"""Runtime layout v2 install/rebind contracts."""

from __future__ import annotations

from pathlib import Path

import pytest

import runtime_layout


def _workspace(root: Path, namespace: str = "flow") -> Path:
    flow = root / ".flow"
    flow.mkdir(parents=True)
    (flow / "workspace.toml").write_text(f'[memory]\nnamespace = "{namespace}"\n', encoding="utf-8")
    return flow


def test_fresh_install_publishes_local_pointer_and_stamp(tmp_path: Path) -> None:
    flow = _workspace(tmp_path, "demo")
    layout = runtime_layout.ensure_layout(tmp_path)

    runtime = flow / "runtime"
    assert (runtime / "layout-version").read_text(encoding="utf-8").strip() == "2"
    assert (runtime / "memory-root").read_text(encoding="utf-8").strip() == ".flow/memory"
    assert layout.memory_base == (flow / "memory").resolve()
    assert layout.memory_base.is_dir()


def test_external_memory_base_publishes_absolute_pointer(tmp_path: Path) -> None:
    flow = _workspace(tmp_path, "demo")
    external = tmp_path / "external" / "memory"
    layout = runtime_layout.ensure_layout(tmp_path, memory_base=external)

    assert layout.memory_base == external.resolve()
    pointer = (flow / "runtime" / "memory-root").read_text(encoding="utf-8").strip()
    assert pointer == str(external.resolve())


def test_workspace_local_memory_pointer_survives_checkout_relocation(tmp_path: Path) -> None:
    old = tmp_path / "old"
    old.mkdir()
    _workspace(old, "demo")
    runtime_layout.ensure_layout(old)
    store = old / ".flow" / "memory" / "demo"
    store.mkdir()
    (store / "knowledge.jsonl").write_text("preserved\n", encoding="utf-8")

    new = tmp_path / "new"
    old.rename(new)
    layout = runtime_layout.ensure_layout(new)

    assert layout.memory_base == (new / ".flow" / "memory").resolve()
    assert (layout.memory_base / "demo" / "knowledge.jsonl").read_text() == "preserved\n"


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


def test_missing_namespace_refuses(tmp_path: Path) -> None:
    flow = tmp_path / ".flow"
    flow.mkdir(parents=True)
    (flow / "workspace.toml").write_text("[memory]\n", encoding="utf-8")

    with pytest.raises(runtime_layout.RuntimeLayoutError, match=r"memory\.namespace"):
        runtime_layout.ensure_layout(tmp_path)


@pytest.mark.parametrize(
    "namespace",
    [
        "runs",
        "runtime",
        "memory",
        "workspace.toml",
        ".hidden",
        "a/b",
        "..",
        "recall-pending.jsonl.bak",
    ],
)
def test_reserved_or_unsafe_namespaces_refuse(namespace: str) -> None:
    with pytest.raises(runtime_layout.RuntimeLayoutError, match="unsafe or reserved"):
        runtime_layout.validate_namespace(namespace)
