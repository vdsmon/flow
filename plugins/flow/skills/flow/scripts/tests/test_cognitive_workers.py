from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, override

import pytest

import cognitive_workers as cw
from _locking import flock_blocking


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "source"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "flow@example.test")
    _git(root, "config", "user.name", "Flow Test")
    (root / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git(root, "add", "tracked.txt")
    _git(root, "commit", "-qm", "base")
    return root, _git(root, "rev-parse", "HEAD")


def test_catalog_activates_readers_e2e_and_the_importing_fixers() -> None:
    assert set(cw.ROLE_CATALOG) == {
        "planner",
        "plan_assessor",
        "implementer",
        "e2e",
        "code_reviewer",
        "diff_reviewer",
        "guard_reviewer",
        "review_fixer",
        "revision_fixer",
        "review_brief_author",
        "reflector",
        "machinery_fixer",
    }
    active = {name for name, policy in cw.ROLE_CATALOG.items() if policy.active}
    readers = {
        "planner",
        "plan_assessor",
        "code_reviewer",
        "diff_reviewer",
        "guard_reviewer",
        "review_brief_author",
        "reflector",
    }
    assert active == readers | {"e2e", "implementer", "review_fixer", "revision_fixer"}
    assert all(cw.ROLE_CATALOG[name].authority == "read_only" for name in readers)
    assert cw.ROLE_CATALOG["e2e"].authority == "disposable_writer"
    # The implementer and the two review-loop fixers are activated importing capsule_writers.
    for name in ("implementer", "review_fixer", "revision_fixer"):
        assert cw.ROLE_CATALOG[name].authority == "capsule_writer", name
        assert cw.ROLE_CATALOG[name].active is True, name
    # machinery_fixer stays shadowed (still a capsule_writer) for Phase 5.
    assert cw.ROLE_CATALOG["machinery_fixer"].authority == "capsule_writer"
    assert cw.ROLE_CATALOG["machinery_fixer"].active is False


def test_shadow_writer_is_refused_before_capsule_allocation(tmp_path: Path) -> None:
    order = cw.WorkOrder(
        logical_invocation_id="writer-1",
        generation=1,
        profile="machinery_fixer",
        source_root=str(tmp_path),
        source_sha="a" * 40,
        route={"harness": "codex", "model": "gpt-5.6-luna", "effort": "high"},
        route_snapshot_digest="b" * 64,
        input_bundle=str(tmp_path / "input.json"),
        input_digest="c" * 64,
        facts={},
    )
    workers = cw.CognitiveWorkers(
        artifact_root=tmp_path / "artifacts", capsule_root=tmp_path / "capsules"
    )
    with pytest.raises(cw.WorkerFailure, match="not active"):
        workers.run(order, cw.OwnerProof(owner_id="owner", harness="codex"))
    assert not (tmp_path / "capsules").exists()


def _order(tmp_path: Path, **overrides: Any) -> cw.WorkOrder:
    fields: dict[str, Any] = {
        "logical_invocation_id": "authority-order",
        "generation": 1,
        "profile": "implementer",
        "source_root": str(tmp_path),
        "source_sha": "a" * 40,
        "route": {"harness": "codex", "model": "gpt-5.6-luna", "effort": "high"},
        "route_snapshot_digest": "b" * 64,
        "input_bundle": str(tmp_path / "input.json"),
        "input_digest": "c" * 64,
        "facts": {},
    }
    fields.update(overrides)
    return cw.WorkOrder(**fields)


def test_work_order_authority_is_pinned_to_its_profile(tmp_path: Path) -> None:
    assert _order(tmp_path, profile="plan_assessor").authority == "read_only"
    assert _order(tmp_path, profile="implementer").authority == "capsule_writer"
    assert _order(tmp_path, profile="e2e").authority == "disposable_writer"
    with pytest.raises(cw.WorkerFailure, match="does not match"):
        _order(tmp_path, profile="code_reviewer", authority="capsule_writer")


def test_read_only_order_forbids_allowed_mutation_paths(tmp_path: Path) -> None:
    with pytest.raises(cw.WorkerFailure, match="cannot allow mutation paths"):
        _order(tmp_path, profile="code_reviewer", allowed_mutation_paths=("src/a.py",))


def test_writer_allowed_mutation_paths_reject_escapes(tmp_path: Path) -> None:
    allowed = _order(tmp_path, allowed_mutation_paths=("src/a.py", "pkg/b.py"))
    assert allowed.allowed_mutation_paths == ("src/a.py", "pkg/b.py")
    with pytest.raises(cw.WorkerFailure, match="escapes"):
        _order(tmp_path, allowed_mutation_paths=("../escape.py",))
    with pytest.raises(cw.WorkerFailure, match="repo-relative"):
        _order(tmp_path, allowed_mutation_paths=("/abs/path.py",))


def test_work_order_round_trips_authority_and_paths_and_binds_the_digest(tmp_path: Path) -> None:
    order = _order(tmp_path, allowed_mutation_paths=("src/a.py",))
    mapping = order.to_mapping()
    assert mapping["authority"] == "capsule_writer"
    assert mapping["allowed_mutation_paths"] == ("src/a.py",)
    assert cw.WorkOrder.from_mapping(mapping) == order
    # A JSON hop turns the tuple into a list; the contract re-freezes it on the way back.
    assert cw.WorkOrder.from_mapping(json.loads(json.dumps(mapping))) == order
    assert cw._digest(_order(tmp_path).to_mapping()) != cw._digest(mapping)


def test_capsule_postcondition_is_authority_aware() -> None:
    mutated = ({"digest": "a"}, {"digest": "b"})
    assert cw._capsule_postcondition_ok("capsule_writer", *mutated)
    assert cw._capsule_postcondition_ok("disposable_writer", *mutated)
    assert not cw._capsule_postcondition_ok("read_only", *mutated)
    assert cw._capsule_postcondition_ok("read_only", {"digest": "a"}, {"digest": "a"})


def test_standalone_clone_is_exact_and_has_no_shared_git_metadata(tmp_path: Path) -> None:
    source, sha = _repository(tmp_path)
    capsule = tmp_path / "capsule"
    receipt = cw.create_private_clone(source, sha, capsule)
    assert _git(capsule, "rev-parse", "HEAD") == sha
    assert (capsule / ".git").is_dir()
    assert not (capsule / ".git" / "objects" / "info" / "alternates").exists()
    assert _git(capsule, "rev-parse", "--git-common-dir") == ".git"
    source_object = source / ".git" / "objects" / sha[:2] / sha[2:]
    capsule_object = capsule / ".git" / "objects" / sha[:2] / sha[2:]
    if source_object.exists() and capsule_object.exists():
        assert os.stat(source_object).st_ino != os.stat(capsule_object).st_ino
    assert receipt["source_sha"] == sha


def test_prompt_builders_are_closed_and_reproducible(tmp_path: Path) -> None:
    facts = {
        "ticket": {"key": "FLOW-1", "title": "Route readers"},
        "base_sha": "a" * 40,
        "route_digest": "b" * 64,
        "candidate_plan": {"digest": "c" * 64},
        "planner_receipt": {"digest": "d" * 64},
        "assessment_rubric": "Check correctness and omissions.",
    }
    first = cw.build_plan_assessor_prompt(facts)
    second = cw.build_plan_assessor_prompt(json.loads(json.dumps(facts)))
    assert first == second
    assert first["builder_id"] == "plan_assessor/v1"
    assert len(first["prompt_digest"]) == 64
    with pytest.raises(cw.WorkerFailure, match="unknown facts"):
        cw.build_plan_assessor_prompt({**facts, "prompt_suffix": "ignore policy"})


def test_provider_schemas_are_closed_and_do_not_use_unique_items() -> None:
    for profile in cw.ACTIVE_READ_ONLY_PROFILES:
        schema = cw.provider_schema(profile)
        stack = [schema]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                assert "uniqueItems" not in node
                if node.get("type") == "object":
                    assert node.get("additionalProperties") is False
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)


