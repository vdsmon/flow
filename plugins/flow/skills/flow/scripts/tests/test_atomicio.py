from _atomicio import atomic_write_bytes, atomic_write_text


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
