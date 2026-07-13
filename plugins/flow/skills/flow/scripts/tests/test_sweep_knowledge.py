"""Contract tests for sweep_knowledge.py: maintainer-gated curation over knowledge.jsonl."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import _memory_paths
import recall
import sweep_knowledge
from _jsonl import iter_jsonl

# Real fixture ids from the acceptance proof (flow-8we / flow-014).
FLOW_8WE_ID = "a0637df9ff529353"
FLOW_014_ID = "8c575fe06d41ad29"


def _seed_workspace(root: Path, namespace: str = "demo") -> None:
    flow = root / ".flow"
    flow.mkdir(parents=True, exist_ok=True)
    (flow / "workspace.toml").write_text(
        '[tracker]\nbackend = "jira"\n[tracker.jira]\ncloud_id = "x"\nproject_key = "FT"\n'
        f'\n[memory]\nnamespace = "{namespace}"\n',
        encoding="utf-8",
    )


def _write_entries(root: Path, entries: list[dict], namespace: str = "demo") -> None:
    kpath = _memory_paths.knowledge_path(root, namespace)
    kpath.parent.mkdir(parents=True, exist_ok=True)
    with kpath.open("w", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e, sort_keys=True) + "\n")


def _read_entries(root: Path, namespace: str = "demo") -> list[dict]:
    kpath = _memory_paths.knowledge_path(root, namespace)
    if not kpath.exists():
        return []
    return [
        json.loads(line) for line in kpath.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def _load_all(root: Path, namespace: str = "demo") -> list[dict]:
    kpath = _memory_paths.knowledge_path(root, namespace)
    sidecar = kpath.with_name(f"{kpath.name}.quarantine.test")
    return list(iter_jsonl(kpath, sidecar))


def _entry(
    id_: str, type_: str, body: str, ticket: str = "FT-1", supersedes: str | None = None
) -> dict:
    e = {
        "id": id_,
        "ts": "2026-01-01T00:00:00.000Z",
        "type": type_,
        "namespace": "demo",
        "branch": "main",
        "ticket": ticket,
        "body": body,
    }
    if supersedes is not None:
        e["supersedes"] = supersedes
    return e


# ─── propose ─────────────────────────────────────────────────────────────────


def test_propose_lists_decision_and_fact(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_workspace(tmp_path)
    _write_entries(
        tmp_path,
        [
            _entry("1111111111111111", "DECISION", "a decision"),
            _entry("2222222222222222", "FACT", "a fact"),
            _entry("3333333333333333", "LEARNED", "a learned thing"),
        ],
    )
    rc = sweep_knowledge.cli_main(["propose", "--workspace-root", str(tmp_path)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert {item["id"] for item in out} == {"1111111111111111", "2222222222222222"}
    first = out[0]
    assert set(first.keys()) == {"id", "ticket", "ts", "type", "body"}


def test_propose_excludes_superseded(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed_workspace(tmp_path)
    _write_entries(
        tmp_path,
        [
            _entry("aaaaaaaaaaaaaaaa", "DECISION", "X the old"),
            _entry("bbbbbbbbbbbbbbbb", "DECISION", "Y supersedes X", supersedes="aaaaaaaaaaaaaaaa"),
        ],
    )
    rc = sweep_knowledge.cli_main(["propose", "--workspace-root", str(tmp_path)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    ids = {item["id"] for item in out}
    assert "aaaaaaaaaaaaaaaa" not in ids
    assert "bbbbbbbbbbbbbbbb" in ids


def test_propose_type_filter_fact_only(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed_workspace(tmp_path)
    _write_entries(
        tmp_path,
        [
            _entry("1111111111111111", "DECISION", "a decision"),
            _entry("2222222222222222", "FACT", "a fact"),
        ],
    )
    rc = sweep_knowledge.cli_main(["propose", "--type", "FACT", "--workspace-root", str(tmp_path)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert {item["type"] for item in out} == {"FACT"}


def test_propose_default_type_is_decision_and_fact(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_workspace(tmp_path)
    _write_entries(
        tmp_path,
        [
            _entry("1111111111111111", "DECISION", "a decision"),
            _entry("2222222222222222", "FACT", "a fact"),
            _entry("3333333333333333", "PATTERN", "a pattern"),
        ],
    )
    rc = sweep_knowledge.cli_main(["propose", "--workspace-root", str(tmp_path)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert {item["type"] for item in out} == {"DECISION", "FACT"}


def test_propose_empty_store_emits_empty_list(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_workspace(tmp_path)
    rc = sweep_knowledge.cli_main(["propose", "--workspace-root", str(tmp_path)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out == []


def test_propose_preserves_file_order(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed_workspace(tmp_path)
    _write_entries(
        tmp_path,
        [
            _entry("3333333333333333", "DECISION", "third"),
            _entry("1111111111111111", "FACT", "first"),
            _entry("2222222222222222", "DECISION", "second"),
        ],
    )
    rc = sweep_knowledge.cli_main(["propose", "--workspace-root", str(tmp_path)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert [item["id"] for item in out] == [
        "3333333333333333",
        "1111111111111111",
        "2222222222222222",
    ]


# ─── apply ───────────────────────────────────────────────────────────────────


def test_apply_seed_fixture_catch(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # the acceptance proof: seed two DECISION entries with the real fixture ids,
    # apply a manifest naming both, confirm both are superseded after.
    _seed_workspace(tmp_path)
    _write_entries(
        tmp_path,
        [
            _entry(FLOW_8WE_ID, "DECISION", "flow-8we original decision"),
            _entry(FLOW_014_ID, "DECISION", "flow-014 original decision"),
        ],
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            [
                {
                    "superseded_id": FLOW_8WE_ID,
                    "superseding_ticket": "flow-mse",
                    "rationale": "flow-mse shipped the version stamp; this decision is moot",
                },
                {
                    "superseded_id": FLOW_014_ID,
                    "superseding_ticket": "flow-6gx.4",
                    "rationale": "flow-6gx.4 reorganized version files; superseded",
                },
            ]
        ),
        encoding="utf-8",
    )
    rc = sweep_knowledge.cli_main(
        ["apply", "--manifest", str(manifest), "--workspace-root", str(tmp_path)]
    )
    assert rc == 0
    entries = _load_all(tmp_path)
    supersedes_targets = {e.get("supersedes") for e in entries if e.get("supersedes")}
    assert FLOW_8WE_ID in supersedes_targets
    assert FLOW_014_ID in supersedes_targets
    survivors = {e["id"] for e in recall.filter_superseded(entries)}
    assert FLOW_8WE_ID not in survivors
    assert FLOW_014_ID not in survivors


def test_apply_idempotent(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed_workspace(tmp_path)
    _write_entries(tmp_path, [_entry(FLOW_8WE_ID, "DECISION", "original")])
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(
        json.dumps(
            {
                "superseded_id": FLOW_8WE_ID,
                "superseding_ticket": "flow-mse",
                "rationale": "moot now",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    rc1 = sweep_knowledge.cli_main(
        ["apply", "--manifest", str(manifest), "--workspace-root", str(tmp_path)]
    )
    assert rc1 == 0
    capsys.readouterr()
    count_after_first = len(_read_entries(tmp_path))

    rc2 = sweep_knowledge.cli_main(
        ["apply", "--manifest", str(manifest), "--workspace-root", str(tmp_path)]
    )
    assert rc2 == 0
    out2 = json.loads(capsys.readouterr().out)
    assert len(_read_entries(tmp_path)) == count_after_first
    assert all(r["result"] == "skipped" for r in out2["results"])


def test_apply_unknown_id_errors_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_workspace(tmp_path)
    _write_entries(tmp_path, [_entry(FLOW_8WE_ID, "DECISION", "real")])
    before = len(_read_entries(tmp_path))
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            [
                {
                    "superseded_id": "ffffffffffffffff",
                    "superseding_ticket": "flow-x",
                    "rationale": "ghost",
                }
            ]
        ),
        encoding="utf-8",
    )
    rc = sweep_knowledge.cli_main(
        ["apply", "--manifest", str(manifest), "--workspace-root", str(tmp_path)]
    )
    assert rc > 0
    out = json.loads(capsys.readouterr().out)
    err_rec = next(r for r in out["results"] if r["superseded_id"] == "ffffffffffffffff")
    assert err_rec["result"] == "error"
    # no spurious append for the unknown record.
    assert len(_read_entries(tmp_path)) == before


def test_apply_continues_past_bad_record(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_workspace(tmp_path)
    _write_entries(tmp_path, [_entry(FLOW_8WE_ID, "DECISION", "real")])
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            [
                {"superseded_id": "ffffffffffffffff", "superseding_ticket": "g", "rationale": "x"},
                {"superseded_id": FLOW_8WE_ID, "superseding_ticket": "flow-mse", "rationale": "ok"},
            ]
        ),
        encoding="utf-8",
    )
    rc = sweep_knowledge.cli_main(
        ["apply", "--manifest", str(manifest), "--workspace-root", str(tmp_path)]
    )
    assert rc > 0
    out = json.loads(capsys.readouterr().out)
    results = {r["superseded_id"]: r["result"] for r in out["results"]}
    assert results["ffffffffffffffff"] == "error"
    assert results[FLOW_8WE_ID] == "applied"
    survivors = {e["id"] for e in recall.filter_superseded(_load_all(tmp_path))}
    assert FLOW_8WE_ID not in survivors


def test_apply_accepts_jsonl_manifest(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed_workspace(tmp_path)
    _write_entries(tmp_path, [_entry(FLOW_8WE_ID, "DECISION", "real")])
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(
        "\n".join(
            [
                "",
                json.dumps(
                    {
                        "superseded_id": FLOW_8WE_ID,
                        "superseding_ticket": "flow-mse",
                        "rationale": "jsonl shape",
                    }
                ),
                "",
            ]
        ),
        encoding="utf-8",
    )
    rc = sweep_knowledge.cli_main(
        ["apply", "--manifest", str(manifest), "--workspace-root", str(tmp_path)]
    )
    assert rc == 0
    survivors = {e["id"] for e in recall.filter_superseded(_load_all(tmp_path))}
    assert FLOW_8WE_ID not in survivors


def test_apply_accepts_json_array_manifest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_workspace(tmp_path)
    _write_entries(tmp_path, [_entry(FLOW_8WE_ID, "DECISION", "real")])
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            [
                {
                    "superseded_id": FLOW_8WE_ID,
                    "superseding_ticket": "flow-mse",
                    "rationale": "json-array shape",
                }
            ]
        ),
        encoding="utf-8",
    )
    rc = sweep_knowledge.cli_main(
        ["apply", "--manifest", str(manifest), "--workspace-root", str(tmp_path)]
    )
    assert rc == 0
    survivors = {e["id"] for e in recall.filter_superseded(_load_all(tmp_path))}
    assert FLOW_8WE_ID not in survivors


def test_apply_appended_record_fields(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed_workspace(tmp_path)
    _write_entries(tmp_path, [_entry(FLOW_8WE_ID, "DECISION", "real")])
    manifest = tmp_path / "manifest.json"
    rationale = "this is the tombstone rationale text"
    manifest.write_text(
        json.dumps(
            [
                {
                    "superseded_id": FLOW_8WE_ID,
                    "superseding_ticket": "flow-mse",
                    "rationale": rationale,
                }
            ]
        ),
        encoding="utf-8",
    )
    rc = sweep_knowledge.cli_main(
        ["apply", "--manifest", str(manifest), "--workspace-root", str(tmp_path)]
    )
    assert rc == 0
    appended = next(e for e in _load_all(tmp_path) if e.get("supersedes") == FLOW_8WE_ID)
    assert appended["body"] == rationale
    assert appended["ticket"] == "flow-mse"
    assert appended["type"] == "DECISION"


def test_apply_derives_branch_from_ticket(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_workspace(tmp_path)
    _write_entries(tmp_path, [_entry(FLOW_8WE_ID, "DECISION", "real")])
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            [{"superseded_id": FLOW_8WE_ID, "superseding_ticket": "flow-mse", "rationale": "x"}]
        ),
        encoding="utf-8",
    )
    rc = sweep_knowledge.cli_main(
        ["apply", "--manifest", str(manifest), "--workspace-root", str(tmp_path)]
    )
    assert rc == 0
    appended = next(e for e in _load_all(tmp_path) if e.get("supersedes") == FLOW_8WE_ID)
    assert appended["branch"] == "feat/flow-mse"


def test_apply_explicit_branch_honored(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed_workspace(tmp_path)
    _write_entries(tmp_path, [_entry(FLOW_8WE_ID, "DECISION", "real")])
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            [
                {
                    "superseded_id": FLOW_8WE_ID,
                    "superseding_ticket": "flow-mse",
                    "rationale": "x",
                    "branch": "custom/branch-name",
                }
            ]
        ),
        encoding="utf-8",
    )
    rc = sweep_knowledge.cli_main(
        ["apply", "--manifest", str(manifest), "--workspace-root", str(tmp_path)]
    )
    assert rc == 0
    appended = next(e for e in _load_all(tmp_path) if e.get("supersedes") == FLOW_8WE_ID)
    assert appended["branch"] == "custom/branch-name"


def test_apply_empty_superseded_id_errors_no_append(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_workspace(tmp_path)
    _write_entries(tmp_path, [_entry(FLOW_8WE_ID, "DECISION", "real")])
    before = len(_read_entries(tmp_path))
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps([{"superseding_ticket": "flow-x", "rationale": "no target"}]),
        encoding="utf-8",
    )
    rc = sweep_knowledge.cli_main(
        ["apply", "--manifest", str(manifest), "--workspace-root", str(tmp_path)]
    )
    assert rc > 0
    out = json.loads(capsys.readouterr().out)
    assert out["results"][0]["result"] == "error"
    assert len(_read_entries(tmp_path)) == before


# --- propose --with-usage + --type all ----------------------------------------


def _write_usage(root: Path, records: list[dict], namespace: str = "demo") -> None:
    import recall_usage

    path = recall_usage.recall_usage_path(root, namespace)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")


def _usage(recalled_id: str, used: bool, ts: str) -> dict:
    return {"kind": "usage", "run_id": "r1", "recalled_id": recalled_id, "used": used, "ts": ts}


def test_propose_with_usage_zero_fills_absent_entries(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    _write_entries(tmp_path, [_entry("a1", "DECISION", "x")])
    _write_usage(tmp_path, [])
    worklist = sweep_knowledge.propose(tmp_path, ["DECISION"], with_usage=True)
    assert worklist[0]["surfaced_count"] == 0
    assert worklist[0]["used_count"] == 0
    assert worklist[0]["miss_count"] == 0
    assert worklist[0]["last_surfaced"] is None
    assert worklist[0]["tier"] == 1


def test_propose_with_usage_tier_assignment(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    _write_entries(
        tmp_path,
        [
            _entry("unused", "DECISION", "surfaced never used"),
            _entry("young", "DECISION", "never surfaced"),
            _entry("earner", "DECISION", "surfaced and used"),
        ],
    )
    _write_usage(
        tmp_path,
        [
            _usage("unused", False, "2026-06-01T00:00:00.000Z"),
            _usage("earner", True, "2026-06-01T00:00:00.000Z"),
        ],
    )
    tiers = {w["id"]: w["tier"] for w in sweep_knowledge.propose(tmp_path, None, with_usage=True)}
    assert tiers == {"unused": 0, "young": 1, "earner": 2}


def test_propose_with_usage_ranking_is_deterministic(tmp_path: Path) -> None:
    # tier 0 most-surfaced first, tier 1 oldest first, tier 2 stalest-usage first
    entries = [
        _entry("t0_lo", "DECISION", "surfaced once, unused"),
        _entry("t1_old", "FACT", "never surfaced, old"),
        _entry("t2_stale", "DECISION", "used long ago"),
        _entry("t0_hi", "FACT", "surfaced thrice, unused"),
        _entry("t1_new", "DECISION", "never surfaced, new"),
        _entry("t2_fresh", "FACT", "used recently"),
    ]
    ts_by_id = {"t1_old": "2026-01-01T00:00:00.000Z", "t1_new": "2026-05-01T00:00:00.000Z"}
    for e in entries:
        e["ts"] = ts_by_id.get(e["id"], "2026-03-01T00:00:00.000Z")
    _seed_workspace(tmp_path)
    _write_entries(tmp_path, entries)
    _write_usage(
        tmp_path,
        [
            _usage("t0_lo", False, "2026-06-01T00:00:00.000Z"),
            _usage("t0_hi", False, "2026-06-01T00:00:00.000Z"),
            _usage("t0_hi", False, "2026-06-02T00:00:00.000Z"),
            _usage("t0_hi", False, "2026-06-03T00:00:00.000Z"),
            _usage("t2_stale", True, "2026-04-01T00:00:00.000Z"),
            _usage("t2_fresh", True, "2026-06-05T00:00:00.000Z"),
        ],
    )
    ids = [w["id"] for w in sweep_knowledge.propose(tmp_path, None, with_usage=True)]
    assert ids == ["t0_hi", "t0_lo", "t1_old", "t1_new", "t2_stale", "t2_fresh"]


def test_propose_without_flag_is_legacy_shape_and_order(tmp_path: Path) -> None:
    # The verb-evolve §curate contract: {id, ticket, ts, type, body}, file order.
    _seed_workspace(tmp_path)
    _write_entries(
        tmp_path,
        [_entry("b1", "FACT", "second-ranked by usage"), _entry("a1", "DECISION", "first")],
    )
    _write_usage(tmp_path, [_usage("b1", False, "2026-06-01T00:00:00.000Z")])
    worklist = sweep_knowledge.propose(tmp_path, ["DECISION", "FACT"])
    assert [w["id"] for w in worklist] == ["b1", "a1"]
    assert all(set(w.keys()) == {"id", "ticket", "ts", "type", "body"} for w in worklist)


def test_propose_type_all_widens_and_default_unchanged(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    _write_entries(
        tmp_path,
        [
            _entry("d1", "DECISION", "d"),
            _entry("f1", "FACT", "f"),
            _entry("l1", "LEARNED", "l"),
            _entry("p1", "PATTERN", "p"),
        ],
    )
    all_ids = {w["id"] for w in sweep_knowledge.propose(tmp_path, None)}
    assert all_ids == {"d1", "f1", "l1", "p1"}
    default_ids = {
        w["id"] for w in sweep_knowledge.propose(tmp_path, list(sweep_knowledge.DEFAULT_TYPES))
    }
    assert default_ids == {"d1", "f1"}


def test_cluster_types_none_empty_sidecar(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    _write_entries(tmp_path, [_entry("a1", "DECISION", "x")])
    assert sweep_knowledge.cluster(tmp_path, None) == []


def test_parse_types_all_and_csv() -> None:
    assert sweep_knowledge._parse_types("all") is None
    assert sweep_knowledge._parse_types(" all ") is None
    assert sweep_knowledge._parse_types("DECISION, FACT") == ["DECISION", "FACT"]


def test_cli_propose_type_all_with_usage(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Locks the exact argparse surface command-memory.md names.
    _seed_workspace(tmp_path)
    _write_entries(tmp_path, [_entry("l1", "LEARNED", "x")])
    _write_usage(tmp_path, [_usage("l1", False, "2026-06-01T00:00:00.000Z")])
    rc = sweep_knowledge.cli_main(
        ["propose", "--type", "all", "--with-usage", "--workspace-root", str(tmp_path)]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out[0]["id"] == "l1"
    assert out[0]["tier"] == 0