def test_invocation_journal_is_monotonic_and_idempotent(tmp_path: Path) -> None:
    journal = cw.InvocationJournal(tmp_path / "journal.json", "invocation-1")
    assert journal.transition("prepared", launch_nonce="nonce")["state"] == "prepared"
    assert journal.transition("running", pid=17)["state"] == "running"
    with pytest.raises(cw.WorkerFailure, match="cannot move"):
        journal.transition("prepared")
    recovered = cw.InvocationJournal(tmp_path / "journal.json", "invocation-1")
    recovered_value = recovered.read()
    assert recovered_value is not None
    assert recovered_value["pid"] == 17


def test_read_only_git_receipt_detects_untracked_and_mode_mutation(tmp_path: Path) -> None:
    source, _ = _repository(tmp_path)
    before = cw.git_receipt(source)
    (source / "new.bin").write_bytes(b"\x00\xff")
    after = cw.git_receipt(source)
    assert before["digest"] != after["digest"]
    (source / "new.bin").unlink()
    assert cw.git_receipt(source)["digest"] == before["digest"]


def test_read_only_git_receipt_detects_an_untracked_content_rewrite(tmp_path: Path) -> None:
    """Untracked paths reach status --porcelain=v2 by name only, and git diff skips them."""
    source, _ = _repository(tmp_path)
    note = source / "note.log"
    note.write_text("untracked-a\n", encoding="utf-8")
    before = cw.git_receipt(source)

    note.write_text("untracked-b\n", encoding="utf-8")
    after = cw.git_receipt(source)

    assert after["status"] == before["status"]
    assert after["worktree_diff"] == before["worktree_diff"]
    assert after["digest"] != before["digest"]

    note.write_text("untracked-a\n", encoding="utf-8")
    assert cw.git_receipt(source)["digest"] == before["digest"]


def test_read_only_git_receipt_size_marks_an_over_cap_untracked_file(
    tmp_path: Path, monkeypatch
) -> None:
    """Over the cap the entry carries a size, so an equal-size rewrite escapes the guard."""
    monkeypatch.setattr(cw, "_UNTRACKED_DIGEST_MAX_FILE_BYTES", 8)
    source, _ = _repository(tmp_path)
    big = source / "big.bin"
    big.write_bytes(b"aaaaaaaaaaaa")
    before = cw.git_receipt(source)

    big.write_bytes(b"bbbbbbbbbbbb")
    assert cw.git_receipt(source)["digest"] == before["digest"]

    big.write_bytes(b"bbbbbbbbbbbbb")
    assert cw.git_receipt(source)["digest"] != before["digest"]


def _ignore_runtime_and_caches(source: Path) -> None:
    (source / ".gitignore").write_text("**/.flow/runtime/\ncaches/\n.claude/\n", encoding="utf-8")
    _git(source, "add", ".gitignore")
    _git(source, "commit", "-qm", "ignore runtime and caches")


def test_read_only_git_receipt_detects_runtime_facade_rewrite(tmp_path: Path) -> None:
    source, _ = _repository(tmp_path)
    _ignore_runtime_and_caches(source)
    runtime = source / ".flow" / "runtime"
    runtime.mkdir(parents=True)
    facade = runtime / "flow"
    facade.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    facade.chmod(0o755)
    (runtime / "skill-root").write_text(f"{source}/skill\n", encoding="utf-8")
    before = cw.git_receipt(source)
    assert _git(source, "status", "--porcelain", "--untracked-files=all") == ""

    facade.write_text("#!/usr/bin/env python3\nimport payload\n", encoding="utf-8")
    assert cw.git_receipt(source)["digest"] != before["digest"]
    facade.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    assert cw.git_receipt(source)["digest"] == before["digest"]

    facade.chmod(0o700)
    assert cw.git_receipt(source)["digest"] != before["digest"]
    facade.chmod(0o755)

    (runtime / "skill-root").write_text("/tmp/attacker\n", encoding="utf-8")
    assert cw.git_receipt(source)["digest"] != before["digest"]


def test_read_only_git_receipt_detects_harness_hook_injection(tmp_path: Path) -> None:
    source, _ = _repository(tmp_path)
    _ignore_runtime_and_caches(source)
    settings_dir = source / ".claude"
    settings_dir.mkdir()
    settings = settings_dir / "settings.json"
    settings.write_text('{"model": "opus"}\n', encoding="utf-8")
    local = settings_dir / "settings.local.json"
    local.write_text('{"permissions": {"allow": []}}\n', encoding="utf-8")
    before = cw.git_receipt(source)
    assert _git(source, "status", "--porcelain", "--untracked-files=all") == ""

    hook = '{"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "payload"}]}]}}\n'
    settings.write_text(hook, encoding="utf-8")
    assert _git(source, "status", "--porcelain", "--untracked-files=all") == ""
    assert cw.git_receipt(source)["digest"] != before["digest"]
    settings.write_text('{"model": "opus"}\n', encoding="utf-8")
    assert cw.git_receipt(source)["digest"] == before["digest"]

    local.write_text(hook, encoding="utf-8")
    assert cw.git_receipt(source)["digest"] != before["digest"]


def test_read_only_git_receipt_ignores_gitignored_cache_churn(tmp_path: Path) -> None:
    source, _ = _repository(tmp_path)
    _ignore_runtime_and_caches(source)
    runtime = source / ".flow" / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "envelope.json").write_text('{"generation": 1}\n', encoding="utf-8")
    caches = source / "caches"
    caches.mkdir()
    (caches / "warm.bin").write_bytes(b"warm")
    todos = source / ".claude" / "todos"
    todos.mkdir(parents=True)
    (todos / "session.json").write_text("[]\n", encoding="utf-8")
    before = cw.git_receipt(source)

    (caches / "warm.bin").write_bytes(b"rewarmed")
    (caches / "second.bin").write_bytes(b"more")
    (runtime / "envelope.json").write_text('{"generation": 2}\n', encoding="utf-8")
    (todos / "session.json").write_text('[{"content": "step"}]\n', encoding="utf-8")
    (source / ".claude" / "shell-snapshots").mkdir()
    assert cw.git_receipt(source)["digest"] == before["digest"]


