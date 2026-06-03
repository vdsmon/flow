"""Tests for recall.py — hand-rolled BM25 ranker."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import _memory_paths
import recall


def _seed_workspace(root: Path, namespace: str = "demo") -> None:
    flow = root / ".flow"
    flow.mkdir(parents=True, exist_ok=True)
    (flow / "workspace.toml").write_text(
        f'[tracker]\nbackend = "jira"\n[tracker.jira]\ncloud_id = "x"\nproject_key = "FT"\n\n[memory]\nnamespace = "{namespace}"\n',
        encoding="utf-8",
    )


def _write_entries(root: Path, namespace: str, entries: list[dict]) -> Path:
    kpath = _memory_paths.knowledge_path(root, namespace)
    kpath.parent.mkdir(parents=True, exist_ok=True)
    with kpath.open("w", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e, sort_keys=True) + "\n")
    return kpath


def _make_entry(
    id_: str,
    body: str,
    ts: str = "2026-01-01T00:00:00.000Z",
    type_: str = "LEARNED",
    branch: str = "main",
    ticket: str = "FT-1",
) -> dict:
    return {
        "id": id_,
        "ts": ts,
        "type": type_,
        "namespace": "demo",
        "branch": branch,
        "ticket": ticket,
        "body": body,
    }


# ─── Tokenizer ───────────────────────────────────────────────────────────────


def test_tokenize_basic() -> None:
    assert recall.tokenize("Hello World") == ["hello", "world"]


def test_tokenize_collapses_whitespace_and_punct() -> None:
    assert recall.tokenize("foo, bar! baz.") == ["foo", "bar", "baz"]


def test_tokenize_nfkc_normalizes_unicode_compat() -> None:
    # Full-width ASCII codepoints: U+FF46 U+FF4F U+FF4F.
    # NFKC normalizes these to plain f / o / o.
    full_width = chr(0xFF46) + chr(0xFF4F) + chr(0xFF4F)
    assert recall.tokenize(full_width) == ["foo"]


def test_tokenize_empty_returns_empty() -> None:
    assert recall.tokenize("") == []


def test_tokenize_preserves_word_underscores() -> None:
    assert recall.tokenize("foo_bar") == ["foo_bar"]


# ─── rank() — empty corpora ──────────────────────────────────────────────────


def test_rank_empty_corpus_returns_empty() -> None:
    assert recall.rank("anything", []) == []


def test_rank_empty_query_returns_no_results() -> None:
    # An empty query tokenizes to [], so nothing actually matched -> [] (not the
    # newest entries masquerading as matches).
    entries = [_make_entry("a" * 16, "first"), _make_entry("b" * 16, "second")]
    assert recall.rank("", entries, top_n=10) == []


def test_rank_whitespace_query_returns_no_results() -> None:
    entries = [_make_entry("a" * 16, "first"), _make_entry("b" * 16, "second")]
    assert recall.rank("   \t\n", entries, top_n=10) == []


# ─── rank() — basic BM25 ─────────────────────────────────────────────────────


def test_rank_query_match_in_body_outranks_no_match() -> None:
    entries = [
        _make_entry("a" * 16, "atomic write needs fsync"),
        _make_entry("b" * 16, "lorem ipsum dolor sit amet"),
    ]
    results = recall.rank("atomic write", entries, top_n=2)
    assert results[0]["body"].startswith("atomic")
    assert results[0]["score"] > results[1]["score"]


def test_rank_term_frequency_increases_score() -> None:
    entries = [
        _make_entry("a" * 16, "fsync"),
        _make_entry("b" * 16, "fsync fsync fsync"),
    ]
    results = recall.rank("fsync", entries, top_n=2)
    # Higher TF wins (assuming similar field lengths).
    bodies_ordered = [r["body"] for r in results]
    assert bodies_ordered[0] == "fsync fsync fsync"


def test_rank_top_n_limits_results() -> None:
    entries = [_make_entry(f"{i:016x}", f"foo {i}") for i in range(10)]
    results = recall.rank("foo", entries, top_n=3)
    assert len(results) == 3


def test_rank_returns_entry_shape_with_score() -> None:
    entries = [_make_entry("a" * 16, "foo", type_="DECISION")]
    results = recall.rank("foo", entries, top_n=1)
    assert results[0]["id"] == "a" * 16
    assert results[0]["type"] == "DECISION"
    assert "score" in results[0]


# ─── rank() — exact-match boost ──────────────────────────────────────────────


def test_branch_exact_match_boosts_score() -> None:
    entries = [
        _make_entry("a" * 16, "fsync", branch="feature/x"),
        _make_entry("b" * 16, "fsync", branch="feature/y"),
    ]
    no_filter = recall.rank("fsync", entries, top_n=2)
    with_filter = recall.rank("fsync", entries, branch_filter="feature/x", top_n=2)
    # Without filter: ts-tied tiebreak, scores equal.
    # With filter: feature/x entry gets the additive branch bonus.
    a_score_unfiltered = next(r["score"] for r in no_filter if r["id"] == "a" * 16)
    a_score_filtered = next(r["score"] for r in with_filter if r["id"] == "a" * 16)
    assert a_score_filtered == pytest.approx(
        a_score_unfiltered + recall.BRANCH_EXACT_BONUS, rel=1e-9
    )


def test_branch_filter_case_insensitive() -> None:
    entries = [_make_entry("a" * 16, "fsync", branch="Feature/X")]
    results = recall.rank("fsync", entries, branch_filter="feature/x", top_n=1)
    # Bonus still applies despite case difference.
    no_boost = recall.rank("fsync", entries, top_n=1)
    assert results[0]["score"] == pytest.approx(
        no_boost[0]["score"] + recall.BRANCH_EXACT_BONUS, rel=1e-9
    )


def test_ticket_exact_match_boosts_score() -> None:
    entries = [_make_entry("a" * 16, "fsync", ticket="FT-1")]
    base = recall.rank("fsync", entries, top_n=1)
    boosted = recall.rank("fsync", entries, ticket_filters=["FT-1"], top_n=1)
    assert boosted[0]["score"] == pytest.approx(
        base[0]["score"] + recall.TICKET_EXACT_BONUS, rel=1e-9
    )


def test_ticket_bonus_stronger_than_branch_bonus() -> None:
    assert recall.TICKET_EXACT_BONUS > recall.BRANCH_EXACT_BONUS


def test_ticket_filter_multiple_tickets_any_matches() -> None:
    entries = [
        _make_entry("a" * 16, "fsync", ticket="FT-1"),
        _make_entry("b" * 16, "fsync", ticket="FT-2"),
    ]
    base = recall.rank("fsync", entries, top_n=2)
    boosted = recall.rank("fsync", entries, ticket_filters=["FT-2", "FT-99"], top_n=2)
    a_base = next(r["score"] for r in base if r["id"] == "a" * 16)
    a_boosted = next(r["score"] for r in boosted if r["id"] == "a" * 16)
    b_boosted = next(r["score"] for r in boosted if r["id"] == "b" * 16)
    assert a_base == a_boosted
    assert b_boosted > a_boosted


def test_branch_and_ticket_boosts_stack() -> None:
    entries = [_make_entry("a" * 16, "fsync", branch="main", ticket="FT-1")]
    base = recall.rank("fsync", entries, top_n=1)
    boosted = recall.rank("fsync", entries, branch_filter="main", ticket_filters=["FT-1"], top_n=1)
    assert boosted[0]["score"] == pytest.approx(
        base[0]["score"] + recall.BRANCH_EXACT_BONUS + recall.TICKET_EXACT_BONUS, rel=1e-9
    )


def test_ticket_exact_match_ranks_first_even_with_zero_text_score() -> None:
    # FT-9 body shares no tokens with the query, so its BM25 text score is 0.
    # The additive ticket bonus must still float it above a non-requested record
    # whose body matches a query term.
    entries = [
        _make_entry("a" * 16, "completely unrelated prose", ticket="FT-9"),
        _make_entry("b" * 16, "fsync durability notes", ticket="FT-2"),
    ]
    results = recall.rank("fsync", entries, ticket_filters=["FT-9"], top_n=2)
    assert results[0]["id"] == "a" * 16
    assert results[0]["score"] > results[1]["score"]


# ─── rank() — field weights ──────────────────────────────────────────────────


def test_field_weights_branch_outranks_body() -> None:
    """When the query token appears in branch (weight 1.5) on doc A but body
    (weight 1.0) on doc B, A should outrank B with everything else equal.

    Test corpora deliberately varies the field of interest across docs so IDF
    isn't collapsed."""
    entries = [
        _make_entry("a" * 16, "unrelated content", branch="cooldown-fix"),
        _make_entry("b" * 16, "cooldown is the body text", branch="other"),
    ]
    results = recall.rank("cooldown", entries, top_n=2)
    assert results[0]["id"] == "a" * 16


