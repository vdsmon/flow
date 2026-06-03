"""File a self-work (machinery) bead into flow's OWN beads DB.

Used by the reflect sling-bead path and `/flow evolve`. Two guarantees:

- Gated on maintainer mode. Outside it the bead is NOT filed (exit 4), so a normal
  user run never requires a flow checkout and machinery friction stays dormant.
- Always targets flow's beads (the resolved maintainer repo root), never the run's
  tracker — which may be Jira. A machinery finding is about the harness, not the
  user's project, so it must land in flow's backlog regardless of the run.

Stdlib-only. `bd` is invoked with cwd = the flow repo so it resolves that repo's
.beads DB.

Identity / convergence. With `--dedup-key <slug>` the bead is stamped with an
`evid:<slug>` label and, before creating, flow's beads are checked for an existing
bead carrying that label in ANY status (open or closed). If one exists the create
is skipped (exit 5). Keying on a stable slug — not the wording — is what stops the
audit refiling open work AND re-proposing findings already closed or rejected, so
the loop converges instead of churning.

CLI:
  flow_beads_create.py --workspace-root <dir> --summary <s> --description <d>
      [--type task] [--labels a,b] [--parent KEY] [--dedup-key SLUG]

Exit codes:
  0 = filed (prints the new bead key)
  2 = bd error (stderr propagated)
  4 = not a maintainer setup (dormant; nothing filed)
  5 = duplicate: a bead with this --dedup-key already exists (prints its key)
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


class DuplicateBead(Exception):
    """Raised when a bead with the given --dedup-key already exists. Exit 5."""

    def __init__(self, existing_key: str, dedup_key: str) -> None:
        super().__init__(f"bead for evid:{dedup_key} already exists: {existing_key}")
        self.existing_key = existing_key
        self.dedup_key = dedup_key


# every stored status, so dedup also catches closed/rejected findings (not just open)
_ALL_STATUSES = "open,in_progress,blocked,deferred,closed"


def _default_runner() -> Runner:
    def run(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, check=False)

    return run


def _find_by_dedup_key(repo: Path, dedup_key: str, run: Runner) -> str | None:
    """Return the key of an existing bead labelled evid:<dedup_key>, or None."""
    result = run(
        ["bd", "list", "-l", f"evid:{dedup_key}", "--status", _ALL_STATUSES, "--json"], repo
    )
    if result.returncode != 0:
        raise BeadCreateError(f"bd list (dedup check) failed: {result.stderr.strip()}")
    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return None
    items = (
        payload
        if isinstance(payload, list)
        else payload.get("issues", [])
        if isinstance(payload, dict)
        else []
    )
    for item in items:
        if isinstance(item, dict) and item.get("id"):
            return str(item["id"])
    return None


def create_bead(
    workspace_root: Path,
    summary: str,
    description: str,
    *,
    type: str = "task",
    labels: list[str] | None = None,
    parent: str | None = None,
    dedup_key: str | None = None,
    runner: Runner | None = None,
) -> str:
    """File a bead into flow's beads and return the new key.

    Raises NotMaintainer outside maintainer mode (caller decides whether that is
    fine — for the reflect dormant path it is). Raises DuplicateBead when
    dedup_key matches an existing bead. Raises BeadCreateError on bd failure.
    """
    repo = resolve_maintainer_repo(workspace_root)
    if repo is None:
        raise NotMaintainer(
            "not a flow maintainer setup (no [maintainer] marker); machinery bead not filed"
        )
    run = runner or _default_runner()
    labels = list(labels or [])
    if dedup_key:
        existing = _find_by_dedup_key(repo, dedup_key, run)
        if existing is not None:
            raise DuplicateBead(existing, dedup_key)
        labels.append(f"evid:{dedup_key}")
    args = ["bd", "create", summary, "--type", type, "--description", description]
    if labels:
        args += ["--labels", ",".join(labels)]
    if parent:
        args += ["--parent", parent]
    args.append("--json")
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
    parser.add_argument("--dedup-key", default=None)
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
            dedup_key=args.dedup_key,
        )
    except NotMaintainer as exc:
        print(str(exc), file=sys.stderr)
        return 4
    except DuplicateBead as exc:
        print(exc.existing_key)
        print(str(exc), file=sys.stderr)
        return 5
    except BeadCreateError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(key)
    return 0


if __name__ == "__main__":
    sys.exit(cli_main(sys.argv[1:]))
