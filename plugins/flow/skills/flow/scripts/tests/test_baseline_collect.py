"""Tests for baseline_collect.py — pre-migration time-to-PR baseline."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import baseline_collect

# ─── percentile() ──────────────────────────────────────────────────────────────


def test_percentile_median_of_odd_list() -> None:
    assert baseline_collect.percentile([10, 20, 30, 40, 50], 50) == 30.0


def test_percentile_p90_interpolated() -> None:
    # h = (5-1)*0.9 = 3.6 -> 40 + 0.6*(50-40) = 46.0
    assert baseline_collect.percentile([10, 20, 30, 40, 50], 90) == 46.0


def test_percentile_median_of_even_list() -> None:
    assert baseline_collect.percentile([10, 20, 30, 40], 50) == 25.0


def test_percentile_empty_returns_zero() -> None:
    assert baseline_collect.percentile([], 50) == 0.0


def test_percentile_single_element() -> None:
    assert baseline_collect.percentile([7.5], 90) == 7.5


def test_percentile_pct_100_no_index_error() -> None:
    assert baseline_collect.percentile([10, 20, 30, 40, 50], 100) == 50.0


def test_percentile_does_not_mutate_caller_list() -> None:
    values = [30, 10, 20]
    baseline_collect.percentile(values, 50)
    assert values == [30, 10, 20]


# ─── build_baseline() ──────────────────────────────────────────────────────────


def _samples() -> list[dict[str, object]]:
    return [
        {"ticket": "FT-1", "time_to_pr_hours": 10},
        {"ticket": "FT-2", "time_to_pr_hours": 20},
        {"ticket": "FT-3", "time_to_pr_hours": 30},
        {"ticket": "FT-4", "time_to_pr_hours": 40},
        {"ticket": "FT-5", "time_to_pr_hours": 50},
    ]


def test_build_baseline_computes_stats() -> None:
    baseline = baseline_collect.build_baseline(
        _samples(), collected_at="2026-05-28T00:00:00Z", source="manual"
    )
    assert baseline["median_hours"] == 30.0
    assert baseline["p90_hours"] == 46.0
    assert baseline["n"] == 5
    assert baseline["collected_at"] == "2026-05-28T00:00:00Z"
    assert baseline["source"] == "manual"
    assert baseline["samples"] == _samples()


def test_build_baseline_empty_raises() -> None:
    with pytest.raises(baseline_collect._NoSamples):
        baseline_collect.build_baseline([], collected_at="2026-05-28T00:00:00Z")


# ─── write/read round-trip ─────────────────────────────────────────────────────


def test_write_then_read_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    baseline = baseline_collect.build_baseline(_samples(), collected_at="2026-05-28T00:00:00Z")
    baseline_collect.write_baseline(path, baseline)
    assert baseline_collect.read_baseline(path) == baseline


def test_read_missing_file_returns_none(tmp_path: Path) -> None:
    assert baseline_collect.read_baseline(tmp_path / "absent.json") is None


# ─── CLI ───────────────────────────────────────────────────────────────────────


def test_cli_build_from_inline_json_writes_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "baseline.json"
    inline = json.dumps(_samples())
    rc = baseline_collect.cli_main(["build", "--samples-json", inline, "--path", str(path)])
    assert rc == 0
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["median_hours"] == 30.0
    assert stored["p90_hours"] == 46.0
    assert stored["n"] == 5
    printed = json.loads(capsys.readouterr().out)
    assert printed["n"] == 5


def test_cli_build_from_file_writes_file(tmp_path: Path) -> None:
    samples_file = tmp_path / "samples.json"
    samples_file.write_text(json.dumps(_samples()), encoding="utf-8")
    path = tmp_path / "baseline.json"
    rc = baseline_collect.cli_main(
        ["build", "--samples-json", str(samples_file), "--path", str(path)]
    )
    assert rc == 0
    assert json.loads(path.read_text(encoding="utf-8"))["n"] == 5


def test_cli_show_reads_stored_baseline(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "baseline.json"
    baseline_collect.cli_main(
        ["build", "--samples-json", json.dumps(_samples()), "--path", str(path)]
    )
    capsys.readouterr()
    rc = baseline_collect.cli_main(["show", "--path", str(path)])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["n"] == 5


def test_cli_build_empty_samples_returns_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "baseline.json"
    rc = baseline_collect.cli_main(["build", "--samples-json", "[]", "--path", str(path)])
    assert rc == 1
    assert not path.exists()


def test_cli_build_malformed_json_returns_1(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    rc = baseline_collect.cli_main(["build", "--samples-json", "{not json", "--path", str(path)])
    assert rc == 1


def test_cli_show_missing_baseline_returns_3(tmp_path: Path) -> None:
    rc = baseline_collect.cli_main(["show", "--path", str(tmp_path / "absent.json")])
    assert rc == 3
