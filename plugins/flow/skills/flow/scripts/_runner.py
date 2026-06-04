"""Shared subprocess-runner factories: positional-cwd and keyword-only contracts."""

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
