"""Resolve the work-phase subagent model for a run (opus plans, sonnet writes).

Pure, stdlib-only. Prints the work model to pin on a run's code-writing subagents
(BELOW the opus session model), or nothing when the caller should inherit the
session model.

The disposition is ON BY DEFAULT: a full-lane run downshifts its work subagents to
`sonnet` unless a workspace overrides or disables it. Lane comes from the ticket
frontmatter (a local read, no tracker call): absent or "full" -> downshift;
"express"/"light" -> the run already launched a cheap session, so its work inherits
that model and needs no pin. Hot and normal runs are both full-lane and both
downshift; the `hot` label is deliberately NOT read (a hot bead follows the split
too: opus plans/reviews, sonnet writes).

`[models] work_model` overrides the default: set it to a model name to pin that
model, or to one of OFF_VALUES ("off"/"none"/"") to OPT OUT (inherit the session
everywhere). Fail-open: an unexpected read error prints nothing.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import ticket_frontmatter
from _workspace import load_workspace_toml

_SKIP_LANES = ("express", "light")
_DEFAULT_WORK_MODEL = "sonnet"
# a `work_model` set to any of these disables the downshift (inherit the session).
OFF_VALUES = frozenset({"", "off", "none", "false"})


def resolve_work_model(workspace_root: Path, ticket: str) -> str:
    """Return the work_model to pin, or "" to inherit the session model."""
    try:
        try:
            fm = ticket_frontmatter.read(workspace_root / ".flow" / "tickets" / f"{ticket}.md")
        except Exception:
            fm = {}
        lane = fm.get("lane")
        if isinstance(lane, str) and lane in _SKIP_LANES:
            return ""
        work_model = _DEFAULT_WORK_MODEL
        try:
            models = load_workspace_toml(workspace_root).get("models")
            if isinstance(models, dict) and isinstance(models.get("work_model"), str):
                work_model = models["work_model"]
        except Exception:
            pass  # no/invalid workspace.toml -> keep the default
        if work_model.strip().lower() in OFF_VALUES:
            return ""
        return work_model
    except Exception:
        return ""


def cli_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Print the work-phase subagent model for a run (empty = inherit session)."
    )
    parser.add_argument("--workspace-root", default=".")
    parser.add_argument("--ticket", required=True)
    args = parser.parse_args(argv)
    model = resolve_work_model(Path(args.workspace_root).expanduser().resolve(), args.ticket)
    if model:
        sys.stdout.write(model + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = ["OFF_VALUES", "cli_main", "resolve_work_model"]
