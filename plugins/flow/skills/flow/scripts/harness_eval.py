"""Regression-eval scorer over the frozen decider corpus (epic flow-63q).

Library + thin CLI. Stdlib-only.

`score` replays the frozen corpus (`harness_corpus.load_corpus`) against a
candidate skill-checkout AND a baseline checkout (default: the checkout this
script lives in) and emits a per-split (held_in / held_out) pass/regress delta
JSON plus `non_regression`. Raw data only; gating policy lives in the merge
gate (flow-63q.4).

Each checkout is replayed in its own subprocess via the internal `drive`
subcommand (JSON payload on stdin), so baseline and candidate never share
`sys.modules` and every decider's sibling imports resolve inside the target
checkout, not this one.

Exit codes:
  0 = scored, non_regression true
  1 = bad args / environment (candidate or baseline dir missing)
  2 = corpus or driver error (CorpusError, driver nonzero exit, unparseable
      driver stdout, case-set mismatch, subprocess timeout); argparse usage
      errors also exit 2
  3 = scored, at least one regressed case
"""

from __future__ import annotations

import argparse
import importlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import harness_corpus

DEFAULT_TIMEOUT_SECS = 120.0

_SPLITS = ("held_in", "held_out")


class EvalError(Exception):
    pass


# ─── Driver (runs in the per-checkout subprocess) ────────────────────────────


