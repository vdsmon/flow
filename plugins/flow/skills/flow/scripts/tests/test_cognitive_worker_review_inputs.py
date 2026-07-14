from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

import cognitive_workers as cw


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def test_review_bundle_preserves_layers_binary_paths_and_symlinks(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "flow@example.test")
    _git(root, "config", "user.name", "Flow Test")
    (root / "both.bin").write_bytes(b"base\x00")
    (root / "delete.txt").write_text("delete\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "base")

    (root / "both.bin").write_bytes(b"staged\x00")
    _git(root, "add", "both.bin")
    (root / "both.bin").write_bytes(b"unstaged\xff")
    (root / "delete.txt").unlink()
    weird = root / "line\nbreak.bin"
    weird.write_bytes(b"\x00\xff")
    os.symlink("both.bin", root / "link")

    output = tmp_path / "bundle"
    receipt = cw.build_review_input_bundle(root, output)
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert receipt["schema"] == "flow.review-input-bundle/v1"
    assert manifest["layers"]["staged"]["patch_digest"]
    assert manifest["layers"]["worktree"]["patch_digest"]
    staged_paths = manifest["layers"]["staged"]["changes"]
    worktree_paths = manifest["layers"]["worktree"]["changes"]
    assert any(item["path"]["path"] == "both.bin" for item in staged_paths)
    assert any(item["path"]["path"] == "both.bin" for item in worktree_paths)
    assert any(item["path_encoding"] == "base64" for item in manifest["untracked"])
    assert any(item["kind"] == "symlink" for item in manifest["untracked"])
    assert (output.stat().st_mode & 0o222) == 0
    assert _git(root, "status", "--porcelain")


def _repository(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "flow@example.test")
    _git(root, "config", "user.name", "Flow Test")
    (root / "base.txt").write_text("base\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "base")
    return root


def test_review_bundle_never_applies_a_patch_or_touches_the_index(tmp_path: Path) -> None:
    source = Path(cw.__file__).read_text(encoding="utf-8")
    assert "git apply" not in source
    assert '"apply"' not in source

    root = _repository(tmp_path)
    (root / "staged.txt").write_text("staged\n", encoding="utf-8")
    _git(root, "add", "staged.txt")
    (root / "base.txt").write_text("dirty\n", encoding="utf-8")
    status_before = _git(root, "status", "--porcelain")
    staged_before = _git(root, "diff", "--cached", "--name-status")

    receipt_before = cw.git_receipt(root)
    cw.build_review_input_bundle(root, tmp_path / "bundle")

    assert cw.git_receipt(root)["digest"] == receipt_before["digest"]
    assert _git(root, "status", "--porcelain") == status_before
    assert _git(root, "diff", "--cached", "--name-status") == staged_before
    assert (root / "base.txt").read_text(encoding="utf-8") == "dirty\n"


def test_review_bundle_rejects_a_repository_that_changes_during_capture(
    tmp_path: Path, monkeypatch
) -> None:
    root = _repository(tmp_path)
    real = cw.git_receipt
    calls = {"n": 0}

    def racing(target: Path) -> dict[str, object]:
        calls["n"] += 1
        if calls["n"] == 2:
            (root / "raced.txt").write_text("raced\n", encoding="utf-8")
        return real(target)

    monkeypatch.setattr(cw, "git_receipt", racing)
    with pytest.raises(cw.WorkerFailure, match="changed while review evidence") as error:
        cw.build_review_input_bundle(root, tmp_path / "bundle")
    assert error.value.code == "baseline_mismatch"
    assert not (tmp_path / "bundle").exists()


def test_review_bundle_rejects_an_in_place_tracked_rewrite_during_capture(
    tmp_path: Path, monkeypatch
) -> None:
    """The rewritten file is already modified, so its porcelain=v2 record never moves."""
    root = _repository(tmp_path)
    (root / "base.txt").write_text("dirty-a\n", encoding="utf-8")
    status_before = _git(root, "status", "--porcelain=v2")
    real = cw.git_receipt
    calls = {"n": 0}

    def racing(target: Path) -> dict[str, object]:
        calls["n"] += 1
        if calls["n"] == 2:
            (root / "base.txt").write_text("dirty-b\n", encoding="utf-8")
        return real(target)

    monkeypatch.setattr(cw, "git_receipt", racing)
    with pytest.raises(cw.WorkerFailure, match="changed while review evidence") as error:
        cw.build_review_input_bundle(root, tmp_path / "bundle")

    assert error.value.code == "baseline_mismatch"
    assert _git(root, "status", "--porcelain=v2") == status_before
    assert not (tmp_path / "bundle").exists()


def test_review_bundle_rejects_an_untracked_rewrite_during_capture(
    tmp_path: Path, monkeypatch
) -> None:
    """The untracked blobs are read inside the receipt bracket, so the rewrite is caught."""
    root = _repository(tmp_path)
    (root / "note.log").write_text("untracked-a\n", encoding="utf-8")
    real = cw.git_receipt
    calls = {"n": 0}

    def racing(target: Path) -> dict[str, object]:
        calls["n"] += 1
        if calls["n"] == 2:
            (root / "note.log").write_text("untracked-b-much-longer\n", encoding="utf-8")
        return real(target)

    monkeypatch.setattr(cw, "git_receipt", racing)
    with pytest.raises(cw.WorkerFailure, match="changed while review evidence") as error:
        cw.build_review_input_bundle(root, tmp_path / "bundle")

    assert error.value.code == "baseline_mismatch"
    assert not (tmp_path / "bundle").exists()


def test_review_bundle_refuses_a_special_file(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    os.mkfifo(root / "pipe")
    blobs = tmp_path / "blobs"
    blobs.mkdir()

    with pytest.raises(cw.WorkerFailure, match="special file"):
        cw._path_payload(root, b"pipe", blobs, 1024)

    # Git itself never reports a FIFO as untracked, so the bundle publishes without it.
    cw.build_review_input_bundle(root, tmp_path / "bundle")
    manifest = json.loads((tmp_path / "bundle" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["untracked"] == []


def test_review_bundle_records_deletes_renames_and_mode_changes(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    (root / "moved.txt").write_text("move me\n", encoding="utf-8")
    (root / "chmod.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "second")

    _git(root, "mv", "moved.txt", "renamed.txt")
    _git(root, "rm", "-q", "base.txt")
    (root / "chmod.sh").chmod(0o755)
    _git(root, "add", "chmod.sh")

    cw.build_review_input_bundle(root, tmp_path / "bundle")
    manifest = json.loads((tmp_path / "bundle" / "manifest.json").read_text(encoding="utf-8"))
    staged = {item["path"]["path"]: item for item in manifest["layers"]["staged"]["changes"]}

    assert staged["base.txt"]["status"].startswith("D")
    assert staged["moved.txt"]["status"].startswith(("D", "R"))
    assert staged["chmod.sh"]["old_mode"] == "100644"
    assert staged["chmod.sh"]["new_mode"] == "100755"


def test_patch_layers_ignore_an_external_diff_driver(tmp_path: Path) -> None:
    """An external driver replaces the patch and nullifies --binary, dropping binary content."""
    root = _repository(tmp_path)
    (root / "image.bin").write_bytes(b"\x00base\xff")
    _git(root, "add", "image.bin")
    _git(root, "commit", "-qm", "binary")

    driver = tmp_path / "external-diff"
    driver.write_text("#!/bin/sh\necho EXTERNAL-DIFF-RAN\n", encoding="utf-8")
    driver.chmod(0o755)
    _git(root, "config", "diff.external", str(driver))

    (root / "image.bin").write_bytes(b"\x00staged\xff")
    _git(root, "add", "image.bin")
    (root / "base.txt").write_text("worktree\n", encoding="utf-8")

    cw.build_review_input_bundle(root, tmp_path / "bundle")
    staged = (tmp_path / "bundle" / "raw" / "staged.patch").read_bytes()
    worktree = (tmp_path / "bundle" / "raw" / "worktree.patch").read_bytes()

    assert b"EXTERNAL-DIFF-RAN" not in staged
    assert b"EXTERNAL-DIFF-RAN" not in worktree
    assert staged.startswith(b"diff --git ")
    assert b"GIT binary patch" in staged
    assert worktree.startswith(b"diff --git ")
    assert b"@@ " in worktree


def test_untracked_blob_bytes_survive_the_round_trip(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    payload = bytes(range(256)) * 4
    (root / "raw.bin").write_bytes(payload)

    cw.build_review_input_bundle(root, tmp_path / "bundle")
    manifest = json.loads((tmp_path / "bundle" / "manifest.json").read_text(encoding="utf-8"))
    entry = next(item for item in manifest["untracked"] if item["path"] == "raw.bin")

    blob = tmp_path / "bundle" / "blobs" / entry["sha256"]
    assert blob.read_bytes() == payload
    assert entry["length"] == len(payload)
    assert entry["kind"] == "file"


def test_review_bundle_fails_closed_on_its_size_policy(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    (root / "big.bin").write_bytes(b"\x00" * 4096)

    with pytest.raises(cw.WorkerFailure, match="byte limit"):
        cw.build_review_input_bundle(root, tmp_path / "bundle", max_bytes=16)
    with pytest.raises(cw.WorkerFailure, match="untracked-file limit"):
        cw.build_review_input_bundle(root, tmp_path / "bundle", max_files=0)
    assert not (tmp_path / "bundle").exists()


def test_review_bundle_rejects_an_oversized_file_without_reading_it(
    tmp_path: Path, monkeypatch
) -> None:
    root = _repository(tmp_path)
    huge = root / "huge.bin"
    with huge.open("wb") as handle:
        handle.truncate(64 * 1024 * 1024)
    reads: list[Path] = []
    real_read = Path.read_bytes

    def watched(self: Path) -> bytes:
        reads.append(self)
        return real_read(self)

    monkeypatch.setattr(Path, "read_bytes", watched)
    with pytest.raises(cw.WorkerFailure, match="byte limit"):
        cw.build_review_input_bundle(root, tmp_path / "bundle", max_bytes=4096)

    assert huge not in reads
    assert not (tmp_path / "bundle").exists()


def test_review_bundle_is_immutable_once_published(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    (root / "note.txt").write_text("note\n", encoding="utf-8")
    receipt = cw.build_review_input_bundle(root, tmp_path / "bundle")

    manifest_path = tmp_path / "bundle" / "manifest.json"
    assert (manifest_path.stat().st_mode & 0o222) == 0
    with pytest.raises(cw.WorkerFailure, match="already exists"):
        cw.build_review_input_bundle(root, tmp_path / "bundle")
    assert cw._input_digest(tmp_path / "bundle") == receipt["digest"]


def test_a_git_stat_cache_refresh_is_not_a_repository_change(tmp_path: Path) -> None:
    """Git rewrites .git/index to refresh its stat cache; that is not a mutation."""
    root = _repository(tmp_path)
    before = cw.git_receipt(root)
    index_bytes = (root / ".git" / "index").read_bytes()

    os.utime(root / "base.txt", (0, 0))
    _git(root, "status")
    _git(root, "update-index", "--refresh")

    assert (root / ".git" / "index").read_bytes() != index_bytes
    assert cw.git_receipt(root)["digest"] == before["digest"]

    (root / "base.txt").write_text("mutated\n", encoding="utf-8")
    assert cw.git_receipt(root)["digest"] != before["digest"]


def test_index_flags_cannot_hide_a_tracked_file_rewrite(tmp_path: Path) -> None:
    """`update-index --assume-unchanged` hides a rewrite from ls-files --stage and status."""
    root = _repository(tmp_path)
    before = cw.git_receipt(root)["digest"]

    _git(root, "update-index", "--assume-unchanged", "base.txt")
    (root / "base.txt").write_text("MALICIOUS PAYLOAD\n", encoding="utf-8")

    assert cw.git_receipt(root)["digest"] != before

    _git(root, "update-index", "--no-assume-unchanged", "base.txt")
    (root / "base.txt").write_text("base\n", encoding="utf-8")
    assert cw.git_receipt(root)["digest"] == before


def test_an_in_place_tracked_rewrite_is_a_repository_change(tmp_path: Path) -> None:
    """status --porcelain=v2 and ls-files --stage report no worktree content hash."""
    root = _repository(tmp_path)
    (root / "base.txt").write_text("dirty-a\n", encoding="utf-8")
    before = cw.git_receipt(root)

    (root / "base.txt").write_text("dirty-b\n", encoding="utf-8")
    after = cw.git_receipt(root)

    assert after["status"] == before["status"]
    assert after["index"] == before["index"]
    assert after["digest"] != before["digest"]


def test_an_injected_git_hook_is_a_repository_change(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    before = cw.git_receipt(root)["digest"]

    (root / ".git" / "hooks" / "pre-commit").write_text("#!/bin/sh\nexfiltrate\n", encoding="utf-8")

    assert cw.git_receipt(root)["digest"] != before
