"""File a self-work (machinery) bead into flow's OWN beads DB.

Used by the reflect sling-bead path and `/flow evolve`. Two guarantees:

- Gated on maintainer mode. Outside it the bead is NOT filed (exit 4), so a normal
  user run never requires a flow checkout and machinery friction stays dormant.
- Always targets flow's beads (the resolved maintainer repo root), never the run's
  tracker — which may be Jira. A machinery finding is about the harness, not the
  user's project, so it must land in flow's backlog regardless of the run.

Stdlib-only. `bd` is invoked with cwd = the flow repo so it resolves that repo's
.beads DB.

CLI:
  flow_beads_create.py --workspace-root <dir> --summary <s> --description <d>
      [--type task] [--labels a,b] [--parent KEY]

Exit codes:
  0 = filed (prints the new bead key)
  2 = bd error (stderr propagated)
  4 = not a maintainer setup (dormant; nothing filed)
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from maintainer import resolve_maintainer_repo

Runner = Callable[..., subprocess.CompletedProcess[str]]


class NotMaintainer(Exception):
    """Raised when the run is not in maintainer mode. Exit 4."""


class BeadCreateError(Exception):
    """Raised when `bd create` fails or returns no id. Exit 2."""


def _default_runner() -> Runner:
    def run(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, check=False)

    return run


def create_bead(
    workspace_root: Path,
    summary: str,
    description: str,
    *,
    type: str = "task",
    labels: list[str] | None = None,
    parent: str | None = None,
    runner: Runner | None = None,
) -> str:
    """File a bead into flow's beads and return the new key.

    Raises NotMaintainer outside maintainer mode (caller decides whether that is
    fine — for the reflect dormant path it is). Raises BeadCreateError on bd
    failure.
    """
    repo = resolve_maintainer_repo(workspace_root)
    if repo is None:
        raise NotMaintainer(
            "not a flow maintainer setup (no [maintainer] marker); machinery bead not filed"
        )
    args = ["bd", "create", summary, "--type", type, "--description", description]
    if labels:
        args += ["--labels", ",".join(labels)]
    if parent:
        args += ["--parent", parent]
    args.append("--json")
    run = runner or _default_runner()
    result = run(args, repo)
    if result.returncode != 0:
        raise BeadCreateError(f"bd create failed: {result.stderr.strip()}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise BeadCreateError(
            f"bd create did not return JSON: {exc}; raw={result.stdout!r}"
        ) from exc
    key = str(payload.get("id", "")) if isinstance(payload, dict) else ""
    if not key:
        # never re-run create on a parse miss: a second bd create mints a duplicate
        raise BeadCreateError(f"bd create returned no top-level id; raw={payload!r}")
    return key


def cli_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="File a self-work bead into flow's beads.")
    parser.add_argument("--workspace-root", required=True)
    parser.add_argument("--summary", required=True)
    parser.add_argument("--description", required=True)
    parser.add_argument("--type", default="task")
    parser.add_argument("--labels", default="")
    parser.add_argument("--parent", default=None)
    args = parser.parse_args(argv)

    labels = [s for s in (p.strip() for p in args.labels.split(",")) if s]
    try:
        key = create_bead(
            Path(args.workspace_root),
            args.summary,
            args.description,
            type=args.type,
            labels=labels,
            parent=args.parent,
        )
    except NotMaintainer as exc:
        print(str(exc), file=sys.stderr)
        return 4
    except BeadCreateError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(key)
    return 0


if __name__ == "__main__":
    sys.exit(cli_main(sys.argv[1:]))
