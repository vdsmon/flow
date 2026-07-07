import io
import json
import sys
import threading

import pytest

import machinery_edit
from machinery_edit import _current_branch, _load_payload, apply_edit, main


@pytest.fixture
def skill_root(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "references").mkdir()
    return tmp_path


def _write(p, text):
    p.write_text(text, encoding="utf-8")


def test_apply_unique_anchor(skill_root):
    f = skill_root / "scripts" / "x.py"
    _write(f, "a = 1  # body — fill\nb = 2\n")
    result, code = apply_edit(skill_root, f, "# body — fill", "# body: fill")
    assert code == 0
    assert result["status"] == "applied"
    assert f.read_text() == "a = 1  # body: fill\nb = 2\n"


def test_already_applied_is_idempotent(skill_root):
    f = skill_root / "scripts" / "x.py"
    _write(f, "value = NEW\n")
    result, code = apply_edit(skill_root, f, "value = OLD", "value = NEW")
    assert code == 0
    assert result["status"] == "already_applied"
    assert f.read_text() == "value = NEW\n"


def test_anchor_not_found(skill_root):
    f = skill_root / "scripts" / "x.py"
    _write(f, "unrelated\n")
    result, code = apply_edit(skill_root, f, "OLD", "NEW")
    assert code == 3
    assert result["status"] == "anchor_not_found"


def test_ambiguous_anchor(skill_root):
    f = skill_root / "scripts" / "x.py"
    _write(f, "dup\ndup\n")
    result, code = apply_edit(skill_root, f, "dup", "fixed")
    assert code == 4
    assert result["status"] == "ambiguous"
    assert result["occurrences"] == 2
    assert f.read_text() == "dup\ndup\n"


def test_refuse_path_outside_tree(skill_root, tmp_path):
    outside = tmp_path.parent / "elsewhere.py"
    _write(outside, "OLD\n")
    result, code = apply_edit(skill_root, outside, "OLD", "NEW")
    assert code == 2
    assert result["status"] == "refused"
    assert outside.read_text() == "OLD\n"


def test_refuse_snapshot_pinned_registry(skill_root):
    f = skill_root / "stage-registry.toml"
    _write(f, "OLD\n")
    result, code = apply_edit(skill_root, f, "OLD", "NEW")
    assert code == 2
    assert result["status"] == "refused"
    assert f.read_text() == "OLD\n"


def test_refuse_protected_branch(skill_root):
    f = skill_root / "scripts" / "x.py"
    _write(f, "OLD\n")
    result, code = apply_edit(skill_root, f, "OLD", "NEW", branch_resolver=lambda _: "main")
    assert code == 2
    assert result["status"] == "refused"
    assert "protected branch" in result["reason"]
    assert f.read_text() == "OLD\n"


def test_feature_branch_still_applies(skill_root):
    f = skill_root / "scripts" / "x.py"
    _write(f, "OLD\n")
    result, code = apply_edit(skill_root, f, "OLD", "NEW", branch_resolver=lambda _: "feature/x")
    assert code == 0
    assert result["status"] == "applied"
    assert f.read_text() == "NEW\n"


def test_non_repo_branch_none_still_applies(skill_root):
    f = skill_root / "scripts" / "x.py"
    _write(f, "OLD\n")
    result, code = apply_edit(skill_root, f, "OLD", "NEW", branch_resolver=lambda _: None)
    assert code == 0
    assert result["status"] == "applied"
    assert f.read_text() == "NEW\n"


def test_refuse_detached_head(skill_root):
    f = skill_root / "scripts" / "x.py"
    _write(f, "OLD\n")
    result, code = apply_edit(skill_root, f, "OLD", "NEW", branch_resolver=lambda _: "HEAD")
    assert code == 2
    assert result["status"] == "refused"
    assert "detached" in result["reason"]
    assert f.read_text() == "OLD\n"


def test_refuse_empty_branch(skill_root):
    f = skill_root / "scripts" / "x.py"
    _write(f, "OLD\n")
    result, code = apply_edit(skill_root, f, "OLD", "NEW", branch_resolver=lambda _: "")
    assert code == 2
    assert result["status"] == "refused"
    assert f.read_text() == "OLD\n"


def test_refuse_git_error_sentinel_caller(skill_root):
    f = skill_root / "scripts" / "x.py"
    _write(f, "OLD\n")
    result, code = apply_edit(
        skill_root, f, "OLD", "NEW", branch_resolver=lambda _: machinery_edit._GIT_ERROR
    )
    assert code == 2
    assert result["status"] == "refused"
    assert "failing closed" in result["reason"]
    assert "git failed" in result["reason"]
    assert f.read_text() == "OLD\n"


class _FakeProc:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_branch_read_fails_after_worktree_confirmed_refuses(skill_root, monkeypatch):
    def fake_run(cmd, **kwargs):
        if "--is-inside-work-tree" in cmd:
            return _FakeProc(0, stdout="true\n")
        if "--abbrev-ref" in cmd:
            return _FakeProc(128, stderr="fatal: index file corrupt")
        raise AssertionError(f"unexpected git call: {cmd}")

    monkeypatch.setattr(machinery_edit.subprocess, "run", fake_run)
    assert _current_branch(skill_root) == machinery_edit._GIT_ERROR


def test_probe_oserror_refuses(skill_root, monkeypatch):
    def fake_run(cmd, **kwargs):
        raise OSError("git binary missing")

    monkeypatch.setattr(machinery_edit.subprocess, "run", fake_run)
    assert _current_branch(skill_root) == machinery_edit._GIT_ERROR


