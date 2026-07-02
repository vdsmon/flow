"""Contract tests for model_resolve.py: the work-phase model resolver.

Disposition is ON BY DEFAULT: a full-lane run downshifts to `sonnet` unless the
workspace overrides `[models] work_model` or opts out (work_model in OFF_VALUES).
"express"/"light" always skip; any read error fails open (prints nothing).
"""

from __future__ import annotations

from pathlib import Path

import model_resolve


def _make_workspace(
    tmp_path: Path,
    *,
    lane: str | None = None,
    work_model: str | None = None,
    models_block: bool = False,
    frontmatter: bool = True,
) -> Path:
    flow = tmp_path / ".flow"
    (flow / "tickets").mkdir(parents=True)

    lines = ["[tracker]", 'backend = "beads"', "", "[tracker.beads]", 'prefix = "test"']
    if models_block:
        lines += ["", "[models]"]
        if work_model is not None:
            lines.append(f'work_model = "{work_model}"')
    (flow / "workspace.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")

    if frontmatter:
        fm = ["+++", 'ticket = "test-1"', 'status = "in_progress"']
        if lane is not None:
            fm.append(f'lane = "{lane}"')
        fm += ["+++", "", "plan body"]
        (flow / "tickets" / "test-1.md").write_text("\n".join(fm) + "\n", encoding="utf-8")
    return tmp_path


def test_default_on_lane_absent(tmp_path: Path) -> None:
    # no [models] block at all -> the default is on -> sonnet.
    ws = _make_workspace(tmp_path, lane=None, models_block=False)
    assert model_resolve.resolve_work_model(ws, "test-1") == "sonnet"


def test_default_on_lane_full(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path, lane="full", models_block=False)
    assert model_resolve.resolve_work_model(ws, "test-1") == "sonnet"


def test_empty_models_block_defaults(tmp_path: Path) -> None:
    # [models] present but no work_model key -> still the default.
    ws = _make_workspace(tmp_path, lane=None, models_block=True, work_model=None)
    assert model_resolve.resolve_work_model(ws, "test-1") == "sonnet"


def test_explicit_sonnet(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path, lane=None, models_block=True, work_model="sonnet")
    assert model_resolve.resolve_work_model(ws, "test-1") == "sonnet"


def test_override_to_a_different_model(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path, lane=None, models_block=True, work_model="opus")
    assert model_resolve.resolve_work_model(ws, "test-1") == "opus"


def test_opt_out_off(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path, lane=None, models_block=True, work_model="off")
    assert model_resolve.resolve_work_model(ws, "test-1") == ""


def test_opt_out_none(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path, lane=None, models_block=True, work_model="none")
    assert model_resolve.resolve_work_model(ws, "test-1") == ""


def test_opt_out_empty_string(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path, lane=None, models_block=True, work_model="")
    assert model_resolve.resolve_work_model(ws, "test-1") == ""


def test_lane_express_skips(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path, lane="express", models_block=False)
    assert model_resolve.resolve_work_model(ws, "test-1") == ""


def test_lane_light_skips(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path, lane="light", models_block=False)
    assert model_resolve.resolve_work_model(ws, "test-1") == ""


def test_missing_frontmatter_defaults(tmp_path: Path) -> None:
    # No frontmatter file -> read() returns {} -> lane absent -> default sonnet.
    ws = _make_workspace(tmp_path, models_block=False, frontmatter=False)
    assert model_resolve.resolve_work_model(ws, "test-1") == "sonnet"


def test_missing_workspace_toml_keeps_default(tmp_path: Path) -> None:
    # No .flow/workspace.toml -> load raises -> the default (sonnet) still applies.
    tickets = tmp_path / ".flow" / "tickets"
    tickets.mkdir(parents=True)
    (tickets / "test-1.md").write_text('+++\nlane = "full"\n+++\n', encoding="utf-8")
    assert model_resolve.resolve_work_model(tmp_path, "test-1") == "sonnet"


def test_cli_prints_default(tmp_path: Path, capsys) -> None:
    ws = _make_workspace(tmp_path, lane=None, models_block=False)
    rc = model_resolve.cli_main(["--workspace-root", str(ws), "--ticket", "test-1"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "sonnet"


def test_cli_prints_nothing_when_opted_out(tmp_path: Path, capsys) -> None:
    ws = _make_workspace(tmp_path, lane=None, models_block=True, work_model="off")
    rc = model_resolve.cli_main(["--workspace-root", str(ws), "--ticket", "test-1"])
    assert rc == 0
    assert capsys.readouterr().out == ""
