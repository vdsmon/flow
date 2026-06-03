"""Tests for metric.py — tickets-per-week behind the 14-day checkpoint.

Builds real ship-events + state.json on temp dirs (matching the on-disk shapes
observe_ship_event.py and state.py write), with an explicit `now` so window math
is deterministic. Checkpoint mode aggregates across two manifest participants
written to a temp manifest path.
"""

from __future__ import annotations

import json
from pathlib import Path

import metric


def _seed_workspace(root: Path, namespace: str = "demo") -> None:
    flow = root / ".flow"
    (flow / namespace / "ship-events").mkdir(parents=True, exist_ok=True)
    (flow / "workspace.toml").write_text(
        f'[tracker]\nbackend = "jira"\n\n[memory]\nnamespace = "{namespace}"\n',
        encoding="utf-8",
    )


def _write_ship_event(
    root: Path,
    ticket: str,
    *,
    shipped_at: str,
    observed_by_run_id: str = "abcdef0123456789",
    namespace: str = "demo",
    filename: str | None = None,
) -> Path:
    record = {
        "ticket": ticket,
        "shipped_at": shipped_at,
        "evidence": {"merged": True},
        "observed_at": "2026-05-20T10:00:00Z",
        "observed_by_run_id": observed_by_run_id,
    }
    ship_dir = root / ".flow" / namespace / "ship-events"
    ship_dir.mkdir(parents=True, exist_ok=True)
    path = ship_dir / (filename or f"{ticket}.json")
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _write_state(
    root: Path,
    ticket: str,
    *,
    run_id: str,
    reflect_status: str | None = "completed",
) -> Path:
    stages: dict = {
        "implement": {"status": "completed"},
    }
    if reflect_status is not None:
        stages["reflect"] = {"status": reflect_status}
    state = {
        "schema_version": 1,
        "ticket": ticket,
        "run_id": run_id,
        "backend": "jira",
        "started_at": "2026-05-19T09:00:00Z",
        "stages": stages,
    }
    state_dir = root / ".flow" / "runs" / ticket
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "state.json"
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


_NOW = "2026-05-28T12:00:00Z"
_SINCE = "2026-05-14T00:00:00Z"
_UNTIL = "2026-05-28T12:00:00Z"


def _compute(root: Path, namespace: str = "demo") -> dict:
    return metric.compute(root, namespace, since_iso=_SINCE, until_iso=_UNTIL, now_iso=_NOW)


# ─── default_window ──────────────────────────────────────────────────────────


def test_default_window_floors_since_to_midnight() -> None:
    since, until = metric.default_window(_NOW)
    assert since == "2026-05-14T00:00:00Z"
    assert until == _NOW


def test_default_window_bad_now_raises() -> None:
    import pytest

    with pytest.raises(ValueError, match="now"):
        metric.default_window("not-a-date")


# ─── load_ship_events ────────────────────────────────────────────────────────


