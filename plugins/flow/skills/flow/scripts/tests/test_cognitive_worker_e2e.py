"""Fault suite for the activated disposable-capsule E2E writer (flow-fu7u).

E2E is the first LIVE writer: it launches with a writable sandbox, mutates its capsule,
has those mutations captured as report evidence, imports NOTHING, takes no writer lock,
and always discards the capsule so the authoritative worktree is provably untouched. Each
test fails when its load-bearing source hunk is stripped (verified by surgical revert).
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

import agent_routes
import cognitive_workers as cw


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def _baseline_repo(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "source"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "flow@example.test")
    _git(root, "config", "user.name", "Flow Test")
    (root / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git(root, "add", "tracked.txt")
    _git(root, "commit", "-qm", "base")
    return root, _git(root, "rev-parse", "HEAD")


def _e2e_order(source: Path, sha: str, logical_id: str = "e2e-run-1") -> cw.WorkOrder:
    bundle = source / "tracked.txt"
    return cw.WorkOrder(
        logical_invocation_id=logical_id,
        generation=1,
        profile="e2e",
        source_root=str(source),
        source_sha=sha,
        route={"harness": "codex", "model": "fake", "effort": "medium"},
        route_snapshot_digest="b" * 64,
        input_bundle=str(bundle),
        input_digest=hashlib.sha256(bundle.read_bytes()).hexdigest(),
        facts={
            "stage_e2e": "Run the recipe.",
            "ticket": {"key": "F-1"},
            "source_sha": sha,
            "e2e_recipe": "run the suite",
            "evidence_contract": "rung 1 only",
        },
        run_id="run-1",
        stage="e2e",
        substep="main",
        stage_generation=1,
        lease_fence="fence-1",
    )


def _owner() -> cw.OwnerProof:
    return cw.OwnerProof(owner_id="owner", harness="codex", run_id="run-1", lease_fence="fence-1")


class _E2EAdapter:
    """A capsule worker that writes fixtures/build products, then emits an E2EReport."""

    harness = "codex"

    def __init__(self) -> None:
        self.launches = 0
        self.authorities: list[str] = []

    def preflight(self, route, authority="read_only"):
        return {"executable": "/usr/bin/codex", "version": "codex 1", "harness": "codex"}

    def command(self, route, prompt, schema_path, capsule, authority="read_only"):
        self.launches += 1
        self.authorities.append(authority)
        body = (
            "import json,sys,subprocess,pathlib;"
            "pathlib.Path('build').mkdir(exist_ok=True);"
            "pathlib.Path('build/artifact.bin').write_bytes(bytes(range(64)));"
            "pathlib.Path('tracked.txt').write_text('recipe touched this\\n');"
            "sha=subprocess.run(['git','rev-parse','HEAD'],capture_output=True,text=True)"
            ".stdout.strip();"
            "sys.stdout.write(json.dumps({'result':{'verdict':'pass',"
            "'summary':'recipe green','evidence':'42 passed, 0 failed','source_sha':sha}}))"
        )
        return [sys.executable, "-c", body]

    def session_command(self, *args, **kwargs):
        raise AssertionError("an e2e order never carries a provider session")


def _e2e_facts(sha: str) -> dict:
    return {
        "stage_e2e": "Run the recipe.",
        "ticket": {"key": "F-1"},
        "source_sha": sha,
        "e2e_recipe": "run the suite",
        "evidence_contract": "rung 1 only",
    }


def _e2e_descriptor(sha: str) -> dict:
    return {
        "stage": "e2e",
        "generation": 1,
        "cognitive_substeps": {
            "main": {
                "logical_invocation_id": "run-1:e2e:main:1",
                "run_id": "run-1",
                "stage": "e2e",
                "substep": "main",
                "stage_generation": 1,
                "source_sha": sha,
                "route_snapshot_digest": "b" * 64,
                "profile": "e2e",
                "desired_route": {"harness": "codex", "model": "fake", "effort": "medium"},
                "activation": "pending",
                "conditional": False,
                "lease_fence": "fence-1",
            }
        },
    }


class _SeedProbeAdapter:
    """Reads the seeded working tree, writes only its own build product, reports what it saw."""

    harness = "codex"

    def __init__(self) -> None:
        self.launches = 0

    def preflight(self, route, authority="read_only"):
        return {"executable": "/usr/bin/codex", "version": "codex 1", "harness": "codex"}

    def command(self, route, prompt, schema_path, capsule, authority="read_only"):
        self.launches += 1
        body = (
            "import json,sys,subprocess,pathlib;"
            "tracked=pathlib.Path('tracked.txt').read_text().strip();"
            "newfile=pathlib.Path('newfile.txt').exists();"
            "pathlib.Path('build').mkdir(exist_ok=True);"
            "pathlib.Path('build/artifact.bin').write_bytes(bytes(range(64)));"
            "sha=subprocess.run(['git','rev-parse','HEAD'],capture_output=True,text=True)"
            ".stdout.strip();"
            "sys.stdout.write(json.dumps({'result':{'verdict':'pass','summary':'recipe green',"
            "'evidence':'tracked='+tracked+';newfile='+('yes' if newfile else 'no'),"
            "'source_sha':sha}}))"
        )
        return [sys.executable, "-c", body]

    def session_command(self, *args, **kwargs):
        raise AssertionError("an e2e order never carries a provider session")


def _no_import(*_args, **_kwargs):
    raise AssertionError("a disposable E2E writer must never enter the import path")


def test_dispatch_seeds_capsule_with_uncommitted_working_state(tmp_path: Path, monkeypatch) -> None:
    """dispatch -> seed -> run: the recipe sees the ticket's uncommitted code, not the base."""
    source, sha = _baseline_repo(tmp_path)
    # The ticket's implement/code_review edits, still uncommitted in the authoritative worktree.
    (source / "tracked.txt").write_text("ticket change\n", encoding="utf-8")
    (source / "newfile.txt").write_text("ticket added this\n", encoding="utf-8")
    bundle = tmp_path / "bundle.json"
    bundle.write_text("{}\n", encoding="utf-8")

    order = cw.prepare_work_order(
        _e2e_descriptor(sha),
        substep="main",
        source_root=source,
        input_bundle=bundle,
        facts=_e2e_facts(sha),
        output=tmp_path / "orders" / "main.json",
    )
    # Dispatch sealed the seed as an immutable, digest-bound patch.
    assert order.seed_patch is not None
    assert hashlib.sha256(Path(order.seed_patch).read_bytes()).hexdigest() == order.seed_digest

    adapter = _SeedProbeAdapter()
    monkeypatch.setattr(cw.CognitiveWorkers, "_import_after_validation", _no_import)
    workers = cw.CognitiveWorkers(
        artifact_root=tmp_path / "artifacts",
        capsule_root=tmp_path / "capsules",
        adapters={"codex": adapter},
    )
    before = cw.git_receipt(source)["digest"]

    outcome = workers.run(order, _owner())

    assert outcome.status == "succeeded"
    assert adapter.launches == 1
    # The recipe ran against the ticket's real code: the seeded tracked edit and the seeded
    # new untracked file were both present in the capsule.
    assert outcome.result is not None
    assert outcome.result["evidence"] == "tracked=ticket change;newfile=yes"

    # Evidence is measured against the seeded baseline, so it reports only what the RECIPE
    # wrote, never the seeded ticket diff.
    mutations = outcome.result["capsule_mutations"]
    assert mutations["seeded"] is True
    assert mutations["touched"] == ["build/artifact.bin"]

    # The authoritative worktree is byte-identical and still carries its uncommitted work.
    assert cw.git_receipt(source)["digest"] == before
    assert (source / "tracked.txt").read_text() == "ticket change\n"
    assert (source / "newfile.txt").read_text() == "ticket added this\n"
    assert not (source / "build").exists()

    # Nothing imported; the capsule (and every mutation in it) is disposed.
    assert "change" not in outcome.receipts
    assert outcome.receipts["disposal"]["absent"] is True
    capsule = (tmp_path / "capsules") / hashlib.sha256(
        f"{order.logical_invocation_id}:{order.generation}".encode()
    ).hexdigest()
    assert not capsule.exists()


