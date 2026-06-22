"""Tests for recall_usage.py (usage + miss records) and metric.recall-hit-rate."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import _memory_paths
import memory_embed
import metric
import recall_usage
import state


def _stub_embedder_cmd(tmp_path: Path) -> str:
    """A deterministic 4-dim fake embedder, same contract as the real one. Two
    texts with the same word multiset embed to the same vector (cosine 1.0)."""
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
    return f"{sys.executable} {stub}"


def _seed_workspace(
    root: Path,
    *,
    semantic: bool,
    embedder: str = "",
    model: str = "stub-model",
    initialized: bool = False,
) -> None:
    flow = root / ".flow"
    flow.mkdir(parents=True, exist_ok=True)
    toml = (
        '[tracker]\nbackend = "jira"\n[tracker.jira]\ncloud_id = "x"\nproject_key = "FT"\n\n'
        '[memory]\nnamespace = "demo"\n\n'
    )
    if semantic:
        toml += (
            "[memory.semantic]\nenabled = true\n"
            f'model = "{model}"\nthreshold = 0.0\nembedder = "{embedder}"\n'
        )
    (flow / "workspace.toml").write_text(toml, encoding="utf-8")
    if initialized:
        (flow / ".initialized").write_text("", encoding="utf-8")


def _make_entry(id_: str, body: str, ts: str, ticket: str) -> dict:
    return {
        "id": id_,
        "ts": ts,
        "type": "LEARNED",
        "namespace": "demo",
        "branch": "main",
        "ticket": ticket,
        "body": body,
    }


def _write_knowledge(root: Path, entries: list[dict]) -> Path:
    kpath = _memory_paths.knowledge_path(root, "demo")
    kpath.parent.mkdir(parents=True, exist_ok=True)
    with kpath.open("w", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e, sort_keys=True) + "\n")
    return kpath


def _ticket_dir(root: Path, ticket: str) -> Path:
    return root / ".flow" / "runs" / ticket


def _write_state(root: Path, ticket: str, run_id: str, started_at: str) -> Path:
    td = _ticket_dir(root, ticket)
    td.mkdir(parents=True, exist_ok=True)
    (td / "state.json").write_text(
        json.dumps(
            {
                "schema_version": state.SCHEMA_VERSION,
                "ticket": ticket,
                "run_id": run_id,
                "backend": "jira",
                "started_at": started_at,
                "stages": {},
            }
        ),
        encoding="utf-8",
    )
    return td


def _write_recall_log(root: Path, ticket: str, returned_ids: list[str]) -> None:
    td = _ticket_dir(root, ticket)
    td.mkdir(parents=True, exist_ok=True)
    (td / "recall-log.jsonl").write_text(
        json.dumps({"returned_ids": returned_ids}) + "\n", encoding="utf-8"
    )


def _read_usage(root: Path) -> list[dict]:
    path = recall_usage.recall_usage_path(root, "demo")
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


# ─── record_usage ────────────────────────────────────────────────────────────


def test_record_usage_marks_used_from_recall_log(tmp_path: Path) -> None:
    _seed_workspace(tmp_path, semantic=False)
    _write_state(tmp_path, "FT-2", "run-1", "2026-02-01T00:00:00Z")
    _write_recall_log(tmp_path, "FT-2", ["a" * 16, "b" * 16, "c" * 16])

    written = recall_usage.record_usage(
        tmp_path,
        ticket="FT-2",
        ticket_dir=_ticket_dir(tmp_path, "FT-2"),
        used_ids=["a" * 16, "c" * 16],
    )
    assert len(written) == 3
    by_id = {r["recalled_id"]: r["used"] for r in written}
    assert by_id == {"a" * 16: True, "b" * 16: False, "c" * 16: True}
    assert all(r["run_id"] == "run-1" and r["kind"] == "usage" for r in written)


def test_record_usage_dedups_on_rerun(tmp_path: Path) -> None:
    _seed_workspace(tmp_path, semantic=False)
    _write_state(tmp_path, "FT-2", "run-1", "2026-02-01T00:00:00Z")
    _write_recall_log(tmp_path, "FT-2", ["a" * 16])

    first = recall_usage.record_usage(
        tmp_path, ticket="FT-2", ticket_dir=_ticket_dir(tmp_path, "FT-2"), used_ids=["a" * 16]
    )
    second = recall_usage.record_usage(
        tmp_path, ticket="FT-2", ticket_dir=_ticket_dir(tmp_path, "FT-2"), used_ids=["a" * 16]
    )
    assert len(first) == 1
    assert second == []
    assert len(_read_usage(tmp_path)) == 1


def test_record_usage_no_recall_log_writes_nothing(tmp_path: Path) -> None:
    _seed_workspace(tmp_path, semantic=False)
    _write_state(tmp_path, "FT-2", "run-1", "2026-02-01T00:00:00Z")
    written = recall_usage.record_usage(
        tmp_path, ticket="FT-2", ticket_dir=_ticket_dir(tmp_path, "FT-2"), used_ids=[]
    )
    assert written == []


# ─── detect_misses ───────────────────────────────────────────────────────────


def _seed_miss_scenario(
    tmp_path: Path, *, returned_ids: list[str], model: str = "stub-model"
) -> Path:
    """Pre-existing entry P (in the index) + a near-dup new entry N (NOT in the
    index — proves detect embeds fresh). Returns the run's ticket-dir."""
    embedder = _stub_embedder_cmd(tmp_path)
    _seed_workspace(tmp_path, semantic=True, embedder=embedder, model=model)
    # P only, then reindex → the sidecar holds P but not N.
    _write_knowledge(
        tmp_path, [_make_entry("p" * 16, "alpha beta gamma delta", "2026-01-01T00:00:00Z", "FT-1")]
    )
    memory_embed.reindex(tmp_path, "demo", model="stub-model", embedder=embedder)
    # now add N (this run's near-dup append), unindexed.
    _write_knowledge(
        tmp_path,
        [
            _make_entry("p" * 16, "alpha beta gamma delta", "2026-01-01T00:00:00Z", "FT-1"),
            _make_entry("n" * 16, "alpha beta gamma delta", "2026-03-01T00:00:00Z", "FT-2"),
        ],
    )
    _write_state(tmp_path, "FT-2", "run-1", "2026-02-01T00:00:00Z")
    _write_recall_log(tmp_path, "FT-2", returned_ids)
    return _ticket_dir(tmp_path, "FT-2")


