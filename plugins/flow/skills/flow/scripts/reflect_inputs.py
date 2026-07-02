"""Bundle reflect-stage inputs into a single JSON payload.

Library + thin CLI. Stdlib-only.

Reads:
  - `<ticket-dir>/state.json` via `state.read()`
  - ticket frontmatter via `ticket_frontmatter.read()` (path derived from
    `--ticket-frontmatter <path>` flag, optional)
  - final diff via `diff_extract.diff_since_stage("ticket", ...)`
  - per-stage subagent reports via `state.json.stages.<name>.output_path`

Output: single JSON object to stdout, structured for the reflect LLM. Includes
a best-effort `harness_eval` availability block advertising the frozen-corpus
regression eval (`harness_eval.py score`) to the reflect agent.

Exit codes:
  0 = ok.
  1 = state.json invalid/missing, or diff environment broken (git not on
      PATH / bad cwd raises FileNotFoundError, caught before _GitError).
  2 = diff-extract git error (git ran, returned nonzero, e.g. bad ref).
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
import harness_corpus
import recall
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


def _harness_eval_block(scripts_dir: Path | None = None) -> dict[str, Any]:
    if scripts_dir is None:
        scripts_dir = Path(__file__).resolve().parent
    eval_path = scripts_dir / "harness_eval.py"
    corpus_path = scripts_dir / "harness_corpus.json"
    try:
        if not eval_path.is_file():
            return {"available": False, "reason": f"harness_eval.py not found at {eval_path}"}
        cases = harness_corpus.load_corpus(corpus_path)
    except (harness_corpus.CorpusError, OSError) as exc:
        return {"available": False, "reason": str(exc)}
    counts = {"held_in": 0, "held_out": 0}
    for case in cases:
        counts[case["split"]] += 1
    return {
        "available": True,
        "eval_path": str(eval_path),
        "corpus_path": str(corpus_path),
        "case_counts": counts,
    }


def _lenient_jsonl(path: Path) -> list[Any]:
    """Per-line json.loads, skipping blanks + malformed lines. Read-only, never writes a
    quarantine sidecar (mirrors the friction read in bundle()).
    """
    out: list[Any] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _recalled_ids(log_path: Path) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    for rec in _lenient_jsonl(log_path):
        if not isinstance(rec, dict):
            continue
        for rid in rec.get("returned_ids", []):
            if isinstance(rid, str) and rid and rid not in seen:
                seen.add(rid)
                ids.append(rid)
    return ids


def _recalled_entries(ticket_dir: Path, cwd: Path) -> list[dict[str, Any]]:
    """Entries recalled INTO this run, joined recall-log `returned_ids` -> live
    knowledge bodies. Best-effort: any missing log / knowledge / memory-config
    degrades to []. Read-only (no quarantine sidecar): mirrors the friction read.
    """
    log_path = ticket_dir / "recall-log.jsonl"
    if not log_path.exists():
        return []
    try:
        recalled_ids = _recalled_ids(log_path)
        if not recalled_ids:
            return []
        namespace = _memory_paths.resolve_namespace(cwd)
        kpath = _memory_paths.knowledge_path(cwd, namespace)
        if not kpath.exists():
            return []
        by_id: dict[str, dict[str, Any]] = {
            e["id"]: e
            for e in _lenient_jsonl(kpath)
            if isinstance(e, dict) and isinstance(e.get("id"), str)
        }
        dead = recall.superseded_ids(list(by_id.values()))
        out: list[dict[str, Any]] = []
        for rid in recalled_ids:
            e = by_id.get(rid)
            if rid in dead or e is None:
                continue
            out.append(
                {
                    "id": rid,
                    "type": e.get("type"),
                    "body": e.get("body"),
                    "ts": e.get("ts"),
                    "branch": e.get("branch"),
                    "ticket": e.get("ticket"),
                }
            )
        return out
    except (_memory_paths._MemoryConfigError, OSError):
        return []


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

    # In-flight friction entries for THIS run, the primary evidence for reflect's machinery
    # lens. Tolerant of an absent log / unconfigured memory (best-effort).
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
        "recalled_entries": _recalled_entries(ticket_dir, cwd),
        "reflect_config": _reflect_config(cwd),
        "harness_eval": _harness_eval_block(),
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
