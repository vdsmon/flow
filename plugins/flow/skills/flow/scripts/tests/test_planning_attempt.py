from __future__ import annotations

import contextlib
import hashlib
import json
import re
import threading
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


def _write_json(path: Path, value: dict[str, object]) -> Path:
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


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
        "physical_attempt": {"pid": 17, "terminal_acknowledged": True},
        "cleanup": {"capsule_absent": True, "quarantined": False},
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


def _ready_for_gate(attempt: pa.PlanningAttempt) -> pa.PlanEnvelope:
    envelope = _accept(attempt)
    attempt.assess(_verdict(attempt))
    attempt.revalidate(
        pa.RevalidationReceipt.create(
            approved_base="a" * 40,
            latest_base="a" * 40,
            changed_paths=[],
            planned_paths=["src/planning.py"],
            context_paths=["src/routing.py", ".flow/workspace.toml"],
        )
    )
    return envelope


def test_provider_schema_is_closed_without_provider_side_uniqueness() -> None:
    schema = pa.envelope_json_schema()

    def walk(value: object) -> None:
        if isinstance(value, dict):
            assert "uniqueItems" not in value
            if value.get("type") == "object":
                assert value.get("additionalProperties") is False
                required = value.get("required", [])
                properties = value.get("properties", {})
                assert isinstance(required, list)
                assert isinstance(properties, dict)
                assert set(required) == set(properties)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(schema)
    assert set(schema["properties"]["author"]["properties"]) == {
        "id",
        "harness",
        "model",
    }
    assert set(schema["properties"]["plan"]["properties"]) == set(pa._PLAN_REQUIRED)


def test_envelope_schema_teaches_the_exact_author_identity() -> None:
    author = pa.envelope_json_schema()["properties"]["author"]["properties"]
    pattern = author["id"]["pattern"]
    assert re.search(pattern, "codex:gpt-5.6-sol")
    assert re.search(pattern, "claude_code:opus")
    assert not re.search(pattern, "gpt-5.6-sol")
    for field in ("id", "harness", "model"):
        assert author[field]["description"]
    assert "<harness>:<model>" in author["id"]["description"]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("incorporated_feedback_ids", ["F-1", "F-1"], "must not contain duplicates"),
        (
            "questions",
            [
                {"id": "Q-1", "text": "First?", "anchors": []},
                {"id": "Q-1", "text": "Again?", "anchors": []},
            ],
            "duplicate question id",
        ),
    ],
)
def test_semantic_validation_still_rejects_duplicate_lists(
    field: str, value: object, message: str
) -> None:
    payload = _plan()
    payload["status"] = "NEEDS_INPUT" if field == "questions" else "PLAN_READY"
    payload[field] = value
    with pytest.raises(pa.AttemptError, match=message):
        pa.PlanEnvelope.from_mapping(payload)


def test_semantic_validation_rejects_provider_object_extensions() -> None:
    payload = _plan()
    author = payload["author"]
    assert isinstance(author, dict)
    payload["author"] = {**author, "thread": "must-stay-outside-envelope"}
    with pytest.raises(pa.AttemptError, match=r"author.*unknown"):
        pa.PlanEnvelope.from_mapping(payload)

    payload = _plan()
    plan = payload["plan"]
    assert isinstance(plan, dict)
    payload["plan"] = {**plan, "reading_time": "five minutes"}
    with pytest.raises(pa.AttemptError, match=r"plan.*unknown"):
        pa.PlanEnvelope.from_mapping(payload)


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
        activation="active",
        transport="cli",
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
    route_config_relevant = pa.RevalidationReceipt.create(
        approved_base="a" * 40,
        latest_base="b" * 40,
        changed_paths=[".flow/workspace.toml"],
        planned_paths=["src/planning.py"],
        context_paths=[".flow/workspace.toml"],
    )
    assert route_config_relevant.classification == "relevant"


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
    _ready_for_gate(attempt)
    current = attempt.current
    assert current is not None
    plan_bytes = pa.approval_plan_bytes(current)
    gate = attempt.gate_tuple()
    receipt = attempt.freeze(
        native_gate_id="gate-42",
        expected_gate_digest=gate.digest,
        plan_bytes=plan_bytes,
    )
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
    _ready_for_gate(another)
    with pytest.raises(pa.AttemptError, match="canonical rendering"):
        another.freeze(
            native_gate_id="gate-other",
            expected_gate_digest=another.gate_tuple().digest,
            plan_bytes=b"# Different plan\n",
        )
    with pytest.raises(pa.AttemptError, match="frozen"):
        attempt.add_feedback(feedback_id="F-late", verbatim="late", anchors=[], owner_synthesis="")


