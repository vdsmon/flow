"""Tests for metric.py corpus-health (knowledge.jsonl live-vs-superseded health).

Seeds a real workspace (`.flow/workspace.toml` + namespace dir) and writes
`.flow/<namespace>/knowledge.jsonl` lines matching the recall entry schema
(id, ts, type, supersedes), one JSON object per line. The supersession window is
driven by explicit since/until so the math is deterministic.
"""

from __future__ import annotations

import json
from pathlib import Path

import metric


def _seed_workspace(root: Path, namespace: str = "demo") -> None:
    flow = root / ".flow"
    (flow / namespace).mkdir(parents=True, exist_ok=True)
    (flow / ".initialized").write_text("", encoding="utf-8")
    (flow / "workspace.toml").write_text(
        f'[tracker]\nbackend = "jira"\n\n[memory]\nnamespace = "{namespace}"\n',
        encoding="utf-8",
    )


def _write_knowledge(
    root: Path,
    entries: list[dict],
    *,
    namespace: str = "demo",
) -> Path:
    kdir = root / ".flow" / namespace
    kdir.mkdir(parents=True, exist_ok=True)
    path = kdir / "knowledge.jsonl"
    lines = [json.dumps(e, sort_keys=True) for e in entries]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


SINCE = "2026-06-01T00:00:00Z"
UNTIL = "2026-06-08T00:00:00Z"
NOW = "2026-06-19T00:00:00Z"


def _compute(root: Path, namespace: str = "demo") -> dict:
    return metric.compute_corpus_health(
        root, namespace, since_iso=SINCE, until_iso=UNTIL, now_iso=NOW
    )


