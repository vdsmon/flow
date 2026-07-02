"""Tests for group_candidates.py: the deterministic half of /flow group."""

from __future__ import annotations

import group_candidates as gc


class _FakeTracker:
    """Returns canned Ticket dicts by key; list_assigned yields refs."""

    def __init__(self, tickets: dict[str, dict]):
        self._tickets = tickets

    def get(self, key):
        return self._tickets[key]

    def list_assigned(self, filter="open"):
        return [{"key": k, "summary": t["summary"]} for k, t in self._tickets.items()]


def _t(key, summary, *, description="body", type="Task", parent=None, links=None):
    return {
        "key": key,
        "summary": summary,
        "status": "To Do",
        "type": type,
        "description": description,
        "parent": parent,
        "links": links or [],
    }


def test_summary_tokens_punctuation_insensitive() -> None:
    # the FT-1207 vs FT-1190 title-twin: different punctuation, same tokens
    a = gc._summary_tokens("[AR 2083 - Rappi] - Sheet 3 - Arca")
    b = gc._summary_tokens("[AR 2083 - Rappi / Sheet 3 - ARCA]")
    assert a == b


def test_dup_hint_flags_empty_body_twin_directionally() -> None:
    records = [
        gc._normalize(_t("FT-1207", "[AR 2083 - Rappi] - Sheet 3 - Arca", description="real")),
        gc._normalize(_t("FT-1190", "[AR 2083 - Rappi / Sheet 3 - ARCA]", description="")),
    ]
    hints = gc._dup_hints(records)
    assert hints == [{"key": "FT-1190", "duplicate_of": "FT-1207", "title_overlap": 1.0}]


def test_no_dup_hint_when_body_present() -> None:
    records = [
        gc._normalize(_t("FT-1", "Sheet 3 Arca", description="x")),
        gc._normalize(_t("FT-2", "Sheet 3 Arca", description="y")),
    ]
    assert gc._dup_hints(records) == []


def test_no_dup_hint_when_titles_diverge() -> None:
    records = [
        gc._normalize(_t("FT-1", "Sheet 2 ventas", description="")),
        gc._normalize(_t("FT-2", "compras date filter", description="z")),
    ]
    assert gc._dup_hints(records) == []


def test_collect_explicit_keys() -> None:
    tracker = _FakeTracker(
        {
            "FT-1184": _t("FT-1184", "Sheet 2.1", parent="FT-894", links=[]),
            "FT-1207": _t(
                "FT-1207",
                "Sheet 3 Arca",
                links=[{"kind": "relates", "from_key": "FT-1207", "to_key": "FT-1028"}],
            ),
        }
    )
    bundle = gc.collect(tracker, ["FT-1184", "FT-1207"], None)
    keys = [c["key"] for c in bundle["candidates"]]
    assert keys == ["FT-1184", "FT-1207"]
    rec = bundle["candidates"][0]
    assert rec["parent"] == "FT-894"
    assert bundle["candidates"][1]["links"] == [{"kind": "relates", "to_key": "FT-1028"}]


def test_collect_selector_enriches_each_ref() -> None:
    tracker = _FakeTracker(
        {
            "FT-1": _t("FT-1", "alpha", description=""),
            "FT-2": _t("FT-2", "alpha", description="real"),
        }
    )
    bundle = gc.collect(tracker, [], "open")
    assert {c["key"] for c in bundle["candidates"]} == {"FT-1", "FT-2"}
    # body_empty surfaced from the enriched get(), not the ref
    assert any(c["body_empty"] for c in bundle["candidates"])


def test_cli_no_input_exits_3(capsys) -> None:
    rc = gc.cli_main([])
    assert rc == 3
    assert "ticket keys or --mine" in capsys.readouterr().err