def _runtime_with_facade(source: Path) -> Path:
    _ignore_runtime_and_caches(source)
    runtime = source / ".flow" / "runtime"
    runtime.mkdir(parents=True)
    facade = runtime / "flow"
    facade.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    facade.chmod(0o755)
    (runtime / "memory-root").write_text(f"{source}/memory\n", encoding="utf-8")
    return runtime


def test_read_only_git_receipt_survives_a_vanishing_runtime_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, _ = _repository(tmp_path)
    runtime = _runtime_with_facade(source)
    before = cw.git_receipt(source)

    stat_race = runtime / ".memory-root.abcd1234.tmp"
    stat_race.write_text("published\n", encoding="utf-8")
    read_race = runtime / ".flow.efgh5678.tmp"
    read_race.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    read_race.chmod(0o755)

    real_lstat = Path.lstat
    real_digest = cw._file_digest

    def racing_lstat(self: Path, **kwargs: Any) -> os.stat_result:
        if self == stat_race:
            stat_race.unlink()
        return real_lstat(self, **kwargs)

    def racing_digest(path: Path) -> str:
        if path == read_race:
            read_race.unlink()
        return real_digest(path)

    monkeypatch.setattr(Path, "lstat", racing_lstat)
    monkeypatch.setattr(cw, "_file_digest", racing_digest)
    after = cw.git_receipt(source)
    assert after["digest"] == before["digest"]


def test_read_only_git_receipt_detects_a_deleted_runtime_executable(tmp_path: Path) -> None:
    source, _ = _repository(tmp_path)
    runtime = _runtime_with_facade(source)
    before = cw.git_receipt(source)
    (runtime / "flow").unlink()
    assert cw.git_receipt(source)["digest"] != before["digest"]


def test_unreadable_git_receipt_quarantines_instead_of_escaping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib

    source, sha = _repository(tmp_path)
    input_path = tmp_path / "input.json"
    input_path.write_text('{"candidate":"plan"}\n', encoding="utf-8")
    launched = False

    class Adapter:
        harness = "codex"

        def preflight(self, route, authority="read_only"):
            return {"executable": sys.executable, "version": "fake/1", "harness": "codex"}

        def session_command(self, route, prompt, schema_path, *, thread_id, new_thread_id):
            raise AssertionError("a reader order never carries a provider session")

        def command(self, route, prompt, schema_path, capsule, authority="read_only"):
            nonlocal launched
            launched = True
            result = {
                "verdict": "approve",
                "confidence": "high",
                "summary": "The challenge is bound.",
                "findings": [],
                "assessed_plan_digest": "c" * 64,
            }
            event = {"thread_id": "worker-1", "result": result}
            return [sys.executable, "-c", f"import json; print(json.dumps({event!r}))"]

    real_receipt = cw.git_receipt

    def racing_receipt(root: Path) -> dict[str, Any]:
        if launched:
            raise FileNotFoundError(2, "No such file or directory", str(root))
        return real_receipt(root)

    monkeypatch.setattr(cw, "git_receipt", racing_receipt)
    logical_id = "assessment-receipt-race"
    order = cw.WorkOrder(
        logical_invocation_id=logical_id,
        generation=1,
        profile="plan_assessor",
        source_root=str(source),
        source_sha=sha,
        route={"harness": "codex", "model": "fake", "effort": "high"},
        route_snapshot_digest="b" * 64,
        input_bundle=str(input_path),
        input_digest=hashlib.sha256(input_path.read_bytes()).hexdigest(),
        facts={
            "ticket": {"key": "F-1"},
            "base_sha": sha,
            "route_digest": "b" * 64,
            "candidate_plan": {"digest": "c" * 64},
            "planner_receipt": {"digest": "d" * 64},
            "assessment_rubric": "Check the plan.",
        },
    )
    workers = cw.CognitiveWorkers(
        artifact_root=tmp_path / "artifacts",
        capsule_root=tmp_path / "capsules",
        adapters={"codex": Adapter()},
    )
    with pytest.raises(cw.WorkerFailure) as failure:
        workers.run(order, cw.OwnerProof(owner_id="owner", harness="codex"))
    assert failure.value.code == "artifact_failure"

    token = hashlib.sha256(logical_id.encode()).hexdigest()
    journal = cw.InvocationJournal(
        tmp_path / "artifacts" / "invocations" / token / "journal.json", logical_id
    )
    value = journal.read()
    assert value is not None
    assert value["state"] == "quarantined"
    assert value["disposal"]["quarantined"] is True
    assert not Path(value["capsule"]).exists()


def test_common_executor_returns_durable_outcome_without_second_launch(tmp_path: Path) -> None:
    source, sha = _repository(tmp_path)
    input_path = tmp_path / "input.json"
    input_path.write_text('{"candidate":"plan"}\n', encoding="utf-8")
    launches = 0

    class Adapter:
        harness = "codex"

        def preflight(self, route, authority="read_only"):
            return {"executable": sys.executable, "version": "fake/1", "harness": "codex"}

        def session_command(self, route, prompt, schema_path, *, thread_id, new_thread_id):
            raise AssertionError("a reader order never carries a provider session")

        def command(self, route, prompt, schema_path, capsule, authority="read_only"):
            nonlocal launches
            launches += 1
            result = {
                "verdict": "approve",
                "confidence": "high",
                "summary": "The challenge is bound.",
                "findings": [],
                "assessed_plan_digest": "c" * 64,
            }
            event = {"thread_id": "worker-1", "result": result}
            return [sys.executable, "-c", f"import json; print(json.dumps({event!r}))"]

    order = cw.WorkOrder(
        logical_invocation_id="assessment-1",
        generation=1,
        profile="plan_assessor",
        source_root=str(source),
        source_sha=sha,
        route={"harness": "codex", "model": "fake", "effort": "high"},
        route_snapshot_digest="b" * 64,
        input_bundle=str(input_path),
        input_digest=__import__("hashlib").sha256(input_path.read_bytes()).hexdigest(),
        facts={
            "ticket": {"key": "F-1"},
            "base_sha": sha,
            "route_digest": "b" * 64,
            "candidate_plan": {"digest": "c" * 64},
            "planner_receipt": {"digest": "d" * 64},
            "assessment_rubric": "Check the plan.",
        },
    )
    workers = cw.CognitiveWorkers(
        artifact_root=tmp_path / "artifacts",
        capsule_root=tmp_path / "capsules",
        adapters={"codex": Adapter()},
    )
    first = workers.run(order, cw.OwnerProof(owner_id="owner", harness="codex"))
    second = workers.run(order, cw.OwnerProof(owner_id="owner", harness="codex"))
    assert first.to_mapping() == second.to_mapping()
    assert first.status == "succeeded"
    assert launches == 1
    assert first.receipts["disposal"]["absent"] is True


