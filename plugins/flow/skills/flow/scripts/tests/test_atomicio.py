import os
import stat

import pytest

from _atomicio import atomic_write_bytes, atomic_write_text

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX file modes only")


def test_atomic_write_text_creates_and_overwrites(tmp_path):
    p = tmp_path / "sub" / "f.txt"
    atomic_write_text(p, "hello")
    assert p.read_text(encoding="utf-8") == "hello"
    atomic_write_text(p, "world")
    assert p.read_text(encoding="utf-8") == "world"


def test_atomic_write_bytes_roundtrip(tmp_path):
    p = tmp_path / "f.bin"
    atomic_write_bytes(p, b"\x00\x01\x02")
    assert p.read_bytes() == b"\x00\x01\x02"


def test_atomic_write_leaves_no_tmp_files(tmp_path):
    p = tmp_path / "f.txt"
    atomic_write_text(p, "x")
    leftovers = [q.name for q in tmp_path.iterdir() if q.name != "f.txt"]
    assert leftovers == []


@posix_only
def test_atomic_write_preserves_existing_mode(tmp_path):
    p = tmp_path / "f.txt"
    atomic_write_text(p, "hello")
    os.chmod(p, 0o664)
    atomic_write_text(p, "world")
    assert stat.S_IMODE(p.stat().st_mode) == 0o664


@posix_only
def test_atomic_write_preserves_restrictive_mode(tmp_path):
    p = tmp_path / "f.txt"
    atomic_write_text(p, "hello")
    os.chmod(p, 0o600)
    atomic_write_text(p, "world")
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


@posix_only
def test_atomic_write_new_file_is_0o644(tmp_path):
    p = tmp_path / "f.txt"
    atomic_write_text(p, "hello")
    assert stat.S_IMODE(p.stat().st_mode) == 0o644


@posix_only
def test_atomic_write_explicit_mode_replaces_existing_mode(tmp_path):
    p = tmp_path / "tool"
    atomic_write_text(p, "old", mode=0o600)
    atomic_write_text(p, "new", mode=0o755)
    assert p.read_text(encoding="utf-8") == "new"
    assert stat.S_IMODE(p.stat().st_mode) == 0o755
