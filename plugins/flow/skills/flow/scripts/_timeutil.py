"""Shared UTC ISO8601 timestamp parsing and formatting.

recall_pending.py / lease.py / metric.py each grew a byte-near
copy of the parser; this is the one copy. The format side (iso_z /
utcnow_iso) is the matching emitter: second precision, trailing 'Z',
round-trippable through parse_iso.
"""

from __future__ import annotations

from datetime import UTC, datetime


def parse_iso(value: object) -> datetime | None:
    """Parse a UTC ISO8601 timestamp into a tz-aware datetime, or None on failure.

    Lenient: a naive value is treated as UTC. On py3.12 a trailing 'Z' is
    accepted by fromisoformat. A non-str value returns None (the metric.py
    callers can hand in non-strings).
    """
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def iso_z(dt: datetime) -> str:
    """Format a datetime as UTC ISO8601 with second precision and trailing 'Z'."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def utcnow_iso() -> str:
    """Current UTC time as ISO8601 with second precision and trailing 'Z'."""
    return iso_z(datetime.now(UTC))


def utcnow_iso_ms() -> str:
    """UTC ISO8601 with millisecond precision + Z suffix."""
    now = datetime.now(UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%S") + f".{now.microsecond // 1000:03d}Z"


def ts_token() -> str:
    """Current UTC time as a colon-free filename token (quarantine/backup names)."""
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
