from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import planning_attempt as pa


def _plan(*, version: int = 1, parent: str | None = None) -> dict[str, object]:
    return {
        "attempt_id": "attempt-1",
        "version": version,
        "parent_digest": parent,
        "base_sha": "a" * 40,
        "route_digest": "b" * 64,
        "author": {
            "id": "codex:gpt-5.6-sol",
            "harness": "codex",
            "model": "gpt-5.6-sol",
            "thread": "live-1",
        },
        "status": "PLAN_READY",
        "plan": {
            "motivation": "Make planning provenance explicit.",
            "goal": "Make every planning decision reviewable and exactly attributable.",
            "scenarios": [{"before": "Implicit owner model", "after": "Exact planner route"}],
            "architecture": ["owner", "planner", "native gate"],
            "decisions": ["Keep one writable owner cockpit"],
            "acceptance_outcomes": ["The approved plan is bound to its author and assessor"],
            "steps": ["Run the exact planner route", "Review and approve the typed result"],
            "files": ["src/planning.py"],
            "context_paths": ["src/routing.py"],
            "verification": ["pytest"],
            "e2e_recipe": "Run the fake planner CLI integration test.",
            "lane": "full",
            "compatibility": ["Existing host-native planning remains available"],
            "rollout": "Enable only through an explicit route override.",
            "risks": ["provider CLI drift"],
        },
        "questions": [],
        "incorporated_feedback_ids": [],
    }


def _attempt() -> pa.PlanningAttempt:
    return pa.PlanningAttempt.create(
        attempt_id="attempt-1",
        base_sha="a" * 40,
        route_digest="b" * 64,
        owner_identity="claude-owner",
    )


def _launch_receipt(
    *,
    profile: str = "planner",
    snapshot_digest: str = "b" * 64,
    harness: str = "codex",
    model: str = "gpt-5.6-sol",
    version: int = 1,
    activation: str = "active",
    transport: str = "cli",
    worker_id: str | None = None,
) -> dict[str, object]:
    desired = {"harness": harness, "model": model, "effort": "xhigh"}
    body: dict[str, object] = {
        "schema": "flow.agent-route-receipt/v1",
        "snapshot_digest": snapshot_digest,
        "profile": profile,
        "source": "override",
        "desired": desired,
        "effective": desired if activation == "active" else None,
        "activation": activation,
        "reason": "test receipt",
        "launch_request": desired,
        "transport": transport,
        "adapter_version": "test",
        "canonical_model": None,
        "worker_id": worker_id,
        "prompt_hash": pa.canonical_digest({"version": version}),
        "schema_hash": "d" * 64,
    }
    return {**body, "digest": pa.canonical_digest(body)}


def _accept(
    attempt: pa.PlanningAttempt, payload: dict[str, object] | None = None
) -> pa.PlanEnvelope:
    value = payload or _plan()
    version_value = value["version"]
    assert isinstance(version_value, int)
    version = version_value
    return attempt.accept(value, launch_receipt=_launch_receipt(version=version))


def _verdict(
    attempt: pa.PlanningAttempt,
    *,
    outcome: str = "pass",
    author_id: str = "codex:gpt-5.6-sol",
    plan_digest: str | None = None,
    fresh: bool = False,
) -> pa.AssessorVerdict:
    current = attempt.current
    assert current is not None
    return pa.AssessorVerdict.create(
        assessor_id="claude-owner",
        author_id=author_id,
        plan_digest=plan_digest or current.digest,
        outcome=outcome,
        findings=[] if outcome == "pass" else ["missing test"],
        fresh=fresh,
    )


def test_accepts_complete_envelope_with_compare_and_swap() -> None:
    attempt = _attempt()
    first = _accept(attempt)
    second = _accept(attempt, _plan(version=2, parent=first.digest))
    assert second.version == 2
    assert attempt.current == second
    assert [item.version for item in attempt.history] == [1, 2]


def test_accept_requires_exact_unique_planner_launch_attestation() -> None:
    attempt = _attempt()
    with pytest.raises(pa.AttemptError, match="does not prove"):
        attempt.accept(
            _plan(),
            launch_receipt=_launch_receipt(activation="shadow", transport="unknown"),
        )
    assert attempt.current is None
    first_receipt = _launch_receipt(version=1)
    first = attempt.accept(_plan(), launch_receipt=first_receipt)
    with pytest.raises(pa.AttemptError, match="already been bound"):
        attempt.accept(
            _plan(version=2, parent=first.digest),
            launch_receipt=first_receipt,
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("attempt_id", "other", "attempt id"),
        ("base_sha", "c" * 40, "base SHA"),
        ("route_digest", "d" * 64, "route digest"),
        ("version", 2, "version"),
        ("parent_digest", "e" * 64, "parent digest"),
    ],
)
def test_rejects_stale_or_mismatched_envelopes(field: str, value: object, message: str) -> None:
    attempt = _attempt()
    payload = _plan()
    payload[field] = value
    with pytest.raises(pa.AttemptError, match=message):
        _accept(attempt, payload)
    assert attempt.current is None