def test_load_ship_events_skips_dupe_corrupt_intent(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    _write_ship_event(tmp_path, "FT-1", shipped_at="2026-05-20T10:00:00Z")
    # dupe / corrupt / intent siblings must be ignored by name.
    _write_ship_event(
        tmp_path, "FT-1", shipped_at="2026-05-20T10:00:00Z", filename="FT-1.json.dupe.1.json"
    )
    _write_ship_event(
        tmp_path, "FT-2", shipped_at="2026-05-20T10:00:00Z", filename="FT-2.json.corrupt.x.json"
    )
    _write_ship_event(
        tmp_path,
        "FT-3",
        shipped_at="2026-05-20T10:00:00Z",
        filename="FT-3.json.quarantine-intent.20260520T100000Z.json",
    )
    events = metric.load_ship_events(tmp_path, "demo")
    assert [e["ticket"] for e in events] == ["FT-1"]


def test_load_ship_events_quarantines_malformed(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    _write_ship_event(tmp_path, "FT-1", shipped_at="2026-05-20T10:00:00Z")
    ship_dir = tmp_path / ".flow" / "demo" / "ship-events"
    (ship_dir / "FT-bad.json").write_text("{not json", encoding="utf-8")
    (ship_dir / "FT-noship.json").write_text(json.dumps({"ticket": "FT-noship"}), encoding="utf-8")
    events = metric.load_ship_events(tmp_path, "demo")
    assert [e["ticket"] for e in events] == ["FT-1"]
    quarantine = tmp_path / ".flow" / "demo" / "ship-events.quarantine"
    assert quarantine.exists()
    assert len(quarantine.read_text(encoding="utf-8").splitlines()) == 2


def test_load_ship_events_no_dir(tmp_path: Path) -> None:
    assert metric.load_ship_events(tmp_path, "demo") == []


# ─── classify_attribution ────────────────────────────────────────────────────


def test_classify_via_flow_when_state_matches(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    _write_ship_event(
        tmp_path, "FT-1", shipped_at="2026-05-20T10:00:00Z", observed_by_run_id="run-aaa"
    )
    _write_state(tmp_path, "FT-1", run_id="run-aaa", reflect_status="completed")
    event = metric.load_ship_events(tmp_path, "demo")[0]
    assert metric.classify_attribution(tmp_path, event) == metric.ATTR_VIA_FLOW


def test_classify_not_attributed_no_state(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    _write_ship_event(tmp_path, "FT-1", shipped_at="2026-05-20T10:00:00Z")
    event = metric.load_ship_events(tmp_path, "demo")[0]
    assert metric.classify_attribution(tmp_path, event) == metric.ATTR_NOT_ATTRIBUTED


def test_classify_not_attributed_run_id_mismatch(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    _write_ship_event(
        tmp_path, "FT-1", shipped_at="2026-05-20T10:00:00Z", observed_by_run_id="run-aaa"
    )
    _write_state(tmp_path, "FT-1", run_id="run-zzz", reflect_status="completed")
    event = metric.load_ship_events(tmp_path, "demo")[0]
    assert metric.classify_attribution(tmp_path, event) == metric.ATTR_NOT_ATTRIBUTED


def test_classify_not_attributed_reflect_not_completed(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    _write_ship_event(
        tmp_path, "FT-1", shipped_at="2026-05-20T10:00:00Z", observed_by_run_id="run-aaa"
    )
    _write_state(tmp_path, "FT-1", run_id="run-aaa", reflect_status="in_progress")
    event = metric.load_ship_events(tmp_path, "demo")[0]
    assert metric.classify_attribution(tmp_path, event) == metric.ATTR_NOT_ATTRIBUTED


def test_classify_not_attributed_no_reflect_stage(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    _write_ship_event(
        tmp_path, "FT-1", shipped_at="2026-05-20T10:00:00Z", observed_by_run_id="run-aaa"
    )
    _write_state(tmp_path, "FT-1", run_id="run-aaa", reflect_status=None)
    event = metric.load_ship_events(tmp_path, "demo")[0]
    assert metric.classify_attribution(tmp_path, event) == metric.ATTR_NOT_ATTRIBUTED


def test_classify_not_attributed_corrupt_state(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    _write_ship_event(tmp_path, "FT-1", shipped_at="2026-05-20T10:00:00Z")
    state_dir = tmp_path / ".flow" / "runs" / "FT-1"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "state.json").write_text("{broken", encoding="utf-8")
    event = metric.load_ship_events(tmp_path, "demo")[0]
    assert metric.classify_attribution(tmp_path, event) == metric.ATTR_NOT_ATTRIBUTED


# ─── compute: window + attribution mix ───────────────────────────────────────


def test_compute_counts_two_attributions(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    # FT-1: reflect completed + matching run id -> shipped_via_flow
    _write_ship_event(
        tmp_path, "FT-1", shipped_at="2026-05-20T10:00:00Z", observed_by_run_id="run-1"
    )
    _write_state(tmp_path, "FT-1", run_id="run-1", reflect_status="completed")
    # FT-2: no state -> shipped_backend_not_attributed
    _write_ship_event(tmp_path, "FT-2", shipped_at="2026-05-21T11:00:00Z")
    result = _compute(tmp_path)
    assert result["shipped"] == 2
    assert result[metric.ATTR_VIA_FLOW] == 1
    assert result[metric.ATTR_NOT_ATTRIBUTED] == 1
    by_ticket = {t["ticket"]: t["attribution"] for t in result["tickets"]}
    assert by_ticket["FT-1"] == metric.ATTR_VIA_FLOW
    assert by_ticket["FT-2"] == metric.ATTR_NOT_ATTRIBUTED


def test_compute_excludes_out_of_window(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    # in window
    _write_ship_event(tmp_path, "FT-1", shipped_at="2026-05-20T10:00:00Z")
    # before since (exclusive lower bound is inclusive at 00:00; this is earlier)
    _write_ship_event(tmp_path, "FT-OLD", shipped_at="2026-05-13T23:59:59Z")
    # at/after until -> excluded (half-open upper bound)
    _write_ship_event(tmp_path, "FT-FUTURE", shipped_at="2026-05-28T12:00:00Z")
    result = _compute(tmp_path)
    assert result["shipped"] == 1
    assert [t["ticket"] for t in result["tickets"]] == ["FT-1"]


def test_compute_window_boundaries_half_open(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    # exactly at since -> included
    _write_ship_event(tmp_path, "FT-SINCE", shipped_at=_SINCE)
    # exactly at until -> excluded
    _write_ship_event(tmp_path, "FT-UNTIL", shipped_at=_UNTIL)
    result = _compute(tmp_path)
    assert [t["ticket"] for t in result["tickets"]] == ["FT-SINCE"]


def test_compute_bad_since_raises(tmp_path: Path) -> None:
    import pytest

    _seed_workspace(tmp_path)
    with pytest.raises(ValueError, match="since"):
        metric.compute(tmp_path, "demo", since_iso="nope", until_iso=_UNTIL, now_iso=_NOW)


# ─── checkpoint aggregation ──────────────────────────────────────────────────


def _write_manifest(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(e, sort_keys=True) + "\n" for e in entries), encoding="utf-8"
    )


def test_checkpoint_aggregates_two_participants(tmp_path: Path) -> None:
    ws_a = tmp_path / "ws_a"
    ws_b = tmp_path / "ws_b"
    _seed_workspace(ws_a, namespace="ns_a")
    _seed_workspace(ws_b, namespace="ns_b")

    # ws_a: one via-flow ticket
    _write_ship_event(
        ws_a, "A-1", shipped_at="2026-05-20T10:00:00Z", observed_by_run_id="ra", namespace="ns_a"
    )
    _write_state(ws_a, "A-1", run_id="ra", reflect_status="completed")
    # ws_b: one via-flow ticket + one not-attributed
    _write_ship_event(
        ws_b, "B-1", shipped_at="2026-05-21T10:00:00Z", observed_by_run_id="rb", namespace="ns_b"
    )
    _write_state(ws_b, "B-1", run_id="rb", reflect_status="completed")
    _write_ship_event(ws_b, "B-2", shipped_at="2026-05-22T10:00:00Z", namespace="ns_b")

    manifest = tmp_path / "manifest.jsonl"
    _write_manifest(
        manifest,
        [
            {
                "ts": "2026-05-01T00:00:00Z",
                "workspace_root": str(ws_a),
                "namespace": "ns_a",
                "checkpoint_mode": "personal",
            },
            {
                "ts": "2026-05-02T00:00:00Z",
                "workspace_root": str(ws_b),
                "namespace": "ns_b",
                "checkpoint_mode": "personal",
            },
            # a work-mode participant must be excluded from personal aggregation
            {
                "ts": "2026-05-02T00:00:00Z",
                "workspace_root": str(ws_a),
                "namespace": "ns_a",
                "checkpoint_mode": "work",
            },
        ],
    )

    result = metric.compute_checkpoint(
        "personal",
        since_iso=_SINCE,
        until_iso=_UNTIL,
        now_iso=_NOW,
        manifest_path=manifest,
    )
    assert result["participant_count"] == 2
    assert result["shipped"] == 3
    assert result[metric.ATTR_VIA_FLOW] == 2
    assert result[metric.ATTR_NOT_ATTRIBUTED] == 1


def test_checkpoint_excludes_initialized_after_until(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    _seed_workspace(ws, namespace="ns")
    _write_ship_event(
        ws, "X-1", shipped_at="2026-05-20T10:00:00Z", observed_by_run_id="rx", namespace="ns"
    )
    _write_state(ws, "X-1", run_id="rx", reflect_status="completed")
    manifest = tmp_path / "manifest.jsonl"
    _write_manifest(
        manifest,
        [
            {
                "initialized_at": "2026-06-01T00:00:00Z",  # after until
                "workspace_path": str(ws),
                "namespace": "ns",
                "checkpoint_mode": "personal",
            }
        ],
    )
    result = metric.compute_checkpoint(
        "personal", since_iso=_SINCE, until_iso=_UNTIL, now_iso=_NOW, manifest_path=manifest
    )
    assert result["participant_count"] == 0
    assert result["shipped"] == 0


def test_checkpoint_missing_manifest_is_empty(tmp_path: Path) -> None:
    result = metric.compute_checkpoint(
        "work",
        since_iso=_SINCE,
        until_iso=_UNTIL,
        now_iso=_NOW,
        manifest_path=tmp_path / "nope.jsonl",
    )
    assert result["participant_count"] == 0
    assert result["shipped"] == 0


# ─── CLI ─────────────────────────────────────────────────────────────────────


def test_cli_namespace_required_without_checkpoint(tmp_path: Path, capsys) -> None:
    rc = metric.cli_main(["tickets-per-week", "--workspace-root", str(tmp_path)])
    assert rc == 1
    assert "namespace is required" in capsys.readouterr().err


def test_cli_happy_prints_json(tmp_path: Path, capsys) -> None:
    _seed_workspace(tmp_path)
    _write_ship_event(tmp_path, "FT-1", shipped_at="2026-05-20T10:00:00Z")
    rc = metric.cli_main(
        [
            "tickets-per-week",
            "--namespace",
            "demo",
            "--workspace-root",
            str(tmp_path),
            "--since",
            "2026-05-14",
            "--until",
            "2026-05-28",
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["shipped"] == 1
    assert payload["since"] == "2026-05-14T00:00:00Z"
    assert payload["until"] == "2026-05-28T00:00:00Z"


def test_cli_checkpoint_requires_mode(tmp_path: Path, capsys) -> None:
    rc = metric.cli_main(["tickets-per-week", "--checkpoint"])
    assert rc == 1
    assert "--mode" in capsys.readouterr().err


def test_cli_checkpoint_aggregates(tmp_path: Path, capsys) -> None:
    ws = tmp_path / "ws"
    _seed_workspace(ws, namespace="ns")
    _write_ship_event(
        ws, "X-1", shipped_at="2026-05-20T10:00:00Z", observed_by_run_id="rx", namespace="ns"
    )
    _write_state(ws, "X-1", run_id="rx", reflect_status="completed")
    manifest = tmp_path / "manifest.jsonl"
    _write_manifest(
        manifest,
        [
            {
                "ts": "2026-05-01T00:00:00Z",
                "workspace_root": str(ws),
                "namespace": "ns",
                "checkpoint_mode": "work",
            }
        ],
    )
    rc = metric.cli_main(
        [
            "tickets-per-week",
            "--checkpoint",
            "--mode",
            "work",
            "--manifest-path",
            str(manifest),
            "--since",
            "2026-05-14",
            "--until",
            "2026-05-28",
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "work"
    assert payload["participant_count"] == 1
    assert payload[metric.ATTR_VIA_FLOW] == 1


def test_cli_bad_date_returns_1(tmp_path: Path, capsys) -> None:
    rc = metric.cli_main(["tickets-per-week", "--namespace", "demo", "--since", "not-a-date"])
    assert rc == 1
