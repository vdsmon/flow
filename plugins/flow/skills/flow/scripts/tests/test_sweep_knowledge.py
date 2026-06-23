"""Contract tests for sweep_knowledge.py — maintainer-gated curation over knowledge.jsonl."""

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
        f'[tracker]\nbackend = "jira"\n[tracker.jira]\ncloud_id = "x"\nproject_key = "FT"\n\n[memory]\nnamespace = "{namespace}"\n',
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
