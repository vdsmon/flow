from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

import flow_beads_create as fbc

Recorder = list[tuple[list[str], Path]]


def _marked_ws(tmp_path: Path) -> Path:
    d = tmp_path / "flow"
    (d / ".flow").mkdir(parents=True)
    (d / ".flow" / "workspace.toml").write_text(
        "[maintainer]\nself_target = true\n", encoding="utf-8"
    )
    return d


def _plain_ws(tmp_path: Path) -> Path:
    d = tmp_path / "proj"
    (d / ".flow").mkdir(parents=True)
    (d / ".flow" / "workspace.toml").write_text('[tracker]\nbackend = "beads"\n', encoding="utf-8")
    return d


def _runner(
    returncode: int = 0, stdout: str = '{"id": "flow-x1"}', stderr: str = ""
) -> tuple[Callable[..., subprocess.CompletedProcess[str]], Recorder]:
    calls: Recorder = []

    def run(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        calls.append((args, cwd))
        return subprocess.CompletedProcess(args, returncode, stdout, stderr)

    return run, calls


def test_create_bead_targets_flow_beads(tmp_path):
    repo = _marked_ws(tmp_path)
    run, calls = _runner()
    key = fbc.create_bead(
        repo,
        "title",
        "body",
        type="bug",
        labels=["evolve", "machinery"],
        parent="flow-aut",
        runner=run,
    )
    assert key == "flow-x1"
    args, cwd = calls[0]
    assert cwd == repo.resolve()  # bd runs in the flow repo, not the run's cwd
    assert args[:3] == ["bd", "create", "title"]
    assert "--json" in args
    assert args[args.index("--type") + 1] == "bug"
    assert args[args.index("--labels") + 1] == "evolve,machinery"
    assert args[args.index("--parent") + 1] == "flow-aut"


def test_create_bead_not_maintainer_raises(tmp_path, monkeypatch):
    monkeypatch.setattr("maintainer._global_config_path", lambda: tmp_path / "absent.toml")
    plain = _plain_ws(tmp_path)
    run, calls = _runner()
    with pytest.raises(fbc.NotMaintainer):
        fbc.create_bead(plain, "t", "b", runner=run)
    assert calls == []  # bd never invoked


def test_create_bead_bd_error(tmp_path):
    repo = _marked_ws(tmp_path)
    run, _ = _runner(returncode=1, stderr="boom")
    with pytest.raises(fbc.BeadCreateError):
        fbc.create_bead(repo, "t", "b", runner=run)


def test_create_bead_no_id_does_not_retry(tmp_path):
    repo = _marked_ws(tmp_path)
    run, calls = _runner(stdout="{}")
    with pytest.raises(fbc.BeadCreateError):
        fbc.create_bead(repo, "t", "b", runner=run)
    assert len(calls) == 1  # no duplicate create on a parse miss


def test_cli_not_maintainer_exit_4(tmp_path, monkeypatch):
    monkeypatch.setattr("maintainer._global_config_path", lambda: tmp_path / "absent.toml")
    plain = _plain_ws(tmp_path)
    rc = fbc.cli_main(["--workspace-root", str(plain), "--summary", "t", "--description", "b"])
    assert rc == 4


def _dispatch_runner(
    list_by_label: dict[str, list[dict]] | None = None, create_id: str = "flow-new"
) -> tuple[Callable[..., subprocess.CompletedProcess[str]], Recorder]:
    """Fake runner answering `bd list` PER `-l <label>` and `bd create` distinctly.

    list_by_label maps a label -> the items that label's list returns; unknown
    labels default to []. This lets the exact `evid:` and fuzzy `evidfile:` lists
    return different answers (otherwise the fuzzy tests would be vacuous).
    """
    import json

    by_label = dict(list_by_label or {})
    calls: Recorder = []

    def run(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        calls.append((args, cwd))
        if len(args) >= 2 and args[1] == "list":
            label = args[args.index("-l") + 1] if "-l" in args else ""
            return subprocess.CompletedProcess(args, 0, json.dumps(by_label.get(label, [])), "")
        return subprocess.CompletedProcess(args, 0, json.dumps({"id": create_id}), "")

    return run, calls


def test_fingerprint_is_format_invariant():
    a = fbc.fingerprint("scripts/mise.toml: TY skips hooks")
    b = fbc.fingerprint("scripts-mise-toml-ty-skips-hooks")
    c = fbc.fingerprint("Scripts/Mise.toml   ty skips HOOKS")
    assert a == b == c  # wording/format variance collapses to one key
    assert len(a) == 12
    assert fbc.fingerprint("a-different-finding") != a


_MST_TITLE = (
    "spec --auto Plan subagent told to read pre-bootstrap "
    "ticket.json/tickets.md that do not exist yet"
)
_9JK_TITLE = "spec --auto Plan subagent reads pre-bootstrap .flow run files that do not exist yet"


def test_dedup_new_creates_with_evid_label(tmp_path):
    repo = _marked_ws(tmp_path)
    run, calls = _dispatch_runner()  # all labels list empty
    key = fbc.create_bead(
        repo,
        "t",
        "b",
        dedup_key="references/verb-spec.md::quotepath-bug",
        labels=["evolve"],
        runner=run,
    )
    assert key == "flow-new"
    evid = f"evid:{fbc.fingerprint('references/verb-spec.md::quotepath-bug')}"
    evidfile = f"evidfile:{fbc.fingerprint('verb-spec.md')}"
    list_calls = [c for c in calls if c[0][1] == "list"]
    assert len(list_calls) == 2  # exact then file-anchored
    assert "-l" in list_calls[0][0] and evid == list_calls[0][0][list_calls[0][0].index("-l") + 1]
    assert evidfile == list_calls[1][0][list_calls[1][0].index("-l") + 1]
    create_args = calls[-1][0]
    assert create_args[:2] == ["bd", "create"]
    stamped = create_args[create_args.index("--labels") + 1]
    assert evid in stamped and evidfile in stamped and "evolve" in stamped


def test_dedup_existing_skips_create(tmp_path):
    repo = _marked_ws(tmp_path)
    evid = f"evid:{fbc.fingerprint('quotepath-bug')}"
    run, calls = _dispatch_runner(list_by_label={evid: [{"id": "flow-old"}]})
    with pytest.raises(fbc.DuplicateBead) as ei:
        fbc.create_bead(repo, "t", "b", dedup_key="quotepath-bug", runner=run)
    assert ei.value.existing_key == "flow-old"
    assert len(calls) == 1  # only the exact list check; create never ran


def test_dedup_fuzzy_converges_real_symptoms(tmp_path):
    repo = _marked_ws(tmp_path)
    dedup_key = "references/verb-spec.md::auto-plan-reads-prebootstrap-flow-files"
    evid = f"evid:{fbc.fingerprint(dedup_key)}"
    evidfile = f"evidfile:{fbc.fingerprint('verb-spec.md')}"
    run, calls = _dispatch_runner(
        list_by_label={evid: [], evidfile: [{"id": "flow-mst", "title": _MST_TITLE}]}
    )
    with pytest.raises(fbc.DuplicateBead) as ei:
        fbc.create_bead(repo, _9JK_TITLE, "b", dedup_key=dedup_key, runner=run)
    assert ei.value.existing_key == "flow-mst"
    assert not any(c[0][1] == "create" for c in calls)  # create never ran
    assert len([c for c in calls if c[0][1] == "list"]) == 2  # exact miss then fuzzy hit


def test_dedup_fuzzy_path_variance_still_collides(tmp_path):
    repo = _marked_ws(tmp_path)
    # candidate was filed under a path-prefixed file; new finding uses the bare name
    assert fbc.fingerprint(fbc._basename("references/verb-spec.md")) == fbc.fingerprint(
        fbc._basename("verb-spec.md")
    )
    evidfile = f"evidfile:{fbc.fingerprint('verb-spec.md')}"
    run, _ = _dispatch_runner(list_by_label={evidfile: [{"id": "flow-mst", "title": _MST_TITLE}]})
    with pytest.raises(fbc.DuplicateBead) as ei:
        fbc.create_bead(repo, _9JK_TITLE, "b", dedup_key="verb-spec.md::bare-prefix", runner=run)
    assert ei.value.existing_key == "flow-mst"


def test_dedup_fuzzy_does_not_merge_distinct_same_file(tmp_path):
    repo = _marked_ws(tmp_path)
    distinct = "verb-spec.md bootstrap writes ticket frontmatter to the wrong worktree path"
    evidfile = f"evidfile:{fbc.fingerprint('verb-spec.md')}"
    run, calls = _dispatch_runner(
        list_by_label={evidfile: [{"id": "flow-other", "title": distinct}]}
    )
    key = fbc.create_bead(
        repo, _9JK_TITLE, "b", dedup_key="references/verb-spec.md::sym", runner=run
    )
    assert key == "flow-new"
    assert any(c[0][1] == "create" for c in calls)  # not deduped, create ran


def test_dedup_no_separator_skips_fuzzy(tmp_path):
    repo = _marked_ws(tmp_path)
    run, calls = _dispatch_runner()  # no "::" → exact only
    key = fbc.create_bead(repo, "t", "b", dedup_key="quotepath-bug", runner=run)
    assert key == "flow-new"
    list_calls = [c for c in calls if c[0][1] == "list"]
    assert len(list_calls) == 1  # exact only; no evidfile lookup
    create_args = calls[-1][0]
    stamped = create_args[create_args.index("--labels") + 1]
    assert not any(lbl.startswith("evidfile:") for lbl in stamped.split(","))