def test_rejects_prose_delta_and_malformed_questions() -> None:
    attempt = _attempt()
    payload = _plan()
    payload["plan"] = "Changed section three"
    with pytest.raises(pa.AttemptError, match="complete plan object"):
        _accept(attempt, payload)
    payload = _plan()
    payload["status"] = "NEEDS_INPUT"
    payload["questions"] = [{"id": "Q-1", "text": "", "anchors": []}]
    with pytest.raises(pa.AttemptError, match="question text"):
        _accept(attempt, payload)


def test_rejects_incomplete_plan_and_author_identity() -> None:
    attempt = _attempt()
    payload = _plan()
    payload["plan"] = {"motivation": "why"}
    with pytest.raises(pa.AttemptError, match="goal"):
        _accept(attempt, payload)
    payload = _plan()
    payload["author"] = {"harness": "codex", "model": "gpt-5.6-sol"}
    with pytest.raises(pa.AttemptError, match="author requires id"):
        _accept(attempt, payload)


def test_failed_accept_is_atomic_when_feedback_was_already_rejected() -> None:
    attempt = _attempt()
    attempt.add_feedback(
        feedback_id="F-1", verbatim="Do not do this", anchors=[], owner_synthesis=""
    )
    attempt.reject_feedback("F-1", "Conflicts with an accepted constraint")
    payload = _plan()
    payload["incorporated_feedback_ids"] = ["F-1"]
    before = attempt.to_mapping()
    with pytest.raises(pa.AttemptError, match="cannot also be incorporated"):
        _accept(attempt, payload)
    assert attempt.to_mapping() == before
    assert attempt.current is None


def test_feedback_is_verbatim_watermarked_and_gate_blocking() -> None:
    attempt = _attempt()
    attempt.add_feedback(
        feedback_id="F-1",
        verbatim="Keep the fallback visible.",
        anchors=["review:fallback"],
        owner_synthesis="The fallback must preserve the gate contract.",
    )
    payload = _plan()
    payload["incorporated_feedback_ids"] = ["F-1"]
    envelope = _accept(attempt, payload)
    attempt.assess(_verdict(attempt))
    attempt.revalidate(
        pa.RevalidationReceipt.create(
            approved_base="a" * 40,
            latest_base="c" * 40,
            changed_paths=["README.md"],
            planned_paths=["src/planning.py"],
            context_paths=["src/routing.py"],
        )
    )
    gate = attempt.gate_tuple()
    assert envelope.incorporated_feedback_ids == ("F-1",)
    assert attempt.feedback["F-1"].verbatim == "Keep the fallback visible."
    assert gate.feedback_watermark == pa.feedback_watermark(attempt.feedback.values())
    with pytest.raises(pa.AttemptError, match="cannot later be rejected"):
        attempt.reject_feedback("F-1", "changed my mind")


def test_feedback_preserves_verbatim_whitespace(tmp_path: Path) -> None:
    attempt = _attempt()
    entry = attempt.add_feedback(
        feedback_id="F-exact",
        verbatim="  Keep my spacing.\n",
        anchors=[],
        owner_synthesis="spacing matters",
    )
    assert entry.verbatim == "  Keep my spacing.\n"
    attempt.save_bundle(tmp_path)
    loaded = pa.PlanningAttempt.load_bundle(tmp_path)
    assert loaded.feedback["F-exact"].verbatim == "  Keep my spacing.\n"


def test_pending_feedback_and_failed_assessment_block_gate() -> None:
    attempt = _attempt()
    _accept(attempt)
    attempt.add_feedback(feedback_id="F-1", verbatim="Explain why.", anchors=[], owner_synthesis="")
    with pytest.raises(pa.AttemptError, match="pending feedback"):
        attempt.gate_tuple()
    attempt.reject_feedback("F-1", "Conflicts with the ticket acceptance outcome")
    attempt.assess(_verdict(attempt, outcome="fail"))
    with pytest.raises(pa.AttemptError, match="assessment has not passed"):
        attempt.gate_tuple()