def test_live_vs_superseded_with_tombstone(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    _write_knowledge(
        tmp_path,
        [
            {"id": "a", "ts": "2026-05-01T00:00:00Z", "type": "MEMORY"},
            {"id": "b", "ts": "2026-06-02T00:00:00Z", "type": "MEMORY", "supersedes": "a"},
            {"id": "c", "ts": "2026-06-03T00:00:00Z", "type": "MEMORY"},
        ],
    )
    result = _compute(tmp_path)
    assert result["total_entries"] == 3
    assert result["superseded_entries"] == 1  # a is named by b.supersedes
    assert result["live_entries"] == 2
    assert result["supersession_rate"] == round(1 / 3, 4)


def test_supersedes_in_window_half_open(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    _write_knowledge(
        tmp_path,
        [
            {"id": "old", "ts": "2026-05-01T00:00:00Z", "type": "MEMORY"},
            # tombstone exactly at since -> counted
            {"id": "b", "ts": SINCE, "type": "MEMORY", "supersedes": "old"},
            # tombstone before window -> excluded
            {"id": "p", "ts": "2026-05-31T23:59:59Z", "type": "MEMORY", "supersedes": "q"},
            # tombstone exactly at until -> excluded (half-open)
            {"id": "u", "ts": UNTIL, "type": "MEMORY", "supersedes": "r"},
        ],
    )
    result = _compute(tmp_path)
    assert result["supersedes_in_window"] == 1


def test_oldest_live_decision(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    _write_knowledge(
        tmp_path,
        [
            {"id": "d1", "ts": "2026-06-10T00:00:00Z", "type": "DECISION"},
            {"id": "d2", "ts": "2026-06-04T00:00:00Z", "type": "DECISION"},
            {"id": "m", "ts": "2026-06-01T00:00:00Z", "type": "MEMORY"},
        ],
    )
    result = _compute(tmp_path)
    assert result["decisions_total"] == 2
    assert result["decisions_live"] == 2
    old = result["oldest_live_decision"]
    assert old is not None
    assert old["id"] == "d2"
    assert old["ts"] == "2026-06-04T00:00:00Z"
    # NOW is 2026-06-19, d2 ts is 2026-06-04 -> 15 days
    assert old["age_days"] == 15.0


def test_no_live_decisions_null(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    _write_knowledge(
        tmp_path,
        [
            {"id": "m1", "ts": "2026-06-01T00:00:00Z", "type": "MEMORY"},
            {"id": "m2", "ts": "2026-06-02T00:00:00Z", "type": "MEMORY"},
        ],
    )
    result = _compute(tmp_path)
    assert result["decisions_total"] == 0
    assert result["decisions_live"] == 0
    assert result["oldest_live_decision"] is None


def test_superseded_decision_excluded(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    _write_knowledge(
        tmp_path,
        [
            {"id": "old_dec", "ts": "2026-06-01T00:00:00Z", "type": "DECISION"},
            {
                "id": "new_dec",
                "ts": "2026-06-05T00:00:00Z",
                "type": "DECISION",
                "supersedes": "old_dec",
            },
        ],
    )
    result = _compute(tmp_path)
    assert result["decisions_total"] == 2
    assert result["decisions_live"] == 1  # old_dec is superseded
    assert result["superseded_entries"] == 1
    assert result["live_entries"] == 1
    old = result["oldest_live_decision"]
    assert old is not None
    assert old["id"] == "new_dec"  # the only live decision


def test_missing_file_zeros(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    result = _compute(tmp_path)
    assert result["total_entries"] == 0
    assert result["live_entries"] == 0
    assert result["superseded_entries"] == 0
    assert result["supersession_rate"] == 0.0
    assert result["supersedes_in_window"] == 0
    assert result["decisions_total"] == 0
    assert result["decisions_live"] == 0
    assert result["oldest_live_decision"] is None


def test_empty_file_zeros(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    (tmp_path / ".flow" / "demo" / "knowledge.jsonl").write_text("", encoding="utf-8")
    result = _compute(tmp_path)
    assert result["total_entries"] == 0
    assert result["supersession_rate"] == 0.0


def test_malformed_line_quarantined(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    kpath = tmp_path / ".flow" / "demo" / "knowledge.jsonl"
    good = json.dumps({"id": "a", "ts": "2026-06-02T00:00:00Z", "type": "MEMORY"}, sort_keys=True)
    kpath.write_text(good + "\n{not json\n" + good + "\n", encoding="utf-8")
    before = kpath.read_text(encoding="utf-8")
    result = _compute(tmp_path)
    assert result["total_entries"] == 2
    sidecars = list((tmp_path / ".flow" / "demo").glob("knowledge.jsonl.quarantine.*"))
    assert sidecars, "expected a quarantine sidecar"
    assert kpath.read_text(encoding="utf-8") == before


def test_cli_happy_prints_json(tmp_path: Path, capsys) -> None:
    _seed_workspace(tmp_path)
    _write_knowledge(
        tmp_path,
        [
            {"id": "a", "ts": "2026-05-01T00:00:00Z", "type": "DECISION"},
            {"id": "b", "ts": "2026-06-02T00:00:00Z", "type": "MEMORY", "supersedes": "a"},
        ],
    )
    rc = metric.cli_main(
        [
            "corpus-health",
            "--namespace",
            "demo",
            "--workspace-root",
            str(tmp_path),
            "--since",
            "2026-06-01",
            "--until",
            "2026-06-08",
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["total_entries"] == 2
    assert payload["superseded_entries"] == 1
    assert payload["live_entries"] == 1
    assert payload["supersedes_in_window"] == 1
    assert payload["since"] == "2026-06-01T00:00:00Z"
    assert payload["until"] == "2026-06-08T00:00:00Z"
    assert payload["resolved_workspace_root"] == str(tmp_path.resolve())


def test_cli_namespace_required(tmp_path: Path, capsys) -> None:
    rc = metric.cli_main(["corpus-health", "--workspace-root", str(tmp_path)])
    assert rc == 1
    assert "namespace" in capsys.readouterr().err


def test_cli_no_flow_dir(tmp_path: Path, capsys) -> None:
    rc = metric.cli_main(
        ["corpus-health", "--namespace", "demo", "--workspace-root", str(tmp_path)]
    )
    assert rc == 1
    assert "no .flow" in capsys.readouterr().err


def test_passthrough_from_recall(tmp_path: Path, capsys) -> None:
    import recall

    _seed_workspace(tmp_path)
    _write_knowledge(
        tmp_path,
        [
            {"id": "a", "ts": "2026-06-02T00:00:00Z", "type": "DECISION"},
        ],
    )
    rc = recall.cli_main(
        [
            "--metric",
            "corpus-health",
            "--namespace",
            "demo",
            "--workspace-root",
            str(tmp_path),
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["total_entries"] == 1
    assert payload["decisions_total"] == 1
    assert payload["decisions_live"] == 1
