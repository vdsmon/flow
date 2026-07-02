"""Contract tests for sweep_knowledge.py cluster / apply-cluster (consolidation lane).

cluster is deterministic and read-only: it groups live, same-type, sidecar-indexed
entries by complete-linkage cosine over hand-written unit vectors (`load_index` is a
pure file read, no embedder subprocess). apply-cluster writes one canonical entry
per confirmed manifest record via `memory_append` with a list-valued `supersedes`.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

import _memory_paths
import memory_embed
import recall
import sweep_knowledge
from _jsonl import iter_jsonl

TAU = 0.9


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


def _write_index(root: Path, vectors: dict[str, list[float]], namespace: str = "demo") -> None:
    path = memory_embed.embed_index_path(root, namespace)
    path.parent.mkdir(parents=True, exist_ok=True)
    dim = len(next(iter(vectors.values()))) if vectors else 0
    lines = [
        json.dumps(
            {"_header": {"model": "stub-model", "dim": dim, "ts": "2026-01-01T00:00:00Z"}},
            sort_keys=True,
        )
    ]
    for eid in sorted(vectors):
        lines.append(json.dumps({"id": eid, "v": vectors[eid]}, sort_keys=True))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _load_all(root: Path, namespace: str = "demo") -> list[dict]:
    kpath = _memory_paths.knowledge_path(root, namespace)
    sidecar = kpath.with_name(f"{kpath.name}.quarantine.test")
    return list(iter_jsonl(kpath, sidecar))


def _entry(
    id_: str, type_: str, body: str, ticket: str = "FT-1", supersedes: str | list | None = None
) -> dict:
    e: dict = {
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


def _unit(angle_deg: float) -> list[float]:
    rad = math.radians(angle_deg)
    return [math.cos(rad), math.sin(rad)]


A = "a" * 16
B = "b" * 16
C = "c" * 16
D = "d" * 16
T = "t" * 16


def _cluster(root: Path, capsys: pytest.CaptureFixture[str], types: str = "DECISION,FACT") -> list:
    rc = sweep_knowledge.cli_main(
        [
            "cluster",
            "--type",
            types,
            "--threshold",
            str(TAU),
            "--workspace-root",
            str(root),
        ]
    )
    assert rc == 0
    return json.loads(capsys.readouterr().out)


# ─── cluster ─────────────────────────────────────────────────────────────────


def test_cluster_two_near_dups_and_one_far(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_workspace(tmp_path)
    _write_entries(
        tmp_path,
        [
            _entry(A, "DECISION", "claim A"),
            _entry(B, "DECISION", "claim B, near A"),
            _entry(C, "DECISION", "claim C, unrelated"),
        ],
    )
    _write_index(tmp_path, {A: _unit(0), B: _unit(5), C: _unit(90)})
    groups = _cluster(tmp_path, capsys, types="DECISION")
    assert len(groups) == 1
    group = groups[0]
    assert group["type"] == "DECISION"
    assert {m["id"] for m in group["members"]} == {A, B}
    assert group["min_cosine"] == pytest.approx(math.cos(math.radians(5)), abs=1e-6)


def test_cluster_cross_type_pair_does_not_cluster(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_workspace(tmp_path)
    _write_entries(
        tmp_path,
        [
            _entry(A, "DECISION", "claim A"),
            _entry(D, "FACT", "claim D, identical vector but different type"),
        ],
    )
    # identical vectors (cosine 1.0) -> would obviously cluster if type were ignored.
    _write_index(tmp_path, {A: _unit(0), D: _unit(0)})
    groups = _cluster(tmp_path, capsys, types="DECISION,FACT")
    assert groups == []


def test_cluster_complete_linkage_rejects_non_transitive_triple(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_workspace(tmp_path)
    _write_entries(
        tmp_path,
        [
            _entry(A, "DECISION", "claim A"),
            _entry(B, "DECISION", "claim B"),
            _entry(C, "DECISION", "claim C"),
        ],
    )
    # A-B and B-C are both close (20deg apart, cos~0.94 >= tau); A-C are 40deg apart
    # (cos~0.77 < tau). Complete linkage must reject the full triple.
    _write_index(tmp_path, {A: _unit(0), B: _unit(20), C: _unit(40)})
    groups = _cluster(tmp_path, capsys, types="DECISION")
    assert len(groups) == 1
    assert {m["id"] for m in groups[0]["members"]} == {A, B}


def test_cluster_missing_sidecar_emits_empty_list(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_workspace(tmp_path)
    _write_entries(tmp_path, [_entry(A, "DECISION", "claim A"), _entry(B, "DECISION", "claim B")])
    groups = _cluster(tmp_path, capsys, types="DECISION")
    assert groups == []


def test_cluster_excludes_superseded_entries(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_workspace(tmp_path)
    _write_entries(
        tmp_path,
        [
            _entry(A, "DECISION", "claim A, now dead"),
            _entry(B, "DECISION", "claim B, near dead A"),
            _entry(T, "DECISION", "tombstone", supersedes=A),
        ],
    )
    # A and B are near-dup (5deg apart); T is far from both. If A were NOT excluded
    # as superseded, {A, B} would cluster. With A excluded only B and T remain, and
    # they are far apart -> no group.
    _write_index(tmp_path, {A: _unit(0), B: _unit(5), T: _unit(90)})
    groups = _cluster(tmp_path, capsys, types="DECISION")
    assert groups == []


# ─── apply-cluster ─────────────────────────────────────────────────────────────


def _apply_cluster(root: Path, manifest_path: Path) -> tuple[int, dict]:
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = sweep_knowledge.cli_main(
            ["apply-cluster", "--manifest", str(manifest_path), "--workspace-root", str(root)]
        )
    return rc, json.loads(buf.getvalue())


def test_apply_cluster_merges_two_members_into_one_survivor(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    _write_entries(
        tmp_path,
        [_entry(A, "DECISION", "claim A"), _entry(B, "DECISION", "claim B, near-dup of A")],
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            [
                {
                    "canonical_body": "merged canonical claim",
                    "canonical_ticket": "flow-consolidated",
                    "member_ids": [A, B],
                }
            ]
        ),
        encoding="utf-8",
    )
    rc, summary = _apply_cluster(tmp_path, manifest)
    assert rc == 0
    assert summary["any_error"] is False
    result = summary["results"][0]
    assert result["result"] == "applied"
    new_id = result["new_id"]
    assert result["merged"] == 2

    all_entries = _load_all(tmp_path)
    canonical = next(e for e in all_entries if e["id"] == new_id)
    assert canonical["supersedes"] == [A, B]
    assert canonical["body"] == "merged canonical claim"
    assert canonical["ticket"] == "flow-consolidated"
    assert canonical["type"] == "DECISION"
    assert canonical["branch"] == "feat/flow-consolidated"

    survivors = recall.filter_superseded(all_entries)
    assert len(survivors) == 1
    assert survivors[0]["id"] == new_id


def test_apply_cluster_idempotent_rerun_appends_nothing(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    _write_entries(
        tmp_path,
        [_entry(A, "DECISION", "claim A"), _entry(B, "DECISION", "claim B")],
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            [
                {
                    "canonical_body": "merged canonical claim",
                    "canonical_ticket": "flow-consolidated",
                    "member_ids": [A, B],
                }
            ]
        ),
        encoding="utf-8",
    )
    rc1, _ = _apply_cluster(tmp_path, manifest)
    assert rc1 == 0
    count_after_first = len(_load_all(tmp_path))

    rc2, summary2 = _apply_cluster(tmp_path, manifest)
    assert rc2 == 0
    assert len(_load_all(tmp_path)) == count_after_first
    assert all(r["result"] == "skipped" for r in summary2["results"])


def test_apply_cluster_unknown_member_id_errors_nonzero(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    _write_entries(tmp_path, [_entry(A, "DECISION", "claim A")])
    before = len(_load_all(tmp_path))
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            [
                {
                    "canonical_body": "merged canonical claim",
                    "canonical_ticket": "flow-consolidated",
                    "member_ids": [A, "ffffffffffffffff"],
                }
            ]
        ),
        encoding="utf-8",
    )
    rc, summary = _apply_cluster(tmp_path, manifest)
    assert rc == 5
    assert summary["any_error"] is True
    assert summary["results"][0]["result"] == "error"
    assert len(_load_all(tmp_path)) == before


def test_apply_cluster_empty_member_ids_errors_no_append(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    _write_entries(tmp_path, [_entry(A, "DECISION", "claim A")])
    before = len(_load_all(tmp_path))
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            [
                {
                    "canonical_body": "canonical of nothing",
                    "canonical_ticket": "flow-empty",
                    "member_ids": [],
                }
            ]
        ),
        encoding="utf-8",
    )
    rc, summary = _apply_cluster(tmp_path, manifest)
    assert rc > 0
    assert summary["any_error"] is True
    assert summary["results"][0]["result"] == "error"
    assert len(_load_all(tmp_path)) == before
