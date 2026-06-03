"""BeadsAdapter — `bd` CLI subprocess adapter for the Tracker protocol.

Stdlib-only. Transport is `subprocess.run` by default; tests inject a fake via
the `runner` constructor parameter (same shape as JiraAdapter's `http=`).

Auth: none — `bd` is a local-only tracker; the database lives under the
workspace's `.beads/` dir. Adapter operates on whatever workspace `bd` resolves
from `BEADS_DIR` env or cwd.

Workspace config (`[tracker.beads]` block in `.flow/workspace.toml`):

- `prefix` — repo-derived slug used by `bd init`. Already created by init.py.
- `shared_server` — bool, default True. Adapter doesn't read this; bd does.
- `actor` — optional. Defaults to `$USER`. Used by `list_assigned`.

See `inventory.md` "Beads CLI surface" section for the full subcommand table,
state normalization, transition synthesis, and stderr-to-failure-kind mapping.

TODO (phase 8): transient-failure paths should append to
`.flow/pending-mutations.jsonl` via `pending-mutations.py`. Adapter currently
raises `TrackerError` immediately — the dispatcher's retry loop owns transient
recovery until phase 8 ships.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from tracker import (
    Attachment,
    Capability,
    Comment,
    Content,
    FieldSpec,
    Link,
    NotSupported,
    ShipState,
    Sprint,
    Ticket,
    TicketRef,
    TicketState,
    TrackerConfigError,
    TrackerError,
    Transition,
    TransitionFailureKind,
    TransitionResult,
)

# ─── Module-level constants ──────────────────────────────────────────────────

# Minimum bd CLI version the adapter has been tested against. The preflight
# version parse rejects anything older. Pin via semver-style major.minor.patch.
_BD_VERSION_MIN: tuple[int, int, int] = (1, 0, 0)

# Native status → NORMALIZED_STATES. See inventory.md normalization table.
_BD_TO_NORMALIZED = {
    "open": "open",
    "in_progress": "in_progress",
    "blocked": "blocked",
    "deferred": "cancelled",
    "closed": "done",
}

# Synthesized transitions per current native status. bd has no explicit
# "list transitions" subcommand; the workflow is uniform (any state → any
# other state via `bd update --status` / `bd close` / `bd reopen`).
_BD_TRANSITIONS: dict[str, list[str]] = {
    "open": ["in_progress", "blocked", "closed"],
    "in_progress": ["open", "blocked", "closed"],
    "blocked": ["open", "in_progress", "closed"],
    "deferred": ["open", "closed"],
    "closed": ["open"],
}

# Closed-enum capability advertisement. 14 entries — exactly the
# CAPABILITY_ENUM from tracker.py. Only comments_markdown + resolutions are
# True; beads is local-only and intentionally narrow.
_BEADS_CAPABILITIES: list[Capability] = [
    {"name": "comments_adf", "supported": False, "payload_schema": None},
    {"name": "comments_markdown", "supported": True, "payload_schema": None},
    {"name": "attachments", "supported": False, "payload_schema": None},
    {"name": "watchers", "supported": False, "payload_schema": None},
    {"name": "sprints", "supported": False, "payload_schema": None},
    {"name": "fix_versions", "supported": False, "payload_schema": None},
    {"name": "components", "supported": False, "payload_schema": None},
    {"name": "epic_link", "supported": False, "payload_schema": None},
    {"name": "pr_links", "supported": False, "payload_schema": None},
    {"name": "ci_links", "supported": False, "payload_schema": None},
    {"name": "boards", "supported": False, "payload_schema": None},
    {"name": "custom_fields", "supported": False, "payload_schema": None},
    {"name": "transitions_with_validators", "supported": False, "payload_schema": None},
    {"name": "resolutions", "supported": True, "payload_schema": None},
]

# Priority maps: bd takes 0-4 integer. Protocol uses string. Round-trip
# preserves "P<n>" surface so dashboards stay readable.
_PRIORITY_STR_TO_INT: dict[str, int] = {
    "p0": 0,
    "highest": 0,
    "0": 0,
    "p1": 1,
    "high": 1,
    "1": 1,
    "p2": 2,
    "medium": 2,
    "2": 2,
    "p3": 3,
    "low": 3,
    "3": 3,
    "p4": 4,
    "lowest": 4,
    "4": 4,
}

# Stderr regexes for failure-kind classification (see inventory.md).
_RE_NO_DB = re.compile(r"(?i)no beads database found")
_RE_NOT_FOUND = re.compile(r"(?i)(issue not found|no such issue|unknown id)")
_RE_PERMISSION = re.compile(r"(?i)(permission denied|forbidden|not authorized)")

_BD_VERSION_RE = re.compile(r"bd version (\d+)\.(\d+)\.(\d+)")

Runner = Callable[..., subprocess.CompletedProcess[str]]


# ─── Exceptions ──────────────────────────────────────────────────────────────


class _BeadsError(TrackerError):
    """Internal: carries bd exit code + stderr for upstream classification."""

    def __init__(self, exit_code: int, stderr: str, cmd_args: list[str]) -> None:
        super().__init__(
            f"bd command failed (rc={exit_code}): args={cmd_args!r} stderr={stderr.strip()!r}"
        )
        self.exit_code = exit_code
        self.stderr = stderr
        self.cmd_args = cmd_args


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _default_runner() -> Runner:
    def runner(
        args: list[str],
        *,
        cwd: Path | None = None,
        check: bool = False,
        input: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            args,
            cwd=str(cwd) if cwd else None,
            check=check,
            capture_output=True,
            text=True,
            input=input,
        )

    return runner


def _content_to_markdown(body: Content) -> str:
    """Reject ADF (bd is markdown-only); accept md + plain verbatim."""
    fmt = body["fmt"]
    if fmt == "adf":
        raise NotSupported("BeadsAdapter does not support ADF content. Use fmt='md' or 'plain'.")
    return body["body"]


def _priority_str_to_bd_int(priority: str) -> int:
    key = priority.strip().lower()
    if key in _PRIORITY_STR_TO_INT:
        return _PRIORITY_STR_TO_INT[key]
    raise TrackerError(
        f"BeadsAdapter cannot map priority={priority!r} to bd 0-4 scale; "
        f"accepted: P0|P1|P2|P3|P4 (or highest|high|medium|low|lowest)."
    )


def _priority_bd_int_to_str(value: Any) -> str:
    if isinstance(value, int) and 0 <= value <= 4:
        return f"P{value}"
    if isinstance(value, str) and value.isdigit():
        return f"P{int(value)}"
    return str(value)


def _normalize_state(native_status: str) -> tuple[str, str]:
    key = native_status.strip().lower().replace(" ", "_").replace("-", "_")
    normalized = _BD_TO_NORMALIZED.get(key)
    if normalized is None:
        return (
            "open",
            f"native={native_status!r} unknown -> open (default; check bd statuses)",
        )
    return normalized, f"native={native_status!r} -> {normalized} (direct map)"


def _classify_failure(stderr: str) -> TransitionFailureKind:
    if _RE_NO_DB.search(stderr) or _RE_NOT_FOUND.search(stderr):
        return "wrong_source_state"
    if _RE_PERMISSION.search(stderr):
        return "permission_denied"
    return "validator_failed"


# ─── Adapter ─────────────────────────────────────────────────────────────────


class BeadsAdapter:
    """`bd` CLI adapter. PURE READ for `is_shipped`. Mutations re-read on success."""

    backend = "beads"

    def __init__(
        self,
        config: dict[str, Any],
        runner: Runner | None = None,
    ) -> None:
        self._config: dict[str, Any] = config
        self._runner: Runner = runner or _default_runner()
        self._prefix: str = str(config.get("prefix", "")) or ""

        # Workspace root resolution: bd uses cwd by default; tests inject by
        # passing a `workspace_root` config key. Adapter does not require this
        # to exist for read-only operations against a configured BEADS_DIR.
        ws = config.get("workspace_root")
        self._workspace_root: Path | None = Path(ws).resolve() if ws else None

        self._actor: str = (
            str(config.get("actor")) if config.get("actor") else os.environ.get("USER", "")
        )

        self.capabilities: list[Capability] = list(_BEADS_CAPABILITIES)

        # Preflight: bd --version. Refuses construction if bd missing/too old.
        self._verify_bd_version()

    # ─── Subprocess plumbing ─────────────────────────────────────────────

    def _run(
        self,
        args: list[str],
        *,
        input: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return self._runner(
            ["bd", *args],
            cwd=self._workspace_root,
            check=False,
            input=input,
        )

    def _run_json(self, args: list[str]) -> Any:
        """Run `bd ... --json` and parse stdout. Raises `_BeadsError` on non-zero."""
        cp = self._run([*args, "--json"])
        if cp.returncode != 0:
            raise _BeadsError(cp.returncode, cp.stderr, args)
        if not cp.stdout.strip():
            return None
        try:
            return json.loads(cp.stdout)
        except json.JSONDecodeError as exc:
            raise TrackerError(
                f"bd {args[0]} --json returned non-JSON output: {exc}; stdout={cp.stdout!r}"
            ) from exc

    def _verify_bd_version(self) -> None:
        try:
            cp = self._run(["version"])
        except FileNotFoundError as exc:
            raise TrackerConfigError(
                "bd CLI not found on PATH; install via `brew install beads` "
                "(or equivalent) before initializing a beads-backed workspace."
            ) from exc
        if cp.returncode != 0:
            raise TrackerConfigError(
                f"bd version check failed (rc={cp.returncode}): "
                f"{cp.stderr.strip() or cp.stdout.strip()}"
            )
        match = _BD_VERSION_RE.search(cp.stdout)
        if match is None:
            raise TrackerConfigError(
                f"bd version output not recognized: {cp.stdout!r}; "
                f"expected `bd version X.Y.Z (...)`."
            )
        version = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
        if version < _BD_VERSION_MIN:
            raise TrackerConfigError(
                f"bd version {'.'.join(map(str, version))} is older than "
                f"required minimum {'.'.join(map(str, _BD_VERSION_MIN))}."
            )

    # ─── Marshalling: bd JSON → Protocol shapes ──────────────────────────

    def _comment_from_json(self, raw: dict[str, Any]) -> Comment:
        return {
            "id": str(raw.get("id", "")),
            "author": str(raw.get("author", "")),
            "body": {"body": str(raw.get("body", "")), "fmt": "md"},
            "created_at": str(raw.get("created_at", "")),
        }

    def _link_from_dep(self, key: str, raw: dict[str, Any]) -> Link:
        return {
            "kind": str(raw.get("type", "depends_on")),
            "from_key": key,
            "to_key": str(raw.get("target", "")),
        }

    def _ticket_ref_from_json(self, raw: dict[str, Any]) -> TicketRef:
        return {
            "key": str(raw.get("id", "")),
            "summary": str(raw.get("title", "")),
            "status": str(raw.get("status", "")),
            "priority": _priority_bd_int_to_str(raw.get("priority", "")),
        }

    def _ticket_from_json(self, raw: dict[str, Any]) -> Ticket:
        key = str(raw.get("id", ""))
        comments_raw = raw.get("comments") or []
        deps_raw = raw.get("dependencies") or []
        return {
            "key": key,
            "summary": str(raw.get("title", "")),
            "status": str(raw.get("status", "")),
            "priority": _priority_bd_int_to_str(raw.get("priority", "")),
            "description": str(raw.get("description", "")),
            "type": str(raw.get("type", "task")),
            "assignee": raw.get("assignee") or None,
            "comments": [self._comment_from_json(c) for c in comments_raw if isinstance(c, dict)],
            "parent": raw.get("parent") or None,
            "attachments": [],  # bd has no attachments concept
            "links": [self._link_from_dep(key, d) for d in deps_raw if isinstance(d, dict)],
        }

    def _state_from_issue(self, raw: dict[str, Any]) -> TicketState:
        native = str(raw.get("status", ""))
        normalized, diag = _normalize_state(native)
        closed_at = raw.get("closed_at")
        return {
            "native_status": native,
            "native_status_category": None,  # bd has no category dim like Jira
            "resolution": str(raw.get("closure_reason"))
            if closed_at and raw.get("closure_reason")
            else None,
            "normalized": cast("Any", normalized),
            "adapter_mapping_diagnostic": diag,
        }

    # ─── Lifecycle (mandatory) ───────────────────────────────────────────

    def get(self, key: str) -> Ticket:
        raw = self._run_json(["show", key])
        if not isinstance(raw, dict):
            raise TrackerError(f"bd show {key} --json returned non-object: {raw!r}")
        return self._ticket_from_json(raw)

    def list_assigned(self, filter: str = "open") -> list[TicketRef]:
        args = ["list", "--status", filter] if filter else ["list"]
        if self._actor:
            args.extend(["--assignee", self._actor])
        raw = self._run_json(args)
        items = (
            raw
            if isinstance(raw, list)
            else (raw.get("issues", []) if isinstance(raw, dict) else [])
        )
        return [self._ticket_ref_from_json(i) for i in items if isinstance(i, dict)]

    def list_linked(self, key: str) -> list[TicketRef]:
        raw = self._run_json(["dep", "list", key])
        items = (
            raw
            if isinstance(raw, list)
            else (raw.get("dependencies", []) if isinstance(raw, dict) else [])
        )
        refs: list[TicketRef] = []
        for dep in items:
            if not isinstance(dep, dict):
                continue
            target = str(dep.get("target", "")) or str(dep.get("id", ""))
            if not target:
                continue
            try:
                ref_raw = self._run_json(["show", target])
                if isinstance(ref_raw, dict):
                    refs.append(self._ticket_ref_from_json(ref_raw))
            except _BeadsError:
                # Dangling reference; skip rather than fail the whole listing.
                continue
        return refs

    def list_transitions(self, key: str) -> list[Transition]:
        cur = self.state(key)["native_status"].lower().replace(" ", "_").replace("-", "_")
        targets = _BD_TRANSITIONS.get(cur, [])
        return [
            {
                "id": f"bd:to:{t}",
                "name": f"to {t}",
                "to_state": t,
                "to_normalized_state": cast("Any", _BD_TO_NORMALIZED.get(t, "open")),
                "required_fields": [],
                "available": True,
                "unavailable_reason": None,
            }
            for t in targets
        ]

    def create(
        self,
        summary: Content,
        description: Content,
        type: str,
        parent: str | None = None,
        labels: list[str] | None = None,
        assignee: str | None = None,
    ) -> str:
        args = [
            "create",
            "--title",
            summary["body"],
            "--description",
            _content_to_markdown(description),
            "--type",
            type,
        ]
        if parent:
            args.extend(["--parent", parent])
        if labels:
            args.extend(["--labels", ",".join(labels)])
        if assignee:
            args.extend(["--assignee", assignee])
        raw = self._run_json(args)
        # bd create --json returns either {"id": "bd-..."} or the full issue;
        # both carry a top-level id. Never re-run create on a parse miss: a
        # second `bd create` would mint a duplicate ticket.
        if isinstance(raw, dict):
            new_id = str(raw.get("id", ""))
            if new_id:
                return new_id
        raise TrackerError(f"bd create did not return a top-level id; raw={raw!r}")

    def set_summary(self, key: str, summary: Content) -> None:
        cp = self._run(["update", key, "--title", summary["body"]])
        if cp.returncode != 0:
            raise _BeadsError(cp.returncode, cp.stderr, ["update", key, "--title"])
        self._verify_field(key, "title", summary["body"])

    def set_description(self, key: str, description: Content) -> None:
        body = _content_to_markdown(description)
        cp = self._run(["update", key, "--description", body])
        if cp.returncode != 0:
            raise _BeadsError(cp.returncode, cp.stderr, ["update", key, "--description"])
        self._verify_field(key, "description", body)

    def set_priority(self, key: str, priority: str) -> None:
        n = _priority_str_to_bd_int(priority)
        cp = self._run(["priority", key, str(n)])
        if cp.returncode != 0:
            raise _BeadsError(cp.returncode, cp.stderr, ["priority", key, str(n)])
        self._verify_field(key, "priority", n)

    def set_labels(self, key: str, labels: list[str]) -> None:
        # bd update has --set-labels (replace all), --add-label (append),
        # --remove-label (subtract). Protocol's "set" semantics need replacement.
        joined = ",".join(labels)
        cp = self._run(["update", key, "--set-labels", joined])
        if cp.returncode != 0:
            raise _BeadsError(cp.returncode, cp.stderr, ["update", key, "--set-labels"])
        self._verify_field(key, "labels", labels)

    def set_assignee(self, key: str, account_id: str | None) -> None:
        # bd uses actor-name strings; account_id is passed through verbatim.
        new = account_id or ""
        cp = self._run(["update", key, "--assignee", new])
        if cp.returncode != 0:
            raise _BeadsError(cp.returncode, cp.stderr, ["update", key, "--assignee"])
        self._verify_field(key, "assignee", new or None)

    def transition(
        self,
        key: str,
        transition_id: str,
        fields: dict[str, Any] | None = None,
    ) -> TransitionResult:
        del fields  # bd transitions take no required fields
        if not transition_id.startswith("bd:to:"):
            return {
                "success": False,
                "failure_kind": "ambiguous_transition",
                "failure_detail": f"transition_id {transition_id!r} not in bd format",
                "new_state": None,
            }
        target = transition_id[len("bd:to:") :]

        if target == "closed":
            cp = self._run(["close", key])
        elif target == "open":
            cur = self.state(key)["native_status"].lower()
            cp = self._run(
                ["reopen", key] if cur == "closed" else ["update", key, "--status", "open"]
            )
        else:
            cp = self._run(["update", key, "--status", target])

        if cp.returncode != 0:
            return {
                "success": False,
                "failure_kind": _classify_failure(cp.stderr),
                "failure_detail": cp.stderr.strip() or cp.stdout.strip(),
                "new_state": None,
            }
        new_state = self.state(key)
        if new_state["normalized"] != _BD_TO_NORMALIZED.get(target):
            return {
                "success": False,
                "failure_kind": "validator_failed",
                "failure_detail": (
                    f"postcondition mismatch: requested {target!r}, got "
                    f"{new_state['native_status']!r}"
                ),
                "new_state": new_state,
            }
        return {
            "success": True,
            "failure_kind": None,
            "failure_detail": None,
            "new_state": new_state,
        }

    def comment(self, key: str, body: Content) -> None:
        markdown = _content_to_markdown(body)
        cp = self._run(["comment", key, "--stdin"], input=markdown)
        if cp.returncode != 0:
            raise _BeadsError(cp.returncode, cp.stderr, ["comment", key, "--stdin"])

    def link(self, from_key: str, to_key: str, kind: str) -> None:
        cp = self._run(["dep", "add", from_key, to_key, "--type", kind])
        if cp.returncode != 0:
            raise _BeadsError(cp.returncode, cp.stderr, ["dep", "add"])

    def state(self, key: str) -> TicketState:
        raw = self._run_json(["show", key])
        if not isinstance(raw, dict):
            raise TrackerError(f"bd show {key} --json returned non-object: {raw!r}")
        return self._state_from_issue(raw)

    def project_requires_pr(self) -> bool:
        # beads is local-only; no PR validator concept.
        return False

    def is_shipped(self, key: str) -> ShipState:
        """PURE READ. Never writes under `.flow/`.

        Returns:
            not_shipped         — status != closed
            not_yet_observed    — status == closed + git commit referencing key
            indeterminate       — status == closed but no commit evidence found

        Workspace's `observe_ship_event` is responsible for freezing
        `not_yet_observed` into a stored ship-event record.
        """
        try:
            raw = self._run_json(["show", key])
        except _BeadsError:
            return {
                "state": "not_shipped",
                "shipped_at": None,
                "evidence": None,
                "source": "none",
            }
        if not isinstance(raw, dict):
            return {
                "state": "indeterminate",
                "shipped_at": None,
                "evidence": None,
                "source": "none",
            }
        status = str(raw.get("status", "")).lower()
        if status != "closed":
            return {
                "state": "not_shipped",
                "shipped_at": None,
                "evidence": None,
                "source": "none",
            }

        commit_sha = self._git_log_first_commit(key)
        if commit_sha is None:
            return {
                "state": "indeterminate",
                "shipped_at": None,
                "evidence": {
                    "tracker": "beads",
                    "tracker_status": status,
                    "commit_sha": None,
                },
                "source": "none",
            }
        evidence: dict[str, Any] = {
            "tracker": "beads",
            "tracker_status": status,
            "commit_sha": commit_sha,
            "closure_reason": raw.get("closure_reason"),
            "closed_at": raw.get("closed_at"),
        }
        return {
            "state": "not_yet_observed",
            "shipped_at": None,
            "evidence": evidence,
            "source": "live_backend_query",
        }

    # ─── Capability-gated (all NotSupported) ─────────────────────────────

    def set_sprint(self, key: str, sprint_id: str) -> None:
        del key, sprint_id
        raise NotSupported("BeadsAdapter does not support sprints")

    def list_sprints(self, project: str) -> list[Sprint]:
        del project
        raise NotSupported("BeadsAdapter does not support sprints")

    def add_watcher(self, key: str, account_id: str) -> None:
        del key, account_id
        raise NotSupported("BeadsAdapter does not support watchers")

    def set_fix_versions(self, key: str, versions: list[str]) -> None:
        del key, versions
        raise NotSupported("BeadsAdapter does not support fix_versions")

    def set_components(self, key: str, components: list[str]) -> None:
        del key, components
        raise NotSupported("BeadsAdapter does not support components")

    def set_epic_link(self, key: str, epic_key: str) -> None:
        del key, epic_key
        raise NotSupported(
            "BeadsAdapter does not support epic_link; use `parent` via create() instead."
        )

    def board_rank(self, key: str, after_key: str | None) -> None:
        del key, after_key
        raise NotSupported("BeadsAdapter does not support boards")

    def set_custom_field(
        self,
        key: str,
        field_key: str,
        value: Any,
        schema: FieldSpec,
    ) -> None:
        del key, field_key, value, schema
        raise NotSupported("BeadsAdapter does not support custom_fields")

    def get_attachments(self, key: str) -> list[Attachment]:
        del key
        raise NotSupported("BeadsAdapter does not support attachments")

    def upload_attachment(self, key: str, path: str) -> str:
        del key, path
        raise NotSupported("BeadsAdapter does not support attachments")

    # ─── Postcondition + git helpers ─────────────────────────────────────

    def _verify_field(self, key: str, field_name: str, expected: Any) -> None:
        """Re-read after a mutation; fail loud if the field did not change.

        Field-specific normalization: labels list vs comma-string, priority
        int vs "P<n>" string, assignee "" vs None.
        """
        raw = self._run_json(["show", key])
        if not isinstance(raw, dict):
            raise TrackerError(f"bd show {key} --json (post-write) returned non-object: {raw!r}")
        actual = raw.get(field_name)
        if field_name == "labels":
            actual_set = set(actual or [])
            expected_set = set(expected or [])
            if actual_set != expected_set:
                raise TrackerError(
                    f"postcondition: labels mismatch on {key}: "
                    f"expected={sorted(expected_set)!r} actual={sorted(actual_set)!r}"
                )
            return
        if field_name == "assignee":
            actual_n = actual or None
            expected_n = expected or None
            if actual_n != expected_n:
                raise TrackerError(
                    f"postcondition: assignee mismatch on {key}: "
                    f"expected={expected_n!r} actual={actual_n!r}"
                )
            return
        if field_name == "priority":
            actual_int = (
                int(actual) if isinstance(actual, (int, str)) and str(actual).isdigit() else actual
            )
            if actual_int != expected:
                raise TrackerError(
                    f"postcondition: priority mismatch on {key}: "
                    f"expected={expected!r} actual={actual!r}"
                )
            return
        if actual != expected:
            raise TrackerError(
                f"postcondition: {field_name} mismatch on {key}: "
                f"expected={expected!r} actual={actual!r}"
            )

    def _git_log_first_commit(self, key: str) -> str | None:
        cp = self._runner(
            ["git", "log", f"--grep={key}", "--pretty=format:%H", "-n", "1"],
            cwd=self._workspace_root,
            check=False,
        )
        if cp.returncode != 0:
            return None
        sha = cp.stdout.strip()
        return sha or None


__all__ = ["BeadsAdapter", "Runner"]
