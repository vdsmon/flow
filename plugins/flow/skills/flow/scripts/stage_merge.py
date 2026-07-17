"""Merge-stage absorber: the `merge` stage core (references/stage-merge.md).

Two entry points mirror the stage's shape: `probe` (§1 eligibility; no merge/close
side effects) and `execute` (§3 merge + Cover-close; the side effects). The §2 hot
guard-property review stays LLM prose BETWEEN the two calls; this script never
judges whether a diff removed a safety property.

Every external command shells through one injectable `Runner`, as an argument
list: `git`, `gh`, `bd`, and each sibling CLI (`forge_cli.py`, `main_ci_health.py`,
`harness_eval.py`, `evolve_self_merge.py`, `tracker_cli.py`), the latter invoked
as `[sys.executable, <script path>, ...]`. Shelling the gates rather than
importing their decision logic is deliberate: the gate stays byte-identical to
what the prose runs today, so behavior is property-equivalent to the prose
recipe by construction, not by a differential test. The only pure import is
`ticket_frontmatter.read` for the covers list.

`probe` re-reads CI, replays the harness eval when the PR touches scripts,
probes main's CI health, and asks `evolve_self_merge.py` for the merge/skip
verdict. It writes `harness_eval.json` and (on a hot merge verdict)
`merge_guard_diff.txt` under `<ticket-dir>/stages/`, the same paths the prose
wrote today. It performs no merge, close, or branch-delete.

`execute` performs the side effects: `--already-merged` closes the bead + covers
with no merge; otherwise it rebuilds the §3 push-state guard (fetch, then
compare HEAD to `origin/<branch>` by returncode, so a deleted remote ref reads
as "unpushed" and skips rather than closing), checks `mergeStateStatus`, and on
CLEAN/DRAFT runs `mark-ready` + `merge --squash` through the forge seam. A
`bd close` / cover-close / `delete-branch` only follows a `merge` returncode of
0; a merge failure is `STATUS=failed` and closes nothing. `delete-branch` is
remote-only; this script never tears down the local worktree or branch (that
stays with the drain janitor).

CLI: `stage_merge.py probe --workspace-root . --ticket-dir <dir> --key <KEY>`
-> verdict JSON. `stage_merge.py execute --workspace-root . --pr <id> --key
<KEY> [--already-merged]` -> result JSON.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import ticket_frontmatter

Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]

SCRIPTS_DIR = Path(__file__).resolve().parent

_PR_URL_RE = re.compile(r"^PR_URL=(.+)$", re.MULTILINE)
_PR_ID_RE = re.compile(r"(\d+)$")
_SCRIPTS_PY_RE = re.compile(r"^plugins/flow/skills/flow/scripts/.*\.py$")


class StageMergeError(Exception):
    """Raised when `probe` cannot proceed (e.g. no PR_ID to parse)."""


def _default_runner(cwd: Path) -> Runner:
    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, check=False)

    return run


def _script(name: str) -> str:
    return str(SCRIPTS_DIR / name)


def _warn(msg: str) -> None:
    sys.stderr.write(f"stage-merge: {msg}\n")


def _best_effort(result: subprocess.CompletedProcess[str], msg: str) -> None:
    if result.returncode != 0:
        _warn(f"{msg} (exit {result.returncode}): {result.stderr.strip()}")


# ─── PR id parsing ────────────────────────────────────────────────────────────


def parse_pr_id(create_pr_out: Path) -> str | None:
    """Parse the PR id off `create_pr.out`'s `PR_URL=` line (trailing digits)."""
    try:
        text = create_pr_out.read_text(encoding="utf-8")
    except OSError:
        return None
    m = _PR_URL_RE.search(text)
    if not m:
        return None
    digits = _PR_ID_RE.search(m.group(1).strip())
    return digits.group(1) if digits else None


# ─── probe (§1 eligibility; no side effects) ──────────────────────────────────


def _verdict(
    *,
    already_merged: bool,
    pr_id: str | None,
    action: str | None = None,
    is_hot: bool | None = None,
    reason: str | None = None,
    ci_status: str | None = None,
    eval_status: str | None = None,
    regressed_cases: list[str] | None = None,
    changed_files: list[str] | None = None,
    guard_diff_path: str | None = None,
    review_brief_status: str | None = None,
    review_brief_reason: str | None = None,
    review_brief_path: str | None = None,
) -> dict[str, Any]:
    return {
        "already_merged": already_merged,
        "pr_id": pr_id,
        "action": action,
        "is_hot": is_hot,
        "reason": reason,
        "ci_status": ci_status,
        "eval_status": eval_status,
        "regressed_cases": regressed_cases or [],
        "changed_files": changed_files or [],
        "guard_diff_path": guard_diff_path,
        "review_brief_status": review_brief_status,
        "review_brief_reason": review_brief_reason,
        "review_brief_path": review_brief_path,
    }


