"""Fault suite for the writer capture + compare-and-swap import machinery (flow-d8am).

Every writer profile stays active=False, so none of this runs through a live run(); the
helpers are driven directly against real git worktrees and fake clock/lock inputs. Each test
fails when its load-bearing source hunk is stripped (verified by surgical revert).
"""

from __future__ import annotations

import dataclasses
import hashlib
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import cognitive_workers as cw
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
        artifact_root=tmp_path / "artifacts", capsule_root=tmp_path / "capsules"
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


_WRITER_RESULT = {"summary": "implemented", "changed_files": ["src/impl.txt"]}


class _WriterAdapter:
    harness = "codex"

    def __init__(self) -> None:
        self.launches = 0

    def preflight(self, route, authority="read_only"):
        return {"executable": "/usr/bin/codex", "version": "codex 1", "harness": "codex"}

    def command(self, route, prompt, schema_path, capsule, authority="read_only"):
        self.launches += 1
        body = (
            "import json,sys,pathlib;"
            "pathlib.Path('src').mkdir(exist_ok=True);"
            "pathlib.Path('src/impl.txt').write_text('impl\\n');"
            f"sys.stdout.write(json.dumps({{'result': {_WRITER_RESULT!r}}}))"
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
        result=_WRITER_RESULT,
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
