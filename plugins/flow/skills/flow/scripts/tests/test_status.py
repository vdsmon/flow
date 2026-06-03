from __future__ import annotations

from pathlib import Path

import pytest

import state
import status


def _ws(root: Path, pipeline: tuple[str, ...] = ("ticket", "plan", "commit")) -> Path:
    flow = root / ".flow"
    (flow / "runs").mkdir(parents=True)
    stages_toml = ", ".join(f'"{s}"' for s in pipeline)
    (flow / "workspace.toml").write_text(
        f'[tracker]\nbackend = "jira"\n[pipeline]\nstages = [{stages_toml}]\n[memory]\nnamespace = "FT"\n',
        encoding="utf-8",
    )
    return root


def _seed_run(
    root: Path,
    ticket: str,
    stages: list[str],
    *,
    finished: list[str] | None = None,
    failed: str | None = None,
) -> Path:
    td = root / ".flow" / "runs" / ticket
    state.init(td, ticket, "jira", stages)
    for s in finished or []:
        state.force_stage_status(td, s, "completed")
    if failed:
        state.force_stage_status(td, failed, "failed")
    return td


def test_status_no_flow_returns_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = status.cli_main(["--workspace-root", str(tmp_path)])
    assert rc == 1
    assert "init" in capsys.readouterr().err


def test_collect_two_runs(tmp_path: Path) -> None:
    _ws(tmp_path)
    _seed_run(tmp_path, "FT-1", ["ticket", "plan", "commit"], finished=["ticket"])
    _seed_run(tmp_path, "FT-2", ["ticket", "plan"], finished=["ticket", "plan"])
    rows = status.collect(tmp_path)
    assert [r["ticket"] for r in rows] == ["FT-1", "FT-2"]
    r1 = next(r for r in rows if r["ticket"] == "FT-1")
    assert r1["completed"] == 1
    assert r1["total_stages"] == 3
    assert r1["next_or_blocked"] == "plan:pending"
    r2 = next(r for r in rows if r["ticket"] == "FT-2")
    assert r2["next_or_blocked"] == "done"


def test_collect_failed_stage(tmp_path: Path) -> None:
    _ws(tmp_path)
    _seed_run(tmp_path, "FT-3", ["ticket", "plan"], finished=["ticket"], failed="plan")
    rows = status.collect(tmp_path)
    assert rows[0]["next_or_blocked"] == "plan:failed"
    assert rows[0]["failed"] == 1


def test_collect_ticket_filter(tmp_path: Path) -> None:
    _ws(tmp_path)
    _seed_run(tmp_path, "FT-1", ["ticket"], finished=["ticket"])
    _seed_run(tmp_path, "FT-2", ["ticket"])
    rows = status.collect(tmp_path, ticket="FT-2")
    assert len(rows) == 1
    assert rows[0]["ticket"] == "FT-2"


def test_render_table_contains_ids(tmp_path: Path) -> None:
    _ws(tmp_path)
    _seed_run(tmp_path, "FT-9", ["ticket", "plan"], finished=["ticket"])
    out = status.render_table(status.collect(tmp_path))
    assert "FT-9" in out
    assert "1/2" in out
    assert "TICKET" in out


def test_render_table_empty() -> None:
    assert status.render_table([]) == "(no runs)"
