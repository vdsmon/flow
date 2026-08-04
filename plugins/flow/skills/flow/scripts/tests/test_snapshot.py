"""Contract tests for snapshot.py (TOCTOU run snapshot emit + verify).

Covers: emit then verify match; workspace.toml edit -> drift names workspace_toml;
stage-registry edit -> drift names stage_registry; no snapshot -> (True, absent);
a tracked file vanishing mid-verify -> fail-closed abort.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import snapshot

# ─── Fixtures ────────────────────────────────────────────────────────────────


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _stage_registry_text() -> str:
    return """[[stage]]
name = "create_pr"
default_handler = "none"
"""


def _bare_workspace_text() -> str:
    return """[pipeline]
stages = ["create_pr"]

[pipeline.handlers]
create_pr = "inline"
"""


def _make_skill_root(tmp_path: Path) -> Path:
    skill_root = tmp_path / "skill_root"
    _write(snapshot.stage_registry_path(skill_root), _stage_registry_text())
    return skill_root


def _make_workspace(tmp_path: Path, workspace_text: str) -> Path:
    workspace_root = tmp_path / "workspace"
    _write(workspace_root / ".flow" / "workspace.toml", workspace_text)
    return workspace_root


# ─── Tests ────────────────────────────────────────────────────────────────────


def test_emit_then_verify_match_bare(tmp_path: Path) -> None:
    skill_root = _make_skill_root(tmp_path)
    workspace_root = _make_workspace(tmp_path, _bare_workspace_text())

    json_path = snapshot.write_snapshot(workspace_root, "FT-1", skill_root=skill_root)
    assert json_path == snapshot.snapshot_json_path(workspace_root, "FT-1")
    assert json_path.exists()
    assert snapshot.snapshot_sha_path(workspace_root, "FT-1").exists()

    ok, detail = snapshot.verify_snapshot(workspace_root, "FT-1", skill_root=skill_root)
    assert ok is True
    assert detail == "match"


def test_payload_components_are_workspace_registry_engine(tmp_path: Path) -> None:
    # Payload shape is the hashed contract: exactly these three components feed
    # master_hash. `handlers` was retired with the `skill:` handler form, so its
    # absence is pinned here rather than left to drift back in unnoticed.
    skill_root = _make_skill_root(tmp_path)
    workspace_root = _make_workspace(tmp_path, _bare_workspace_text())
    snap = snapshot.compute_snapshot(workspace_root, skill_root=skill_root)
    assert set(snap) == {"workspace_toml", "stage_registry", "engine", "master_hash"}


def test_workspace_edit_drift_names_workspace_toml(tmp_path: Path) -> None:
    skill_root = _make_skill_root(tmp_path)
    workspace_root = _make_workspace(tmp_path, _bare_workspace_text())
    snapshot.write_snapshot(workspace_root, "FT-1", skill_root=skill_root)

    _write(
        workspace_root / ".flow" / "workspace.toml",
        _bare_workspace_text() + "\n# user edit\n",
    )

    ok, detail = snapshot.verify_snapshot(workspace_root, "FT-1", skill_root=skill_root)
    assert ok is False
    assert "workspace_toml" in detail


def test_stage_registry_edit_drift_names_stage_registry(tmp_path: Path) -> None:
    skill_root = _make_skill_root(tmp_path)
    workspace_root = _make_workspace(tmp_path, _bare_workspace_text())
    snapshot.write_snapshot(workspace_root, "FT-1", skill_root=skill_root)

    _write(
        snapshot.stage_registry_path(skill_root),
        _stage_registry_text() + '\n[[stage]]\nname = "plan"\n',
    )

    ok, detail = snapshot.verify_snapshot(workspace_root, "FT-1", skill_root=skill_root)
    assert ok is False
    assert "stage_registry" in detail


def test_verify_with_no_snapshot_is_absent(tmp_path: Path) -> None:
    skill_root = _make_skill_root(tmp_path)
    workspace_root = _make_workspace(tmp_path, _bare_workspace_text())
    ok, detail = snapshot.verify_snapshot(workspace_root, "FT-1", skill_root=skill_root)
    assert ok is True
    assert "no snapshot" in detail


def test_drifted_components_workspace_toml_only() -> None:
    stored = {"workspace_toml": "a", "stage_registry": "r"}
    current = {"workspace_toml": "b", "stage_registry": "r"}
    assert snapshot.drifted_components(stored, current) == ["workspace_toml"]


def test_drifted_components_workspace_and_stage_registry() -> None:
    stored = {"workspace_toml": "a", "stage_registry": "r"}
    current = {"workspace_toml": "b", "stage_registry": "s"}
    assert snapshot.drifted_components(stored, current) == ["workspace_toml", "stage_registry"]


def test_drifted_components_identical_is_empty() -> None:
    snap = {"workspace_toml": "a", "stage_registry": "r", "engine": {}}
    assert snapshot.drifted_components(snap, dict(snap)) == []


def test_name_drift_output_unchanged() -> None:
    # regression guard on the formatter refactor: byte-identical to the old body.
    ws = {"workspace_toml": "a", "stage_registry": "r"}
    ws2 = {"workspace_toml": "b", "stage_registry": "r"}
    assert snapshot._name_drift(ws, ws2) == "drift: workspace_toml"

    both = {"workspace_toml": "b", "stage_registry": "s"}
    assert snapshot._name_drift(ws, both) == "drift: workspace_toml, stage_registry"

    same = {"workspace_toml": "a", "stage_registry": "r"}
    assert snapshot._name_drift(same, dict(same)) == (
        "drift: master_hash mismatch (component diff inconclusive)"
    )


def test_component_files_maps_workspace_and_stage_registry(tmp_path: Path) -> None:
    # skill_root under workspace_root → both components map to rel posix paths.
    workspace_root = tmp_path
    skill_root = tmp_path / "skill"
    files = snapshot.component_files(
        ["workspace_toml", "stage_registry"],
        workspace_root=workspace_root,
        skill_root=skill_root,
    )
    assert files == {
        "workspace_toml": ".flow/workspace.toml",
        "stage_registry": "skill/stage-registry.toml",
    }


def test_component_files_stage_registry_outside_workspace_is_none(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    skill_root = tmp_path / "elsewhere" / "skill"
    files = snapshot.component_files(
        ["stage_registry"],
        workspace_root=workspace_root,
        skill_root=skill_root,
    )
    assert files == {"stage_registry": None}


def test_component_files_handler_maps_to_none(tmp_path: Path) -> None:
    files = snapshot.component_files(
        ["handler plan"],
        workspace_root=tmp_path,
        skill_root=tmp_path / "skill",
    )
    assert files == {"handler plan": None}


def test_classify_drift_no_snapshot_is_true() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        skill_root = _make_skill_root(tmp_path)
        workspace_root = _make_workspace(tmp_path, _bare_workspace_text())
        ok, detail, comps, current = snapshot.classify_drift(
            workspace_root, "FT-1", skill_root=skill_root
        )
        assert ok is True
        assert detail == "no snapshot to verify"
        assert comps == []
        assert current is None


def test_classify_drift_match(tmp_path: Path) -> None:
    skill_root = _make_skill_root(tmp_path)
    workspace_root = _make_workspace(tmp_path, _bare_workspace_text())
    snapshot.write_snapshot(workspace_root, "FT-1", skill_root=skill_root)
    ok, detail, comps, current = snapshot.classify_drift(
        workspace_root, "FT-1", skill_root=skill_root
    )
    assert ok is True
    assert detail == "match"
    assert comps == []
    assert current is not None
    sha = snapshot.snapshot_sha_path(workspace_root, "FT-1").read_text(encoding="utf-8").strip()
    assert current["master_hash"] == sha


def test_partial_write_sha_present_json_absent_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """sha is written before json: a survivor of an interrupted write is sha-present /
    json-absent, so classify_drift on real drift fails CLOSED, not (True, "no snapshot")."""
    skill_root = _make_skill_root(tmp_path)
    workspace_root = _make_workspace(tmp_path, _bare_workspace_text())

    real_write = snapshot.atomic_write_text
    calls = {"n": 0}

    def flaky(path: Path, text: str) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("interrupted on json write")
        real_write(path, text)

    monkeypatch.setattr(snapshot, "atomic_write_text", flaky)
    with pytest.raises(OSError, match="interrupted on json write"):
        snapshot.write_snapshot(workspace_root, "FT-1", skill_root=skill_root)

    assert snapshot.snapshot_sha_path(workspace_root, "FT-1").exists()
    assert not snapshot.snapshot_json_path(workspace_root, "FT-1").exists()

    _write(
        workspace_root / ".flow" / "workspace.toml",
        _bare_workspace_text() + "\n# drift edit\n",
    )
    ok, detail, _, _ = snapshot.classify_drift(workspace_root, "FT-1", skill_root=skill_root)
    assert ok is False
    assert detail != "no snapshot to verify"


def test_classify_drift_names_components(tmp_path: Path) -> None:
    skill_root = _make_skill_root(tmp_path)
    workspace_root = _make_workspace(tmp_path, _bare_workspace_text())
    snapshot.write_snapshot(workspace_root, "FT-1", skill_root=skill_root)
    _write(
        workspace_root / ".flow" / "workspace.toml",
        _bare_workspace_text() + "\n# edit\n",
    )
    ok, detail, comps, current = snapshot.classify_drift(
        workspace_root, "FT-1", skill_root=skill_root
    )
    assert ok is False
    assert comps == ["workspace_toml"]
    assert detail == "drift: workspace_toml"
    assert current is not None


# ─── fail-closed on a vanished tracked file mid-verify ─────────────────────────


def test_classify_drift_vanished_workspace_toml_fails_closed(tmp_path: Path) -> None:
    skill_root = _make_skill_root(tmp_path)
    workspace_root = _make_workspace(tmp_path, _bare_workspace_text())
    snapshot.write_snapshot(workspace_root, "FT-1", skill_root=skill_root)

    (workspace_root / ".flow" / "workspace.toml").unlink()

    ok, detail, comps, current = snapshot.classify_drift(
        workspace_root, "FT-1", skill_root=skill_root
    )
    assert ok is False
    assert comps == []
    assert detail != "no snapshot to verify"
    assert current is None


def test_classify_drift_vanished_stage_registry_fails_closed(tmp_path: Path) -> None:
    skill_root = _make_skill_root(tmp_path)
    workspace_root = _make_workspace(tmp_path, _bare_workspace_text())
    snapshot.write_snapshot(workspace_root, "FT-1", skill_root=skill_root)

    snapshot.stage_registry_path(skill_root).unlink()

    ok, detail, comps, current = snapshot.classify_drift(
        workspace_root, "FT-1", skill_root=skill_root
    )
    assert ok is False
    assert comps == []
    assert detail != "no snapshot to verify"
    assert current is None


def test_classify_drift_plugin_reinstall_race_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The primary threat: a tracked file vanishes between the existence check and the read while
    the snapshot is being recomputed. Physical deletion can't reproduce the enumerate-then-vanish
    race, so patch the real read path to raise and prove classify_drift catches it into a
    fail-closed abort. Aimed at workspace.toml, a permanently-hashed component (it was aimed at a
    plugin tree until the `skill:` handler form was retired)."""
    skill_root = _make_skill_root(tmp_path)
    workspace_root = _make_workspace(tmp_path, _bare_workspace_text())
    snapshot.write_snapshot(workspace_root, "FT-1", skill_root=skill_root)

    real_read_text = snapshot._read_text

    def boom(path: Path) -> str:
        if path.name == "workspace.toml":
            raise FileNotFoundError("tracked file removed mid-verify")
        return real_read_text(path)

    monkeypatch.setattr(snapshot, "_read_text", boom)

    ok, detail, comps, current = snapshot.classify_drift(
        workspace_root, "FT-1", skill_root=skill_root
    )
    assert ok is False
    assert comps == []
    assert detail
    assert detail != "no snapshot to verify"
    assert current is None