def test_freeze_binds_the_approved_plan_lane_into_the_receipt(tmp_path: Path) -> None:
    attempt = _attempt()
    payload = _plan()
    plan = payload["plan"]
    assert isinstance(plan, dict)
    payload["plan"] = {**plan, "lane": "light"}
    attempt.accept(payload, launch_receipt=_launch_receipt(version=1))
    attempt.assess(_verdict(attempt))
    attempt.revalidate(
        pa.RevalidationReceipt.create(
            approved_base="a" * 40,
            latest_base="a" * 40,
            changed_paths=[],
            planned_paths=["src/planning.py"],
            context_paths=["src/routing.py", ".flow/workspace.toml"],
        )
    )
    current = attempt.current
    assert current is not None
    plan_bytes = pa.approval_plan_bytes(current)
    gate = attempt.gate_tuple()
    receipt = attempt.freeze(
        native_gate_id="gate-lane",
        expected_gate_digest=gate.digest,
        plan_bytes=plan_bytes,
    )
    assert receipt.plan_lane == "light"
    path = tmp_path / "approval.json"
    pa.write_approval_receipt(path, receipt)
    loaded = pa.load_approval_receipt(path)
    assert loaded.plan_lane == "light"

    raw = json.loads(path.read_text())
    raw["plan_lane"] = "full"
    with pytest.raises(pa.AttemptError, match="canonical content"):
        pa.load_approval_receipt(_write_json(tmp_path / "tampered.json", raw))


def test_legacy_receipt_without_plan_lane_round_trips_its_digest(tmp_path: Path) -> None:
    attempt = _attempt()
    _ready_for_gate(attempt)
    current = attempt.current
    assert current is not None
    plan_bytes = pa.approval_plan_bytes(current)
    gate = attempt.gate_tuple()
    receipt = attempt.freeze(
        native_gate_id="gate-legacy",
        expected_gate_digest=gate.digest,
        plan_bytes=plan_bytes,
    )
    legacy_body = {
        key: value
        for key, value in receipt.to_mapping().items()
        if key not in {"digest", "plan_lane"}
    }
    legacy_digest = pa.canonical_digest(legacy_body)
    legacy = {**legacy_body, "digest": legacy_digest}
    path = _write_json(tmp_path / "legacy.json", legacy)

    loaded = pa.load_approval_receipt(path)
    assert loaded.plan_lane is None

    reserialized = {key: value for key, value in loaded.to_mapping().items() if key != "digest"}
    assert "plan_lane" not in reserialized
    assert pa.canonical_digest(reserialized) == legacy_digest


def test_approval_receipt_rejects_explicit_null_and_invalid_plan_lane(tmp_path: Path) -> None:
    attempt = _attempt()
    _ready_for_gate(attempt)
    current = attempt.current
    assert current is not None
    plan_bytes = pa.approval_plan_bytes(current)
    gate = attempt.gate_tuple()
    receipt = attempt.freeze(
        native_gate_id="gate-invalid",
        expected_gate_digest=gate.digest,
        plan_bytes=plan_bytes,
    )
    legacy_body = {
        key: value
        for key, value in receipt.to_mapping().items()
        if key not in {"digest", "plan_lane"}
    }

    null_body = {**legacy_body, "plan_lane": None}
    null_receipt = {**null_body, "digest": pa.canonical_digest(null_body)}
    with pytest.raises(pa.AttemptError, match="plan_lane must not be null"):
        pa.load_approval_receipt(_write_json(tmp_path / "null.json", null_receipt))

    invalid_body = {**legacy_body, "plan_lane": "sideways"}
    invalid_receipt = {**invalid_body, "digest": pa.canonical_digest(invalid_body)}
    with pytest.raises(pa.AttemptError, match="plan_lane must be one of"):
        pa.load_approval_receipt(_write_json(tmp_path / "invalid.json", invalid_receipt))

    unhashable_body = {**legacy_body, "plan_lane": []}
    unhashable_receipt = {**unhashable_body, "digest": pa.canonical_digest(unhashable_body)}
    with pytest.raises(pa.AttemptError, match="plan_lane must be one of"):
        pa.load_approval_receipt(_write_json(tmp_path / "unhashable.json", unhashable_receipt))