def test_assessor_policy_and_author_separation() -> None:
    assert pa.requires_fresh_assessor(owner_authored=True)
    assert pa.requires_fresh_assessor(unattended=True)
    assert pa.requires_fresh_assessor(hot=True)
    assert pa.requires_fresh_assessor(escalated=True)
    assert not pa.requires_fresh_assessor()
    with pytest.raises(pa.AttemptError, match="author and assessor"):
        pa.AssessorVerdict.create(
            assessor_id="same",
            author_id="same",
            plan_digest="a" * 64,
            outcome="pass",
            findings=[],
        )


def test_assessment_is_bound_to_current_plan_and_its_actual_author() -> None:
    attempt = _attempt()
    _accept(attempt)
    with pytest.raises(pa.AttemptError, match="plan author"):
        attempt.assess(_verdict(attempt, author_id="some-other-planner"))
    with pytest.raises(pa.AttemptError, match="current plan digest"):
        attempt.assess(_verdict(attempt, plan_digest="f" * 64))
    assert attempt.assessment is None
    attempt.assess(_verdict(attempt))
    assert attempt.assessment is not None


def test_fresh_assessment_requires_matching_worker_launch_provenance() -> None:
    attempt = _attempt()
    _accept(attempt)
    receipt = _launch_receipt(
        profile="plan_assessor",
        harness="claude_code",
        model="opus",
        activation="shadow",
        transport="codex_collaboration",
        worker_id="assessor-agent-1",
    )
    current = attempt.current
    assert current is not None
    verdict = pa.AssessorVerdict.create(
        assessor_id="assessor-agent-1",
        author_id=current.author["id"],
        plan_digest=current.digest,
        outcome="pass",
        findings=[],
        fresh=True,
        launch_receipt_digest=str(receipt["digest"]),
    )
    with pytest.raises(pa.AttemptError, match="structured launch receipt"):
        attempt.assess(verdict, require_fresh=True)
    attempt.assess(verdict, require_fresh=True, launch_receipt=receipt)
    assert attempt.assessment_launch_receipt == receipt


def test_selective_revalidation_classifies_unrelated_relevant_and_ambiguous() -> None:
    unrelated = pa.RevalidationReceipt.create(
        approved_base="a" * 40,
        latest_base="b" * 40,
        changed_paths=["docs/readme.md"],
        planned_paths=["src/planning.py"],
        context_paths=["src/routing.py"],
    )
    relevant = pa.RevalidationReceipt.create(
        approved_base="a" * 40,
        latest_base="b" * 40,
        changed_paths=["src/routing.py"],
        planned_paths=["src/planning.py"],
        context_paths=["src/routing.py"],
    )
    ambiguous = pa.RevalidationReceipt.create(
        approved_base="a" * 40,
        latest_base="b" * 40,
        changed_paths=None,
        planned_paths=["src/planning.py"],
        context_paths=[],
    )
    assert unrelated.classification == "unrelated"
    assert relevant.classification == "relevant"
    assert ambiguous.classification == "ambiguous"
    prefix_relevant = pa.RevalidationReceipt.create(
        approved_base="a" * 40,
        latest_base="b" * 40,
        changed_paths=["src/planning/worker.py"],
        planned_paths=["src/planning"],
        context_paths=[],
    )
    sibling_unrelated = pa.RevalidationReceipt.create(
        approved_base="a" * 40,
        latest_base="b" * 40,
        changed_paths=["src/planning_extra.py"],
        planned_paths=["src/planning.py"],
        context_paths=[],
    )
    assert prefix_relevant.classification == "relevant"
    assert sibling_unrelated.classification == "unrelated"


def test_relevant_drift_invalidates_current_plan() -> None:
    attempt = _attempt()
    _accept(attempt)
    attempt.revalidate(
        pa.RevalidationReceipt.create(
            approved_base="a" * 40,
            latest_base="b" * 40,
            changed_paths=["src/planning.py"],
            planned_paths=["src/planning.py"],
            context_paths=[],
        )
    )
    assert attempt.current is None
    assert attempt.requires_fresh_rehydration is True