def test_probe_unknown_nonzero_refuses(skill_root, monkeypatch):
    def fake_run(cmd, **kwargs):
        if "--is-inside-work-tree" in cmd:
            return _FakeProc(128, stderr="fatal: detected dubious ownership")
        raise AssertionError(f"unexpected git call: {cmd}")

    monkeypatch.setattr(machinery_edit.subprocess, "run", fake_run)
    assert _current_branch(skill_root) == machinery_edit._GIT_ERROR


def test_non_repo_dir_allows(tmp_path):
    assert _current_branch(tmp_path) is None


def test_clean_false_allows(skill_root, monkeypatch):
    def fake_run(cmd, **kwargs):
        if "--is-inside-work-tree" in cmd:
            return _FakeProc(0, stdout="false\n")
        raise AssertionError(f"unexpected git call: {cmd}")

    monkeypatch.setattr(machinery_edit.subprocess, "run", fake_run)
    assert _current_branch(skill_root) is None


def test_empty_old_is_error(skill_root):
    f = skill_root / "scripts" / "x.py"
    _write(f, "x\n")
    _, code = apply_edit(skill_root, f, "", "NEW")
    assert code == 1


def test_old_equals_new_is_error(skill_root):
    f = skill_root / "scripts" / "x.py"
    _write(f, "x\n")
    _, code = apply_edit(skill_root, f, "same", "same")
    assert code == 1


def test_missing_file_is_error(skill_root):
    f = skill_root / "scripts" / "ghost.py"
    _, code = apply_edit(skill_root, f, "OLD", "NEW")
    assert code == 1


def test_file_deleted_between_check_and_read_is_error(skill_root, monkeypatch):
    f = skill_root / "scripts" / "x.py"
    _write(f, "a = 1\n")
    monkeypatch.setattr(
        type(f), "read_text", lambda self, **kw: (_ for _ in ()).throw(FileNotFoundError(self))
    )
    result, code = apply_edit(skill_root, f, "a = 1", "a = 2")
    assert code == 1
    assert result["status"] == "error"
    assert result["reason"] == "file does not exist"


def test_concurrent_writers_no_lost_update(skill_root):
    """N threads each replace a distinct anchor on the SAME file. Without the
    flock + atomic write, read-modify-write interleaving would drop some edits.
    With it, every replacement survives."""
    n = 12
    f = skill_root / "scripts" / "x.py"
    _write(f, "".join(f"line{i}=OLD\n" for i in range(n)))

    barrier = threading.Barrier(n)
    errors: list = []

    def worker(i):
        barrier.wait()  # maximize contention
        try:
            _, code = apply_edit(skill_root, f, f"line{i}=OLD", f"line{i}=NEW")
            assert code == 0
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    final = f.read_text()
    assert "OLD" not in final
    assert final.count("=NEW") == n


def test_load_payload_from_file(tmp_path):
    payload = {"file": "scripts/x.py", "old": "OLD", "new": "NEW"}
    p = tmp_path / "payload.json"
    _write(p, json.dumps(payload))
    assert _load_payload(str(p)) == payload


def test_load_payload_from_stdin(monkeypatch):
    payload = {"file": "scripts/x.py", "old": "OLD", "new": "NEW"}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert _load_payload(None) == payload


def test_load_payload_missing_file_raises(tmp_path):
    ghost = tmp_path / "nope.json"
    with pytest.raises(FileNotFoundError):
        _load_payload(str(ghost))


def test_load_payload_malformed_json_raises(tmp_path):
    p = tmp_path / "bad.json"
    _write(p, "not json {")
    with pytest.raises(json.JSONDecodeError):
        _load_payload(str(p))


def test_main_apply_happy(skill_root, tmp_path):
    target = skill_root / "scripts" / "x.py"
    _write(target, "a = 1  # ANCHOR\nb = 2\n")
    payload = tmp_path / "payload.json"
    _write(payload, json.dumps({"file": str(target), "old": "# ANCHOR", "new": "# FIXED"}))
    code = main(["apply", "--skill-root", str(skill_root), "--payload", str(payload)])
    assert code == 0
    assert target.read_text() == "a = 1  # FIXED\nb = 2\n"


def test_main_bad_payload_missing_file(skill_root, tmp_path):
    ghost = tmp_path / "ghost.json"
    code = main(["apply", "--skill-root", str(skill_root), "--payload", str(ghost)])
    assert code == 1


def test_main_malformed_json_payload(skill_root, tmp_path):
    payload = tmp_path / "bad.json"
    _write(payload, "not json {")
    code = main(["apply", "--skill-root", str(skill_root), "--payload", str(payload)])
    assert code == 1


def test_main_missing_keys(skill_root, tmp_path):
    payload = tmp_path / "payload.json"
    _write(payload, json.dumps({}))
    code = main(["apply", "--skill-root", str(skill_root), "--payload", str(payload)])
    assert code == 1


def test_main_refused_path_outside(skill_root, tmp_path):
    outside = tmp_path.parent / "outside.py"
    _write(outside, "OLD\n")
    payload = tmp_path / "payload.json"
    _write(payload, json.dumps({"file": str(outside), "old": "OLD", "new": "NEW"}))
    code = main(["apply", "--skill-root", str(skill_root), "--payload", str(payload)])
    assert code == 2
    assert outside.read_text() == "OLD\n"
