"""Collision-proof Flow runtime layout (v2): install and rebind, no migration.

Layout v2 separates executable metadata from durable memory::

    .flow/runtime/{flow,skill-root,memory-root,layout-version}
    .flow/memory/<namespace>/

Every workspace on this machine is v2; an unstamped workspace is a fresh
install, never a candidate for data migration.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from _atomicio import atomic_write_text

LAYOUT_VERSION = 2
_LOCAL_MEMORY_ROOT = ".flow/memory"
# Every non-hidden first component Flow owns directly under a `.flow/` root.
# `flow` is intentionally absent: the launcher lives under runtime/ in v2.
_RESERVED_NAMESPACES = frozenset(
    {
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
        "tickets",
        "workspace.toml",
    }
)
_RESERVED_NAMESPACE_PREFIXES = ("pending-mutations.jsonl.", "recall-pending.jsonl.")


class RuntimeLayoutError(RuntimeError):
    """Base for safe layout refusals."""


class MemoryConflictError(RuntimeLayoutError):
    """A rebind would orphan a namespace that already contains data."""


@dataclass(frozen=True)
class RuntimeLayout:
    workspace_root: Path
    flow_dir: Path
    runtime_dir: Path
    launcher: Path
    skill_root_file: Path
    memory_root_file: Path
    version_file: Path
    memory_base: Path
    version: int = LAYOUT_VERSION


def _runtime_dir(workspace_root: Path) -> Path:
    return workspace_root / ".flow" / "runtime"


def is_v2(workspace_root: Path) -> bool:
    """Return whether the workspace carries a valid layout-v2 stamp."""
    try:
        return int((_runtime_dir(workspace_root) / "layout-version").read_text().strip()) == 2
    except (OSError, ValueError):
        return False


def _workspace_data(root: Path) -> dict[str, Any]:
    try:
        data = tomllib.loads((root / ".flow" / "workspace.toml").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _namespace(root: Path) -> str:
    memory = _workspace_data(root).get("memory")
    namespace = memory.get("namespace") if isinstance(memory, dict) else None
    if not isinstance(namespace, str) or not namespace:
        raise RuntimeLayoutError("workspace.toml missing or empty memory.namespace")
    return validate_namespace(namespace)


def validate_namespace(namespace: str) -> str:
    """Return a path-safe namespace that cannot collide with Flow-owned roots."""
    if (
        not namespace
        or namespace in {".", ".."}
        or namespace.startswith(".")
        or Path(namespace).name != namespace
        or namespace.casefold() in _RESERVED_NAMESPACES
        or namespace.casefold().startswith(_RESERVED_NAMESPACE_PREFIXES)
    ):
        raise RuntimeLayoutError(
            f"unsafe or reserved memory namespace {namespace!r}; choose a project-specific name"
        )
    return namespace


def _read_v2_memory_base(root: Path) -> Path:
    """Read a v2 pointer without guessing past missing or malformed metadata."""
    path = _runtime_dir(root) / "memory-root"
    try:
        raw = path.read_text(encoding="utf-8").strip("\r\n")
    except (OSError, UnicodeError) as exc:
        raise RuntimeLayoutError(f"cannot read layout-v2 memory root at {path}: {exc}") from exc
    if raw == _LOCAL_MEMORY_ROOT:
        return (root / ".flow" / "memory").resolve()
    if not raw:
        raise RuntimeLayoutError(f"layout-v2 memory root at {path} is empty")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise RuntimeLayoutError(
            f"layout-v2 memory root at {path} must be {_LOCAL_MEMORY_ROOT!r} or an absolute path"
        )
    return candidate.resolve()


def _tree_nonempty(path: Path) -> bool:
    if not path.exists():
        return False
    if not path.is_dir():
        return True
    return next(path.iterdir(), None) is not None


def _layout(root: Path, memory_base: Path) -> RuntimeLayout:
    flow = root / ".flow"
    runtime = flow / "runtime"
    return RuntimeLayout(
        workspace_root=root,
        flow_dir=flow,
        runtime_dir=runtime,
        launcher=runtime / "flow",
        skill_root_file=runtime / "skill-root",
        memory_root_file=runtime / "memory-root",
        version_file=runtime / "layout-version",
        memory_base=memory_base,
    )


def _publish_layout(layout: RuntimeLayout, *, create_memory: bool = True) -> None:
    if create_memory:
        layout.memory_base.mkdir(parents=True, exist_ok=True)
    local = (layout.flow_dir / "memory").resolve()
    pointer = (
        _LOCAL_MEMORY_ROOT
        if layout.memory_base.resolve() == local
        else str(layout.memory_base.resolve())
    )
    atomic_write_text(layout.memory_root_file, pointer + "\n")
    atomic_write_text(layout.version_file, f"{LAYOUT_VERSION}\n")


def ensure_layout(workspace_root: Path, *, memory_base: Path | None = None) -> RuntimeLayout:
    """Install layout v2 (or refresh its pointers) and return its resolved paths.

    ``memory_base`` is the base directory containing namespaces and is used by
    worktree bootstrap to bind directly to the main workspace's store.
    """
    root = workspace_root.expanduser().resolve()
    namespace = _namespace(root)
    flow = root / ".flow"
    flow.mkdir(parents=True, exist_ok=True)
    runtime = flow / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)

    if is_v2(root):
        recorded = _read_v2_memory_base(root)
        selected = memory_base.expanduser().resolve() if memory_base is not None else recorded
        if selected != recorded and _tree_nonempty(recorded / namespace):
            raise MemoryConflictError(
                f"refusing to rebind layout-v2 memory from {recorded} to {selected}; "
                f"the existing {namespace!r} namespace contains data"
            )
        if not selected.is_dir():
            raise RuntimeLayoutError(
                f"layout-v2 memory root is missing or not a directory: {selected}; "
                "refusing to create a replacement that could hide existing memory"
            )
        layout = _layout(root, selected)
        _publish_layout(layout, create_memory=False)
        return layout

    selected_base = (
        memory_base.expanduser().resolve()
        if memory_base is not None
        else (flow / "memory").resolve()
    )
    layout = _layout(root, selected_base)
    _publish_layout(layout)
    return layout


__all__ = [
    "LAYOUT_VERSION",
    "MemoryConflictError",
    "RuntimeLayout",
    "RuntimeLayoutError",
    "ensure_layout",
    "is_v2",
    "validate_namespace",
]
