"""Resolve a launch-agent model/effort hint.

Resolution is four steps, in order:

1. the workspace's ``[models]`` entry, when it names this stage/role;
2. an explicit ``off`` / ``none`` / ``""`` there, which means "inherit the session" and
   deliberately SKIPS the registry default — the opt-out is from the default, not just
   from a value;
3. the registry default, ``[stage.agent_defaults.<role>]`` in ``stage-registry.toml``;
4. nothing, which means inherit the session.

Steps 1 and 4 are the pre-existing behaviour, so deleting every default degrades to it.
Step 2 is why "absent" and "off" must not collapse: they resolve differently.

``[models].<stage>`` may be a bare string — one model hint for every agent that stage
launches, carrying no effort — or a table keyed by ROLE
(``[models.code_review].reviewer``), where a role's value is a model string or an inline
table with ``model`` and ``effort``. Each FIELD resolves independently: a bare-string
model does not suppress a registry effort default.

A tier becomes a model name through the harness of whatever LAUNCHES the agent, which is
not necessarily ``FLOW_HARNESS``: under Claude Code, ``code_review`` may be wired to the
bundled Codex reviewer, whose vocabulary is Codex's. Callers that know their launcher pass
``launcher_harness``; otherwise it is derived from the launch kind
(``_registry.LAUNCH_KINDS``). A handler flow cannot classify yields NO default at all,
because injecting a host model name into a third-party Codex launcher would break a
supported configuration that today receives no hint whatsoever.

Flow does not attest which provider or model actually ran.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from _harness import HarnessError, flow_harness
from _registry import (
    CODEX_HANDLERS,
    LAUNCH_HANDLER,
    LAUNCH_KINDS,
    load_registry,
    load_tiers,
    registry_path,
)
from _workspace import load_workspace_toml

OFF_VALUES = frozenset({"", "off", "none", "false"})

FIELDS = ("model", "effort")

CODEX = "codex"


def _clean(value: object) -> str:
    if not isinstance(value, str) or value.strip().lower() in OFF_VALUES:
        return ""
    return value


def _field_value(raw: object, field: str, *, bare_is_model: bool) -> str | None:
    """None when this field is absent, "" when explicitly off, else the value."""
    if bare_is_model and field != "model":
        # A bare model string says nothing about effort, so effort still falls through.
        return None
    return "" if not _clean(raw) else str(raw)


def _workspace_field(models: object, stage: str, role: str, field: str) -> str | None:
    """The workspace's value for this field: None when absent, "" when explicitly off.

    Absent must stay distinguishable from off; the registry default fires only for absent.
    """
    entry = models.get(stage) if isinstance(models, dict) else None
    if entry is None:
        return None
    if isinstance(entry, str):
        return _field_value(entry, field, bare_is_model=True)
    if not isinstance(entry, dict):
        # Malformed but PRESENT. The operator meant something here, so inherit rather than
        # silently substituting a default for a value they got wrong; validate_workspace
        # is what tells them about it.
        return ""
    if not role and len(entry) == 1:
        role = str(next(iter(entry)))
    role_value = entry.get(role) if role else None
    if role_value is None:
        return None
    if isinstance(role_value, str):
        return _field_value(role_value, field, bare_is_model=True)
    if not isinstance(role_value, dict):
        return ""
    if field not in role_value:
        return None
    return _field_value(role_value.get(field), field, bare_is_model=False)


def _parent_harness() -> str:
    try:
        return flow_harness()
    except HarnessError:
        return ""


def _launcher_harness(workspace_root: Path, stage: str, role: str, explicit: str) -> str:
    """The harness of whatever launches this site, or "" when it cannot be classified.

    An explicit value always wins: the dispatcher binds it into the descriptor at
    ``cmd_next``, and re-reading ``[pipeline.handlers]`` at hint time instead would be a
    TOCTOU — the stage already in flight keeps the OLD handler while a reconfigure changes
    what resolution reads.
    """
    if explicit:
        return explicit
    if LAUNCH_KINDS.get(stage, {}).get(role, "") != LAUNCH_HANDLER:
        # NATIVE always runs on the host; CALLER with no explicit value is the documented
        # native fallback. Both are the parent harness.
        return _parent_harness()
    try:
        pipeline = load_workspace_toml(workspace_root).get("pipeline")
        handlers = pipeline.get("handlers") if isinstance(pipeline, dict) else None
        handler = str(handlers.get(stage, "")) if isinstance(handlers, dict) else ""
    except Exception:
        return ""
    if handler in CODEX_HANDLERS:
        return CODEX
    if handler == "inline" or handler.startswith("subagent:"):
        return _parent_harness()
    return ""


# The registry stores a TIER, not a model name; the name is derived per harness. Effort
# needs no such indirection, so it is stored and read under its own name.
_REGISTRY_KEY = {"model": "tier", "effort": "effort"}


def _registry_default(stage: str, role: str, field: str) -> str:
    if not role:
        roles = LAUNCH_KINDS.get(stage, {})
        if len(roles) != 1:
            return ""
        role = next(iter(roles))
    try:
        entry = {e.name: e for e in load_registry(registry_path())}.get(stage)
    except (OSError, ValueError):
        return ""
    if entry is None:
        return ""
    return str(entry.agent_defaults.get(role, {}).get(_REGISTRY_KEY.get(field, field), ""))


def resolve_agent_hint(
    workspace_root: Path,
    stage: str,
    role: str = "",
    field: str = "model",
    *,
    launcher_harness: str = "",
) -> str:
    """Return the hint for ``stage``/``role``, or ``""`` to inherit the session."""
    try:
        models = load_workspace_toml(workspace_root).get("models")
    except Exception:
        # Unreadable workspace inherits, exactly as before this file grew defaults. A
        # registry default here would substitute policy for a workspace nobody could read.
        return ""
    configured = _workspace_field(models, stage, role, field)
    if configured is not None:
        # Includes the explicit opt-out: "" here means inherit, skipping the default.
        return _clean(configured)

    default = _registry_default(stage, role, field)
    if not default or field != "model":
        return default
    harness = _launcher_harness(workspace_root, stage, role, launcher_harness)
    if not harness:
        return ""
    return load_tiers().get(harness, {}).get(default, "")


def cli_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Print the model or effort hint for a stage's agent (empty = inherit session)."
        )
    )
    parser.add_argument("--workspace-root", default=".")
    parser.add_argument("--stage", required=True)
    parser.add_argument("--role", default="", help="the launching role (reviewer, fixer, ...)")
    parser.add_argument("--field", choices=FIELDS, default="model")
    parser.add_argument(
        "--launcher-harness",
        default="",
        help=(
            "harness of the agent about to be launched, when the caller knows it (the "
            "bundled Codex assessor passes 'codex'). NOT FLOW_HARNESS, which names the "
            "host this process runs under."
        ),
    )
    args = parser.parse_args(argv)
    hint = resolve_agent_hint(
        Path(args.workspace_root).expanduser().resolve(),
        args.stage,
        role=args.role,
        field=args.field,
        launcher_harness=args.launcher_harness,
    )
    if hint:
        sys.stdout.write(hint + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = ["CODEX", "FIELDS", "OFF_VALUES", "cli_main", "resolve_agent_hint"]
