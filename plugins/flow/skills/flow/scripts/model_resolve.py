"""Resolve an optional native-agent hint from ``[models]``.

``[models].<stage>`` may be a bare string — one model hint for every agent that
stage launches — or a table keyed by ROLE (``[models.code_review].reviewer``),
where each role's value is a model string or an inline table with ``model`` and
``effort``. Missing, disabled, or unreadable configuration means "inherit the
driver session model". Flow does not attest which provider or model actually ran;
the vocabulary of a value belongs to whatever launches the agent (a host model
name for a native agent, a reviewer-CLI model name for a bundled reviewer).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from _workspace import load_workspace_toml

OFF_VALUES = frozenset({"", "off", "none", "false"})

FIELDS = ("model", "effort")


def _clean(value: object) -> str:
    if not isinstance(value, str) or value.strip().lower() in OFF_VALUES:
        return ""
    return value


def resolve_agent_hint(
    workspace_root: Path, stage: str, role: str = "", field: str = "model"
) -> str:
    """Return the configured hint for ``stage``/``role``, or ``""`` to inherit.

    A bare-string stage entry is the explicit stage-wide model hint: it applies
    to every role and carries no effort. A table entry yields hints for the roles
    it names; when the caller passes no role and the table names exactly one, that
    one applies — the generic launch recipe carries no role, so a single-role
    table must not silently resolve to nothing. Two or more roles need the caller
    to say which.
    """
    try:
        models = load_workspace_toml(workspace_root).get("models")
        entry = models.get(stage) if isinstance(models, dict) else None
        if isinstance(entry, str):
            return _clean(entry) if field == "model" else ""
        if not isinstance(entry, dict):
            return ""
        if not role and len(entry) == 1:
            role = str(next(iter(entry)))
        role_value = entry.get(role) if role else None
        if isinstance(role_value, str):
            return _clean(role_value) if field == "model" else ""
        if isinstance(role_value, dict):
            return _clean(role_value.get(field))
        return ""
    except Exception:
        return ""


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
    args = parser.parse_args(argv)
    hint = resolve_agent_hint(
        Path(args.workspace_root).expanduser().resolve(),
        args.stage,
        role=args.role,
        field=args.field,
    )
    if hint:
        sys.stdout.write(hint + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = ["FIELDS", "OFF_VALUES", "cli_main", "resolve_agent_hint"]