def test_empty_working_delta_seeds_a_clean_base(tmp_path: Path, monkeypatch) -> None:
    """A clean authoritative worktree seals no seed; the capsule stays at the bare base SHA."""
    source, sha = _baseline_repo(tmp_path)
    bundle = tmp_path / "bundle.json"
    bundle.write_text("{}\n", encoding="utf-8")

    order = cw.prepare_work_order(
        _e2e_descriptor(sha),
        substep="main",
        source_root=source,
        input_bundle=bundle,
        facts=_e2e_facts(sha),
        output=tmp_path / "orders" / "main.json",
    )
    assert order.seed_patch is None
    assert order.seed_digest is None

    adapter = _SeedProbeAdapter()
    monkeypatch.setattr(cw.CognitiveWorkers, "_import_after_validation", _no_import)
    workers = cw.CognitiveWorkers(
        artifact_root=tmp_path / "artifacts",
        capsule_root=tmp_path / "capsules",
        adapters={"codex": adapter},
    )

    outcome = workers.run(order, _owner())

    assert outcome.status == "succeeded"
    assert outcome.result is not None
    # The capsule is the bare base: the tracked file is unchanged and no seeded file exists.
    assert outcome.result["evidence"] == "tracked=base;newfile=no"
    mutations = outcome.result["capsule_mutations"]
    assert mutations["seeded"] is False
    assert mutations["touched"] == ["build/artifact.bin"]
    assert outcome.receipts["disposal"]["absent"] is True


