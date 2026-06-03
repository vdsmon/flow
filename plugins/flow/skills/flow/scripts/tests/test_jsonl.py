import json

from _jsonl import iter_jsonl


def test_iter_jsonl_yields_objects(tmp_path):
    p = tmp_path / "k.jsonl"
    q = tmp_path / "k.quarantine"
    p.write_text('{"a": 1}\n\n{"b": 2}\n', encoding="utf-8")
    assert list(iter_jsonl(p, q)) == [{"a": 1}, {"b": 2}]
    assert not q.exists()


def test_iter_jsonl_quarantines_bad_lines(tmp_path):
    p = tmp_path / "k.jsonl"
    q = tmp_path / "k.quarantine"
    p.write_text('{"ok": 1}\nnot json\n[1, 2]\n', encoding="utf-8")
    assert list(iter_jsonl(p, q)) == [{"ok": 1}]
    lines = q.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    recs = [json.loads(line) for line in lines]
    assert recs[0]["raw"] == "not json"
    assert recs[1]["raw"] == "[1, 2]"
    # main file untouched
    assert p.read_text(encoding="utf-8").startswith('{"ok": 1}')


def test_iter_jsonl_missing_file(tmp_path):
    assert list(iter_jsonl(tmp_path / "none.jsonl", tmp_path / "q")) == []
