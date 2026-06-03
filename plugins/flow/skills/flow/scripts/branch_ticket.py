"""Resolve ticket key from current git branch.

Library + thin CLI. Stdlib-only.

Backend-aware:
  - jira:  matches `<PROJECT_KEY>-\\d+` against branch name.
  - beads: matches `<prefix>-[0-9a-z]{4,}` (mirrors `_BD_ID_RE` in
           `tracker_beads.py`).

Exit codes (per plan line 1012-1013):
  0 = match (key on stdout).
  1 = environment error (no git repo, no workspace.toml, malformed toml).
  3 = no match (empty stdout). Callers MUST treat this as "no ticket context".
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import _workspace

Runner = Callable[..., subprocess.CompletedProcess[str]]


def _default_runner() -> Runner:
    def run(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
        )

    return run


@dataclass(frozen=True)
class _TrackerConfig:
    backend: str
    project_key: str | None
    prefix: str | None


class _BranchTicketError(Exception):
    """Environment-level error. Exit code 1."""


def _read_tracker_config(workspace_root: Path) -> _TrackerConfig:
    try:
        data = _workspace.load_workspace_toml(workspace_root)
    except _workspace.WorkspaceConfigError as exc:
        raise _BranchTicketError(str(exc)) from exc
    tracker = data.get("tracker")
    if not isinstance(tracker, dict):
        raise _BranchTicketError("workspace.toml missing [tracker] block")
    backend = tracker.get("backend")
    if backend not in ("jira", "beads"):
        raise _BranchTicketError(f"unknown tracker.backend {backend!r}")
    project_key: str | None = None
    prefix: str | None = None
    if backend == "jira":
        jira = tracker.get("jira")
        if not isinstance(jira, dict) or not jira.get("project_key"):
            raise _BranchTicketError("workspace.toml missing tracker.jira.project_key")
        project_key = str(jira["project_key"])
    else:
        beads = tracker.get("beads")
        if not isinstance(beads, dict) or not beads.get("prefix"):
            raise _BranchTicketError("workspace.toml missing tracker.beads.prefix")
        prefix = str(beads["prefix"])
    return _TrackerConfig(backend=backend, project_key=project_key, prefix=prefix)


def _current_branch(cwd: Path, runner: Runner) -> str:
    result = runner(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd)
    if result.returncode != 0:
        raise _BranchTicketError(f"git rev-parse failed: {result.stderr.strip()}")
    branch = result.stdout.strip()
    if not branch:
        raise _BranchTicketError("git rev-parse returned empty branch name")
    return branch


def _match_key(branch: str, cfg: _TrackerConfig) -> str | None:
    if cfg.backend == "jira":
        assert cfg.project_key is not None
        pattern = re.compile(rf"\b({re.escape(cfg.project_key)}-\d+)\b")
    else:
        assert cfg.prefix is not None
        pattern = re.compile(rf"\b({re.escape(cfg.prefix)}-[0-9a-z]{{4,}})\b")
    m = pattern.search(branch)
    return m.group(1) if m else None


def resolve(workspace_root: Path, cwd: Path, runner: Runner | None = None) -> str | None:
    """Returns ticket key on match, None on no-match.

    Raises `_BranchTicketError` on environment failure (caller surfaces as
    exit 1).
    """
    cfg = _read_tracker_config(workspace_root)
    branch = _current_branch(cwd, runner or _default_runner())
    return _match_key(branch, cfg)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Resolve ticket key from current git branch.")
    parser.add_argument("--workspace-root", default=".")
    parser.add_argument("--cwd", default=".")
    return parser.parse_args(argv)


def cli_main(argv: list[str]) -> int:
    args = _parse_args(argv)
    workspace_root = Path(args.workspace_root).resolve()
    cwd = Path(args.cwd).resolve()
    try:
        key = resolve(workspace_root, cwd)
    except _BranchTicketError as exc:
        sys.stderr.write(f"branch-ticket: {exc}\n")
        return 1
    if key is None:
        return 3
    sys.stdout.write(key + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = ["cli_main", "resolve"]
