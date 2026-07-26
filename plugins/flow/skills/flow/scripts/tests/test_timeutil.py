import re
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from _timeutil import iso_z, parse_iso, utcnow_iso


@pytest.mark.parametrize(
    ("value", "expected_offset_seconds"),
    [
        pytest.param("2024-01-01T00:00:00", 0, id="naive_treated_as_utc"),
        pytest.param("2024-01-01T00:00:00Z", 0, id="z_suffix_is_utc"),
        pytest.param("2024-01-01T00:00:00+05:00", 5 * 3600, id="explicit_offset_preserved"),
    ],
)
def test_parse_iso_offset(value: str, expected_offset_seconds: int):
    dt = parse_iso(value)
    assert dt is not None
    offset = dt.utcoffset()
    assert offset is not None
    assert offset.total_seconds() == expected_offset_seconds


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("not-a-date", id="malformed"),
        pytest.param(None, id="none"),
        pytest.param(12345, id="int"),
    ],
)
def test_parse_iso_returns_none(value: Any):
    assert parse_iso(value) is None


def test_naive_utc_equals_z_utc():
    z = parse_iso("2024-01-01T00:00:00Z")
    naive = parse_iso("2024-01-01T00:00:00")
    assert z is not None
    assert naive is not None
    assert z == naive


@pytest.mark.parametrize(
    ("dt", "expected"),
    [
        pytest.param(
            datetime(2024, 1, 2, 3, 4, 5, 678901, tzinfo=UTC),
            "2024-01-02T03:04:05Z",
            id="known_value_truncates_microseconds",
        ),
        pytest.param(
            datetime(2024, 1, 2, 8, 4, 5, tzinfo=timezone(timedelta(hours=5))),
            "2024-01-02T03:04:05Z",
            id="converts_offset_to_utc",
        ),
    ],
)
def test_iso_z(dt: datetime, expected: str):
    assert iso_z(dt) == expected


def test_utcnow_iso_format_and_roundtrip():
    value = utcnow_iso()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value)
    assert parse_iso(value) is not None
