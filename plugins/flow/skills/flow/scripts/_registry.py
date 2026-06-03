"""Shared stage-registry.toml loader.

stage-registry.toml is one schema; before this it had four independent parsers
(init.py, validate_workspace.py, dispatch_stage.py, lint_ticket.py) and two
parallel dataclasses. This is the single loader returning one StageEntry that
carries every registry field; each consumer reads the subset it needs.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class StageEntry:
    name: str
    description: str = ""
    default_handler: str = "none"
    default_timeout_min: int = 10
    default_heartbeat_required: bool = False
    default_max_no_progress_min: int = 10
    required_capabilities: list[str] = field(default_factory=list)
    required_predecessors: list[str] = field(default_factory=list)
    required: bool = False
    required_when_compounding: bool = False
    reference_doc: str | None = None
    roles: list[str] = field(default_factory=list)
    required_fields: list[str] = field(default_factory=list)


def _str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def load_registry(path: Path) -> list[StageEntry]:
    """Parse stage-registry.toml into StageEntry records, preserving file order.

    Raises ValueError on a malformed registry (non-array `stage`, non-table
    entry, or an entry missing `name`).
    """
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    stages_raw = data.get("stage", [])
    if not isinstance(stages_raw, list):
        raise ValueError("stage-registry.toml: 'stage' is not an array")
    out: list[StageEntry] = []
    for entry in stages_raw:
        if not isinstance(entry, dict):
            raise ValueError("stage-registry.toml: entry is not a table")
        if "name" not in entry:
            raise ValueError("stage-registry.toml: entry missing 'name'")
        out.append(
            StageEntry(
                name=str(entry["name"]),
                description=str(entry.get("description", "")),
                default_handler=str(entry.get("default_handler", "none")),
                default_timeout_min=int(entry.get("default_timeout_min", 10)),
                default_heartbeat_required=bool(entry.get("default_heartbeat_required", False)),
                default_max_no_progress_min=int(entry.get("default_max_no_progress_min", 10)),
                required_capabilities=_str_list(entry.get("required_capabilities")),
                required_predecessors=_str_list(entry.get("required_predecessors")),
                required=bool(entry.get("required", False)),
                required_when_compounding=bool(entry.get("required_when_compounding", False)),
                reference_doc=entry.get("reference_doc"),
                roles=_str_list(entry.get("roles")),
                required_fields=_str_list(entry.get("required_fields")),
            )
        )
    return out


def registry_by_name(path: Path) -> dict[str, StageEntry]:
    """load_registry as a name -> StageEntry map."""
    return {e.name: e for e in load_registry(path)}


__all__ = ["StageEntry", "load_registry", "registry_by_name"]
