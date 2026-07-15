"""Fault suite for the writer capture + compare-and-swap import machinery (flow-d8am).

The mechanical helpers are driven directly against real git worktrees and fake clock/lock
inputs. The implementer is now an active importing writer (flow-jrv4), so the live-run
section at the tail exercises it end to end through run() and prepare_work_order; the other
three writers stay active=False. Each test fails when its load-bearing source hunk is
stripped (verified by surgical revert).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import agent_routes
import cognitive_workers as cw
import dispatch_stage as ds
from _locking import LOCK_RETRY_DELAY_S, flock_blocking, flock_retry


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def _init(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "flow@example.test")
    _git(root, "config", "user.name", "Flow Test")


def _baseline_repo(tmp_path: Path, name: str = "source") -> tuple[Path, str]:
    """A repo whose owned files live under src/, plus an unowned other/ file."""
    root = tmp_path / name
    _init(root)
    (root / "src").mkdir()
    (root / "src" / "keep.txt").write_text("keep\n", encoding="utf-8")
    (root / "src" / "tomodify.txt").write_text("base\n", encoding="utf-8")
    (root / "src" / "torename.txt").write_text("moved\n", encoding="utf-8")
    (root / "src" / "todelete.txt").write_text("gone\n", encoding="utf-8")
    (root / "src" / "tochmod.sh").write_text("echo hi\n", encoding="utf-8")
    (root / "other").mkdir()
    (root / "other" / "outside.txt").write_text("outside\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root, _git(root, "rev-parse", "HEAD")


def _tree(root: Path) -> str:
    _git(root, "add", "-A")
    return _git(root, "write-tree")


def _order(source: Path, sha: str, *, allowed: tuple[str, ...] = ("src",)) -> cw.WorkOrder:
    return cw.WorkOrder(
        logical_invocation_id="writer-1",
        generation=1,
        profile="implementer",
        source_root=str(source),
        source_sha=sha,
        route={"harness": "codex", "model": "fake", "effort": "high"},
        route_snapshot_digest="b" * 64,
        input_bundle=str(source / "src" / "keep.txt"),
        input_digest="c" * 64,
        facts={},
        allowed_mutation_paths=allowed,
        run_id="run-1",
        stage="implement",
        substep="implement",
        stage_generation=1,
        lease_fence="fence-1",
    )


def _frozen_observer(order: cw.WorkOrder, source: Path) -> dict:
    """A DispatchObserver that echoes the order's frozen values, so no external CAS dim drifts.

    The capture/apply/resume mechanics under test do not exercise dispatch drift; the live-drift
    coverage lives in the observer tests below. This keeps those mechanical tests isolated from
    the (now required) live-dispatch observation seam.
    """
    return {
        "dispatch_generation": order.stage_generation,
        "route_snapshot": order.route_snapshot_digest,
        "lease_fence": order.lease_fence,
    }


# ─── path normalization + ownership ──────────────────────────────────────────


def test_normalize_collapses_odd_forms_and_rejects_escapes() -> None:
    assert cw._normalize_repo_path("src/./a") == "src/a"
    assert cw._normalize_repo_path("src//a/") == "src/a"
    for bad in ("/abs", "a\\b", "..", "src/../etc", "", "."):
        with pytest.raises(cw.WorkerFailure) as err:
            cw._normalize_repo_path(bad)
        assert err.value.code == "ownership_violation"


def test_within_allowed_matches_file_and_directory_prefix() -> None:
    allowed = frozenset({"src", "docs/readme.md"})
    assert cw._within_allowed("src/a/b.py", allowed)
    assert cw._within_allowed("docs/readme.md", allowed)
    assert not cw._within_allowed("srcx/a", allowed)
    assert not cw._within_allowed("docs/other.md", allowed)


# ─── patch capture + round-trip ──────────────────────────────────────────────


def _mutate_capsule(capsule: Path) -> None:
    """Every replayable change kind: modify, rename, delete, chmod, binary add, large add."""
    (capsule / "src" / "tomodify.txt").write_text("changed\n", encoding="utf-8")
    _git(capsule, "mv", "src/torename.txt", "src/renamed.txt")
    _git(capsule, "rm", "-q", "src/todelete.txt")
    (capsule / "src" / "tochmod.sh").chmod(0o755)
    (capsule / "src" / "image.bin").write_bytes(bytes(range(256)) * 8)
    (capsule / "src" / "large.txt").write_text("x\n" * 50_000, encoding="utf-8")


def test_capture_round_trips_every_change_kind(tmp_path: Path) -> None:
    source, sha = _baseline_repo(tmp_path)
    capsule = tmp_path / "capsule"
    cw.create_private_clone(source, sha, capsule)
    _mutate_capsule(capsule)
    capture = cw._capture_capsule_patch(capsule, sha, ("src",))

    meta = capture["metadata"]
    assert meta["binary"] is True
    assert meta["renames"] == 1
    assert meta["deletions"] == 1
    assert meta["mode_changes"] == 1
    assert meta["additions"] >= 2  # image.bin + large.txt
    assert "src/renamed.txt" in capture["touched"]
    assert "src/torename.txt" in capture["touched"]  # both rename sides are touched

    fresh = tmp_path / "fresh"
    cw.create_private_clone(source, sha, fresh)
    applied = subprocess.run(
        ["git", "apply", "--index"],
        cwd=fresh,
        input=capture["patch"],
        capture_output=True,
        check=False,
    )
    assert applied.returncode == 0, applied.stderr.decode()
    assert _tree(fresh) == _tree(capsule)  # binary + rename + mode + delete + large all replay


def test_touched_path_outside_allowed_is_ownership_violation(tmp_path: Path) -> None:
    source, sha = _baseline_repo(tmp_path)
    capsule = tmp_path / "capsule"
    cw.create_private_clone(source, sha, capsule)
    (capsule / "other" / "outside.txt").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(cw.WorkerFailure) as err:
        cw._capture_capsule_patch(capsule, sha, ("src",))
    assert err.value.code == "ownership_violation"


def test_rename_escaping_allowed_set_is_ownership_violation(tmp_path: Path) -> None:
    source, sha = _baseline_repo(tmp_path)
    capsule = tmp_path / "capsule"
    cw.create_private_clone(source, sha, capsule)
    _git(capsule, "mv", "src/torename.txt", "other/escaped.txt")
    with pytest.raises(cw.WorkerFailure) as err:
        cw._capture_capsule_patch(capsule, sha, ("src",))
    assert err.value.code == "ownership_violation"


def test_capture_failure_preserves_capsule(tmp_path: Path) -> None:
    source, sha = _baseline_repo(tmp_path)
    capsule = tmp_path / "capsule"
    cw.create_private_clone(source, sha, capsule)
    (capsule / "src" / "tomodify.txt").write_text("changed\n", encoding="utf-8")
    with pytest.raises(cw.WorkerFailure) as err:
        cw._capture_capsule_patch(capsule, "0" * 40, ("src",))  # bad baseline object
    assert err.value.code == "patch_capture_failed"
    assert capsule.exists()


# ─── journal importing state ─────────────────────────────────────────────────


def test_importing_state_is_reachable_and_monotonic(tmp_path: Path) -> None:
    journal = cw.InvocationJournal(tmp_path / "journal.json", "inv-1")
    for state in ("prepared", "cloning", "running", "terminal", "validated", "importing"):
        assert journal.transition(state)["state"] == state
    assert journal.transition("importing")["state"] == "importing"  # idempotent re-entry
    with pytest.raises(cw.WorkerFailure, match="cannot move"):
        journal.transition("validated")  # backward
    assert journal.transition("completed")["state"] == "completed"
    with pytest.raises(cw.WorkerFailure, match="terminal"):
        journal.transition("quarantined")  # the terminal set stays frozen (same rank, refused)


# ─── CAS refusal checks ──────────────────────────────────────────────────────


def test_cas_refusals_flags_each_of_the_six_fields() -> None:
    base = {
        "head": "h",
        "index": "i",
        "owned_baseline": "o",
        "dispatch_generation": 1,
        "route_snapshot": "r",
        "lease_fence": "f",
    }
    assert cw._cas_refusals(base, base) == ()
    for field in cw._CAS_FIELDS:
        drifted = {**base, field: "DRIFTED"}
        assert cw._cas_refusals(base, drifted) == (field,)


def _capsule_with_owned_edit(tmp_path: Path, source: Path, sha: str) -> tuple[Path, dict]:
    capsule = tmp_path / "capsule"
    cw.create_private_clone(source, sha, capsule)
    (capsule / "src" / "tomodify.txt").write_text("changed\n", encoding="utf-8")
    capture = cw._capture_capsule_patch(capsule, sha, ("src",))
    return capsule, capture


def _import(
    source: Path,
    order: cw.WorkOrder,
    capture: dict,
    tmp_path: Path,
    *,
    expected_overrides: dict | None = None,
    observed_external: dict | None = None,
    lock_path: Path | None = None,
) -> dict:
    patch_path = tmp_path / "patch.bin"
    patch_path.write_bytes(capture["patch"])
    journal = cw.InvocationJournal(tmp_path / "import-journal.json", order.logical_invocation_id)
    journal.transition("prepared")
    journal.transition("cloning")
    journal.transition("running")
    journal.transition("terminal")
    journal.transition("validated")
    expected = {
        "head": order.source_sha,
        "index": cw.git_receipt(source)["index"]["sha256"],
        "owned_baseline": cw._owned_baseline_digest(source, order.allowed_mutation_paths),
        "dispatch_generation": order.stage_generation,
        "route_snapshot": order.route_snapshot_digest,
        "lease_fence": order.lease_fence,
    }
    if expected_overrides:
        expected.update(expected_overrides)
    external = observed_external or {
        "dispatch_generation": order.stage_generation,
        "route_snapshot": order.route_snapshot_digest,
        "lease_fence": order.lease_fence,
    }
    return cw._import_capsule_patch(
        source,
        order=order,
        capture=capture,
        patch_path=patch_path,
        expected=expected,
        observed_external=external,
        journal=journal,
        lock_path=lock_path,
    )


def test_successful_import_applies_and_records_change_receipt(tmp_path: Path) -> None:
    source, sha = _baseline_repo(tmp_path)
    order = _order(source, sha)
    _, capture = _capsule_with_owned_edit(tmp_path, source, sha)
    receipt = _import(source, order, capture, tmp_path)

    assert receipt["schema"] == cw.CHANGE_RECEIPT_SCHEMA
    assert receipt["import_result"] == "applied"
    assert receipt["baseline_digest"] == sha
    assert receipt["touched_paths"] == ["src/tomodify.txt"]
    assert receipt["allowed_paths"] == ["src"]
    assert receipt["import_target"]["head_before"] == receipt["import_target"]["head_after"] == sha
    assert len(receipt["authoritative_diff_digest"]) == 64
    assert (source / "src" / "tomodify.txt").read_text() == "changed\n"
    assert "src/tomodify.txt" in _git(source, "diff", "--cached", "--name-only")


@pytest.mark.parametrize(
    ("field", "mutate"),
    [
        ("head", "commit"),
        ("index", "stage"),
        ("owned_baseline", "worktree"),
        ("dispatch_generation", "external"),
        ("route_snapshot", "external"),
        ("lease_fence", "external"),
    ],
)
def test_each_cas_field_refuses_baseline_mismatch(tmp_path: Path, field: str, mutate: str) -> None:
    source, sha = _baseline_repo(tmp_path)
    order = _order(source, sha)
    _, capture = _capsule_with_owned_edit(tmp_path, source, sha)
    # Snapshot the pristine expected before drifting a single dimension of the live target.
    expected = _live_expected(source, order)
    before_content = (source / "src" / "tomodify.txt").read_text()
    observed_external = {
        "dispatch_generation": order.stage_generation,
        "route_snapshot": order.route_snapshot_digest,
        "lease_fence": order.lease_fence,
    }
    if mutate == "commit":
        (source / "src" / "keep.txt").write_text("drifted\n", encoding="utf-8")
        _git(source, "commit", "-aqm", "external commit")
    elif mutate == "stage":
        (source / "src" / "keep.txt").write_text("staged\n", encoding="utf-8")
        _git(source, "add", "src/keep.txt")
    elif mutate == "worktree":
        (source / "src" / "tomodify.txt").write_text("external\n", encoding="utf-8")
        before_content = "external\n"
    else:  # a dispatch/route/lease fact the module reads externally
        observed_external[field] = "DRIFTED"

    patch_path = tmp_path / "patch.bin"
    patch_path.write_bytes(capture["patch"])
    journal = cw.InvocationJournal(tmp_path / "j.json", order.logical_invocation_id)
    for state in ("prepared", "cloning", "running", "terminal", "validated"):
        journal.transition(state)
    with pytest.raises(cw.WorkerFailure) as err:
        cw._import_capsule_patch(
            source,
            order=order,
            capture=capture,
            patch_path=patch_path,
            expected=expected,
            observed_external=observed_external,
            journal=journal,
        )
    assert err.value.code == "baseline_mismatch"
    assert field in str(err.value)
    # the authoritative owned file is left untouched by a refused import
    assert (source / "src" / "tomodify.txt").read_text() == before_content


# ─── apply atomicity, conflict, partial ──────────────────────────────────────


def _live_expected(source: Path, order: cw.WorkOrder) -> dict:
    return {
        "head": order.source_sha,
        "index": cw.git_receipt(source)["index"]["sha256"],
        "owned_baseline": cw._owned_baseline_digest(source, order.allowed_mutation_paths),
        "dispatch_generation": order.stage_generation,
        "route_snapshot": order.route_snapshot_digest,
        "lease_fence": order.lease_fence,
    }


def test_failed_apply_is_conflict_and_leaves_worktree_and_capsule(tmp_path: Path) -> None:
    source, sha = _baseline_repo(tmp_path)
    order = _order(source, sha)
    capsule, capture = _capsule_with_owned_edit(tmp_path, source, sha)
    # Diverge the authoritative owned file so the base-expecting patch cannot apply, then set
    # expected == the drifted live state so CAS passes and the apply-failure branch is isolated.
    (source / "src" / "tomodify.txt").write_text("diverged-context\n", encoding="utf-8")
    patch_path = tmp_path / "patch.bin"
    patch_path.write_bytes(capture["patch"])
    journal = cw.InvocationJournal(tmp_path / "j.json", order.logical_invocation_id)
    for state in ("prepared", "cloning", "running", "terminal", "validated"):
        journal.transition(state)

    with pytest.raises(cw.WorkerFailure) as err:
        cw._import_capsule_patch(
            source,
            order=order,
            capture=capture,
            patch_path=patch_path,
            expected=_live_expected(source, order),
            observed_external={
                "dispatch_generation": order.stage_generation,
                "route_snapshot": order.route_snapshot_digest,
                "lease_fence": order.lease_fence,
            },
            journal=journal,
        )
    assert err.value.code == "patch_import_conflict"
    assert (source / "src" / "tomodify.txt").read_text() == "diverged-context\n"  # unchanged
    assert not _git(source, "diff", "--cached", "--name-only")  # nothing staged
    assert capsule.exists()  # capsule preserved as recovery evidence
    assert patch_path.exists()  # patch preserved as recovery evidence


def test_partial_application_is_indeterminate_write(tmp_path: Path) -> None:
    source, sha = _baseline_repo(tmp_path)
    order = _order(source, sha)
    _, capture = _capsule_with_owned_edit(tmp_path, source, sha)
    # A resume (journal already importing) whose owned file crashed into a third content state:
    # the patch (base -> changed) neither applies forward nor reverses, so it is genuinely partial.
    (source / "src" / "tomodify.txt").write_text("half-way\n", encoding="utf-8")
    patch_path = tmp_path / "patch.bin"
    patch_path.write_bytes(capture["patch"])
    journal = cw.InvocationJournal(tmp_path / "j.json", order.logical_invocation_id)
    for state in ("prepared", "cloning", "running", "terminal", "validated", "importing"):
        journal.transition(state)

    with pytest.raises(cw.WorkerFailure) as err:
        cw._import_capsule_patch(
            source,
            order=order,
            capture=capture,
            patch_path=patch_path,
            expected=_live_expected(source, order),
            observed_external={
                "dispatch_generation": order.stage_generation,
                "route_snapshot": order.route_snapshot_digest,
                "lease_fence": order.lease_fence,
            },
            journal=journal,
        )
    assert err.value.code == "indeterminate_write"
    assert (source / "src" / "tomodify.txt").read_text() == "half-way\n"  # never re-baselined


# ─── sole-writer lock ────────────────────────────────────────────────────────


def test_writer_busy_on_domain_lock_contention(tmp_path: Path, monkeypatch) -> None:
    source, sha = _baseline_repo(tmp_path)
    order = _order(source, sha)
    _, capture = _capsule_with_owned_edit(tmp_path, source, sha)
    lock_path = tmp_path / "domain.lock"
    monkeypatch.setattr(cw, "flock_retry", lambda p, **k: flock_retry(p, retries=2, delay=0.01))

    holding = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with flock_blocking(lock_path):
            holding.set()
            release.wait(5)

    holder = threading.Thread(target=hold)
    holder.start()
    holding.wait(5)
    try:
        with pytest.raises(cw.WorkerFailure) as err:
            _import(source, order, capture, tmp_path, lock_path=lock_path)
        assert err.value.code == "writer_busy"
    finally:
        release.set()
        holder.join(5)
    assert LOCK_RETRY_DELAY_S  # sanity: the real substrate is still the flock retry


# ─── idempotent resume / recovery ────────────────────────────────────────────


def test_repeat_import_resumes_without_second_apply(tmp_path: Path) -> None:
    source, sha = _baseline_repo(tmp_path)
    order = _order(source, sha)
    _, capture = _capsule_with_owned_edit(tmp_path, source, sha)
    patch_path = tmp_path / "patch.bin"
    patch_path.write_bytes(capture["patch"])
    # Pin the CAS reference to the pre-apply state, so the resume must recognize the applied tree
    # via apply-state detection rather than a second (failing) apply or a false index-drift refusal.
    expected = {
        "head": order.source_sha,
        "index": cw.git_receipt(source)["index"]["sha256"],
        "owned_baseline": cw._owned_baseline_digest(source, order.allowed_mutation_paths),
        "dispatch_generation": order.stage_generation,
        "route_snapshot": order.route_snapshot_digest,
        "lease_fence": order.lease_fence,
    }
    external = {
        "dispatch_generation": order.stage_generation,
        "route_snapshot": order.route_snapshot_digest,
        "lease_fence": order.lease_fence,
    }
    journal = cw.InvocationJournal(tmp_path / "journal.json", order.logical_invocation_id)
    for state in ("prepared", "cloning", "running", "terminal", "validated"):
        journal.transition(state)

    def do_import() -> dict:
        return cw._import_capsule_patch(
            source,
            order=order,
            capture=capture,
            patch_path=patch_path,
            expected=expected,
            observed_external=external,
            journal=journal,
        )

    first = do_import()  # validated -> importing, applies
    assert first["import_result"] == "applied"
    assert (journal.read() or {}).get("state") == "importing"
    applied_content = (source / "src" / "tomodify.txt").read_text()

    second = do_import()  # journal already importing: already-applied tree finalizes
    assert second["import_result"] == "resumed"  # not re-applied
    assert (source / "src" / "tomodify.txt").read_text() == applied_content


def test_import_after_validation_wires_capture_and_import(tmp_path: Path) -> None:
    source, sha = _baseline_repo(tmp_path)
    order = _order(source, sha)
    capsule = tmp_path / "capsules" / hashlib.sha256(b"writer-1:1").hexdigest()
    cw.create_private_clone(source, sha, capsule)
    (capsule / "src" / "tomodify.txt").write_text("changed\n", encoding="utf-8")

    workers = cw.CognitiveWorkers(
        artifact_root=tmp_path / "artifacts",
        capsule_root=tmp_path / "capsules",
        dispatch_observer=_frozen_observer,
    )
    invocation = workers._invocation_dir(order.logical_invocation_id)
    journal = cw.InvocationJournal(invocation / "journal.json", order.logical_invocation_id)
    journal.transition("prepared")
    journal.transition("cloning")
    journal.transition(
        "running",
        authoritative_before=cw.git_receipt(source),
        owned_baseline_before=cw._owned_baseline_digest(source, order.allowed_mutation_paths),
    )
    journal.transition("terminal")
    journal.transition("validated")

    receipt = workers._import_after_validation(order, source, capsule, journal)
    assert receipt["import_result"] == "applied"
    assert (source / "src" / "tomodify.txt").read_text() == "changed\n"
    assert (invocation / "capsule-patch.bin").exists()  # patch persisted for a resume
    assert (journal.read() or {}).get("state") == "importing"

    # A resume reads the persisted patch, detects the applied tree, and finalizes idempotently.
    resumed = workers._import_after_validation(order, source, capsule, journal)
    assert resumed["import_result"] == "resumed"


# ─── run() writer wiring (dormant branch, activated for the test) ─────────────


def _fake_prompt(_facts: object) -> cw.PromptMaterial:
    return cw.PromptMaterial(
        builder_id="implementer/v1",
        template_digest="a" * 64,
        facts_digest="b" * 64,
        artifact_digests={},
        schema_digest="c" * 64,
        prompt="implement it",
        prompt_digest="d" * 64,
    )


def _activate_writer(monkeypatch) -> None:
    monkeypatch.setitem(
        cw.ROLE_CATALOG,
        "implementer",
        dataclasses.replace(cw.ROLE_CATALOG["implementer"], active=True),
    )
    monkeypatch.setitem(cw.PROMPT_BUILDERS, "implementer", _fake_prompt)


def _writer_order(source: Path, sha: str, logical_id: str) -> cw.WorkOrder:
    bundle = source / "src" / "keep.txt"
    return cw.WorkOrder(
        logical_invocation_id=logical_id,
        generation=1,
        profile="implementer",
        source_root=str(source),
        source_sha=sha,
        route={"harness": "codex", "model": "fake", "effort": "high"},
        route_snapshot_digest="b" * 64,
        input_bundle=str(bundle),
        input_digest=hashlib.sha256(bundle.read_bytes()).hexdigest(),
        facts={},
        allowed_mutation_paths=("src",),
        run_id="run-1",
        stage="implement",
        substep="implement",
        stage_generation=1,
        lease_fence="fence-1",
        result_schema={"type": "object", "additionalProperties": True},
    )


def _writer_result(sha: str) -> dict:
    """A valid implementation-report/v1 body: the closed contract Flow now validates."""
    return {
        "summary": "implemented",
        "evidence": "wrote src/impl.txt; tests green",
        "source_sha": sha,
    }


class _WriterAdapter:
    harness = "codex"

    def __init__(self) -> None:
        self.launches = 0

    def preflight(self, route, authority="read_only"):
        return {"executable": "/usr/bin/codex", "version": "codex 1", "harness": "codex"}

    def command(self, route, prompt, schema_path, capsule, authority="read_only"):
        self.launches += 1
        body = (
            "import json,sys,pathlib,subprocess;"
            "pathlib.Path('src').mkdir(exist_ok=True);"
            "pathlib.Path('src/impl.txt').write_text('impl\\n');"
            "sha=subprocess.run(['git','rev-parse','HEAD'],capture_output=True,text=True)"
            ".stdout.strip();"
            "sys.stdout.write(json.dumps({'result':{'summary':'implemented',"
            "'evidence':'wrote src/impl.txt; tests green','source_sha':sha}}))"
        )
        return [sys.executable, "-c", body]

    def session_command(self, *args, **kwargs):
        raise AssertionError("a writer order never carries a provider session")


def _owner() -> cw.OwnerProof:
    return cw.OwnerProof(owner_id="owner", harness="codex", run_id="run-1", lease_fence="fence-1")


def test_active_writer_captures_and_imports_through_run(tmp_path: Path, monkeypatch) -> None:
    _activate_writer(monkeypatch)
    source, sha = _baseline_repo(tmp_path)
    order = _writer_order(source, sha, "writer-run-1")
    adapter = _WriterAdapter()
    workers = cw.CognitiveWorkers(
        artifact_root=tmp_path / "artifacts",
        capsule_root=tmp_path / "capsules",
        adapters={"codex": adapter},
        dispatch_observer=_frozen_observer,
    )

    outcome = workers.run(order, _owner())
    assert outcome.status == "succeeded"
    assert adapter.launches == 1
    change = outcome.receipts["change"]
    assert change["import_result"] == "applied"
    assert change["touched_paths"] == ["src/impl.txt"]
    assert (source / "src" / "impl.txt").read_text() == "impl\n"
    assert "src/impl.txt" in _git(source, "diff", "--cached", "--name-only")
    assert outcome.receipts["disposal"]["absent"] is True
    invocation = workers._invocation_dir(order.logical_invocation_id)
    assert (
        cw.InvocationJournal(invocation / "journal.json", order.logical_invocation_id).read() or {}
    ).get("state") == "completed"


def test_run_resumes_an_importing_journal_without_relaunch(tmp_path: Path, monkeypatch) -> None:
    _activate_writer(monkeypatch)
    source, sha = _baseline_repo(tmp_path)
    order = _writer_order(source, sha, "writer-resume-1")
    workers = cw.CognitiveWorkers(
        artifact_root=tmp_path / "artifacts",
        capsule_root=tmp_path / "capsules",
        adapters={"codex": _RelaunchGuard()},
        dispatch_observer=_frozen_observer,
    )
    invocation = workers._invocation_dir(order.logical_invocation_id)
    capsule = (tmp_path / "capsules") / hashlib.sha256(
        f"{order.logical_invocation_id}:1".encode()
    ).hexdigest()
    capsule_receipt = cw.create_private_clone(source, sha, capsule)
    (capsule / "src" / "impl.txt").write_text("impl\n", encoding="utf-8")
    capture = cw._capture_capsule_patch(capsule, sha, ("src",))
    # Snapshot the pre-import state, then simulate a crash after the apply but before completion.
    auth_before = cw.git_receipt(source)
    owned_before = cw._owned_baseline_digest(source, order.allowed_mutation_paths)
    subprocess.run(["git", "apply", "--index"], cwd=source, input=capture["patch"], check=True)
    invocation.mkdir(parents=True, exist_ok=True)
    (invocation / "capsule-patch.bin").write_bytes(capture["patch"])
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
    journal = cw.InvocationJournal(invocation / "journal.json", order.logical_invocation_id)
    journal.transition("prepared", launch_nonce="n")
    journal.transition("cloning", capsule=str(capsule))
    journal.transition(
        "running",
        authoritative_before=auth_before,
        capsule_before=cw.git_receipt(capsule),
        capsule_receipt=capsule_receipt,
        owned_baseline_before=owned_before,
    )
    journal.transition("terminal", process=process.__dict__)
    journal.transition(
        "validated",
        result=_writer_result(sha),
        worker_id=None,
        authoritative_after=cw.git_receipt(source),
    )
    journal.transition(
        "importing",
        import_lock="seed",
        patch=str(invocation / "capsule-patch.bin"),
        capture={
            key: capture[key] for key in ("patch_digest", "touched", "allowed", "metadata", "empty")
        },
    )

    outcome = workers.run(order, _owner())
    assert outcome.status == "succeeded"
    assert outcome.receipts["change"]["import_result"] == "resumed"  # finalized, never re-applied
    assert (source / "src" / "impl.txt").read_text() == "impl\n"
    assert (journal.read() or {}).get("state") == "completed"


class _RelaunchGuard:
    harness = "codex"

    def preflight(self, route, authority="read_only"):
        return {"executable": "/usr/bin/codex", "version": "codex 1", "harness": "codex"}

    def command(self, route, prompt, schema_path, capsule, authority="read_only"):
        raise AssertionError("resuming an importing journal must never relaunch the model")

    def session_command(self, *args, **kwargs):
        raise AssertionError("a writer order never carries a provider session")


# ─── flow-lhjs: live-dispatch CAS (dispatch_generation, route_snapshot, lease_fence) ─────────


def _drive_after_validation(
    tmp_path: Path,
    source: Path,
    order: cw.WorkOrder,
    observer,
    *,
    before_import=None,
) -> dict:
    """Set up a validated capsule_writer journal, then run _import_after_validation."""
    capsule = tmp_path / "capsule"
    cw.create_private_clone(source, order.source_sha, capsule)
    (capsule / "src" / "tomodify.txt").write_text("changed\n", encoding="utf-8")
    workers = cw.CognitiveWorkers(
        artifact_root=tmp_path / "artifacts",
        capsule_root=tmp_path / "capsules",
        dispatch_observer=observer,
    )
    invocation = workers._invocation_dir(order.logical_invocation_id)
    journal = cw.InvocationJournal(invocation / "journal.json", order.logical_invocation_id)
    for st in ("prepared", "cloning"):
        journal.transition(st)
    journal.transition(
        "running",
        authoritative_before=cw.git_receipt(source),
        owned_baseline_before=cw._owned_baseline_digest(source, order.allowed_mutation_paths),
    )
    journal.transition("terminal")
    journal.transition("validated")
    if before_import is not None:
        before_import()
    return workers._import_after_validation(order, source, capsule, journal)


@pytest.mark.parametrize("field", ["dispatch_generation", "route_snapshot", "lease_fence"])
def test_import_after_validation_refuses_on_observed_dispatch_drift(
    tmp_path: Path, field: str
) -> None:
    source, sha = _baseline_repo(tmp_path)
    order = _order(source, sha)
    frozen = _frozen_observer(order, source)
    before = (source / "src" / "tomodify.txt").read_text()

    def drifted_observer(_order: cw.WorkOrder, _source: Path) -> dict:
        return {**frozen, field: "DRIFTED"}

    with pytest.raises(cw.WorkerFailure) as err:
        _drive_after_validation(tmp_path, source, order, drifted_observer)
    assert err.value.code == "baseline_mismatch"
    assert field in str(err.value)  # the drifted dimension is named
    assert (source / "src" / "tomodify.txt").read_text() == before  # owned file untouched


def test_import_after_validation_applies_when_observed_dispatch_matches(tmp_path: Path) -> None:
    # Over-refusal guard: a matching live observation must not block a legitimate import. (Reverting
    # the observer wiring also returns the order's values, so this succeeds either way; it is not a
    # killer, unlike the drift and disk-read tests.)
    source, sha = _baseline_repo(tmp_path)
    order = _order(source, sha)
    receipt = _drive_after_validation(tmp_path, source, order, _frozen_observer)
    assert receipt["import_result"] == "applied"
    assert (source / "src" / "tomodify.txt").read_text() == "changed\n"


def _seed_run_state(source: Path, *, run_id: str, stage: str, generation: int) -> tuple[str, str]:
    """Write a real state.json/run.lock/route-snapshot under .flow/runs; return (digest, nonce)."""
    import agent_routes
    import lease
    import state
    from _timeutil import utcnow_iso

    td = source / ".flow" / "runs" / "TICKET"
    td.mkdir(parents=True, exist_ok=True)
    head = _git(source, "rev-parse", "HEAD")
    state.init(td, "TICKET", "beads", [stage], run_id=run_id)
    for step in range(generation):
        state.begin_stage(td, stage, head)
        if step + 1 < generation:
            state.force_stage_status(td, stage, "pending")
    run_lease = lease.acquire(
        td,
        run_id,
        3600,
        utcnow_iso(),
        current_boot="boot",
        hostname=socket.gethostname(),
        cwd=str(source),
    )
    body = {
        "schema": agent_routes.SCHEMA,
        "owner_harness": "codex",
        "routes": {},
        "stage_execution": {},
    }
    snapshot = {**body, "digest": agent_routes.canonical_digest(body)}
    (td / "route-snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")
    return snapshot["digest"], run_lease.session_nonce


def test_observe_live_dispatch_reads_disk_values_not_the_order(tmp_path: Path) -> None:
    # Kills the "default observer is genuinely live" hunk: the run on disk holds values the order
    # never froze, so a stub echoing the order would return the order's values and fail here.
    source, sha = _baseline_repo(tmp_path)
    digest, nonce = _seed_run_state(source, run_id="run-1", stage="implement", generation=2)
    order = dataclasses.replace(_order(source, sha), stage_generation=1)
    observed = cw.observe_live_dispatch(order, source)
    assert observed["dispatch_generation"] == 2 != order.stage_generation
    assert observed["route_snapshot"] == digest != order.route_snapshot_digest
    assert observed["lease_fence"] == nonce != order.lease_fence


def test_import_refuses_when_live_state_is_corrupt_recovered_from_bak(tmp_path: Path) -> None:
    # Fail-open killer: live state.json corrupts, its newest .bak still holds the pre-bump
    # generation the order froze, so a read that discards the recovery exit_code reports the stale
    # generation, it matches the order, and the CAS wrongly applies over real drift. Generation is
    # the only guard that moves on a pending->in_progress re-dispatch, so this is a true fail-open.
    source, sha = _baseline_repo(tmp_path)
    digest, nonce = _seed_run_state(source, run_id="run-1", stage="implement", generation=2)
    order = dataclasses.replace(
        _order(source, sha),
        stage_generation=1,  # frozen at G; the newest .bak holds this pre-bump value
        route_snapshot_digest=digest,
        lease_fence=nonce,
    )
    state_json = source / ".flow" / "runs" / "TICKET" / "state.json"

    def corrupt_live_state() -> None:
        state_json.write_text("{ not json", encoding="utf-8")

    with pytest.raises(cw.WorkerFailure) as err:
        _drive_after_validation(
            tmp_path, source, order, cw.observe_live_dispatch, before_import=corrupt_live_state
        )
    assert err.value.code == "baseline_mismatch"
    assert "dispatch_generation" in str(err.value)  # pins the refusal to the generation dimension


# ─── flow-xvhd: owned-baseline excludes gitignored content the patch can never carry ─────────


def test_owned_baseline_digest_excludes_gitignored_churn(tmp_path: Path) -> None:
    source, _ = _baseline_repo(tmp_path)
    (source / "src" / ".gitignore").write_text("cache/\n", encoding="utf-8")
    (source / "src" / "cache").mkdir()
    _git(source, "add", "-A")
    _git(source, "commit", "-qm", "ignore cache")
    base = cw._owned_baseline_digest(source, ("src",))
    (source / "src" / "cache" / "junk.bin").write_bytes(b"\x00" * 4096)  # gitignored churn
    assert cw._owned_baseline_digest(source, ("src",)) == base  # ignored churn does not drift
    (source / "src" / "tomodify.txt").write_text("real edit\n", encoding="utf-8")  # tracked owned
    assert cw._owned_baseline_digest(source, ("src",)) != base  # a real tracked change still drifts


def test_owned_baseline_digest_skips_submodule_gitlink(tmp_path: Path) -> None:
    # A submodule gitlink lists (via ls-files --cached) as a directory path, so read_bytes() on it
    # raises IsADirectoryError and crashes the CAS observation unless the gitlink is skipped.
    source, _ = _baseline_repo(tmp_path)
    (source / "src" / "sub").mkdir()  # the submodule's on-disk working dir
    commit = _git(source, "rev-parse", "HEAD")
    _git(source, "update-index", "--add", "--cacheinfo", f"160000,{commit},src/sub")
    assert "src/sub" in cw._tracked_capturable_paths(source)
    digest = cw._owned_baseline_digest(source, ("src",))  # must not raise IsADirectoryError
    assert isinstance(digest, str)
    assert len(digest) == 64


def test_import_after_validation_survives_gitignored_churn(tmp_path: Path) -> None:
    source, _ = _baseline_repo(tmp_path)
    (source / "src" / ".gitignore").write_text("cache/\n", encoding="utf-8")
    (source / "src" / "cache").mkdir()
    _git(source, "add", "-A")
    _git(source, "commit", "-qm", "ignore cache")
    order = _order(source, _git(source, "rev-parse", "HEAD"))

    def churn() -> None:
        (source / "src" / "cache" / "churn.bin").write_bytes(b"\xff" * 2048)

    receipt = _drive_after_validation(
        tmp_path, source, order, _frozen_observer, before_import=churn
    )
    assert receipt["import_result"] == "applied"  # ignored churn did not trip baseline_mismatch
    assert (source / "src" / "tomodify.txt").read_text() == "changed\n"


# ─── flow-c71o: git_receipt covers the seed machinery's refs/flow/ namespace ─────────────────


def test_git_receipt_detects_a_stray_flow_ref(tmp_path: Path) -> None:
    source, _ = _baseline_repo(tmp_path)
    before = cw.git_receipt(source)
    assert "flow_refs" in before
    # The read_only_violation guard trips on authoritative_before['digest'] != authoritative_after,
    # so a stray refs/flow/* in the authoritative repo must move that digest to stay caught.
    tree = _git(source, "write-tree")
    _git(source, "update-ref", cw.SEED_BASELINE_REF, tree)
    after = cw.git_receipt(source)
    assert before["digest"] != after["digest"]


def test_seed_helper_writes_its_ref_only_in_the_capsule(tmp_path: Path) -> None:
    source, sha = _baseline_repo(tmp_path)
    (source / "src" / "tomodify.txt").write_text("seed change\n", encoding="utf-8")
    seed = cw._capture_working_delta(source, sha)
    _git(source, "checkout", "--", "src/tomodify.txt")  # restore the authoritative worktree
    assert seed  # a non-empty seed patch, so the helper writes the ref
    capsule = tmp_path / "capsule"
    cw.create_private_clone(source, sha, capsule, seed=seed)
    assert _git(capsule, "for-each-ref", cw.SEED_BASELINE_REF)  # seeded in the capsule
    assert not _git(source, "for-each-ref", "refs/flow/")  # authoritative repo has no refs/flow/*


# ─── flow-jrv4: the LIVE implementer through the real activated catalog ───────────────────────
#
# Unlike the run()-wiring tests above (which monkeypatch active + a fake prompt/schema), these
# drive the real activated implementer end to end: prepare_work_order seals allowed_mutation_paths
# from baseline.json, and run() launches through the real ROLE_CATALOG, prompt, and schema.


def _impl_facts(sha: str) -> dict:
    return {
        "stage_implement": "Implement the ticket with TDD.",
        "ticket": {"key": "T-1"},
        "source_sha": sha,
        "plan": "add src/impl.txt and its test",
        "planned_files": ["src"],
        "report_contract": "summary + evidence + exact source_sha",
    }


def _write_baseline(ticket_dir: Path, planned: list[str]) -> None:
    ticket_dir.mkdir(parents=True, exist_ok=True)
    (ticket_dir / "baseline.json").write_text(
        json.dumps({"head_sha": "x", "planned_files": planned, "blobs": {}}),
        encoding="utf-8",
    )


def _implement_descriptor(sha: str, ticket_dir: Path) -> dict:
    return {
        "stage": "implement",
        "generation": 1,
        "cognitive_substeps": {
            "implement": {
                "logical_invocation_id": "run-1:implement:implement:1",
                "run_id": "run-1",
                "stage": "implement",
                "substep": "implement",
                "stage_generation": 1,
                "source_sha": sha,
                "route_snapshot_digest": "b" * 64,
                "profile": "implementer",
                "desired_route": {"harness": "codex", "model": "fake", "effort": "high"},
                "activation": "pending",
                "conditional": False,
                "lease_fence": "fence-1",
                "ticket_dir": str(ticket_dir),
            }
        },
    }


def _prepare_implementer(
    tmp_path: Path, source: Path, sha: str, planned: list[str]
) -> cw.WorkOrder:
    ticket_dir = tmp_path / "td"
    _write_baseline(ticket_dir, planned)
    return cw.prepare_work_order(
        _implement_descriptor(sha, ticket_dir),
        substep="implement",
        source_root=source,
        input_bundle=source / "src" / "keep.txt",
        facts=_impl_facts(sha),
        output=tmp_path / "orders" / "implement.json",
    )


class _ReportWriterAdapter:
    """Writes one file and returns a valid implementation-report/v1 citing the capsule HEAD."""

    harness = "codex"

    def __init__(self, relpath: str) -> None:
        self.relpath = relpath
        self.launches = 0
        self.launched_authority: str | None = None

    def preflight(self, route, authority="read_only"):
        self.launched_authority = authority
        return {"executable": "/usr/bin/codex", "version": "codex 1", "harness": "codex"}

    def command(self, route, prompt, schema_path, capsule, authority="read_only"):
        self.launches += 1
        # A capsule_writer launches with the WRITABLE sandbox, not the read-only planning mode.
        assert authority == "capsule_writer"
        parent = str(Path(self.relpath).parent)
        body = (
            "import json,sys,pathlib,subprocess;"
            f"pathlib.Path({parent!r}).mkdir(parents=True,exist_ok=True);"
            f"pathlib.Path({self.relpath!r}).write_text('impl\\n');"
            "sha=subprocess.run(['git','rev-parse','HEAD'],capture_output=True,text=True)"
            ".stdout.strip();"
            "sys.stdout.write(json.dumps({'result':{'summary':'built it',"
            "'evidence':'wrote a planned file; tests green','source_sha':sha}}))"
        )
        return [sys.executable, "-c", body]

    def session_command(self, *args, **kwargs):
        raise AssertionError("an implementer order never carries a provider session")


def test_prepare_work_order_seals_planned_files_as_allowed_mutation_paths(tmp_path: Path) -> None:
    # KILLER for the prepare_work_order sealing hunk: strip it and allowed_mutation_paths is empty.
    source, sha = _baseline_repo(tmp_path)
    order = _prepare_implementer(tmp_path, source, sha, ["src/impl.txt", "./src/impl.txt", "pkg/x"])
    assert order.profile == "implementer"
    assert order.authority == "capsule_writer"
    # planned_files became the order's allowed set: normalized (./ stripped) and de-duplicated.
    assert order.allowed_mutation_paths == ("src/impl.txt", "pkg/x")
    # A clean authoritative worktree seals no seed patch (the implementer double-counts otherwise).
    assert order.seed_patch is None
    assert order.seed_digest is None


def test_active_implementer_imports_a_planned_file_change_end_to_end(tmp_path: Path) -> None:
    # KILLER for the ROLE_CATALOG activation flip: strip it and run() raises capability_missing.
    source, sha = _baseline_repo(tmp_path)
    order = _prepare_implementer(tmp_path, source, sha, ["src"])
    assert order.allowed_mutation_paths == ("src",)
    adapter = _ReportWriterAdapter("src/impl.txt")
    workers = cw.CognitiveWorkers(
        artifact_root=tmp_path / "artifacts",
        capsule_root=tmp_path / "capsules",
        adapters={"codex": adapter},
        dispatch_observer=_frozen_observer,
    )

    outcome = workers.run(order, _owner())

    assert outcome.status == "succeeded"
    assert adapter.launches == 1
    assert adapter.launched_authority == "capsule_writer"  # preflighted the writable sandbox
    # The real model-facing contract validated (no monkeypatched prompt or schema).
    assert outcome.result is not None
    assert set(outcome.result) == {"summary", "evidence", "source_sha"}
    assert outcome.result["source_sha"] == sha

    change = outcome.receipts["change"]
    assert change["schema"] == cw.CHANGE_RECEIPT_SCHEMA
    assert change["import_result"] == "applied"
    assert change["touched_paths"] == ["src/impl.txt"]
    assert change["allowed_paths"] == ["src"]
    allowed = frozenset(change["allowed_paths"])
    assert all(cw._within_allowed(t, allowed) for t in change["touched_paths"])  # touched ⊆ allowed
    # The change LANDED in the authoritative worktree, staged for the commit stage.
    assert (source / "src" / "impl.txt").read_text() == "impl\n"
    assert "src/impl.txt" in _git(source, "diff", "--cached", "--name-only")
    # The capsule is disposed after a successful import.
    assert outcome.receipts["disposal"]["absent"] is True
    capsule = (tmp_path / "capsules") / hashlib.sha256(
        f"{order.logical_invocation_id}:{order.generation}".encode()
    ).hexdigest()
    assert not capsule.exists()


def test_active_implementer_refuses_import_outside_allowed_paths(tmp_path: Path) -> None:
    # Hole-closing safety test: a worker that touches a path outside allowed_mutation_paths is an
    # ownership_violation and NOTHING is imported. (Also fails on the catalog flip: without it run()
    # raises capability_missing, not ownership_violation.)
    source, sha = _baseline_repo(tmp_path)
    order = _prepare_implementer(tmp_path, source, sha, ["src"])  # only src/ is allowed
    before = cw.git_receipt(source)["digest"]
    adapter = _ReportWriterAdapter("other/outside.txt")  # writes OUTSIDE the allowed set
    workers = cw.CognitiveWorkers(
        artifact_root=tmp_path / "artifacts",
        capsule_root=tmp_path / "capsules",
        adapters={"codex": adapter},
        dispatch_observer=_frozen_observer,
    )

    with pytest.raises(cw.WorkerFailure) as err:
        workers.run(order, _owner())

    assert err.value.code == "ownership_violation"
    # The authoritative worktree is byte-identical: the unowned change never reached it.
    assert cw.git_receipt(source)["digest"] == before
    assert (source / "other" / "outside.txt").read_text() == "outside\n"  # source copy untouched
    assert not _git(source, "diff", "--cached", "--name-only")  # nothing staged
    # The capsule is preserved as recovery evidence (a capture failure never disposes it).
    capsule = (tmp_path / "capsules") / hashlib.sha256(
        f"{order.logical_invocation_id}:{order.generation}".encode()
    ).hexdigest()
    assert capsule.exists()


# ─── flow-jrv4: the LIVE review-loop fixers (review_fixer, revision_fixer) ─────────────────────
#
# The two review-loop fixers activate in this increment. They share the implementer's closed
# report contract ({summary, evidence, source_sha}) and its allowed-paths-from-planned_files seal,
# but run post-implement over the ticket's uncommitted edits, so a seed applies.


def _review_fixer_facts(sha: str) -> dict:
    return {
        "stage_review_loop": "Address the review findings.",
        "ticket": {"key": "T-1"},
        "source_sha": sha,
        "review_findings": "CI failed on src/impl.txt; fix it",
        "planned_files": ["src"],
        "report_contract": "summary + evidence + exact source_sha",
    }


def _revision_fixer_facts(sha: str) -> dict:
    return {
        "stage_review_loop": "Apply the requested revision.",
        "ticket": {"key": "T-1"},
        "source_sha": sha,
        "revision_instruction": "rename the label in src/impl.txt",
        "planned_files": ["src"],
        "report_contract": "summary + evidence + exact source_sha",
    }


def _fixer_descriptor(
    sha: str, ticket_dir: Path, profile: str, substep: str, stage: str = "review_loop"
) -> dict:
    return {
        "stage": stage,
        "generation": 1,
        "cognitive_substeps": {
            substep: {
                "logical_invocation_id": f"run-1:{stage}:{substep}:1",
                "run_id": "run-1",
                "stage": stage,
                "substep": substep,
                "stage_generation": 1,
                "source_sha": sha,
                "route_snapshot_digest": "b" * 64,
                "profile": profile,
                "desired_route": {"harness": "codex", "model": "fake", "effort": "high"},
                "activation": "pending",
                "conditional": True,
                "lease_fence": "fence-1",
                "ticket_dir": str(ticket_dir),
            }
        },
    }


def _prepare_fixer(
    tmp_path: Path,
    source: Path,
    sha: str,
    planned: list[str],
    *,
    profile: str,
    facts: dict,
    substep: str,
    stage: str = "review_loop",
) -> cw.WorkOrder:
    ticket_dir = tmp_path / "td"
    _write_baseline(ticket_dir, planned)
    return cw.prepare_work_order(
        _fixer_descriptor(sha, ticket_dir, profile, substep, stage),
        substep=substep,
        source_root=source,
        input_bundle=source / "src" / "keep.txt",
        facts=facts,
        output=tmp_path / "orders" / f"{substep}.json",
    )


def test_active_review_fixer_imports_a_planned_file_change_via_cas(tmp_path: Path) -> None:
    # KILLER for the review_fixer catalog activation: strip it and run() raises capability_missing.
    source, sha = _baseline_repo(tmp_path)
    order = _prepare_fixer(
        tmp_path,
        source,
        sha,
        ["src"],
        profile="review_fixer",
        facts=_review_fixer_facts(sha),
        substep="review_fix",
    )
    assert order.profile == "review_fixer"
    assert order.authority == "capsule_writer"
    assert order.allowed_mutation_paths == ("src",)
    adapter = _ReportWriterAdapter("src/impl.txt")
    workers = cw.CognitiveWorkers(
        artifact_root=tmp_path / "artifacts",
        capsule_root=tmp_path / "capsules",
        adapters={"codex": adapter},
        dispatch_observer=_frozen_observer,
    )

    outcome = workers.run(order, _owner())

    assert outcome.status == "succeeded"
    assert adapter.launched_authority == "capsule_writer"
    assert outcome.result is not None
    assert set(outcome.result) == {"summary", "evidence", "source_sha"}
    assert outcome.result["source_sha"] == sha

    change = outcome.receipts["change"]
    assert change["schema"] == cw.CHANGE_RECEIPT_SCHEMA
    assert change["import_result"] == "applied"
    assert change["touched_paths"] == ["src/impl.txt"]
    assert change["allowed_paths"] == ["src"]
    allowed = frozenset(change["allowed_paths"])
    assert all(cw._within_allowed(t, allowed) for t in change["touched_paths"])  # touched ⊆ allowed
    assert (source / "src" / "impl.txt").read_text() == "impl\n"  # change LANDED
    assert "src/impl.txt" in _git(source, "diff", "--cached", "--name-only")
    assert outcome.receipts["disposal"]["absent"] is True  # capsule disposed after import


def test_active_revision_fixer_imports_a_planned_file_change_via_cas(tmp_path: Path) -> None:
    # KILLER for the revision_fixer activation: strip it and run() raises capability_missing.
    source, sha = _baseline_repo(tmp_path)
    order = _prepare_fixer(
        tmp_path,
        source,
        sha,
        ["src"],
        profile="revision_fixer",
        facts=_revision_fixer_facts(sha),
        substep="revision_fix",
    )
    assert order.profile == "revision_fixer"
    assert order.authority == "capsule_writer"
    adapter = _ReportWriterAdapter("src/impl.txt")
    workers = cw.CognitiveWorkers(
        artifact_root=tmp_path / "artifacts",
        capsule_root=tmp_path / "capsules",
        adapters={"codex": adapter},
        dispatch_observer=_frozen_observer,
    )

    outcome = workers.run(order, _owner())

    assert outcome.status == "succeeded"
    change = outcome.receipts["change"]
    assert change["import_result"] == "applied"
    assert change["touched_paths"] == ["src/impl.txt"]
    assert (source / "src" / "impl.txt").read_text() == "impl\n"
    assert outcome.receipts["disposal"]["absent"] is True


def test_active_fixer_refuses_import_outside_allowed_paths(tmp_path: Path) -> None:
    # A fixer touching a path outside planned_files is an ownership_violation; nothing is imported.
    source, sha = _baseline_repo(tmp_path)
    order = _prepare_fixer(
        tmp_path,
        source,
        sha,
        ["src"],
        profile="review_fixer",
        facts=_review_fixer_facts(sha),
        substep="review_fix",
    )
    before = cw.git_receipt(source)["digest"]
    adapter = _ReportWriterAdapter("other/outside.txt")  # OUTSIDE the allowed set
    workers = cw.CognitiveWorkers(
        artifact_root=tmp_path / "artifacts",
        capsule_root=tmp_path / "capsules",
        adapters={"codex": adapter},
        dispatch_observer=_frozen_observer,
    )

    with pytest.raises(cw.WorkerFailure) as err:
        workers.run(order, _owner())

    assert err.value.code == "ownership_violation"
    assert cw.git_receipt(source)["digest"] == before  # authoritative worktree untouched
    assert not _git(source, "diff", "--cached", "--name-only")  # nothing staged


def test_seeded_fixer_imports_only_its_own_delta_not_the_seed(tmp_path: Path) -> None:
    # KILLER for the seed-baseline capture fix (flow-wtm4): a fixer runs post-implement over the
    # ticket's uncommitted edits (the seed). WITHOUT the fix, _capture_capsule_patch diffs against
    # source_sha and double-counts the seed; re-applying the seed hunk onto the authoritative
    # worktree that already carries it fails "does not match index" -> patch_import_conflict.
    source, sha = _baseline_repo(tmp_path)
    # The ticket's uncommitted implement edit on a PLANNED file, unstaged (so the authoritative
    # index still holds the base and a double-counted seed hunk cannot apply).
    (source / "src" / "tomodify.txt").write_text("seeded ticket edit\n", encoding="utf-8")

    order = _prepare_fixer(
        tmp_path,
        source,
        sha,
        ["src"],
        profile="review_fixer",
        facts=_review_fixer_facts(sha),
        substep="review_fix",
    )
    # A dirty authoritative worktree seals the uncommitted delta as a digest-bound seed.
    assert order.seed_patch is not None
    assert hashlib.sha256(Path(order.seed_patch).read_bytes()).hexdigest() == order.seed_digest

    # The fixer writes ONE new planned file on top of the seed; it never touches the seeded file.
    adapter = _ReportWriterAdapter("src/fix.txt")
    workers = cw.CognitiveWorkers(
        artifact_root=tmp_path / "artifacts",
        capsule_root=tmp_path / "capsules",
        adapters={"codex": adapter},
        dispatch_observer=_frozen_observer,
    )

    outcome = workers.run(order, _owner())

    assert outcome.status == "succeeded"
    change = outcome.receipts["change"]
    assert change["import_result"] == "applied"
    # Captured against the SEEDED baseline: ONLY the writer's own file, never the seeded edit.
    assert change["touched_paths"] == ["src/fix.txt"]
    # The writer's delta LANDED; the authoritative worktree keeps its uncommitted seed intact.
    assert (source / "src" / "fix.txt").read_text() == "impl\n"
    assert (source / "src" / "tomodify.txt").read_text() == "seeded ticket edit\n"


def test_code_review_review_fixer_seeds_pre_commit_and_imports_only_its_delta(
    tmp_path: Path,
) -> None:
    # flow-7yjk: code_review routes its fix through the review_fixer capsule PRE-COMMIT, so the
    # worktree carries implement's uncommitted edits (the seed). The fixer's patch is captured
    # against the seeded baseline, so ONLY its own delta imports; without the SEED_BASELINE_REF
    # capture (flow-wtm4) the seed double-counts and re-applying it fails "does not match index"
    # -> patch_import_conflict. Same seed+CAS machinery as review_loop, driven from code_review.
    source, sha = _baseline_repo(tmp_path)
    # implement's uncommitted edit on a PLANNED file, unstaged (the pre-commit working state).
    (source / "src" / "tomodify.txt").write_text("implement's uncommitted edit\n", encoding="utf-8")

    order = _prepare_fixer(
        tmp_path,
        source,
        sha,
        ["src"],
        profile="review_fixer",
        facts=_review_fixer_facts(sha),
        substep="review_fix",
        stage="code_review",
    )
    assert order.stage == "code_review"
    assert order.authority == "capsule_writer"
    # The pre-commit dirty worktree seals the uncommitted delta as a digest-bound seed.
    assert order.seed_patch is not None
    assert hashlib.sha256(Path(order.seed_patch).read_bytes()).hexdigest() == order.seed_digest

    adapter = _ReportWriterAdapter("src/fix.txt")  # writes ONE new planned file on top of the seed
    workers = cw.CognitiveWorkers(
        artifact_root=tmp_path / "artifacts",
        capsule_root=tmp_path / "capsules",
        adapters={"codex": adapter},
        dispatch_observer=_frozen_observer,
    )

    outcome = workers.run(order, _owner())

    assert outcome.status == "succeeded"
    change = outcome.receipts["change"]
    assert change["import_result"] == "applied"
    # ONLY the fixer's own file imports; implement's seeded edit is never re-imported.
    assert change["touched_paths"] == ["src/fix.txt"]
    allowed = frozenset(change["allowed_paths"])
    assert all(cw._within_allowed(t, allowed) for t in change["touched_paths"])  # touched ⊆ allowed
    assert (source / "src" / "fix.txt").read_text() == "impl\n"  # the fix LANDED
    assert (source / "src" / "tomodify.txt").read_text() == "implement's uncommitted edit\n"
    assert outcome.receipts["disposal"]["absent"] is True  # capsule disposed after import


def _dispatch_git_workspace(tmp_path: Path) -> tuple[Path, str]:
    """A real git repo that is also a Flow workspace, so cmd_next seals over a real HEAD."""
    root, sha = _baseline_repo(tmp_path, name="ws")
    flow = root / ".flow"
    flow.mkdir()
    (flow / ".initialized").touch()
    (flow / "workspace.toml").write_text(
        "[tracker]\n"
        'backend = "beads"\n'
        "[tracker.beads]\n"
        'prefix = "T"\n'
        "[pipeline]\n"
        'stages = ["ticket", "implement"]\n'
        "[pipeline.handlers]\n"
        'ticket = "inline"\n'
        'implement = "subagent:general-purpose"\n'
        "[memory]\n"
        'namespace = "T"\n'
        "auto_recall = true\n"
        "compounding = false\n"
        'recall_by = ["branch"]\n'
        "recall_top_n = 5\n",
        encoding="utf-8",
    )
    return root, sha


def test_real_dispatch_seals_ticket_dir_through_prepare_into_run(tmp_path: Path) -> None:
    # KILLER for the dispatch_stage ticket_dir seal (flow-p9o5): real dispatch keys the implement
    # substep 'main' and seals ticket_dir; prepare reads planned_files THROUGH that ticket_dir.
    # Revert the seal and _sealed_planned_files returns () -> the planned change is refused
    # ownership_violation. Every other live import test hand-builds substep/baseline and misses it.
    source, sha = _dispatch_git_workspace(tmp_path)
    ds.cmd_init(source, "T-1")
    td = source / ".flow" / "runs" / "T-1"
    agent_routes.snapshot(source, "codex", output_path=td / "route-snapshot.json")

    ds.cmd_next(source, "T-1")  # ticket stage
    ds.cmd_finish(source, "T-1", "ticket", "completed")
    rc, descriptor = ds.cmd_next(source, "T-1")  # implement stage
    assert rc == 0, descriptor
    assert descriptor["stage"] == "implement"

    sealed = descriptor["cognitive_substeps"]
    assert set(sealed) == {"main"}  # real dispatch keys a non-composite agent stage 'main'
    main = sealed["main"]
    assert main["profile"] == "implementer"
    assert main["source_sha"] == sha
    assert main["ticket_dir"] == str(td)  # the seal under revert

    # records_diff_baseline pre-hook writes baseline.json under the SEALED ticket_dir.
    (td / "baseline.json").write_text(
        json.dumps({"head_sha": sha, "planned_files": ["src"], "blobs": {}}),
        encoding="utf-8",
    )

    order = cw.prepare_work_order(
        descriptor,
        substep="main",
        source_root=source,
        input_bundle=source / "src" / "keep.txt",
        facts=_impl_facts(sha),
        output=tmp_path / "orders" / "main.json",
    )
    # prepare located planned_files ONLY via the sealed ticket_dir; reverting the seal empties this.
    assert order.allowed_mutation_paths == ("src",)

    adapter = _ReportWriterAdapter("src/impl.txt")
    workers = cw.CognitiveWorkers(
        artifact_root=tmp_path / "artifacts",
        capsule_root=tmp_path / "capsules",
        adapters={"codex": adapter},
        dispatch_observer=_frozen_observer,
    )
    # The owner proof carries the run's real dispatch-minted run_id and lease fence.
    owner = cw.OwnerProof(
        owner_id="owner", harness="codex", run_id=order.run_id, lease_fence=order.lease_fence
    )

    outcome = workers.run(order, owner)

    assert outcome.status == "succeeded"
    change = outcome.receipts["change"]
    assert change["import_result"] == "applied"
    assert change["touched_paths"] == ["src/impl.txt"]
    assert (source / "src" / "impl.txt").read_text() == "impl\n"  # planned change imported
