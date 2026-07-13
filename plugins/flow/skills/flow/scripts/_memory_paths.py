"""Shared path + namespace helpers for the memory cohort.

Avoids duplicating workspace.toml parsing across memory_append / recall /
reflect_inputs / observe_ship_event.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

_LOCAL_V2_MEMORY_ROOT = ".flow/memory"


class _MemoryConfigError(Exception):
    """Raised when workspace.toml is missing or lacks [memory] namespace."""


def resolve_namespace(workspace_root: Path) -> str:
    """Read `.flow/workspace.toml` [memory] namespace.

    Raises `_MemoryConfigError` if workspace.toml missing or malformed.
    """
    path = workspace_root / ".flow" / "workspace.toml"
    if not path.exists():
        raise _MemoryConfigError(f"no workspace.toml at {path}")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise _MemoryConfigError(f"workspace.toml does not parse: {exc}") from exc
    memory = data.get("memory")
    if not isinstance(memory, dict):
        raise _MemoryConfigError("workspace.toml missing [memory] block")
    namespace = memory.get("namespace")
    if not isinstance(namespace, str) or not namespace:
        raise _MemoryConfigError("workspace.toml missing or empty memory.namespace")
    return namespace


def resolve_memory_base(workspace_root: Path) -> Path:
    """Resolve the directory whose direct children are memory namespaces.

    Resolution order, most specific first:
      1. Layout v2's `.flow/runtime/memory-root`, which points at a dedicated
         memory base such as the main checkout's `.flow/memory`.
      2. For an unstamped v1 workspace, `.flow/memory-root`.
      3. For an unstamped v1 workspace, `[memory].root`.
      4. The v1 workspace-local `.flow` fallback.

    Keeping the final three branches until the migration gate runs lets every
    reader see legacy data before it is atomically moved. Newly initialized and
    migrated workspaces are stamped v2 and never consult those paths.
    """
    runtime = workspace_root / ".flow" / "runtime"
    try:
        version = (runtime / "layout-version").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        version = ""
    if version == "2":
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

    sibling = workspace_root / ".flow" / "memory-root"
    try:
        text = sibling.read_text(encoding="utf-8").strip()
    except OSError:
        text = ""
    if text:
        return Path(text).expanduser()

    path = workspace_root / ".flow" / "workspace.toml"
    if path.exists():
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError:
            data = {}
        memory = data.get("memory")
        if isinstance(memory, dict):
            root = memory.get("root")
            if isinstance(root, str) and root:
                return Path(root).expanduser()
    return workspace_root / ".flow"


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
    path = workspace_root / ".flow" / "workspace.toml"
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
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
