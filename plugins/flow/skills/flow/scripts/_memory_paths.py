"""Shared path + namespace helpers for the memory cohort.

Avoids duplicating workspace.toml parsing across memory_append / recall /
reflect_inputs / observe_ship_event.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import _workspace

_LOCAL_V2_MEMORY_ROOT = ".flow/memory"


class _MemoryConfigError(Exception):
    """Raised when workspace.toml is missing or lacks [memory] namespace."""


def resolve_namespace(workspace_root: Path) -> str:
    """Read `.flow/workspace.toml` [memory] namespace.

    Raises `_MemoryConfigError` if workspace.toml missing or malformed.
    """
    try:
        data = _workspace.load_workspace_toml(workspace_root)
    except _workspace.WorkspaceConfigError as exc:
        raise _MemoryConfigError(str(exc)) from exc
    memory = data.get("memory")
    if not isinstance(memory, dict):
        raise _MemoryConfigError("workspace.toml missing [memory] block")
    namespace = memory.get("namespace")
    if not isinstance(namespace, str) or not namespace:
        raise _MemoryConfigError("workspace.toml missing or empty memory.namespace")
    return namespace


def resolve_memory_base(workspace_root: Path) -> Path:
    """Resolve the directory whose direct children are memory namespaces.

    Layout v2's `.flow/runtime/memory-root` is the only source: it points at a
    dedicated memory base such as the main checkout's `.flow/memory`. An
    unstamped or unreadable layout fails closed.
    """
    runtime = workspace_root / ".flow" / "runtime"
    try:
        version = (runtime / "layout-version").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        version = ""
    if version != "2":
        raise _MemoryConfigError(
            f"workspace at {workspace_root} carries no layout-v2 stamp; run Flow workspace setup"
        )
    try:
        text = (runtime / "memory-root").read_text(encoding="utf-8").strip("\r\n")
    except (OSError, UnicodeError) as exc:
        raise _MemoryConfigError(
            f"cannot read layout-v2 memory root at {runtime / 'memory-root'}: {exc}"
        ) from exc
    if text == _LOCAL_V2_MEMORY_ROOT:
        selected = workspace_root / ".flow" / "memory"
    else:
        selected = Path(text).expanduser()
    if not text or (text != _LOCAL_V2_MEMORY_ROOT and not selected.is_absolute()):
        raise _MemoryConfigError(
            f"layout-v2 memory root must be {_LOCAL_V2_MEMORY_ROOT!r} or an absolute path"
        )
    if not selected.is_dir():
        raise _MemoryConfigError(
            f"layout-v2 memory root is missing or not a directory: {selected}; "
            "refusing to create a replacement that could hide existing memory"
        )
    return selected


def namespace_root(workspace_root: Path, namespace: str) -> Path:
    return resolve_memory_base(workspace_root) / namespace


def knowledge_path(workspace_root: Path, namespace: str) -> Path:
    return namespace_root(workspace_root, namespace) / "knowledge.jsonl"


def knowledge_lock_path(workspace_root: Path, namespace: str) -> Path:
    return namespace_root(workspace_root, namespace) / "knowledge.jsonl.lock"


def friction_path(workspace_root: Path, namespace: str) -> Path:
    return namespace_root(workspace_root, namespace) / "friction.jsonl"


def load_semantic_config(workspace_root: Path) -> dict[str, Any]:
    """Read `[memory.semantic]` from workspace.toml. Absent block -> {} (semantic off).

    Keys: `enabled` (bool), `model` (str), `threshold` (float), `embedder` (str).
    Any read/parse error returns {} so callers stay on the BM25 path.
    """
    try:
        data = _workspace.load_workspace_toml(workspace_root)
    except _workspace.WorkspaceConfigError:
        return {}
    memory = data.get("memory")
    if not isinstance(memory, dict):
        return {}
    semantic = memory.get("semantic")
    return semantic if isinstance(semantic, dict) else {}


def friction_lock_path(workspace_root: Path, namespace: str) -> Path:
    return namespace_root(workspace_root, namespace) / "friction.jsonl.lock"


def ship_events_dir(workspace_root: Path, namespace: str) -> Path:
    return namespace_root(workspace_root, namespace) / "ship-events"


def ship_event_path(workspace_root: Path, namespace: str, ticket: str) -> Path:
    return ship_events_dir(workspace_root, namespace) / f"{ticket}.json"


def revert_events_dir(workspace_root: Path, namespace: str) -> Path:
    return namespace_root(workspace_root, namespace) / "revert-events"


def revert_event_path(workspace_root: Path, namespace: str, reverting_sha: str) -> Path:
    return revert_events_dir(workspace_root, namespace) / f"{reverting_sha}.json"
