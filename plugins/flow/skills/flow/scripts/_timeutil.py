"""Shared UTC ISO8601 timestamp parsing.

heartbeat.py / recall_pending.py / lease.py / metric.py each grew a byte-near
copy of this; validate_postmortem.py grew the strict (Z-required) variant. This
is the one copy. require_z makes the divergent contract explicit and testable.
"""

from __future__ import annotations

from datetime import UTC, datetime


def parse_iso(value: object, *, require_z: bool = False) -> datetime | None:
    """Parse a UTC ISO8601 timestamp into a tz-aware datetime, or None on failure.

    Lenient by default: a naive value is treated as UTC. On py3.12 a trailing
    'Z' is accepted by fromisoformat. With require_z=True, any value not ending
    in 'Z' is rejected (the validate_postmortem validation contract). A non-str
    value returns None (the metric.py callers can hand in non-strings).
    """
    if not isinstance(value, str):
        return None
    if require_z and not value.endswith("Z"):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt
