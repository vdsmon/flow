"""Tests for metric.py revert-rate — reopened-bead join (option A).

A revert is a shipped bead that is reopened and re-closed AFTER its shipped_at,
detected at compute time by joining each in-window ship-event to its tracker
status history. The history seam (`metric._status_history`) is monkeypatched with
in-memory fakes keyed by ticket, so these tests never shell `bd`. The real
bd-JSON parsing in `_status_history` is verified by inspection (see plan).

Decidable events (clean-no-reopen, reopened-and-reclosed) land in `tickets[]` and
form the `shipped` denominator. Undecidable / unmeasurable events
(history_unavailable, tracker_unsupported, reopened_not_yet_reclosed) land in
`skipped[]` and are NOT counted toward `shipped`.
"""

from __future__ import annotations

import json
from pathlib import Path

import _memory_paths
import metric


def _seed_workspace(root: Path, namespace: str = "demo", backend: str = "beads") -> None:
    flow = root / ".flow"
    (flow / namespace).mkdir(parents=True, exist_ok=True)
    (flow / "workspace.toml").write_text(
        f'[tracker]\nbackend = "{backend}"\n\n[memory]\nnamespace = "{namespace}"\n',
        encoding="utf-8",
    )


def _write_ship_event(
    root: Path,
    ticket: str,
    shipped_at: str,
    *,
    namespace: str = "demo",
    stamped: bool = False,
) -> None:
    sdir = _memory_paths.ship_events_dir(root, namespace)
    sdir.mkdir(parents=True, exist_ok=True)
    event: dict = {
        "ticket": ticket,
        "shipped_at": shipped_at,
        "observed_by_run_id": f"run-{ticket}",
    }
    if stamped:
        event["flow_attribution"] = {
            "plan_started_at_iso": "2026-06-01T10:00:00Z",
            "create_pr_finished_at_iso": "2026-06-01T12:00:00Z",
        }
    (sdir / f"{ticket}.json").write_text(json.dumps(event, sort_keys=True), encoding="utf-8")


def _patch_history(monkeypatch, histories: dict[str, list[tuple[str, str]] | None]) -> None:
    """Fake metric._status_history: map ticket -> [(iso, status)] or None.

    A ticket absent from the map returns None (history unavailable).
    """

    def fake(workspace_root, namespace, ticket):
        snaps = histories.get(ticket)
        if snaps is None:
            return None
        from _timeutil import parse_iso

        out = []
        for iso, status in snaps:
            dt = parse_iso(iso)
            if dt is not None:
                out.append((dt, status))
        out.sort(key=lambda p: p[0])
        return out

    monkeypatch.setattr(metric, "_status_history", fake)


SINCE = "2026-06-01T00:00:00Z"
UNTIL = "2026-06-08T00:00:00Z"


def _compute(root: Path, namespace: str = "demo") -> dict:
    return metric.compute_revert_rate(root, namespace, since_iso=SINCE, until_iso=UNTIL)


def test_no_reopen_after_ship_not_a_revert(tmp_path: Path, monkeypatch) -> None:
    _seed_workspace(tmp_path)
    _write_ship_event(tmp_path, "FT-1", "2026-06-03T00:00:00Z")
    _patch_history(
        monkeypatch,
        {
            "FT-1": [
                ("2026-06-02T00:00:00Z", "in_progress"),
                ("2026-06-03T00:00:00Z", "closed"),
                ("2026-06-04T00:00:00Z", "closed"),
            ]
        },
    )
    result = _compute(tmp_path)
    assert result["shipped"] == 1
    assert result["n_reverts"] == 0
    assert result["revert_rate"] == 0
    assert result["tickets"][0]["reverted"] is False


def test_reopen_reclose_after_ship_counted(tmp_path: Path, monkeypatch) -> None:
    _seed_workspace(tmp_path)
    _write_ship_event(tmp_path, "FT-1", "2026-06-03T00:00:00Z")
    _patch_history(
        monkeypatch,
        {
            "FT-1": [
                ("2026-06-03T00:00:00Z", "closed"),
                ("2026-06-04T00:00:00Z", "open"),
                ("2026-06-05T00:00:00Z", "closed"),
            ]
        },
    )
    result = _compute(tmp_path)
    assert result["shipped"] == 1
    assert result["n_reverts"] == 1
    assert result["revert_rate"] == 1.0
    t = result["tickets"][0]
    assert t["reverted"] is True
    assert t["reopened_at"] == "2026-06-04T00:00:00+00:00"
    assert t["reclosed_at"] == "2026-06-05T00:00:00+00:00"


