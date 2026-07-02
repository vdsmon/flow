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
    assert p.read_text(encoding="utf-8").startswith('{"ok": 1}')


def test_iter_jsonl_quarantine_is_idempotent(tmp_path):
    # The main file is never rewritten, so the same bad line is re-read on every
    # pass (append-dedup scan, compact, recall). Re-quarantine must not grow the
    # sidecar without bound.
    p = tmp_path / "k.jsonl"
    q = tmp_path / "k.quarantine"
    p.write_text('{"ok": 1}\nnot json\n[1, 2]\n', encoding="utf-8")
    for _ in range(3):
        assert list(iter_jsonl(p, q)) == [{"ok": 1}]
    lines = q.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    raws = {json.loads(line)["raw"] for line in lines}
    assert raws == {"not json", "[1, 2]"}


def test_iter_jsonl_missing_file(tmp_path):
    assert list(iter_jsonl(tmp_path / "none.jsonl", tmp_path / "q")) == []
