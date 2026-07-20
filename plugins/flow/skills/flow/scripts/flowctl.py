"""Allowlisted command facade for Flow's existing Python scripts."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from bundle_discover import HarnessError, flow_harness

SKILL_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = SKILL_ROOT / "scripts"

COMMANDS = {
    "agent-route": "agent_routes.py",
    "branch-ticket": "branch_ticket.py",
    "cockpit": "cockpit_cli.py",
    "compose-commit": "compose_commit.py",
    "cognitive-worker": "cognitive_workers.py",
    "create-pr": "create_pr.py",
    "diff": "diff_extract.py",
    "dispatch": "dispatch_stage.py",
    "evolve-drain": "evolve_drain.py",
    "evolve-reap": "evolve_reap.py",
    "fleet": "fleet.py",
    "flow-beads-create": "flow_beads_create.py",
    "forge": "forge_cli.py",
    "friction": "flow_friction.py",
    "friction-escalate": "friction_escalate.py",
    "frontmatter": "ticket_frontmatter.py",
    "group-candidates": "group_candidates.py",
    "group-persist": "group_persist.py",
    "handler": "resolve_handler.py",
    "harness-eval": "harness_eval.py",
    "lint-comments": "lint_comments.py",
    "lint-ticket": "lint_ticket.py",
    "lifecycle": "lifecycle_cli.py",
    "machinery-edit": "machinery_edit.py",
    "maintainer": "maintainer.py",
    "maintainer-preflight": "maintainer_preflight.py",
    "maintainer-senses": "senses_deadman.py",
    "memory-append": "memory_append.py",
    "merge": "stage_merge.py",
    "metric": "metric.py",
    "model": "model_resolve.py",
    "observe-at-close": "observe_at_close.py",
    "observe-ship-event": "observe_ship_event.py",
    "pending-mutations": "pending_mutations.py",
    "queue-drain": "queue_drain.py",
    "queue-status": "queue_status.py",
    "recall": "recall.py",
    "recall-usage": "recall_usage.py",
    "recover": "recover.py",
    "review-brief": "review_brief.py",
    "reflect-inputs": "reflect_inputs.py",
    "revise-config": "revise_config.py",
    "run-report": "run_report.py",
    "scrub-ci-skip": "scrub_ci_skip.py",
    "status": "status.py",
    "sweep-knowledge": "sweep_knowledge.py",
    "sync": "sync.py",
    "tracker": "tracker_cli.py",
    "triage": "triage.py",
    "validate": "validate_workspace.py",
    "worktree": "flow_worktree.py",
    "worktree-janitor": "worktree_janitor.py",
    "worker-pool": "worker_pool.py",
}


def resolve_command(command: str) -> Path | None:
    script = COMMANDS.get(command)
    return SCRIPTS_DIR / script if script is not None else None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run an allowlisted Flow command.")
    parser.add_argument("--workspace-root", required=True)
    parser.add_argument("command")
    parser.add_argument("args", nargs=argparse.REMAINDER)
    return parser


def cli_main(argv: list[str]) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    workspace_root = Path(args.workspace_root).expanduser()
    if not workspace_root.is_absolute():
        parser.error("--workspace-root must be an absolute path")

    # Validate at the facade boundary so a misspelled adapter fails on every
    # command, including commands that never perform bundle discovery.
    try:
        flow_harness()
    except HarnessError as exc:
        parser.error(str(exc))

    script = resolve_command(args.command)
    if script is None:
        parser.error(
            f"unknown command {args.command!r}; allowed commands: {', '.join(sorted(COMMANDS))}"
        )
    if not script.is_file():
        parser.error(f"mapped script is missing: {script}")

    try:
        os.chdir(workspace_root)
    except OSError as exc:
        parser.error(f"cannot enter workspace root {workspace_root}: {exc}")
    os.environ["FLOW_SKILL_DIR"] = str(SKILL_ROOT)
    os.environ["CLAUDE_SKILL_DIR"] = str(SKILL_ROOT)
    os.execv(sys.executable, [sys.executable, str(script), *args.args])
    return 1


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))
