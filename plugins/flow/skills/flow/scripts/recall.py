"""BM25 ranker over `.flow/<namespace>/knowledge.jsonl`.

Library + thin CLI. Stdlib-only. Hand-rolled BM25 implementation per plan spec
(no rank-bm25 dep).

BM25 pinned params:
  k1 = 1.5
  b  = 0.75
  Tokenizer: re.findall(r'\\b\\w+\\b', NFKC(text).lower()). No stopwords.
  IDF scope: current namespace only.
  Field weights (multiplier on per-field token contribution):
    body=1.0, type=0.5, branch=1.5, ticket=2.0
  Exact-match boost (additive bonus on final score, so a requested exact match
  ranks first even when its BM25 text score is 0):
    branch match -> + BRANCH_EXACT_BONUS
    ticket match -> + TICKET_EXACT_BONUS  (stronger than branch)
  Tiebreak: ts DESC (ms precision); missing ts sorts last (oldest).

`--metric tickets-per-week` deferred to phase 8d. Mvp = query mode only.

Quarantine: malformed JSONL lines appended to sidecar
`<file>.quarantine.<ts>` (per-invocation); main file untouched; scan
continues with valid entries; never crash.

Exit codes:
  0 = ok (empty result still 0 with `[]`).
  1 = workspace invalid / namespace unresolvable.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any

import _memory_paths
from _jsonl import iter_jsonl

K1 = 1.5
B_PARAM = 0.75
FIELD_WEIGHTS: dict[str, float] = {
    "body": 1.0,
    "type": 0.5,
    "branch": 1.5,
    "ticket": 2.0,
}
# Additive exact-match bonuses. Sized to dominate any realistic BM25 text score
# so a requested exact match always sorts ahead of non-requested term matches,
# while preserving text-score ordering among records of equal exactness. Ticket
# bonus stays stronger than branch.
BRANCH_EXACT_BONUS = 100.0
TICKET_EXACT_BONUS = 1000.0

_TOKEN_RE = re.compile(r"\b\w+\b", re.UNICODE)


# ─── Tokenize ────────────────────────────────────────────────────────────────


def tokenize(text: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", text).lower()
    return _TOKEN_RE.findall(normalized)


# ─── Quarantine ──────────────────────────────────────────────────────────────


def _ts_token() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


# ─── Load ────────────────────────────────────────────────────────────────────


def _load_entries(knowledge_path: Path) -> list[dict[str, Any]]:
    if not knowledge_path.exists():
        return []
    # per-invocation sidecar so each scan's malformed lines land in their own file
    sidecar = knowledge_path.with_name(f"{knowledge_path.name}.quarantine.{_ts_token()}")
    return list(iter_jsonl(knowledge_path, sidecar))


# ─── BM25 ────────────────────────────────────────────────────────────────────


def _idf(n_docs: int, df: int) -> float:
    """Robertson/Spärck Jones with +1 smoothing; max(0, ...) to avoid negative IDF."""
    return max(0.0, math.log((n_docs - df + 0.5) / (df + 0.5) + 1.0))


def _doc_field_text(entry: dict[str, Any], field: str) -> str:
    value = entry.get(field, "")
    return str(value) if value is not None else ""


def _bm25_field_score(
    query_tokens: list[str],
    doc_tokens: list[str],
    idf_map: dict[str, float],
    avgdl: float,
) -> float:
    if not doc_tokens or avgdl == 0:
        return 0.0
    tf: dict[str, int] = {}
    for tok in doc_tokens:
        tf[tok] = tf.get(tok, 0) + 1
    score = 0.0
    dl = len(doc_tokens)
    for q in query_tokens:
        if q not in tf:
            continue
        f = tf[q]
        idf = idf_map.get(q, 0.0)
        num = f * (K1 + 1.0)
        den = f + K1 * (1.0 - B_PARAM + B_PARAM * dl / avgdl)
        score += idf * (num / den)
    return score


def _build_idf_map(
    query_tokens: list[str],
    docs_field_tokens: list[list[str]],
) -> dict[str, float]:
    n = len(docs_field_tokens)
    idf_map: dict[str, float] = {}
    doc_token_sets = [set(toks) for toks in docs_field_tokens]
    unique_query = set(query_tokens)
    for q in unique_query:
        df = sum(1 for toks in doc_token_sets if q in toks)
        idf_map[q] = _idf(n, df)
    return idf_map


def rank(
    query: str,
    entries: list[dict[str, Any]],
    branch_filter: str | None = None,
    ticket_filters: list[str] | None = None,
    top_n: int = 5,
) -> list[dict[str, Any]]:
    """Score entries with BM25, apply boosts, sort, return top_n."""
    if not entries:
        return []
    query_tokens = tokenize(query)
    if not query_tokens:
        return []
    # Per-field tokenization for every doc.
    per_field_tokens: dict[str, list[list[str]]] = {
        field: [tokenize(_doc_field_text(e, field)) for e in entries] for field in FIELD_WEIGHTS
    }
    # Per-field IDF map + avgdl.
    field_idf: dict[str, dict[str, float]] = {}
    field_avgdl: dict[str, float] = {}
    for field, docs_toks in per_field_tokens.items():
        field_idf[field] = _build_idf_map(query_tokens, docs_toks)
        total = sum(len(t) for t in docs_toks)
        field_avgdl[field] = total / len(docs_toks) if docs_toks else 0.0

    ticket_set_lower = {t.lower() for t in (ticket_filters or [])}
    branch_lower = branch_filter.lower() if branch_filter else None

    scored: list[tuple[float, dict[str, Any]]] = []
    for idx, entry in enumerate(entries):
        weighted_sum = 0.0
        for field, weight in FIELD_WEIGHTS.items():
            doc_toks = per_field_tokens[field][idx]
            field_score = _bm25_field_score(
                query_tokens, doc_toks, field_idf[field], field_avgdl[field]
            )
            weighted_sum += weight * field_score
        # Additive exact-match bonuses so a requested match ranks first even when
        # its BM25 text score is 0.
        if branch_lower is not None and _doc_field_text(entry, "branch").lower() == branch_lower:
            weighted_sum += BRANCH_EXACT_BONUS
        if ticket_set_lower and _doc_field_text(entry, "ticket").lower() in ticket_set_lower:
            weighted_sum += TICKET_EXACT_BONUS
        scored.append((weighted_sum, entry))

    # Sort by (score DESC, ts DESC). _neg_ts_key gives ts-descending via negated codepoints
    # (ISO8601 lexical order matches chronological, so negation flips to DESC).
    scored.sort(key=lambda pair: (-pair[0], _neg_ts_key(pair[1].get("ts", ""))))

    results: list[dict[str, Any]] = []
    for score, entry in scored[:top_n]:
        results.append(
            {
                "id": entry.get("id"),
                "type": entry.get("type"),
                "branch": entry.get("branch"),
                "ticket": entry.get("ticket"),
                "body": entry.get("body"),
                "ts": entry.get("ts"),
                "score": round(score, 6),
            }
        )
    return results


def _neg_ts_key(ts: str) -> tuple[int, ...]:
    """Sort key for ts DESC tiebreak. ISO8601 lexical ordering matches chrono,
    so negate via tuple of negative codepoints. The leading presence flag (0 for
    present, 1 for missing/empty) forces a missing ts to sort last (oldest)
    instead of first.
    """
    if not ts:
        return (1,)
    return (0, *(-ord(c) for c in ts))


# ─── CLI ─────────────────────────────────────────────────────────────────────


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="BM25 ranker over knowledge.jsonl.")
    parser.add_argument("query")
    parser.add_argument("--branch", default=None)
    parser.add_argument("--tickets", default=None, help="comma-separated ticket keys.")
    parser.add_argument("--top-n", type=int, default=5)
    parser.add_argument("--workspace-root", default=".")
    return parser.parse_args(argv)


def cli_main(argv: list[str]) -> int:
    # `recall.py --metric <...>` is a passthrough to the metric calculator so the
    # 14-day checkpoint has one entry point. Everything else is BM25 query mode.
    if "--metric" in argv:
        import metric

        return metric.cli_main([a for a in argv if a != "--metric"])
    args = _parse_args(argv)
    workspace_root = Path(args.workspace_root).resolve()
    try:
        namespace = _memory_paths.resolve_namespace(workspace_root)
    except _memory_paths._MemoryConfigError as exc:
        sys.stderr.write(f"recall: {exc}\n")
        return 1
    kpath = _memory_paths.knowledge_path(workspace_root, namespace)
    entries = _load_entries(kpath)
    tickets: list[str] = []
    if args.tickets:
        tickets = [t.strip() for t in args.tickets.split(",") if t.strip()]
    results = rank(
        query=args.query,
        entries=entries,
        branch_filter=args.branch,
        ticket_filters=tickets or None,
        top_n=args.top_n,
    )
    sys.stdout.write(json.dumps(results, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = [
    "BRANCH_EXACT_BONUS",
    "B_PARAM",
    "FIELD_WEIGHTS",
    "K1",
    "TICKET_EXACT_BONUS",
    "cli_main",
    "rank",
    "tokenize",
]