def test_detect_misses_flags_unrecalled_near_dup(tmp_path: Path) -> None:
    td = _seed_miss_scenario(tmp_path, returned_ids=[])
    misses = recall_usage.detect_misses(tmp_path, ticket="FT-2", ticket_dir=td)
    assert len(misses) == 1
    m = misses[0]
    assert m["kind"] == "miss" and m["type"] == "RECALL_MISS"
    assert m["relearned_id"] == "n" * 16
    assert m["missed_id"] == "p" * 16
    assert m["similarity"] >= 0.90


def test_detect_misses_silent_when_near_dup_was_recalled(tmp_path: Path) -> None:
    td = _seed_miss_scenario(tmp_path, returned_ids=["p" * 16])
    misses = recall_usage.detect_misses(tmp_path, ticket="FT-2", ticket_dir=td)
    assert misses == []


def test_detect_misses_noop_when_semantic_off(tmp_path: Path) -> None:
    _seed_workspace(tmp_path, semantic=False)
    _write_knowledge(
        tmp_path,
        [
            _make_entry("p" * 16, "alpha beta", "2026-01-01T00:00:00Z", "FT-1"),
            _make_entry("n" * 16, "alpha beta", "2026-03-01T00:00:00Z", "FT-2"),
        ],
    )
    _write_state(tmp_path, "FT-2", "run-1", "2026-02-01T00:00:00Z")
    _write_recall_log(tmp_path, "FT-2", [])
    assert (
        recall_usage.detect_misses(
            tmp_path, ticket="FT-2", ticket_dir=_ticket_dir(tmp_path, "FT-2")
        )
        == []
    )


def test_detect_misses_noop_on_model_mismatch(tmp_path: Path) -> None:
    # config model "other-model" but the sidecar was built with "stub-model";
    # comparing a fresh "other" vector to a stub-era index is garbage → no-op.
    td = _seed_miss_scenario(tmp_path, returned_ids=[], model="other-model")
    assert recall_usage.detect_misses(tmp_path, ticket="FT-2", ticket_dir=td) == []


def test_detect_misses_dedups_on_rerun(tmp_path: Path) -> None:
    td = _seed_miss_scenario(tmp_path, returned_ids=[])
    first = recall_usage.detect_misses(tmp_path, ticket="FT-2", ticket_dir=td)
    second = recall_usage.detect_misses(tmp_path, ticket="FT-2", ticket_dir=td)
    assert len(first) == 1
    assert second == []


