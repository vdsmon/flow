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
# (forge_github, forge_bitbucket) and create_pr.
CwdRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]


def cwd_default_runner(repo: Path) -> CwdRunner:
    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(args, cwd=str(repo), capture_output=True, text=True, check=False)

    return run


def checked(
    result: subprocess.CompletedProcess[str],
    what: str,
    exc: type[Exception],
    *,
    stderr_only: bool = False,
    strip: bool = False,
) -> str:
    """Raise `exc` when `result` failed, else return its stdout.

    The one check-and-raise the runner contracts share. `stderr_only` keeps the
    forge/tool message shape (stderr alone); the default falls back stderr ->
    stdout -> "unknown error". `strip` returns stdout.strip() for callers that
    parse a single token.
    """
    if result.returncode != 0:
        if stderr_only:
            detail = (result.stderr or "").strip()
        else:
            detail = (result.stderr or result.stdout or "unknown error").strip()
        raise exc(f"{what} failed: {detail}")
    out = result.stdout or ""
    return out.strip() if strip else out


def git_text(
    args: list[str],
    cwd: Path,
    runner: Runner,
    exc: type[Exception],
    *,
    quote_path_off: bool = False,
    strip: bool = False,
) -> str:
    """Run one git command through a positional-cwd runner; raise `exc` on failure.

    `quote_path_off` prepends `-c core.quotePath=false` so non-ASCII paths come
    back literal (UTF-8) instead of C-quoted; parsers that compare raw output
    against planned paths need it or the ownership gate false-flags a legit file.
    """
    prefix = ["git", "-c", "core.quotePath=false"] if quote_path_off else ["git"]
    result = runner([*prefix, *args], cwd)
    if result.returncode != 0:
        raise exc(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip() if strip else result.stdout


def gitignored(files: list[str], cwd: Path, runner: Runner, exc: type[Exception]) -> list[str]:
    """Return the subset of `files` git ignores. check-ignore exits 0 when a path
    is ignored, 1 when none are, so it cannot go through git_text (which raises
    on non-zero)."""
    if not files:
        return []
    result = runner(["git", "check-ignore", "--", *files], cwd)
    if result.returncode not in (0, 1):
        raise exc(f"git check-ignore failed: {result.stderr.strip()}")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]
