"""Shared subprocess-runner factories: positional-cwd, keyword-only, and cwd-bound contracts."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

# Contract A: positional cwd, check=False. Used by diff_extract, branch_ticket,
# recall_pending, flow_worktree.
Runner = Callable[[list[str], Path], subprocess.CompletedProcess[str]]


def default_runner() -> Runner:
    def run(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, check=False)

    return run


# Contract B: keyword-only cwd/check/input. Used by init, tracker_beads.
KwRunner = Callable[..., subprocess.CompletedProcess[str]]


def kw_default_runner() -> KwRunner:
    def run(
        args: list[str],
        *,
        cwd: Path | None = None,
        check: bool = False,
        input: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            args,
            cwd=str(cwd) if cwd else None,
            check=check,
            capture_output=True,
            text=True,
            input=input,
        )

    return run


# Contract C: cwd bound into the closure, args-only call. Used by forge adapters
# (forge_github, forge_bitbucket), the evolve cluster (evolve_reap, evolve_select),
# and create_pr.
CwdRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]


def cwd_default_runner(repo: Path) -> CwdRunner:
    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(args, cwd=str(repo), capture_output=True, text=True, check=False)

    return run