# ─── metric: recall-hit-rate ─────────────────────────────────────────────────


def _write_usage_records(tmp_path: Path, records: list[dict]) -> None:
    path = recall_usage.recall_usage_path(tmp_path, "demo")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, sort_keys=True) + "\n")


def test_recall_hit_rate_precision_and_misses(tmp_path: Path) -> None:
    _seed_workspace(tmp_path, semantic=False)
    _write_usage_records(
        tmp_path,
        [
            {
                "kind": "usage",
                "run_id": "r1",
                "ticket": "FT-1",
                "recalled_id": "a",
                "used": True,
                "ts": "2026-03-02T00:00:00Z",
            },
            {
                "kind": "usage",
                "run_id": "r1",
                "ticket": "FT-1",
                "recalled_id": "b",
                "used": False,
                "ts": "2026-03-02T00:00:00Z",
            },
            {
                "kind": "usage",
                "run_id": "r2",
                "ticket": "FT-2",
                "recalled_id": "c",
                "used": True,
                "ts": "2026-03-03T00:00:00Z",
            },
            {
                "kind": "miss",
                "type": "RECALL_MISS",
                "run_id": "r2",
                "ticket": "FT-2",
                "relearned_id": "n",
                "missed_id": "m",
                "similarity": 0.97,
                "ts": "2026-03-03T00:00:00Z",
            },
        ],
    )
    result = metric.compute_recall_hit_rate(
        tmp_path, "demo", since_iso="2026-03-01T00:00:00Z", until_iso="2026-03-10T00:00:00Z"
    )
    assert result["surfaced"] == 3
    assert result["used"] == 2
    assert result["hit_rate"] == round(2 / 3, 4)
    assert result["misses"] == 1
    assert result["runs"] == 2


def test_recall_hit_rate_windows_out_of_range(tmp_path: Path) -> None:
    _seed_workspace(tmp_path, semantic=False)
    _write_usage_records(
        tmp_path,
        [
            {
                "kind": "usage",
                "run_id": "r1",
                "ticket": "FT-1",
                "recalled_id": "a",
                "used": True,
                "ts": "2026-01-01T00:00:00Z",
            },
            {
                "kind": "usage",
                "run_id": "r2",
                "ticket": "FT-2",
                "recalled_id": "b",
                "used": True,
                "ts": "2026-03-05T00:00:00Z",
            },
        ],
    )
    result = metric.compute_recall_hit_rate(
        tmp_path, "demo", since_iso="2026-03-01T00:00:00Z", until_iso="2026-03-10T00:00:00Z"
    )
    assert result["surfaced"] == 1
    assert result["used"] == 1
    assert result["hit_rate"] == 1.0


def test_recall_hit_rate_empty_is_zero(tmp_path: Path) -> None:
    _seed_workspace(tmp_path, semantic=False)
    result = metric.compute_recall_hit_rate(
        tmp_path, "demo", since_iso="2026-03-01T00:00:00Z", until_iso="2026-03-10T00:00:00Z"
    )
    assert result == {
        "since": "2026-03-01T00:00:00Z",
        "until": "2026-03-10T00:00:00Z",
        "surfaced": 0,
        "used": 0,
        "hit_rate": 0.0,
        "misses": 0,
        "runs": 0,
    }


def test_recall_hit_rate_cli_via_metric(tmp_path: Path, capsys) -> None:
    _seed_workspace(tmp_path, semantic=False, initialized=True)
    _write_usage_records(
        tmp_path,
        [
            {
                "kind": "usage",
                "run_id": "r1",
                "ticket": "FT-1",
                "recalled_id": "a",
                "used": True,
                "ts": "2026-03-02T00:00:00Z",
            },
        ],
    )
    rc = metric.cli_main(
        [
            "recall-hit-rate",
            "--namespace",
            "demo",
            "--workspace-root",
            str(tmp_path),
            "--since",
            "2026-03-01",
            "--until",
            "2026-03-10",
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["surfaced"] == 1 and payload["hit_rate"] == 1.0


def test_cli_main_record_usage_no_state_returns_3(tmp_path: Path) -> None:
    _seed_workspace(tmp_path, semantic=False)
    rc = recall_usage.cli_main(
        [
            "record-usage",
            "--ticket",
            "FT-99",
            "--ticket-dir",
            str(_ticket_dir(tmp_path, "FT-99")),
            "--workspace-root",
            str(tmp_path),
        ]
    )
    assert rc == 3
