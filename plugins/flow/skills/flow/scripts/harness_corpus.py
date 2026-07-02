"""Frozen decider-fixture corpus: loader + validator + replayer (lib, no CLI).

The corpus lives in the sibling `harness_corpus.json`: deterministic
(input -> expected output) cases for the four pure deciders in `DECIDERS`,
split `held_in` / `held_out`. `tests/test_harness_corpus.py` replays every
case against the live deciders, so any behavior change in one of them must
re-freeze the corpus in the same PR. The flow-63q.2 score CLI (`harness_eval.py`)
consumes `load_corpus` + `DECIDERS` and replays each skill checkout in its own
`drive` subprocess, because an in-process swap would share `sys.modules` across
checkouts. `run_case`/`replay` and their `resolve=` parameter are a test-injection
seam only (`tests/test_harness_corpus.py`), not the candidate-checkout path.

Stdlib-only, no side effects beyond reading the corpus file.
"""

from __future__ import annotations

import importlib
import json
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

# decider name -> (module, function, options). The only non-JSON-native
# parameter across all four is partition's `inflight_keys` set; `set_args`
# names the args the replayer converts list -> set before invocation.
DECIDERS: dict[str, tuple[str, str, dict[str, Any]]] = {
    "evolve_select.partition": ("evolve_select", "partition", {"set_args": ("inflight_keys",)}),
    "evolve_drain.decide": ("evolve_drain", "decide", {}),
    "evolve_self_merge.decide": ("evolve_self_merge", "decide", {}),
    "triage.is_hot_change": ("triage", "is_hot_change", {}),
}

_SPLITS = ("held_in", "held_out")

Resolver = Callable[[str], Any]


class CorpusError(Exception):
    pass


def _default_path() -> Path:
    return Path(__file__).resolve().parent / "harness_corpus.json"


def _validate_case(case: Any, index: int, seen_ids: set[str]) -> None:
    if not isinstance(case, dict):
        raise CorpusError(f"case #{index} is not an object")
    case_id = case.get("case_id")
    if not isinstance(case_id, str) or not case_id:
        raise CorpusError(f"case #{index}: case_id missing or empty")
    if case_id in seen_ids:
        raise CorpusError(f"duplicate case_id {case_id!r}")
    seen_ids.add(case_id)
    if case.get("split") not in _SPLITS:
        raise CorpusError(f"case {case_id!r}: split must be one of {_SPLITS}")
    if case.get("decider") not in DECIDERS:
        raise CorpusError(f"case {case_id!r}: unknown decider {case.get('decider')!r}")
    if not isinstance(case.get("args"), dict):
        raise CorpusError(f"case {case_id!r}: args must be an object")
    if "expected" not in case:
        raise CorpusError(f"case {case_id!r}: missing expected")


def load_corpus(path: Path | str | None = None) -> list[dict[str, Any]]:
    """Read + validate the corpus file; return its cases. CorpusError on any violation."""
    p = Path(path) if path is not None else _default_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CorpusError(f"cannot read corpus {p}: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise CorpusError("schema_version must be 1")
    cases = data.get("cases")
    if not isinstance(cases, list) or not cases:
        raise CorpusError("cases must be a non-empty list")
    seen_ids: set[str] = set()
    for index, case in enumerate(cases):
        _validate_case(case, index, seen_ids)
    return cases


def run_case(case: dict[str, Any], *, resolve: Resolver = importlib.import_module) -> Any:
    """Invoke the case's decider on its args; return the JSON-normalized result."""
    module_name, func_name, options = DECIDERS[case["decider"]]
    func = getattr(resolve(module_name), func_name)
    args = dict(case["args"])
    for name in options.get("set_args", ()):
        if name in args:
            args[name] = set(args[name])
    result = func(**args)
    return json.loads(json.dumps(result))


def replay(
    cases: Iterable[dict[str, Any]], *, resolve: Resolver = importlib.import_module
) -> list[dict[str, Any]]:
    """Replay cases; one row per case, with actual/expected attached on mismatch."""
    rows: list[dict[str, Any]] = []
    for case in cases:
        actual = run_case(case, resolve=resolve)
        row: dict[str, Any] = {
            "case_id": case["case_id"],
            "split": case["split"],
            "decider": case["decider"],
            "ok": actual == case["expected"],
        }
        if not row["ok"]:
            row["actual"] = actual
            row["expected"] = case["expected"]
        rows.append(row)
    return rows