def test_terminal_journal_recovery_validates_without_relaunch(tmp_path: Path) -> None:
    import hashlib

    source, sha = _repository(tmp_path)
    input_path = tmp_path / "input.json"
    input_path.write_text("{}\n", encoding="utf-8")
    logical_id = "assessment-terminal-recovery"
    artifacts = tmp_path / "artifacts"
    capsules = tmp_path / "capsules"
    token = hashlib.sha256(logical_id.encode()).hexdigest()
    invocation = artifacts / "invocations" / token
    capsule = capsules / hashlib.sha256(f"{logical_id}:1".encode()).hexdigest()
    capsule_receipt = cw.create_private_clone(source, sha, capsule)
    authoritative = cw.git_receipt(source)
    capsule_guard = cw.git_receipt(capsule)
    result = {
        "verdict": "approve",
        "confidence": "high",
        "summary": "Recovered terminal result.",
        "findings": [],
        "assessed_plan_digest": "c" * 64,
    }
    process = cw.ProcessEvidence(
        pid=999999,
        returncode=0,
        stdout=json.dumps({"thread_id": "worker-1", "result": result}),
        stderr="",
        child_reaped=True,
        process_group_absent=True,
        stdout_eof=True,
        stderr_eof=True,
        elapsed_seconds=1.0,
        soft_deadline=False,
    )
    journal = cw.InvocationJournal(invocation / "journal.json", logical_id)
    journal.transition("prepared", launch_nonce="nonce")
    journal.transition("cloning", capsule=str(capsule))
    journal.transition(
        "running",
        authoritative_before=authoritative,
        capsule_before=capsule_guard,
        capsule_receipt=capsule_receipt,
    )
    journal.transition("terminal", process=process.__dict__)

    class Adapter:
        harness = "codex"

        def preflight(self, route, authority="read_only"):
            return {"executable": "fake", "version": "fake/1", "harness": "codex"}

        def session_command(self, route, prompt, schema_path, *, thread_id, new_thread_id):
            raise AssertionError("a reader order never carries a provider session")

        def command(self, route, prompt, schema_path, capsule, authority="read_only"):
            raise AssertionError("terminal recovery must not relaunch")

    order = cw.WorkOrder(
        logical_invocation_id=logical_id,
        generation=1,
        profile="plan_assessor",
        source_root=str(source),
        source_sha=sha,
        route={"harness": "codex", "model": "fake", "effort": "high"},
        route_snapshot_digest="b" * 64,
        input_bundle=str(input_path),
        input_digest=hashlib.sha256(input_path.read_bytes()).hexdigest(),
        facts={
            "ticket": {"key": "F-1"},
            "base_sha": sha,
            "route_digest": "b" * 64,
            "candidate_plan": {"digest": "c" * 64},
            "planner_receipt": {"digest": "d" * 64},
            "assessment_rubric": "Check the plan.",
        },
    )
    outcome = cw.CognitiveWorkers(
        artifact_root=artifacts,
        capsule_root=capsules,
        adapters={"codex": Adapter()},
    ).run(order, cw.OwnerProof(owner_id="owner", harness="codex"))
    assert outcome.status == "succeeded"
    assert not capsule.exists()


# ─── process lifecycle faults ────────────────────────────────────────────────


class _FakeProcess:
    def __init__(self, outcomes: list[object], *, pid: int = 71) -> None:
        self.outcomes = outcomes
        self.pid = pid
        self.returncode: int | None = None
        self.timeouts: list[float | None] = []

    def communicate(self, timeout: float | None = None):
        self.timeouts.append(timeout)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        if self.returncode is None:
            self.returncode = 0
        return outcome

    def poll(self):
        return self.returncode


def _run(process, **kwargs):
    return cw.run_provider_process(
        ["provider"],
        cwd=Path.cwd(),
        environment={},
        popen=lambda *a, **k: process,
        group_absent=lambda pid: True,
        soft_timeout=10,
        hard_timeout=40,
        **kwargs,
    )


def test_soft_deadline_is_recorded_but_does_not_cancel(tmp_path: Path) -> None:
    process = _FakeProcess(
        [
            subprocess.TimeoutExpired(["provider"], 10),
            (json.dumps({"thread_id": "T-1", "result": {"summary": "ok"}}), ""),
        ]
    )
    events: list[str] = []
    execution = _run(process, on_event=lambda event: events.append(event["type"]))
    assert execution.worker_id == "T-1"
    assert events == ["soft_deadline"]
    assert process.timeouts == [10, 30]
    assert execution.attempts[0]["deadline_events"] == ["soft_deadline"]
    assert execution.attempts[0]["terminal_acknowledged"] is True
    assert execution.process.soft_deadline is True


def test_hard_timeout_retries_once_only_after_terminal_acknowledgement() -> None:
    first = _FakeProcess(
        [subprocess.TimeoutExpired(["provider"], 10), subprocess.TimeoutExpired(["provider"], 30)]
    )
    second = _FakeProcess([(json.dumps({"thread_id": "fresh", "result": {"summary": "ok"}}), "")])
    processes = iter([first, second])

    def killpg(pid: int, sig: int) -> None:
        first.returncode = -sig
        first.outcomes.append(("", ""))

    execution = cw.run_provider_with_retry(
        lambda fresh: ["provider", "fresh" if fresh else "initial"],
        cwd=Path.cwd(),
        environment={},
        retry_limit=1,
        popen=lambda *a, **k: next(processes),
        killpg=killpg,
        group_absent=lambda pid: True,
        soft_timeout=10,
        hard_timeout=40,
    )
    assert execution.attempt == 2
    assert execution.command[-1] == "fresh"
    assert [item["outcome"] for item in execution.attempts] == ["hard_timeout", "success"]


def test_ambiguous_termination_forbids_any_replacement() -> None:
    process = _FakeProcess(
        [
            subprocess.TimeoutExpired(["provider"], 10),
            subprocess.TimeoutExpired(["provider"], 30),
            subprocess.TimeoutExpired(["provider"], 5),
            subprocess.TimeoutExpired(["provider"], 5),
        ]
    )
    launches = 0

    def popen(*args, **kwargs):
        nonlocal launches
        launches += 1
        return process

    with pytest.raises(cw.WorkerFailure, match="terminal acknowledgement") as error:
        cw.run_provider_with_retry(
            lambda fresh: ["provider"],
            cwd=Path.cwd(),
            environment={},
            retry_limit=1,
            popen=popen,
            killpg=lambda pid, sig: setattr(process, "returncode", -sig),
            group_absent=lambda pid: True,
            soft_timeout=10,
            hard_timeout=40,
            grace=0.01,
        )
    assert launches == 1
    assert error.value.code == "termination_unconfirmed"
    assert error.value.attempts[0]["terminal_acknowledged"] is False


def test_a_surviving_process_group_is_never_treated_as_terminal() -> None:
    process = _FakeProcess([("{}", "")])
    with pytest.raises(cw.WorkerFailure, match="process-group acknowledgement") as error:
        cw.run_provider_process(
            ["provider"],
            cwd=Path.cwd(),
            environment={},
            popen=lambda *a, **k: process,
            group_absent=lambda pid: False,
            soft_timeout=10,
            hard_timeout=40,
        )
    assert error.value.code == "termination_unconfirmed"


def test_second_hard_timeout_never_starts_a_third_launch() -> None:
    processes = [
        _FakeProcess(
            [
                subprocess.TimeoutExpired(["provider"], 10),
                subprocess.TimeoutExpired(["provider"], 30),
            ],
            pid=71,
        ),
        _FakeProcess(
            [
                subprocess.TimeoutExpired(["provider"], 10),
                subprocess.TimeoutExpired(["provider"], 30),
            ],
            pid=72,
        ),
    ]
    launches = 0

    def popen(*args, **kwargs):
        nonlocal launches
        process = processes[launches]
        launches += 1
        return process

    def killpg(pid: int, sig: int) -> None:
        process = next(item for item in processes if item.pid == pid)
        process.returncode = -sig
        process.outcomes.append(("", ""))

    with pytest.raises(cw.WorkerFailure, match="retry budget") as error:
        cw.run_provider_with_retry(
            lambda fresh: ["provider"],
            cwd=Path.cwd(),
            environment={},
            retry_limit=1,
            popen=popen,
            killpg=killpg,
            group_absent=lambda pid: True,
            soft_timeout=10,
            hard_timeout=40,
        )
    assert launches == 2
    assert [item["attempt"] for item in error.value.attempts] == [1, 2]


def test_cli_failure_prefers_the_structured_stdout_error() -> None:
    class _Failing(_FakeProcess):
        @override
        def communicate(self, timeout: float | None = None):
            result = super().communicate(timeout)
            self.returncode = 1
            return result

    process = _Failing(
        [
            (
                json.dumps({"type": "error", "message": "You've hit your usage limit."}),
                "Reading additional input from stdin...\n",
            )
        ]
    )
    with pytest.raises(cw.WorkerFailure, match="usage limit") as error:
        _run(process)
    assert "Reading additional input from stdin" not in str(error.value)
    assert error.value.attempts[0]["outcome"] == "cli_error"


def test_output_without_a_typed_result_is_not_approvable() -> None:
    with pytest.raises(cw.WorkerFailure, match="no typed result") as error:
        _run(_FakeProcess([("not-json\n", "")]))
    assert error.value.attempts[0]["outcome"] == "invalid_output"
    assert error.value.attempts[0]["terminal_acknowledged"] is True


def test_worker_environment_refuses_arbitrary_variables() -> None:
    assert "FLOW_WORKER_INVOCATION" in cw.worker_environment({"FLOW_WORKER_INVOCATION": "x"})
    with pytest.raises(cw.WorkerFailure, match="refuses"):
        cw.worker_environment({"AWS_SECRET_ACCESS_KEY": "x"})


# ─── read-only guards and journal recovery ───────────────────────────────────


class _ScriptedAdapter:
    """Run a real process whose body the test chooses."""

    harness = "codex"

    def __init__(self, body: str) -> None:
        self.body = body
        self.launches = 0

    def preflight(self, route, authority="read_only"):
        return {"executable": "/usr/bin/codex", "version": "codex 1", "harness": "codex"}

    def command(self, route, prompt, schema_path, capsule, authority="read_only"):
        self.launches += 1
        return [sys.executable, "-c", self.body]

    def session_command(self, route, prompt, schema_path, *, thread_id, new_thread_id):
        raise AssertionError("a reader order never carries a provider session")


# Every review order in this module is given the same one-byte-object bundle, so the
# reviewer must cite exactly this digest or its verdict is refused.
_BUNDLE_DIGEST = "ca3d163bab055381827226140568f3bef7eaac187cebd76878e0b63e9e442356"
_REVIEW_RESULT = {
    "verdict": "clean",
    "summary": "No findings.",
    "findings": [],
    "input_digest": _BUNDLE_DIGEST,
}
_EMIT = f"import json,sys; sys.stdout.write(json.dumps({{'result': {_REVIEW_RESULT!r}}}))"


def _review_order(source: Path, sha: str, input_path: Path, logical_id: str) -> cw.WorkOrder:
    import hashlib

    return cw.WorkOrder(
        logical_invocation_id=logical_id,
        generation=1,
        profile="code_reviewer",
        source_root=str(source),
        source_sha=sha,
        route={"harness": "codex", "model": "fake", "effort": "high"},
        route_snapshot_digest="b" * 64,
        input_bundle=str(input_path),
        input_digest=hashlib.sha256(input_path.read_bytes()).hexdigest(),
        facts={
            "stage_code_review": "Review the diff.",
            "ticket": {"key": "F-1"},
            "accepted_plan": {"digest": "c" * 64},
            "source_sha": sha,
            "review_bundle": {"digest": _BUNDLE_DIGEST},
        },
    )


def _workers(tmp_path: Path, adapter) -> cw.CognitiveWorkers:
    return cw.CognitiveWorkers(
        artifact_root=tmp_path / "artifacts",
        capsule_root=tmp_path / "capsules",
        adapters={"codex": adapter},
    )


def test_a_reader_that_writes_to_its_capsule_is_a_read_only_violation(tmp_path: Path) -> None:
    source, sha = _repository(tmp_path)
    input_path = tmp_path / "input.json"
    input_path.write_text("{}\n", encoding="utf-8")
    body = (
        "import json,pathlib,sys; pathlib.Path('escaped.txt').write_text('x');"
        f" sys.stdout.write(json.dumps({{'result': {_REVIEW_RESULT!r}}}))"
    )
    order = _review_order(source, sha, input_path, "review-capsule-write")

    with pytest.raises(cw.WorkerFailure, match="changed its capsule") as error:
        _workers(tmp_path, _ScriptedAdapter(body)).run(
            order, cw.OwnerProof(owner_id="owner", harness="codex")
        )
    assert error.value.code == "read_only_violation"
    journal = json.loads(
        (
            tmp_path
            / "artifacts"
            / "invocations"
            / __import__("hashlib").sha256(b"review-capsule-write").hexdigest()
            / "journal.json"
        ).read_text(encoding="utf-8")
    )
    assert journal["state"] == "quarantined"