def test_native_approval_requires_the_exact_pre_gate_digest() -> None:
    attempt = _attempt()
    current = _ready_for_gate(attempt)
    before = attempt.to_mapping()
    with pytest.raises(pa.AttemptError, match="pre-gate digest"):
        attempt.freeze(
            native_gate_id="gate-stale",
            expected_gate_digest="0" * 64,
            plan_bytes=pa.approval_plan_bytes(current),
        )
    assert attempt.to_mapping() == before
    assert attempt.frozen is False


def test_feedback_watermark_change_invalidates_an_outstanding_gate() -> None:
    attempt = _attempt()
    current = _ready_for_gate(attempt)
    stale_gate = attempt.gate_tuple()
    attempt.add_feedback(
        feedback_id="F-late",
        verbatim="Record this explicit rejection.",
        anchors=["plan:rollout"],
        owner_synthesis="This conflicts with the approved no-fallback decision.",
    )
    attempt.reject_feedback("F-late", "The accepted strict route forbids fallback.")
    attempt.assess(_verdict(attempt))
    attempt.revalidate(
        pa.RevalidationReceipt.create(
            approved_base="a" * 40,
            latest_base="a" * 40,
            changed_paths=[],
            planned_paths=["src/planning.py"],
            context_paths=[".flow/workspace.toml"],
        )
    )
    assert attempt.gate_tuple().feedback_watermark != stale_gate.feedback_watermark
    with pytest.raises(pa.AttemptError, match="pre-gate digest"):
        attempt.freeze(
            native_gate_id="gate-stale-feedback",
            expected_gate_digest=stale_gate.digest,
            plan_bytes=pa.approval_plan_bytes(current),
        )


def test_attempt_mutations_serialize_the_complete_load_cas_save_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempt = _attempt()
    attempt.save_bundle(tmp_path)
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()
    second_reached_lock = threading.Event()
    errors: list[BaseException] = []
    real_flock = pa._locking.flock_blocking

    @contextlib.contextmanager
    def observed_flock(path: Path):
        if threading.current_thread().name == "second-mutation":
            second_reached_lock.set()
        with real_flock(path):
            yield

    monkeypatch.setattr(pa._locking, "flock_blocking", observed_flock)

    def mutate(feedback_id: str, entered: threading.Event, wait: bool) -> None:
        try:

            def operation(loaded: pa.PlanningAttempt) -> None:
                entered.set()
                if wait:
                    assert release_first.wait(timeout=5)
                loaded.add_feedback(
                    feedback_id=feedback_id,
                    verbatim=feedback_id,
                    anchors=[],
                    owner_synthesis="",
                )

            pa.mutate_bundle(tmp_path, operation)
        except BaseException as exc:  # pragma: no cover - reported by the assertion below
            errors.append(exc)

    first = threading.Thread(target=mutate, args=("F-1", first_entered, True))
    second = threading.Thread(
        target=mutate,
        args=("F-2", second_entered, False),
        name="second-mutation",
    )
    first.start()
    assert first_entered.wait(timeout=5)
    second.start()
    assert second_reached_lock.wait(timeout=5)
    assert not second_entered.is_set()
    release_first.set()
    first.join(timeout=5)
    second.join(timeout=5)
    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert set(pa.PlanningAttempt.load_bundle(tmp_path).feedback) == {"F-1", "F-2"}


def test_concurrent_sibling_plan_versions_have_exactly_one_cas_winner(
    tmp_path: Path,
) -> None:
    _attempt().save_bundle(tmp_path)
    start = threading.Barrier(3)
    outcomes: list[str] = []

    def accept(worker_id: str) -> None:
        start.wait(timeout=5)
        try:
            pa.mutate_bundle(
                tmp_path,
                lambda attempt: attempt.accept(
                    _plan(),
                    launch_receipt=_launch_receipt(worker_id=worker_id),
                ),
            )
        except pa.AttemptError as exc:
            outcomes.append(str(exc))
        else:
            outcomes.append("accepted")

    workers = [
        threading.Thread(target=accept, args=("planner-a",)),
        threading.Thread(target=accept, args=("planner-b",)),
    ]
    for worker in workers:
        worker.start()
    start.wait(timeout=5)
    for worker in workers:
        worker.join(timeout=5)
        assert not worker.is_alive()
    assert sorted(outcomes) == [
        "accepted",
        "plan envelope version 1 does not match expected 2",
    ]
    loaded = pa.PlanningAttempt.load_bundle(tmp_path)
    assert [envelope.version for envelope in loaded.history] == [1]
    assert len(loaded.planner_launch_receipts) == 1


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
                "gate",
                "--attempt-dir",
                str(attempt_dir),
            ]
        )
        == 0
    )
    gate = json.loads(capsys.readouterr().out)
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
                "--expected-gate-digest",
                gate["digest"],
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


