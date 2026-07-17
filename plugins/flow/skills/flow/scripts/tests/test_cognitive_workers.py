from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import signal
import stat
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
    assert active == readers | {
        "e2e",
        "implementer",
        "review_fixer",
        "revision_fixer",
        "machinery_fixer",
    }
    assert all(cw.ROLE_CATALOG[name].authority == "read_only" for name in readers)
    assert cw.ROLE_CATALOG["e2e"].authority == "disposable_writer"
    # The implementer and the two review-loop fixers are activated importing capsule_writers.
    for name in ("implementer", "review_fixer", "revision_fixer"):
        assert cw.ROLE_CATALOG[name].authority == "capsule_writer", name
        assert cw.ROLE_CATALOG[name].active is True, name
    # machinery_fixer is an active read_only capsule; reflect applies its report via the guard.
    assert cw.ROLE_CATALOG["machinery_fixer"].authority == "read_only"
    assert cw.ROLE_CATALOG["machinery_fixer"].active is True


def test_role_catalog_carries_per_role_soft_and_hard_budgets() -> None:
    readers = {
        "planner",
        "plan_assessor",
        "code_reviewer",
        "diff_reviewer",
        "guard_reviewer",
        "review_brief_author",
        "reflector",
        "machinery_fixer",
    }
    for name in readers:
        policy = cw.ROLE_CATALOG[name]
        assert (policy.soft_budget_seconds, policy.hard_budget_seconds) == (600, 2400), name
    for name in ("implementer", "review_fixer", "revision_fixer"):
        policy = cw.ROLE_CATALOG[name]
        assert (policy.soft_budget_seconds, policy.hard_budget_seconds) == (1200, 5400), name
    assert (
        cw.ROLE_CATALOG["e2e"].soft_budget_seconds,
        cw.ROLE_CATALOG["e2e"].hard_budget_seconds,
    ) == (900, 3600)
    # The module constants stay the reader defaults external callers may still reference.
    assert cw.SOFT_TIMEOUT_SECONDS == 600
    assert cw.HARD_TIMEOUT_SECONDS == 2400


def test_cumulative_role_budget_accounts_for_permitted_retries() -> None:
    # Readers retry once (1 + 1 attempts) at 2400s each.
    assert cw.cumulative_role_budget("planner") == 2 * 2400
    assert cw.cumulative_role_budget("machinery_fixer") == 2 * 2400
    # Writers and e2e never retry (1 attempt).
    assert cw.cumulative_role_budget("implementer") == 5400
    assert cw.cumulative_role_budget("e2e") == 3600