# ─── rank() — tiebreak ts DESC ───────────────────────────────────────────────


def test_tiebreak_ts_desc() -> None:
    entries = [
        _make_entry("a" * 16, "fsync", ts="2026-01-01T00:00:00.000Z"),
        _make_entry("b" * 16, "fsync", ts="2026-06-01T00:00:00.000Z"),
        _make_entry("c" * 16, "fsync", ts="2026-03-01T00:00:00.000Z"),
    ]
    results = recall.rank("fsync", entries, top_n=3)
    # All have same score; tiebreak orders by ts DESC.
    assert [r["id"] for r in results] == ["b" * 16, "c" * 16, "a" * 16]


def test_tiebreak_missing_ts_sorts_last() -> None:
    # Two records tied on score: one with a ts, one without. The one WITH ts
    # ranks first (a missing ts is treated as oldest, not newest).
    with_ts = _make_entry("a" * 16, "fsync", ts="2026-01-01T00:00:00.000Z")
    no_ts = _make_entry("b" * 16, "fsync")
    del no_ts["ts"]
    results = recall.rank("fsync", [no_ts, with_ts], top_n=2)
    assert [r["id"] for r in results] == ["a" * 16, "b" * 16]


def test_tiebreak_empty_ts_sorts_last() -> None:
    with_ts = _make_entry("a" * 16, "fsync", ts="2026-01-01T00:00:00.000Z")
    empty_ts = _make_entry("b" * 16, "fsync", ts="")
    results = recall.rank("fsync", [empty_ts, with_ts], top_n=2)
    assert [r["id"] for r in results] == ["a" * 16, "b" * 16]