# ─── write_snapshot with a precomputed snapshot ────────────────────────────────


def test_write_snapshot_accepts_precomputed_snapshot(tmp_path: Path) -> None:
    skill_root = _make_skill_root(tmp_path)
    workspace_root = _make_workspace(tmp_path, _bare_workspace_text())

    precomputed = snapshot.compute_snapshot(workspace_root, skill_root=skill_root)
    json_path = snapshot.write_snapshot(
        workspace_root, "FT-1", skill_root=skill_root, snapshot=precomputed
    )

    stored = json.loads(json_path.read_text(encoding="utf-8"))
    assert stored == precomputed
    sha = snapshot.snapshot_sha_path(workspace_root, "FT-1").read_text(encoding="utf-8").strip()
    assert sha == precomputed["master_hash"]

    ok, detail = snapshot.verify_snapshot(workspace_root, "FT-1", skill_root=skill_root)
    assert ok is True
    assert detail == "match"


# ─── Engine component (flow-2pp) ─────────────────────────────────────────────
# The canonical snapshot gains an "engine" component: a tree hash over the MAIN checkout's skill
# tree (resolved via `git worktree list`, invocation-path independent), active ONLY when that
# checkout sits on a protected branch, exactly the marketplace-tracks-main window where a mid-run
# `git pull` + `claude plugin marketplace update` swaps engine code under a running pipeline.


