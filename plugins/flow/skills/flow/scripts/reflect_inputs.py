"""Bundle reflect-stage inputs into a single JSON payload.

Library + thin CLI. Stdlib-only.

Reads:
  - `<ticket-dir>/state.json` via `state.read()`
  - ticket frontmatter via `ticket_frontmatter.read()` (path derived from
    `--ticket-frontmatter <path>` flag, optional)
  - final diff via `diff_extract.diff_since_stage("ticket", ...)`
  - per-stage subagent reports via `state.json.stages.<name>.output_path`

Output: single JSON object to stdout, structured for the reflect LLM.

Exit codes:
  0 = ok.
  1 = state.json invalid/missing, or diff environment broken (git not on
      PATH / bad cwd raises FileNotFoundError, caught before _GitError).
  2 = diff-extract git error (git ran, returned nonzero — e.g. bad ref).
  3 = I/O error reading state.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path
from typing import Any

import _memory_paths
import _workspace
import diff_extract
import state
import ticket_frontmatter

# Reflect-stage gates, read from workspace.toml [reflect]. Defaults differ by
# blast radius: machinery (the harness self-edit lens) is OFF unless a skill
# developer opts in; claude_memory (writing the global ~/.claude memory) is ON
# because cross-session compounding is the safe-ship default.
_REFLECT_DEFAULTS = {"machinery": False, "claude_memory": True}


def _reflect_config(cwd: Path) -> dict[str, bool]:
    cfg = dict(_REFLECT_DEFAULTS)
    try:
        block = _workspace.load_workspace_toml(cwd).get("reflect", {})
    except _workspace.WorkspaceConfigError:
        return cfg
    for key in cfg:
        if isinstance(block.get(key), bool):
            cfg[key] = block[key]
    return cfg


def bundle(
    ticket: str,
    ticket_dir: Path,
    cwd: Path,
    ticket_frontmatter_path: Path | None = None,
) -> dict[str, Any]:
    """Return a JSON-serializable bundle of reflect-stage inputs.

    Raises:
        FileNotFoundError if state.json missing.
        state.StateUnrecoverable on state read failure.
        diff_extract._BaselineMissing / _GitError on diff failure.
    """
    ts, exit_code = state.read(ticket_dir)
    if ts is None or exit_code == 2:
        raise FileNotFoundError(f"no usable state.json at {ticket_dir}")

    fm: dict[str, Any] = {}
    if ticket_frontmatter_path is not None:
        fm = ticket_frontmatter.read(ticket_frontmatter_path)

    # diff_since_stage may raise BaselineMissing if ticket stage never started.
    # Allow caller to surface that via exit 2 from CLI.
    diff_payload: dict[str, Any] | None
    try:
        diff_payload = diff_extract.diff_since_stage("ticket", ticket_dir, cwd)
    except diff_extract._BaselineMissing:
        diff_payload = None

    subagent_reports: list[dict[str, Any]] = []
    for stage_name, record in ts.stages.items():
        out_path = record.output_path
        if not out_path:
            continue
        report_path = Path(out_path)
        body: str | None = None
        try:
            body = report_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            # an inline stage may record an output_path without ever writing the
            # file; an absent report is normal, not an error worth a warning.
            body = None
        except OSError as exc:
            sys.stderr.write(f"reflect-inputs: report file unreadable at {report_path}: {exc}\n")
        subagent_reports.append(
            {
                "stage": stage_name,
                "path": str(report_path),
                "body": body,
            }
        )

    # In-flight friction entries for THIS run — the primary evidence for reflect's
    # machinery lens. Tolerant of an absent log / unconfigured memory (best-effort).
    friction: list[dict[str, Any]] = []
    try:
        namespace = _memory_paths.resolve_namespace(cwd)
        fpath = _memory_paths.friction_path(cwd, namespace)
        if fpath.exists():
            for line in fpath.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    fe = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if fe.get("run_id") == ts.run_id:
                    friction.append(fe)
    except (_memory_paths._MemoryConfigError, OSError):
        pass

    return {
        "ticket": ticket,
        "run_id": ts.run_id,
        "state": dataclasses.asdict(ts),
        "ticket_frontmatter": fm,
        "final_diff": diff_payload,
        "subagent_reports": subagent_reports,
        "friction": friction,
        "reflect_config": _reflect_config(cwd),
    }


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bundle reflect-stage inputs into one JSON.")
    parser.add_argument("--ticket", required=True)
    parser.add_argument("--ticket-dir", required=True)
    parser.add_argument(
        "--ticket-frontmatter",
        default=None,
        help="path to ticket .md frontmatter file (optional).",
    )
    parser.add_argument("--cwd", default=".")
    return parser.parse_args(argv)


def cli_main(argv: list[str]) -> int:
    args = _parse_args(argv)
    ticket_dir = Path(args.ticket_dir).resolve()
    cwd = Path(args.cwd).resolve()
    fm_path = Path(args.ticket_frontmatter).resolve() if args.ticket_frontmatter else None
    try:
        payload = bundle(
            ticket=args.ticket,
            ticket_dir=ticket_dir,
            cwd=cwd,
            ticket_frontmatter_path=fm_path,
        )
    except FileNotFoundError as exc:
        sys.stderr.write(f"reflect-inputs: {exc}\n")
        return 1
    except state.StateUnrecoverable as exc:
        sys.stderr.write(f"reflect-inputs: state corrupt: {exc}\n")
        return 1
    except diff_extract._GitError as exc:
        sys.stderr.write(f"reflect-inputs: diff failed: {exc}\n")
        return 2
    except OSError as exc:
        sys.stderr.write(f"reflect-inputs: I/O error: {exc}\n")
        return 3
    sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = ["bundle", "cli_main"]
