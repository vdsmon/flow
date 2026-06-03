import threading

import pytest

from machinery_edit import apply_edit


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
    assert f.read_text() == "dup\ndup\n"  # untouched


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
