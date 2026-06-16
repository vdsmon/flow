"""Tests for group_persist.py — durable cover-set persist/derive."""

from __future__ import annotations

import group_persist as gp


class _FakeTracker:
    """Holds one ticket's comments; comment() appends, get() returns them."""

    def __init__(self, comments=None):
        self._comments = list(comments or [])

    def comment(self, key, body):
        self._comments.append({"body": body, "created_at": f"t{len(self._comments)}"})

    def get(self, key):
        return {"key": key, "comments": list(self._comments)}


def _marker_comment(covers_line: str, created_at: str):
    return {
        "body": {"body": f"flow-group covers: {covers_line}", "fmt": "plain"},
        "created_at": created_at,
    }


def test_format_and_parse_roundtrip() -> None:
    line = gp.format_marker(["FT-1207", "FT-1208"])
    assert line == "flow-group covers: FT-1207, FT-1208"
    assert gp.parse_marker(line) == ["FT-1207", "FT-1208"]


def test_parse_marker_ignores_non_marker() -> None:
    assert gp.parse_marker("just a normal comment") is None


def test_latest_covers_picks_most_recent_by_created_at() -> None:
    comments = [
        _marker_comment("FT-1, FT-2", "2026-06-01T00:00:00Z"),
        {"body": {"body": "unrelated", "fmt": "plain"}, "created_at": "2026-06-02T00:00:00Z"},
        _marker_comment("FT-3, FT-4", "2026-06-03T00:00:00Z"),
    ]
    assert gp.latest_covers(comments) == ["FT-3", "FT-4"]


def test_latest_covers_none_when_absent() -> None:
    assert gp.latest_covers([{"body": {"body": "hi"}, "created_at": "x"}]) is None


def test_persist_writes_marker_comment() -> None:
    tracker = _FakeTracker()
    out = gp.persist(tracker, "FT-1184", ["FT-1207", "FT-1208"])
    assert out["persisted"] is True
    # round-trips: derive reads back what persist wrote
    assert gp.derive(tracker, "FT-1184")["covers"] == ["FT-1207", "FT-1208"]


def test_persist_idempotent_when_unchanged() -> None:
    tracker = _FakeTracker([_marker_comment("FT-1207, FT-1208", "2026-06-01T00:00:00Z")])
    out = gp.persist(tracker, "FT-1184", ["FT-1207", "FT-1208"])
    assert out["persisted"] is False and out["reason"] == "unchanged"
    # no second comment appended
    assert len(tracker.get("FT-1184")["comments"]) == 1


def test_persist_rewrites_when_set_changes() -> None:
    tracker = _FakeTracker([_marker_comment("FT-1207", "2026-06-01T00:00:00Z")])
    out = gp.persist(tracker, "FT-1184", ["FT-1207", "FT-1208"])
    assert out["persisted"] is True
    assert gp.derive(tracker, "FT-1184")["covers"] == ["FT-1207", "FT-1208"]


def test_derive_empty_when_no_marker() -> None:
    assert gp.derive(_FakeTracker(), "FT-1184") == {"lead": "FT-1184", "covers": []}


def test_cli_persist_requires_nonempty_covers(capsys, monkeypatch) -> None:
    monkeypatch.setattr(gp, "_tracker_for", lambda root: _FakeTracker())
    rc = gp.cli_main(["persist", "--lead", "FT-1", "--covers", " , ", "--workspace-root", "."])
    assert rc == 3
    assert "resolved to nothing" in capsys.readouterr().err


def test_cli_derive_roundtrips(capsys, monkeypatch) -> None:
    import json

    tracker = _FakeTracker()
    monkeypatch.setattr(gp, "_tracker_for", lambda root: tracker)
    assert gp.cli_main(["persist", "--lead", "FT-1184", "--covers", "FT-1207,FT-1208"]) == 0
    capsys.readouterr()
    assert gp.cli_main(["derive", "--lead", "FT-1184"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"lead": "FT-1184", "covers": ["FT-1207", "FT-1208"]}
