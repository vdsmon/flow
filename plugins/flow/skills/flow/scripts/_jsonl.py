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


def iter_jsonl(path: Path, quarantine_sidecar: Path) -> Iterator[dict[str, Any]]:
    """Yield each valid JSON object from `path`.

    Blank lines are skipped. A line that fails json.loads or decodes to a
    non-object is appended to `quarantine_sidecar` and skipped. The main file is
    never modified. Yields nothing if the file does not exist.
    """
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            stripped = line.rstrip("\n")
            if not stripped.strip():
                continue
            try:
                entry = json.loads(stripped)
            except json.JSONDecodeError as exc:
                append_quarantine(quarantine_sidecar, stripped, f"json: {exc}")
                continue
            if not isinstance(entry, dict):
                append_quarantine(quarantine_sidecar, stripped, "not an object")
                continue
            yield entry


__all__ = ["append_quarantine", "iter_jsonl"]
