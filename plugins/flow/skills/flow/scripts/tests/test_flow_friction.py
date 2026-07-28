"""Contract tests for flow_friction.py, append-only friction log."""

from __future__ import annotations

import fcntl
import json
import multiprocessing
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import _memory_paths
import flow_friction
import recall
from tests.wsfactory import make_workspace, memory, tracker


def _seed_workspace(root: Path, namespace: str = "demo") -> None:
    make_workspace(root, tracker("jira"), memory(namespace))


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def test_append_returns_entry(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    entry = flow_friction.append(
        tmp_path, "FT-1", "run0001", "implement", "RECONCILE", "expanded planned_files"
    )
    assert entry["type"] == "RECONCILE"
    assert entry["ticket"] == "FT-1"
    assert entry["run_id"] == "run0001"
    assert entry["stage"] == "implement"
    assert entry["severity"] == "major"
    assert entry["id"]
    assert "detail" not in entry  # omitted when not provided


def test_append_writes_jsonl_line(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    flow_friction.append(tmp_path, "FT-1", "r", "ticket", "DRIFT", "config changed", detail="x")
    fpath = _memory_paths.friction_path(tmp_path, "demo")
    rows = _read_jsonl(fpath)
    assert len(rows) == 1
    assert rows[0]["type"] == "DRIFT"
    assert rows[0]["detail"] == "x"


def test_append_accumulates_no_dedup(tmp_path: Path) -> None:
    # identical events are distinct entries (no dedup): both land.
    _seed_workspace(tmp_path)
    flow_friction.append(tmp_path, "FT-1", "r", "implement", "RETRY", "same")
    flow_friction.append(tmp_path, "FT-1", "r", "implement", "RETRY", "same")
    rows = _read_jsonl(_memory_paths.friction_path(tmp_path, "demo"))
    assert len(rows) == 2
    assert rows[0]["id"] != rows[1]["id"]


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"type_": "BOGUS"}, id="invalid_type"),
        pytest.param({"type_": "RETRY", "severity": "loud"}, id="invalid_severity"),
    ],
)
def test_invalid_enum_raises(tmp_path: Path, kwargs: dict[str, str]) -> None:
    _seed_workspace(tmp_path)
    with pytest.raises(flow_friction._InvalidType):
        flow_friction.append(tmp_path, "FT-1", "r", "implement", body="x", **kwargs)


