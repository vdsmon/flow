"""Contract tests for tracker.py (factory dispatch + Protocol conformance).

Phase 2 deliverable. Adapter modules are stubs; this test asserts the factory
correctly hands construction off to them and surfaces stub failures as
`NotImplementedError` (not `ImportError`, not silent success).

Also asserts the Protocol's structural compatibility against a hand-rolled fake
adapter so the `@runtime_checkable` Tracker actually matches conforming objects.
"""

from __future__ import annotations

from typing import Any

import pytest

import tracker as t

# ─── Factory dispatch ───────────────────────────────────────────────────────


def test_make_tracker_rejects_missing_backend() -> None:
    with pytest.raises(t.TrackerConfigError, match=r"tracker\.backend missing"):
        t.make_tracker({})


def test_make_tracker_rejects_unknown_backend() -> None:
    with pytest.raises(t.TrackerConfigError, match="not recognized"):
        t.make_tracker({"backend": "github-projects"})


def test_make_tracker_rejects_none_backend() -> None:
    with pytest.raises(t.TrackerConfigError):
        t.make_tracker({"backend": None})


def test_make_tracker_jira_constructs_with_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATLASSIAN_EMAIL", "you@example.com")
    monkeypatch.setenv("ATLASSIAN_API_TOKEN", "fake-token")
    adapter = t.make_tracker({"backend": "jira", "cloud_id": "x", "project_key": "FT"})
    assert adapter.backend == "jira"


def test_make_tracker_jira_without_creds_raises_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ATLASSIAN_EMAIL", raising=False)
    monkeypatch.delenv("ATLASSIAN_API_TOKEN", raising=False)
    with pytest.raises(t.TrackerConfigError, match="ATLASSIAN_EMAIL"):
        t.make_tracker({"backend": "jira", "cloud_id": "x", "project_key": "FT"})


