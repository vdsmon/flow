"""Shared JSONL reader with malformed-line quarantine.

recall.py and memory_append.py both walk `.flow/<namespace>/knowledge.jsonl`,
skipping blank lines, json-decoding each line, quarantining anything that fails
to parse or is not a JSON object. The main file is never rewritten; bad lines are
appended to a sidecar. This is the one copy of that contract.
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any


def append_quarantine(sidecar: Path, raw_line: str, reason: str) -> None:
    """Append one `{reason, raw}` record to the quarantine sidecar (fsynced)."""
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    record = {"reason": reason, "raw": raw_line}
    with sidecar.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
        fh.flush()
        with contextlib.suppress(OSError):
            os.fsync(fh.fileno())


def _quarantined_raws(sidecar: Path) -> set[str]:
    """Raw lines already recorded in the sidecar, so re-quarantine is idempotent."""
    if not sidecar.exists():
        return set()
    raws: set[str] = set()
    with sidecar.open("r", encoding="utf-8") as fh:
        for line in fh:
            with contextlib.suppress(json.JSONDecodeError):
                rec = json.loads(line)
                if isinstance(rec, dict) and isinstance(rec.get("raw"), str):
                    raws.add(rec["raw"])
    return raws


def iter_jsonl(path: Path, quarantine_sidecar: Path) -> Iterator[dict[str, Any]]:
    """Yield each valid JSON object from `path`.

    Blank lines are skipped. A line that fails json.loads or decodes to a
    non-object is appended to `quarantine_sidecar` and skipped. The main file is
    never modified. Yields nothing if the file does not exist.

    Quarantine is idempotent: a malformed line already in the sidecar is not
    re-appended. Because the main file is never rewritten, the same bad line is
    re-read on every pass (every append-dedup scan, compact, recall); without
    this the sidecar would grow by one record per call, forever. The sidecar is
    read lazily, only once a malformed line is actually hit, so a clean file
    pays no extra I/O.
    """
    if not path.exists():
        return
    seen_bad: set[str] | None = None
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            stripped = line.rstrip("\n")
            if not stripped.strip():
                continue
            try:
                entry = json.loads(stripped)
            except json.JSONDecodeError as exc:
                reason = f"json: {exc}"
            else:
                if isinstance(entry, dict):
                    yield entry
                    continue
                reason = "not an object"
            if seen_bad is None:
                seen_bad = _quarantined_raws(quarantine_sidecar)
            if stripped not in seen_bad:
                append_quarantine(quarantine_sidecar, stripped, reason)
                seen_bad.add(stripped)


__all__ = ["append_quarantine", "iter_jsonl"]
