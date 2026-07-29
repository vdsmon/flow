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

_SUBAGENT_PREFIX = "subagent:"

# Charset-strict handler grammar, the workspace.toml validation spec:
#   inline | none | subagent:<type> | subagent:<plugin>:<type>
# subagent types are restricted to safe identifiers. parse_handler is the lax
# structural twin used on the runtime dispatch path; validate_workspace enforces
# this charset.
#
# The single optional colon in a subagent type carries a plugin namespace, the form
# a host uses for an agent shipped by a plugin (`flow:codex-reviewer`). It stays an
# identifier charset with no shell metacharacters: workspace.toml names an agent, never
# a command. That matters because .flow/workspace.toml can ride in planned_files, the
# ownership gate excludes .flow/, and the drift gate reconciles owned drift mid-run, so
# a command-valued handler would let one stage write what a later stage executes.
HANDLER_RE = re.compile(r"^(inline|none|subagent:[A-Za-z0-9_-]+(?::[A-Za-z0-9_-]+)?)$")


@dataclass(frozen=True)
class ParsedHandler:
    kind: str
    name: str = ""
    args: str = ""


def parse_handler(value: str) -> ParsedHandler | None:
    """Structural parse of a handler string, or None when the kind is unknown or
    nothing follows a `subagent:` prefix.

    Lax on charset; that is HANDLER_RE's concern.
    """
    if value in ("inline", "none"):
        return ParsedHandler(kind=value)
    if value.startswith(_SUBAGENT_PREFIX):
        rest = value[len(_SUBAGENT_PREFIX) :]
        if not rest:
            return None
        return ParsedHandler(kind="subagent", name=rest)
    return None


# Which engine launches each site's agent. `roles` (above) is a different concept
# entirely — dispatch markers like `records_diff_baseline` — and the two must never be
# conflated; see `agent_defaults` on StageEntry.
#   handler — the workspace's `[pipeline.handlers][stage]` IS this agent
#   native  — always a fresh host-native agent, whatever the stage handler is
#   caller  — chosen at runtime with a documented fallback, so only the caller knows
LAUNCH_HANDLER = "handler"
LAUNCH_NATIVE = "native"
LAUNCH_CALLER = "caller"

LAUNCH_KINDS: dict[str, dict[str, str]] = {
    "plan": {"assessor": LAUNCH_CALLER},
    "implement": {"implementer": LAUNCH_HANDLER},
    "code_review": {"reviewer": LAUNCH_HANDLER, "fixer": LAUNCH_NATIVE},
    "e2e": {"runner": LAUNCH_HANDLER},
    "review_loop": {"fixer": LAUNCH_NATIVE},
    "review_brief": {"author": LAUNCH_NATIVE},
}

# Handlers that shell out to Codex, so their model vocabulary is Codex's rather than the
# host's. `codex-assessor` also shells to Codex but is never a handler; it is the one
# LAUNCH_CALLER site. A handler outside this set and outside the flow-owned native agents
# is UNKNOWN: it gets no registry default at all (see model_resolve), because injecting a
# host model name into a third-party Codex launcher would break a supported configuration
# that today receives no hint whatsoever.
CODEX_HANDLERS = frozenset({"subagent:flow:codex-reviewer"})

TIERS = ("standard", "deep")


@dataclass(frozen=True)
class StageEntry:
    name: str
    description: str = ""
    default_handler: str = "none"
    default_timeout_min: int = 10
    required_predecessors: list[str] = field(default_factory=list)
    required_when_compounding: bool = False
    reference_doc: str | None = None
    roles: list[str] = field(default_factory=list)
    required_fields: list[str] = field(default_factory=list)
    # Per-launch-role `{tier, effort}` defaults. Named `agent_defaults`, never `roles`:
    # `roles` is taken and load-bearing (dispatch markers), and TOML rejects a table that
    # collides with an existing array anyway.
    agent_defaults: dict[str, dict[str, str]] = field(default_factory=dict)


def registry_path() -> Path:
    """The shipped stage-registry.toml beside this scripts directory."""
    return Path(__file__).resolve().parent.parent / "stage-registry.toml"


def load_tiers(path: Path | None = None) -> dict[str, dict[str, str]]:
    """Parse the `[tiers.<harness>]` model maps. Missing or malformed -> {}."""
    try:
        data = tomllib.loads((path or registry_path()).read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    tiers = data.get("tiers")
    if not isinstance(tiers, dict):
        return {}
    return {
        str(harness): {str(k): str(v) for k, v in table.items() if isinstance(v, str)}
        for harness, table in tiers.items()
        if isinstance(table, dict)
    }


def _str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _agent_defaults(value: object) -> dict[str, dict[str, str]]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, dict[str, str]] = {}
    for role, table in value.items():
        if isinstance(table, dict):
            out[str(role)] = {str(k): str(v) for k, v in table.items() if isinstance(v, str)}
    return out


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
                required_when_compounding=bool(entry.get("required_when_compounding", False)),
                reference_doc=entry.get("reference_doc"),
                roles=_str_list(entry.get("roles")),
                required_fields=_str_list(entry.get("required_fields")),
                agent_defaults=_agent_defaults(entry.get("agent_defaults")),
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