def test_make_tracker_beads_constructs_with_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    # Phase 6: the factory should pass through to BeadsAdapter, which preflights `bd --version`.
    # With no live bd available it would raise, so the default runner is replaced with one that
    # reports a recent version.
    import subprocess

    import tracker_beads as tb

    def fake_runner(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del args, kwargs
        return subprocess.CompletedProcess(
            args=[], returncode=0, stdout="bd version 1.0.4 (test)\n", stderr=""
        )

    monkeypatch.setattr(tb, "_default_runner", lambda: fake_runner)
    adapter = t.make_tracker({"backend": "beads", "prefix": "safemic"})
    assert adapter.backend == "beads"


def test_make_tracker_beads_refuses_when_bd_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    import tracker_beads as tb

    def fake_runner(args: list[str], **kwargs: Any) -> Any:
        del args, kwargs
        raise FileNotFoundError("bd not on PATH")

    monkeypatch.setattr(tb, "_default_runner", lambda: fake_runner)
    with pytest.raises(t.TrackerConfigError, match="bd CLI not found"):
        t.make_tracker({"backend": "beads", "prefix": "safemic"})


def test_known_backends_enum_matches_factory_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    # If KNOWN_BACKENDS grows, the factory MUST grow with it. This test catches
    # a future drift where a new backend is added to the enum but not wired in.
    # Stub credentials + runners so the live preflight doesn't escape the test.
    monkeypatch.setenv("ATLASSIAN_EMAIL", "x@x")
    monkeypatch.setenv("ATLASSIAN_API_TOKEN", "fake")

    import subprocess

    import tracker_beads as tb

    def fake_runner(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del args, kwargs
        return subprocess.CompletedProcess(
            args=[], returncode=0, stdout="bd version 1.0.4 (test)\n", stderr=""
        )

    monkeypatch.setattr(tb, "_default_runner", lambda: fake_runner)
    for backend in t.KNOWN_BACKENDS:
        adapter = t.make_tracker(
            {"backend": backend, "cloud_id": "x", "project_key": "FT", "prefix": "p"}
        )
        assert adapter.backend == backend


# ─── Exception hierarchy ─────────────────────────────────────────────────────


def test_not_supported_is_tracker_error() -> None:
    assert issubclass(t.NotSupported, t.TrackerError)


def test_tracker_config_error_is_tracker_error() -> None:
    assert issubclass(t.TrackerConfigError, t.TrackerError)


def test_tracker_error_is_exception() -> None:
    assert issubclass(t.TrackerError, Exception)


# ─── Protocol structural conformance ─────────────────────────────────────────


class _FakeAdapter:
    """Minimal Tracker conformant for structural Protocol matching."""

    backend = "fake"

    def get(self, key: str) -> t.Ticket:  # pragma: no cover - structural
        raise NotImplementedError

    def list_assigned(self, filter: str = "open") -> list[t.TicketRef]:  # pragma: no cover
        raise NotImplementedError

    def list_linked(self, key: str) -> list[t.TicketRef]:  # pragma: no cover
        raise NotImplementedError

    def list_transitions(self, key: str) -> list[t.Transition]:  # pragma: no cover
        raise NotImplementedError

    def create(
        self,
        summary: t.Content,
        description: t.Content,
        type: str,
        parent: str | None = None,
        labels: list[str] | None = None,
        assignee: str | None = None,
    ) -> str:  # pragma: no cover
        raise NotImplementedError

    def set_summary(self, key: str, summary: t.Content) -> None:  # pragma: no cover
        raise NotImplementedError

    def set_description(self, key: str, description: t.Content) -> None:  # pragma: no cover
        raise NotImplementedError

    def set_priority(self, key: str, priority: str) -> None:  # pragma: no cover
        raise NotImplementedError

    def set_labels(self, key: str, labels: list[str]) -> None:  # pragma: no cover
        raise NotImplementedError

    def set_assignee(self, key: str, account_id: str | None) -> None:  # pragma: no cover
        raise NotImplementedError

    def transition(
        self,
        key: str,
        transition_id: str,
        fields: dict[str, Any] | None = None,
    ) -> t.TransitionResult:  # pragma: no cover
        raise NotImplementedError

    def comment(self, key: str, body: t.Content) -> None:  # pragma: no cover
        raise NotImplementedError

    def link(self, from_key: str, to_key: str, kind: str) -> None:  # pragma: no cover
        raise NotImplementedError

    def state(self, key: str) -> t.TicketState:  # pragma: no cover
        raise NotImplementedError

    def project_requires_pr(self) -> bool:  # pragma: no cover
        return False

    def is_shipped(self, key: str) -> t.ShipState:  # pragma: no cover
        raise NotImplementedError

    def set_sprint(self, key: str, sprint_id: str) -> None:  # pragma: no cover
        raise t.NotSupported

    def list_sprints(self, project: str) -> list[t.Sprint]:  # pragma: no cover
        raise t.NotSupported

    def add_watcher(self, key: str, account_id: str) -> None:  # pragma: no cover
        raise t.NotSupported

    def set_fix_versions(self, key: str, versions: list[str]) -> None:  # pragma: no cover
        raise t.NotSupported

    def set_components(self, key: str, components: list[str]) -> None:  # pragma: no cover
        raise t.NotSupported

    def set_epic_link(self, key: str, epic_key: str) -> None:  # pragma: no cover
        raise t.NotSupported

    def board_rank(self, key: str, after_key: str | None) -> None:  # pragma: no cover
        raise t.NotSupported

    def get_attachments(self, key: str) -> list[t.Attachment]:  # pragma: no cover
        raise t.NotSupported

    def upload_attachment(self, key: str, path: str) -> str:  # pragma: no cover
        raise t.NotSupported

    def download_attachment(self, attachment: t.Attachment) -> bytes:  # pragma: no cover
        raise t.NotSupported


def test_fake_adapter_is_structurally_a_tracker() -> None:
    # @runtime_checkable Protocols verify method NAMES, not signatures. The
    # presence of every required attribute is what we assert here.
    adapter = _FakeAdapter()
    assert isinstance(adapter, t.Tracker)


def test_object_missing_methods_is_not_a_tracker() -> None:
    class Partial:
        backend = "partial"

        def get(self, key: str) -> t.Ticket:
            raise NotImplementedError

    assert not isinstance(Partial(), t.Tracker)


# ─── Type roundtrips ─────────────────────────────────────────────────────────


# ─── Public surface ──────────────────────────────────────────────────────────


def test_public_surface_in_dunder_all() -> None:
    expected = {
        "NORMALIZED_STATES",
        "Tracker",
        "make_tracker",
        "Ticket",
        "TicketRef",
        "TicketState",
        "Transition",
        "TransitionResult",
        "ShipState",
        "Content",
        "TrackerError",
        "NotSupported",
        "TrackerConfigError",
        "KNOWN_BACKENDS",
    }
    assert expected.issubset(set(t.__all__))
