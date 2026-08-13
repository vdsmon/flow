"""Tests for metric.py friction-per-run (friction events per distinct run).

Seeds a real workspace (`.flow/workspace.toml` + namespace dir) and writes
`.flow/<namespace>/friction.jsonl` lines matching flow_friction.append's key set
(id, ts, run_id, ticket, stage, type, severity, body), one JSON object per line.
Windowing is driven by explicit since/until so the math is deterministic.
"""

from __future__ import annotations

import json
from pathlib import Path

import metric
from tests.wsfactory import make_workspace, memory, tracker


def _seed_workspace(root: Path, namespace: str = "demo") -> None:
    make_workspace(
        root,
        tracker("jira", subtable=False),
        memory(namespace),
        initialized=True,
        namespace_dir=namespace,
    )


def _write_friction(
    root: Path,
    entries: list[dict],
    *,
    namespace: str = "demo",
) -> Path:
    fdir = root / ".flow" / namespace
    fdir.mkdir(parents=True, exist_ok=True)
    path = fdir / "friction.jsonl"
    lines = [json.dumps(e, sort_keys=True) for e in entries]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _entry(
    *,
    ts: str | None,
    run_id: object = "run-1",
    type_: str = "RETRY",
    severity: str = "major",
    ticket: str = "FT-1",
    stage: str = "implement",
) -> dict:
    e: dict = {
        "id": "deadbeef",
        "run_id": run_id,
        "ticket": ticket,
        "stage": stage,
        "type": type_,
        "severity": severity,
        "body": "snag",
    }
    if ts is not None:
        e["ts"] = ts
    return e


SINCE = "2026-06-01T00:00:00Z"
UNTIL = "2026-06-08T00:00:00Z"


def _compute(root: Path, namespace: str = "demo") -> dict:
    return metric.compute_friction_per_run(root, namespace, since_iso=SINCE, until_iso=UNTIL)