def test_inactive_profile_is_refused_before_capsule_allocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Every catalog profile is active now, so the defensive not-active guard has no natural
    # subject; force one inactive to prove the guard still refuses before any capsule is cut.
    inactive = dataclasses.replace(cw.ROLE_CATALOG["machinery_fixer"], active=False)
    monkeypatch.setitem(cw.ROLE_CATALOG, "machinery_fixer", inactive)
    order = cw.WorkOrder(
        logical_invocation_id="inactive-1",
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
        self.inputs: list[str | None] = []

    def communicate(self, timeout: float | None = None, input: str | None = None):
        self.timeouts.append(timeout)
        self.inputs.append(input)
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
    assert events == ["launch", "soft_deadline"]
    assert process.timeouts == [10, 30]
    assert execution.attempts[0]["deadline_events"] == ["soft_deadline"]
    assert execution.attempts[0]["terminal_acknowledged"] is True
    assert execution.process.soft_deadline is True


def test_launch_event_carries_pid_pgid_and_started_at_before_any_output_is_read() -> None:
    process = _FakeProcess([(json.dumps({"thread_id": "T-1", "result": {"summary": "ok"}}), "")])
    events: list[dict[str, object]] = []
    execution = _run(process, on_event=events.append)
    assert execution.worker_id == "T-1"
    launches = [event for event in events if event["type"] == "launch"]
    assert len(launches) == 1
    launch = launches[0]
    assert launch["attempt"] == 1
    assert launch["pid"] == process.pid
    assert launch["pgid"] == process.pid
    assert isinstance(launch["started_at"], float)


def test_launch_callback_failure_that_drains_cleanly_reraises_the_callback_error() -> None:
    """A non-WorkerFailure callback error (e.g. an OSError from a journal write) is wrapped as
    a WorkerFailure once the group's death is confirmed -- a bare exception would escape every
    WorkerFailure handler upstream and leave the invocation unjournaled and its capsule
    undisposed. A WorkerFailure raised by the callback itself is reraised verbatim instead
    (see test_launch_callback_worker_failure_is_reraised_verbatim_when_drained_cleanly).
    """
    process = _FakeProcess([("", "")])
    killed: list[int] = []

    def on_event(event: dict[str, object]) -> None:
        if event["type"] == "launch":
            raise OSError("journal write failed")

    with pytest.raises(cw.WorkerFailure, match="journal write failed") as error:
        cw.run_provider_process(
            ["provider"],
            cwd=Path.cwd(),
            environment={},
            popen=lambda *a, **k: process,
            killpg=lambda pid, sig: killed.append(sig),
            group_absent=lambda pid: True,
            soft_timeout=10,
            hard_timeout=40,
            on_event=on_event,
        )
    assert error.value.code == "launch_callback_failed"
    # Terminated (not killed): the drain communicate() call below succeeded on the first try.
    assert killed == [signal.SIGTERM]
    assert process.poll() is not None


def test_launch_callback_worker_failure_is_reraised_verbatim_when_drained_cleanly() -> None:
    process = _FakeProcess([("", "")])

    def on_event(event: dict[str, object]) -> None:
        if event["type"] == "launch":
            raise cw.WorkerFailure("journal digest is invalid", code="recovery_required")

    with pytest.raises(cw.WorkerFailure, match="journal digest is invalid") as error:
        cw.run_provider_process(
            ["provider"],
            cwd=Path.cwd(),
            environment={},
            popen=lambda *a, **k: process,
            killpg=lambda pid, sig: None,
            group_absent=lambda pid: True,
            soft_timeout=10,
            hard_timeout=40,
            on_event=on_event,
        )
    assert error.value.code == "recovery_required"


def test_launch_callback_failure_with_no_terminal_proof_becomes_termination_unconfirmed() -> None:
    process = _FakeProcess(
        [subprocess.TimeoutExpired(["provider"], 5), subprocess.TimeoutExpired(["provider"], 5)]
    )

    def on_event(event: dict[str, object]) -> None:
        if event["type"] == "launch":
            raise ValueError("journal write failed")

    with pytest.raises(cw.WorkerFailure, match="launch-journaling failure") as error:
        cw.run_provider_process(
            ["provider"],
            cwd=Path.cwd(),
            environment={},
            popen=lambda *a, **k: process,
            killpg=lambda pid, sig: None,
            group_absent=lambda pid: True,
            soft_timeout=10,
            hard_timeout=40,
            on_event=on_event,
        )
    assert error.value.code == "termination_unconfirmed"
    assert process.poll() is None


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
        lambda fresh: (
            ["provider", "fresh" if fresh else "initial"],
            "fresh prompt" if fresh else "initial prompt",
        ),
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
    assert first.inputs == ["initial prompt", None, None]
    assert second.inputs == ["fresh prompt"]


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
            lambda fresh: (["provider"], None),
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
            lambda fresh: (["provider"], None),
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


# ─── stdin transport (ARG_MAX) ────────────────────────────────────────────────


def test_run_provider_process_pipes_a_huge_prompt_via_stdin_only_on_the_first_call() -> None:
    huge_prompt = "P" * (3 * 1024 * 1024)
    process = _FakeProcess([(json.dumps({"thread_id": "T-1", "result": {"summary": "ok"}}), "")])
    captured_kwargs: dict[str, Any] = {}

    def popen(command: list[str], **kwargs: Any) -> _FakeProcess:
        captured_kwargs.update(kwargs)
        return process

    execution = cw.run_provider_process(
        ["provider", "-"],
        cwd=Path.cwd(),
        environment={},
        stdin_payload=huge_prompt,
        popen=popen,
        group_absent=lambda pid: True,
        soft_timeout=10,
        hard_timeout=40,
    )
    assert captured_kwargs["stdin"] == subprocess.PIPE
    assert process.inputs == [huge_prompt]
    assert execution.worker_id == "T-1"


def test_run_provider_process_never_resends_input_after_a_soft_timeout() -> None:
    process = _FakeProcess(
        [
            subprocess.TimeoutExpired(["provider"], 10),
            (json.dumps({"thread_id": "T-1", "result": {"summary": "ok"}}), ""),
        ]
    )
    execution = _run(process, stdin_payload="prompt bytes")
    assert process.inputs == ["prompt bytes", None]
    assert execution.worker_id == "T-1"


def test_run_provider_process_omits_input_through_every_hard_deadline_continuation() -> None:
    process = _FakeProcess(
        [
            subprocess.TimeoutExpired(["provider"], 10),
            subprocess.TimeoutExpired(["provider"], 30),
            ("tail stdout", "tail stderr"),
        ]
    )
    with pytest.raises(cw.WorkerFailure, match="hard deadline"):
        _run(process, stdin_payload="prompt bytes", killpg=lambda pid, sig: None)
    assert process.inputs == ["prompt bytes", None, None]


def test_run_provider_process_legacy_call_never_pipes_stdin() -> None:
    """Callers that omit ``stdin_payload`` keep today's no-stdin Popen and communicate shape."""
    process = _FakeProcess([(json.dumps({"thread_id": "T-1", "result": {"summary": "ok"}}), "")])
    captured_kwargs: dict[str, Any] = {}

    def popen(command: list[str], **kwargs: Any) -> _FakeProcess:
        captured_kwargs.update(kwargs)
        return process

    execution = cw.run_provider_process(
        ["provider"],
        cwd=Path.cwd(),
        environment={},
        popen=popen,
        group_absent=lambda pid: True,
        soft_timeout=10,
        hard_timeout=40,
    )
    assert "stdin" not in captured_kwargs
    assert process.inputs == [None]
    assert execution.worker_id == "T-1"


def test_full_run_fresh_retry_pairs_the_fresh_prompt_and_keeps_command_digest_on_the_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The retry factory pairs each attempt's own prompt with its own argv end to end.

    A forced fresh retry must reach the second process's stdin with
    ``order.fresh_provider_prompt``, never the initial prompt, while the journal's
    ``command_digest`` stays pinned to the first attempt's argv even though the
    second attempt's argv differs (a distinct session id per attempt).
    """
    source, sha = _repository(tmp_path)
    input_path = tmp_path / "input.json"
    input_path.write_text("{}\n", encoding="utf-8")

    envelope = {
        "attempt_id": "attempt-1",
        "version": 1,
        "parent_digest": None,
        "base_sha": sha,
        "route_digest": "b" * 64,
        "author": {"id": "codex:fake", "harness": "codex", "model": "fake"},
        "status": "PLAN_READY",
        "plan": {
            "motivation": "m",
            "goal": "g",
            "scenarios": [{"before": "b", "after": "a"}],
            "architecture": ["x"],
            "decisions": ["d"],
            "acceptance_outcomes": ["o"],
            "steps": ["s"],
            "files": ["f.py"],
            "context_paths": [],
            "verification": ["v"],
            "e2e_recipe": "run",
            "lane": "full",
            "compatibility": [],
            "rollout": "r",
            "risks": ["r"],
        },
        "questions": [],
        "incorporated_feedback_ids": [],
    }

    processes: list[_FakeProcess] = []
    launched_commands: list[list[str]] = []
    real_popen = cw.subprocess.Popen

    def fake_popen(command: list[str], **kwargs: Any) -> Any:
        # Only the fake provider launch is faked; every git subprocess this run makes
        # (clone, receipts, HEAD checks) must reach the real Popen unchanged.
        if not command or command[0] != "fake-codex":
            return real_popen(command, **kwargs)
        launched_commands.append(list(command))
        if not processes:
            process = _FakeProcess(
                [
                    subprocess.TimeoutExpired(command, 1),
                    subprocess.TimeoutExpired(command, 1),
                    ("", ""),
                ],
                pid=900001,
            )
        else:
            process = _FakeProcess(
                [(json.dumps({"thread_id": "fresh-thread", "result": envelope}), "")],
                pid=900002,
            )
        processes.append(process)
        return process

    monkeypatch.setattr(cw.subprocess, "Popen", fake_popen)

    class Adapter:
        harness = "codex"

        def preflight(self, route: Any, authority: str = "read_only") -> dict[str, str]:
            return {"executable": "fake", "version": "fake/1", "harness": "codex"}

        def command(self, *args: Any, **kwargs: Any) -> list[str]:
            raise AssertionError("a planner order never uses the plain capsule command")

        def session_command(
            self, route: Any, prompt: str, schema_path: Path, *, thread_id: Any, new_thread_id: Any
        ) -> list[str]:
            return ["fake-codex", "exec", "--session-id", str(new_thread_id), "-"]

    order = cw.WorkOrder(
        logical_invocation_id="planner-fresh-retry",
        generation=1,
        profile="planner",
        source_root=str(source),
        source_sha=sha,
        route={"harness": "codex", "model": "fake", "effort": "high"},
        route_snapshot_digest="b" * 64,
        input_bundle=str(input_path),
        input_digest=hashlib.sha256(input_path.read_bytes()).hexdigest(),
        facts={},
        provider_prompt="INITIAL PLANNER PROMPT",
        fresh_provider_prompt="FRESH REHYDRATION PROMPT",
        session={
            "thread_id": None,
            "initial_session_id": "initial-session",
            "fresh_session_id": "fresh-session",
        },
    )
    workers = cw.CognitiveWorkers(
        artifact_root=tmp_path / "artifacts",
        capsule_root=tmp_path / "capsules",
        adapters={"codex": Adapter()},
    )

    outcome = workers.run(order, cw.OwnerProof(owner_id="owner", harness="codex"))

    assert outcome.status == "succeeded"
    first_command = ["fake-codex", "exec", "--session-id", "initial-session", "-"]
    fresh_command = ["fake-codex", "exec", "--session-id", "fresh-session", "-"]
    assert launched_commands == [first_command, fresh_command]
    assert processes[0].inputs == ["INITIAL PLANNER PROMPT", None, None]
    assert processes[1].inputs == ["FRESH REHYDRATION PROMPT"]
    assert outcome.receipts["command"] == fresh_command

    invocation = (
        tmp_path / "artifacts" / "invocations" / hashlib.sha256(b"planner-fresh-retry").hexdigest()
    )
    journal_value = json.loads((invocation / "journal.json").read_text(encoding="utf-8"))
    assert journal_value["command_digest"] == cw._digest(first_command)
    assert journal_value["command_digest"] != cw._digest(fresh_command)
    assert journal_value["command"] == fresh_command

    # The journaled and returned command evidence is exactly the executed argv: no prompt
    # bytes ride either, since both attempts' prompts went out over stdin instead.
    command_evidence = json.dumps([journal_value["command"], outcome.receipts["command"]])
    assert "INITIAL PLANNER PROMPT" not in command_evidence
    assert "FRESH REHYDRATION PROMPT" not in command_evidence


# ─── failure-tail retention ──────────────────────────────────────────────────


def test_hard_timeout_retains_the_final_grace_communicate_output() -> None:
    process = _FakeProcess(
        [
            subprocess.TimeoutExpired(["provider"], 10),
            subprocess.TimeoutExpired(["provider"], 30),
            ("tail stdout", "tail stderr"),
        ]
    )
    retained: list[tuple[int, bytes, bytes]] = []
    with pytest.raises(cw.WorkerFailure, match="hard deadline"):
        _run(
            process,
            killpg=lambda pid, sig: None,
            retain_failure=lambda attempt, out, err: retained.append((attempt, out, err)),
        )
    assert retained == [(1, b"tail stdout", b"tail stderr")]


def test_hard_timeout_retention_falls_back_to_timeoutexpired_bytes_when_streams_never_close() -> (
    None
):
    hard_exc = subprocess.TimeoutExpired(
        ["provider"], 30, output=b"raw stdout bytes", stderr=b"raw stderr bytes"
    )
    process = _FakeProcess(
        [
            subprocess.TimeoutExpired(["provider"], 10),
            hard_exc,
            subprocess.TimeoutExpired(["provider"], 5),
            subprocess.TimeoutExpired(["provider"], 5),
        ]
    )
    retained: list[tuple[int, bytes, bytes]] = []
    with pytest.raises(cw.WorkerFailure):
        _run(
            process,
            killpg=lambda pid, sig: None,
            grace=0.01,
            retain_failure=lambda attempt, out, err: retained.append((attempt, out, err)),
        )
    # The final post-kill communicate() returned nothing, so retention falls back to the
    # hard-deadline TimeoutExpired's own captured bytes, undecoded.
    assert retained == [(1, b"raw stdout bytes", b"raw stderr bytes")]


def test_worker_exited_retains_the_captured_streams() -> None:
    class _Failing(_FakeProcess):
        @override
        def communicate(self, timeout: float | None = None, input: str | None = None):
            result = super().communicate(timeout, input)
            self.returncode = 7
            return result

    process = _Failing([("boom stdout", "boom stderr")])
    retained: list[tuple[int, bytes, bytes]] = []
    with pytest.raises(cw.WorkerFailure, match="exited 7"):
        _run(process, retain_failure=lambda attempt, out, err: retained.append((attempt, out, err)))
    assert retained == [(1, b"boom stdout", b"boom stderr")]


def test_invalid_output_retains_the_captured_streams() -> None:
    retained: list[tuple[int, bytes, bytes]] = []
    with pytest.raises(cw.WorkerFailure, match="no typed result"):
        _run(
            _FakeProcess([("not-json\n", "some stderr")]),
            retain_failure=lambda attempt, out, err: retained.append((attempt, out, err)),
        )
    assert retained == [(1, b"not-json\n", b"some stderr")]


def test_success_never_invokes_the_retention_callback() -> None:
    process = _FakeProcess([(json.dumps({"thread_id": "T-1", "result": {"summary": "ok"}}), "")])
    retained: list[tuple[int, bytes, bytes]] = []
    _run(process, retain_failure=lambda attempt, out, err: retained.append((attempt, out, err)))
    assert retained == []


def test_retention_failure_never_blocks_the_acknowledged_fresh_retry() -> None:
    first = _FakeProcess(
        [subprocess.TimeoutExpired(["provider"], 10), subprocess.TimeoutExpired(["provider"], 30)]
    )
    second = _FakeProcess([(json.dumps({"thread_id": "fresh", "result": {"summary": "ok"}}), "")])
    processes = iter([first, second])

    def killpg(pid: int, sig: int) -> None:
        first.returncode = -sig
        first.outcomes.append(("", ""))

    def failing_retain(attempt: int, out: bytes, err: bytes) -> None:
        raise RuntimeError("disk full")

    execution = cw.run_provider_with_retry(
        lambda fresh: (["provider", "fresh" if fresh else "initial"], None),
        cwd=Path.cwd(),
        environment={},
        retry_limit=1,
        popen=lambda *a, **k: next(processes),
        killpg=killpg,
        group_absent=lambda pid: True,
        soft_timeout=10,
        hard_timeout=40,
        retain_failure=failing_retain,
    )
    assert execution.attempt == 2
    assert [item["outcome"] for item in execution.attempts] == ["hard_timeout", "success"]


def test_persist_failure_tail_truncates_to_the_last_50kib_atomically(tmp_path: Path) -> None:
    invocation = tmp_path / "invocation"
    invocation.mkdir()
    data = b"x" * (60 * 1024) + b"tail-marker"
    cw._persist_failure_tail(invocation, 2, "stdout", data)
    path = invocation / "attempt-2-stdout.tail"
    written = path.read_bytes()
    assert written == data[-cw._FAILURE_TAIL_MAX_BYTES :]
    assert len(written) == cw._FAILURE_TAIL_MAX_BYTES
    assert written.endswith(b"tail-marker")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_invocation_failure_tail_writes_both_streams(tmp_path: Path) -> None:
    invocation = tmp_path / "invocation"
    invocation.mkdir()
    callback = cw._invocation_failure_tail(invocation)
    callback(3, b"out-data", b"err-data")
    assert (invocation / "attempt-3-stdout.tail").read_bytes() == b"out-data"
    assert (invocation / "attempt-3-stderr.tail").read_bytes() == b"err-data"


def test_cli_failure_prefers_the_structured_stdout_error() -> None:
    class _Failing(_FakeProcess):
        @override
        def communicate(self, timeout: float | None = None, input: str | None = None):
            result = super().communicate(timeout, input)
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


def test_running_journal_records_launch_evidence_for_the_current_attempt(
    tmp_path: Path,
) -> None:
    source, sha = _repository(tmp_path)
    input_path = tmp_path / "input.json"
    input_path.write_text("{}\n", encoding="utf-8")
    order = _review_order(source, sha, input_path, "review-launch-evidence")

    outcome = _workers(tmp_path, _ScriptedAdapter(_EMIT)).run(
        order, cw.OwnerProof(owner_id="owner", harness="codex")
    )
    assert outcome.status == "succeeded"

    journal = _violation_journal(tmp_path, "review-launch-evidence")
    # Boot evidence is written at the "running" transition, pre-spawn, so it is present even
    # if the process crashes before the launch callback fires; the launch record's own
    # hostname/boot_id (below) are the per-attempt copy.
    assert isinstance(journal["hostname"], str)
    assert journal["hostname"]
    assert isinstance(journal["boot_id"], str)
    launch = journal["launch"]
    assert launch["attempt"] == 1
    assert isinstance(launch["pid"], int)
    assert launch["pgid"] == launch["pid"]
    assert isinstance(launch["hostname"], str)
    assert launch["hostname"]
    assert isinstance(launch["boot_id"], str)
    assert isinstance(launch["started_at"], float)
    assert journal["launches"] == [launch]


def test_bare_oserror_from_the_launch_callback_kills_the_group_and_disposes_the_capsule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An OSError out of the real on_launch hook (e.g. a journal write hitting a full disk)
    must reach `run()`'s WorkerFailure handler -- not escape it -- so the invocation lands on
    "blocked" (the group is provably dead: no live evidence to preserve) and its capsule is
    disposed rather than left an orphan.
    """
    source, sha = _repository(tmp_path)
    input_path = tmp_path / "input.json"
    input_path.write_text("{}\n", encoding="utf-8")
    order = _review_order(source, sha, input_path, "review-launch-oserror")

    original_transition = cw.InvocationJournal.transition

    def flaky_transition(self: cw.InvocationJournal, state: str, **fields: Any) -> dict[str, Any]:
        if "launch" in fields:
            raise OSError("disk full")
        return original_transition(self, state, **fields)

    monkeypatch.setattr(cw.InvocationJournal, "transition", flaky_transition)

    with pytest.raises(cw.WorkerFailure, match="disk full") as error:
        _workers(tmp_path, _ScriptedAdapter(_EMIT)).run(
            order, cw.OwnerProof(owner_id="owner", harness="codex")
        )

    assert error.value.code == "launch_callback_failed"
    journal = _violation_journal(tmp_path, "review-launch-oserror")
    assert journal["state"] == "blocked"
    assert journal["failure"]["code"] == "launch_callback_failed"
    capsule = Path(journal["disposal"]["capsule"])
    assert not capsule.exists()


def test_full_run_capsule_branch_pairs_the_bundle_prompt_with_its_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The capsule branch of build_invocation (a non-session order) must reach stdin too.

    Every implementer, review_fixer, revision_fixer, E2E, and read-only capsule worker
    takes this branch, unlike the session branch already covered by
    ``test_full_run_fresh_retry_pairs_the_fresh_prompt_and_keeps_command_digest_on_the_first``.
    """
    source, sha = _repository(tmp_path)
    input_path = tmp_path / "input.json"
    input_path.write_text("{}\n", encoding="utf-8")

    captured_prompts: list[str] = []
    process = _FakeProcess([(json.dumps({"thread_id": "worker-1", "result": _REVIEW_RESULT}), "")])
    real_popen = cw.subprocess.Popen

    def fake_popen(command: list[str], **kwargs: Any) -> Any:
        # subprocess.run() is implemented on top of Popen, so every git subprocess this
        # run makes (clone, receipts, HEAD checks) must reach the real Popen unchanged.
        if not command or command[0] != "fake-reviewer":
            return real_popen(command, **kwargs)
        return process

    monkeypatch.setattr(cw.subprocess, "Popen", fake_popen)

    class Adapter:
        harness = "codex"

        def preflight(self, route, authority="read_only"):
            return {"executable": "/usr/bin/codex", "version": "codex 1", "harness": "codex"}

        def command(self, route, prompt, schema_path, capsule, authority="read_only"):
            captured_prompts.append(prompt)
            return ["fake-reviewer", "-"]

        def session_command(self, route, prompt, schema_path, *, thread_id, new_thread_id):
            raise AssertionError("a capsule order never carries a provider session")

    order = _review_order(source, sha, input_path, "review-capsule-branch")
    workers = _workers(tmp_path, Adapter())

    outcome = workers.run(order, cw.OwnerProof(owner_id="owner", harness="codex"))

    assert outcome.status == "succeeded"
    assert captured_prompts
    assert process.inputs == [captured_prompts[0]]


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


def _violation_journal(tmp_path: Path, logical_id: str) -> dict[str, Any]:
    token = hashlib.sha256(logical_id.encode()).hexdigest()
    path = tmp_path / "artifacts" / "invocations" / token / "journal.json"
    value: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return value


def test_authoritative_violation_diagnostics_name_the_injected_untracked_path(
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
    order = _review_order(source, sha, input_path, "review-source-diagnostics")

    with pytest.raises(cw.WorkerFailure, match="authoritative repository") as error:
        _workers(tmp_path, _ScriptedAdapter(body)).run(
            order, cw.OwnerProof(owner_id="owner", harness="codex")
        )
    assert "leaked.txt" in str(error.value)
    assert "added untracked paths" in str(error.value)

    failure = _violation_journal(tmp_path, "review-source-diagnostics")["failure"]
    assert failure["target"] == "authoritative_repository"
    assert failure["untracked_content"]["added"] == ["leaked.txt"]
    assert failure["untracked_content"]["removed"] == []
    assert "leaked.txt" in failure["live_status_dirty_paths"]
    assert "leaked.txt" in failure["detail"]


def test_capped_dirty_paths_elides_past_the_display_limit() -> None:
    paths = [f"file-{i}.txt" for i in range(cw._DIRTY_PATHS_DISPLAY_LIMIT + 10)]

    capped = cw._capped_dirty_paths(paths)

    assert len(capped) == cw._DIRTY_PATHS_DISPLAY_LIMIT + 1
    assert capped[:-1] == paths[: cw._DIRTY_PATHS_DISPLAY_LIMIT]
    assert capped[-1] == "... (+10 more, 60 total)"


def test_capped_dirty_paths_is_a_no_op_at_or_under_the_limit() -> None:
    paths = [f"file-{i}.txt" for i in range(cw._DIRTY_PATHS_DISPLAY_LIMIT)]

    assert cw._capped_dirty_paths(paths) == paths


def test_authoritative_violation_diagnostics_cap_a_shim_sized_dirty_tree(
    tmp_path: Path,
) -> None:
    """A shim (mise/uv) can materialize tens of thousands of untracked paths in one violation.

    Without a cap, `_capped_dirty_paths` would write every one of them into both the raised
    WorkerFailure message and the digest-verified journal.json failure record, which is
    re-read and re-digested on every later transition.
    """
    source, sha = _repository(tmp_path)
    input_path = tmp_path / "input.json"
    input_path.write_text("{}\n", encoding="utf-8")
    extra = cw._DIRTY_PATHS_DISPLAY_LIMIT + 25
    body = (
        "import json,pathlib,sys;"
        f" [pathlib.Path({str(source)!r}, f'leaked-{{i}}.txt').write_text('x')"
        f" for i in range({extra})];"
        f" sys.stdout.write(json.dumps({{'result': {_REVIEW_RESULT!r}}}))"
    )
    order = _review_order(source, sha, input_path, "review-shim-dirty-tree")

    with pytest.raises(cw.WorkerFailure, match="authoritative repository") as error:
        _workers(tmp_path, _ScriptedAdapter(body)).run(
            order, cw.OwnerProof(owner_id="owner", harness="codex")
        )

    message = str(error.value)
    assert f"more, {extra} total)" in message

    failure = _violation_journal(tmp_path, "review-shim-dirty-tree")["failure"]
    dirty_paths = failure["live_status_dirty_paths"]
    assert len(dirty_paths) == cw._DIRTY_PATHS_DISPLAY_LIMIT + 1
    assert dirty_paths[-1] == f"... (+{extra - cw._DIRTY_PATHS_DISPLAY_LIMIT} more, {extra} total)"


def test_capsule_violation_diagnostics_name_the_injected_untracked_path(tmp_path: Path) -> None:
    source, sha = _repository(tmp_path)
    input_path = tmp_path / "input.json"
    input_path.write_text("{}\n", encoding="utf-8")
    body = (
        "import json,pathlib,sys; pathlib.Path('escaped.txt').write_text('x');"
        f" sys.stdout.write(json.dumps({{'result': {_REVIEW_RESULT!r}}}))"
    )
    order = _review_order(source, sha, input_path, "review-capsule-diagnostics")

    with pytest.raises(cw.WorkerFailure, match="changed its capsule") as error:
        _workers(tmp_path, _ScriptedAdapter(body)).run(
            order, cw.OwnerProof(owner_id="owner", harness="codex")
        )
    assert "escaped.txt" in str(error.value)

    failure = _violation_journal(tmp_path, "review-capsule-diagnostics")["failure"]
    assert failure["target"] == "capsule"
    assert failure["untracked_content"]["added"] == ["escaped.txt"]


def test_authoritative_violation_with_clean_status_uses_the_documented_fallback(
    tmp_path: Path,
) -> None:
    """A HEAD move with no working-tree change is invisible to `git status`.

    `git commit --allow-empty` drifts the stored receipt's "head" field without touching the
    working tree or index, so the live status probe is clean and the diagnostic falls back to
    the documented "no visible dirt" wording in both the raised message and the journal.
    """
    source, sha = _repository(tmp_path)
    input_path = tmp_path / "input.json"
    input_path.write_text("{}\n", encoding="utf-8")
    body = (
        "import json,subprocess,sys;"
        f" subprocess.run(['git', 'commit', '--allow-empty', '-qm', 'x'],"
        f" cwd={str(source)!r}, check=True);"
        f" sys.stdout.write(json.dumps({{'result': {_REVIEW_RESULT!r}}}))"
    )
    order = _review_order(source, sha, input_path, "review-source-clean-status")

    with pytest.raises(cw.WorkerFailure, match="authoritative repository") as error:
        _workers(tmp_path, _ScriptedAdapter(body)).run(
            order, cw.OwnerProof(owner_id="owner", harness="codex")
        )
    assert "no visible dirt; index-flag/ref drift" in str(error.value)

    failure = _violation_journal(tmp_path, "review-source-clean-status")["failure"]
    assert failure["detail"] == (
        "changed receipt fields: head; no visible dirt; index-flag/ref drift"
    )
    assert failure["changed_fields"] == ["head"]
    assert failure["live_status_dirty_paths"] == []


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


def _running_journal(
    workers: cw.CognitiveWorkers,
    logical_id: str,
    *,
    launch: dict[str, object] | None,
    capsule: Path | None = None,
) -> tuple[Path, cw.InvocationJournal]:
    """Seed a "running" journal. `capsule` defaults to a path under capsule_root, matching
    every real invocation (CognitiveWorkers.run always mints capsules there); pass an
    out-of-root path to exercise the cancel containment guard."""
    invocation = workers._invocation_dir(logical_id)
    journal = cw.InvocationJournal(invocation / "journal.json", logical_id)
    journal.transition("prepared", launch_nonce="nonce")
    journal.transition(
        "running",
        capsule=str(capsule if capsule is not None else workers.capsule_root / logical_id),
        **({"launch": launch} if launch is not None else {}),
    )
    return invocation, journal


def _launch_evidence(**overrides: object) -> dict[str, object]:
    evidence: dict[str, object] = {
        "attempt": 1,
        "pid": 4242,
        "pgid": 4242,
        "hostname": "recovery-host",
        "boot_id": "boot-a",
        "started_at": 1000.0,
    }
    evidence.update(overrides)
    return evidence


def test_recover_invocation_quarantines_a_dead_same_boot_pgid_with_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workers = _workers(tmp_path, _ScriptedAdapter(_EMIT))
    invocation, _journal = _running_journal(workers, "review-dead-pgid", launch=_launch_evidence())
    capsule = Path(json.loads((invocation / "journal.json").read_text())["capsule"])
    capsule.mkdir(parents=True)
    (capsule / "evidence.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(cw.lease, "hostname", lambda: "recovery-host")
    monkeypatch.setattr(cw.lease, "boot_id", lambda: "boot-a")
    monkeypatch.setattr(cw, "_process_group_absent", lambda pgid: True)

    receipt = workers.cancel(
        "review-dead-pgid", cw.OwnerProof(owner_id="owner", harness="codex"), "operator"
    )

    assert receipt["state"] == "quarantined"
    assert receipt["idempotent"] is False
    on_disk = json.loads((invocation / "journal.json").read_text())
    assert on_disk["state"] == "quarantined"
    assert on_disk["failure"]["code"] == "termination_unconfirmed"
    assert on_disk["failure"]["owner"] == {"owner_id": "owner", "harness": "codex"}
    assert on_disk["disposal"]["quarantined"] is True
    quarantined = list((tmp_path / "capsules" / "quarantine").glob("*"))
    assert len(quarantined) == 1
    assert (quarantined[0] / "evidence.txt").is_file()


def test_recover_invocation_quarantines_the_journal_but_never_moves_a_capsule_outside_the_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recorded capsule path outside the configured capsule_root is never disposed.

    The "capsule" field is executor-written provenance from the original run; an operator
    who recovers with a different --capsule-root than that run used must not have this path
    relocated into an unrelated quarantine tree. The journal still quarantines -- only the
    disposal is skipped.
    """
    workers = _workers(tmp_path, _ScriptedAdapter(_EMIT))
    outside_root = tmp_path / "elsewhere" / "capsule"
    outside_root.mkdir(parents=True)
    (outside_root / "evidence.txt").write_text("x", encoding="utf-8")
    invocation, _journal = _running_journal(
        workers, "review-outside-root", launch=_launch_evidence(), capsule=outside_root
    )
    monkeypatch.setattr(cw.lease, "hostname", lambda: "recovery-host")
    monkeypatch.setattr(cw.lease, "boot_id", lambda: "boot-a")
    monkeypatch.setattr(cw, "_process_group_absent", lambda pgid: True)

    receipt = workers.cancel(
        "review-outside-root", cw.OwnerProof(owner_id="owner", harness="codex"), "operator"
    )

    assert receipt["state"] == "quarantined"
    on_disk = json.loads((invocation / "journal.json").read_text())
    assert on_disk["state"] == "quarantined"
    assert "disposal" not in on_disk
    assert outside_root.is_dir()
    assert (outside_root / "evidence.txt").is_file()
    assert not (tmp_path / "capsules" / "quarantine").exists()


def test_recover_invocation_refuses_a_live_same_boot_pgid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workers = _workers(tmp_path, _ScriptedAdapter(_EMIT))
    invocation, _journal = _running_journal(workers, "review-live-pgid", launch=_launch_evidence())
    monkeypatch.setattr(cw.lease, "hostname", lambda: "recovery-host")
    monkeypatch.setattr(cw.lease, "boot_id", lambda: "boot-a")
    monkeypatch.setattr(cw, "_process_group_absent", lambda pgid: False)

    with pytest.raises(cw.WorkerFailure, match="still live") as error:
        workers.cancel(
            "review-live-pgid", cw.OwnerProof(owner_id="owner", harness="codex"), "operator"
        )

    assert error.value.code == "execution_busy"
    assert json.loads((invocation / "journal.json").read_text())["state"] == "running"


def test_recover_invocation_prior_boot_mismatch_quarantines_a_reused_live_pgid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A same-host boot mismatch proves the recorded process tree is dead.

    The current numeric pgid can be live and owned by an unrelated process (pgid reuse across
    boots); that must not block recovery, so the absence probe is never even consulted.
    """
    workers = _workers(tmp_path, _ScriptedAdapter(_EMIT))
    invocation, _journal = _running_journal(
        workers, "review-prior-boot", launch=_launch_evidence(boot_id="old-boot")
    )
    probed: list[int] = []
    monkeypatch.setattr(cw.lease, "hostname", lambda: "recovery-host")
    monkeypatch.setattr(cw.lease, "boot_id", lambda: "new-boot")
    monkeypatch.setattr(cw, "_process_group_absent", lambda pgid: (probed.append(pgid), False)[1])

    receipt = workers.cancel(
        "review-prior-boot", cw.OwnerProof(owner_id="owner", harness="codex"), "operator"
    )

    assert receipt["state"] == "quarantined"
    assert probed == []
    assert json.loads((invocation / "journal.json").read_text())["state"] == "quarantined"


def test_recover_invocation_fails_closed_on_a_cross_host_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workers = _workers(tmp_path, _ScriptedAdapter(_EMIT))
    invocation, _journal = _running_journal(
        workers, "review-cross-host", launch=_launch_evidence(hostname="other-host")
    )
    monkeypatch.setattr(cw.lease, "hostname", lambda: "recovery-host")
    monkeypatch.setattr(cw.lease, "boot_id", lambda: "boot-a")

    with pytest.raises(cw.WorkerFailure, match="different host") as error:
        workers.cancel(
            "review-cross-host", cw.OwnerProof(owner_id="owner", harness="codex"), "operator"
        )

    assert error.value.code == "recovery_required"
    assert json.loads((invocation / "journal.json").read_text())["state"] == "running"


def test_recover_invocation_fails_closed_on_incomplete_launch_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A launch record present but missing a required field still fails closed.

    Unlike a wholly absent "launch" (see the launchless-journal test below), a present-but-
    incomplete dict has been through the evidence gate and failed its completeness check --
    that must still refuse recovery outright rather than take the unconditional-quarantine path.
    """
    workers = _workers(tmp_path, _ScriptedAdapter(_EMIT))
    incomplete = _launch_evidence(hostname=None)
    invocation, _journal = _running_journal(
        workers, "review-incomplete-evidence", launch=incomplete
    )
    monkeypatch.setattr(cw.lease, "hostname", lambda: "recovery-host")
    monkeypatch.setattr(cw.lease, "boot_id", lambda: "boot-a")

    with pytest.raises(cw.WorkerFailure, match="launch evidence") as error:
        workers.cancel(
            "review-incomplete-evidence",
            cw.OwnerProof(owner_id="owner", harness="codex"),
            "operator",
        )

    assert error.value.code == "recovery_required"
    assert json.loads((invocation / "journal.json").read_text())["state"] == "running"


def test_recover_invocation_quarantines_a_launchless_running_journal_unconditionally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A running/cancelling journal with no launch record at all can never earn the evidence
    _require_dead_launch demands -- every journal written before launch evidence existed, or a
    supervisor that died before the launch callback fired, is launchless forever. It takes the
    old unconditional-quarantine path instead of wedging shut with no escape hatch.
    """
    workers = _workers(tmp_path, _ScriptedAdapter(_EMIT))
    invocation, _journal = _running_journal(workers, "review-no-launch", launch=None)
    monkeypatch.setattr(cw.lease, "hostname", lambda: "recovery-host")
    monkeypatch.setattr(cw.lease, "boot_id", lambda: "boot-a")

    receipt = workers.cancel(
        "review-no-launch", cw.OwnerProof(owner_id="owner", harness="codex"), "operator"
    )

    assert receipt["state"] == "quarantined"
    assert receipt["idempotent"] is False
    on_disk = json.loads((invocation / "journal.json").read_text())
    assert on_disk["state"] == "quarantined"
    assert on_disk["failure"]["code"] == "termination_unconfirmed"


def test_recover_invocation_prepared_and_cloning_quarantine_without_launch_evidence(
    tmp_path: Path,
) -> None:
    workers = _workers(tmp_path, _ScriptedAdapter(_EMIT))
    invocation = workers._invocation_dir("review-prepared-only")
    journal = cw.InvocationJournal(invocation / "journal.json", "review-prepared-only")
    journal.transition("prepared", launch_nonce="nonce")

    receipt = workers.cancel(
        "review-prepared-only", cw.OwnerProof(owner_id="owner", harness="codex"), "operator"
    )

    assert receipt["state"] == "quarantined"
    assert json.loads((invocation / "journal.json").read_text())["state"] == "quarantined"


def test_recover_invocation_cloning_quarantines_and_disposes_the_capsule_without_launch_evidence(
    tmp_path: Path,
) -> None:
    """ "cloning" is the only never-launched state carrying a "capsule" field (`run()` writes it
    via `journal.transition("cloning", capsule=str(capsule))` before any process spawns), so it
    is the one state that actually drives `_cancel_locked`'s dispose-without-launch-evidence
    branch. The "prepared" test above proves the journal-only transition; without this test a
    regression there (e.g. a capsule dir that doesn't exist yet, so disposal silently no-ops)
    could ship green.
    """
    workers = _workers(tmp_path, _ScriptedAdapter(_EMIT))
    invocation = workers._invocation_dir("review-cloning-only")
    journal = cw.InvocationJournal(invocation / "journal.json", "review-cloning-only")
    journal.transition("prepared", launch_nonce="nonce")
    capsule = workers.capsule_root / "review-cloning-only"
    capsule.mkdir(parents=True)
    (capsule / "evidence.txt").write_text("x", encoding="utf-8")
    journal.transition("cloning", capsule=str(capsule))

    receipt = workers.cancel(
        "review-cloning-only", cw.OwnerProof(owner_id="owner", harness="codex"), "operator"
    )

    assert receipt["state"] == "quarantined"
    on_disk = json.loads((invocation / "journal.json").read_text())
    assert on_disk["state"] == "quarantined"
    assert on_disk["disposal"]["quarantined"] is True
    quarantined = list((tmp_path / "capsules" / "quarantine").glob("*"))
    assert len(quarantined) == 1
    assert (quarantined[0] / "evidence.txt").is_file()
    assert not capsule.exists()


def test_recover_invocation_cli_moves_a_dead_running_journal_to_quarantined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workers = _workers(tmp_path, _ScriptedAdapter(_EMIT))
    invocation, _journal = _running_journal(
        workers, "review-cli-recovery", launch=_launch_evidence()
    )
    monkeypatch.setattr(cw.lease, "hostname", lambda: "recovery-host")
    monkeypatch.setattr(cw.lease, "boot_id", lambda: "boot-a")
    monkeypatch.setattr(cw, "_process_group_absent", lambda pgid: True)
    monkeypatch.setenv("FLOW_HARNESS", "codex")

    rc = cw.cli_main(
        [
            "recover-invocation",
            "--logical-invocation-id",
            "review-cli-recovery",
            "--artifact-root",
            str(tmp_path / "artifacts"),
            "--capsule-root",
            str(tmp_path / "capsules"),
            "--reason",
            "operator recovery",
        ]
    )

    assert rc == 0
    assert json.loads((invocation / "journal.json").read_text())["state"] == "quarantined"


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


def test_run_records_the_roles_soft_and_hard_budgets_in_physical_attempts(
    tmp_path: Path,
) -> None:
    source, sha = _repository(tmp_path)
    input_path = tmp_path / "input.json"
    input_path.write_text("{}\n", encoding="utf-8")
    order = _review_order(source, sha, input_path, "review-budget-receipt")
    outcome = _workers(tmp_path, _ScriptedAdapter(_EMIT)).run(
        order, cw.OwnerProof(owner_id="owner", harness="codex")
    )
    attempts = outcome.receipts["physical_attempts"]
    assert attempts[0]["soft_budget_seconds"] == 600
    assert attempts[0]["hard_budget_seconds"] == 2400


def test_worker_exited_persists_failure_tails_beside_the_journal(tmp_path: Path) -> None:
    source, sha = _repository(tmp_path)
    input_path = tmp_path / "input.json"
    input_path.write_text("{}\n", encoding="utf-8")
    body = "import sys; sys.stdout.write('boom-out'); sys.stderr.write('boom-err'); sys.exit(3)"
    order = _review_order(source, sha, input_path, "review-worker-exited")

    with pytest.raises(cw.WorkerFailure, match="exited 3") as error:
        _workers(tmp_path, _ScriptedAdapter(body)).run(
            order, cw.OwnerProof(owner_id="owner", harness="codex")
        )
    assert error.value.code == "worker_exited"

    invocation = (
        tmp_path / "artifacts" / "invocations" / hashlib.sha256(b"review-worker-exited").hexdigest()
    )
    assert (invocation / "attempt-1-stdout.tail").read_bytes() == b"boom-out"
    assert (invocation / "attempt-1-stderr.tail").read_bytes() == b"boom-err"


def test_a_successful_invocation_never_creates_failure_tail_artifacts(tmp_path: Path) -> None:
    source, sha = _repository(tmp_path)
    input_path = tmp_path / "input.json"
    input_path.write_text("{}\n", encoding="utf-8")
    order = _review_order(source, sha, input_path, "review-success-no-tails")
    _workers(tmp_path, _ScriptedAdapter(_EMIT)).run(
        order, cw.OwnerProof(owner_id="owner", harness="codex")
    )
    invocation = (
        tmp_path
        / "artifacts"
        / "invocations"
        / hashlib.sha256(b"review-success-no-tails").hexdigest()
    )
    assert list(invocation.glob("*.tail")) == []


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


# --- machinery_fixer: a read_only capsule whose report reflect applies via machinery_edit ---

_MACHINERY_FACTS = {
    "stage_reflect": "Diagnose the harness friction and return anchored edits only.",
    "friction": [{"anchor": "planned_files", "severity": "major"}],
    "source_sha": "a" * 40,
    "harness_files": ["references/stage-reflect.md"],
    "report_contract": {"schema": "machinery-fix-report/v1"},
}


def _machinery_order(source: Path, sha: str, input_path: Path, logical_id: str) -> cw.WorkOrder:
    import hashlib

    return cw.WorkOrder(
        logical_invocation_id=logical_id,
        generation=1,
        profile="machinery_fixer",
        source_root=str(source),
        source_sha=sha,
        route={"harness": "codex", "model": "gpt-5.6-luna", "effort": "high"},
        route_snapshot_digest="b" * 64,
        input_bundle=str(input_path),
        input_digest=hashlib.sha256(input_path.read_bytes()).hexdigest(),
        facts={**_MACHINERY_FACTS, "source_sha": sha},
    )


def _machinery_report(sha: str) -> dict[str, Any]:
    return {
        "summary": "Scan owned files with --untracked-files=all.",
        "source_sha": sha,
        "edits": [
            {
                "file": "references/stage-reflect.md",
                "old": "OLD ANCHOR",
                "new": "NEW ANCHOR",
                "rationale": "the bare status collapses a fully-untracked dir",
            }
        ],
    }


def test_machinery_fixer_order_binds_read_only_and_builds_its_prompt(tmp_path: Path) -> None:
    input_path = tmp_path / "input.json"
    input_path.write_text("{}\n", encoding="utf-8")
    order = _machinery_order(tmp_path, "a" * 40, input_path, "m-order")
    # A read_only order carries no writer surface: no allowed paths, no seed.
    assert order.authority == "read_only"
    assert order.allowed_mutation_paths == ()
    assert order.seed_patch is None
    material = cw.build_machinery_fixer_prompt(order.facts)
    assert material.builder_id == "machinery_fixer/v1"
    assert "FLOW COGNITIVE ROLE: machinery_fixer" in material.prompt
    # The closed fact set is enforced: a missing fact is refused.
    with pytest.raises(cw.WorkerFailure, match="missing facts"):
        cw.build_machinery_fixer_prompt({"friction": []})


def test_machinery_fix_report_rejects_malformed_edits() -> None:
    good = {
        "summary": "s",
        "source_sha": "a" * 40,
        "edits": [{"file": "f", "old": "O", "new": "N", "rationale": "r"}],
    }
    assert cw.validate_typed_result("machinery_fixer", good)["summary"] == "s"
    malformed = (
        # missing the rationale anchor field
        {"summary": "s", "source_sha": "a" * 40, "edits": [{"file": "f", "old": "O", "new": "N"}]},
        # old == new is a no-op the guard would reject
        {
            "summary": "s",
            "source_sha": "a" * 40,
            "edits": [{"file": "f", "old": "X", "new": "X", "rationale": "r"}],
        },
        # an extra key breaks the closed edit shape
        {
            "summary": "s",
            "source_sha": "a" * 40,
            "edits": [{"file": "f", "old": "O", "new": "N", "rationale": "r", "extra": 1}],
        },
        # an empty anchor string
        {
            "summary": "s",
            "source_sha": "a" * 40,
            "edits": [{"file": "", "old": "O", "new": "N", "rationale": "r"}],
        },
        # source_sha is not a 40-char SHA
        {"summary": "s", "source_sha": "short", "edits": []},
        # the edits field is absent
        {"summary": "s", "source_sha": "a" * 40},
    )
    for bad in malformed:
        with pytest.raises(cw.WorkerFailure, match=r"contract|no-op|SHA"):
            cw.validate_typed_result("machinery_fixer", bad)


def test_machinery_fix_report_edits_apply_through_the_machinery_edit_guard(tmp_path: Path) -> None:
    import machinery_edit

    # The report's edit shape is exactly machinery_edit.py's {file, old, new} payload.
    edit_props = set(
        cw.provider_schema("machinery_fixer")["properties"]["edits"]["items"]["properties"]
    )
    assert {"file", "old", "new"} <= edit_props

    report = cw.validate_typed_result("machinery_fixer", _machinery_report("a" * 40))
    edit = report["edits"][0]
    skill_root = tmp_path / "skill"
    (skill_root / "references").mkdir(parents=True)
    target = skill_root / edit["file"]
    target.write_text("intro OLD ANCHOR outro\n", encoding="utf-8")

    # Happy path: reflect applies the report edit through the guard (skill_root is not a repo, so
    # it resolves to no branch and the apply is allowed).
    applied, code = machinery_edit.apply_edit(
        skill_root, Path(edit["file"]), edit["old"], edit["new"]
    )
    assert code == 0
    assert applied["status"] == "applied"
    assert "NEW ANCHOR" in target.read_text(encoding="utf-8")

    # Refusal honored: a protected-branch skill root refuses (exit 2 -> PROPOSE + RECORD).
    refused, rc = machinery_edit.apply_edit(
        skill_root,
        Path(edit["file"]),
        "NEW ANCHOR",
        "OTHER",
        branch_resolver=lambda _root: "main",
    )
    assert rc == 2
    assert refused["status"] == "refused"

    # anchor_not_found: the anchor is gone and the replacement absent -> the agent re-derives.
    gone, rc2 = machinery_edit.apply_edit(skill_root, Path(edit["file"]), "MISSING ANCHOR", "X")
    assert rc2 == 3
    assert gone["status"] == "anchor_not_found"


def test_machinery_fixer_runs_read_only_and_never_enters_the_cas_import(tmp_path: Path) -> None:
    source, sha = _repository(tmp_path)
    input_path = tmp_path / "input.json"
    input_path.write_text("{}\n", encoding="utf-8")
    report = _machinery_report(sha)
    body = f"import json,sys; sys.stdout.write(json.dumps({{'result': {report!r}}}))"
    order = _machinery_order(source, sha, input_path, "machinery-read-only")

    outcome = _workers(tmp_path, _ScriptedAdapter(body)).run(
        order, cw.OwnerProof(owner_id="owner", harness="codex")
    )
    assert outcome.status == "succeeded"
    assert outcome.result is not None
    assert outcome.result["edits"][0]["file"] == "references/stage-reflect.md"
    # read_only: the capsule is disposed and no capsule-writer change receipt was produced, so the
    # capsule stayed byte-identical and the CAS import branch was never entered.
    assert outcome.receipts["disposal"]["absent"] is True
    assert "change" not in outcome.receipts

    # The report is bound to the capsule SHA: a report citing the wrong source SHA is refused.
    wrong = {**report, "source_sha": "0" * 40}
    wrong_body = f"import json,sys; sys.stdout.write(json.dumps({{'result': {wrong!r}}}))"
    wrong_order = _machinery_order(source, sha, input_path, "machinery-wrong-sha")
    with pytest.raises(cw.WorkerFailure, match="exact capsule source SHA"):
        _workers(tmp_path, _ScriptedAdapter(wrong_body)).run(
            wrong_order, cw.OwnerProof(owner_id="owner", harness="codex")
        )


def test_run_stage_launches_machinery_fixer_and_records_a_reasoned_skip(tmp_path: Path) -> None:
    source, sha = _repository(tmp_path)
    ticket_dir = tmp_path / "td"
    ticket_dir.mkdir()
    input_path = ticket_dir / "machinery_fix.input.json"
    input_path.write_text("{}\n", encoding="utf-8")

    def sealed(logical: str) -> dict[str, Any]:
        return {
            "logical_invocation_id": logical,
            "run_id": "0123456789abcdef",
            "stage": "reflect",
            "substep": "machinery_fix",
            "stage_generation": 1,
            "source_sha": sha,
            "route_snapshot_digest": "b" * 64,
            "profile": "machinery_fixer",
            "desired_route": {"harness": "codex", "model": "gpt-5.6-luna", "effort": "high"},
            "activation": "pending",
            "conditional": True,
            "ticket_dir": str(ticket_dir),
            "lease_fence": None,
            "artifact_root": str(ticket_dir / "cognitive" / "reflect"),
        }

    report = _machinery_report(sha)
    body = f"import json,sys; sys.stdout.write(json.dumps({{'result': {report!r}}}))"
    workers = _workers(tmp_path, _ScriptedAdapter(body))

    # Routed derivation: run_stage launches the machinery_fixer capsule and publishes its report.
    launched = cw.run_stage(
        {
            "stage": "reflect",
            "generation": 1,
            "cognitive_substeps": {"machinery_fix": sealed("run-launch")},
        },
        {
            "machinery_fix": {
                "facts": {**_MACHINERY_FACTS, "source_sha": sha},
                "input_bundle": str(input_path),
            }
        },
        source_root=source,
        artifact_root=ticket_dir / "cognitive" / "reflect",
        capsule_root=ticket_dir / "cognitive" / "capsules",
        owner_id="owner",
        owner_harness="codex",
        workers=workers,
    )
    assert launched["cognitive_outcomes"]["machinery_fix"]["status"] == "succeeded"
    published = json.loads(Path(launched["results"]["machinery_fix"]).read_text(encoding="utf-8"))
    assert published["edits"][0]["file"] == "references/stage-reflect.md"
    assert launched["cognitive_skips"] == {}

    # No-fix path: the conditional machinery_fix substep satisfies the fence with a reasoned skip.
    skipped = cw.run_stage(
        {
            "stage": "reflect",
            "generation": 1,
            "cognitive_substeps": {"machinery_fix": sealed("run-skip")},
        },
        {"machinery_fix": {"skip": {"reason": "machinery reflection off; no anchored fix"}}},
        source_root=source,
        artifact_root=ticket_dir / "cognitive" / "reflect",
        capsule_root=ticket_dir / "cognitive" / "capsules",
        owner_id="owner",
        owner_harness="codex",
        workers=workers,
    )
    assert set(skipped["cognitive_skips"]) == {"machinery_fix"}
    assert skipped["cognitive_outcomes"] == {}
