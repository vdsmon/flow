"""BM25 ranker over `.flow/<namespace>/knowledge.jsonl`.

Library + thin CLI. Stdlib-only. Hand-rolled BM25 implementation (no
rank-bm25 dep).

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

`--metric <name>` dispatches to metric.py; everything else is BM25 query mode.

Quarantine: malformed JSONL lines appended to sidecar
`<file>.quarantine.<ts>` (per-invocation); main file untouched; scan
continues with valid entries; never crash.

Semantic fusion (optional, gated by `[memory.semantic]`): when enabled and a
fresh sidecar index loads, the query is embedded once (via `memory_embed.embed`,
which shells a uvx subprocess), cosine-scored in pure Python against each indexed
live vector, threshold pre-filtered, then RRF-fused with the BM25 ranking. ANY
failure falls through to the unchanged BM25 `rank()` and a backend-status line on
stderr. Absent/off config → byte-identical pure BM25.

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
import tomllib
import unicodedata
from pathlib import Path
from typing import Any

import _memory_paths
from _jsonl import iter_jsonl

K1 = 1.5
B_PARAM = 0.75
RRF_K = 60
DEFAULT_THRESHOLD = 0.30
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


# ─── Supersession ────────────────────────────────────────────────────────────


def superseded_ids(entries: list[dict[str, Any]]) -> set[str]:
    """The dead-set: every non-empty `supersedes` target across all entries."""
    dead: set[str] = set()
    for e in entries:
        target = e.get("supersedes")
        if isinstance(target, str) and target:
            dead.add(target)
    return dead


def filter_superseded(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop entries whose id is named by some other entry's `supersedes`. Resolves
    an A<-B<-C chain for free: A and B are both referenced, only C survives."""
    dead = superseded_ids(entries)
    return [e for e in entries if e.get("id") not in dead]


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


# ─── Semantic config + fusion ──────────────────────────────────────────────────


def _load_config(workspace_root: Path) -> dict[str, Any]:
    """Read `[memory.semantic]` from workspace.toml. Absent block → {} (semantic off).

    Keys: `enabled` (bool), `model` (str), `threshold` (float), `embedder` (str).
    Any read/parse error returns {} so recall stays pure BM25.
    """
    path = workspace_root / ".flow" / "workspace.toml"
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    memory = data.get("memory")
    if not isinstance(memory, dict):
        return {}
    semantic = memory.get("semantic")
    return semantic if isinstance(semantic, dict) else {}


def _cosine(a: list[float], b: list[float]) -> float:
    """Pure-Python cosine similarity. 0.0 on a zero vector or a length mismatch."""
    if len(a) != len(b):
        return 0.0
    dot = na = nb = 0.0
    for x, y in zip(a, b, strict=True):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def rrf_fuse(
    bm25_order: list[str],
    cosine_order: list[str],
    *,
    k: int = RRF_K,
) -> dict[str, float]:
    """Reciprocal-rank fusion of two id rankings → {id: rrf_score}.

    Each list contributes `1/(k + rank)` (rank 0-based). An id present in one list
    only still scores from that list, so a cosine-missing (unindexed) entry still
    ranks via BM25 — the graceful partial-index property.
    """
    scores: dict[str, float] = {}
    for rank_i, eid in enumerate(bm25_order):
        scores[eid] = scores.get(eid, 0.0) + 1.0 / (k + rank_i)
    for rank_i, eid in enumerate(cosine_order):
        scores[eid] = scores.get(eid, 0.0) + 1.0 / (k + rank_i)
    return scores


def _semantic_rank(
    query: str,
    entries: list[dict[str, Any]],
    config: dict[str, Any],
    workspace_root: Path,
    namespace: str,
    *,
    branch_filter: str | None,
    ticket_filters: list[str] | None,
    threshold: float,
    top_n: int,
) -> tuple[list[dict[str, Any]], str]:
    """Hybrid cosine+BM25 fusion over the live entry set.

    Returns (results, status). Raises on any failure (caller falls back to BM25).
    """
    import memory_embed

    model = str(config.get("model") or memory_embed._DEFAULT_MODEL)
    embedder = config.get("embedder") or None

    header, indexed = memory_embed.load_index(workspace_root, namespace)
    if not indexed:
        raise RuntimeError("empty or missing index")
    if header.get("model") != model:
        raise RuntimeError(f"index model {header.get('model')!r} != configured {model!r}")

    query_vec = memory_embed.embed([query], model=model, embedder=embedder)[0]

    # cosine over indexed live entries, τ pre-filter, descending.
    cosine_scores: dict[str, float] = {}
    for entry in entries:
        eid = entry.get("id")
        if not isinstance(eid, str):
            continue
        vec = indexed.get(eid)
        if vec is None:
            continue
        sim = _cosine(query_vec, vec)
        if sim >= threshold:
            cosine_scores[eid] = sim
    cosine_order = sorted(cosine_scores, key=lambda e: -cosine_scores[e])

    # full BM25 ranking (all live entries), id order.
    bm25_results = rank(
        query=query,
        entries=entries,
        branch_filter=branch_filter,
        ticket_filters=ticket_filters,
        top_n=len(entries),
    )
    bm25_order = [str(r["id"]) for r in bm25_results if r.get("id") is not None]

    fused = rrf_fuse(bm25_order, cosine_order)
    by_id = {str(e.get("id")): e for e in entries}

    ticket_set_lower = {t.lower() for t in (ticket_filters or [])}
    branch_lower = branch_filter.lower() if branch_filter else None
    for eid, entry in by_id.items():
        if branch_lower is not None and _doc_field_text(entry, "branch").lower() == branch_lower:
            fused[eid] = fused.get(eid, 0.0) + BRANCH_EXACT_BONUS
        if ticket_set_lower and _doc_field_text(entry, "ticket").lower() in ticket_set_lower:
            fused[eid] = fused.get(eid, 0.0) + TICKET_EXACT_BONUS

    ranked_ids = sorted(
        fused,
        key=lambda eid: (-fused[eid], _neg_ts_key(str(by_id.get(eid, {}).get("ts", "")))),
    )
    results: list[dict[str, Any]] = []
    for eid in ranked_ids[:top_n]:
        entry = by_id.get(eid)
        if entry is None:
            continue
        results.append(
            {
                "id": entry.get("id"),
                "type": entry.get("type"),
                "branch": entry.get("branch"),
                "ticket": entry.get("ticket"),
                "body": entry.get("body"),
                "ts": entry.get("ts"),
                "score": round(fused[eid], 6),
            }
        )
    return results, f"semantic-active model={model} cosine_candidates={len(cosine_order)}"


# ─── CLI ─────────────────────────────────────────────────────────────────────


def _read_query(args: argparse.Namespace) -> str | None:
    """Resolve the query from the positional, then --query-file, then stdin.

    --query-file / stdin carry the ticket title+body so it is never a shell
    positional (avoids the `"`/`\\`/newline hazard). The positional still wins so
    existing `recall.py "<query>"` prose is byte-identical. None when none given.
    """
    if args.query is not None:
        return args.query
    if args.query_file:
        return Path(args.query_file).read_text(encoding="utf-8")
    try:
        if not sys.stdin.isatty():
            piped = sys.stdin.read()
            if piped.strip():
                return piped
    except (OSError, ValueError):
        return None
    return None


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="BM25 ranker over knowledge.jsonl.")
    parser.add_argument("query", nargs="?", default=None)
    parser.add_argument("--branch", default=None)
    parser.add_argument("--tickets", default=None, help="comma-separated ticket keys.")
    parser.add_argument("--ticket", default=None, help="ticket key for --record-pending.")
    parser.add_argument("--top-n", type=int, default=5)
    parser.add_argument("--include-superseded", action="store_true")
    parser.add_argument("--workspace-root", default=".")
    parser.add_argument("--semantic", action="store_true", help="force the semantic path on.")
    parser.add_argument("--threshold", type=float, default=None, help="cosine τ pre-filter.")
    parser.add_argument("--query-file", default=None, help="read the query from a file.")
    parser.add_argument(
        "--record-pending",
        action="store_true",
        help="append the recalled ids to recall-pending (needs --branch + --ticket).",
    )
    parser.add_argument("--reindex", action="store_true", help="dispatch to memory_embed reindex.")
    parser.add_argument("--full", action="store_true", help="with --reindex: force a full rebuild.")
    return parser.parse_args(argv)


