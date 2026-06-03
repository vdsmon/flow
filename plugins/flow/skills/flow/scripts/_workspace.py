"""Shared workspace.toml read + parse.

The file-read + TOML-parse boilerplate was copied across branch_ticket.py,
tracker_cli.py, validate_workspace.py and init.py. This is the one reader. Each
consumer keeps its own `[tracker]` validation and exit-code mapping by catching
WorkspaceConfigError, so per-consumer error contracts are unchanged.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any


class WorkspaceConfigError(Exception):
    """workspace.toml is missing or does not parse."""


def workspace_toml_path(workspace_root: Path) -> Path:
    return workspace_root / ".flow" / "workspace.toml"


def load_workspace_toml(workspace_root: Path) -> dict[str, Any]:
    """Read + parse `.flow/workspace.toml`.

    Raises WorkspaceConfigError if the file is absent or not valid TOML. The
    message wording matches what consumers historically emitted so their existing
    stderr assertions hold.
    """
    path = workspace_toml_path(workspace_root)
    if not path.exists():
        raise WorkspaceConfigError(f"no workspace.toml at {path}")
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise WorkspaceConfigError(f"workspace.toml does not parse: {exc}") from exc


__all__ = ["WorkspaceConfigError", "load_workspace_toml", "workspace_toml_path"]
