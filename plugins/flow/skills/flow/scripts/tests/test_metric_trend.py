"""Tests for metric.py `trend` — the four-measure window roll-up.

`trend` calls compute(), compute_time_to_pr(), compute_friction_per_run(), and
compute_revert_rate() over one [since, until) window and renders a table (default)
or a JSON object (`--json`). The revert leg needs a real git repo (else
_scan_main_reverts raises RevertScanError) and a monkeypatched _status_history;
this harness mirrors test_metric_revert.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import _memory_paths
import metric


def _seed_workspace(
    root: Path, namespace: str = "demo", backend: str = "beads", *, git: bool = True
) -> None:
    flow = root / ".flow"
    (flow / namespace).mkdir(parents=True, exist_ok=True)
    (flow / "workspace.toml").write_text(
        f'[tracker]\nbackend = "{backend}"\n\n[memory]\nnamespace = "{namespace}"\n',
        encoding="utf-8",
    )
    if git:
        _git_init(root)


def _git(root: Path, *args: str) -> str:
    import subprocess

    proc = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def _git_init(root: Path) -> None:
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "commit", "--allow-empty", "-m", "root")


def _commit_with_message(root: Path, message: str) -> str:
    _git(root, "commit", "--allow-empty", "-m", message)
    return _git(root, "rev-parse", "HEAD")


def _make_git_revert(root: Path, feature_message: str) -> tuple[str, str]:
    feature_sha = _commit_with_message(root, feature_message)
    revert_msg = f'Revert "{feature_message.splitlines()[0]}"\n\nThis reverts commit {feature_sha}.'
    revert_sha = _commit_with_message(root, revert_msg)
    return feature_sha, revert_sha


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


SINCE = "2026-06-01"
UNTIL = "2026-06-08"
SINCE_ISO = "2026-06-01T00:00:00Z"
UNTIL_ISO = "2026-06-08T00:00:00Z"

MEASURE_KEYS = ("tickets-per-week", "time-to-pr", "friction-per-run", "revert-rate")


def test_json_has_all_four_measure_keys_with_headlines(tmp_path: Path, monkeypatch, capsys) -> None:
    _seed_workspace(tmp_path)
    _write_ship_event(tmp_path, "FT-1", "2026-06-03T00:00:00Z", stamped=True)
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
            "trend",
            "--namespace",
            "demo",
            "--workspace-root",
            str(tmp_path),
            "--since",
            SINCE,
            "--until",
            UNTIL,
            "--json",
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    for key in MEASURE_KEYS:
        assert key in payload, f"missing measure key {key}"
    for key in ("shipped", "shipped_via_flow", "shipped_backend_not_attributed"):
        assert key in payload["tickets-per-week"]
    for key in ("n_measured", "median_hours", "p90_hours"):
        assert key in payload["time-to-pr"]
    for key in ("total_events", "runs", "events_per_run"):
        assert key in payload["friction-per-run"]
    for key in ("shipped", "n_reverts", "revert_rate", "reverts_by_source"):
        assert key in payload["revert-rate"]


def test_json_top_level_window_and_resolved_root(tmp_path: Path, monkeypatch, capsys) -> None:
    _seed_workspace(tmp_path)
    _patch_history(monkeypatch, {})
    rc = metric.cli_main(
        [
            "trend",
            "--namespace",
            "demo",
            "--workspace-root",
            str(tmp_path),
            "--since",
            SINCE,
            "--until",
            UNTIL,
            "--json",
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["since"] == SINCE_ISO
    assert payload["until"] == UNTIL_ISO
    assert payload["resolved_workspace_root"] == str(tmp_path.resolve())


def test_json_revert_by_source_split_present(tmp_path: Path, monkeypatch, capsys) -> None:
    _seed_workspace(tmp_path)
    _write_ship_event(tmp_path, "FT-1", "2026-06-03T00:00:00Z")
    _patch_history(monkeypatch, {"FT-1": [("2026-06-03T00:00:00Z", "closed")]})
    _make_git_revert(tmp_path, "feat: thing (#1)\n\nticket: FT-1")
    rc = metric.cli_main(
        [
            "trend",
            "--namespace",
            "demo",
            "--workspace-root",
            str(tmp_path),
            "--since",
            SINCE,
            "--until",
            UNTIL,
            "--json",
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    by_source = payload["revert-rate"]["reverts_by_source"]
    assert "tracker" in by_source
    assert "git" in by_source
    assert by_source["git"] == 1


def test_default_table_renders_each_measure_and_not_json(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _seed_workspace(tmp_path)
    _write_ship_event(tmp_path, "FT-1", "2026-06-03T00:00:00Z")
    _patch_history(monkeypatch, {"FT-1": [("2026-06-03T00:00:00Z", "closed")]})
    _make_git_revert(tmp_path, "feat: thing (#1)\n\nticket: FT-1")
    rc = metric.cli_main(
        [
            "trend",
            "--namespace",
            "demo",
            "--workspace-root",
            str(tmp_path),
            "--since",
            SINCE,
            "--until",
            UNTIL,
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    for name in MEASURE_KEYS:
        assert name in out
    assert "tracker=0" in out
    assert "git=1" in out
    import pytest

    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


def test_namespace_required(tmp_path: Path, capsys) -> None:
    rc = metric.cli_main(["trend", "--workspace-root", str(tmp_path)])
    assert rc == 1
    assert "namespace" in capsys.readouterr().err


def test_no_flow_dir(tmp_path: Path, capsys) -> None:
    rc = metric.cli_main(["trend", "--namespace", "demo", "--workspace-root", str(tmp_path)])
    assert rc == 1
    assert "no .flow" in capsys.readouterr().err


def test_happy_run_returns_zero(tmp_path: Path, monkeypatch, capsys) -> None:
    _seed_workspace(tmp_path)
    _write_ship_event(tmp_path, "FT-1", "2026-06-03T00:00:00Z")
    _patch_history(monkeypatch, {"FT-1": [("2026-06-03T00:00:00Z", "closed")]})
    rc = metric.cli_main(
        [
            "trend",
            "--namespace",
            "demo",
            "--workspace-root",
            str(tmp_path),
            "--since",
            SINCE,
            "--until",
            UNTIL,
            "--json",
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    # all four measure keys prove trend did not fall through to the tpw path.
    for key in MEASURE_KEYS:
        assert key in payload


def test_revert_scan_error_fails_loud(tmp_path: Path, monkeypatch, capsys) -> None:
    _seed_workspace(tmp_path, git=False)
    _write_ship_event(tmp_path, "FT-1", "2026-06-03T00:00:00Z")
    _patch_history(monkeypatch, {"FT-1": [("2026-06-03T00:00:00Z", "closed")]})
    rc = metric.cli_main(
        [
            "trend",
            "--namespace",
            "demo",
            "--workspace-root",
            str(tmp_path),
            "--since",
            SINCE,
            "--until",
            UNTIL,
        ]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "git scan failed" in err
