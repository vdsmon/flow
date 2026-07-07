"""Tests for the shared external memory store (`[memory].root`).

A git-worktree run has cwd = the worktree, so without a shared root every per-ticket
worktree would get its own `.flow/<ns>/` store and the compounding-knowledge layer
would fragment. `[memory].root` redirects the store to one stable absolute path.
"""

from __future__ import annotations

from pathlib import Path

import _memory_paths
import init as init_mod
import validate_workspace as vw


def _write_workspace(
    root: Path, *, namespace: str = "demo", memory_root: str | None = None
) -> None:
    flow = root / ".flow"
    flow.mkdir(parents=True, exist_ok=True)
    lines = [
        "[tracker]",
        'backend = "jira"',
        "[tracker.jira]",
        'cloud_id = "x"',
        'project_key = "FT"',
        "[memory]",
        f'namespace = "{namespace}"',
    ]
    if memory_root is not None:
        lines.append(f'root = "{memory_root}"')
    (flow / "workspace.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_base_falls_back_to_local_flow_when_root_unset(tmp_path: Path) -> None:
    _write_workspace(tmp_path)
    assert _memory_paths.resolve_memory_base(tmp_path) == tmp_path / ".flow"
    assert (
        _memory_paths.knowledge_path(tmp_path, "demo")
        == tmp_path / ".flow" / "demo" / "knowledge.jsonl"
    )


def test_base_uses_root_when_set(tmp_path: Path) -> None:
    shared = tmp_path / "main" / ".flow"
    shared.mkdir(parents=True)
    worktree = tmp_path / "wt"
    _write_workspace(worktree, memory_root=str(shared))

    assert _memory_paths.resolve_memory_base(worktree) == shared
    # knowledge + ship-events resolve under the shared store, not the worktree's .flow
    assert _memory_paths.knowledge_path(worktree, "demo") == shared / "demo" / "knowledge.jsonl"
    assert (
        _memory_paths.knowledge_lock_path(worktree, "demo")
        == shared / "demo" / "knowledge.jsonl.lock"
    )
    assert _memory_paths.ship_events_dir(worktree, "demo") == shared / "demo" / "ship-events"
    assert (
        _memory_paths.ship_event_path(worktree, "demo", "FT-1")
        == shared / "demo" / "ship-events" / "FT-1.json"
    )
    # the worktree's own .flow is NOT used for the store
    assert (
        _memory_paths.knowledge_path(worktree, "demo")
        != worktree / ".flow" / "demo" / "knowledge.jsonl"
    )


def test_base_expands_user_in_root(tmp_path: Path) -> None:
    _write_workspace(tmp_path, memory_root="~/some/.flow")
    assert _memory_paths.resolve_memory_base(tmp_path) == Path("~/some/.flow").expanduser()


def _write_sibling(root: Path, text: str) -> None:
    flow = root / ".flow"
    flow.mkdir(parents=True, exist_ok=True)
    (flow / "memory-root").write_text(text, encoding="utf-8")


def test_base_uses_sibling_when_present(tmp_path: Path) -> None:
    # sibling and a DIFFERENT [memory].root in the toml; the sibling wins.
    toml_root = tmp_path / "toml-store" / ".flow"
    sibling_root = tmp_path / "sibling-store" / ".flow"
    _write_workspace(tmp_path, memory_root=str(toml_root))
    _write_sibling(tmp_path, str(sibling_root) + "\n")
    assert _memory_paths.resolve_memory_base(tmp_path) == sibling_root


def test_base_falls_back_to_workspace_root_when_no_sibling(tmp_path: Path) -> None:
    # no sibling, [memory].root set -> resolves from workspace.toml (back-compat).
    toml_root = tmp_path / "toml-store" / ".flow"
    _write_workspace(tmp_path, memory_root=str(toml_root))
    assert _memory_paths.resolve_memory_base(tmp_path) == toml_root


def test_base_expands_user_in_sibling(tmp_path: Path) -> None:
    _write_workspace(tmp_path)
    _write_sibling(tmp_path, "~/some/.flow\n")
    assert _memory_paths.resolve_memory_base(tmp_path) == Path("~/some/.flow").expanduser()


def test_base_tolerates_empty_or_whitespace_sibling(tmp_path: Path) -> None:
    # an empty/whitespace sibling falls through to the next source ([memory].root).
    toml_root = tmp_path / "toml-store" / ".flow"
    _write_workspace(tmp_path, memory_root=str(toml_root))
    _write_sibling(tmp_path, "   \n")
    assert _memory_paths.resolve_memory_base(tmp_path) == toml_root


def test_base_tolerates_unparseable_workspace(tmp_path: Path) -> None:
    flow = tmp_path / ".flow"
    flow.mkdir()
    (flow / "workspace.toml").write_text("this is = not [ valid toml", encoding="utf-8")
    assert _memory_paths.resolve_memory_base(tmp_path) == tmp_path / ".flow"


def _memory_data(root: object) -> dict:
    mem: dict[str, object] = {
        "namespace": "demo",
        "auto_recall": True,
        "compounding": True,
        "recall_by": ["branch"],
        "recall_top_n": 5,
    }
    if root is not None:
        mem["root"] = root
    return {"memory": mem}


def test_validate_accepts_absolute_root() -> None:
    result = vw.ValidationResult()
    vw._validate_memory_block(_memory_data("/abs/shared/.flow"), result)
    assert all("memory.root" not in v for v in result.violations), result.violations


def test_validate_root_unset_is_fine() -> None:
    result = vw.ValidationResult()
    vw._validate_memory_block(_memory_data(None), result)
    assert all("memory.root" not in v for v in result.violations), result.violations


def test_validate_rejects_relative_root() -> None:
    result = vw.ValidationResult()
    vw._validate_memory_block(_memory_data("relative/.flow"), result)
    assert any("memory.root" in v and "absolute" in v for v in result.violations)


def test_validate_rejects_non_string_root() -> None:
    result = vw.ValidationResult()
    vw._validate_memory_block(_memory_data(123), result)
    assert any("memory.root" in v for v in result.violations)


def _init_config(tmp_path: Path):
    return init_mod.InitConfig(
        backend="jira",
        bundle="bare",
        workspace_root=tmp_path,
        jira=init_mod.JiraConfig(cloud_id="x", project_key="FT", assignee_account_id=None),
    )


def test_render_never_writes_root(tmp_path: Path) -> None:
    # init never writes [memory].root; the worktree share rides the gitignored
    # .flow/memory-root sibling. The read path (resolve_memory_base) still
    # honors a hand-set root, covered above.
    toml = init_mod._render_workspace_toml(
        _init_config(tmp_path), "demo", ["ticket"], {"ticket": "inline"}
    )
    assert "root =" not in toml