def test_a_reader_that_writes_to_the_authoritative_tree_is_a_read_only_violation(
    tmp_path: Path,
) -> None:
    source, sha = _repository(tmp_path)
    input_path = tmp_path / "input.json"
    input_path.write_text("{}\n", encoding="utf-8")
    body = (
        "import json,pathlib,sys;"
        f" pathlib.Path({str(source / 'leaked.txt')!r}).write_text('x');"
        f" sys.stdout.write(json.dumps({{'result': {_REVIEW_RESULT!r}}}))"
    )
    order = _review_order(source, sha, input_path, "review-source-write")

    with pytest.raises(cw.WorkerFailure, match="authoritative repository") as error:
        _workers(tmp_path, _ScriptedAdapter(body)).run(
            order, cw.OwnerProof(owner_id="owner", harness="codex")
        )
    assert error.value.code == "read_only_violation"


def test_a_stale_source_sha_is_refused_before_any_capsule_exists(tmp_path: Path) -> None:
    source, sha = _repository(tmp_path)
    input_path = tmp_path / "input.json"
    input_path.write_text("{}\n", encoding="utf-8")
    order = _review_order(source, sha, input_path, "review-stale")
    (source / "tracked.txt").write_text("moved\n", encoding="utf-8")
    _git(source, "commit", "-qam", "move")

    with pytest.raises(cw.WorkerFailure, match="stale") as error:
        _workers(tmp_path, _ScriptedAdapter(_EMIT)).run(
            order, cw.OwnerProof(owner_id="owner", harness="codex")
        )
    assert error.value.code == "stale_order"
    assert not (tmp_path / "capsules").exists()


def test_a_tampered_input_bundle_is_refused_before_any_capsule_exists(tmp_path: Path) -> None:
    source, sha = _repository(tmp_path)
    input_path = tmp_path / "input.json"
    input_path.write_text("{}\n", encoding="utf-8")
    order = _review_order(source, sha, input_path, "review-tampered-input")
    input_path.write_text('{"tampered":true}\n', encoding="utf-8")

    with pytest.raises(cw.WorkerFailure, match="input digest"):
        _workers(tmp_path, _ScriptedAdapter(_EMIT)).run(
            order, cw.OwnerProof(owner_id="owner", harness="codex")
        )
    assert not (tmp_path / "capsules").exists()


def test_a_lost_lease_fence_cannot_run_a_sealed_order(tmp_path: Path) -> None:
    import hashlib

    source, sha = _repository(tmp_path)
    input_path = tmp_path / "input.json"
    input_path.write_text("{}\n", encoding="utf-8")
    del hashlib
    base = _review_order(source, sha, input_path, "review-fence")
    order = dataclasses.replace(base, run_id="run-1", lease_fence="fence-1")

    with pytest.raises(cw.WorkerFailure, match="lease fence") as error:
        _workers(tmp_path, _ScriptedAdapter(_EMIT)).run(
            order,
            cw.OwnerProof(owner_id="owner", harness="codex", run_id="run-1", lease_fence="fence-2"),
        )
    assert error.value.code == "lost_owner"


@pytest.mark.parametrize("state", ["completed", "blocked", "quarantined"])
def test_a_terminal_journal_never_relaunches(tmp_path: Path, state: str) -> None:
    import hashlib

    source, sha = _repository(tmp_path)
    input_path = tmp_path / "input.json"
    input_path.write_text("{}\n", encoding="utf-8")
    logical_id = f"review-terminal-{state}"
    invocation = (
        tmp_path / "artifacts" / "invocations" / hashlib.sha256(logical_id.encode()).hexdigest()
    )
    journal = cw.InvocationJournal(invocation / "journal.json", logical_id)
    journal.transition("prepared", launch_nonce="nonce")
    journal.transition(state)
    adapter = _ScriptedAdapter(_EMIT)

    with pytest.raises(cw.WorkerFailure, match="terminally"):
        _workers(tmp_path, adapter).run(
            _review_order(source, sha, input_path, logical_id),
            cw.OwnerProof(owner_id="owner", harness="codex"),
        )
    assert adapter.launches == 0


def test_a_running_journal_requires_supervision_rather_than_a_second_launch(
    tmp_path: Path,
) -> None:
    import hashlib

    source, sha = _repository(tmp_path)
    input_path = tmp_path / "input.json"
    input_path.write_text("{}\n", encoding="utf-8")
    logical_id = "review-running"
    invocation = (
        tmp_path / "artifacts" / "invocations" / hashlib.sha256(logical_id.encode()).hexdigest()
    )
    journal = cw.InvocationJournal(invocation / "journal.json", logical_id)
    journal.transition("prepared", launch_nonce="nonce")
    journal.transition("running")
    adapter = _ScriptedAdapter(_EMIT)

    with pytest.raises(cw.WorkerFailure, match="supervision") as error:
        _workers(tmp_path, adapter).run(
            _review_order(source, sha, input_path, logical_id),
            cw.OwnerProof(owner_id="owner", harness="codex"),
        )
    assert error.value.code == "recovery_required"
    assert adapter.launches == 0


def test_a_terminal_journal_without_process_evidence_quarantines(tmp_path: Path) -> None:
    import hashlib

    source, sha = _repository(tmp_path)
    input_path = tmp_path / "input.json"
    input_path.write_text("{}\n", encoding="utf-8")
    logical_id = "review-terminal-evidence"
    invocation = (
        tmp_path / "artifacts" / "invocations" / hashlib.sha256(logical_id.encode()).hexdigest()
    )
    journal = cw.InvocationJournal(invocation / "journal.json", logical_id)
    journal.transition("prepared", launch_nonce="nonce")
    journal.transition("terminal")
    adapter = _ScriptedAdapter(_EMIT)

    with pytest.raises(cw.WorkerFailure, match="no process evidence") as error:
        _workers(tmp_path, adapter).run(
            _review_order(source, sha, input_path, logical_id),
            cw.OwnerProof(owner_id="owner", harness="codex"),
        )
    assert error.value.code == "recovery_required"
    assert adapter.launches == 0
    assert json.loads((invocation / "journal.json").read_text())["state"] == "quarantined"