def test_native_receipt_binds_exact_tuple_and_plan_bytes(tmp_path: Path) -> None:
    attempt = _attempt()
    _accept(attempt)
    attempt.assess(_verdict(attempt))
    attempt.revalidate(
        pa.RevalidationReceipt.create(
            approved_base="a" * 40,
            latest_base="a" * 40,
            changed_paths=[],
            planned_paths=["src/planning.py"],
            context_paths=[],
        )
    )
    current = attempt.current
    assert current is not None
    plan_bytes = pa.approval_plan_bytes(current)
    receipt = attempt.freeze(native_gate_id="gate-42", plan_bytes=plan_bytes)
    path = tmp_path / "approval.json"
    pa.write_approval_receipt(path, receipt)
    loaded = pa.load_approval_receipt(path)
    assert loaded.approved_base_sha == "a" * 40
    assert loaded.plan_file_sha256 == hashlib.sha256(plan_bytes).hexdigest()
    current = attempt.current
    assert current is not None
    assert loaded.gate.plan_digest == current.digest
    assert json.loads(path.read_text())["digest"] == receipt.digest
    another = _attempt()
    _accept(another)
    another.assess(_verdict(another))
    another.revalidate(
        pa.RevalidationReceipt.create(
            approved_base="a" * 40,
            latest_base="a" * 40,
            changed_paths=[],
            planned_paths=["src/planning.py"],
            context_paths=[],
        )
    )
    with pytest.raises(pa.AttemptError, match="canonical rendering"):
        another.freeze(native_gate_id="gate-other", plan_bytes=b"# Different plan\n")
    with pytest.raises(pa.AttemptError, match="frozen"):
        attempt.add_feedback(feedback_id="F-late", verbatim="late", anchors=[], owner_synthesis="")


def test_ephemeral_bundle_excludes_worker_thread_receipt(tmp_path: Path) -> None:
    attempt = _attempt()
    _accept(attempt)
    attempt.save_bundle(tmp_path)
    raw = (tmp_path / "attempt.json").read_text(encoding="utf-8")
    assert "live-1" not in raw
    assert "thread_id" not in raw
    loaded = pa.PlanningAttempt.load_bundle(tmp_path)
    assert loaded.current is not None
    assert loaded.current.author["harness"] == "codex"


def test_cli_round_trip_reaches_exact_approval_receipt(tmp_path: Path, capsys) -> None:
    attempt_dir = tmp_path / "attempt"
    assert (
        pa.cli_main(
            [
                "create",
                "--attempt-dir",
                str(attempt_dir),
                "--attempt-id",
                "attempt-1",
                "--base-sha",
                "a" * 40,
                "--route-digest",
                "b" * 64,
                "--owner-identity",
                "owner",
            ]
        )
        == 0
    )
    capsys.readouterr()
    envelope_path = tmp_path / "envelope.json"
    envelope_path.write_text(json.dumps(_plan()), encoding="utf-8")
    route_receipt_path = tmp_path / "planner-route.json"
    route_receipt_path.write_text(json.dumps(_launch_receipt()), encoding="utf-8")
    assert (
        pa.cli_main(
            [
                "accept",
                "--attempt-dir",
                str(attempt_dir),
                "--envelope-from",
                str(envelope_path),
                "--route-receipt",
                str(route_receipt_path),
            ]
        )
        == 0
    )
    accepted = json.loads(capsys.readouterr().out)
    verdict_path = tmp_path / "verdict.json"
    verdict_path.write_text(
        json.dumps(
            pa.AssessorVerdict.create(
                assessor_id="owner",
                author_id="codex:gpt-5.6-sol",
                plan_digest=accepted["digest"],
                outcome="pass",
                findings=[],
            ).to_mapping()
        ),
        encoding="utf-8",
    )
    assert (
        pa.cli_main(
            [
                "assess",
                "--attempt-dir",
                str(attempt_dir),
                "--verdict-from",
                str(verdict_path),
            ]
        )
        == 0
    )
    capsys.readouterr()
    revalidation_path = tmp_path / "revalidation.json"
    revalidation_path.write_text(
        json.dumps(
            pa.RevalidationReceipt.create(
                approved_base="a" * 40,
                latest_base="a" * 40,
                changed_paths=[],
                planned_paths=["src/planning.py"],
                context_paths=[],
            ).to_mapping()
        ),
        encoding="utf-8",
    )
    assert (
        pa.cli_main(
            [
                "revalidate",
                "--attempt-dir",
                str(attempt_dir),
                "--receipt-from",
                str(revalidation_path),
            ]
        )
        == 0
    )
    capsys.readouterr()
    plan_path = tmp_path / "plan.md"
    assert (
        pa.cli_main(
            [
                "render-plan",
                "--attempt-dir",
                str(attempt_dir),
                "--output",
                str(plan_path),
            ]
        )
        == 0
    )
    capsys.readouterr()
    approval_path = tmp_path / "approval.json"
    assert (
        pa.cli_main(
            [
                "approve",
                "--attempt-dir",
                str(attempt_dir),
                "--native-gate-id",
                "gate-1",
                "--plan-from",
                str(plan_path),
                "--output",
                str(approval_path),
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert (
        pa.cli_main(
            [
                "verify-approval",
                "--receipt",
                str(approval_path),
                "--plan-from",
                str(plan_path),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["native_gate_id"] == "gate-1"
