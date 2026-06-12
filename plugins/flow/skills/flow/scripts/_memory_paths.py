"""Shared path + namespace helpers for the memory cohort.

Avoids duplicating workspace.toml parsing across memory_append / recall /
reflect_inputs / observe_ship_event.
"""

from __future__ import annotations

import tomllib
from pathlib import Path


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
    """Resolve the base dir that holds the memory store (the `.flow` to write under).

    Resolution order, most specific first:
      1. `.flow/memory-root` (gitignored sibling, plain text single abs path): the
         worktree bootstrap writes it to redirect the store to the shared (main)
         `.flow` without touching the tracked workspace.toml.
      2. `.flow/workspace.toml` [memory].root when set (the init-time render path).
      3. the workspace-local `.flow` (non-worktree runs stay byte-identical).
    """
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
