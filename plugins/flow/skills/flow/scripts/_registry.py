"""Shared stage-registry.toml loader.

stage-registry.toml is one schema; before this it had four independent parsers
(init.py, validate_workspace.py, dispatch_stage.py, lint_ticket.py) and two
parallel dataclasses. This is the single loader returning one StageEntry that
carries every registry field; each consumer reads the subset it needs.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

_SKILL_PREFIX = "skill:"
_SUBAGENT_PREFIX = "subagent:"

# Charset-strict handler grammar, the workspace.toml validation spec:
#   inline | none | subagent:<type> | skill:<name>[:<args>]
# subagent types and skill names are restricted to safe identifiers; skill args,
# when present, must be non-empty. parse_handler is the lax structural twin used
# on the runtime dispatch path; validate_workspace enforces this charset.
HANDLER_RE = re.compile(r"^(inline|none|subagent:[A-Za-z0-9_-]+|skill:[A-Za-z0-9_.-]+(?::.+)?)$")


@dataclass(frozen=True)
class ParsedHandler:
    kind: str
    name: str = ""
    args: str = ""


def parse_handler(value: str) -> ParsedHandler | None:
    """Structural parse of a handler string, or None when the kind is unknown or
    nothing follows a `subagent:`/`skill:` prefix.

    Lax on charset (that is HANDLER_RE's concern) and on an empty skill name after
    a non-empty `skill:` body: `skill::args` parses as name="", args="args".
    Callers that reject an empty name check `parsed.name` themselves.
    """
    if value in ("inline", "none"):
        return ParsedHandler(kind=value)
    if value.startswith(_SUBAGENT_PREFIX):
        rest = value[len(_SUBAGENT_PREFIX) :]
        if not rest:
            return None
        return ParsedHandler(kind="subagent", name=rest)
    if value.startswith(_SKILL_PREFIX):
        rest = value[len(_SKILL_PREFIX) :]
        if not rest:
            return None
        name, _, args = rest.partition(":")
        return ParsedHandler(kind="skill", name=name, args=args)
    return None


@dataclass(frozen=True)
class StageEntry:
    name: str
    description: str = ""
    default_handler: str = "none"
    default_timeout_min: int = 10
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


__all__ = [
    "HANDLER_RE",
    "ParsedHandler",
    "StageEntry",
    "load_registry",
    "parse_handler",
    "registry_by_name",
]