def _git(cwd: Path, *args: str) -> None:
    import subprocess

    subprocess.run(
        ["git", "-c", "user.email=t@t.t", "-c", "user.name=t", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )


def _make_engine_checkout(tmp_path: Path, branch: str = "main") -> tuple[Path, Path]:
    """Real git repo shaped like the flow repo (skill tree under plugins/...).

    Returns (repo_root, skill_root).
    """
    repo = tmp_path / "mainco"
    skill = repo / "plugins" / "flow" / "skills" / "flow"
    _write(snapshot.stage_registry_path(skill), _stage_registry_text())
    _write(skill / "scripts" / "engine.py", "X = 1\n")
    _write(skill / "SKILL.md", "# skill\n")
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", branch)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    return repo, skill


def test_engine_active_on_protected_branch(tmp_path: Path) -> None:
    _repo, skill = _make_engine_checkout(tmp_path, branch="main")
    workspace_root = _make_workspace(tmp_path, _bare_workspace_text())
    snap = snapshot.compute_snapshot(workspace_root, skill_root=skill)
    assert snap["engine"]["branch"] == "main"
    assert snap["engine"]["tree_hash"]


def test_engine_active_on_dev_branch(tmp_path: Path) -> None:
    _repo, skill = _make_engine_checkout(tmp_path, branch="dev")
    workspace_root = _make_workspace(tmp_path, _bare_workspace_text())
    snap = snapshot.compute_snapshot(workspace_root, skill_root=skill)
    assert snap["engine"]["branch"] == "dev"


def test_engine_inactive_on_feature_branch(tmp_path: Path) -> None:
    _repo, skill = _make_engine_checkout(tmp_path, branch="feature/x")
    workspace_root = _make_workspace(tmp_path, _bare_workspace_text())
    snap = snapshot.compute_snapshot(workspace_root, skill_root=skill)
    assert snap["engine"] == {}


def test_engine_inactive_outside_git(tmp_path: Path) -> None:
    skill_root = _make_skill_root(tmp_path)
    workspace_root = _make_workspace(tmp_path, _bare_workspace_text())
    snap = snapshot.compute_snapshot(workspace_root, skill_root=skill_root)
    assert snap["engine"] == {}
    snapshot.write_snapshot(workspace_root, "FT-1", skill_root=skill_root)
    ok, _ = snapshot.verify_snapshot(workspace_root, "FT-1", skill_root=skill_root)
    assert ok


def test_engine_drift_detected_and_named(tmp_path: Path) -> None:
    _repo, skill = _make_engine_checkout(tmp_path, branch="main")
    workspace_root = _make_workspace(tmp_path, _bare_workspace_text())
    snapshot.write_snapshot(workspace_root, "FT-1", skill_root=skill)
    _write(skill / "scripts" / "engine.py", "X = 2\n")
    ok, detail = snapshot.verify_snapshot(workspace_root, "FT-1", skill_root=skill)
    assert not ok
    assert "engine" in detail


def test_engine_excludes_stage_registry(tmp_path: Path) -> None:
    # stage-registry.toml is already its own component; the engine hash skips it
    # so one edit never lights two components.
    _repo, skill = _make_engine_checkout(tmp_path, branch="main")
    workspace_root = _make_workspace(tmp_path, _bare_workspace_text())
    before = snapshot.compute_snapshot(workspace_root, skill_root=skill)
    _write(snapshot.stage_registry_path(skill), _stage_registry_text() + "\n# edited\n")
    after = snapshot.compute_snapshot(workspace_root, skill_root=skill)
    assert before["engine"] == after["engine"]
    comps = snapshot.drifted_components(before, after)
    assert comps == ["stage_registry"]


def test_engine_anchors_on_main_root_from_worktree_copy(tmp_path: Path) -> None:
    # invocation-path independence: computing from a linked worktree's skill
    # copy hashes the MAIN checkout's engine tree, and worktree-local edits
    # (run-private) do not perturb it.
    repo, skill = _make_engine_checkout(tmp_path, branch="main")
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "feature/run", str(wt))
    skill_wt = wt / "plugins" / "flow" / "skills" / "flow"
    workspace_root = _make_workspace(tmp_path, _bare_workspace_text())
    from_main = snapshot.compute_snapshot(workspace_root, skill_root=skill)
    from_wt = snapshot.compute_snapshot(workspace_root, skill_root=skill_wt)
    assert from_main["engine"] == from_wt["engine"]
    _write(skill_wt / "scripts" / "engine.py", "X = 3\n")
    after_wt_edit = snapshot.compute_snapshot(workspace_root, skill_root=skill_wt)
    assert after_wt_edit["engine"] == from_main["engine"]


