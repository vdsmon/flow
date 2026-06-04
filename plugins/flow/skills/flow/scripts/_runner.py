"""Shared positional subprocess runner: `(args, cwd) -> CompletedProcess[str]`.

This is the one copy of the positional, non-checking runner used by
diff_extract.py, branch_ticket.py, recall_pending.py, and flow_worktree.py. Each
had a byte-identical `_default_runner` and a `Runner` alias; flow_worktree.py
kept (and still keeps) a STRICTER local alias `Callable[[list[str], Path], ...]`,
which the strict-return factory below satisfies.

Two other runner contracts in this package are intentionally NOT shared here,
because consolidating them would change behavior:
- lease.py: `(list[str]) -> str`, check=True, returns `.stdout`, no cwd.
- the keyword-cwd family (tracker_beads.py, init.py):
  `runner(args, *, cwd=None, check=..., input=...)`.
The ticket counted "6 byte-identical modules"; the real positional family is 4.
Documenting the gap here so a future consolidation does not blindly merge the
incompatible contracts under the shared `Runner` name.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

Runner = Callable[..., subprocess.CompletedProcess[str]]


def default_runner() -> Callable[[list[str], Path], subprocess.CompletedProcess[str]]:
    def run(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
        )

    return run
