"""Shared helpers for the evolve drain cluster (lib, no CLI).

Definitions duplicated verbatim across evolve_reap / evolve_select /
evolve_drain / evolve_session_cleanup live here once: the tool-call wrappers,
the `feature/<key>` branch regex, the bead-label query sets, and the
worktree-pool run-dir resolution.
"""

from __future__ import annotations

import glob
import json
import re
import subprocess
from pathlib import Path

FLOW_KEY_RE = re.compile(r"^feature/(flow-[a-z0-9]+(?:\.\d+)?)(?:-.*)?$", re.IGNORECASE)


class NotMaintainer(Exception):
    """Raised when the run is not in maintainer mode. Exit 4."""


class ToolError(Exception):
    """Raised when an injected tool (bd/git/gh) fails. Exit 2."""


def ok(result: subprocess.CompletedProcess[str], what: str) -> str:
    if result.returncode != 0:
        raise ToolError(f"{what} failed: {result.stderr.strip()}")
    return result.stdout or ""


def loads(raw: str) -> list:
    try:
        payload = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        items = payload.get("issues") or payload.get("prs") or []
        return items if isinstance(items, list) else []
    return []


def key_from_ref(ref: str) -> str | None:
    m = FLOW_KEY_RE.match(ref.removeprefix("origin/"))
    return m.group(1) if m else None


def bead_labels(include_proposals: bool) -> list[str]:
    """The bd label set an evolve query spans (`proposal` only when opted in)."""
    return ["evolve", "proposal"] if include_proposals else ["evolve"]


def run_dir_for(repo: Path, key: str) -> Path | None:
    """The in-flight run's ticket dir under the worktree pool for `key`.

    Worktrees live at `<repo>/.flow/worktrees/feature-<key>-<slug>/` (see
    flow_worktree._worktree_path); the run state is `.flow/runs/<key>/`. Absent =
    no lease to read (a leaked branch with no worktree, or the common post-reap
    case), so the caller treats it as non-live rather than waiting on it forever.
    """
    base = repo / ".flow" / "worktrees"
    for wt in sorted(glob.glob(str(base / f"feature-{key}*"))):
        run_dir = Path(wt) / ".flow" / "runs" / key
        if run_dir.exists():
            return run_dir
    return None