def test_component_files_maps_engine_to_none(tmp_path: Path) -> None:
    workspace_root = _make_workspace(tmp_path, _bare_workspace_text())
    skill_root = _make_skill_root(tmp_path)
    out = snapshot.component_files(["engine"], workspace_root=workspace_root, skill_root=skill_root)
    assert out == {"engine": None}


def test_engine_ignores_untracked_files(tmp_path: Path) -> None:
    # machine-local untracked trees in the main checkout (a venv, pytest
    # caches, editor scratch) churn without an engine swap; only git-tracked
    # files feed the hash.
    _repo, skill = _make_engine_checkout(tmp_path, branch="main")
    workspace_root = _make_workspace(tmp_path, _bare_workspace_text())
    before = snapshot.compute_snapshot(workspace_root, skill_root=skill)
    _write(skill / "scripts" / ".venv" / "activate_this.py", "VENV = 1\n")
    _write(skill / ".pytest_cache" / "README.md", "cache\n")
    after = snapshot.compute_snapshot(workspace_root, skill_root=skill)
    assert before["engine"] == after["engine"]
    _write(skill / "scripts" / "engine.py", "X = 9\n")  # tracked file still trips
    swapped = snapshot.compute_snapshot(workspace_root, skill_root=skill)
    assert swapped["engine"] != before["engine"]