def test_active_e2e_captures_evidence_discards_capsule_and_never_imports(
    tmp_path: Path, monkeypatch
) -> None:
    source, sha = _baseline_repo(tmp_path)
    order = _e2e_order(source, sha)
    adapter = _E2EAdapter()
    # A hard proof that the disposable branch never routes through the importer/lock.
    monkeypatch.setattr(cw.CognitiveWorkers, "_import_after_validation", _no_import)
    workers = cw.CognitiveWorkers(
        artifact_root=tmp_path / "artifacts",
        capsule_root=tmp_path / "capsules",
        adapters={"codex": adapter},
    )
    before = cw.git_receipt(source)["digest"]

    outcome = workers.run(order, _owner())

    assert outcome.status == "succeeded"
    assert adapter.launches == 1
    # 1a wired the writable sandbox through order.authority; it reaches the adapter as such.
    assert adapter.authorities == ["disposable_writer"]

    # Mutation discard: the authoritative worktree is byte-identical across the whole run.
    assert cw.git_receipt(source)["digest"] == before
    assert (source / "tracked.txt").read_text() == "base\n"
    assert not (source / "build").exists()

    # Evidence retention: the capsule mutations are captured into the E2E result.
    assert outcome.result is not None
    mutations = outcome.result["capsule_mutations"]
    assert mutations["empty"] is False
    assert set(mutations["touched"]) == {"build/artifact.bin", "tracked.txt"}
    assert mutations["diffstat"]["additions"] >= 1
    assert outcome.result["verdict"] == "pass"
    assert outcome.result["source_sha"] == sha

    # No import: no change receipt, no writer import lock touched.
    assert "change" not in outcome.receipts
    assert not cw._writer_import_lock_path(source).exists()

    # Disposal: the capsule (and every source mutation in it) is discarded.
    assert outcome.receipts["disposal"]["absent"] is True
    capsule = (tmp_path / "capsules") / hashlib.sha256(
        f"{order.logical_invocation_id}:1".encode()
    ).hexdigest()
    assert not capsule.exists()
    invocation = workers._invocation_dir(order.logical_invocation_id)
    assert (
        cw.InvocationJournal(invocation / "journal.json", order.logical_invocation_id).read() or {}
    ).get("state") == "completed"


