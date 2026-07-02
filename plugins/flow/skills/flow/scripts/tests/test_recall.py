"""Tests for recall.py, hand-rolled BM25 ranker."""

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
    # Full-width ASCII codepoints U+FF46 U+FF4F U+FF4F; NFKC normalizes these to plain f / o / o.
    full_width = chr(0xFF46) + chr(0xFF4F) + chr(0xFF4F)
    assert recall.tokenize(full_width) == ["foo"]


def test_tokenize_empty_returns_empty() -> None:
    assert recall.tokenize("") == []


def test_tokenize_preserves_word_underscores() -> None:
    assert recall.tokenize("foo_bar") == ["foo_bar"]


# ─── rank(), empty corpora ───────────────────────────────────────────────────


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


# ─── rank(), basic BM25 ──────────────────────────────────────────────────────


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


# ─── rank(), exact-match boost ───────────────────────────────────────────────


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


# ─── rank(), field weights ───────────────────────────────────────────────────


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


# ─── rank(), tiebreak ts DESC ────────────────────────────────────────────────


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


# ─── supersession filter ─────────────────────────────────────────────────────


def test_superseded_ids_collects_targets() -> None:
    entries = [
        _make_entry("a" * 16, "first"),
        {**_make_entry("b" * 16, "second"), "supersedes": "a" * 16},
    ]
    assert recall.superseded_ids(entries) == {"a" * 16}


def test_superseded_ids_ignores_empty_and_missing() -> None:
    entries = [
        _make_entry("a" * 16, "no field"),
        {**_make_entry("b" * 16, "empty"), "supersedes": ""},
        {**_make_entry("c" * 16, "none"), "supersedes": None},
    ]
    assert recall.superseded_ids(entries) == set()


def test_superseded_ids_unions_list_targets() -> None:
    entries = [
        _make_entry("a" * 16, "first"),
        _make_entry("b" * 16, "second"),
        _make_entry("c" * 16, "third"),
        {**_make_entry("d" * 16, "canonical"), "supersedes": ["a" * 16, "b" * 16, "c" * 16]},
    ]
    assert recall.superseded_ids(entries) == {"a" * 16, "b" * 16, "c" * 16}


def test_superseded_ids_mixed_str_and_list_tombstones() -> None:
    entries = [
        _make_entry("a" * 16, "first"),
        _make_entry("b" * 16, "second"),
        _make_entry("c" * 16, "third"),
        {**_make_entry("s" * 16, "str tombstone"), "supersedes": "a" * 16},
        {**_make_entry("l" * 16, "list tombstone"), "supersedes": ["b" * 16, "c" * 16]},
    ]
    dead = recall.superseded_ids(entries)
    assert dead == {"a" * 16, "b" * 16, "c" * 16}
    survivors = {e["id"] for e in recall.filter_superseded(entries)}
    assert survivors == {"s" * 16, "l" * 16}


def test_filter_superseded_resolves_chain() -> None:
    # A <- B <- C: B.supersedes=A, C.supersedes=B. Only C survives.
    a = _make_entry("a" * 16, "claim A")
    b = {**_make_entry("b" * 16, "claim B"), "supersedes": "a" * 16}
    c = {**_make_entry("c" * 16, "claim C"), "supersedes": "b" * 16}
    survivors = recall.filter_superseded([a, b, c])
    assert [e["id"] for e in survivors] == ["c" * 16]


def test_filter_superseded_keeps_unreferenced() -> None:
    a = _make_entry("a" * 16, "kept")
    b = {**_make_entry("b" * 16, "tombstone"), "supersedes": "a" * 16}
    d = _make_entry("d" * 16, "independent")
    survivors = recall.filter_superseded([a, b, d])
    ids = {e["id"] for e in survivors}
    assert ids == {"b" * 16, "d" * 16}


