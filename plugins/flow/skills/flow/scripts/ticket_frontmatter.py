"""Per-ticket TOML frontmatter reader/writer.

Library + thin CLI. Stdlib-only — `tomllib` for parse, hand-rolled emit.

**Frontmatter format**: `+++`-delimited TOML at top of file, followed by
freeform markdown body. We OWN this format (no preexisting ticket files).

Example:
    +++
    ticket = "FT-1234"
    status = "in_progress"
    started_at = "2026-05-28T14:32:00Z"
    finished_at = ""
    agent_id = ""
    labels = ["auth", "oncall"]
    +++

    Freeform markdown here.

Invariants:
  - All writes go through atomic temp-fsync-rename + flock(EX) on a sibling
    `<path>.lock` file (mirrors `state.py:_Flock`).
  - Frontmatter block is replaced wholesale on write; markdown body below the
    second `+++` is preserved byte-for-byte.
  - Read-side malformed → quarantine to `<path>.quarantine.<ts>`, return empty
    dict + warning to stderr.
  - Write-side malformed → exit 2 (no auto-recovery; advisor: quarantine is a
    read-side recovery, not a write-side override).

`--set k=v` parsing rules:
  1. `null` → empty string `""` (TOML has no null).
  2. `true` / `false` → TOML bool.
  3. `^-?\\d+$` → TOML integer.
  4. `^\\[.*\\]$` → TOML array, parsed via `tomllib` so quoted items with
     commas survive; falls back to naive comma-split for bare-word lists.
  5. `NOW` → UTC ISO8601 string via `_utcnow_iso()`.
  6. Otherwise → TOML string (always double-quoted on write).

Exit codes (per plan line 1017-1020):
  0 = ok (read always returns 0, emitting {} when the file is absent or its
      frontmatter is malformed)
  1 = lock contention (couldn't acquire after 3 x 1s retry)
  2 = update-side schema invalid (file has content but no/!parseable
      frontmatter block; aborts without touching the file), or a malformed
      --set pair
  3 = I/O error
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import fcntl
import json
import os
import re
import sys
import tempfile
import time
import tomllib
from pathlib import Path
from typing import Any

DELIM = "+++"
LOCK_RETRY_COUNT = 3
LOCK_RETRY_DELAY_S = 1.0
_INT_RE = re.compile(r"^-?\d+$")
_LIST_RE = re.compile(r"^\[.*\]$")
_BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _utcnow_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _ts_token() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def _lock_path(path: Path) -> Path:
    return path.with_name(path.name + ".lock")


def _toml_escape(value: str) -> str:
    out: list[str] = []
    for ch in value:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as tmp:
        tmp.write(content)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)


class _Flock:
    """POSIX fcntl.flock context manager with bounded retry on contention.

    Non-blocking LOCK_EX | LOCK_NB; retries up to LOCK_RETRY_COUNT with
    LOCK_RETRY_DELAY_S backoff between attempts. On exhaustion raises
    `_LockContention`.
    """

    def __init__(self, lock_path: Path) -> None:
        self._lock_path = lock_path
        self._fd: int | None = None

    def __enter__(self) -> _Flock:
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(str(self._lock_path), os.O_RDWR | os.O_CREAT, 0o644)
        for attempt in range(LOCK_RETRY_COUNT):
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except BlockingIOError:
                if attempt == LOCK_RETRY_COUNT - 1:
                    os.close(self._fd)
                    self._fd = None
                    raise _LockContention(
                        f"could not lock {self._lock_path} after {LOCK_RETRY_COUNT} attempts"
                    ) from None
                time.sleep(LOCK_RETRY_DELAY_S)
        raise _LockContention(f"lock loop exited without lock on {self._lock_path}")

    def __exit__(self, *exc: object) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None


class _LockContention(Exception):
    """Raised when lock cannot be acquired within retry budget. Exit code 1."""


class _SchemaInvalid(Exception):
    """Raised when frontmatter is malformed at write time. Exit code 2."""


# ─── Parse / split ───────────────────────────────────────────────────────────


def _split_frontmatter(text: str) -> tuple[str | None, str]:
    """Returns (frontmatter_block, body).

    - frontmatter_block is the raw TOML between the two `+++` lines (excluding
      the delimiters themselves), or None if the file has no frontmatter block.
    - body is everything after the closing `+++` line (or the whole text when
      no frontmatter block exists).
    """
    if not text.startswith(DELIM):
        return None, text
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != DELIM:
        return None, text
    end_idx: int | None = None
    for i in range(1, len(lines)):
        if lines[i].strip() == DELIM:
            end_idx = i
            break
    if end_idx is None:
        return None, text
    fm = "".join(lines[1:end_idx])
    body = "".join(lines[end_idx + 1 :])
    if body.startswith("\n"):
        body = body[1:]
    return fm, body


def _parse_frontmatter(fm: str) -> dict[str, Any]:
    """Parse TOML block. Raises tomllib.TOMLDecodeError on failure."""
    data = tomllib.loads(fm)
    if not isinstance(data, dict):
        raise tomllib.TOMLDecodeError("frontmatter root is not a table")
    return data


# ─── Emit ────────────────────────────────────────────────────────────────────


def _emit_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    # datetime is a subclass of date; one branch covers both. ISO 8601 with a
    # `T` separator is a TOML datetime literal, emitted unquoted to keep the type.
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, list):
        return "[" + ", ".join(_emit_value(v) for v in value) + "]"
    return _toml_escape(str(value))


def _emit_key(key: str) -> str:
    if _BARE_KEY_RE.match(key):
        return key
    return _toml_escape(key)


def _emit_frontmatter(data: dict[str, Any]) -> str:
    lines: list[str] = []
    for key, value in data.items():
        lines.append(f"{_emit_key(key)} = {_emit_value(value)}")
    return "\n".join(lines) + ("\n" if lines else "")


def _render_full(data: dict[str, Any], body: str) -> str:
    fm = _emit_frontmatter(data)
    return f"{DELIM}\n{fm}{DELIM}\n\n{body}" if body else f"{DELIM}\n{fm}{DELIM}\n"


# ─── Quarantine ──────────────────────────────────────────────────────────────


def _quarantine(path: Path) -> Path:
    dst = path.with_name(f"{path.name}.quarantine.{_ts_token()}")
    with contextlib.suppress(OSError):
        os.replace(path, dst)
    return dst


# ─── Public API ──────────────────────────────────────────────────────────────


def read(path: Path) -> dict[str, Any]:
    """Read frontmatter as a dict.

    Returns empty dict + writes warning to stderr if file missing or
    frontmatter malformed (quarantines the corrupt file in the latter case).
    """
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        sys.stderr.write(f"ticket-frontmatter: read failed for {path}: {exc}\n")
        return {}
    fm, _body = _split_frontmatter(text)
    if fm is None:
        return {}
    try:
        return _parse_frontmatter(fm)
    except tomllib.TOMLDecodeError as exc:
        dst = _quarantine(path)
        sys.stderr.write(
            f"ticket-frontmatter: malformed frontmatter at {path} ({exc}); quarantined to {dst}\n"
        )
        return {}


def update(path: Path, updates: dict[str, str]) -> None:
    """Atomic in-place update of frontmatter values.

    Read-modify-write is fully serialized under the file's `.lock` flock — two
    concurrent updaters cannot interleave their RMW windows. Preserves body
    bytes + key ordering; new keys appended in insertion order. On unparseable
    existing frontmatter raises `_SchemaInvalid` (CLI surfaces as exit 2).
    """
    with _Flock(_lock_path(path)):
        text = path.read_text(encoding="utf-8") if path.exists() else f"{DELIM}\n{DELIM}\n"
        fm, body = _split_frontmatter(text)
        existing: dict[str, Any]
        if fm is None:
            if text.strip():
                raise _SchemaInvalid(f"file {path} exists but has no frontmatter block")
            existing = {}
        else:
            try:
                existing = _parse_frontmatter(fm)
            except tomllib.TOMLDecodeError as exc:
                raise _SchemaInvalid(f"frontmatter at {path} does not parse: {exc}") from exc
        for key, raw_value in updates.items():
            existing[key] = _coerce_value(raw_value)
        _atomic_write_text(path, _render_full(existing, body))


def _coerce_value(raw: str) -> Any:
    if raw == "null":
        return ""
    if raw == "true":
        return True
    if raw == "false":
        return False
    if raw == "NOW":
        return _utcnow_iso()
    if _INT_RE.match(raw):
        return int(raw)
    if _LIST_RE.match(raw):
        try:
            parsed = tomllib.loads("_v = " + raw)["_v"]
        except tomllib.TOMLDecodeError:
            # legacy bare-word lists like `[a, b, c]` aren't valid TOML; fall
            # back to naive comma split so they keep working.
            inner = raw[1:-1].strip()
            if not inner:
                return []
            return [item.strip() for item in inner.split(",")]
        return parsed
    return raw


# ─── CLI ─────────────────────────────────────────────────────────────────────


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read/write ticket TOML frontmatter.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_read = sub.add_parser("read", help="Read frontmatter to JSON.")
    p_read.add_argument("path")

    p_update = sub.add_parser("update", help="Update frontmatter keys atomically.")
    p_update.add_argument("path")
    p_update.add_argument(
        "--set",
        action="append",
        dest="set_pairs",
        required=True,
        help="key=value (repeat for multiple keys).",
    )

    return parser.parse_args(argv)


def _parse_set_pair(pair: str) -> tuple[str, str]:
    if "=" not in pair:
        raise ValueError(f"--set value {pair!r} missing '='")
    key, _, value = pair.partition("=")
    key = key.strip()
    if not key:
        raise ValueError(f"--set value {pair!r} has empty key")
    return key, value


def cli_main(argv: list[str]) -> int:
    args = _parse_args(argv)
    path = Path(args.path).resolve()

    if args.cmd == "read":
        data = read(path)
        sys.stdout.write(json.dumps(data, indent=2, sort_keys=True) + "\n")
        return 0

    if args.cmd == "update":
        try:
            updates = dict(_parse_set_pair(pair) for pair in args.set_pairs)
        except ValueError as exc:
            sys.stderr.write(f"ticket-frontmatter: {exc}\n")
            return 2
        try:
            update(path, updates)
        except _SchemaInvalid as exc:
            sys.stderr.write(f"ticket-frontmatter: {exc}\n")
            return 2
        except _LockContention as exc:
            sys.stderr.write(f"ticket-frontmatter: {exc}\n")
            return 1
        except OSError as exc:
            sys.stderr.write(f"ticket-frontmatter: I/O error: {exc}\n")
            return 3
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = ["cli_main", "read", "update"]
