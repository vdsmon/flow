"""Claude Code SessionStart hook for /flow.

Detects whether the cwd is inside an initialized /flow workspace, recalls
relevant memory, records recall-pending entries, and prints a markdown context
block to stdout (Claude Code injects stdout as session context).

Orchestrates the existing scripts via subprocess (matching how SKILL.md invokes
them) rather than importing them. Child python scripts run under `sys.executable`
so they inherit the 3.11+ interpreter that tomllib needs; git stays a bare
`git`.

Robustness: any git failure, missing workspace.toml, or script error must NOT
crash the session. `build_context` returns "" instead of raising; `cli_main`
wraps the whole thing and always exits 0. A hook that errors should be silent,
not fatal.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import sys
import time
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

Runner = Callable[[list[str], Path], subprocess.CompletedProcess[str]]

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "skills" / "flow" / "scripts"
_DEFAULT_TOP_N = 5
_DEFAULT_RECALL_BY = ("branch", "current-ticket")
_SNIPPET_LEN = 160


# ─── Runner ──────────────────────────────────────────────────────────────────


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


def _script(name: str) -> list[str]:
    """argv prefix to run a sibling script under the current interpreter."""
    return [sys.executable, str(_SCRIPTS_DIR / name)]


# ─── Workspace detection ───────────────────────────────────────────────────────


def find_workspace_root(cwd: Path) -> Path | None:
    """Walk up from cwd looking for `.flow/.initialized`. None if not found."""
    for candidate in (cwd, *cwd.parents):
        if (candidate / ".flow" / ".initialized").exists():
            return candidate
    return None


def _read_memory_config(workspace_root: Path) -> tuple[list[str], int]:
    """Read `[memory]` recall_by + recall_top_n from workspace.toml.

    Missing file/keys fall back to defaults rather than raising.
    """
    path = workspace_root / ".flow" / "workspace.toml"
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return list(_DEFAULT_RECALL_BY), _DEFAULT_TOP_N
    memory = data.get("memory")
    if not isinstance(memory, dict):
        return list(_DEFAULT_RECALL_BY), _DEFAULT_TOP_N
    recall_by_raw = memory.get("recall_by")
    if isinstance(recall_by_raw, list):
        recall_by = [str(v) for v in recall_by_raw]
    else:
        recall_by = list(_DEFAULT_RECALL_BY)
    top_n_raw = memory.get("recall_top_n")
    top_n = top_n_raw if isinstance(top_n_raw, int) and top_n_raw > 0 else _DEFAULT_TOP_N
    return recall_by, top_n


# ─── Git context ───────────────────────────────────────────────────────────────


def _git_value(args: list[str], cwd: Path, runner: Runner) -> str:
    """Run a git command, return trimmed stdout, or "" on any non-zero exit."""
    result = runner(["git", *args], cwd)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _resolve_ticket(workspace_root: Path, cwd: Path, runner: Runner) -> str:
    """branch_ticket.py resolution. Any non-zero exit means no ticket context."""
    result = runner(
        [*_script("branch_ticket.py"), "--workspace-root", str(workspace_root)],
        cwd,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


# ─── Recall ──────────────────────────────────────────────────────────────────


def _recall(
    query: str, workspace_root: Path, branch: str, top_n: int, cwd: Path, runner: Runner
) -> list[dict[str, Any]]:
    """Run recall.py for one query. Returns the parsed list, or [] on any failure."""
    args = [
        *_script("recall.py"),
        query,
        "--workspace-root",
        str(workspace_root),
        "--top-n",
        str(top_n),
    ]
    if branch:
        args += ["--branch", branch]
    result = runner(args, cwd)
    if result.returncode != 0:
        return []
    try:
        parsed = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _record_pending(
    workspace_root: Path,
    *,
    branch: str,
    head_sha: str,
    cwd: Path,
    resolved_ticket: str,
    query: str,
    entries: list[dict[str, Any]],
    runner: Runner,
) -> None:
    """Append a recall-pending entry. Recording is a side-effect: any failure
    (lock/invalid/io/crash) is swallowed so the context block still renders.
    """
    ids = [str(e.get("id", "")) for e in entries]
    scores = [str(e.get("score", "")) for e in entries]
    args = [
        *_script("recall_pending.py"),
        "append",
        "--workspace-root",
        str(workspace_root),
        "--branch",
        branch,
        "--head-sha",
        head_sha,
        "--cwd",
        str(cwd),
        "--resolved-ticket",
        resolved_ticket,
        "--query",
        query,
        "--returned-ids",
        ",".join(ids),
        "--rank-scores",
        ",".join(scores),
    ]
    with contextlib.suppress(OSError):
        runner(args, cwd)


# ─── Render ──────────────────────────────────────────────────────────────────


def _snippet(body: Any) -> str:
    text = " ".join(str(body or "").split())
    if len(text) > _SNIPPET_LEN:
        return text[: _SNIPPET_LEN - 1].rstrip() + "…"
    return text


def _render(entries: list[dict[str, Any]]) -> str:
    lines = ["## /flow recall", ""]
    for entry in entries:
        etype = str(entry.get("type") or "note")
        ticket = str(entry.get("ticket") or "").strip()
        prefix = f"**{etype}**" + (f" ({ticket})" if ticket else "")
        snippet = _snippet(entry.get("body"))
        lines.append(f"- {prefix}: {snippet}" if snippet else f"- {prefix}")
    return "\n".join(lines)


# ─── Orchestration ─────────────────────────────────────────────────────────────


def build_context(workspace_root: Path, cwd: Path, runner: Runner | None = None) -> str:
    """Build the markdown context block for a session under `workspace_root`.

    Returns "" (never raises) when: workspace.toml is absent, git fails, no
    recall queries apply, or no entries are recalled.
    """
    runner = runner or _default_runner()
    if not (workspace_root / ".flow" / "workspace.toml").exists():
        return ""

    branch = _git_value(["branch", "--show-current"], cwd, runner)
    head_sha = _git_value(["rev-parse", "HEAD"], cwd, runner)
    if not branch or not head_sha:
        return ""

    resolved_ticket = _resolve_ticket(workspace_root, cwd, runner)
    recall_by, top_n = _read_memory_config(workspace_root)

    queries: list[str] = []
    if "branch" in recall_by and branch:
        queries.append(branch)
    if "current-ticket" in recall_by and resolved_ticket:
        queries.append(resolved_ticket)

    seen_ids: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for query in queries:
        entries = _recall(query, workspace_root, branch, top_n, cwd, runner)
        _record_pending(
            workspace_root,
            branch=branch,
            head_sha=head_sha,
            cwd=cwd,
            resolved_ticket=resolved_ticket,
            query=query,
            entries=entries,
            runner=runner,
        )
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            eid = str(entry.get("id", ""))
            if eid and eid in seen_ids:
                continue
            seen_ids.add(eid)
            deduped.append(entry)

    if not deduped:
        return ""
    return _render(deduped[:top_n])


_NIGHTLY_STALE_SECS = 36 * 3600
_WEEKLY_STALE_SECS = 8 * 24 * 3600
_NIGHTLY_ZOMBIE_GRACE = 3 * 3600
_WEEKLY_ZOMBIE_GRACE = 6 * 3600


def _parse_run_record(text: str) -> tuple[float | None, float | None, str | None]:
    """Parse run-record text; return (last_start, last_end_ts, last_end_outcome)."""
    last_start: float | None = None
    last_end_ts: float | None = None
    last_end_outcome: str | None = None
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            ts = int(parts[1])
        except ValueError:
            continue
        if parts[0] == "start":
            last_start = float(ts)
        elif parts[0] == "end" and len(parts) >= 3:
            last_end_ts = float(ts)
            last_end_outcome = parts[2]
    return last_start, last_end_ts, last_end_outcome


def _staleness_line(
    schedule: str,
    last_start: float | None,
    last_end_ts: float | None,
    last_end_outcome: str | None,
    stale_secs: int,
    zombie_grace: int,
    now: float,
) -> str:
    if last_end_ts is not None:
        if last_end_outcome == "fail":
            return f"- **{schedule}** last run recorded `fail`"
        if (now - last_end_ts) > stale_secs:
            hours = int((now - last_end_ts) / 3600)
            return f"- **{schedule}** last completed {hours}h ago (stale >{stale_secs // 3600}h)"
    elif last_start is not None and (now - last_start) > zombie_grace:
        hours_elapsed = int((now - last_start) / 3600)
        return f"- **{schedule}** started {hours_elapsed}h ago with no completion (hung?)"
    return ""


def _check_schedule_staleness(evolve_dir: Path) -> str:
    """Read nightly/weekly run-records and return a warning block if stale.

    Returns "" on any exception or when no record file exists (schedule not armed).
    Run-record format per line: `start <epoch>` or `end <epoch> ok|fail`.
    """
    try:
        now = time.time()
        warn_lines: list[str] = []
        for schedule, record_name, stale_secs, zombie_grace in (
            ("nightly", "nightly.run-record", _NIGHTLY_STALE_SECS, _NIGHTLY_ZOMBIE_GRACE),
            ("weekly", "weekly.run-record", _WEEKLY_STALE_SECS, _WEEKLY_ZOMBIE_GRACE),
        ):
            record_path = evolve_dir / record_name
            if not record_path.exists():
                continue
            raw = record_path.read_text(encoding="utf-8").strip()
            if not raw:
                continue
            last_start, last_end_ts, last_end_outcome = _parse_run_record(raw)
            line = _staleness_line(
                schedule, last_start, last_end_ts, last_end_outcome, stale_secs, zombie_grace, now
            )
            if line:
                warn_lines.append(line)
        if not warn_lines:
            return ""
        return "## /flow schedule\n\n" + "\n".join(warn_lines)
    except Exception:
        return ""


def cli_main(argv: list[str], *, _evolve_dir: Path | None = None) -> int:
    evolve_dir = _evolve_dir if _evolve_dir is not None else Path.home() / ".flow-evolve"
    cwd = Path(argv[0]).resolve() if argv else Path.cwd()
    try:
        workspace_root = find_workspace_root(cwd)
        if workspace_root is None:
            return 0
        stale_block = _check_schedule_staleness(evolve_dir)
        if stale_block:
            sys.stdout.write(stale_block + "\n")
        block = build_context(workspace_root, cwd)
        if block:
            sys.stdout.write(block + "\n")
    except Exception:
        # A hook crash must never break the session; swallow everything.
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = ["_check_schedule_staleness", "build_context", "cli_main", "find_workspace_root"]
