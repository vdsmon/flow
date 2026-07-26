"""Tests for memory_embed.py (embedder seam + sidecar index).

A STUB embedder command (a tiny inline python script emitting deterministic fake
vectors) exercises the real subprocess socket so the suite is dependency-free.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import _memory_paths
import memory_embed
from tests.wsfactory import make_workspace, memory, tracker

# A stub embedder: reads newline texts on stdin, emits a deterministic 4-dim vector
# per line (a hash-bucketed one-hot-ish vector), as JSON. Same contract as the real
# embedder dependency.
_STUB = (
    "import sys, json\n"
    "texts=[l.rstrip(chr(10)) for l in sys.stdin.read().splitlines()]\n"
    "def vec(t):\n"
    "    v=[0.0,0.0,0.0,0.0]\n"
    "    for w in t.split():\n"
    "        v[sum(map(ord,w))%4]+=1.0\n"
    "    return v\n"
    "sys.stdout.write(json.dumps([vec(t) for t in texts]))\n"
)


@pytest.fixture
def stub_cmd(tmp_path: Path) -> str:
    p = tmp_path / "stub_embedder.py"
    p.write_text(_STUB, encoding="utf-8")
    return f"{sys.executable} {p}"


def _seed_workspace(root: Path, namespace: str = "demo") -> None:
    make_workspace(root, tracker("jira"), memory(namespace))


def _write_entries(root: Path, namespace: str, entries: list[dict]) -> Path:
    kpath = _memory_paths.knowledge_path(root, namespace)
    kpath.parent.mkdir(parents=True, exist_ok=True)
    with kpath.open("w", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e, sort_keys=True) + "\n")
    return kpath


def _entry(id_: str, body: str, **kw) -> dict:
    return {"id": id_, "type": "LEARNED", "branch": "main", "ticket": "FT-1", "body": body, **kw}


# ─── embed contract ────────────────────────────────────────────────────────────


def test_embed_returns_vectors_via_subprocess(stub_cmd: str) -> None:
    vectors = memory_embed.embed(["foo bar", "baz"], embedder=stub_cmd)
    assert len(vectors) == 2
    assert all(isinstance(v, list) and len(v) == 4 for v in vectors)


def test_embed_empty_input_no_shell(stub_cmd: str) -> None:
    assert memory_embed.embed([], embedder=stub_cmd) == []


@pytest.mark.parametrize(
    ("stub_source", "texts"),
    [
        pytest.param(None, ["x"], id="missing_command"),
        pytest.param("import sys; sys.exit(1)\n", ["x"], id="nonzero_exit"),
        pytest.param("print('not json')\n", ["x"], id="unparseable_stdout"),
        pytest.param("import json; print(json.dumps([[1.0]]))\n", ["a", "b"], id="wrong_count"),
    ],
)
def test_embed_raises_unavailable(
    tmp_path: Path, stub_source: str | None, texts: list[str]
) -> None:
    if stub_source is None:
        embedder = "/nonexistent/embedder-binary-xyz"
    else:
        stub = tmp_path / "stub.py"
        stub.write_text(stub_source, encoding="utf-8")
        embedder = f"{sys.executable} {stub}"
    with pytest.raises(memory_embed._EmbedderUnavailable):
        memory_embed.embed(texts, embedder=embedder)


# ─── index build ───────────────────────────────────────────────────────────────


def test_reindex_builds_sidecar(tmp_path: Path, stub_cmd: str) -> None:
    _seed_workspace(tmp_path)
    _write_entries(tmp_path, "demo", [_entry("a" * 16, "fsync durability"), _entry("b" * 16, "x")])
    summary = memory_embed.reindex(tmp_path, "demo", embedder=stub_cmd)
    assert summary["live"] == 2
    assert summary["embedded"] == 2
    path = memory_embed.embed_index_path(tmp_path, "demo")
    assert path.exists()
    header, vectors = memory_embed.load_index(tmp_path, "demo")
    assert header["dim"] == 4
    assert set(vectors) == {"a" * 16, "b" * 16}


def test_reindex_incremental_embeds_only_missing(tmp_path: Path, stub_cmd: str) -> None:
    _seed_workspace(tmp_path)
    _write_entries(tmp_path, "demo", [_entry("a" * 16, "one")])
    memory_embed.reindex(tmp_path, "demo", embedder=stub_cmd)
    _write_entries(tmp_path, "demo", [_entry("a" * 16, "one"), _entry("b" * 16, "two")])
    summary = memory_embed.reindex(tmp_path, "demo", embedder=stub_cmd)
    assert summary["embedded"] == 1
    assert summary["kept"] == 1


def test_reindex_second_run_embeds_zero(tmp_path: Path, stub_cmd: str) -> None:
    _seed_workspace(tmp_path)
    _write_entries(tmp_path, "demo", [_entry("a" * 16, "one"), _entry("b" * 16, "two")])
    memory_embed.reindex(tmp_path, "demo", embedder=stub_cmd)
    summary = memory_embed.reindex(tmp_path, "demo", embedder=stub_cmd)
    assert summary["embedded"] == 0
    assert summary["kept"] == 2


def test_reindex_supersede_filtered_candidate_set(tmp_path: Path, stub_cmd: str) -> None:
    _seed_workspace(tmp_path)
    _write_entries(
        tmp_path,
        "demo",
        [
            _entry("a" * 16, "stale claim"),
            _entry("b" * 16, "fresh claim", supersedes="a" * 16),
        ],
    )
    summary = memory_embed.reindex(tmp_path, "demo", embedder=stub_cmd)
    assert summary["live"] == 1
    _, vectors = memory_embed.load_index(tmp_path, "demo")
    assert set(vectors) == {"b" * 16}
    assert "a" * 16 not in vectors


def test_reindex_drops_dead_id_on_supersede(tmp_path: Path, stub_cmd: str) -> None:
    _seed_workspace(tmp_path)
    _write_entries(tmp_path, "demo", [_entry("a" * 16, "live")])
    memory_embed.reindex(tmp_path, "demo", embedder=stub_cmd)
    _write_entries(
        tmp_path,
        "demo",
        [_entry("a" * 16, "live"), _entry("b" * 16, "newer", supersedes="a" * 16)],
    )
    memory_embed.reindex(tmp_path, "demo", embedder=stub_cmd)
    _, vectors = memory_embed.load_index(tmp_path, "demo")
    assert set(vectors) == {"b" * 16}


def test_reindex_model_mismatch_forces_full_rebuild(tmp_path: Path, stub_cmd: str) -> None:
    _seed_workspace(tmp_path)
    _write_entries(tmp_path, "demo", [_entry("a" * 16, "one"), _entry("b" * 16, "two")])
    memory_embed.reindex(tmp_path, "demo", model="model-A", embedder=stub_cmd)
    # a different configured model invalidates the header → full rebuild.
    summary = memory_embed.reindex(tmp_path, "demo", model="model-B", embedder=stub_cmd)
    assert summary["full"] is True
    assert summary["embedded"] == 2
    assert summary["kept"] == 0
    header, _ = memory_embed.load_index(tmp_path, "demo")
    assert header["model"] == "model-B"


def test_reindex_empty_corpus(tmp_path: Path, stub_cmd: str) -> None:
    _seed_workspace(tmp_path)
    summary = memory_embed.reindex(tmp_path, "demo", embedder=stub_cmd)
    assert summary["live"] == 0
    assert summary["embedded"] == 0


# ─── load_index tolerance ────────────────────────────────────────────────────


def test_load_index_absent_returns_empty(tmp_path: Path) -> None:
    header, vectors = memory_embed.load_index(tmp_path, "demo")
    assert header == {}
    assert vectors == {}


# ─── CLI ─────────────────────────────────────────────────────────────────────


def test_cli_reindex(tmp_path: Path, stub_cmd: str, capsys: pytest.CaptureFixture[str]) -> None:
    _seed_workspace(tmp_path)
    _write_entries(tmp_path, "demo", [_entry("a" * 16, "one")])
    rc = memory_embed.cli_main(
        ["reindex", "--workspace-root", str(tmp_path), "--embedder", stub_cmd]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["live"] == 1


def test_cli_reindex_full_flag(
    tmp_path: Path, stub_cmd: str, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_workspace(tmp_path)
    _write_entries(tmp_path, "demo", [_entry("a" * 16, "one")])
    rc = memory_embed.cli_main(
        ["reindex", "--workspace-root", str(tmp_path), "--full", "--embedder", stub_cmd]
    )
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["full"] is True


def test_cli_reindex_no_workspace_exit_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = memory_embed.cli_main(["reindex", "--workspace-root", str(tmp_path)])
    assert rc == 1
    assert "workspace.toml" in capsys.readouterr().err


def test_cli_reindex_embedder_unavailable_exit_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_workspace(tmp_path)
    _write_entries(tmp_path, "demo", [_entry("a" * 16, "one")])
    rc = memory_embed.cli_main(
        ["reindex", "--workspace-root", str(tmp_path), "--embedder", "/nonexistent/xyz"]
    )
    assert rc == 2


def test_cli_embed_stdin(
    tmp_path: Path,
    stub_cmd: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO("foo bar\nbaz\n"))
    rc = memory_embed.cli_main(["embed", "--embedder", stub_cmd])
    assert rc == 0
    vectors = json.loads(capsys.readouterr().out)
    assert len(vectors) == 2