def test_reopen_reclose_before_ship_not_counted(tmp_path: Path, monkeypatch) -> None:
    _seed_workspace(tmp_path)
    _write_ship_event(tmp_path, "FT-1", "2026-06-05T00:00:00Z")
    _patch_history(
        monkeypatch,
        {
            "FT-1": [
                ("2026-06-02T00:00:00Z", "closed"),
                ("2026-06-03T00:00:00Z", "open"),
                ("2026-06-04T00:00:00Z", "closed"),
                ("2026-06-05T00:00:00Z", "closed"),
            ]
        },
    )
    result = _compute(tmp_path)
    assert result["shipped"] == 1
    assert result["n_reverts"] == 0
    assert result["tickets"][0]["reverted"] is False


def test_in_flight_reopen_skipped(tmp_path: Path, monkeypatch) -> None:
    _seed_workspace(tmp_path)
    _write_ship_event(tmp_path, "FT-1", "2026-06-03T00:00:00Z")
    _patch_history(
        monkeypatch,
        {
            "FT-1": [
                ("2026-06-03T00:00:00Z", "closed"),
                ("2026-06-04T00:00:00Z", "open"),
            ]
        },
    )
    result = _compute(tmp_path)
    assert result["shipped"] == 0
    assert result["n_reverts"] == 0
    assert result["tickets"] == []
    assert result["skipped"] == [{"ticket": "FT-1", "reason": "reopened_not_yet_reclosed"}]
    assert result["n_skipped"] == 1


def test_consecutive_duplicate_statuses_collapse(tmp_path: Path, monkeypatch) -> None:
    _seed_workspace(tmp_path)
    _write_ship_event(tmp_path, "FT-1", "2026-06-03T00:00:00Z")
    _patch_history(
        monkeypatch,
        {
            "FT-1": [
                ("2026-06-03T00:00:00Z", "closed"),
                ("2026-06-03T01:00:00Z", "closed"),
                ("2026-06-03T02:00:00Z", "closed"),
                ("2026-06-04T00:00:00Z", "closed"),
            ]
        },
    )
    result = _compute(tmp_path)
    assert result["shipped"] == 1
    assert result["n_reverts"] == 0
    assert result["tickets"][0]["reverted"] is False


def test_attribution_split(tmp_path: Path, monkeypatch) -> None:
    _seed_workspace(tmp_path)
    _write_ship_event(tmp_path, "FT-1", "2026-06-03T00:00:00Z", stamped=True)
    _write_ship_event(tmp_path, "FT-2", "2026-06-03T00:00:00Z", stamped=False)
    revert = [
        ("2026-06-03T00:00:00Z", "closed"),
        ("2026-06-04T00:00:00Z", "open"),
        ("2026-06-05T00:00:00Z", "closed"),
    ]
    _patch_history(monkeypatch, {"FT-1": revert, "FT-2": revert})
    result = _compute(tmp_path)
    assert result["n_reverts"] == 2
    assert result["reverts_via_flow"] == 1
    assert result["reverts_not_attributed"] == 1


def test_window_filtering_excludes_out_of_window_revert(tmp_path: Path, monkeypatch) -> None:
    _seed_workspace(tmp_path)
    _write_ship_event(tmp_path, "FT-1", "2026-05-20T00:00:00Z")
    _patch_history(
        monkeypatch,
        {
            "FT-1": [
                ("2026-05-20T00:00:00Z", "closed"),
                ("2026-05-21T00:00:00Z", "open"),
                ("2026-05-22T00:00:00Z", "closed"),
            ]
        },
    )
    result = _compute(tmp_path)
    assert result["shipped"] == 0
    assert result["n_reverts"] == 0
    assert result["tickets"] == []