def test_snapshot_revision_isolation(tmp_path: Path) -> None:
    # write_snapshot(.., revision="r1") nests under the revision dir; the
    # ticket-level sha is untouched, and no-revision behavior is unchanged.
    skill_root = _make_skill_root(tmp_path)
    workspace_root = _make_workspace(tmp_path, _bare_workspace_text())

    snapshot.write_snapshot(workspace_root, "FT-1", skill_root=skill_root)
    ticket_sha = snapshot.snapshot_sha_path(workspace_root, "FT-1")
    ticket_sha_before = ticket_sha.read_text(encoding="utf-8")

    rev_json = snapshot.write_snapshot(workspace_root, "FT-1", skill_root=skill_root, revision="r1")
    rev_sha = snapshot.snapshot_sha_path(workspace_root, "FT-1", revision="r1")
    assert rev_json == snapshot.snapshot_json_path(workspace_root, "FT-1", revision="r1")
    assert (
        rev_sha == workspace_root / ".flow" / "runs" / "FT-1" / "revisions" / "r1" / "snapshot.sha"
    )
    assert rev_sha.exists()
    # the ticket-level sha is byte-untouched by the revision write
    assert ticket_sha.read_text(encoding="utf-8") == ticket_sha_before

    # classify_drift against the revision baseline sees match; drifting workspace.toml
    # trips the revision baseline (proves the revision sha is the one being read).
    ok, detail = snapshot.verify_snapshot(
        workspace_root, "FT-1", skill_root=skill_root, revision="r1"
    )
    assert ok is True
    assert detail == "match"

    _write(
        workspace_root / ".flow" / "workspace.toml",
        _bare_workspace_text() + "\n# user edit\n",
    )
    ok, detail = snapshot.verify_snapshot(
        workspace_root, "FT-1", skill_root=skill_root, revision="r1"
    )
    assert ok is False
    assert "workspace_toml" in detail


# ─── engine_tree_clean (flow-p9sc) ─────────────────────────────────────────────
# The discriminator for the engine-drift re-anchor: a committed advance leaves
# the engine working tree clean vs its own HEAD; a dirty (uncommitted) tree is
# the only way a genuine mid-run engine mutation manifests and stays fail-closed.


def test_engine_tree_clean_on_committed_checkout(tmp_path: Path) -> None:
    _repo, skill = _make_engine_checkout(tmp_path, branch="main")
    assert snapshot.engine_tree_clean(skill) is True


def test_engine_tree_clean_false_when_dirty(tmp_path: Path) -> None:
    _repo, skill = _make_engine_checkout(tmp_path, branch="main")
    _write(skill / "scripts" / "engine.py", "X = 99\n")  # tracked edit, uncommitted
    assert snapshot.engine_tree_clean(skill) is False


def test_engine_tree_clean_false_outside_git(tmp_path: Path) -> None:
    skill_root = _make_skill_root(tmp_path)
    assert snapshot.engine_tree_clean(skill_root) is False
