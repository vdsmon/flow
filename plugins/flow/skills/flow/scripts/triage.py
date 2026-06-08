"""/flow triage: surface the deferred queue + each bead's open-question comment.

Read-only. Lists every `deferred` bead (whole queue, unscoped by assignee) PLUS
`blocked` beads whose comments carry the defer stem (decided-mode hot blocks),
each with the last "could not self-approve" defer comment inline, so a human can
answer it and reopen via the tracker_cli seams (the reopen mutation lives in
verb-triage.md, not here). Deferred is a beads-native concept; non-beads
backends short-circuit.

`triage.py decided` is a separate probe used by the `--auto` path: it reads a
bead's recorded triage decision + classifies whether the planned change is hot,
so a reopened bead carrying a decision does not re-defer on the answered
question.

Stdlib-only. The `bd` transport is injectable (`runner=`) for offline tests.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from _workspace import WorkspaceConfigError, load_workspace_toml
from tracker_beads import BeadsAdapter
from tracker_cli import _read_tracker_config, _WorkspaceConfigError

# The defer comment stem written by the `--auto` path (verb-spec.md). Both the
# template form `... self-approve:` and the in-the-wild `... self-approve (HOT...`
# share this prefix, so we match on the stem and accept whatever follows.
_DEFER_STEM = "flow --auto could not self-approve"

# Recorded-decision stems written by the reopen recipe (verb-triage.md). New
# reopens write `TRIAGE-DECISION:`; the already-reopened beads carry the legacy
# `DECISION:` stem, so detection accepts both (zero backfill).
_DECISION_STEMS = ("TRIAGE-DECISION:", "DECISION:")

# Guard set for hot-change classification (self-contained; not shared with
# verb-evolve.md prose). A change touching any of these basenames is hot: it
# must not blind-ship from a decided-mode --auto run, even if the bead carries
# no `hot` label.
_GUARD_FILES = frozenset(
    {
        "lease.py",
        "snapshot.py",
        "_atomicio.py",
        "_locking.py",
        "state.py",
        "dispatch_stage.py",
        "diff_extract.py",
        "machinery_edit.py",
        "flow_friction.py",
        "SKILL.md",
        "stage-registry.toml",
        "CLAUDE.md",
    }
)

_NO_COMMENT = "(no open-question comment)"


def is_hot_change(files: list[str]) -> bool:
    return any(Path(f).name in _GUARD_FILES for f in files)


def advisor_adjudicates(workspace_root: Path) -> bool:
    """`[evolve] advisor_adjudicates` from workspace.toml (bool); default True.

    Default ON: when the `--auto` path hits a judgment fork it escalates to the
    advisor for a ship/block/defer ruling instead of deferring. Opt OUT with an
    explicit `advisor_adjudicates = false` (restores the old defer-on-fork
    behavior). Only an explicit `false` disables it; an absent key/section/file
    reads as on. The safety nets still hold either way: `is_hot_change` is a hard
    floor (hot never auto-proceeds), a broad-blast verdict blocks for human merge,
    and the PR review/merge keystone is unchanged.
    """
    try:
        config = load_workspace_toml(workspace_root)
    except WorkspaceConfigError:
        return True
    section = config.get("evolve")
    if not isinstance(section, dict):
        return True
    value = section.get("advisor_adjudicates")
    return value if isinstance(value, bool) else True


def adjudicate_hot(workspace_root: Path) -> bool:
    """`[evolve] adjudicate_hot` from workspace.toml (bool); default False.

    Default OFF: the hot hard-floor holds for user projects, so a hot change
    never self-proceeds unattended. Opt IN with an explicit
    `adjudicate_hot = true` (a maintainer self-target preference) to lift the
    floor: a hot change then ships on an advisor `proceed` like a non-hot one.
    Only an explicit `True` enables it; an absent key/section/file (and any read
    error) reads as off, the conservative side. Sibling of `advisor_adjudicates`.

    Lifting the floor removes BOTH the verb-spec step 5.3 `proceed`->`block`
    downgrade and the flow_worktree bootstrap refusal. The remaining gates still
    hold: advisor adjudication rules on the fork, and the merge-time
    guard-property review plus CI back-stop every hot landing.
    """
    try:
        config = load_workspace_toml(workspace_root)
    except WorkspaceConfigError:
        return False
    section = config.get("evolve")
    if not isinstance(section, dict):
        return False
    value = section.get("adjudicate_hot")
    return value if isinstance(value, bool) else False


def _recorded_decision(comments: list[Any]) -> str | None:
    """Newest-by-created_at comment whose text starts with a decision stem.

    Start-anchored on the left-stripped text to avoid mid-text false positives
    (a defer comment that merely mentions "the decision"). bd keys comment
    bodies under `text` (not `body`). Returns the decision text with the matched
    stem stripped + leading whitespace trimmed, else None.
    """
    if not comments:
        return None
    ordered = sorted(comments, key=lambda c: str(c.get("created_at", "")))
    chosen: str | None = None
    for c in ordered:
        text = str(c.get("text", ""))
        stripped = text.lstrip()
        for stem in _DECISION_STEMS:
            if stripped.startswith(stem):
                chosen = stripped[len(stem) :].lstrip()
                break
    return chosen


def decided(
    config: dict[str, Any],
    key: str,
    files: list[str],
    *,
    runner: Any = None,
) -> dict[str, Any]:
    """Probe a bead for a recorded triage decision + hot classification.

    Does its own raw `bd show <key> --include-comments --json` (the
    `_run_json` pattern appends `--json`), reading `labels` + `comments`
    straight off the raw dict. Never raises: any bd-read failure returns a
    block-by-default result.
    """
    try:
        adapter = BeadsAdapter(config, runner=runner)
        raw = adapter._run_json(["show", key, "--include-comments"])
    except Exception:
        return {"decided": False, "answer": None, "is_hot": True}

    issue = raw[0] if isinstance(raw, list) and raw else raw
    if not isinstance(issue, dict):
        return {"decided": False, "answer": None, "is_hot": True}

    labels = issue.get("labels") or []
    comments = issue.get("comments") or []
    answer = _recorded_decision(comments if isinstance(comments, list) else [])
    is_decided = answer is not None
    is_hot = is_hot_change(files) or ("hot" in labels)
    # decided but hotness indeterminate (no --files, no hot label) -> block.
    if is_decided and not files and "hot" not in labels:
        is_hot = True
    return {"decided": is_decided, "answer": answer, "is_hot": is_hot}


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


def collect(config: dict[str, Any], *, runner: Any = None) -> list[dict[str, Any]]:
    adapter = BeadsAdapter(config, runner=runner)

    def _items(raw: Any) -> list[Any]:
        return (
            raw
            if isinstance(raw, list)
            else (raw.get("issues", []) if isinstance(raw, dict) else [])
        )

    deferred = _items(adapter._run_json(["list", "--status", "deferred"]))
    blocked = _items(adapter._run_json(["list", "--status", "blocked"]))

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
        sys.stderr.write("triage: workspace not initialized; run `/flow init`\n")
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
    rows = collect(config, runner=runner)
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


def _cmd_adjudicate_enabled(args: argparse.Namespace) -> int:
    workspace_root = Path(args.workspace_root).expanduser().resolve()
    sys.stdout.write("true\n" if advisor_adjudicates(workspace_root) else "false\n")
    return 0


def _cmd_adjudicate_hot_enabled(args: argparse.Namespace) -> int:
    workspace_root = Path(args.workspace_root).expanduser().resolve()
    sys.stdout.write("true\n" if adjudicate_hot(workspace_root) else "false\n")
    return 0


def _default_to_list(argv: list[str]) -> list[str]:
    """Prepend `list` when the first non-flag token is not a known subcommand.

    Keeps the legacy `triage.py --workspace-root .` call (and all existing
    prose/tests) working with the restructured subparser layout. A top-level
    `-h`/`--help` is left untouched so the parser shows the subcommand group
    (the seam checker discovers `{list,decided}` from that usage line).
    """
    for tok in argv:
        if tok in ("-h", "--help"):
            return argv
        if tok.startswith("-"):
            continue
        if tok in ("list", "decided", "adjudicate-enabled", "adjudicate-hot-enabled"):
            return argv
        break
    return ["list", *argv]


def cli_main(argv: list[str], runner: Any = None) -> int:
    parser = argparse.ArgumentParser(description="/flow triage: list deferred beads.")
    sub = parser.add_subparsers(dest="command")

    p_list = sub.add_parser("list", help="list deferred + decided-mode hot-block beads")
    p_list.add_argument("--workspace-root", default=".")
    p_list.add_argument("--json", action="store_true")

    p_decided = sub.add_parser("decided", help="probe a bead's recorded triage decision")
    p_decided.add_argument("--workspace-root", default=".")
    p_decided.add_argument("--key", required=True)
    p_decided.add_argument("--files", default=None)

    p_adj = sub.add_parser(
        "adjudicate-enabled",
        help="print whether [evolve] advisor_adjudicates is on (true/false)",
    )
    p_adj.add_argument("--workspace-root", default=".")

    p_adj_hot = sub.add_parser(
        "adjudicate-hot-enabled",
        help="print whether [evolve] adjudicate_hot is on (true/false)",
    )
    p_adj_hot.add_argument("--workspace-root", default=".")

    args = parser.parse_args(_default_to_list(argv))

    if args.command == "decided":
        return _cmd_decided(args, runner)
    if args.command == "adjudicate-enabled":
        return _cmd_adjudicate_enabled(args)
    if args.command == "adjudicate-hot-enabled":
        return _cmd_adjudicate_hot_enabled(args)
    return _cmd_list(args, runner)


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = [
    "adjudicate_hot",
    "advisor_adjudicates",
    "cli_main",
    "collect",
    "decided",
    "is_hot_change",
    "render_table",
]