# ─── Quarantine ──────────────────────────────────────────────────────────────


def test_load_quarantines_malformed(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    kpath = _memory_paths.knowledge_path(tmp_path, "demo")
    kpath.parent.mkdir(parents=True, exist_ok=True)
    kpath.write_text(
        "not json\n"
        + json.dumps(_make_entry("a" * 16, "fsync"), sort_keys=True)
        + "\n"
        + "[]\n",  # not an object
        encoding="utf-8",
    )
    entries = recall._load_entries(kpath)
    assert len(entries) == 1
    quarantines = list(kpath.parent.glob("knowledge.jsonl.quarantine.*"))
    assert len(quarantines) == 1
    q_lines = quarantines[0].read_text(encoding="utf-8").splitlines()
    assert len(q_lines) == 2


def test_load_missing_file_returns_empty(tmp_path: Path) -> None:
    assert recall._load_entries(tmp_path / "missing.jsonl") == []


# ─── CLI ─────────────────────────────────────────────────────────────────────


def test_cli_empty_corpus_emits_empty_array(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_workspace(tmp_path)
    rc = recall.cli_main(["query", "--workspace-root", str(tmp_path)])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == []


def test_cli_no_workspace_returns_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = recall.cli_main(["query", "--workspace-root", str(tmp_path)])
    assert rc == 1
    assert "workspace.toml" in capsys.readouterr().err


def test_cli_returns_top_n(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed_workspace(tmp_path)
    _write_entries(
        tmp_path,
        "demo",
        [_make_entry(f"{i:016x}", "fsync matters") for i in range(5)],
    )
    rc = recall.cli_main(["fsync", "--top-n", "3", "--workspace-root", str(tmp_path)])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload) == 3


def test_cli_branch_filter_applied(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed_workspace(tmp_path)
    _write_entries(
        tmp_path,
        "demo",
        [
            _make_entry("a" * 16, "fsync", branch="main"),
            _make_entry("b" * 16, "fsync", branch="other"),
        ],
    )
    rc = recall.cli_main(["fsync", "--branch", "main", "--workspace-root", str(tmp_path)])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    # `main`-branch entry should come first due to x2 boost.
    assert payload[0]["id"] == "a" * 16


def test_cli_tickets_csv_parsed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed_workspace(tmp_path)
    _write_entries(
        tmp_path,
        "demo",
        [
            _make_entry("a" * 16, "fsync", ticket="FT-2"),
            _make_entry("b" * 16, "fsync", ticket="FT-99"),
        ],
    )
    rc = recall.cli_main(["fsync", "--tickets", "FT-99,FT-100", "--workspace-root", str(tmp_path)])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["id"] == "b" * 16