def test_e2e_recovery_reuses_the_stored_mutation_summary(tmp_path: Path, monkeypatch) -> None:
    """A crash after disposal re-creates a CLEAN capsule; the summary must come from the journal."""
    source, sha = _baseline_repo(tmp_path)
    order = _e2e_order(source, sha, "e2e-resume-1")

    class _RelaunchGuard:
        harness = "codex"

        def preflight(self, route, authority="read_only"):
            return {"executable": "/usr/bin/codex", "version": "codex 1", "harness": "codex"}

        def command(self, *args, **kwargs):
            raise AssertionError("recovery from a validated journal must never relaunch")

        def session_command(self, *args, **kwargs):
            raise AssertionError("an e2e order never carries a provider session")

    monkeypatch.setattr(cw.CognitiveWorkers, "_import_after_validation", _no_import)
    workers = cw.CognitiveWorkers(
        artifact_root=tmp_path / "artifacts",
        capsule_root=tmp_path / "capsules",
        adapters={"codex": _RelaunchGuard()},
    )
    invocation = workers._invocation_dir(order.logical_invocation_id)
    invocation.mkdir(parents=True, exist_ok=True)
    process = cw.ProcessEvidence(
        pid=424242,
        returncode=0,
        stdout="",
        stderr="",
        child_reaped=True,
        process_group_absent=True,
        stdout_eof=True,
        stderr_eof=True,
        elapsed_seconds=1.0,
        soft_deadline=False,
    )
    stored_mutations = {
        "schema": "flow.e2e-capsule-mutations/v1",
        "touched": ["build/artifact.bin", "tracked.txt"],
        "diffstat": {"binary": True, "additions": 1, "deletions": 0},
        "patch_digest": "e" * 64,
        "empty": False,
    }
    stored_result = {
        "verdict": "pass",
        "summary": "recipe green",
        "evidence": "42 passed",
        "source_sha": sha,
    }
    journal = cw.InvocationJournal(invocation / "journal.json", order.logical_invocation_id)
    journal.transition("prepared", launch_nonce="n")
    journal.transition("cloning")
    journal.transition(
        "running",
        authoritative_before=cw.git_receipt(source),
        capsule_before={"digest": "capsule-before"},
    )
    journal.transition("terminal", process=process.__dict__)
    journal.transition(
        "validated",
        result=stored_result,
        worker_id=None,
        authoritative_after=cw.git_receipt(source),
        capsule_mutations=stored_mutations,
    )

    outcome = workers.run(order, _owner())

    assert outcome.status == "succeeded"
    assert outcome.result is not None
    # The re-created capsule is clean, so a re-capture would read zero mutations. The summary
    # is the durably-journaled one, proving the capture happened before disposal.
    assert outcome.result["capsule_mutations"] == stored_mutations
    assert "change" not in outcome.receipts
    assert outcome.receipts["disposal"]["absent"] is True


def test_e2e_provider_schema_is_closed_and_model_facing() -> None:
    schema = cw.provider_schema("e2e")
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"verdict", "summary", "evidence", "source_sha"}
    assert schema["properties"]["verdict"]["enum"] == ["pass", "fail"]
    # Flow captures the mutation summary; the model is never asked to author it.
    assert "capsule_mutations" not in schema["properties"]


def test_e2e_validate_typed_result_rejects_malformed() -> None:
    good = {"verdict": "pass", "summary": "s", "evidence": "e", "source_sha": "a" * 40}
    assert cw.validate_typed_result("e2e", good) == good
    for bad in (
        {"verdict": "green", "summary": "s", "evidence": "e", "source_sha": "a" * 40},
        {"verdict": "pass", "summary": "s", "evidence": "e", "source_sha": "a" * 39},
        {"verdict": "pass", "summary": "s", "evidence": "e"},
        {**good, "capsule_mutations": {}},
    ):
        with pytest.raises(cw.WorkerFailure) as err:
            cw.validate_typed_result("e2e", bad)
        assert err.value.code == "invalid_result"


def _workspace(tmp_path: Path) -> Path:
    flow = tmp_path / ".flow"
    flow.mkdir(parents=True)
    (flow / "workspace.toml").write_text(
        '[tracker]\nbackend = "beads"\n[tracker.beads]\nprefix = "x"\n', encoding="utf-8"
    )
    return tmp_path


def test_e2e_route_activates_on_a_cli_receipt_while_machinery_fixer_stays_shadow(
    tmp_path: Path,
) -> None:
    snap = agent_routes.snapshot(_workspace(tmp_path), "codex")
    assert snap["routes"]["e2e"]["activation"] == "pending"

    def _attest(profile: str, transport: str) -> dict:
        request = dict(snap["routes"][profile]["desired"])
        return agent_routes.attest(
            snap,
            profile,
            {
                "request": request,
                "response": {
                    "accepted": True,
                    **request,
                    "transport": transport,
                    "adapter_version": "adapter/1",
                },
                "prompt_hash": "a" * 64,
                "schema_hash": "b" * 64,
                "physical_attempt": {"pid": 17, "terminal_acknowledged": True},
                "cleanup": {"capsule_absent": True, "quarantined": False},
            },
        )

    # E2E is disposal-terminal like the readers, so an exact CLI receipt activates it.
    assert _attest("e2e", "cli")["activation"] == "active"
    # Each importing writer disposes its capsule after import, so it activates on the same proof;
    # only machinery_fixer stays shadow.
    for writer in ("implementer", "review_fixer", "revision_fixer"):
        assert _attest(writer, "cli")["activation"] == "active", writer
    assert _attest("machinery_fixer", "cli")["activation"] == "shadow"
