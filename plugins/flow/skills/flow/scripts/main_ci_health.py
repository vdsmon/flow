"""Probe the default branch's (main's) CI health by sha, the per-drain-turn gate.

Before any evolve self-merge or drain reap promotion, /flow asks this whether main's
own CI is genuinely red. Two concurrently-green PRs that semantically conflict can land
on main untested; the red surfaces only when a later run inherits it. There is no daemon
to host a standing watcher, so this is a PER-TURN GATE, not a watcher: probe once, pause
this turn if red, resume next turn.

The verdict is ASYMMETRIC: only `failed` pauses. `green`, `pending`, and a probe
`error` (transient gh 401 / network) all resume: a pause on a still-running or
unprobeable main would freeze auto-merge on noise. `_classify_rollup` (reused from
forge_github) already folds CANCELLED/STALE/NEUTRAL/SKIPPED → pending, so a superseded
concurrent run never reads as a failure.

Probe path: `gh api repos/{owner}/{repo}/commits/<sha>/check-runs` (REST, sha-keyed;
owner/repo auto-resolve from the cwd git remote). REST returns lowercase `status`
(`completed`), so each entry's `status` is uppercased before `_classify_rollup` (which
compares raw `status != "COMPLETED"`); `conclusion` passes through (the classifier
uppercases it). flow's CI is one GitHub Actions check on `push` to main, so a fresh
squash-merge sha carries a completed check-run within ~1 turn; an empty window reads
`pending` → resume (correct under the per-turn framing).

CLI: `main_ci_health.py probe --workspace-root . [--sha <sha>]` → JSON
`{status, sha, failing_checks}`. classify_main_ci(check_runs) is a pure helper for
unit-testing without gh.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from forge_github import _classify_rollup

Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]


def classify_main_ci(check_runs: list) -> dict[str, Any]:
    """Pure verdict from REST check-run entries `[{name, status, conclusion}, ...]`.

    Uppercases each entry's `status` (REST emits lowercase `completed`) so it matches
    `_classify_rollup`'s raw `status != "COMPLETED"` check, then reuses that classifier
    (inheriting its CANCELLED/STALE/NEUTRAL/SKIPPED → pending folding). Returns
    `{status: green|pending|failed, failing_checks: [...]}`; failing_checks lists the
    names of any check whose conclusion is terminal non-SUCCESS, empty otherwise.
    """
    rollup = [
        {**e, "status": (e.get("status") or "").upper()} for e in check_runs if isinstance(e, dict)
    ]
    classified = _classify_rollup(rollup)
    status = classified["status"]
    failing = (
        [c["name"] for c in classified["checks"] if c["conclusion"] not in ("SUCCESS", "")]
        if status == "failed"
        else []
    )
    return {"status": status, "failing_checks": failing}


_READ_BACKOFFS = (0.5, 1.0)


def _ok_read(
    run: Runner, args: list[str], sleep: Callable[[float], None] = time.sleep
) -> subprocess.CompletedProcess[str]:
    """Like forge_github._ok_read: a transient gh/network non-zero survives a bounded
    retry. Returns the final CompletedProcess (caller inspects returncode)."""
    result = run(args)
    for backoff in _READ_BACKOFFS:
        if result.returncode == 0:
            return result
        sleep(backoff)
        result = run(args)
    return result


def _gh_runner(repo: Path) -> Runner:
    # export GH_TOKEN so a headless gh-keyring 401 flake does not sink every probe
    # (memory gh-keyring-401-headless-needs-gh-token). A token read failure leaves the
    # env unset; the probe still degrades to status:"error" (resume), never a pause.
    import os

    env = dict(os.environ)
    tok = subprocess.run(
        ["gh", "auth", "token"], cwd=str(repo), capture_output=True, text=True, check=False
    )
    if tok.returncode == 0 and tok.stdout.strip():
        env["GH_TOKEN"] = tok.stdout.strip()

    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            args, cwd=str(repo), capture_output=True, text=True, check=False, env=env
        )

    return run


def probe(workspace_root: Path, *, sha: str | None = None, runner: Runner | None = None) -> dict:
    """Probe main's CI health by sha. Any gh/network/parse failure → status:"error"
    (resume), NEVER a pause. When `sha` is absent, resolve `origin/main` after a
    read-only fetch."""
    run = runner or _gh_runner(workspace_root)
    if sha is None:
        run(["git", "fetch", "--quiet", "origin"])
        rev = run(["git", "rev-parse", "origin/main"])
        if rev.returncode != 0 or not rev.stdout.strip():
            return {"status": "error", "sha": None, "failing_checks": []}
        sha = rev.stdout.strip()
    result = _ok_read(
        run,
        [
            "gh",
            "api",
            f"repos/{{owner}}/{{repo}}/commits/{sha}/check-runs",
            "--jq",
            ".check_runs",
        ],
    )
    if result.returncode != 0:
        return {"status": "error", "sha": sha, "failing_checks": []}
    try:
        check_runs = json.loads(result.stdout or "[]") or []
    except json.JSONDecodeError:
        return {"status": "error", "sha": sha, "failing_checks": []}
    verdict = classify_main_ci(check_runs)
    return {"status": verdict["status"], "sha": sha, "failing_checks": verdict["failing_checks"]}


def cli_main(argv: list[str], runner: Runner | None = None) -> int:
    parser = argparse.ArgumentParser(description="Probe main's CI health by sha.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("probe", help="probe main's CI rollup")
    p.add_argument("--workspace-root", default=".")
    p.add_argument("--sha", default=None, help="sha to probe; default resolves origin/main")
    args = parser.parse_args(argv)
    result = probe(Path(args.workspace_root).resolve(), sha=args.sha, runner=runner)
    sys.stdout.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = ["classify_main_ci", "cli_main", "probe"]