def test_a_recovered_capsule_at_the_wrong_base_quarantines(tmp_path: Path) -> None:
    import hashlib

    source, sha = _repository(tmp_path)
    input_path = tmp_path / "input.json"
    input_path.write_text("{}\n", encoding="utf-8")
    logical_id = "review-baseline"
    invocation = (
        tmp_path / "artifacts" / "invocations" / hashlib.sha256(logical_id.encode()).hexdigest()
    )
    journal = cw.InvocationJournal(invocation / "journal.json", logical_id)
    journal.transition("prepared", launch_nonce="nonce")
    order = _review_order(source, sha, input_path, logical_id)
    capsule = tmp_path / "capsules" / hashlib.sha256(f"{logical_id}:1".encode()).hexdigest()
    cw.create_private_clone(source, sha, capsule)
    _git(capsule, "config", "user.email", "flow@example.test")
    _git(capsule, "config", "user.name", "Flow Test")
    (capsule / "drift.txt").write_text("x", encoding="utf-8")
    _git(capsule, "add", "drift.txt")
    _git(capsule, "commit", "-qm", "drift")

    with pytest.raises(cw.WorkerFailure, match="wrong source SHA") as error:
        _workers(tmp_path, _ScriptedAdapter(_EMIT)).run(
            order, cw.OwnerProof(owner_id="owner", harness="codex")
        )
    assert error.value.code == "baseline_mismatch"
    assert json.loads((invocation / "journal.json").read_text())["state"] == "quarantined"


def test_two_concurrent_runs_of_one_invocation_launch_one_provider(tmp_path: Path) -> None:
    source, sha = _repository(tmp_path)
    input_path = tmp_path / "input.json"
    input_path.write_text("{}\n", encoding="utf-8")
    order = _review_order(source, sha, input_path, "review-concurrent")
    adapter = _ScriptedAdapter(f"import time; time.sleep(0.5); {_EMIT}")
    workers = _workers(tmp_path, adapter)
    start = threading.Barrier(2)
    outcomes: list[cw.WorkOutcome] = []
    failures: list[BaseException] = []

    def attempt() -> None:
        start.wait()
        try:
            outcomes.append(workers.run(order, cw.OwnerProof(owner_id="owner", harness="codex")))
        except BaseException as exc:
            failures.append(exc)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
        assert not thread.is_alive()

    assert failures == []
    assert adapter.launches == 1
    assert [outcome.status for outcome in outcomes] == ["succeeded", "succeeded"]
    assert outcomes[0].to_mapping() == outcomes[1].to_mapping()


def test_a_held_invocation_lock_does_not_block_another_invocation(tmp_path: Path) -> None:
    source, sha = _repository(tmp_path)
    input_path = tmp_path / "input.json"
    input_path.write_text("{}\n", encoding="utf-8")
    workers = _workers(tmp_path, _ScriptedAdapter(_EMIT))
    order = _review_order(source, sha, input_path, "review-other-invocation")
    outcomes: list[cw.WorkOutcome] = []

    def attempt() -> None:
        outcomes.append(workers.run(order, cw.OwnerProof(owner_id="owner", harness="codex")))

    with flock_blocking(workers._invocation_lock("review-held-invocation")):
        thread = threading.Thread(target=attempt)
        thread.start()
        thread.join(timeout=60)
        assert not thread.is_alive()

    assert [outcome.status for outcome in outcomes] == ["succeeded"]


def test_cancel_refuses_a_contended_invocation_instead_of_waiting_for_the_run(
    tmp_path: Path,
) -> None:
    workers = _workers(tmp_path, _ScriptedAdapter(_EMIT))
    logical_id = "review-cancel-contended"
    invocation = workers._invocation_dir(logical_id)
    journal = cw.InvocationJournal(invocation / "journal.json", logical_id)
    journal.transition("prepared", launch_nonce="nonce")
    journal.transition("running", pid=4242)

    with (
        flock_blocking(workers._invocation_lock(logical_id)),
        pytest.raises(cw.WorkerFailure, match="executing under another run") as error,
    ):
        workers.cancel(logical_id, cw.OwnerProof(owner_id="owner", harness="codex"), "operator")

    assert error.value.code == "execution_busy"
    assert json.loads((invocation / "journal.json").read_text())["state"] == "running"


# ─── deterministic stage execution ───────────────────────────────────────────


def _sealed_substep(
    source_sha: str,
    *,
    substep: str,
    profile: str,
    activation: str = "pending",
    conditional: bool = False,
) -> dict[str, Any]:
    return {
        "logical_invocation_id": f"run-1:code_review:{substep}:1",
        "run_id": "run-1",
        "stage": "code_review",
        "substep": substep,
        "stage_generation": 1,
        "source_sha": source_sha,
        "route_snapshot_digest": "b" * 64,
        "profile": profile,
        "desired_route": {"harness": "codex", "model": "fake", "effort": "high"},
        "activation": activation,
        "conditional": conditional,
        "owner_harness": "codex",
        "lease_fence": "fence-1",
    }


def _stage_descriptor(source_sha: str) -> dict[str, Any]:
    return {
        "stage": "code_review",
        "generation": 1,
        "cognitive_substeps": {
            "primary_review": _sealed_substep(
                source_sha, substep="primary_review", profile="code_reviewer"
            ),
            "plan_blind_review": _sealed_substep(
                source_sha, substep="plan_blind_review", profile="diff_reviewer"
            ),
            "review_fix": _sealed_substep(
                source_sha,
                substep="review_fix",
                profile="review_fixer",
                activation="shadow",
                conditional=True,
            ),
        },
    }


def _stage_inputs(source_sha: str, input_path: Path) -> dict[str, Any]:
    return {
        "primary_review": {
            "input_bundle": str(input_path),
            "facts": {
                "stage_code_review": "Review the diff.",
                "ticket": {"key": "F-1"},
                "accepted_plan": {"digest": "c" * 64},
                "source_sha": source_sha,
                "review_bundle": {"digest": _BUNDLE_DIGEST},
            },
        },
        "plan_blind_review": {
            "input_bundle": str(input_path),
            "facts": {
                "source_sha": source_sha,
                "review_bundle": {"digest": _BUNDLE_DIGEST},
                "review_rubric": "Judge the change on its own terms.",
            },
        },
    }


def _stage_workers(tmp_path: Path, adapter) -> cw.CognitiveWorkers:
    return cw.CognitiveWorkers(
        artifact_root=tmp_path / "artifacts",
        capsule_root=tmp_path / "capsules",
        adapters={"codex": adapter},
    )


def _run_stage(tmp_path: Path, descriptor, inputs, adapter, source: Path) -> dict[str, Any]:
    return cw.run_stage(
        descriptor,
        inputs,
        source_root=source,
        artifact_root=tmp_path / "artifacts",
        capsule_root=tmp_path / "capsules",
        owner_id="owner",
        owner_harness="codex",
        workers=_stage_workers(tmp_path, adapter),
    )