def _pr_state(pr_id: str, runner: Runner) -> str:
    r = runner(["gh", "pr", "view", pr_id, "--json", "state", "-q", ".state"])
    return r.stdout.strip() if r.returncode == 0 else ""


def _ci_rollup(workspace_root: Path, pr_id: str, runner: Runner) -> str:
    r = runner(
        [
            sys.executable,
            _script("forge_cli.py"),
            "--workspace-root",
            str(workspace_root),
            "ci-rollup",
            "--pr",
            pr_id,
        ]
    )
    if r.returncode != 0:
        # flow-vmzu: surface a non-zero exit, never read it as pending-forever.
        return "error"
    try:
        return json.loads(r.stdout)["status"]
    except (json.JSONDecodeError, KeyError):
        return "error"


def _changed_files(pr_id: str, runner: Runner) -> list[str]:
    r = runner(["gh", "pr", "diff", pr_id, "--name-only"])
    if r.returncode != 0:
        return []
    return [line.strip() for line in r.stdout.splitlines() if line.strip()]


def _review_brief_enabled(workspace_root: Path) -> bool:
    try:
        data = tomllib.loads((workspace_root / ".flow" / "workspace.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return False
    pipeline = data.get("pipeline")
    if not isinstance(pipeline, dict):
        return False
    stages = pipeline.get("stages")
    if not isinstance(stages, list) or "review_brief" not in stages:
        return False
    handlers = pipeline.get("handlers")
    if not isinstance(handlers, dict) or handlers.get("review_brief") == "none":
        return False
    review_brief = data.get("review_brief")
    return not (isinstance(review_brief, dict) and review_brief.get("mode") == "off")


def _review_brief_freshness(
    workspace_root: Path, ticket_dir: Path, pr_id: str, runner: Runner
) -> dict[str, Any]:
    r = runner(
        [
            sys.executable,
            _script("review_brief.py"),
            "freshness",
            "--workspace-root",
            str(workspace_root),
            "--ticket-dir",
            str(ticket_dir),
            "--pr-id",
            pr_id,
        ]
    )
    if r.returncode != 0:
        return {
            "status": "error",
            "reason": r.stderr.strip() or "review-brief freshness probe failed",
        }
    try:
        value = json.loads(r.stdout)
    except json.JSONDecodeError:
        return {"status": "error", "reason": "review-brief freshness output was not JSON"}
    return value if isinstance(value, dict) else {"status": "error", "reason": "bad output"}


def _extract_regressed_cases(stdout: str) -> list[str]:
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return []
    cases: list[str] = []
    for split in (data.get("splits") or {}).values():
        cases.extend(split.get("regressed", []))
    return cases


def _run_harness_eval(
    workspace_root: Path, ticket_dir: Path, runner: Runner
) -> tuple[str, list[str]]:
    candidate = str((workspace_root / "plugins" / "flow" / "skills" / "flow" / "scripts").resolve())
    r = runner([sys.executable, _script("harness_eval.py"), "score", "--candidate", candidate])
    stages_dir = ticket_dir / "stages"
    stages_dir.mkdir(parents=True, exist_ok=True)
    (stages_dir / "harness_eval.json").write_text(r.stdout, encoding="utf-8")
    if r.returncode == 0:
        return "pass", []
    if r.returncode == 3:
        return "regressed", _extract_regressed_cases(r.stdout)
    return "error", []


def _main_ci_probe(workspace_root: Path, runner: Runner) -> str:
    r = runner(
        [
            sys.executable,
            _script("main_ci_health.py"),
            "probe",
            "--workspace-root",
            str(workspace_root),
        ]
    )
    if r.returncode != 0:
        return "error"
    try:
        return json.loads(r.stdout)["status"]
    except (json.JSONDecodeError, KeyError):
        return "error"


def _self_merge_gate(
    workspace_root: Path,
    key: str,
    ci_status: str,
    eval_status: str | None,
    main_ci_status: str,
    changed_files: list[str],
    runner: Runner,
) -> dict[str, Any]:
    argv = [
        sys.executable,
        _script("evolve_self_merge.py"),
        "--workspace-root",
        str(workspace_root),
        "--key",
        key,
        "--ci-status",
        ci_status,
        "--main-ci-status",
        main_ci_status,
    ]
    if eval_status is not None:
        argv += ["--eval-status", eval_status]
    if changed_files:
        argv += ["--changed-files", ",".join(changed_files)]
    r = runner(argv)
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError as exc:
        raise StageMergeError(f"evolve_self_merge.py returned unparseable output: {exc}") from exc


def _write_guard_diff(pr_id: str, ticket_dir: Path, runner: Runner) -> str:
    r = runner(["gh", "pr", "diff", pr_id])
    stages_dir = ticket_dir / "stages"
    stages_dir.mkdir(parents=True, exist_ok=True)
    path = stages_dir / "merge_guard_diff.txt"
    path.write_text(r.stdout, encoding="utf-8")
    return str(path)


def probe(workspace_root: Path, ticket_dir: Path, key: str, *, runner: Runner) -> dict[str, Any]:
    """§1 eligibility probe. No merge/close side effects.

    Writes `harness_eval.json` when the PR touches scripts, and (on a hot merge
    verdict) `merge_guard_diff.txt`, under `<ticket_dir>/stages/`.
    """
    create_pr_out = ticket_dir / "stages" / "create_pr.out"
    pr_id = parse_pr_id(create_pr_out)
    if pr_id is None:
        raise StageMergeError(f"could not parse PR_ID from {create_pr_out}")

    if _pr_state(pr_id, runner) == "MERGED":
        return _verdict(already_merged=True, pr_id=pr_id, reason="already merged")

    brief: dict[str, Any] | None = None
    if _review_brief_enabled(workspace_root):
        brief = _review_brief_freshness(workspace_root, ticket_dir, pr_id, runner)
        # "disabled" here is an AUTHORIZED unattended skip (review_brief.freshness() already
        # cross-checked it against the run's seeded signal), not the enabled/disabled workspace
        # toggle above; both are non-blocking, "missing"/"stale"/"error" still refresh the brief.
        if brief.get("status") not in ("current", "disabled"):
            status = str(brief.get("status") or "error")
            detail = str(brief.get("reason") or "freshness could not be established")
            return _verdict(
                already_merged=False,
                pr_id=pr_id,
                action="refresh_review_brief",
                reason=f"review brief {status}: {detail}",
                review_brief_status=status,
                review_brief_reason=detail,
                review_brief_path=brief.get("html_path"),
            )

    ci_status = _ci_rollup(workspace_root, pr_id, runner)
    changed_files = _changed_files(pr_id, runner)

    eval_status: str | None = None
    regressed_cases: list[str] = []
    if any(_SCRIPTS_PY_RE.match(f) for f in changed_files):
        eval_status, regressed_cases = _run_harness_eval(workspace_root, ticket_dir, runner)

    main_ci_status = _main_ci_probe(workspace_root, runner)

    gate = _self_merge_gate(
        workspace_root, key, ci_status, eval_status, main_ci_status, changed_files, runner
    )

    guard_diff_path = None
    if gate.get("is_hot") and gate.get("action") == "merge":
        guard_diff_path = _write_guard_diff(pr_id, ticket_dir, runner)

    return _verdict(
        already_merged=False,
        pr_id=pr_id,
        action=gate.get("action"),
        is_hot=gate.get("is_hot"),
        reason=gate.get("reason"),
        ci_status=ci_status,
        eval_status=eval_status,
        regressed_cases=regressed_cases,
        changed_files=changed_files,
        guard_diff_path=guard_diff_path,
        review_brief_status=str(brief.get("status")) if brief else "disabled",
        review_brief_reason=str(brief.get("reason")) if brief else None,
        review_brief_path=brief.get("html_path") if brief else None,
    )


# ─── execute (§3 merge + Cover-close; the side effects) ───────────────────────


def _cover_close(workspace_root: Path, key: str, pr_id: str, runner: Runner) -> None:
    """comment -> transition -> dep-remove per cover, order preserved. Best-effort:
    a hiccup on any step warns and moves on; never raises."""
    fm = ticket_frontmatter.read(workspace_root / ".flow" / "tickets" / f"{key}.md")
    covers = fm.get("covers") or []
    for cover in covers:
        comment = runner(
            [
                sys.executable,
                _script("tracker_cli.py"),
                "--workspace-root",
                str(workspace_root),
                "comment",
                "--key",
                cover,
                "--text",
                f"co-delivered by {key} via PR #{pr_id}",
            ]
        )
        _best_effort(comment, f"cover comment for {cover}")
        transition = runner(
            [
                sys.executable,
                _script("tracker_cli.py"),
                "--workspace-root",
                str(workspace_root),
                "transition",
                "--key",
                cover,
                "--to-state",
                "closed",
            ]
        )
        _best_effort(transition, f"cover transition for {cover}")
        dep_remove = runner(["bd", "dep", "remove", cover, key])
        _best_effort(dep_remove, f"cover dep-remove for {cover}")


def _close_bead(key: str, reason: str, runner: Runner) -> None:
    result = runner(["bd", "close", key, "--reason", reason])
    _best_effort(result, f"bd close {key}")


def _execute_already_merged(
    workspace_root: Path, pr_id: str, key: str, runner: Runner
) -> dict[str, Any]:
    _close_bead(key, f"PR #{pr_id} already merged", runner)
    _cover_close(workspace_root, key, pr_id, runner)
    return {"status": "completed", "merged": False, "already_merged": True}


def _execute_merge(workspace_root: Path, pr_id: str, key: str, runner: Runner) -> dict[str, Any]:
    branch = runner(["git", "rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
    runner(["git", "fetch", "--quiet", "origin", branch])
    status = runner(["git", "status", "--porcelain", "--untracked-files=no"])
    local_sha = runner(["git", "rev-parse", "HEAD"])
    remote_sha = runner(["git", "rev-parse", f"origin/{branch}"])
    dirty = bool(status.stdout.strip())
    # A nonzero returncode on either side (e.g. the reap already deleted the
    # remote branch) reads as unpushed too, so it skips rather than promoting
    # to a close.
    unpushed = (
        local_sha.returncode != 0
        or remote_sha.returncode != 0
        or local_sha.stdout.strip() != remote_sha.stdout.strip()
    )
    if dirty or unpushed:
        return {
            "status": "completed",
            "merged": False,
            "reason": "uncommitted or unpushed changes CI never validated",
        }

    merge_state = runner(
        ["gh", "pr", "view", pr_id, "--json", "mergeStateStatus", "-q", ".mergeStateStatus"]
    ).stdout.strip()
    if merge_state == "DIRTY":
        return {"status": "completed", "merged": False, "reason": "DIRTY, left for human"}

    runner(
        [
            sys.executable,
            _script("forge_cli.py"),
            "--workspace-root",
            str(workspace_root),
            "mark-ready",
            "--pr",
            pr_id,
        ]
    )
    merge = runner(
        [
            sys.executable,
            _script("forge_cli.py"),
            "--workspace-root",
            str(workspace_root),
            "merge",
            "--pr",
            pr_id,
            "--squash",
        ]
    )
    if merge.returncode != 0:
        return {"status": "failed", "merged": False, "reason": "merge tool failure"}

    _close_bead(key, f"self-merged via PR #{pr_id}", runner)
    _cover_close(workspace_root, key, pr_id, runner)
    delete = runner(
        [
            sys.executable,
            _script("forge_cli.py"),
            "--workspace-root",
            str(workspace_root),
            "delete-branch",
            "--branch",
            branch,
        ]
    )
    _best_effort(delete, f"delete-branch {branch}")
    return {"status": "completed", "merged": True}


def execute(
    workspace_root: Path, pr_id: str, key: str, *, runner: Runner, already_merged: bool = False
) -> dict[str, Any]:
    """§3 merge + Cover-close, the side effects. `already_merged` skips straight
    to close + cover-close (no merge/mark-ready/delete-branch)."""
    if already_merged:
        return _execute_already_merged(workspace_root, pr_id, key, runner)
    return _execute_merge(workspace_root, pr_id, key, runner)


# ─── CLI ─────────────────────────────────────────────────────────────────────


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Merge-stage absorber: probe + execute.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("probe", help="eligibility probe (§1); no merge/close side effects")
    p.add_argument("--workspace-root", default=".")
    p.add_argument("--ticket-dir", required=True)
    p.add_argument("--key", required=True)

    p = sub.add_parser("execute", help="merge + Cover-close (§3); the side effects")
    p.add_argument("--workspace-root", default=".")
    p.add_argument("--pr", required=True)
    p.add_argument("--key", required=True)
    p.add_argument("--already-merged", action="store_true")

    return parser.parse_args(argv)


def cli_main(argv: list[str], runner: Runner | None = None) -> int:
    args = _parse_args(argv)
    workspace_root = Path(args.workspace_root).resolve()
    run = runner or _default_runner(workspace_root)

    if args.cmd == "probe":
        ticket_dir = Path(args.ticket_dir).resolve()
        try:
            result = probe(workspace_root, ticket_dir, args.key, runner=run)
        except StageMergeError as exc:
            _warn(str(exc))
            return 1
        sys.stdout.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
        return 0

    result = execute(
        workspace_root, args.pr, args.key, runner=run, already_merged=args.already_merged
    )
    sys.stdout.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return 1 if result.get("status") == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = ["cli_main", "execute", "parse_pr_id", "probe"]
