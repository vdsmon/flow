"""Decide whether an evolve run may self-merge its own PR (the `merge` stage core).

Pure decision + a thin CLI. The `merge` stage (references/stage-merge.md) runs after
`reflect` in the evolve self-target; it asks this module whether the run's own bead
is eligible to self-merge, then acts: for a `hot` bead it first spawns an INDEPENDENT
reviewer subagent for the §6A guard-property check (author != reviewer), and only on
a clean review does it merge.

`decide()` is pure (no side effects, no I/O), so the gate logic is unit-tested
directly. The CLI wires the I/O: read the bead's labels (`bd show`), the
`[evolve] auto_merge_hot` flag, and maintainer mode, then call `decide()`.

Gates (mirrors the drain reap's classify, but evaluated in-run):
- not maintainer self-target / not an `evolve` bead / CI not green -> skip (leave the
  PR for the human; never an error).
- the label read itself failing (a transient `bd show` error that outlived the retry)
  -> skip: "labels unreadable" stays distinct from a genuinely unlabeled bead, so a
  read flake is never mistaken for "not an evolve bead".
- harness eval not "pass" (the stage runs `harness_eval.py score` when the PR touches
  scripts and feeds the verdict via `--eval-status`) -> skip: "regressed" names the
  Self-Harness no-degradation rule; anything else blocks conservatively. Omitted ->
  no-op (eval not applicable).
- a `hot` bead self-merges only when `[evolve] auto_merge_hot` is on (else skip ->
  human). The independent property review is the merge stage's job, not this gate's.
- otherwise -> merge. `is_hot` tells the stage whether to run the property review.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import ticket_frontmatter
from _evolve_common import ToolError, bead_show
from _evolve_common import auto_merge_hot as _auto_merge_hot
from triage import is_hot_change

Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]

Action = Literal["merge", "skip"]

_READ_BACKOFFS = (0.5, 1.0)


def decide(
    labels: list[str],
    *,
    is_maintainer: bool,
    auto_merge_hot: bool,
    ci_status: str,
    planned_files: list[str] | None = None,
    eval_status: str | None = None,
    main_ci_status: str | None = None,
    changed_files: list[str] | None = None,
    labels_readable: bool = True,
) -> dict[str, Any]:
    """Pure self-merge gate. Returns {action, is_hot, reason}.

    `labels_readable` is False when the label read itself failed (a transient `bd show` error that
    outlived the retry): skip with "labels unreadable" (the transient-read-failure case, distinct
    from a genuinely empty label list).

    `action` is "skip" (leave the PR for the human) or "merge". `is_hot` is the
    `hot` label OR a guard-file hit in `planned_files` OR one in `changed_files`
    (triage.is_hot_change); it tells the caller whether the §6A independent
    property review must run before merging. `changed_files` is the merge-time
    OBSERVED PR diff: a guard file that entered the PR after planning (a
    review-loop CI fix pushed past the ownership gate) never reaches plan-time
    frontmatter, so the observed diff can only RAISE hotness, never lower it;
    None keeps the plan-time derivation byte-identical. `eval_status` is the
    harness-eval verdict ("pass"/"regressed"/"error", None when the eval did not
    run): "pass" continues, anything else skips ("regressed" by the
    no-degradation rule, the rest conservatively).
    """
    is_hot = (
        ("hot" in labels)
        or is_hot_change(planned_files or [])
        or is_hot_change(changed_files or [])
    )
    if not is_maintainer:
        return {"action": "skip", "is_hot": is_hot, "reason": "not maintainer self-target"}
    if not labels_readable:
        return {"action": "skip", "is_hot": is_hot, "reason": "labels unreadable — bd show failed"}
    if "evolve" not in labels:
        return {"action": "skip", "is_hot": is_hot, "reason": "not an evolve bead"}
    if "proposal" in labels:
        # Proposal beads are the maintainer's judgment call (the auto-vs-propose line), even
        # if one was manually run. Leave the PR for the human.
        return {"action": "skip", "is_hot": is_hot, "reason": "proposal bead — maintainer merges"}
    if ci_status != "green":
        return {"action": "skip", "is_hot": is_hot, "reason": f"CI not green ({ci_status})"}
    if main_ci_status == "failed":
        # the per-drain-turn main-CI health gate: a genuinely red main pauses auto-merge
        # this turn (None / green / pending / a transient probe "error" all no-op then resume).
        return {
            "action": "skip",
            "is_hot": is_hot,
            "reason": "main CI red — auto-merge paused this turn",
        }
    if eval_status is not None and eval_status != "pass":
        if eval_status == "regressed":
            reason = "harness eval regressed — no-degradation rule routes to the human"
        else:
            reason = "harness eval error — no non-regression evidence"
        return {"action": "skip", "is_hot": is_hot, "reason": reason}
    if is_hot and not auto_merge_hot:
        return {"action": "skip", "is_hot": True, "reason": "hot bead and auto_merge_hot is off"}
    return {"action": "merge", "is_hot": is_hot, "reason": "eligible"}


def _default_runner() -> Runner:
    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(args, capture_output=True, text=True, check=False)

    return run


def _bead_labels(
    key: str, runner: Runner, *, sleep: Callable[[float], None] = time.sleep
) -> list[str] | None:
    """The bead's labels via `_evolve_common.bead_show` (authoritative; labels
    live in the tracker, not in ticket.json). A transient `bd show` failure
    survives a bounded retry (mirrors main_ci_health._ok_read). Returns None
    when the read still fails after retries (unreadable), distinct from [] (a
    genuinely unlabeled bead); malformed JSON stays [] (bead_show returns {}
    without raising).
    """
    for backoff in (*_READ_BACKOFFS, None):
        try:
            data = bead_show(runner, key)
        except ToolError:
            if backoff is None:
                return None
            sleep(backoff)
            continue
        labels = data.get("labels")
        return [str(x) for x in labels] if isinstance(labels, list) else []
    return None


def cli_main(argv: list[str], runner: Runner | None = None) -> int:
    parser = argparse.ArgumentParser(description="Decide whether an evolve run self-merges.")
    parser.add_argument("--workspace-root", default=".")
    parser.add_argument("--key", required=True, help="the run's bead key")
    parser.add_argument(
        "--ci-status", required=True, help="green|pending|failed (from review_loop)"
    )
    parser.add_argument(
        "--eval-status",
        default=None,
        choices=("pass", "regressed", "error"),
        help="harness_eval verdict; omit when the eval did not run",
    )
    parser.add_argument(
        "--main-ci-status",
        default=None,
        help="main's CI health (main_ci_health.py probe); only 'failed' pauses the merge",
    )
    parser.add_argument(
        "--changed-files",
        default=None,
        help="comma-separated observed PR diff paths (gh pr diff --name-only); "
        "a guard-file hit raises is_hot even when planned_files never gained it",
    )
    args = parser.parse_args(argv)

    from maintainer import is_maintainer

    workspace_root = Path(args.workspace_root).resolve()
    run = runner or _default_runner()
    labels = _bead_labels(args.key, run)
    fm = ticket_frontmatter.read(workspace_root / ".flow" / "tickets" / f"{args.key}.md")
    pf = fm.get("planned_files")
    planned_files = [str(x) for x in pf] if isinstance(pf, list) else []
    changed_files = (
        [f.strip() for f in args.changed_files.split(",") if f.strip()]
        if args.changed_files is not None
        else None
    )
    result = decide(
        labels or [],
        is_maintainer=is_maintainer(workspace_root),
        auto_merge_hot=_auto_merge_hot(workspace_root),
        ci_status=args.ci_status,
        planned_files=planned_files,
        eval_status=args.eval_status,
        main_ci_status=args.main_ci_status,
        changed_files=changed_files,
        labels_readable=labels is not None,
    )
    sys.stdout.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = ["cli_main", "decide"]
