"""Tests for _runner.py — the one shared positional `(args, cwd)` runner.

Pins the consolidation: the four positional consumers share a single factory and
the loose-alias consumers share a single `Runner` symbol, so re-inlining a copy
is caught here rather than drifting silently.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import _runner
import branch_ticket
import diff_extract
import flow_worktree
import recall_pending


def test_default_runner_honors_cwd_and_returncode(tmp_path: Path) -> None:
    run = _runner.default_runner()
    result = run(["pwd"], tmp_path)
    assert isinstance(result, subprocess.CompletedProcess)
    assert result.returncode == 0
    assert result.stdout.strip() == str(tmp_path.resolve())


def test_default_runner_is_non_checking(tmp_path: Path) -> None:
    run = _runner.default_runner()
    result = run(["false"], tmp_path)
    assert result.returncode != 0


def test_consumers_share_the_factory() -> None:
    assert diff_extract._default_runner is _runner.default_runner
    assert branch_ticket._default_runner is _runner.default_runner
    assert recall_pending._default_runner is _runner.default_runner
    assert flow_worktree._default_runner is _runner.default_runner


def test_loose_alias_identity() -> None:
    assert recall_pending.Runner is _runner.Runner
    assert diff_extract.Runner is _runner.Runner