def test_history_unavailable_skipped(tmp_path: Path, monkeypatch) -> None:
    _seed_workspace(tmp_path)
    _write_ship_event(tmp_path, "FT-1", "2026-06-03T00:00:00Z")
    _patch_history(monkeypatch, {"FT-1": None})
    result = _compute(tmp_path)
    assert result["shipped"] == 0
    assert result["n_reverts"] == 0
    assert result["skipped"] == [{"ticket": "FT-1", "reason": "history_unavailable"}]
    assert result["n_skipped"] == 1


def test_non_beads_backend_all_skipped_no_bd_call(tmp_path: Path, monkeypatch) -> None:
    _seed_workspace(tmp_path, backend="jira")
    _write_ship_event(tmp_path, "FT-1", "2026-06-03T00:00:00Z")

    def boom(*a, **k):
        raise AssertionError("_status_history must not be called for non-beads backend")

    monkeypatch.setattr(metric, "_status_history", boom)
    result = _compute(tmp_path)
    assert result["shipped"] == 0
    assert result["n_reverts"] == 0
    assert result["skipped"] == [{"ticket": "FT-1", "reason": "tracker_unsupported"}]
    assert result["n_skipped"] == 1


def test_revert_rate_zero_when_shipped_zero(tmp_path: Path, monkeypatch) -> None:
    _seed_workspace(tmp_path)
    _patch_history(monkeypatch, {})
    result = _compute(tmp_path)
    assert result["shipped"] == 0
    assert result["n_reverts"] == 0
    assert result["revert_rate"] == 0


def test_commitdate_offset_vs_z_tz_aware(tmp_path: Path, monkeypatch) -> None:
    # shipped_at in Z, history CommitDates in -03:00 offset. The reopen at
    # 2026-06-03T22:00:00-03:00 == 2026-06-04T01:00:00Z is AFTER shipped_at.
    _seed_workspace(tmp_path)
    _write_ship_event(tmp_path, "FT-1", "2026-06-03T12:00:00Z")
    _patch_history(
        monkeypatch,
        {
            "FT-1": [
                ("2026-06-03T08:00:00-03:00", "closed"),
                ("2026-06-03T22:00:00-03:00", "open"),
                ("2026-06-04T09:00:00-03:00", "closed"),
            ]
        },
    )
    result = _compute(tmp_path)
    assert result["shipped"] == 1
    assert result["n_reverts"] == 1


def test_cli_happy_prints_json(tmp_path: Path, monkeypatch, capsys) -> None:
    _seed_workspace(tmp_path)
    _write_ship_event(tmp_path, "FT-1", "2026-06-03T00:00:00Z")
    _patch_history(
        monkeypatch,
        {
            "FT-1": [
                ("2026-06-03T00:00:00Z", "closed"),
                ("2026-06-04T00:00:00Z", "open"),
                ("2026-06-05T00:00:00Z", "closed"),
            ]
        },
    )
    rc = metric.cli_main(
        [
            "revert-rate",
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
    for key in (
        "since",
        "until",
        "shipped",
        "n_reverts",
        "revert_rate",
        "reverts_via_flow",
        "reverts_not_attributed",
        "tickets",
        "skipped",
        "n_skipped",
    ):
        assert key in payload
    assert payload["n_reverts"] == 1
    assert payload["since"] == "2026-06-01T00:00:00Z"
    assert payload["until"] == "2026-06-08T00:00:00Z"


def test_cli_namespace_required(tmp_path: Path, capsys) -> None:
    rc = metric.cli_main(["revert-rate", "--workspace-root", str(tmp_path)])
    assert rc == 1
    assert "namespace" in capsys.readouterr().err


# ─── ub76: cwd silent-zeros guard + resolved-root stamp ──────────────────────


def test_cli_no_flow_fails_loud(tmp_path: Path, capsys) -> None:
    rc = metric.cli_main(["revert-rate", "--namespace", "demo", "--workspace-root", str(tmp_path)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "no .flow" in err
    assert str(tmp_path.resolve()) in err


def test_cli_happy_stamps_resolved_root(tmp_path: Path, monkeypatch, capsys) -> None:
    _seed_workspace(tmp_path)
    _write_ship_event(tmp_path, "FT-1", "2026-06-03T00:00:00Z")
    _patch_history(monkeypatch, {"FT-1": [("2026-06-03T00:00:00Z", "closed")]})
    rc = metric.cli_main(
        [
            "revert-rate",
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
    assert payload["resolved_workspace_root"] == str(tmp_path.resolve())