def test_cli_excludes_superseded_by_default(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_workspace(tmp_path)
    _write_entries(
        tmp_path,
        "demo",
        [
            _make_entry("a" * 16, "fsync matters"),
            {**_make_entry("b" * 16, "fsync matters more"), "supersedes": "a" * 16},
        ],
    )
    rc = recall.cli_main(["fsync", "--workspace-root", str(tmp_path)])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    ids = {r["id"] for r in payload}
    assert "a" * 16 not in ids
    assert "b" * 16 in ids


def test_cli_include_superseded_returns_dead_entry(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_workspace(tmp_path)
    _write_entries(
        tmp_path,
        "demo",
        [
            _make_entry("a" * 16, "fsync matters"),
            {**_make_entry("b" * 16, "fsync matters more"), "supersedes": "a" * 16},
        ],
    )
    rc = recall.cli_main(["fsync", "--include-superseded", "--workspace-root", str(tmp_path)])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    ids = {r["id"] for r in payload}
    assert "a" * 16 in ids
    assert "b" * 16 in ids


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


# ─── RRF fusion (pure) ─────────────────────────────────────────────────────────


def test_rrf_fuse_id_in_both_lists_scores_higher() -> None:
    fused = recall.rrf_fuse(["a", "b"], ["a", "c"])
    # a is rank 0 in both → highest; b and c each appear once.
    assert fused["a"] > fused["b"]
    assert fused["a"] > fused["c"]


def test_rrf_fuse_partial_id_still_ranks_via_one_list() -> None:
    # b is only in the BM25 list (unindexed/cosine-missing). It still scores → the
    # graceful partial-index property.
    fused = recall.rrf_fuse(["a", "b"], ["a"])
    assert "b" in fused
    assert fused["b"] > 0


def test_rrf_fuse_uses_reciprocal_rank() -> None:
    fused = recall.rrf_fuse(["x"], [])
    assert fused["x"] == pytest.approx(1.0 / (recall.RRF_K + 0))


def test_rrf_fuse_weights_default_is_equal() -> None:
    # The weight param is wired but dormant: the (1,1) default must equal the
    # un-weighted equal-RRF fusion exactly.
    assert recall.rrf_fuse(["a", "b"], ["c"]) == recall.rrf_fuse(
        ["a", "b"], ["c"], weights=(1.0, 1.0)
    )


def test_rrf_fuse_weights_bias_bm25_vs_cosine() -> None:
    # A higher bm25 weight lifts a bm25-only id above an equally-ranked cosine-only id.
    fused = recall.rrf_fuse(["a"], ["b"], weights=(2.0, 1.0))
    assert fused["a"] > fused["b"]
    assert fused["a"] == pytest.approx(2.0 / (recall.RRF_K + 0))
    assert fused["b"] == pytest.approx(1.0 / (recall.RRF_K + 0))


# ─── semantic config gating: disabled path is byte-identical to BM25 ───────────


def _stub_embedder_cmd(tmp_path: Path) -> str:
    """A deterministic 4-dim fake embedder, same contract as the real one."""
    import sys as _sys

    stub = tmp_path / "stub_embedder.py"
    stub.write_text(
        "import sys, json\n"
        "texts=[l.rstrip(chr(10)) for l in sys.stdin.read().splitlines()]\n"
        "def vec(t):\n"
        "    v=[0.0,0.0,0.0,0.0]\n"
        "    for w in t.split():\n"
        "        v[sum(map(ord,w))%4]+=1.0\n"
        "    return v\n"
        "sys.stdout.write(json.dumps([vec(t) for t in texts]))\n",
        encoding="utf-8",
    )
    return f"{_sys.executable} {stub}"


def _seed_semantic_workspace(root: Path, *, embedder: str, threshold: float = 0.0) -> None:
    flow = root / ".flow"
    flow.mkdir(parents=True, exist_ok=True)
    (flow / "workspace.toml").write_text(
        '[tracker]\nbackend = "jira"\n[tracker.jira]\ncloud_id = "x"\nproject_key = "FT"\n\n'
        '[memory]\nnamespace = "demo"\n\n'
        "[memory.semantic]\n"
        "enabled = true\n"
        'model = "stub-model"\n'
        f"threshold = {threshold}\n"
        f'embedder = "{embedder}"\n',
        encoding="utf-8",
    )


def test_disabled_path_byte_identical_to_bm25(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """With [memory.semantic] absent, recall.py output is byte-identical to BM25."""
    _seed_workspace(tmp_path)
    _write_entries(
        tmp_path,
        "demo",
        [
            _make_entry("a" * 16, "atomic write needs fsync"),
            _make_entry("b" * 16, "lorem ipsum dolor"),
        ],
    )
    rc = recall.cli_main(["fsync", "--workspace-root", str(tmp_path)])
    assert rc == 0
    via_cli = capsys.readouterr().out

    entries = recall.filter_superseded(
        recall._load_entries(_memory_paths.knowledge_path(tmp_path, "demo"))
    )
    bm25 = recall.rank("fsync", entries, top_n=5)
    expected = json.dumps(bm25, indent=2, sort_keys=True) + "\n"
    assert via_cli == expected


def test_semantic_disabled_flag_off_no_embed_call(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A workspace with semantic enabled=false never touches the embedder."""
    flow = tmp_path / ".flow"
    flow.mkdir(parents=True)
    (flow / "workspace.toml").write_text(
        '[tracker]\nbackend = "jira"\n[tracker.jira]\ncloud_id = "x"\nproject_key = "FT"\n\n'
        '[memory]\nnamespace = "demo"\n\n[memory.semantic]\nenabled = false\n',
        encoding="utf-8",
    )
    _write_entries(tmp_path, "demo", [_make_entry("a" * 16, "fsync matters")])
    rc = recall.cli_main(["fsync", "--workspace-root", str(tmp_path)])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["id"] == "a" * 16


def test_malformed_config_threshold_semantic_off_still_ranks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A workspace.toml threshold typo must not kill recall, even with semantic off."""
    flow = tmp_path / ".flow"
    flow.mkdir(parents=True)
    (flow / "workspace.toml").write_text(
        '[tracker]\nbackend = "jira"\n[tracker.jira]\ncloud_id = "x"\nproject_key = "FT"\n\n'
        '[memory]\nnamespace = "demo"\n\n'
        '[memory.semantic]\nenabled = false\nthreshold = "high"\n',
        encoding="utf-8",
    )
    _write_entries(tmp_path, "demo", [_make_entry("a" * 16, "fsync matters")])
    rc = recall.cli_main(["fsync", "--workspace-root", str(tmp_path)])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["id"] == "a" * 16


def test_malformed_config_threshold_semantic_on_falls_back_to_bm25(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    flow = tmp_path / ".flow"
    flow.mkdir(parents=True)
    (flow / "workspace.toml").write_text(
        '[tracker]\nbackend = "jira"\n[tracker.jira]\ncloud_id = "x"\nproject_key = "FT"\n\n'
        '[memory]\nnamespace = "demo"\n\n'
        '[memory.semantic]\nenabled = true\nthreshold = ["oops"]\n',
        encoding="utf-8",
    )
    _write_entries(tmp_path, "demo", [_make_entry("a" * 16, "fsync matters")])
    rc = recall.cli_main(["fsync", "--workspace-root", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr()
    assert "bm25-fallback" in out.err
    payload = json.loads(out.out)
    assert payload[0]["id"] == "a" * 16


# ─── semantic fusion path ──────────────────────────────────────────────────────


def test_semantic_path_active_with_index(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import memory_embed

    embedder = _stub_embedder_cmd(tmp_path)
    _seed_semantic_workspace(tmp_path, embedder=embedder, threshold=0.0)
    _write_entries(
        tmp_path,
        "demo",
        [
            _make_entry("a" * 16, "local lake table writes"),
            _make_entry("b" * 16, "unrelated prose here"),
        ],
    )
    memory_embed.reindex(tmp_path, "demo", model="stub-model", embedder=embedder)
    rc = recall.cli_main(["lake table", "--workspace-root", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr()
    assert "semantic-active" in out.err
    payload = json.loads(out.out)
    assert {r["id"] for r in payload} <= {"a" * 16, "b" * 16}


def test_semantic_threshold_drops_low_cosine(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import memory_embed

    embedder = _stub_embedder_cmd(tmp_path)
    # threshold 1.01 is unreachable by cosine (max 1.0) → cosine list empty →
    # fusion falls to BM25-only ordering, but the path is still semantic-active.
    _seed_semantic_workspace(tmp_path, embedder=embedder, threshold=1.01)
    _write_entries(tmp_path, "demo", [_make_entry("a" * 16, "fsync matters")])
    memory_embed.reindex(tmp_path, "demo", model="stub-model", embedder=embedder)
    rc = recall.cli_main(["fsync", "--workspace-root", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr()
    assert "cosine_candidates=0" in out.err
    payload = json.loads(out.out)
    assert payload[0]["id"] == "a" * 16


def test_semantic_fusion_reorders_vs_bm25(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Fusion must actually reorder results; running without error is not enough. BM25 ranks
    the lexical match (A) first; the strong cosine match (B, no lexical overlap) is lifted
    above it.

    Stub 4-dim bins by sum(ord(word)) % 4. Query "fsync" -> bin3 = [0,0,0,1].
    A "fsync zzz" -> [0,0,1,1], cosine 0.707 (dropped by tau=0.8) but BM25 rank0.
    B "be be"     -> [0,0,0,2], cosine 1.0 (kept) but BM25 last (no token match).
    """
    import memory_embed

    embedder = _stub_embedder_cmd(tmp_path)
    _seed_semantic_workspace(tmp_path, embedder=embedder, threshold=0.8)
    _write_entries(
        tmp_path,
        "demo",
        [
            _make_entry("a" * 16, "fsync zzz"),
            _make_entry("b" * 16, "be be"),
        ],
    )
    memory_embed.reindex(tmp_path, "demo", model="stub-model", embedder=embedder)

    # BM25 alone ranks the lexical match A first.
    entries = recall.filter_superseded(
        recall._load_entries(_memory_paths.knowledge_path(tmp_path, "demo"))
    )
    assert [r["id"] for r in recall.rank("fsync", entries, top_n=5)] == ["a" * 16, "b" * 16]

    # Semantic fusion lifts B (cosine 1.0) above A (cosine dropped by tau).
    rc = recall.cli_main(["fsync", "--workspace-root", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr()
    assert "semantic-active" in out.err
    assert [r["id"] for r in json.loads(out.out)] == ["b" * 16, "a" * 16]


def test_semantic_topk_caps_candidates_without_starving(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Regression for the absolute-threshold no-op (flow-nylh): cosine candidates
    are selected by RANK (top-K = top_n*2, min 20), never by an embedder-coupled
    absolute threshold. 25 entries all share the query token → all positive cosine;
    the pool caps at 20 and is never starved to 0."""
    import memory_embed

    embedder = _stub_embedder_cmd(tmp_path)
    _seed_semantic_workspace(tmp_path, embedder=embedder, threshold=0.0)
    _write_entries(
        tmp_path,
        "demo",
        [_make_entry(f"{i:02d}".ljust(16, "x"), f"fsync note {i}") for i in range(25)],
    )
    memory_embed.reindex(tmp_path, "demo", model="stub-model", embedder=embedder)
    rc = recall.cli_main(["fsync", "--workspace-root", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr()
    assert "semantic-active" in out.err
    # top-K cap (top_n=5 → K=20): NOT 0 (no starvation) and NOT 25 (capped).
    assert "cosine_candidates=20" in out.err


def test_semantic_falls_back_when_index_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    embedder = _stub_embedder_cmd(tmp_path)
    _seed_semantic_workspace(tmp_path, embedder=embedder, threshold=0.0)
    _write_entries(tmp_path, "demo", [_make_entry("a" * 16, "fsync matters")])
    # no reindex → no sidecar → fall back to BM25, status on stderr.
    rc = recall.cli_main(["fsync", "--workspace-root", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr()
    assert "bm25-fallback" in out.err
    payload = json.loads(out.out)
    assert payload[0]["id"] == "a" * 16


def test_semantic_falls_back_on_model_mismatch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import memory_embed

    embedder = _stub_embedder_cmd(tmp_path)
    _seed_semantic_workspace(tmp_path, embedder=embedder, threshold=0.0)
    _write_entries(tmp_path, "demo", [_make_entry("a" * 16, "fsync matters")])
    # index built under a DIFFERENT model than configured → mismatch → BM25 fallback.
    memory_embed.reindex(tmp_path, "demo", model="other-model", embedder=embedder)
    rc = recall.cli_main(["fsync", "--workspace-root", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr()
    assert "bm25-fallback" in out.err
    payload = json.loads(out.out)
    assert payload[0]["id"] == "a" * 16


def test_semantic_partial_index_graceful(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An entry missing from the index still surfaces via BM25 in the fusion."""
    import memory_embed

    embedder = _stub_embedder_cmd(tmp_path)
    _seed_semantic_workspace(tmp_path, embedder=embedder, threshold=0.0)
    _write_entries(tmp_path, "demo", [_make_entry("a" * 16, "fsync durability")])
    memory_embed.reindex(tmp_path, "demo", model="stub-model", embedder=embedder)
    # add a second entry but do NOT reindex; it is unindexed.
    _write_entries(
        tmp_path,
        "demo",
        [
            _make_entry("a" * 16, "fsync durability"),
            _make_entry("b" * 16, "fsync matters too"),
        ],
    )
    rc = recall.cli_main(["fsync", "--top-n", "5", "--workspace-root", str(tmp_path)])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    ids = {r["id"] for r in payload}
    assert "b" * 16 in ids  # unindexed entry still ranks via BM25


# ─── --record-pending + --reindex + --query-file ──────────────────────────────


def test_record_pending_appends(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import recall_pending

    _seed_workspace(tmp_path)
    _write_entries(tmp_path, "demo", [_make_entry("a" * 16, "fsync matters")])
    rc = recall.cli_main(
        [
            "fsync",
            "--record-pending",
            "--branch",
            "main",
            "--ticket",
            "FT-1",
            "--workspace-root",
            str(tmp_path),
        ]
    )
    assert rc == 0
    pending = recall_pending.list_pending(tmp_path)
    assert pending
    assert pending[0]["hook_time_resolved_ticket"] == "FT-1"
    assert "a" * 16 in pending[0]["returned_ids"]


def test_record_pending_requires_branch_and_ticket(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_workspace(tmp_path)
    _write_entries(tmp_path, "demo", [_make_entry("a" * 16, "fsync")])
    rc = recall.cli_main(["fsync", "--record-pending", "--workspace-root", str(tmp_path)])
    assert rc == 1
    assert "needs --branch and --ticket" in capsys.readouterr().err


def test_query_file_read(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed_workspace(tmp_path)
    _write_entries(tmp_path, "demo", [_make_entry("a" * 16, "fsync durability notes")])
    qf = tmp_path / "q.txt"
    qf.write_text("fsync durability", encoding="utf-8")
    rc = recall.cli_main(["--query-file", str(qf), "--workspace-root", str(tmp_path)])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["id"] == "a" * 16


def test_no_query_exit_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed_workspace(tmp_path)
    _write_entries(tmp_path, "demo", [_make_entry("a" * 16, "fsync")])
    rc = recall.cli_main(["--workspace-root", str(tmp_path)])
    assert rc == 1
    assert "no query" in capsys.readouterr().err


def test_reindex_dispatch(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed_semantic_workspace(tmp_path, embedder=_stub_embedder_cmd(tmp_path), threshold=0.0)
    _write_entries(tmp_path, "demo", [_make_entry("a" * 16, "one")])
    rc = recall.cli_main(["--reindex", "--workspace-root", str(tmp_path)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["live"] == 1


def test_record_pending_promotes_into_recall_log(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """End-to-end: --record-pending writes recall-pending against the worktree, then
    dispatch_stage's promoter (promote_matching) folds it into the run's recall-log.

    The promotion rules are exact matches on branch + cwd + a head-sha-ancestor
    check, so the record and the promote must agree on the workspace root. This is
    the chain the plan's Verification demanded (recalled ids the plan saw appear in
    the reflect bundle's recalled_entries).
    """
    import subprocess

    import recall_pending

    # git-init the tmp dir so promote_matching's `merge-base --is-ancestor` resolves.
    def _git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=str(tmp_path), capture_output=True, check=True)

    _git("init", "--initial-branch=feature/FT-1-x")
    _git("config", "user.email", "t@example.com")
    _git("config", "user.name", "t")
    (tmp_path / "README.md").write_text("x\n", encoding="utf-8")
    _git("add", "README.md")
    _git("commit", "-m", "initial")

    _seed_workspace(tmp_path)
    _write_entries(tmp_path, "demo", [_make_entry("a" * 16, "fsync durability notes")])

    branch = "feature/FT-1-x"
    rc = recall.cli_main(
        [
            "fsync",
            "--record-pending",
            "--branch",
            branch,
            "--ticket",
            "FT-1",
            "--workspace-root",
            str(tmp_path),
        ]
    )
    assert rc == 0

    promoted = recall_pending.promote_matching(
        tmp_path,
        ticket="FT-1",
        branch=branch,
        head_sha="",  # accepted for CLI symmetry; rule (e) compares entry.head_sha to HEAD
        cwd=str(tmp_path),
        now_iso="2026-06-18T00:00:00Z",
    )
    assert promoted, "the recorded entry should promote (branch+cwd+ancestor all match)"
    log = tmp_path / ".flow" / "runs" / "FT-1" / "recall-log.jsonl"
    assert log.exists()
    lines = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    assert any("a" * 16 in rec.get("returned_ids", []) for rec in lines)


# ─── labels (faceted memory) ───────────────────────────────────────────────────


def _labeled_entry(id_: str, body: str, labels: list[str], **kwargs) -> dict:
    return {**_make_entry(id_, body, **kwargs), "labels": labels}


def test_label_filter_returns_only_cluster_members() -> None:
    entries = [
        _labeled_entry("a" * 16, "iva note one", ["form:iva_2083"]),
        _labeled_entry("b" * 16, "iva note two", ["form:iva_2083"]),
        _make_entry("c" * 16, "unrelated"),
    ]
    results = recall.rank("", entries, label_filter="form:iva_2083", top_n=10)
    ids = {r["id"] for r in results}
    assert ids == {"a" * 16, "b" * 16}


def test_label_filter_exhaustive_via_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_workspace(tmp_path)
    entries = [_labeled_entry(f"{i:016x}", f"iva note {i}", ["form:iva_2083"]) for i in range(8)]
    _write_entries(tmp_path, "demo", entries)
    rc = recall.cli_main(["--label", "form:iva_2083", "--workspace-root", str(tmp_path)])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload) == 8


def test_label_only_recall_orders_ts_desc_exit_0(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_workspace(tmp_path)
    entries = [
        _labeled_entry("a" * 16, "old", ["form:iva_2083"], ts="2026-01-01T00:00:00.000Z"),
        _labeled_entry("b" * 16, "new", ["form:iva_2083"], ts="2026-06-01T00:00:00.000Z"),
    ]
    _write_entries(tmp_path, "demo", entries)
    rc = recall.cli_main(["--label", "form:iva_2083", "--workspace-root", str(tmp_path)])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert [r["id"] for r in payload] == ["b" * 16, "a" * 16]


def test_labels_present_in_result_dict() -> None:
    entries = [_labeled_entry("a" * 16, "iva note", ["form:iva_2083"])]
    results = recall.rank("", entries, label_filter="form:iva_2083", top_n=5)
    assert results[0]["labels"] == ["form:iva_2083"]


def test_labels_key_defaults_empty_when_absent() -> None:
    entries = [_make_entry("a" * 16, "foo")]
    results = recall.rank("foo", entries, top_n=1)
    assert results[0]["labels"] == []


def test_backward_compat_no_labels_corpus_score_unchanged() -> None:
    # re-assert an existing score case: adding "labels" to FIELD_WEIGHTS must be
    # a zero-contribution no-op for a label-free corpus (avgdl==0 guard).
    entries = [
        _make_entry("a" * 16, "atomic write needs fsync"),
        _make_entry("b" * 16, "lorem ipsum dolor sit amet"),
    ]
    results = recall.rank("atomic write", entries, top_n=2)
    assert results[0]["body"].startswith("atomic")
    assert results[0]["score"] > results[1]["score"]


def test_field_weight_label_fuzzy_reach() -> None:
    entries = [
        _labeled_entry("a" * 16, "unrelated body", ["form:iva_2083"]),
        _make_entry("b" * 16, "unrelated body too"),
    ]
    results = recall.rank("iva_2083", entries, top_n=2)
    assert results[0]["id"] == "a" * 16


def test_doc_field_text_list_join() -> None:
    entry = {"labels": ["form:iva_2083"]}
    text = recall._doc_field_text(entry, "labels")
    assert recall.tokenize(text) == ["form", "iva_2083"]


def test_semantic_label_filter_restricts_cluster(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # a NON-empty query keeps the semantic path active; --label restricts the
    # candidate cluster inside _semantic_rank (distinct from the label-only,
    # forced-BM25 case covered separately).
    import memory_embed

    embedder = _stub_embedder_cmd(tmp_path)
    _seed_semantic_workspace(tmp_path, embedder=embedder, threshold=0.0)
    _write_entries(
        tmp_path,
        "demo",
        [
            _labeled_entry("a" * 16, "iva note", ["form:iva_2083"]),
            _make_entry("b" * 16, "iva note too"),
        ],
    )
    memory_embed.reindex(tmp_path, "demo", model="stub-model", embedder=embedder)
    rc = recall.cli_main(
        ["iva note", "--label", "form:iva_2083", "--workspace-root", str(tmp_path)]
    )
    assert rc == 0
    out = capsys.readouterr()
    assert "semantic-active" in out.err
    payload = json.loads(out.out)
    assert {r["id"] for r in payload} == {"a" * 16}


def test_label_only_query_forces_bm25_no_embed_call(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import memory_embed

    embedder = _stub_embedder_cmd(tmp_path)
    _seed_semantic_workspace(tmp_path, embedder=embedder, threshold=0.0)
    _write_entries(tmp_path, "demo", [_labeled_entry("a" * 16, "iva note", ["form:iva_2083"])])
    memory_embed.reindex(tmp_path, "demo", model="stub-model", embedder=embedder)

    def _boom(*args, **kwargs):
        raise AssertionError("embed must not be called for a label-only query")

    monkeypatch.setattr(memory_embed, "embed", _boom)
    rc = recall.cli_main(["--label", "form:iva_2083", "--workspace-root", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr()
    assert "semantic-active" not in out.err
    payload = json.loads(out.out)
    assert payload[0]["id"] == "a" * 16


class _MustNotReadStdin:
    def isatty(self) -> bool:
        return False

    def read(self) -> str:
        raise AssertionError("label-only recall must never read stdin")


# ─── digest (--digest markdown card over --label cluster) ─────────────────────


def test_digest_without_label_is_argparse_error(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        recall.cli_main(["--digest", "--workspace-root", str(tmp_path)])
    assert exc.value.code == 2


def test_digest_sections_grouped_in_canonical_order(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_workspace(tmp_path)
    entries = [
        _labeled_entry("f" * 16, "a fact", ["form:iva_2083"], type_="FACT"),
        _labeled_entry("d" * 16, "a decision", ["form:iva_2083"], type_="DECISION"),
        _labeled_entry("l" * 16, "a learning", ["form:iva_2083"], type_="LEARNED"),
    ]
    _write_entries(tmp_path, "demo", entries)
    rc = recall.cli_main(
        ["--label", "form:iva_2083", "--digest", "--workspace-root", str(tmp_path)]
    )
    assert rc == 0
    out = capsys.readouterr().out
    # DECISION, FACT, LEARNED in that order regardless of write/entry order.
    assert out.index("DECISION") < out.index("FACT") < out.index("LEARNED")


def test_digest_only_non_empty_sections_rendered(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_workspace(tmp_path)
    entries = [_labeled_entry("d" * 16, "a decision", ["form:iva_2083"], type_="DECISION")]
    _write_entries(tmp_path, "demo", entries)
    rc = recall.cli_main(
        ["--label", "form:iva_2083", "--digest", "--workspace-root", str(tmp_path)]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "DECISION" in out
    for absent in ("FACT", "LEARNED", "PATTERN", "INVESTIGATION", "DEVIATION"):
        assert absent not in out


def test_digest_all_six_valid_types_render(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_workspace(tmp_path)
    types = ["DECISION", "FACT", "LEARNED", "PATTERN", "INVESTIGATION", "DEVIATION"]
    entries = [
        _labeled_entry(t.lower().ljust(16, "x")[:16], f"body for {t}", ["form:iva_2083"], type_=t)
        for t in types
    ]
    _write_entries(tmp_path, "demo", entries)
    rc = recall.cli_main(
        ["--label", "form:iva_2083", "--digest", "--workspace-root", str(tmp_path)]
    )
    assert rc == 0
    out = capsys.readouterr().out
    for t in types:
        assert t in out
    # canonical order preserved across all six.
    positions = [out.index(t) for t in types]
    assert positions == sorted(positions)


def test_digest_within_section_newest_first(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_workspace(tmp_path)
    entries = [
        _labeled_entry(
            "a" * 16,
            "old learning",
            ["form:iva_2083"],
            type_="LEARNED",
            ts="2026-01-01T00:00:00.000Z",
        ),
        _labeled_entry(
            "b" * 16,
            "new learning",
            ["form:iva_2083"],
            type_="LEARNED",
            ts="2026-06-01T00:00:00.000Z",
        ),
    ]
    _write_entries(tmp_path, "demo", entries)
    rc = recall.cli_main(
        ["--label", "form:iva_2083", "--digest", "--workspace-root", str(tmp_path)]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert out.index("new learning") < out.index("old learning")


def test_digest_line_carries_ts_ticket_first_sentence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_workspace(tmp_path)
    entries = [
        _labeled_entry(
            "a" * 16,
            "First sentence here. Second sentence should be dropped.",
            ["form:iva_2083"],
            type_="LEARNED",
            ts="2026-01-01T00:00:00.000Z",
            ticket="FT-7",
        ),
    ]
    _write_entries(tmp_path, "demo", entries)
    rc = recall.cli_main(
        ["--label", "form:iva_2083", "--digest", "--workspace-root", str(tmp_path)]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "2026-01-01T00:00:00.000Z" in out
    assert "FT-7" in out
    assert "First sentence here." not in out  # trailing period stripped by split
    assert "First sentence here" in out
    assert "Second sentence should be dropped" not in out


def test_digest_no_period_uses_whole_body() -> None:
    entries = [_labeled_entry("a" * 16, "no terminal period here", ["form:iva_2083"])]
    results = recall.rank("", entries, label_filter="form:iva_2083", top_n=5)
    rendered = recall._render_digest(results, "form:iva_2083")
    assert "no terminal period here" in rendered


def test_digest_excludes_superseded(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed_workspace(tmp_path)
    entries = [
        _labeled_entry("a" * 16, "superseded note", ["form:iva_2083"]),
        {
            **_labeled_entry("b" * 16, "canonical note", ["form:iva_2083"]),
            "supersedes": "a" * 16,
        },
    ]
    _write_entries(tmp_path, "demo", entries)
    rc = recall.cli_main(
        ["--label", "form:iva_2083", "--digest", "--workspace-root", str(tmp_path)]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "canonical note" in out
    assert "superseded note" not in out


def test_digest_empty_cluster_still_renders_header(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_workspace(tmp_path)
    rc = recall.cli_main(["--label", "form:missing", "--digest", "--workspace-root", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "form:missing" in out
    assert "no entries" in out.lower()


def test_plain_json_output_without_digest_unchanged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_workspace(tmp_path)
    entries = [_labeled_entry("a" * 16, "iva note", ["form:iva_2083"])]
    _write_entries(tmp_path, "demo", entries)
    rc = recall.cli_main(["--label", "form:iva_2083", "--workspace-root", str(tmp_path)])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["id"] == "a" * 16


def test_label_only_recall_never_touches_stdin(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # pytest's captured stdin raises OSError on read, which _read_query
    # swallows, masking a live hang: the harness Bash pipe has no EOF, so a
    # blocking read wedges the stage. Pin that the read is never attempted.
    _seed_workspace(tmp_path)
    entries = [_labeled_entry("a" * 16, "iva note", ["form:iva_2083"])]
    _write_entries(tmp_path, "demo", entries)
    monkeypatch.setattr(recall.sys, "stdin", _MustNotReadStdin())
    rc = recall.cli_main(["--label", "form:iva_2083", "--workspace-root", str(tmp_path)])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert [r["id"] for r in payload] == ["a" * 16]
