import re
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from _timeutil import iso_z, parse_iso, utcnow_iso


def test_naive_treated_as_utc():
    dt = parse_iso("2024-01-01T00:00:00")
    assert dt is not None
    offset = dt.utcoffset()
    assert offset is not None
    assert offset.total_seconds() == 0


def test_z_suffix_is_utc():
    dt = parse_iso("2024-01-01T00:00:00Z")
    assert dt is not None
    offset = dt.utcoffset()
    assert offset is not None
    assert offset.total_seconds() == 0


def test_explicit_offset_preserved():
    dt = parse_iso("2024-01-01T00:00:00+05:00")
    assert dt is not None
    offset = dt.utcoffset()
    assert offset is not None
    assert offset.total_seconds() == 5 * 3600


def test_malformed_returns_none():
    assert parse_iso("not-a-date") is None


def test_non_str_returns_none():
    none_value: Any = None
    int_value: Any = 12345
    assert parse_iso(none_value) is None
    assert parse_iso(int_value) is None


def test_require_z_accepts_z():
    assert parse_iso("2024-01-01T00:00:00Z", require_z=True) is not None


def test_require_z_rejects_non_z():
    assert parse_iso("2024-01-01T00:00:00", require_z=True) is None


def test_naive_utc_equals_z_utc():
    z = parse_iso("2024-01-01T00:00:00Z")
    naive = parse_iso("2024-01-01T00:00:00")
    assert z is not None
    assert naive is not None
    assert z == naive


def test_iso_z_known_value():
    dt = datetime(2024, 1, 2, 3, 4, 5, 678901, tzinfo=UTC)
    assert iso_z(dt) == "2024-01-02T03:04:05Z"


def test_iso_z_converts_offset_to_utc():
    dt = datetime(2024, 1, 2, 8, 4, 5, tzinfo=timezone(timedelta(hours=5)))
    assert iso_z(dt) == "2024-01-02T03:04:05Z"


def test_utcnow_iso_format_and_roundtrip():
    value = utcnow_iso()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value)
    assert parse_iso(value, require_z=True) is not None