def _record_pending(
    workspace_root: Path,
    *,
    branch: str,
    ticket: str,
    query: str,
    results: list[dict[str, Any]],
) -> None:
    """Append the recalled ids to recall-pending (the producer dispatch_stage init
    promotes into the run's recall-log). Best-effort: any failure is swallowed."""
    import subprocess

    import recall_pending

    ids = [str(r.get("id", "")) for r in results]
    scores = [str(r.get("score", "")) for r in results]
    head_sha = ""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(workspace_root),
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            head_sha = proc.stdout.strip()
    except OSError:
        head_sha = ""
    try:
        recall_pending.append_pending(
            workspace_root,
            hook_observed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            branch=branch,
            head_sha=head_sha,
            cwd=str(workspace_root),
            hook_time_resolved_ticket=ticket,
            query=query,
            returned_ids=ids,
            rank_scores=[float(s) for s in scores if s],
        )
    except Exception as exc:  # recording is a side-effect; never fail the recall
        sys.stderr.write(f"recall: record-pending skipped: {exc}\n")


def cli_main(argv: list[str]) -> int:
    # `recall.py --metric <...>` is a passthrough to the metric calculator so the
    # 14-day checkpoint has one entry point. Everything else is BM25 query mode.
    if "--metric" in argv:
        import metric

        return metric.cli_main([a for a in argv if a != "--metric"])
    args = _parse_args(argv)
    workspace_root = Path(args.workspace_root).resolve()

    # `--reindex` dispatches to memory_embed (mirrors the --metric → metric pattern,
    # but as a real argparse flag so the prose seam validates without a forwarder).
    if args.reindex:
        import memory_embed

        sub = ["reindex", "--workspace-root", str(workspace_root)]
        if args.full:
            sub.append("--full")
        return memory_embed.cli_main(sub)

    if args.record_pending and (not args.branch or not args.ticket):
        # validate before any embedding cost, not after
        sys.stderr.write("recall: --record-pending needs --branch and --ticket\n")
        return 1

    try:
        namespace = _memory_paths.resolve_namespace(workspace_root)
    except _memory_paths._MemoryConfigError as exc:
        sys.stderr.write(f"recall: {exc}\n")
        return 1
    kpath = _memory_paths.knowledge_path(workspace_root, namespace)
    entries = _load_entries(kpath)
    if not args.include_superseded:
        entries = filter_superseded(entries)
    tickets: list[str] = []
    if args.tickets:
        tickets = [t.strip() for t in args.tickets.split(",") if t.strip()]

    query = _read_query(args)
    if query is None:
        sys.stderr.write("recall: no query (positional, --query-file, or stdin)\n")
        return 1

    config = _load_config(workspace_root)
    semantic_on = args.semantic or bool(config.get("enabled"))
    threshold = (
        args.threshold
        if args.threshold is not None
        else float(config.get("threshold", DEFAULT_THRESHOLD))
    )

    results: list[dict[str, Any]] | None = None
    if semantic_on:
        try:
            results, status = _semantic_rank(
                query,
                entries,
                config,
                workspace_root,
                namespace,
                branch_filter=args.branch,
                ticket_filters=tickets or None,
                threshold=threshold,
                top_n=args.top_n,
            )
            sys.stderr.write(f"recall: {status}\n")
        except Exception as exc:
            sys.stderr.write(f"recall: bm25-fallback reason={type(exc).__name__}: {exc}\n")
            results = None

    if results is None:
        results = rank(
            query=query,
            entries=entries,
            branch_filter=args.branch,
            ticket_filters=tickets or None,
            top_n=args.top_n,
        )

    if args.record_pending:
        _record_pending(
            workspace_root,
            branch=args.branch,
            ticket=args.ticket,
            query=query,
            results=results,
        )

    sys.stdout.write(json.dumps(results, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = [
    "BRANCH_EXACT_BONUS",
    "B_PARAM",
    "DEFAULT_THRESHOLD",
    "FIELD_WEIGHTS",
    "K1",
    "RRF_K",
    "TICKET_EXACT_BONUS",
    "cli_main",
    "filter_superseded",
    "rank",
    "rrf_fuse",
    "superseded_ids",
    "tokenize",
]