def _drive() -> int:
    """Replay the stdin payload's cases inside its scripts_dir.

    Scrubs this script's own dir (sys.path[0] when run as a script) and any
    cwd/"" entries from sys.path, then fronts the target checkout, BEFORE any
    decider module is resolved. The parent's `harness_corpus` is already in
    sys.modules from the module-level import; that is safe because no decider
    imports harness_corpus (deciders come from the payload, not from it).
    """
    try:
        payload = json.load(sys.stdin)
        scripts_dir = payload["scripts_dir"]
        deciders = payload["deciders"]
        cases = payload["cases"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        sys.stderr.write(f"harness-eval drive: bad payload: {exc}\n")
        return 2
    parent_dir = str(Path(__file__).resolve().parent)
    cwd = str(Path.cwd())
    sys.path[:] = [p for p in sys.path if p not in ("", ".", parent_dir, cwd)]
    sys.path.insert(0, scripts_dir)
    rows: list[dict[str, Any]] = []
    for case in cases:
        row: dict[str, Any] = {
            "case_id": case["case_id"],
            "split": case["split"],
            "decider": case["decider"],
        }
        try:
            module_name, func_name, options = deciders[case["decider"]]
            func = getattr(importlib.import_module(module_name), func_name)
            args = dict(case["args"])
            for name in options.get("set_args", ()):
                if name in args:
                    args[name] = set(args[name])
            actual = json.loads(json.dumps(func(**args)))
            row["ok"] = actual == case["expected"]
            if not row["ok"]:
                row["actual"] = actual
                row["expected"] = case["expected"]
        except Exception as exc:
            row["ok"] = False
            row["error"] = f"{type(exc).__name__}: {exc}"
        rows.append(row)
    sys.stdout.write(json.dumps({"rows": rows}) + "\n")
    return 0


# ─── Public API ──────────────────────────────────────────────────────────────


def replay_checkout(
    scripts_dir: Path | str,
    cases: list[dict[str, Any]],
    *,
    timeout: float = DEFAULT_TIMEOUT_SECS,
) -> list[dict[str, Any]]:
    """Replay cases inside scripts_dir via a `drive` subprocess; return its rows.

    Raises:
        EvalError (driver nonzero exit, unparseable stdout, timeout)
    """
    target = str(Path(scripts_dir).resolve())
    payload = {
        "scripts_dir": target,
        "deciders": {
            name: [module, func, {"set_args": list(options.get("set_args", ()))}]
            for name, (module, func, options) in harness_corpus.DECIDERS.items()
        },
        "cases": cases,
    }
    try:
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "drive"],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise EvalError(f"driver timed out after {timeout}s for {target}") from exc
    if proc.returncode != 0:
        raise EvalError(
            f"driver failed for {target} (exit {proc.returncode}): {proc.stderr.strip()}"
        )
    try:
        rows = json.loads(proc.stdout)["rows"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise EvalError(f"driver output unparseable for {target}: {exc}") from exc
    if not isinstance(rows, list):
        raise EvalError(f"driver output rows is not a list for {target}")
    return rows


def score_delta(
    baseline_rows: list[dict[str, Any]], candidate_rows: list[dict[str, Any]]
) -> dict[str, Any]:
    """Per-split pass/regress delta between two replay row sets.

    Raises:
        EvalError (case-set mismatch between the two row sets)
    """
    base = {row["case_id"]: row for row in baseline_rows}
    cand = {row["case_id"]: row for row in candidate_rows}
    if set(base) != set(cand):
        raise EvalError(
            "case-set mismatch: "
            f"baseline-only {sorted(set(base) - set(cand))}, "
            f"candidate-only {sorted(set(cand) - set(base))}"
        )
    splits: dict[str, dict[str, Any]] = {}
    non_regression = True
    for split in _SPLITS:
        ids = sorted(cid for cid, row in base.items() if row["split"] == split)
        regressed: list[str] = []
        improved: list[str] = []
        detail: dict[str, dict[str, Any]] = {}
        baseline_pass = candidate_pass = 0
        for cid in ids:
            b_ok = bool(base[cid].get("ok"))
            c_ok = bool(cand[cid].get("ok"))
            baseline_pass += b_ok
            candidate_pass += c_ok
            if b_ok and not c_ok:
                regressed.append(cid)
                crow = cand[cid]
                if "error" in crow:
                    detail[cid] = {"error": crow["error"]}
                else:
                    detail[cid] = {"actual": crow.get("actual"), "expected": crow.get("expected")}
            elif c_ok and not b_ok:
                improved.append(cid)
        if regressed:
            non_regression = False
        splits[split] = {
            "cases": len(ids),
            "baseline_pass": baseline_pass,
            "baseline_fail": len(ids) - baseline_pass,
            "candidate_pass": candidate_pass,
            "candidate_fail": len(ids) - candidate_pass,
            "regressed": regressed,
            "improved": improved,
            "detail": detail,
        }
    return {"cases": len(base), "splits": splits, "non_regression": non_regression}


# ─── CLI ─────────────────────────────────────────────────────────────────────


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Regression-eval scorer: replay the frozen decider corpus "
        "against a candidate skill-checkout vs a baseline."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_score = sub.add_parser("score")
    p_score.add_argument("--candidate", required=True, help="candidate scripts dir")
    p_score.add_argument(
        "--baseline", default=None, help="baseline scripts dir (default: this checkout)"
    )
    p_score.add_argument(
        "--corpus", default=None, help="corpus file (default: sibling harness_corpus.json)"
    )
    p_score.add_argument("--timeout-secs", type=float, default=DEFAULT_TIMEOUT_SECS)

    sub.add_parser("drive", help="internal stdin-JSON replay driver")

    return parser.parse_args(argv)


def cli_main(argv: list[str]) -> int:
    args = _parse_args(argv)
    if args.command == "drive":
        return _drive()

    candidate = Path(args.candidate).resolve()
    baseline = Path(args.baseline).resolve() if args.baseline else Path(__file__).resolve().parent
    if not candidate.is_dir():
        sys.stderr.write(f"harness-eval: candidate scripts dir not found: {candidate}\n")
        return 1
    if not baseline.is_dir():
        sys.stderr.write(f"harness-eval: baseline scripts dir not found: {baseline}\n")
        return 1
    try:
        cases = harness_corpus.load_corpus(args.corpus)
        baseline_rows = replay_checkout(baseline, cases, timeout=args.timeout_secs)
        candidate_rows = replay_checkout(candidate, cases, timeout=args.timeout_secs)
        result = score_delta(baseline_rows, candidate_rows)
    except (harness_corpus.CorpusError, EvalError) as exc:
        sys.stderr.write(f"harness-eval: {exc}\n")
        return 2
    result["baseline"] = str(baseline)
    result["candidate"] = str(candidate)
    sys.stdout.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return 0 if result["non_regression"] else 3


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = [
    "DEFAULT_TIMEOUT_SECS",
    "EvalError",
    "cli_main",
    "replay_checkout",
    "score_delta",
]