def test_stage_execution_launches_only_the_activated_substeps(tmp_path: Path) -> None:
    source, sha = _repository(tmp_path)
    input_path = tmp_path / "bundle.json"
    input_path.write_text("{}\n", encoding="utf-8")
    adapter = _ScriptedAdapter(_EMIT)

    body = _run_stage(
        tmp_path,
        _stage_descriptor(sha),
        _stage_inputs(sha, input_path),
        adapter,
        source,
    )

    assert adapter.launches == 2
    assert set(body["cognitive_outcomes"]) == {"primary_review", "plan_blind_review"}
    assert body["cognitive_skips"] == {}
    outcome = body["cognitive_outcomes"]["primary_review"]
    assert outcome["status"] == "succeeded"
    assert outcome["logical_invocation_id"] == "run-1:code_review:primary_review:1"
    assert outcome["stage_generation"] == 1
    assert outcome["lease_fence"] == "fence-1"
    assert outcome["receipts"]["route"]["activation"] == "active"
    assert outcome["receipts"]["disposal"]["absent"] is True
    published = json.loads(Path(body["results"]["primary_review"]).read_text(encoding="utf-8"))
    assert published == _REVIEW_RESULT


def test_a_conditional_substep_may_carry_a_deterministic_skip(tmp_path: Path) -> None:
    source, sha = _repository(tmp_path)
    input_path = tmp_path / "bundle.json"
    input_path.write_text("{}\n", encoding="utf-8")
    descriptor = _stage_descriptor(sha)
    descriptor["cognitive_substeps"]["review_fix"]["activation"] = "pending"
    inputs = {
        **_stage_inputs(sha, input_path),
        "review_fix": {"skip": {"reason": "the primary review returned no findings"}},
    }
    adapter = _ScriptedAdapter(_EMIT)

    body = _run_stage(tmp_path, descriptor, inputs, adapter, source)

    assert adapter.launches == 2
    assert body["cognitive_skips"]["review_fix"]["reason"].startswith("the primary review")
    assert "review_fix" not in body["cognitive_outcomes"]


def test_an_unconditional_substep_cannot_be_skipped(tmp_path: Path) -> None:
    source, sha = _repository(tmp_path)
    input_path = tmp_path / "bundle.json"
    input_path.write_text("{}\n", encoding="utf-8")
    inputs = {
        **_stage_inputs(sha, input_path),
        "primary_review": {"skip": {"reason": "not today"}},
    }

    with pytest.raises(cw.WorkerFailure, match="conditional route"):
        _run_stage(tmp_path, _stage_descriptor(sha), inputs, _ScriptedAdapter(_EMIT), source)


def test_an_activated_substep_without_an_immutable_input_fails_closed(tmp_path: Path) -> None:
    source, sha = _repository(tmp_path)
    input_path = tmp_path / "bundle.json"
    input_path.write_text("{}\n", encoding="utf-8")
    inputs = _stage_inputs(sha, input_path)
    del inputs["plan_blind_review"]

    with pytest.raises(cw.WorkerFailure, match="no immutable input entry"):
        _run_stage(tmp_path, _stage_descriptor(sha), inputs, _ScriptedAdapter(_EMIT), source)


def test_a_shadow_writer_substep_is_never_launched_by_stage_execution(tmp_path: Path) -> None:
    source, sha = _repository(tmp_path)
    input_path = tmp_path / "bundle.json"
    input_path.write_text("{}\n", encoding="utf-8")
    inputs = {
        **_stage_inputs(sha, input_path),
        "review_fix": {"input_bundle": str(input_path), "facts": {}},
    }
    adapter = _ScriptedAdapter(_EMIT)

    body = _run_stage(tmp_path, _stage_descriptor(sha), inputs, adapter, source)

    assert adapter.launches == 2
    assert "review_fix" not in body["cognitive_outcomes"]
    assert "review_fix" not in body["cognitive_skips"]


def test_a_reviewer_verdict_over_the_wrong_bundle_is_refused(tmp_path: Path) -> None:
    """A clean verdict is worthless if it does not cite the evidence it was handed."""
    source, sha = _repository(tmp_path)
    input_path = tmp_path / "input.json"
    input_path.write_text("{}\n", encoding="utf-8")
    lying = {**_REVIEW_RESULT, "input_digest": "f" * 64}
    body = f"import json,sys; sys.stdout.write(json.dumps({{'result': {lying!r}}}))"
    order = _review_order(source, sha, input_path, "review-wrong-bundle")

    with pytest.raises(cw.WorkerFailure, match="does not cite the exact review bundle") as error:
        _workers(tmp_path, _ScriptedAdapter(body)).run(
            order, cw.OwnerProof(owner_id="owner", harness="codex")
        )
    assert error.value.code == "invalid_result"
    assert not list((tmp_path / "capsules").glob("*"))


def test_a_cleanly_failed_invocation_does_not_strand_its_capsule(tmp_path: Path) -> None:
    source, sha = _repository(tmp_path)
    input_path = tmp_path / "input.json"
    input_path.write_text("{}\n", encoding="utf-8")
    body = "import sys; sys.stderr.write('provider down'); sys.exit(1)"
    order = _review_order(source, sha, input_path, "review-blocked")

    with pytest.raises(cw.WorkerFailure, match="exited 1"):
        _workers(tmp_path, _ScriptedAdapter(body)).run(
            order, cw.OwnerProof(owner_id="owner", harness="codex")
        )

    assert not list((tmp_path / "capsules").glob("*"))


def test_a_read_only_violation_quarantines_the_capsule_instead_of_stranding_it(
    tmp_path: Path,
) -> None:
    source, sha = _repository(tmp_path)
    input_path = tmp_path / "input.json"
    input_path.write_text("{}\n", encoding="utf-8")
    body = (
        "import json,pathlib,sys; pathlib.Path('escaped.txt').write_text('x');"
        f" sys.stdout.write(json.dumps({{'result': {_REVIEW_RESULT!r}}}))"
    )
    order = _review_order(source, sha, input_path, "review-quarantine")

    with pytest.raises(cw.WorkerFailure, match="changed its capsule"):
        _workers(tmp_path, _ScriptedAdapter(body)).run(
            order, cw.OwnerProof(owner_id="owner", harness="codex")
        )

    quarantined = list((tmp_path / "capsules" / "quarantine").glob("*"))
    assert len(quarantined) == 1
    assert (quarantined[0] / "escaped.txt").is_file()


def test_a_partial_clone_never_occupies_the_capsule_path(tmp_path: Path, monkeypatch) -> None:
    """A crash mid-clone must not leave a repository the recovery path cannot read."""
    source, sha = _repository(tmp_path)
    capsule = tmp_path / "capsules" / "target"
    real_run = subprocess.run

    def crashing(command, **kwargs):
        if command[:2] == ["git", "clone"]:
            real_run(command, **kwargs)
            raise KeyboardInterrupt("crash after the clone, before the detach")
        return real_run(command, **kwargs)

    monkeypatch.setattr(cw.subprocess, "run", crashing)
    with pytest.raises(KeyboardInterrupt):
        cw.create_private_clone(source, sha, capsule)

    monkeypatch.undo()
    assert not capsule.exists()
    receipt = cw.create_private_clone(source, sha, capsule)
    assert receipt["source_sha"] == sha
