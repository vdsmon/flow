"""Recall observability: usage (precision) + miss (false-negative) records.

Library + thin CLI. The writer is stdlib-only (flock + fsync append, mirrors
`memory_append`/`flow_friction`); the miss-detector shells the embedder via
`memory_embed` for the FEW new entries only (best-effort, reflect-stage), never
on the hot knowledge write path.

One append-only file `.flow/<namespace>/recall-usage.jsonl`, two record kinds:

  usage: {"kind":"usage","run_id","ticket","recalled_id","used":bool,"ts"}
    one per entry surfaced INTO a run (the recall-log `returned_ids`); `used` is
    the reflect agent's judgment that the entry informed the work or was
    superseded by it. Precision = used / surfaced.

  miss:  {"kind":"miss","type":"RECALL_MISS","run_id","ticket","relearned_id",
          "missed_id","similarity","ts"}
    a near-duplicate of an existing live entry was written THIS run while that
    existing entry was NOT recalled. The run re-learned a fact it already had.
    A false-negative proxy for recall.

The metric reads one file: `metric.py recall-hit-rate` joins both kinds.

Both record kinds are deduped on a stable per-run key so a `FLOW workspace repair` rerun
(same `run_id`) does not double-count. The surfaced set is defined ONE way (the
per-run recall-log `returned_ids`), so the agent only judges `--used-ids`.

Exit codes:
  0 = ok (records written, possibly zero, e.g. semantic off, model mismatch).
  2 = lock contention.
  3 = invalid args (no state.json / unresolvable run).
  4 = I/O error, or workspace memory config missing/invalid.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import _memory_paths
import recall
import state
from _jsonl import iter_jsonl
from _locking import LockContention, flock_retry
from _timeutil import ts_token, utcnow_iso_ms

# bge-small cosines for true near-duplicates sit very high; keep the gate
# conservative so a false miss never poisons the trust the metric exists to
# build. Calibrate down later from the stderr near-miss diagnostics, not blind.
MISS_SIMILARITY = 0.90
# below the gate but worth surfacing so the real knee can be picked from data.
_NEAR_MISS_FLOOR = 0.70


# ─── Paths ───────────────────────────────────────────────────────────────────


def recall_usage_path(workspace_root: Path, namespace: str) -> Path:
    return _memory_paths.namespace_root(workspace_root, namespace) / "recall-usage.jsonl"


def _lock_path(workspace_root: Path, namespace: str) -> Path:
    return recall_usage_path(workspace_root, namespace).with_name("recall-usage.jsonl.lock")


def _quarantine_path(workspace_root: Path, namespace: str) -> Path:
    path = recall_usage_path(workspace_root, namespace)
    return path.with_name(f"{path.name}.quarantine.{ts_token()}")


def _recall_log_path(workspace_root: Path, ticket: str) -> Path:
    return workspace_root / ".flow" / "runs" / ticket / "recall-log.jsonl"


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _append_records(
    workspace_root: Path,
    namespace: str,
    records: list[dict[str, Any]],
    *,
    seen: Any,
) -> list[dict[str, Any]]:
    """Append `records` whose dedup key (via `seen(record)`) is not already on
    disk. Holds the recall-usage flock for the read + the append. Returns the
    records actually written.
    """
    if not records:
        return []
    path = recall_usage_path(workspace_root, namespace)
    quarantine = _quarantine_path(workspace_root, namespace)
    written: list[dict[str, Any]] = []
    with flock_retry(_lock_path(workspace_root, namespace)):
        present = {seen(rec) for rec in iter_jsonl(path, quarantine)}
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            for rec in records:
                key = seen(rec)
                if key in present:
                    continue
                present.add(key)
                fh.write(json.dumps(rec, sort_keys=True) + "\n")
                written.append(rec)
            fh.flush()
            os.fsync(fh.fileno())
    return written


def _surfaced_ids(workspace_root: Path, ticket: str) -> list[str]:
    """Distinct ids surfaced into the run (the recall-log `returned_ids`), in
    first-seen order. Empty when no recall-log (nothing was recalled).
    """
    log_path = _recall_log_path(workspace_root, ticket)
    if not log_path.exists():
        return []
    ids: list[str] = []
    seen: set[str] = set()
    sidecar = log_path.with_name(f"{log_path.name}.quarantine.{ts_token()}")
    for rec in iter_jsonl(log_path, sidecar):
        for rid in rec.get("returned_ids", []):
            if isinstance(rid, str) and rid and rid not in seen:
                seen.add(rid)
                ids.append(rid)
    return ids


def _run_id_started_at(ticket_dir: Path) -> tuple[str, str]:
    """(run_id, started_at) from the run's state.json. Raises FileNotFoundError
    when there is no usable state.json."""
    ts, exit_code = state.read(ticket_dir)
    if ts is None or exit_code == 2:
        raise FileNotFoundError(f"no usable state.json at {ticket_dir}")
    return ts.run_id, ts.started_at


def aggregate_usage(workspace_root: Path, namespace: str) -> dict[str, dict[str, Any]]:
    """Lifetime per-entry rollup of recall-usage.jsonl, keyed by knowledge id.

    Deliberately unwindowed: pruning judges an entry's whole service record, not the last 14 days
    (contrast metric.compute_recall_hit_rate). `usage` records bucket by `recalled_id`; `miss`
    records bucket by `missed_id`: a missed entry got RE-LEARNED while unrecalled, evidence it is
    valuable, so the prune lane reads miss_count as a keep signal. last_surfaced is the
    lexicographic max ts (every writer emits ISO-Z, so string order is time order). Missing file ->
    {}.
    """
    path = recall_usage_path(workspace_root, namespace)
    out: dict[str, dict[str, Any]] = {}

    def bucket(entry_id: str) -> dict[str, Any]:
        return out.setdefault(
            entry_id,
            {"surfaced_count": 0, "used_count": 0, "miss_count": 0, "last_surfaced": None},
        )

    if not path.exists():
        return out
    sidecar = path.with_name(f"{path.name}.quarantine.{ts_token()}")
    for rec in iter_jsonl(path, sidecar):
        kind = rec.get("kind")
        if kind == "usage":
            rid = rec.get("recalled_id")
            if not isinstance(rid, str) or not rid:
                continue
            agg = bucket(rid)
            agg["surfaced_count"] += 1
            if rec.get("used") is True:
                agg["used_count"] += 1
            ts = rec.get("ts")
            if isinstance(ts, str) and (agg["last_surfaced"] is None or ts > agg["last_surfaced"]):
                agg["last_surfaced"] = ts
        elif kind == "miss":
            mid = rec.get("missed_id")
            if isinstance(mid, str) and mid:
                bucket(mid)["miss_count"] += 1
    return out


# ─── Public API ──────────────────────────────────────────────────────────────


def record_usage(
    workspace_root: Path,
    *,
    ticket: str,
    ticket_dir: Path,
    used_ids: list[str],
) -> list[dict[str, Any]]:
    """Write one usage record per surfaced id (recall-log `returned_ids`),
    `used` iff the id is in `used_ids`. Deduped on (run_id, recalled_id).

    Raises:
        FileNotFoundError (no state.json)
        LockContention
        _memory_paths._MemoryConfigError
        OSError
    """
    run_id, _started_at = _run_id_started_at(ticket_dir)
    namespace = _memory_paths.resolve_namespace(workspace_root)
    surfaced = _surfaced_ids(workspace_root, ticket)
    used = set(used_ids)
    now = utcnow_iso_ms()
    records = [
        {
            "kind": "usage",
            "run_id": run_id,
            "ticket": ticket,
            "recalled_id": rid,
            "used": rid in used,
            "ts": now,
        }
        for rid in surfaced
    ]
    return _append_records(
        workspace_root,
        namespace,
        records,
        seen=lambda r: ("usage", r.get("run_id"), r.get("recalled_id")),
    )


def detect_misses(
    workspace_root: Path,
    *,
    ticket: str,
    ticket_dir: Path,
    threshold: float = MISS_SIMILARITY,
) -> list[dict[str, Any]]:
    """Flag near-duplicate re-learns: entries written THIS run whose nearest live
    neighbor (cosine >= threshold) was NOT recalled into the run.

    Self-contained and best-effort. Returns [] (no-op, no embedder shelled) when:
    semantic is disabled; no entries were written this run; the sidecar index is
    absent or its model != the configured model (the post-swap reindex hazard,
    comparing a fresh bge vector to a potion-era index is garbage); or the
    embedder is unavailable. New entries are embedded FRESH (1..N texts) rather
    than read from the sidecar, so a stale/failed reindex cannot silently starve
    detection the way an absolute threshold once starved recall.

    Raises:
        FileNotFoundError (no state.json)
        LockContention
        _memory_paths._MemoryConfigError
        OSError
    """
    config = _memory_paths.load_semantic_config(workspace_root)
    if not config.get("enabled"):
        return []
    run_id, started_at = _run_id_started_at(ticket_dir)
    namespace = _memory_paths.resolve_namespace(workspace_root)
    kpath = _memory_paths.knowledge_path(workspace_root, namespace)
    if not kpath.exists():
        return []
    entries = recall._load_entries(kpath)
    live = recall.filter_superseded(entries)

    new_entries = [
        e
        for e in live
        if e.get("ticket") == ticket
        and isinstance(e.get("ts"), str)
        and e["ts"] >= started_at
        and isinstance(e.get("id"), str)
    ]
    if not new_entries:
        return []
    new_ids = {e["id"] for e in new_entries}

    import memory_embed

    model = str(config.get("model") or memory_embed._DEFAULT_MODEL)
    embedder = config.get("embedder") or None
    header, indexed = memory_embed.load_index(workspace_root, namespace)
    if not indexed or header.get("model") != model:
        return []

    # candidate matches: pre-existing live entries with an indexed vector. A new
    # entry duplicating ANOTHER new entry is not a recall miss (both are this run),
    # and a superseded entry is unreturnable (recall filters it before ranking), so
    # a stale indexed vector for it must never be blamed as a miss.
    live_ids = {e["id"] for e in live if isinstance(e.get("id"), str)}
    candidates = [
        (eid, vec) for eid, vec in indexed.items() if eid in live_ids and eid not in new_ids
    ]
    if not candidates:
        return []

    try:
        new_vecs = memory_embed.embed(
            [memory_embed._entry_text(e) for e in new_entries], model=model, embedder=embedder
        )
    except memory_embed._EmbedderUnavailable as exc:
        sys.stderr.write(f"recall-usage: detect-misses embedder unavailable: {exc}\n")
        return []

    surfaced = set(_surfaced_ids(workspace_root, ticket))
    now = utcnow_iso_ms()
    records: list[dict[str, Any]] = []
    for entry, vec in zip(new_entries, new_vecs, strict=True):
        best_id, best_sim = "", -1.0
        for cand_id, cand_vec in candidates:
            sim = recall._cosine(vec, cand_vec)
            if sim > best_sim:
                best_id, best_sim = cand_id, sim
        if best_sim >= threshold and best_id not in surfaced:
            records.append(
                {
                    "kind": "miss",
                    "type": "RECALL_MISS",
                    "run_id": run_id,
                    "ticket": ticket,
                    "relearned_id": entry["id"],
                    "missed_id": best_id,
                    "similarity": round(best_sim, 4),
                    "ts": now,
                }
            )
        elif best_sim >= _NEAR_MISS_FLOOR:
            # below the gate: surface for later threshold calibration, do not record.
            sys.stderr.write(
                f"recall-usage: near-miss {entry['id']} ~ {best_id} "
                f"sim={round(best_sim, 4)} (recalled={best_id in surfaced})\n"
            )
    return _append_records(
        workspace_root,
        namespace,
        records,
        seen=lambda r: ("miss", r.get("run_id"), r.get("relearned_id"), r.get("missed_id")),
    )


# ─── CLI ─────────────────────────────────────────────────────────────────────


def _split_csv(value: str) -> list[str]:
    return [part for part in value.split(",") if part] if value else []


def _parse_args(argv: list[str]) -> argparse.Namespace:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--workspace-root", default=".")

    parser = argparse.ArgumentParser(
        description="Recall observability: record-usage / detect-misses.", parents=[common]
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_usage = sub.add_parser("record-usage", parents=[common])
    p_usage.add_argument("--ticket", required=True)
    p_usage.add_argument("--ticket-dir", required=True)
    p_usage.add_argument(
        "--used-ids", default="", help="comma-separated recalled ids the run leaned on."
    )

    p_miss = sub.add_parser("detect-misses", parents=[common])
    p_miss.add_argument("--ticket", required=True)
    p_miss.add_argument("--ticket-dir", required=True)
    p_miss.add_argument("--threshold", type=float, default=MISS_SIMILARITY)

    return parser.parse_args(argv)


def cli_main(argv: list[str]) -> int:
    args = _parse_args(argv)
    workspace_root = Path(args.workspace_root).resolve()
    ticket_dir = Path(args.ticket_dir).resolve()
    try:
        if args.command == "record-usage":
            written = record_usage(
                workspace_root,
                ticket=args.ticket,
                ticket_dir=ticket_dir,
                used_ids=_split_csv(args.used_ids),
            )
        else:
            written = detect_misses(
                workspace_root,
                ticket=args.ticket,
                ticket_dir=ticket_dir,
                threshold=args.threshold,
            )
    except FileNotFoundError as exc:
        sys.stderr.write(f"recall-usage: {exc}\n")
        return 3
    except LockContention as exc:
        sys.stderr.write(f"recall-usage: {exc}\n")
        return 2
    except _memory_paths._MemoryConfigError as exc:
        sys.stderr.write(f"recall-usage: {exc}\n")
        return 4
    except OSError as exc:
        sys.stderr.write(f"recall-usage: I/O error: {exc}\n")
        return 4
    sys.stdout.write(json.dumps(written, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = [
    "MISS_SIMILARITY",
    "aggregate_usage",
    "cli_main",
    "detect_misses",
    "recall_usage_path",
    "record_usage",
]
