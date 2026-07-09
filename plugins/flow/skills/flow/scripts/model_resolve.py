"""Resolve the per-stage subagent model for a run (opus plans, sonnet writes).

Pure, stdlib-only. Prints the model to pin on a given stage's subagent (typically BELOW the opus
session model), or nothing when the caller should inherit the session model.

The disposition is ON BY DEFAULT: on a full-lane run each routable stage downshifts to `sonnet`
unless a workspace overrides or disables it. Routable stages (the ones that spawn a subagent) and
their built-in default: see _DEFAULT_STAGE_MODELS. A stage NOT in that map (e.g. `plan`) is not
routable and always inherits the session model.

Lane comes from the ticket frontmatter (a local read, no tracker call): absent or "full" ->
downshift; "express"/"light" -> the run already launched a cheap session, so its work inherits that
model and needs no pin. Hot and normal runs are both full-lane and both downshift; the `hot` label
is deliberately NOT read (a hot bead follows the split too: opus plans/reviews, sonnet writes).

`[models]` overrides the built-in defaults, resolved per stage with this precedence:
  1. `[models].<stage>`  -- a per-stage pin (a model name, or an OFF_VALUE to
     inherit the session for that stage only). Highest priority.
  2. `[models].work_model` -- the DEPRECATED global fallback: one model for every
     routable stage that has no per-stage key. Kept for back-compat.
  3. the built-in _DEFAULT_STAGE_MODELS default (`sonnet`).
An OFF_VALUE ("off"/"none"/"false"/"") at any level opts that stage out (inherit the session).
Fail-open: an unexpected read error prints nothing.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import ticket_frontmatter
from _workspace import load_workspace_toml

_SKIP_LANES = ("express", "light")
# The routable stages (spawn a subagent) and their built-in default model.
# A stage absent here is not routable: it always inherits the session model.
_DEFAULT_STAGE_MODELS = {
    "implement": "sonnet",
    "e2e": "sonnet",
    "code_review": "sonnet",
    "review_loop": "sonnet",
}
# a model set to any of these disables the downshift (inherit the session).
OFF_VALUES = frozenset({"", "off", "none", "false"})


def resolve_stage_model(workspace_root: Path, ticket: str, stage: str) -> str:
    """Return the model to pin for `stage`, or "" to inherit the session model."""
    try:
        if stage not in _DEFAULT_STAGE_MODELS:
            return ""  # not a routable stage
        try:
            fm = ticket_frontmatter.read(workspace_root / ".flow" / "tickets" / f"{ticket}.md")
        except Exception:
            fm = {}
        lane = fm.get("lane")
        if isinstance(lane, str) and lane in _SKIP_LANES:
            return ""
        model = _DEFAULT_STAGE_MODELS[stage]
        try:
            models = load_workspace_toml(workspace_root).get("models")
            if isinstance(models, dict):
                if isinstance(models.get(stage), str):
                    model = models[stage]
                elif isinstance(models.get("work_model"), str):
                    model = models["work_model"]
        except Exception:
            pass  # no/invalid workspace.toml -> keep the built-in default
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
    parser.add_argument("--ticket", required=True)
    parser.add_argument("--stage", required=True)
    args = parser.parse_args(argv)
    model = resolve_stage_model(
        Path(args.workspace_root).expanduser().resolve(), args.ticket, args.stage
    )
    if model:
        sys.stdout.write(model + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = ["OFF_VALUES", "cli_main", "resolve_stage_model"]
