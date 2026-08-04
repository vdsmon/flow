"""bare FLOW cockpit: surface the deferred queue + each bead's open-question comment.

Read-only. Lists every `deferred` bead (whole queue, unscoped by assignee) PLUS
`blocked` beads whose comments carry the defer stem (decided-mode hot blocks),
each with the last "could not self-approve" defer comment inline, so a human can
answer it and reopen via the tracker_cli seams (the reopen mutation lives in
command-target.md, not here). Deferred is a beads-native concept; non-beads
backends short-circuit. `--ready` opt-in adds the ready
queue via one extra `bd ready` call.

`triage.py decided` is a separate probe used by the `--auto` path: it reads a
bead's recorded triage decision + classifies whether the planned change is hot,
so a reopened bead carrying a decision does not re-defer on the answered
question.

Stdlib-only. The `bd` transport is injectable (`runner=`) for offline tests.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from _workspace import WorkspaceConfigError, load_workspace_toml
from tracker_beads import BeadsAdapter
from tracker_cli import _read_tracker_config, _WorkspaceConfigError

# The defer comment stem written by the `--auto` path (delivery-plan.md). Both the
# template form `... self-approve:` and the in-the-wild `... self-approve (HOT...`
# share this prefix, so we match on the stem and accept whatever follows.
_DEFER_STEM = "flow --auto could not self-approve"

# Anchored, case-sensitive match for a recorded decision stem. Tolerates an optional `MAINTAINER `
# prefix and a date/text run before the colon, so a freeform `MAINTAINER DECISION <date>:`
# maintainer comment reads as decided (flow-rvc); case-sensitive so lowercase prose "decision:"
# never matches.
_DECISION_RE = re.compile(r"^(?:MAINTAINER\s+)?(?:TRIAGE-)?DECISION\b[^:\n]*:")

# Guard set for hot-change classification. A change touching any of these
# basenames is hot: it must not blind-ship from a decided-mode --auto run, even
# if the bead carries no `hot` label.
_GUARD_FILES = frozenset(
    {
        "lease.py",
        "snapshot.py",
        "_atomicio.py",
        "_locking.py",
        "state.py",
        "dispatch_stage.py",
        "diff_extract.py",
        "flow_launcher.py",
        "flowctl.py",
        "flow_worktree.py",
        "machinery_edit.py",
        "flow_friction.py",
        "SKILL.md",
        "stage-registry.toml",
        "CLAUDE.md",
        "AGENTS.md",
    }
)

_NO_COMMENT = "(no open-question comment)"


def is_hot_change(files: list[str]) -> bool:
    return any(Path(f).name in _GUARD_FILES for f in files)


def adjudicate_hot(workspace_root: Path) -> bool:
    """`[triage] adjudicate_hot` from workspace.toml (bool); default False.

    Default OFF: the hot hard-floor holds for delivery workspaces, so a hot change
    never self-proceeds unattended. Opt IN with an explicit
    `adjudicate_hot = true` (a self-target workspace preference) to lift the
    floor: a hot change then ships on an advisor `proceed` like a non-hot one.
    Only an explicit `True` enables it; an absent key/section/file (and any read
    error) reads as off, the conservative side.

    Lifting the floor removes BOTH the delivery-plan `proceed`->`block`
    downgrade and the flow_worktree bootstrap refusal. The remaining gates still
    hold: the merge-time guard-property review plus CI back-stop every hot
    landing. No `[triage]` key is validated by validate_workspace.py, so a
    misspelled one reads as absent, i.e. as the conservative default.
    """
    try:
        config = load_workspace_toml(workspace_root)
    except WorkspaceConfigError:
        return False
    section = config.get("triage")
    if not isinstance(section, dict):
        return False
    value = section.get("adjudicate_hot")
    return value if isinstance(value, bool) else False


def _recorded_decision(comments: list[Any]) -> str | None:
    """Newest-by-created_at comment whose text matches `_DECISION_RE`.

    Start-anchored on the left-stripped text (via the `^` anchor) to avoid
    mid-text false positives (a defer comment that merely mentions "the
    decision"). The regex is case-sensitive and tolerates an optional
    `MAINTAINER ` prefix plus a date/text run before the colon, so a freeform
    `MAINTAINER DECISION <date>:` comment reads as decided; lowercase prose
    "decision:" never matches. bd keys comment bodies under `text` (not `body`).
    Returns the decision text with the matched stem stripped + leading
    whitespace trimmed, else None.
    """
    if not comments:
        return None
    ordered = sorted(comments, key=lambda c: str(c.get("created_at", "")))
    chosen: str | None = None
    for c in ordered:
        text = str(c.get("text", ""))
        stripped = text.lstrip()
        m = _DECISION_RE.match(stripped)
        if m:
            chosen = stripped[m.end() :].lstrip()
    return chosen


def decided(
    config: dict[str, Any],
    key: str,
    files: list[str],
    *,
    runner: Any = None,
) -> dict[str, Any]:
    """Probe a bead for a recorded triage decision + hot + hitl classification.

    Does its own raw `bd show <key> --include-comments --json` (the
    `_run_json` pattern appends `--json`), reading `labels` + `comments`
    straight off the raw dict. Never raises: any bd-read failure returns a
    block-by-default result. `hitl` mirrors the label (human-in-the-loop);
    the block-by-default fallback reads `hitl:false`, since an indeterminate
    read already blocks via the hot half, and a spurious hitl-defer would be
    the wrong disposition for a read the gate cannot trust. Where
    `adjudicate_hot` lifts that hot half, the residual pass-through on a
    failed read is the same narrow fail-open the terminal/epic refusals
    accept: a flaky read never strands a legit run.
    """
    try:
        adapter = BeadsAdapter(config, runner=runner)
        raw = adapter._run_json(["show", key, "--include-comments"])
    except Exception:
        return {"decided": False, "answer": None, "is_hot": True, "hitl": False}

    issue = raw[0] if isinstance(raw, list) and raw else raw
    if not isinstance(issue, dict):
        return {"decided": False, "answer": None, "is_hot": True, "hitl": False}

    labels = issue.get("labels") or []
    comments = issue.get("comments") or []
    answer = _recorded_decision(comments if isinstance(comments, list) else [])
    is_decided = answer is not None
    is_hot = is_hot_change(files) or ("hot" in labels)
    # decided but hotness indeterminate (no --files, no hot label) -> block.
    if is_decided and not files and "hot" not in labels:
        is_hot = True
    return {
        "decided": is_decided,
        "answer": answer,
        "is_hot": is_hot,
        "hitl": "hitl" in labels,
    }


def lane(config: dict[str, Any], key: str, *, runner: Any = None) -> str:
    """Resolve a bead's verification lane (express|light|full) from its tier labels.

    The spec-time twin of `flow_worktree._lane_for_bead` (which reads via the tracker
    at bootstrap): the attended planning path calls this BEFORE bootstrap, so the express/light
    skips (advisor probe, plan revision) can fire while planning. Same raw bd read as
    `decided`; policy lives in `tier_policy.lane_for`. Fail-open to "full" so a flaky
    read never silently downshifts a run's gating.
    """
    import tier_policy

    try:
        adapter = BeadsAdapter(config, runner=runner)
        raw = adapter._run_json(["show", key, "--include-comments"])
    except Exception:
        return "full"
    issue = raw[0] if isinstance(raw, list) and raw else raw
    if not isinstance(issue, dict):
        return "full"
    return tier_policy.lane_for(issue.get("labels") or [])


def _comment_text(c: Any) -> str:
    """Comment body across both shapes: raw `bd show --include-comments` keys it
    under `text`; the marshaled Ticket (adapter.get) nests it under `body`."""
    if not isinstance(c, dict):
        return ""
    if "text" in c:
        return str(c.get("text") or "")
    body = c.get("body") or {}
    return body.get("body", "") if isinstance(body, dict) else str(body)


def _open_question(comments: list[Any]) -> str:
    if not comments:
        return _NO_COMMENT
    ordered = sorted(comments, key=lambda c: str(c.get("created_at", "")))
    chosen: Any = None
    for c in ordered:
        if _DEFER_STEM in _comment_text(c):
            chosen = c
    if chosen is None:
        chosen = ordered[-1]
    return _comment_text(chosen)


def _has_defer_stem(comments: list[Any]) -> bool:
    return any(_DEFER_STEM in _comment_text(c) for c in comments)


def collect(
    config: dict[str, Any],
    *,
    include_ready: bool = False,
    runner: Any = None,
) -> list[dict[str, Any]]:
    adapter = BeadsAdapter(config, runner=runner)

    def _items(raw: Any) -> list[Any]:
        return (
            raw
            if isinstance(raw, list)
            else (raw.get("issues", []) if isinstance(raw, dict) else [])
        )

    deferred = _items(adapter._run_json(["list", "--status", "deferred"]))
    blocked = _items(adapter._run_json(["list", "--status", "blocked"]))
    # ready surfacing is opt-in: one extra `bd ready` call. Issued here, after
    # the two lists and before any per-bead show, so the injectable runner's
    # call sequence stays deterministic.
    ready = _items(adapter._run_json(["ready"])) if include_ready else []

    rows: list[dict[str, Any]] = []
    for item in deferred:
        if not isinstance(item, dict):
            continue
        key = str(item.get("id", ""))
        ticket = adapter.get(key)
        rows.append(
            {
                "key": key,
                "title": str(item.get("title", "")),
                "status": "deferred",
                "open_question": _open_question(ticket.get("comments") or []),
            }
        )
    # blocked beads are surfaced ONLY when they carry the defer stem (decided-mode
    # hot blocks). A bare status=blocked is a DAG dependency hold, not a
    # human-input hold, and must not be surfaced or force-reopened from triage.
    for item in blocked:
        if not isinstance(item, dict):
            continue
        key = str(item.get("id", ""))
        raw = adapter._run_json(["show", key, "--include-comments"])
        issue = raw[0] if isinstance(raw, list) and raw else raw
        comments = (issue.get("comments") or []) if isinstance(issue, dict) else []
        if not _has_defer_stem(comments):
            continue
        rows.append(
            {
                "key": key,
                "title": str(item.get("title", "")),
                "status": "blocked",
                "open_question": _open_question(comments),
            }
        )
    # ready beads carry no defer comment by definition: no per-bead show.
    for item in ready:
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "key": str(item.get("id", "")),
                "title": str(item.get("title", "")),
                "status": "ready",
                "open_question": "",
            }
        )
    rows.sort(key=lambda r: r["key"])
    return rows


def _truncate(text: str, width: int = 80) -> str:
    one_line = " ".join(text.split())
    return one_line if len(one_line) <= width else one_line[: width - 1] + "…"


def render_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "(no deferred tickets)"
    headers = ["KEY", "STATUS", "TITLE", "OPEN QUESTION"]
    table = [headers]
    for r in rows:
        status = str(r.get("status", ""))
        # surface advisor-minted rulings so a maintainer can spot them for
        # optional review (a `block` verdict lands the ruling in the defer-stem
        # comment, tagged `(advisor)`).
        if "(advisor)" in str(r.get("open_question", "")):
            status = f"{status} (advisor)"
        table.append(
            [
                str(r["key"]),
                status,
                _truncate(str(r["title"]), 40),
                _truncate(str(r["open_question"])),
            ]
        )
    widths = [max(len(row[i]) for row in table) for i in range(len(headers))]
    return "\n".join(
        "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)) for row in table
    )


def _resolve_config(workspace_root: Path) -> tuple[dict[str, Any] | None, int]:
    """Shared workspace/config resolution. Returns (config, exit_code).

    config is None when the caller should return exit_code (1 not-init, 2
    config error). config is set with exit_code 0 on success.
    """
    if not (workspace_root / ".flow").is_dir():
        sys.stderr.write("triage: workspace not initialized; run `FLOW workspace setup`\n")
        return None, 1
    try:
        config = _read_tracker_config(workspace_root)
    except _WorkspaceConfigError as exc:
        sys.stderr.write(f"triage: {exc}\n")
        return None, 2
    return config, 0


def _cmd_list(args: argparse.Namespace, runner: Any) -> int:
    workspace_root = Path(args.workspace_root).expanduser().resolve()
    config, code = _resolve_config(workspace_root)
    if config is None:
        return code
    if config["backend"] != "beads":
        sys.stdout.write("deferred is a beads concept; nothing to triage\n")
        return 0
    rows = collect(config, include_ready=args.ready, runner=runner)
    if args.json:
        sys.stdout.write(json.dumps(rows, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(render_table(rows) + "\n")
    return 0


def _cmd_decided(args: argparse.Namespace, runner: Any) -> int:
    workspace_root = Path(args.workspace_root).expanduser().resolve()
    config, code = _resolve_config(workspace_root)
    if config is None:
        return code
    files = [f.strip() for f in args.files.split(",") if f.strip()] if args.files else []
    result = decided(config, args.key, files, runner=runner)
    sys.stdout.write(json.dumps(result) + "\n")
    return 0


def _cmd_lane(args: argparse.Namespace, runner: Any) -> int:
    workspace_root = Path(args.workspace_root).expanduser().resolve()
    config, code = _resolve_config(workspace_root)
    if config is None:
        return code
    if config["backend"] != "beads":
        # tiers are a beads/evolve concept; no tier labels -> full lane.
        sys.stdout.write("full\n")
        return 0
    sys.stdout.write(lane(config, args.key, runner=runner) + "\n")
    return 0


def cli_main(argv: list[str], runner: Any = None) -> int:
    parser = argparse.ArgumentParser(description="bare FLOW cockpit: list deferred beads.")
    sub = parser.add_subparsers(dest="command")

    p_list = sub.add_parser("list", help="list deferred + decided-mode hot-block beads")
    p_list.add_argument("--workspace-root", default=".")
    p_list.add_argument("--json", action="store_true")
    p_list.add_argument(
        "--ready",
        action="store_true",
        help="also list ready beads",
    )

    p_decided = sub.add_parser("decided", help="probe a bead's recorded triage decision")
    p_decided.add_argument("--workspace-root", default=".")
    p_decided.add_argument("--key", required=True)
    p_decided.add_argument("--files", default=None)

    p_lane = sub.add_parser(
        "lane", help="resolve a bead's verification lane (express|light|full) from tier labels"
    )
    p_lane.add_argument("--workspace-root", default=".")
    p_lane.add_argument("--key", required=True)

    args = parser.parse_args(argv)

    if args.command == "decided":
        return _cmd_decided(args, runner)
    if args.command == "lane":
        return _cmd_lane(args, runner)
    return _cmd_list(args, runner)


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = [
    "adjudicate_hot",
    "cli_main",
    "collect",
    "decided",
    "is_hot_change",
    "render_table",
]
