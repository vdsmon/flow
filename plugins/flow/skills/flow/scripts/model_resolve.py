"""Resolve an optional native-agent model hint from ``[models]``.

``[models].<stage>`` may name a model for a stage that launches a fresh native
agent. Missing, disabled, or unreadable configuration means "inherit the owner
session model". Flow does not attest which provider or model actually ran.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from _workspace import load_workspace_toml

OFF_VALUES = frozenset({"", "off", "none", "false"})


def resolve_stage_model(workspace_root: Path, stage: str) -> str:
    """Return the configured model hint for ``stage``, or ``""`` to inherit."""
    try:
        models = load_workspace_toml(workspace_root).get("models")
        model = models.get(stage) if isinstance(models, dict) else None
        if not isinstance(model, str):
            return ""
        if model.strip().lower() in OFF_VALUES:
            return ""
        return model
    except Exception:
        return ""


def cli_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Print the subagent model to pin for a stage (empty = inherit session)."
    )
    parser.add_argument("--workspace-root", default=".")
    parser.add_argument("--stage", required=True)
    args = parser.parse_args(argv)
    model = resolve_stage_model(Path(args.workspace_root).expanduser().resolve(), args.stage)
    if model:
        sys.stdout.write(model + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = ["OFF_VALUES", "cli_main", "resolve_stage_model"]
