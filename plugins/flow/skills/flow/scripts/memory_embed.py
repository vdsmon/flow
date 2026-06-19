"""Embedder seam + derived sidecar index for semantic recall.

Pure stdlib. Never imports the embedding model — that lives ONLY inside the uvx
subprocess (`embedder_fastembed.py`, the default; `embedder_model2vec.py`, the
lighter static alternative). The runtime python3 cannot import them, so the
embedder is a CONFIGURED COMMAND that is shelled: newline texts on stdin, a JSON
array of vectors on stdout. recall.py catches `_EmbedderUnavailable` and falls
through to pure BM25.

Sidecar index `.flow/<namespace>/knowledge.embed` (derived; never the
source-of-truth, which stays `knowledge.jsonl`):
  line 1  header  `{"_header": {"model": "<id>", "dim": <int>, "ts": "<iso>"}}`
  body    `{"id": "<entry-id>", "v": [<float>, ...]}` per live entry.
Read via the quarantine-tolerant `iter_jsonl`.

Embedder command resolution:
  1. `[memory.semantic].embedder` (string) when set → shell it.
  2. else the shipped default: `uvx --with fastembed python
     <scripts-dir>/embedder_fastembed.py --model <id>`.
  A missing command / `uvx` absent / nonzero exit / unparseable stdout →
  `_EmbedderUnavailable`.

CLI:
  `memory_embed.py reindex --workspace-root . [--full]` — refresh the sidecar.
  `memory_embed.py embed` — stdin texts → JSON vectors (exercises the contract).

Exit codes:
  0 = ok.
  1 = workspace invalid / namespace unresolvable.
  2 = embedder unavailable (recall would fall back to BM25).
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import _memory_paths
import recall
from _jsonl import iter_jsonl
from _locking import flock_retry

_DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"
# bound the embedder subprocess: a wedged model download / hung uvx must not
# stall the plan phase indefinitely -> a timeout maps to _EmbedderUnavailable
# (BM25 fallback), same as any other embedder failure. The ceiling SCALES with
# batch size: the base covers the fixed cold-start (model load + uvx env resolve),
# the per-text term covers throughput. A single plan-phase query (1 text) stays a
# fast-fail (~base); a full-corpus reindex needs real headroom — bge/ONNX is far
# heavier than the old static model2vec, and the flat 120s ceiling killed a
# 337-entry reindex mid-batch.
_EMBED_TIMEOUT_BASE_S = 120
_EMBED_TIMEOUT_PER_TEXT_S = 2.0


class _EmbedderUnavailable(Exception):
    """The configured embedder could not produce vectors. recall falls back to BM25."""


# ─── Paths ───────────────────────────────────────────────────────────────────


def embed_index_path(workspace_root: Path, namespace: str) -> Path:
    return _memory_paths.namespace_root(workspace_root, namespace) / "knowledge.embed"


def _embed_lock_path(workspace_root: Path, namespace: str) -> Path:
    return _memory_paths.namespace_root(workspace_root, namespace) / "knowledge.embed.lock"


def _scripts_dir() -> Path:
    return Path(__file__).resolve().parent


# ─── Embedder seam ─────────────────────────────────────────────────────────────


def _default_command(model: str) -> list[str]:
    return [
        "uvx",
        "--with",
        "fastembed",
        "python",
        str(_scripts_dir() / "embedder_fastembed.py"),
        "--model",
        model,
    ]


def _resolve_command(model: str, embedder: str | None) -> list[str]:
    if embedder:
        return shlex.split(embedder)
    return _default_command(model)


def embed(
    texts: list[str],
    *,
    model: str = _DEFAULT_MODEL,
    embedder: str | None = None,
) -> list[list[float]]:
    """Embed `texts` by shelling the configured command (batch: one invocation).

    Raises `_EmbedderUnavailable` on a missing command, nonzero exit, or
    unparseable stdout. An empty input returns `[]` without shelling.
    """
    if not texts:
        return []
    command = _resolve_command(model, embedder)
    # one wire line per text: collapse any embedded whitespace (newlines included)
    # so a multi-line ticket body / entry stays a single stdin line and the
    # returned vector count matches len(texts).
    stdin = "\n".join(" ".join(t.split()) for t in texts) + "\n"
    try:
        result = subprocess.run(
            command,
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
            timeout=_EMBED_TIMEOUT_BASE_S + _EMBED_TIMEOUT_PER_TEXT_S * len(texts),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _EmbedderUnavailable(f"embedder command not runnable: {exc}") from exc
    if result.returncode != 0:
        raise _EmbedderUnavailable(
            f"embedder exited {result.returncode}: {result.stderr.strip()[:200]}"
        )
    try:
        vectors = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError) as exc:
        raise _EmbedderUnavailable(f"embedder stdout not JSON: {exc}") from exc
    if not isinstance(vectors, list) or len(vectors) != len(texts):
        raise _EmbedderUnavailable(
            f"embedder returned {len(vectors) if isinstance(vectors, list) else '?'} "
            f"vectors for {len(texts)} texts"
        )
    return [[float(x) for x in row] for row in vectors]


# ─── Index ─────────────────────────────────────────────────────────────────────


def load_index(
    workspace_root: Path, namespace: str
) -> tuple[dict[str, Any], dict[str, list[float]]]:
    """Read the sidecar. Returns (header, {id: vector}); ({}, {}) when absent."""
    path = embed_index_path(workspace_root, namespace)
    if not path.exists():
        return {}, {}
    sidecar = path.with_name(f"{path.name}.quarantine.{_ts_token()}")
    header: dict[str, Any] = {}
    vectors: dict[str, list[float]] = {}
    for rec in iter_jsonl(path, sidecar):
        if "_header" in rec and isinstance(rec["_header"], dict):
            header = rec["_header"]
            continue
        eid = rec.get("id")
        vec = rec.get("v")
        if isinstance(eid, str) and isinstance(vec, list):
            vectors[eid] = [float(x) for x in vec]
    return header, vectors


def _ts_token() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def _entry_text(entry: dict[str, Any]) -> str:
    """The text embedded for one entry. Body is the signal; type is a weak prefix."""
    body = str(entry.get("body") or "")
    etype = str(entry.get("type") or "")
    return f"{etype}: {body}".strip(": ").strip() if etype else body


def _write_index(
    path: Path,
    lock_path: Path,
    header: dict[str, Any],
    vectors: dict[str, list[float]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"_header": header}, sort_keys=True)]
    for eid in sorted(vectors):
        lines.append(json.dumps({"id": eid, "v": vectors[eid]}, sort_keys=True))
    content = "\n".join(lines) + "\n"
    with flock_retry(lock_path):
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)


def reindex(
    workspace_root: Path,
    namespace: str,
    *,
    model: str = _DEFAULT_MODEL,
    embedder: str | None = None,
    incremental: bool = True,
) -> dict[str, Any]:
    """Refresh the sidecar against the live (supersede-filtered) knowledge set.

    Incremental: embed only ids missing from the index. Full (`incremental=False`)
    or a model-id mismatch in the header: re-embed every live entry. Dead ids drop
    out (the rewrite keeps only live ids). Returns a small summary dict.

    Raises `_EmbedderUnavailable` (propagated from `embed`); the CLI maps it to
    exit 2 and the index is left untouched.
    """
    kpath = _memory_paths.knowledge_path(workspace_root, namespace)
    sidecar = kpath.with_name(f"{kpath.name}.quarantine.{_ts_token()}")
    entries = recall.filter_superseded(list(iter_jsonl(kpath, sidecar))) if kpath.exists() else []
    live = {str(e["id"]): _entry_text(e) for e in entries if isinstance(e.get("id"), str)}

    header, indexed = load_index(workspace_root, namespace)
    model_mismatch = header.get("model") != model
    full = (not incremental) or model_mismatch

    if full:
        to_embed = dict(live)
        kept: dict[str, list[float]] = {}
    else:
        kept = {eid: vec for eid, vec in indexed.items() if eid in live}
        to_embed = {eid: text for eid, text in live.items() if eid not in kept}

    embedded: dict[str, list[float]] = {}
    if to_embed:
        ids = list(to_embed)
        vectors = embed([to_embed[i] for i in ids], model=model, embedder=embedder)
        embedded = dict(zip(ids, vectors, strict=True))

    merged = {**kept, **embedded}
    dim = len(next(iter(merged.values()))) if merged else header.get("dim", 0)
    new_header = {"model": model, "dim": dim, "ts": _ts_token()}
    _write_index(
        embed_index_path(workspace_root, namespace),
        _embed_lock_path(workspace_root, namespace),
        new_header,
        merged,
    )
    return {
        "model": model,
        "dim": dim,
        "live": len(live),
        "embedded": len(embedded),
        "kept": len(kept),
        "full": full,
    }


# ─── Config ────────────────────────────────────────────────────────────────────


def _load_semantic_config(workspace_root: Path) -> dict[str, Any]:
    """Read `[memory.semantic]` from workspace.toml. Absent → empty dict."""
    import tomllib

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


# ─── CLI ─────────────────────────────────────────────────────────────────────


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Embedder seam + sidecar index.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_reindex = sub.add_parser("reindex")
    p_reindex.add_argument("--workspace-root", default=".")
    p_reindex.add_argument("--full", action="store_true")
    p_reindex.add_argument("--model", default=None)
    p_reindex.add_argument("--embedder", default=None)

    p_embed = sub.add_parser("embed")
    p_embed.add_argument("--workspace-root", default=".")
    p_embed.add_argument("--model", default=None)
    p_embed.add_argument("--embedder", default=None)

    return parser.parse_args(argv)


def cli_main(argv: list[str]) -> int:
    args = _parse_args(argv)
    workspace_root = Path(args.workspace_root).resolve()
    config = _load_semantic_config(workspace_root)
    model = args.model or str(config.get("model") or _DEFAULT_MODEL)
    embedder = args.embedder or (config.get("embedder") or None)

    if args.command == "embed":
        texts = [line.rstrip("\n") for line in sys.stdin.read().splitlines()]
        try:
            vectors = embed(texts, model=model, embedder=embedder)
        except _EmbedderUnavailable as exc:
            sys.stderr.write(f"memory_embed: {exc}\n")
            return 2
        sys.stdout.write(json.dumps(vectors) + "\n")
        return 0

    try:
        namespace = _memory_paths.resolve_namespace(workspace_root)
    except _memory_paths._MemoryConfigError as exc:
        sys.stderr.write(f"memory_embed: {exc}\n")
        return 1
    try:
        summary = reindex(
            workspace_root,
            namespace,
            model=model,
            embedder=embedder,
            incremental=not args.full,
        )
    except _EmbedderUnavailable as exc:
        sys.stderr.write(f"memory_embed: {exc}\n")
        return 2
    sys.stdout.write(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = [
    "cli_main",
    "embed",
    "embed_index_path",
    "load_index",
    "reindex",
]