def test_cli_happy_path(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    rc = flow_friction.cli_main(
        [
            "--ticket",
            "FT-1",
            "--run-id",
            "r",
            "--stage",
            "create_pr",
            "--type",
            "MISSING_TOOL",
            "--body",
            "skill ship-it not installed",
            "--workspace-root",
            str(tmp_path),
        ]
    )
    assert rc == 0
    rows = _read_jsonl(_memory_paths.friction_path(tmp_path, "demo"))
    assert rows[0]["type"] == "MISSING_TOOL"


def test_cli_invalid_type_returns_3(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    rc = flow_friction.cli_main(
        [
            "--ticket",
            "FT-1",
            "--run-id",
            "r",
            "--stage",
            "x",
            "--type",
            "NOPE",
            "--body",
            "b",
            "--workspace-root",
            str(tmp_path),
        ]
    )
    assert rc == 3


# ─── Concurrency: friction flock contention → exit 2 ───────────────────────────


def _hold_friction_lock(lock_path_str: str, acquired_evt: Any, release_evt: Any) -> None:
    """Top-level so multiprocessing can pickle it on macOS spawn-start.

    Holds an exclusive flock on the friction lock file, signals once held, and
    waits for release. While held, cli_main's flock_retry exhausts and returns 2.
    """
    fd = os.open(lock_path_str, os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    acquired_evt.set()
    release_evt.wait(timeout=30)
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)


def test_cli_lock_contention_returns_2(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    ns = _memory_paths.resolve_namespace(tmp_path)
    lock_path = _memory_paths.friction_lock_path(tmp_path, ns)
    # O_CREAT does not create parent dirs; the holder runs before append()'s mkdir.
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    ctx = multiprocessing.get_context("spawn")
    acquired_evt = ctx.Event()
    release_evt = ctx.Event()
    proc = ctx.Process(target=_hold_friction_lock, args=(str(lock_path), acquired_evt, release_evt))
    proc.start()
    try:
        assert acquired_evt.wait(timeout=10)
        rc = flow_friction.cli_main(
            [
                "--ticket",
                "FT-1",
                "--run-id",
                "r",
                "--stage",
                "implement",
                "--type",
                "RETRY",
                "--body",
                "b",
                "--workspace-root",
                str(tmp_path),
            ]
        )
        assert rc == 2
    finally:
        release_evt.set()
        proc.join(timeout=10)


def test_cli_missing_config_returns_4(tmp_path: Path) -> None:
    # no .flow/workspace.toml seeded
    rc = flow_friction.cli_main(
        [
            "--ticket",
            "FT-1",
            "--run-id",
            "r",
            "--stage",
            "x",
            "--type",
            "RETRY",
            "--body",
            "b",
            "--workspace-root",
            str(tmp_path),
        ]
    )
    assert rc == 4


# ─── plugin_version (self-read, fully guarded) ───────────────────────────────


def _live_plugin_version() -> str:
    path = Path(flow_friction.__file__).resolve().parents[3] / ".claude-plugin" / "plugin.json"
    return json.loads(path.read_text(encoding="utf-8"))["version"]


def test_append_stamps_plugin_version(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    entry = flow_friction.append(tmp_path, "FT-1", "r", "implement", "RETRY", "x")
    live = _live_plugin_version()
    assert isinstance(entry["plugin_version"], str)
    assert entry["plugin_version"]
    assert entry["plugin_version"] == live


def test_append_succeeds_when_plugin_version_guarded_empty(
    tmp_path: Path, monkeypatch: Any
) -> None:
    _seed_workspace(tmp_path)
    monkeypatch.setattr(flow_friction, "plugin_version", lambda: "")
    entry = flow_friction.append(tmp_path, "FT-1", "r", "implement", "RETRY", "x")
    assert entry["plugin_version"] == ""


# ─── related-knowledge echo after the append ─────────────────────────────────

# The stub embedder bins each word by sum(ord(word)) % 4, so two texts whose words all land in one
# bin are collinear (cosine 1.0). `memory_embed._entry_text` prefixes the entry type, and "LEARNED:"
# is itself a bin-1 word, so a bin-1 body stays wholly in bin 1. _BIN3 against a _BIN1 query scores
# 0.3162, under the near-miss floor.
_BIN1 = "isolated atomic embed"
_BIN3 = "fsync worktree write"


def _stub_embedder_cmd(root: Path) -> str:
    """A deterministic 4-dim fake embedder, same contract as the real one."""
    import sys as _sys

    stub = root / "stub_embedder.py"
    stub.write_text(
        "import sys, json\n"
        "texts=[l.rstrip(chr(10)) for l in sys.stdin.read().splitlines()]\n"
        "def vec(t):\n"
        "    v=[0.0,0.0,0.0,0.0]\n"
        "    for w in t.split():\n"
        "        v[sum(map(ord,w))%4]+=1.0\n"
        "    return v\n"
        "sys.stdout.write(json.dumps([vec(t) for t in texts]))\n",
        encoding="utf-8",
    )
    return f"{_sys.executable} {stub}"


def _seed_semantic_workspace(root: Path, bodies: dict[str, str], ticket: str = "FT-OLD") -> None:
    """Workspace with semantic memory on, `bodies` as the corpus, index built.

    `ticket` defaults to a key no friction fixture uses, so the corpus is reachable unless a test
    deliberately collides it with the appending ticket to exercise the exclusion.
    """
    import memory_embed

    embedder = _stub_embedder_cmd(root)
    make_workspace(
        root,
        tracker("jira"),
        memory("demo"),
        {
            "memory.semantic": {
                "enabled": True,
                "model": "stub-model",
                "threshold": 0.0,
                "embedder": embedder,
            }
        },
    )
    kpath = _memory_paths.knowledge_path(root, "demo")
    kpath.parent.mkdir(parents=True, exist_ok=True)
    with kpath.open("w", encoding="utf-8") as fh:
        for eid, body in bodies.items():
            entry = {
                "id": eid,
                "ts": "2026-01-01T00:00:00.000Z",
                "type": "LEARNED",
                "namespace": "demo",
                "branch": "main",
                "ticket": ticket,
                "body": body,
            }
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
    memory_embed.reindex(root, "demo", model="stub-model", embedder=embedder)


def _friction_argv(root: Path, body: str, detail: str | None = None) -> list[str]:
    argv = [
        "--ticket",
        "FT-1",
        "--run-id",
        "r",
        "--stage",
        "implement",
        "--type",
        "MISSING_TOOL",
        "--body",
        body,
        "--workspace-root",
        str(root),
    ]
    if detail is not None:
        argv += ["--detail", detail]
    return argv


def test_cli_prints_related_hits(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed_semantic_workspace(tmp_path, {"a" * 16: _BIN1})
    rc = flow_friction.cli_main(_friction_argv(tmp_path, _BIN1))
    assert rc == 0
    lines = capsys.readouterr().out.splitlines()
    assert "related knowledge (1)" in lines[1]
    assert "a" * 16 in lines[2]
    assert lines[3] == _BIN1


def test_cli_prints_nothing_extra_without_hits(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_semantic_workspace(tmp_path, {"a" * 16: _BIN3})
    rc = flow_friction.cli_main(_friction_argv(tmp_path, _BIN1))
    assert rc == 0
    assert len(capsys.readouterr().out.splitlines()) == 1


def test_cli_excludes_knowledge_written_by_the_appending_ticket(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The CLI must pass `exclude_ticket`, not merely have `similar_entries` support it.

    Every other CLI fixture seeds the corpus under `FT-OLD` while appending as `FT-1`, so the two
    can never collide and dropping the kwarg at the call site changes nothing. Colliding them is
    what makes the wiring observable: this body is collinear with the entry, so without the
    exclusion the run is handed back knowledge it wrote itself.
    """
    _seed_semantic_workspace(tmp_path, {"a" * 16: _BIN1}, ticket="FT-1")
    rc = flow_friction.cli_main(_friction_argv(tmp_path, _BIN1))
    assert rc == 0
    assert len(capsys.readouterr().out.splitlines()) == 1


def test_cli_detail_joins_the_recall_query(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--detail` is part of what the entry says, so it has to be part of what is looked up.

    The body alone is bin-3 against a bin-1 entry and scores 0.0; the detail carries the whole
    signal. The first call is the control that proves the second one's hit comes from the detail and
    not from the body.
    """
    _seed_semantic_workspace(tmp_path, {"a" * 16: _BIN1})
    assert flow_friction.cli_main(_friction_argv(tmp_path, "fsync")) == 0
    assert len(capsys.readouterr().out.splitlines()) == 1

    assert flow_friction.cli_main(_friction_argv(tmp_path, "fsync", detail=_BIN1)) == 0
    assert "related knowledge (1)" in capsys.readouterr().out.splitlines()[1]


def test_cli_renders_every_hit_best_first(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Rendering a multi-hit list is its own behavior: the count in the header and the order.

    Every other CLI fixture seeds exactly one entry, which cannot tell a top-n of 3 from 1 nor a
    real count from a hardcoded one. The corpus is written in an order that is not score order, so
    passing requires the sort rather than the insertion order.
    """
    _seed_semantic_workspace(
        tmp_path,
        {
            "a" * 16: f"{_BIN1} fsync worktree",  # 0.8944
            "b" * 16: _BIN1,  # 1.0
            "c" * 16: f"{_BIN1} fsync",  # 0.9701
        },
    )
    rc = flow_friction.cli_main(_friction_argv(tmp_path, _BIN1))
    assert rc == 0
    lines = capsys.readouterr().out.splitlines()
    assert "related knowledge (3)" in lines[1]
    ranked = [ln.split()[1] for ln in lines if ln.startswith("[")]
    assert ranked == ["b" * 16, "c" * 16, "a" * 16]


def test_cli_first_stdout_line_is_the_appended_jsonl_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_semantic_workspace(tmp_path, {"a" * 16: _BIN1})
    rc = flow_friction.cli_main(_friction_argv(tmp_path, _BIN1))
    assert rc == 0
    out = capsys.readouterr().out.splitlines()
    appended = (
        _memory_paths.friction_path(tmp_path, "demo").read_text(encoding="utf-8").splitlines()
    )
    assert out[0] == appended[-1]
    assert len(out) > 1  # the echo did follow, so line 0 is not the only line there is


def test_cli_record_line_reaches_a_real_pipe_before_the_recall_diagnostics(tmp_path: Path) -> None:
    """The record must reach a PIPED reader first, which capsys structurally cannot check.

    The driver captures this command with the streams merged (`... 2>&1 |`). Through a real pipe
    Python block-buffers stdout while stderr stays unbuffered, so without an explicit flush the
    near-miss diagnostic overtakes a record that was written before it. capsys replaces both streams
    with separate in-memory buffers, so every in-process test here passes either way and the
    ordering is only observable from a subprocess.
    """
    # "LEARNED: isolated fsync worktree" scores 0.7071 against a _BIN1 query: under the floor, so
    # stdout gets no echo, but inside the near-miss band, so stderr gets a line to race with.
    _seed_semantic_workspace(tmp_path, {"a" * 16: "isolated fsync worktree"})
    script = Path(flow_friction.__file__)
    proc = subprocess.run(
        [sys.executable, str(script), *_friction_argv(tmp_path, _BIN1)],
        cwd=script.parent,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=True,
    )
    assert "near-miss" in proc.stdout, "fixture produced no diagnostic for the record to outrun"
    assert json.loads(proc.stdout.splitlines()[0])["ticket"] == "FT-1"


def test_cli_truncates_a_long_related_body(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    long_body = f"{_BIN1} " * 200  # collinear with the query, far past the cap
    assert len(long_body) > flow_friction.RELATED_BODY_CHARS
    _seed_semantic_workspace(tmp_path, {"a" * 16: long_body})
    rc = flow_friction.cli_main(_friction_argv(tmp_path, _BIN1))
    assert rc == 0
    out = capsys.readouterr().out
    assert long_body.strip() not in out
    assert f"truncated at {flow_friction.RELATED_BODY_CHARS} chars" in out
    assert "a" * 16 in out


def test_cli_truncation_pointer_actually_returns_the_entry(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """RUN the fetch pointer the marker prints and assert it returns the entry.

    Two dead pointers shipped behind weaker versions of this test. `--id` was caught by a check that
    the flag exists in the registry; `--ticket` PASSED that same check and was equally dead, because
    `memory search --ticket <key>` carries no query and `recall` exits 1 on an empty one. Flag names
    are the spelling, not the claim. So this parses the query out of the emitted marker and executes
    the command it actually names, which is the only assertion that can tell a working pointer from
    a plausible one.
    """
    long_body = f"{_BIN1} " * 200
    _seed_semantic_workspace(tmp_path, {"a" * 16: long_body})
    assert flow_friction.cli_main(_friction_argv(tmp_path, _BIN1)) == 0
    marker = [line for line in capsys.readouterr().out.splitlines() if "full text:" in line]
    assert marker, "truncated body printed no fetch pointer"

    # command-memory.md maps the public `memory search <query>` onto recall's positional query.
    pointer = marker[0].split("full text:", 1)[1].strip().rstrip("]")
    assert pointer.startswith("FLOW memory search "), f"unrecognized pointer shape: {pointer!r}"
    argv = shlex.split(pointer[len("FLOW memory search ") :])
    assert argv, "the fetch pointer names no query at all"

    assert recall.cli_main([*argv, "--workspace-root", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert out.strip(), "the fetch pointer ran but returned nothing"
    assert "a" * 16 in out, "the fetch pointer ran but did not return the truncated entry"


def test_cli_exits_0_when_related_recall_raises(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: Any
) -> None:
    _seed_workspace(tmp_path)

    def boom(*_args: Any, **_kwargs: Any) -> list[dict]:
        raise RuntimeError("embedder exploded")

    monkeypatch.setattr(recall, "similar_entries", boom)
    rc = flow_friction.cli_main(_friction_argv(tmp_path, "some snag"))
    assert rc == 0
    captured = capsys.readouterr()
    assert "related-recall skipped" in captured.err
    assert len(captured.out.splitlines()) == 1
    rows = _read_jsonl(_memory_paths.friction_path(tmp_path, "demo"))
    assert rows[0]["body"] == "some snag"
