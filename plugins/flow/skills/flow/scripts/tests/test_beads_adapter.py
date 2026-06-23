"""Contract tests for tracker_beads.py.

All tests mock-driven via a `_FakeRunner(responses)` that returns sequenced
`subprocess.CompletedProcess[str]` objects. No live `bd` calls.

Coverage:
- Construction preflight (bd --version): success, missing, too-old, malformed.
- Capability advertisement: 14 closed-enum entries, only comments_markdown +
  resolutions True.
- get/list_assigned/list_linked/list_transitions surface shapes.
- create + setters with postcondition re-read verification.
- transition routing: close / reopen / update-status; failure classification.
- comment via stdin; link via dep add.
- is_shipped: not_shipped / not_yet_observed / indeterminate branches.
- Capability-gated methods raise NotSupported.
- Structural Protocol conformance.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest

import tracker as t
import tracker_beads as tb

# ─── Fakes ───────────────────────────────────────────────────────────────────


def _cp(
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
    args: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=args or [],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


class _FakeRunner:
    """Sequenced subprocess fake. Returns the next response per `run()` call."""

    def __init__(self, responses: list[subprocess.CompletedProcess[str]]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((args, kwargs))
        if not self._responses:
            raise AssertionError(f"FakeRunner ran out of responses; got call args={args!r}")
        return self._responses.pop(0)


def _version_ok() -> subprocess.CompletedProcess[str]:
    return _cp(stdout="bd version 1.0.4 (Homebrew)\n")


def _build_adapter(
    extra_responses: list[subprocess.CompletedProcess[str]],
) -> tuple[tb.BeadsAdapter, _FakeRunner]:
    runner = _FakeRunner([_version_ok(), *extra_responses])
    adapter = tb.BeadsAdapter({"prefix": "testpkg", "actor": "alice"}, runner=runner)
    # Drop the version-check call so tests can index `runner.calls[0]` as the
    # first operational call.
    runner.calls.clear()
    return adapter, runner


# ─── Construction ────────────────────────────────────────────────────────────


def test_construct_ok_with_recent_bd_version() -> None:
    runner = _FakeRunner([_version_ok()])
    adapter = tb.BeadsAdapter({"prefix": "x"}, runner=runner)
    assert adapter.backend == "beads"
    assert isinstance(adapter.capabilities, list)


def test_construct_refuses_when_bd_missing() -> None:
    def runner(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del args, kwargs
        raise FileNotFoundError("bd not on PATH")

    with pytest.raises(t.TrackerConfigError, match="bd CLI not found"):
        tb.BeadsAdapter({"prefix": "x"}, runner=runner)


def test_construct_refuses_when_bd_too_old() -> None:
    runner = _FakeRunner([_cp(stdout="bd version 0.9.0 (test)\n")])
    with pytest.raises(t.TrackerConfigError, match="older than required"):
        tb.BeadsAdapter({"prefix": "x"}, runner=runner)


def test_construct_refuses_when_version_unparseable() -> None:
    runner = _FakeRunner([_cp(stdout="something unexpected\n")])
    with pytest.raises(t.TrackerConfigError, match="version output not recognized"):
        tb.BeadsAdapter({"prefix": "x"}, runner=runner)


def test_construct_refuses_when_version_nonzero_exit() -> None:
    runner = _FakeRunner([_cp(returncode=1, stderr="bd: db corrupt\n")])
    with pytest.raises(t.TrackerConfigError, match="version check failed"):
        tb.BeadsAdapter({"prefix": "x"}, runner=runner)


# ─── Capabilities ────────────────────────────────────────────────────────────


def test_capabilities_advertise_14_closed_enum_entries() -> None:
    adapter, _ = _build_adapter([])
    names = [c["name"] for c in adapter.capabilities]
    assert len(names) == 14
    assert set(names) == {
        "comments_adf",
        "comments_markdown",
        "attachments",
        "watchers",
        "sprints",
        "fix_versions",
        "components",
        "epic_link",
        "pr_links",
        "ci_links",
        "boards",
        "custom_fields",
        "transitions_with_validators",
        "resolutions",
    }


def test_only_comments_markdown_and_resolutions_supported() -> None:
    adapter, _ = _build_adapter([])
    by_name = {c["name"]: c["supported"] for c in adapter.capabilities}
    assert by_name["comments_markdown"] is True
    assert by_name["resolutions"] is True
    assert by_name["comments_adf"] is False
    assert by_name["attachments"] is False
    assert by_name["sprints"] is False
    assert by_name["pr_links"] is False


# ─── Marshalling ─────────────────────────────────────────────────────────────


def _issue_json(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "bd-a1b2",
        "title": "Add cooldown to skill X",
        "description": "long body...",
        "status": "open",
        "issue_type": "task",
        "priority": 2,
        "assignee": "alice",
        "labels": ["pri:high"],
        "parent": None,
        "dependencies": [],
        "comments": [],
        "created_at": "2026-05-01T00:00:00Z",
        "updated_at": "2026-05-02T00:00:00Z",
        "closed_at": None,
    }
    base.update(overrides)
    return base


def test_get_returns_full_ticket_shape() -> None:
    issue = _issue_json(
        comments=[
            {"id": "c1", "author": "bob", "body": "ack", "created_at": "2026-05-01T01:00:00Z"},
        ],
        dependencies=[{"type": "blocks", "target": "bd-9999"}],
    )
    adapter, _ = _build_adapter([_cp(stdout=json.dumps(issue))])
    ticket = adapter.get("bd-a1b2")
    assert ticket["key"] == "bd-a1b2"
    assert ticket["summary"] == "Add cooldown to skill X"
    assert ticket["status"] == "open"
    assert ticket["type"] == "task"
    assert ticket["priority"] == "P2"
    assert ticket["assignee"] == "alice"
    assert len(ticket["comments"]) == 1
    assert ticket["comments"][0]["author"] == "bob"
    assert ticket["comments"][0]["body"]["fmt"] == "md"
    assert ticket["attachments"] == []
    assert len(ticket["links"]) == 1
    assert ticket["links"][0]["kind"] == "blocks"
    assert ticket["links"][0]["to_key"] == "bd-9999"


def test_get_maps_epic_issue_type() -> None:
    # bd emits the issue type under `issue_type`, not `type` (which carries the dep
    # kind); the epic bootstrap guard (flow-jvxj) reads Ticket["type"], so the
    # adapter must surface `issue_type`. Regression: it previously read `raw["type"]`
    # and reported every bead as "task".
    issue = _issue_json(issue_type="epic")
    adapter, _ = _build_adapter([_cp(stdout=json.dumps(issue))])
    assert adapter.get("bd-a1b2")["type"] == "epic"


def test_get_passes_include_comments_flag() -> None:
    adapter, runner = _build_adapter([_cp(stdout=json.dumps(_issue_json()))])
    adapter.get("bd-a1b2")
    show_args = runner.calls[0][0]
    assert "show" in show_args
    assert "--include-comments" in show_args


def test_get_marshals_text_keyed_comment() -> None:
    issue = _issue_json(
        comments=[
            {"id": "c1", "author": "bob", "text": "ack", "created_at": "2026-05-01T01:00:00Z"},
        ],
    )
    adapter, _ = _build_adapter([_cp(stdout=json.dumps(issue))])
    ticket = adapter.get("bd-a1b2")
    assert ticket["comments"][0]["body"]["body"] == "ack"


def test_get_falls_back_to_body_keyed_comment() -> None:
    issue = _issue_json(
        comments=[
            {"id": "c1", "author": "bob", "body": "ack", "created_at": "2026-05-01T01:00:00Z"},
        ],
    )
    adapter, _ = _build_adapter([_cp(stdout=json.dumps(issue))])
    ticket = adapter.get("bd-a1b2")
    assert ticket["comments"][0]["body"]["body"] == "ack"


def test_get_raises_on_non_object_response() -> None:
    adapter, _ = _build_adapter([_cp(stdout='["not an object"]')])
    with pytest.raises(t.TrackerError, match="non-object"):
        adapter.get("bd-a1b2")


def test_get_propagates_bd_error_as_tracker_error() -> None:
    adapter, _ = _build_adapter([_cp(returncode=1, stderr="Error: issue not found\n")])
    with pytest.raises(t.TrackerError, match="bd command failed"):
        adapter.get("bd-ghost")


# ─── bd >=1.0 `show --json` returns a single-element list, not a bare object ───
# Regression for the never-run-in-production beads path: the five `show` callers
# (get / state / _verify_field strict; list_linked / is_shipped graceful) must
# unwrap `[ {issue} ]`. Pre-fix these failed against live bd 1.0.4.


def test_get_unwraps_single_element_list_response() -> None:
    issue = _issue_json(dependencies=[{"type": "blocks", "target": "bd-9999"}])
    adapter, _ = _build_adapter([_cp(stdout=json.dumps([issue]))])
    ticket = adapter.get("bd-a1b2")
    assert ticket["key"] == "bd-a1b2"
    assert ticket["summary"] == "Add cooldown to skill X"
    assert ticket["links"][0]["to_key"] == "bd-9999"


def test_state_unwraps_single_element_list_response() -> None:
    adapter, _ = _build_adapter([_cp(stdout=json.dumps([_issue_json(status="open")]))])
    state = adapter.state("bd-a1b2")
    assert state["native_status"] == "open"


def test_is_shipped_unwraps_single_element_list_response() -> None:
    adapter, _ = _build_adapter(
        [
            _cp(stdout=json.dumps([_issue_json(status="closed", close_reason="fixed")])),
            _cp(stdout="origin/main\n"),  # git symbolic-ref
            _cp(),  # git fetch
            _cp(stdout="abc123def\x00ticket: bd-a1b2\x1e"),  # git log result
        ]
    )
    result = adapter.is_shipped("bd-a1b2")
    assert result["state"] == "not_yet_observed"
    evidence = result["evidence"]
    assert evidence is not None
    assert evidence["commit_sha"] == "abc123def"


def test_set_summary_postcondition_accepts_list_wrapped_show() -> None:
    adapter, _ = _build_adapter(
        [_cp(stdout=""), _cp(stdout=json.dumps([_issue_json(title="new title")]))]
    )
    adapter.set_summary("bd-a1b2", {"body": "new title", "fmt": "plain"})


def test_list_linked_unwraps_list_wrapped_target() -> None:
    deps_payload = [{"type": "blocks", "target": "bd-2"}]
    adapter, _ = _build_adapter(
        [_cp(stdout=json.dumps(deps_payload)), _cp(stdout=json.dumps([_issue_json(id="bd-2")]))]
    )
    refs = adapter.list_linked("bd-1")
    assert len(refs) == 1
    assert refs[0]["key"] == "bd-2"


def test_list_assigned_emits_assignee_filter() -> None:
    issues = [_issue_json(id="bd-1"), _issue_json(id="bd-2", priority=0)]
    adapter, runner = _build_adapter([_cp(stdout=json.dumps(issues))])
    refs = adapter.list_assigned("open")
    assert len(refs) == 2
    assert refs[0]["key"] == "bd-1"
    assert refs[1]["priority"] == "P0"
    args = runner.calls[-1][0]
    assert "--assignee" in args
    assert "alice" in args
    assert "--status" in args


def test_list_linked_fetches_each_dependency_target() -> None:
    deps_payload = [{"type": "blocks", "target": "bd-2"}]
    target_payload = _issue_json(id="bd-2")
    adapter, _ = _build_adapter(
        [_cp(stdout=json.dumps(deps_payload)), _cp(stdout=json.dumps(target_payload))]
    )
    refs = adapter.list_linked("bd-1")
    assert len(refs) == 1
    assert refs[0]["key"] == "bd-2"


def test_list_linked_skips_dangling_references() -> None:
    deps_payload = [
        {"type": "blocks", "target": "bd-2"},
        {"type": "blocks", "target": "bd-ghost"},
    ]
    adapter, _ = _build_adapter(
        [
            _cp(stdout=json.dumps(deps_payload)),
            _cp(stdout=json.dumps(_issue_json(id="bd-2"))),
            _cp(returncode=1, stderr="Error: issue not found\n"),
        ]
    )
    refs = adapter.list_linked("bd-1")
    assert len(refs) == 1
    assert refs[0]["key"] == "bd-2"


# ─── list_transitions ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("native", "expected_targets"),
    [
        ("open", ["in_progress", "blocked", "closed"]),
        ("in_progress", ["open", "blocked", "closed"]),
        ("blocked", ["open", "in_progress", "closed"]),
        ("deferred", ["open", "closed"]),
        ("closed", ["open"]),
    ],
)
def test_list_transitions_synthesizes_from_current_state(
    native: str, expected_targets: list[str]
) -> None:
    issue = _issue_json(status=native)
    adapter, _ = _build_adapter([_cp(stdout=json.dumps(issue))])
    transitions = adapter.list_transitions("bd-a1b2")
    targets = [tr["to_state"] for tr in transitions]
    assert targets == expected_targets
    for tr in transitions:
        assert tr["id"].startswith("bd:to:")
        assert tr["available"] is True


# ─── Create + setters ────────────────────────────────────────────────────────


def test_create_returns_new_id() -> None:
    new_issue = _issue_json(id="bd-new1")
    adapter, runner = _build_adapter([_cp(stdout=json.dumps(new_issue))])
    new_id = adapter.create(
        summary={"body": "title", "fmt": "plain"},
        description={"body": "desc", "fmt": "md"},
        type="task",
        labels=["a", "b"],
    )
    assert new_id == "bd-new1"
    args = runner.calls[-1][0]
    assert "--title" in args
    assert "title" in args
    assert "--labels" in args
    assert "a,b" in args


def test_create_missing_top_level_id_raises_without_second_create() -> None:
    # Response parses but lacks a top-level id (nested shape). create() must
    # raise rather than re-running `bd create` (which would duplicate the ticket).
    nested = {"issue": {"id": "bd-x"}}
    adapter, runner = _build_adapter([_cp(stdout=json.dumps(nested))])
    with pytest.raises(t.TrackerError, match="did not return a top-level id"):
        adapter.create(
            summary={"body": "title", "fmt": "plain"},
            description={"body": "desc", "fmt": "md"},
            type="task",
        )
    create_calls = [c for c in runner.calls if c[0][:2] == ["bd", "create"]]
    assert len(create_calls) == 1


def test_create_rejects_adf_description() -> None:
    adapter, _ = _build_adapter([])
    with pytest.raises(t.NotSupported, match="ADF"):
        adapter.create(
            summary={"body": "title", "fmt": "plain"},
            description={"body": "{}", "fmt": "adf"},
            type="task",
        )


def test_set_summary_re_reads_for_postcondition() -> None:
    issue_after = _issue_json(title="new title")
    adapter, runner = _build_adapter([_cp(stdout=""), _cp(stdout=json.dumps(issue_after))])
    adapter.set_summary("bd-a1b2", {"body": "new title", "fmt": "plain"})
    # Two calls: update + show.
    assert runner.calls[0][0][:2] == ["bd", "update"]
    assert runner.calls[1][0][:2] == ["bd", "show"]


def test_set_summary_postcondition_failure_raises() -> None:
    issue_after = _issue_json(title="DIFFERENT")
    adapter, _ = _build_adapter([_cp(stdout=""), _cp(stdout=json.dumps(issue_after))])
    with pytest.raises(t.TrackerError, match="postcondition"):
        adapter.set_summary("bd-a1b2", {"body": "new title", "fmt": "plain"})


def test_set_priority_maps_string_to_bd_int() -> None:
    issue_after = _issue_json(priority=1)
    adapter, runner = _build_adapter([_cp(stdout=""), _cp(stdout=json.dumps(issue_after))])
    adapter.set_priority("bd-a1b2", "P1")
    args = runner.calls[0][0]
    assert args == ["bd", "priority", "bd-a1b2", "1"]


def test_set_priority_rejects_unknown_label() -> None:
    adapter, _ = _build_adapter([])
    with pytest.raises(t.TrackerError, match="cannot map priority"):
        adapter.set_priority("bd-a1b2", "URGENT")


def test_set_labels_replaces_full_list_via_update() -> None:
    issue_after = _issue_json(labels=["x", "y"])
    adapter, runner = _build_adapter([_cp(stdout=""), _cp(stdout=json.dumps(issue_after))])
    adapter.set_labels("bd-a1b2", ["x", "y"])
    args = runner.calls[0][0]
    assert args[:3] == ["bd", "update", "bd-a1b2"]
    assert "--set-labels" in args
    assert "x,y" in args


def test_set_description_re_reads_for_postcondition() -> None:
    issue_after = _issue_json(description="new body")
    adapter, runner = _build_adapter([_cp(stdout=""), _cp(stdout=json.dumps(issue_after))])
    adapter.set_description("bd-a1b2", {"body": "new body", "fmt": "plain"})
    # Two calls: update + show.
    assert runner.calls[0][0][:2] == ["bd", "update"]
    assert runner.calls[1][0][:2] == ["bd", "show"]
    args = runner.calls[0][0]
    assert "--description" in args
    assert "new body" in args


def test_set_description_postcondition_failure_raises() -> None:
    issue_after = _issue_json(description="DIFFERENT")
    adapter, _ = _build_adapter([_cp(stdout=""), _cp(stdout=json.dumps(issue_after))])
    with pytest.raises(t.TrackerError, match="postcondition"):
        adapter.set_description("bd-a1b2", {"body": "new body", "fmt": "plain"})


def test_set_description_rejects_adf() -> None:
    adapter, _ = _build_adapter([])
    with pytest.raises(t.NotSupported, match="ADF"):
        adapter.set_description("bd-a1b2", {"body": "{}", "fmt": "adf"})


def test_set_assignee_passes_account_id_verbatim() -> None:
    issue_after = _issue_json(assignee="charlie")
    adapter, runner = _build_adapter([_cp(stdout=""), _cp(stdout=json.dumps(issue_after))])
    adapter.set_assignee("bd-a1b2", "charlie")
    args = runner.calls[0][0]
    assert "--assignee" in args
    assert "charlie" in args


def test_set_assignee_none_unassigns() -> None:
    issue_after = _issue_json(assignee=None)
    adapter, _ = _build_adapter([_cp(stdout=""), _cp(stdout=json.dumps(issue_after))])
    adapter.set_assignee("bd-a1b2", None)


# ─── Transition ──────────────────────────────────────────────────────────────


def test_transition_to_closed_calls_bd_close_and_verifies() -> None:
    adapter, runner = _build_adapter(
        [_cp(stdout=""), _cp(stdout=json.dumps(_issue_json(status="closed")))]
    )
    result = adapter.transition("bd-a1b2", "bd:to:closed")
    assert result["success"] is True
    assert result["new_state"] is not None
    assert result["new_state"]["normalized"] == "done"
    assert runner.calls[0][0][:3] == ["bd", "close", "bd-a1b2"]


def test_transition_to_open_from_closed_uses_reopen() -> None:
    # state() pre-check, then reopen, then state() post-check.
    adapter, runner = _build_adapter(
        [
            _cp(stdout=json.dumps(_issue_json(status="closed"))),  # cur state
            _cp(stdout=""),  # reopen
            _cp(stdout=json.dumps(_issue_json(status="open"))),  # postcond
        ]
    )
    result = adapter.transition("bd-a1b2", "bd:to:open")
    assert result["success"] is True
    assert runner.calls[1][0][:3] == ["bd", "reopen", "bd-a1b2"]


def test_transition_to_in_progress_uses_update_status() -> None:
    adapter, runner = _build_adapter(
        [_cp(stdout=""), _cp(stdout=json.dumps(_issue_json(status="in_progress")))]
    )
    result = adapter.transition("bd-a1b2", "bd:to:in_progress")
    assert result["success"] is True
    args = runner.calls[0][0]
    assert args[:3] == ["bd", "update", "bd-a1b2"]
    assert "--status" in args
    assert "in_progress" in args


def test_transition_id_in_wrong_format_returns_failure() -> None:
    adapter, _ = _build_adapter([])
    result = adapter.transition("bd-a1b2", "12345")
    assert result["success"] is False
    assert result["failure_kind"] == "ambiguous_transition"


def test_transition_bd_failure_is_classified() -> None:
    adapter, _ = _build_adapter([_cp(returncode=1, stderr="Error: issue not found\n")])
    result = adapter.transition("bd-ghost", "bd:to:closed")
    assert result["success"] is False
    assert result["failure_kind"] == "wrong_source_state"


def test_transition_postcondition_mismatch_returns_failure() -> None:
    # bd reports success but the post-read shows status didn't change.
    adapter, _ = _build_adapter(
        [_cp(stdout=""), _cp(stdout=json.dumps(_issue_json(status="open")))]
    )
    result = adapter.transition("bd-a1b2", "bd:to:closed")
    assert result["success"] is False
    assert result["failure_kind"] == "validator_failed"
    assert result["new_state"] is not None


# ─── Comment + link ──────────────────────────────────────────────────────────


def test_comment_passes_markdown_via_stdin() -> None:
    adapter, runner = _build_adapter([_cp(stdout="")])
    adapter.comment("bd-a1b2", {"body": "## hello", "fmt": "md"})
    args, kwargs = runner.calls[-1]
    assert args == ["bd", "comment", "bd-a1b2", "--stdin"]
    assert kwargs.get("input") == "## hello"


def test_comment_rejects_adf() -> None:
    adapter, _ = _build_adapter([])
    with pytest.raises(t.NotSupported):
        adapter.comment("bd-a1b2", {"body": "{}", "fmt": "adf"})


def test_link_uses_bd_dep_add() -> None:
    adapter, runner = _build_adapter([_cp(stdout="")])
    adapter.link("bd-1", "bd-2", "blocks")
    args = runner.calls[-1][0]
    assert args[:4] == ["bd", "dep", "add", "bd-1"]
    assert "bd-2" in args
    assert "--type" in args
    assert "blocks" in args


# ─── State + project_requires_pr ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("native", "expected_normalized"),
    [
        ("open", "open"),
        ("in_progress", "in_progress"),
        ("blocked", "blocked"),
        ("deferred", "cancelled"),
        ("closed", "done"),
        ("In Progress", "in_progress"),  # whitespace + case
    ],
)
def test_state_normalization_table(native: str, expected_normalized: str) -> None:
    adapter, _ = _build_adapter([_cp(stdout=json.dumps(_issue_json(status=native)))])
    state = adapter.state("bd-a1b2")
    assert state["normalized"] == expected_normalized
    assert state["native_status"] == native
    assert "adapter_mapping_diagnostic" in state


def test_state_unknown_native_falls_back_to_open() -> None:
    adapter, _ = _build_adapter([_cp(stdout=json.dumps(_issue_json(status="weirdo")))])
    state = adapter.state("bd-a1b2")
    assert state["normalized"] == "open"
    assert "unknown" in state["adapter_mapping_diagnostic"]


def test_project_requires_pr_always_false() -> None:
    adapter, _ = _build_adapter([])
    assert adapter.project_requires_pr() is False


# ─── is_shipped ──────────────────────────────────────────────────────────────
#
# is_shipped gates `not_yet_observed` on the bead key appearing in a commit on
# the DEFAULT branch (the squash-merge), not anywhere reachable from HEAD. The
# closed-path runner sequence is: bd show, symbolic-ref (default ref), fetch,
# git log. Helpers below stand in for the two git calls between show and log.


def _symref_ok(ref: str = "origin/main") -> subprocess.CompletedProcess[str]:
    """`git symbolic-ref --short refs/remotes/origin/HEAD` → e.g. origin/main."""
    return _cp(stdout=f"{ref}\n")


def _git_log_record(sha: str, message: str) -> subprocess.CompletedProcess[str]:
    """One record of `git log --format=%H%x00%B%x1e` output."""
    return _cp(stdout=f"{sha}\x00{message}\x1e")


def test_is_shipped_not_shipped_when_not_closed() -> None:
    adapter, _ = _build_adapter([_cp(stdout=json.dumps(_issue_json(status="open")))])
    result = adapter.is_shipped("bd-a1b2")
    assert result["state"] == "not_shipped"
    assert result["evidence"] is None
    assert result["source"] == "none"


def test_is_shipped_not_yet_observed_when_closed_with_commit() -> None:
    adapter, _ = _build_adapter(
        [
            _cp(
                stdout=json.dumps(
                    _issue_json(
                        status="closed",
                        closed_at="2026-05-15T00:00:00Z",
                        close_reason="fixed",
                    )
                )
            ),
            _symref_ok(),
            _cp(),  # git fetch
            _git_log_record("abc123def", "feat: a thing (#9)\n\nticket: bd-a1b2"),
        ]
    )
    result = adapter.is_shipped("bd-a1b2")
    assert result["state"] == "not_yet_observed"
    assert result["source"] == "live_backend_query"
    assert result["evidence"] is not None
    assert result["evidence"]["commit_sha"] == "abc123def"
    assert result["evidence"]["closure_reason"] == "fixed"


def test_is_shipped_indeterminate_when_closed_without_commit() -> None:
    adapter, _ = _build_adapter(
        [
            _cp(stdout=json.dumps(_issue_json(status="closed"))),
            _symref_ok(),
            _cp(),  # git fetch
            _cp(stdout=""),  # no commit on the default branch
        ]
    )
    result = adapter.is_shipped("bd-a1b2")
    assert result["state"] == "indeterminate"
    assert result["evidence"] is not None
    assert result["evidence"]["commit_sha"] is None


def test_is_shipped_indeterminate_when_closed_unmerged() -> None:
    # flow-qmtd regression: a bead closed administratively-unmerged (its work
    # commit sits only on a feature branch, absent from the default branch) must
    # NOT read as shipped. The git-log probe of the default ref finds nothing.
    adapter, _ = _build_adapter(
        [
            _cp(
                stdout=json.dumps(
                    _issue_json(
                        status="closed",
                        closed_at="2026-06-11T00:00:00Z",
                        close_reason="wave-2: lost tiebreak. PR #259 closed unmerged.",
                    )
                )
            ),
            _symref_ok(),
            _cp(),  # git fetch
            _cp(stdout=""),  # key not present on the default branch
        ]
    )
    result = adapter.is_shipped("bd-a1b2")
    assert result["state"] == "indeterminate"
    assert result["source"] == "none"
    assert result["evidence"] is not None
    assert result["evidence"]["commit_sha"] is None
    assert result["evidence"]["closure_reason"] == "wave-2: lost tiebreak. PR #259 closed unmerged."


def test_is_shipped_grep_targets_default_ref() -> None:
    adapter, runner = _build_adapter(
        [
            _cp(stdout=json.dumps(_issue_json(status="closed", close_reason="fixed"))),
            _symref_ok("origin/main"),
            _cp(),  # git fetch
            _git_log_record("abc123def", "ticket: bd-a1b2"),
        ]
    )
    adapter.is_shipped("bd-a1b2")
    log_calls = [args for args, _ in runner.calls if args[:2] == ["git", "log"]]
    assert len(log_calls) == 1
    # `git log <ref> --grep=...`: the ref is the default branch, not HEAD.
    assert log_calls[0][2] == "origin/main"


def test_is_shipped_reads_close_reason_field() -> None:
    # bd's JSON carries `close_reason`; the old `closure_reason` read was a
    # wrong-key bug that always yielded null.
    adapter, _ = _build_adapter(
        [
            _cp(stdout=json.dumps(_issue_json(status="closed", close_reason="real"))),
            _symref_ok(),
            _cp(),
            _git_log_record("abc123def", "ticket: bd-a1b2"),
        ]
    )
    result = adapter.is_shipped("bd-a1b2")
    assert result["evidence"] is not None
    assert result["evidence"]["closure_reason"] == "real"


def test_is_shipped_close_reason_falls_back_to_legacy_key() -> None:
    adapter, _ = _build_adapter(
        [
            _cp(stdout=json.dumps(_issue_json(status="closed", closure_reason="legacy"))),
            _symref_ok(),
            _cp(),
            _git_log_record("abc123def", "ticket: bd-a1b2"),
        ]
    )
    result = adapter.is_shipped("bd-a1b2")
    assert result["evidence"] is not None
    assert result["evidence"]["closure_reason"] == "legacy"


def test_is_shipped_parent_key_not_matched_by_child_commit() -> None:
    # `git log --grep=flow-a1ti` loosely matches a child's commit (flow-a1ti.2);
    # the word-boundary re-check rejects it, so the parent reads indeterminate.
    adapter, _ = _build_adapter(
        [
            _cp(stdout=json.dumps(_issue_json(status="closed"))),
            _symref_ok(),
            _cp(),
            _git_log_record("deadbeef", "feat: child work\n\nticket: flow-a1ti.2"),
        ]
    )
    result = adapter.is_shipped("flow-a1ti")
    assert result["state"] == "indeterminate"
    assert result["evidence"] is not None
    assert result["evidence"]["commit_sha"] is None


def test_is_shipped_default_ref_falls_back_when_symref_fails() -> None:
    adapter, runner = _build_adapter(
        [
            _cp(stdout=json.dumps(_issue_json(status="closed", close_reason="fixed"))),
            _cp(returncode=1, stderr="fatal: ref refs/remotes/origin/HEAD is not a symbolic ref"),
            _cp(stdout="0123456789abcdef\n"),  # rev-parse --verify origin/main
            _cp(),  # git fetch
            _git_log_record("abc123def", "ticket: bd-a1b2"),
        ]
    )
    result = adapter.is_shipped("bd-a1b2")
    assert result["state"] == "not_yet_observed"
    log_calls = [args for args, _ in runner.calls if args[:2] == ["git", "log"]]
    assert log_calls[0][2] == "origin/main"


def test_is_shipped_handles_bd_show_failure() -> None:
    adapter, _ = _build_adapter([_cp(returncode=1, stderr="Error: issue not found\n")])
    result = adapter.is_shipped("bd-ghost")
    assert result["state"] == "not_shipped"


# ─── Capability-gated NotSupported ───────────────────────────────────────────


def test_capability_gated_methods_raise_not_supported() -> None:
    adapter, _ = _build_adapter([])
    with pytest.raises(t.NotSupported):
        adapter.set_sprint("bd-1", "sprint-1")
    with pytest.raises(t.NotSupported):
        adapter.list_sprints("proj")
    with pytest.raises(t.NotSupported):
        adapter.add_watcher("bd-1", "alice")
    with pytest.raises(t.NotSupported):
        adapter.set_fix_versions("bd-1", ["v1"])
    with pytest.raises(t.NotSupported):
        adapter.set_components("bd-1", ["core"])
    with pytest.raises(t.NotSupported):
        adapter.set_epic_link("bd-1", "bd-epic")
    with pytest.raises(t.NotSupported):
        adapter.board_rank("bd-1", None)
    with pytest.raises(t.NotSupported):
        adapter.set_custom_field("bd-1", "x", "y", {"key": "x", "type": "string"})
    with pytest.raises(t.NotSupported):
        adapter.get_attachments("bd-1")
    with pytest.raises(t.NotSupported):
        adapter.upload_attachment("bd-1", "/tmp/x.png")


# ─── list_issue_types / list_epics degradation ───────────────────────────────


def test_list_issue_types_returns_bd_types() -> None:
    adapter, runner = _build_adapter([])
    types = adapter.list_issue_types()
    names = [t["name"] for t in types]
    assert names == ["task", "bug", "feature", "epic", "chore", "decision"]
    # epic is the only hierarchy-1 type; the rest are 0.
    by_name = {t["name"]: t["hierarchyLevel"] for t in types}
    assert by_name["epic"] == 1
    assert by_name["task"] == 0
    # Static list; no bd call.
    assert runner.calls == []


def test_list_epics_returns_empty() -> None:
    adapter, runner = _build_adapter([])
    assert adapter.list_epics() == []
    assert runner.calls == []


# ─── Protocol conformance ───────────────────────────────────────────────────


def test_beads_adapter_is_structural_tracker() -> None:
    adapter, _ = _build_adapter([])
    assert isinstance(adapter, t.Tracker)