def _cli_create_attempt(tmp_path: Path, capsys) -> Path:
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
    return attempt_dir


def _feedback_cli(attempt_dir: Path, payload: object, path: Path) -> int:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return pa.cli_main(
        [
            "feedback",
            "--attempt-dir",
            str(attempt_dir),
            "--feedback-from",
            str(path),
        ]
    )


def test_cli_feedback_array_records_all_entries(tmp_path: Path, capsys) -> None:
    attempt_dir = _cli_create_attempt(tmp_path, capsys)
    payload = [
        {"id": "F-1", "verbatim": "First finding.", "anchors": [], "owner_synthesis": ""},
        {"id": "F-2", "verbatim": "Second finding.", "anchors": ["src/planning.py"]},
    ]
    assert _feedback_cli(attempt_dir, payload, tmp_path / "feedback.json") == 0
    output = json.loads(capsys.readouterr().out)
    assert isinstance(output, list)
    assert [entry["id"] for entry in output] == ["F-1", "F-2"]
    loaded = pa.PlanningAttempt.load_bundle(attempt_dir)
    assert set(loaded.feedback) == {"F-1", "F-2"}


def test_cli_feedback_array_malformed_entry_records_nothing(tmp_path: Path, capsys) -> None:
    attempt_dir = _cli_create_attempt(tmp_path, capsys)
    payload = [
        {"id": "F-1", "verbatim": "First finding.", "anchors": [], "owner_synthesis": ""},
        {"id": "F-2", "verbatim": "", "anchors": [], "owner_synthesis": ""},
    ]
    assert _feedback_cli(attempt_dir, payload, tmp_path / "feedback.json") == 2
    assert pa.PlanningAttempt.load_bundle(attempt_dir).feedback == {}


def test_cli_feedback_array_duplicate_id_records_nothing(tmp_path: Path, capsys) -> None:
    attempt_dir = _cli_create_attempt(tmp_path, capsys)
    payload = [
        {"id": "F-1", "verbatim": "First finding.", "anchors": [], "owner_synthesis": ""},
        {"id": "F-1", "verbatim": "Same id again.", "anchors": [], "owner_synthesis": ""},
    ]
    assert _feedback_cli(attempt_dir, payload, tmp_path / "feedback.json") == 2
    assert pa.PlanningAttempt.load_bundle(attempt_dir).feedback == {}


def test_cli_feedback_array_rejects_non_object_element(tmp_path: Path, capsys) -> None:
    attempt_dir = _cli_create_attempt(tmp_path, capsys)
    payload = [
        {"id": "F-1", "verbatim": "First finding.", "anchors": [], "owner_synthesis": ""},
        "not an object",
    ]
    assert _feedback_cli(attempt_dir, payload, tmp_path / "feedback.json") == 2
    assert pa.PlanningAttempt.load_bundle(attempt_dir).feedback == {}


def test_cli_feedback_empty_array_records_nothing_and_emits_empty_list(
    tmp_path: Path, capsys
) -> None:
    attempt_dir = _cli_create_attempt(tmp_path, capsys)
    assert _feedback_cli(attempt_dir, [], tmp_path / "feedback.json") == 0
    assert json.loads(capsys.readouterr().out) == []
    assert pa.PlanningAttempt.load_bundle(attempt_dir).feedback == {}


def test_cli_feedback_single_object_shape_unchanged(tmp_path: Path, capsys) -> None:
    attempt_dir = _cli_create_attempt(tmp_path, capsys)
    payload = {"id": "F-1", "verbatim": "Only finding.", "anchors": [], "owner_synthesis": ""}
    assert _feedback_cli(attempt_dir, payload, tmp_path / "feedback.json") == 0
    output = json.loads(capsys.readouterr().out)
    assert isinstance(output, dict)
    assert output["id"] == "F-1"
    loaded = pa.PlanningAttempt.load_bundle(attempt_dir)
    assert set(loaded.feedback) == {"F-1"}


def test_cli_feedback_rejects_non_object_non_array_input(tmp_path: Path, capsys) -> None:
    attempt_dir = _cli_create_attempt(tmp_path, capsys)
    assert _feedback_cli(attempt_dir, "a bare string", tmp_path / "feedback.json") == 2
    assert pa.PlanningAttempt.load_bundle(attempt_dir).feedback == {}