def test_events_grouped_by_run_id_arithmetic(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    _write_friction(
        tmp_path,
        [
            _entry(ts="2026-06-02T10:00:00.000Z", run_id="run-1"),
            _entry(ts="2026-06-03T10:00:00.000Z", run_id="run-1"),
            _entry(ts="2026-06-04T10:00:00.000Z", run_id="run-2"),
            _entry(ts="2026-06-05T10:00:00.000Z", run_id="run-2"),
        ],
    )
    result = _compute(tmp_path)
    assert result["total_events"] == 4
    assert result["runs"] == 2
    assert result["events_per_run"] == 2.0


def test_millisecond_ts_included(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    _write_friction(tmp_path, [_entry(ts="2026-06-05T10:00:00.123Z")])
    result = _compute(tmp_path)
    assert result["total_events"] == 1
    assert result["runs"] == 1


def test_window_half_open(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    _write_friction(
        tmp_path,
        [
            _entry(ts="2026-05-31T23:59:59.000Z", run_id="before"),
            _entry(ts=SINCE, run_id="at-since"),
            _entry(ts=UNTIL, run_id="at-until"),
        ],
    )
    result = _compute(tmp_path)
    assert result["total_events"] == 1
    assert result["runs"] == 1


def test_missing_file_zero(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    result = _compute(tmp_path)
    assert result["total_events"] == 0
    assert result["runs"] == 0
    assert result["events_per_run"] == 0
    assert result["by_type"] == {}
    assert result["by_severity"] == {}


def test_malformed_line_quarantined(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    fpath = tmp_path / ".flow" / "demo" / "friction.jsonl"
    good = json.dumps(_entry(ts="2026-06-02T10:00:00.000Z"), sort_keys=True)
    fpath.write_text(good + "\n{not json\n" + good + "\n", encoding="utf-8")
    before = fpath.read_text(encoding="utf-8")
    result = _compute(tmp_path)
    assert result["total_events"] == 2
    sidecars = list((tmp_path / ".flow" / "demo").glob("friction.jsonl.quarantine.*"))
    assert sidecars, "expected a quarantine sidecar"
    assert fpath.read_text(encoding="utf-8") == before


def test_by_type_and_severity_breakdown(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    _write_friction(
        tmp_path,
        [
            _entry(ts="2026-06-02T10:00:00.000Z", type_="RETRY", severity="major"),
            _entry(ts="2026-06-02T11:00:00.000Z", type_="RETRY", severity="minor"),
            _entry(ts="2026-06-02T12:00:00.000Z", type_="DRIFT", severity="major"),
        ],
    )
    result = _compute(tmp_path)
    assert result["by_type"] == {"RETRY": 2, "DRIFT": 1}
    assert result["by_severity"] == {"major": 2, "minor": 1}
    assert sum(result["by_type"].values()) == result["total_events"]
    assert sum(result["by_severity"].values()) == result["total_events"]


def test_unparseable_or_missing_ts_skipped(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    _write_friction(
        tmp_path,
        [
            _entry(ts="2026-06-02T10:00:00.000Z", run_id="good"),
            _entry(ts=None, run_id="no-ts"),
            _entry(ts="not-a-timestamp", run_id="junk-ts"),
        ],
    )
    result = _compute(tmp_path)
    assert result["total_events"] == 1
    assert result["runs"] == 1


def test_non_string_run_id_not_counted_as_run(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    _write_friction(
        tmp_path,
        [
            _entry(ts="2026-06-02T10:00:00.000Z", run_id="run-1"),
            _entry(ts="2026-06-02T11:00:00.000Z", run_id=None),
        ],
    )
    result = _compute(tmp_path)
    assert result["total_events"] == 2
    assert result["runs"] == 1
    assert result["events_per_run"] == 2.0


def test_cli_happy_prints_json(tmp_path: Path, capsys) -> None:
    _seed_workspace(tmp_path)
    _write_friction(
        tmp_path,
        [
            _entry(ts="2026-06-02T10:00:00.000Z", run_id="run-1"),
            _entry(ts="2026-06-03T10:00:00.000Z", run_id="run-2"),
        ],
    )
    rc = metric.cli_main(
        [
            "friction-per-run",
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
    assert payload["total_events"] == 2
    assert payload["runs"] == 2
    assert payload["events_per_run"] == 1.0
    assert payload["since"] == "2026-06-01T00:00:00Z"
    assert payload["until"] == "2026-06-08T00:00:00Z"


def test_cli_namespace_required(tmp_path: Path, capsys) -> None:
    rc = metric.cli_main(["friction-per-run", "--workspace-root", str(tmp_path)])
    assert rc == 1
    assert "namespace" in capsys.readouterr().err


def test_cli_no_flow_dir(tmp_path: Path, capsys) -> None:
    rc = metric.cli_main(
        ["friction-per-run", "--namespace", "demo", "--workspace-root", str(tmp_path)]
    )
    assert rc == 1
    assert "no .flow" in capsys.readouterr().err


def _write_ship_event(root: Path, ticket: str, run_id: str, *, namespace: str = "demo") -> None:
    ship_dir = root / ".flow" / namespace / "ship-events"
    ship_dir.mkdir(parents=True, exist_ok=True)
    (ship_dir / f"{ticket}.json").write_text(
        json.dumps(
            {
                "ticket": ticket,
                "shipped_at": "2026-05-20T10:00:00Z",
                "evidence": {"merged": True},
                "observed_at": "2026-05-20T10:00:00Z",
                "observed_by_run_id": run_id,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def test_clean_runs_count_in_the_denominator(tmp_path):
    # Denominator = runs the window has evidence for, not runs that complained. Counting
    # only complaining runs floors events_per_run at 1.0, so the measure could never show
    # flow getting better (flow-qlwe).
    _seed_workspace(tmp_path)
    _write_friction(
        tmp_path,
        [
            {"id": "a", "ts": "2026-05-20T09:00:00Z", "run_id": "noisy", "type": "DRIFT"},
            {"id": "b", "ts": "2026-05-20T09:30:00Z", "run_id": "noisy", "type": "DRIFT"},
        ],
    )
    _write_ship_event(tmp_path, "FT-1", "noisy")
    _write_ship_event(tmp_path, "FT-2", "clean-1")
    _write_ship_event(tmp_path, "FT-3", "clean-2")

    result = metric.compute_friction_per_run(
        tmp_path, "demo", since_iso="2026-05-14T00:00:00Z", until_iso="2026-05-28T12:00:00Z"
    )
    assert result["total_events"] == 2
    assert result["runs"] == 3
    assert result["runs_with_friction"] == 1
    assert result["events_per_run"] < 1.0


def test_a_run_with_friction_but_no_ship_event_still_counts_once(tmp_path):
    # An open PR has no frozen ship event, so its run is known only through its friction.
    # Union, not replacement: dropping it would hide in-flight cost entirely.
    _seed_workspace(tmp_path)
    _write_friction(
        tmp_path,
        [{"id": "a", "ts": "2026-05-20T09:00:00Z", "run_id": "in-flight", "type": "DRIFT"}],
    )
    _write_ship_event(tmp_path, "FT-1", "in-flight")
    _write_ship_event(tmp_path, "FT-2", "shipped")

    result = metric.compute_friction_per_run(
        tmp_path, "demo", since_iso="2026-05-14T00:00:00Z", until_iso="2026-05-28T12:00:00Z"
    )
    assert result["runs"] == 2
    assert result["events_per_run"] == 0.5


def test_ship_events_outside_the_window_do_not_pad_the_denominator(tmp_path):
    _seed_workspace(tmp_path)
    _write_friction(
        tmp_path,
        [{"id": "a", "ts": "2026-05-20T09:00:00Z", "run_id": "noisy", "type": "DRIFT"}],
    )
    _write_ship_event(tmp_path, "FT-1", "noisy")
    ship_dir = tmp_path / ".flow" / "demo" / "ship-events"
    (ship_dir / "FT-OLD.json").write_text(
        json.dumps(
            {
                "ticket": "FT-OLD",
                "shipped_at": "2026-01-01T10:00:00Z",
                "evidence": {"merged": True},
                "observed_at": "2026-01-01T10:00:00Z",
                "observed_by_run_id": "ancient",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    result = metric.compute_friction_per_run(
        tmp_path, "demo", since_iso="2026-05-14T00:00:00Z", until_iso="2026-05-28T12:00:00Z"
    )
    assert result["runs"] == 1
