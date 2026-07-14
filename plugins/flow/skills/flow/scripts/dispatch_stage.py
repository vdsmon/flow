"""State-machine driver for Flow target delivery.

Library + thin CLI. Stdlib-only. Imports `state` + `validate_workspace`.

Subcommands: `init`, `next`, `advance`, `release`, `revise-open`. The
dispatcher does NOT invoke handlers itself; it reads/writes state.json and
emits a handler-descriptor JSON for the SKILL.md prose layer to act on.

Lifecycle: pending → in_progress (via `next`) → completed | failed (via
`advance`, which composes the finish step with the next pick).

HARD GATE: validate_workspace.validate() runs on every `init` and every
`next`. Schema violation = exit 1, stderr lists violations.

Exit codes:
    0 = ok
    1 = generic error / validate-workspace failure / state malformed /
        unrecoverable state.json / ticket locked by a live run /
        config-version drift mid-run
    2 = no such ticket dir / not yet initialized
    3 = original run not terminal (revise-open)
    4 = a revision is already live (revise-open)
    5 = stale foreign lease (needs target-specific Flow workspace repair)
    7 = lost lease (another run took over)
"""

from __future__ import annotations

import argparse
import contextlib
import json
import secrets
import socket
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast

import _locking
import fleet
import lease
import recall_pending
import state
import validate_workspace as vw
from _registry import StageEntry, registry_by_name
from _timeutil import utcnow_iso
from snapshot import (
    classify_drift,
    component_files,
    engine_tree_clean,
    snapshot_sha_path,
    write_snapshot,
)

_STAGE_REGISTRY_RELATIVE = Path("stage-registry.toml")

# Lease covering the init handshake before the first stage timeout is known.
_INIT_TTL_S = 600
# Lease TTL = stage timeout * this multiplier. A flat additive buffer left
# near-zero headroom for normal agent-stage variance (a 30min implement
# legitimately ran 38min and self-evicted its own lease, flow-0xex); a
# proportional multiplier gives every stage headroom for overrun. The cost
# is a longer dead-run hold before auto-reclaim on the longest stage
# (review_loop, 60min -> 120min), bounded and recoverable via FLOW workspace repair.
_LEASE_TTL_MULTIPLIER = 2


def _stage_ttl_seconds(stage_meta: StageEntry | None) -> int:
    timeout_min = stage_meta.default_timeout_min if stage_meta else 10
    return timeout_min * 60 * _LEASE_TTL_MULTIPLIER


# A revision sub-run's default stage subset (flow-kx17.2): the PR is already open
# (human-merge keystone holds), so plan/ticket/create_pr/merge are skipped; reflect
# stays in (a human-review catch is prime compounding signal). Intersected with the
# workspace's configured stages, preserving ws.stages order.
_REVISION_DEFAULT_STAGES = (
    "implement",
    "code_review",
    "e2e",
    "commit",
    "review_loop",
    "review_brief",
    "reflect",
)


# ─── Handler-string parsing ──────────────────────────────────────────────────


def _parse_handler(value: str) -> dict[str, Any]:
    """Return a handler-descriptor dict. Assumes validate-workspace already passed."""
    if value == "inline":
        return {"handler_type": "inline"}
    if value == "none":
        return {"handler_type": "none"}
    if value.startswith("subagent:"):
        return {"handler_type": "subagent", "subagent_type": value[len("subagent:") :]}
    if value.startswith("skill:"):
        rest = value[len("skill:") :]
        if ":" in rest:
            name, _, args = rest.partition(":")
            return {"handler_type": "skill", "skill_name": name, "skill_args": args}
        return {"handler_type": "skill", "skill_name": rest, "skill_args": None}
    return {"handler_type": "unknown", "raw": value}


# ─── Git HEAD probe ──────────────────────────────────────────────────────────


def _git_stdout(workspace_root: Path, args: list[str]) -> str:
    try:
        cp = subprocess.run(
            ["git", *args],
            cwd=str(workspace_root),
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return ""
    if cp.returncode != 0:
        return ""
    return cp.stdout.strip()


def _git_head_sha(workspace_root: Path) -> str:
    return _git_stdout(workspace_root, ["rev-parse", "HEAD"])


def _git_branch(workspace_root: Path) -> str:
    return _git_stdout(workspace_root, ["branch", "--show-current"])


def _promote_recall_log(workspace_root: Path, ticket: str) -> None:
    # Best-effort: fold any matching recall-pending entries (written by the
    # plan-phase recall --record-pending) into the per-ticket recall-log on run
    # start. A promotion failure must never abort init.
    with contextlib.suppress(Exception):
        recall_pending.promote_matching(
            workspace_root,
            ticket=ticket,
            branch=_git_branch(workspace_root),
            head_sha=_git_head_sha(workspace_root),
            cwd=str(workspace_root),
            now_iso=utcnow_iso(),
        )


# ─── Public API ──────────────────────────────────────────────────────────────


def _skill_root_from_script() -> Path:
    # `__file__` = .../plugins/flow/skills/flow/scripts/dispatch_stage.py
    return Path(__file__).resolve().parent.parent


def run_dir(workspace_root: Path, ticket: str, revision: str | None = None) -> Path:
    """The run's state/lease container.

    `revision is None` returns the canonical per-ticket dir; a revision sub-run
    (flow-kx17.2) nests one level deeper at `.../revisions/<revision>/`, so it
    gets its own state.json + run.lock + snapshot while the original terminal run
    stays byte-untouched.
    """
    base = workspace_root / ".flow" / "runs" / ticket
    return base if revision is None else base / "revisions" / revision


def _ticket_dir(workspace_root: Path, ticket: str) -> Path:
    return run_dir(workspace_root, ticket, None)


def cmd_init(
    workspace_root: Path,
    ticket: str,
    force: bool = False,
    session_nonce: str | None = None,
) -> tuple[int, dict[str, Any]]:
    result, ws = vw.validate(workspace_root)
    if ws is None:
        return 1, {
            "error": "validate-workspace failed",
            "violations": result.violations,
        }
    td = _ticket_dir(workspace_root, ticket)

    # run_id is the stable per-ticket identity. Reuse the existing one whenever a
    # valid state is present (resume AND --force reset stay the same logical run),
    # so the lease sees us as the owner rather than a foreign run.
    existing, exit_code = state.read(td)
    # exit_code 2 = state.read quarantined a corrupt state.json and no .bak
    # parses. Minting a fresh all-pending run over it would replay a shipped
    # ticket (the exit-2 flavor of flow-k6l6), so refuse like
    # cmd_next/cmd_finish/cmd_status. --force stays the operator-explicit reset
    # and stamps a marker below.
    if exit_code == 2 and not force:
        return 1, {
            "error": f"unrecoverable state.json at {td}",
            "hint": f"FLOW workspace repair {ticket}",
        }
    # exit_code 1 = state.read quarantined a corrupt state.json and restored a
    # valid run from .bak (rewriting it to disk); treat it as valid-for-resume,
    # else a fresh run_id + state.init below would clobber the recovered history
    # to all-pending and replay a shipped ticket (flow-k6l6).
    have_valid = existing is not None and exit_code in (0, 1)
    resuming = have_valid and not force
    run_id = existing.run_id if have_valid else secrets.token_hex(8)
    recovery: dict[str, Any] = {}
    if exit_code == 1:
        recovery = {"state_recovered_from_backup": True}
    elif exit_code == 2:
        recovery = {"state_unrecoverable_replaced": True}

    # session_nonce is the per-session lease component run_id cannot supply: a
    # caller presenting the live owner's nonce re-acquires; one without it (a
    # second target invocation, which can only read run_id from state.json) is blocked at
    # acquire. The first acquire of a run presents none and acquire mints one; it
    # is returned so the dispatching session can carry it on later dispatch calls.
    boot, host, cwd, now = lease.boot_id(), socket.gethostname(), str(workspace_root), utcnow_iso()
    try:
        acquired = lease.acquire(
            td,
            run_id,
            _INIT_TTL_S,
            now,
            stage="init",
            current_boot=boot,
            hostname=host,
            cwd=cwd,
            session_nonce=session_nonce,
            force=force,
        )
    except lease.LeaseHeld as exc:
        return 1, {
            "error": "ticket locked by another live run",
            "holder": asdict(exc.holder),
            "hint": f"FLOW workspace repair {ticket}",
        }
    except lease.LeaseExpiredForeign as exc:
        return 5, {
            "error": "stale lease from another run",
            "holder": asdict(exc.holder),
            "hint": f"FLOW workspace repair {ticket}",
        }
    except lease.LeaseError as exc:
        # corrupt run.lock: cannot confirm ownership. Do NOT auto-clear; hand off
        # to the human-driven takeover (which quarantines the corrupt lock).
        return 1, {
            "error": "corrupt run.lock",
            "detail": str(exc),
            "hint": f"FLOW workspace repair {ticket}",
        }

    # Canonical snapshot for later `next` TOCTOU checks. On a FRESH run (or a
    # --force reset, or a resume whose snapshot was lost) establish S0. On RESUME
    # with an existing snapshot, do NOT re-baseline: that snapshot is the run's
    # TOCTOU baseline, and recomputing it from current content would launder any
    # unowned drift that landed while the run was suspended (a swapped engine, a
    # rewritten workspace.toml), silently defeating the next-stage drift guard
    # (flow-qwf3). The preserved S0 already reflects owned reconciles the original
    # session accepted (cmd_next rewrites it on owned drift), so the very next
    # `next` aborts on genuine unowned drift and reconciles owned drift, with the
    # lease guard in the correct order. Best-effort on the write paths: a snapshot
    # write failure must not block the run (verify treats absence as no-op). But
    # an absent sha makes classify_drift fail OPEN (drift guard silently off), so
    # surface the failure rather than swallow it. cmd_init still returns exit 0.
    marker: dict[str, Any] = {}
    if not (resuming and snapshot_sha_path(workspace_root, ticket).exists()):
        try:
            write_snapshot(workspace_root, ticket, skill_root=_skill_root_from_script())
        except Exception as exc:
            sha_present = snapshot_sha_path(workspace_root, ticket).exists()
            if sha_present:
                sys.stderr.write(
                    f"dispatch init: snapshot write failed for {ticket} ({exc}) but a "
                    "snapshot.sha is present; drift guard remains active (fail-closed)\n"
                )
            else:
                sys.stderr.write(
                    f"dispatch init: snapshot write failed for {ticket} ({exc}) and no "
                    "snapshot.sha exists; the config/version drift guard is OFF for this "
                    "run (fail-open) and drift will NOT be detected. Run "
                    f"`FLOW workspace repair {ticket}` to restore it.\n"
                )
            marker = {"snapshot_write_failed": True, "snapshot_guard_active": sha_present}

    if resuming:
        _promote_recall_log(workspace_root, ticket)
        return 0, {
            "ticket": ticket,
            "run_id": run_id,
            "session_nonce": acquired.session_nonce,
            "stages": ws.stages,
            "ticket_dir": str(td),
            "resumed": True,
            **marker,
            **recovery,
        }

    state.init(td, ticket, ws.backend, ws.stages, run_id=run_id)
    _promote_recall_log(workspace_root, ticket)
    return 0, {
        "ticket": ticket,
        "run_id": run_id,
        "session_nonce": acquired.session_nonce,
        "stages": ws.stages,
        "ticket_dir": str(td),
        "resumed": False,
        **marker,
        **recovery,
    }


def _revise_claim_path(td: Path) -> Path:
    return td / "revise.claim"


def _allocate_rev_id(td: Path) -> str:
    """Next monotonic rev-id (r1, r2, …) by scanning existing revisions/r* dirs.

    Caller MUST hold the revise.claim flock (closes the two-concurrent-opens
    pick-the-same-id TOCTOU).
    """
    revisions = td / "revisions"
    max_n = 0
    if revisions.is_dir():
        for child in revisions.iterdir():
            if child.is_dir() and child.name.startswith("r"):
                with contextlib.suppress(ValueError):
                    max_n = max(max_n, int(child.name[1:]))
    return f"r{max_n + 1}"


def _has_live_revision(td: Path) -> bool:
    """True if any existing revision sub-run holds a live or corrupt lease.

    Caller MUST hold the revise.claim flock. A live/corrupt lease means a
    revision is in flight (one revision at a time per ticket).
    """
    revisions = td / "revisions"
    if not revisions.is_dir():
        return False
    now, boot, host = utcnow_iso(), lease.boot_id(), lease.hostname()
    for child in sorted(revisions.iterdir()):
        if not (child.is_dir() and lease.run_lock_path(child).exists()):
            continue
        info = lease.classify(child, now, current_boot=boot, hostname=host)
        if info.get("state") in ("live", "corrupt"):
            return True
    return False


def cmd_revise_open(
    workspace_root: Path,
    ticket: str,
    stages: list[str] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Open a revision sub-run under a terminal ticket run (flow-kx17.2 keystone).

    A revision is a SUB-RUN at `.flow/runs/<ticket>/revisions/<rev-id>/` with its
    OWN lease/state/snapshot; the original terminal run is NEVER mutated. Guards:
    the original must be terminal (exit 3), and only one revision may be live at a
    time (exit 4). rev-id allocation + the live scan + state seed + lease acquire
    run under a single per-ticket revise.claim flock, closing the rev-id TOCTOU.
    """
    result, ws = vw.validate(workspace_root)
    if ws is None:
        return 1, {"error": "validate-workspace failed", "violations": result.violations}

    orig_td = _ticket_dir(workspace_root, ticket)
    orig, _code = state.read(orig_td)
    if orig is None:
        return 2, {"error": f"no original run state.json at {orig_td}; nothing to revise"}
    if not (state.pick_next_pending(orig, ws.stages) is None and state.find_failed(orig) is None):
        return 3, {
            "error": "original run not terminal",
            "hint": f"FLOW {ticket} or FLOW workspace repair {ticket}",
        }

    if stages is not None:
        # cmd_next picks via pick_next_pending over ws.stages, so a seeded stage
        # outside the pipeline is never visited and the revision would report
        # done immediately with everything still pending. Execution order also
        # follows ws.stages, not this list's order.
        unknown = [s for s in stages if s not in ws.stages]
        if unknown:
            return 1, {
                "error": "revision --stages not in the workspace pipeline: " + ", ".join(unknown),
                "hint": "pipeline stages: " + ", ".join(ws.stages),
            }
        subset = stages
    else:
        default = set(_REVISION_DEFAULT_STAGES)
        subset = [s for s in ws.stages if s in default]

    boot, host, cwd, now = lease.boot_id(), socket.gethostname(), str(workspace_root), utcnow_iso()
    with _locking.flock_blocking(_revise_claim_path(orig_td)):
        if _has_live_revision(orig_td):
            return 4, {"error": "a revision is already live"}
        rev_id = _allocate_rev_id(orig_td)
        rev_dir = run_dir(workspace_root, ticket, rev_id)
        run_id = secrets.token_hex(8)
        state.init(rev_dir, ticket, ws.backend, subset, run_id=run_id)
        acquired = lease.acquire(
            rev_dir,
            run_id,
            _INIT_TTL_S,
            now,
            stage="revise-init",
            current_boot=boot,
            hostname=host,
            cwd=cwd,
            session_nonce=None,
            force=False,
        )

    try:
        write_snapshot(
            workspace_root, ticket, skill_root=_skill_root_from_script(), revision=rev_id
        )
    except Exception as exc:
        sys.stderr.write(
            f"dispatch revise-open: snapshot write failed for {ticket}/{rev_id} ({exc}); the "
            "config/version drift guard is OFF for this revision (fail-open).\n"
        )

    return 0, {
        "ticket": ticket,
        "rev_id": rev_id,
        "run_id": run_id,
        "session_nonce": acquired.session_nonce,
        "revision_dir": str(rev_dir),
        "stages": subset,
    }


_DRIFT_ABORT = {
    "error": "config/version drift mid-run",
    "hint": "FLOW workspace repair <target>",
}


def _planned_files(td: Path) -> set[str]:
    planned: set[str] = set()
    with contextlib.suppress(Exception):
        raw = json.loads((td / "baseline.json").read_text(encoding="utf-8")).get(
            "planned_files", []
        )
        if isinstance(raw, list):
            planned = {str(p).removeprefix("./").replace("\\", "/") for p in raw}
    return planned


def _gate_drift(
    workspace_root: Path, ticket: str, td: Path, revision: str | None = None
) -> tuple[dict[str, Any] | None, str | None, dict[str, Any] | None, list[str]]:
    """Classify the canonical-snapshot drift gate.

    Returns (abort_payload, reconciled_label, current_snapshot, components).
    abort_payload is the exit-1 dict when genuine drift must halt the run, else
    None. reconciled_label is the comma-joined drifted-component names when the
    drift is owned (every changed component maps to a file in this run's
    planned_files, an intended edit → reload the baseline, don't abort), else
    None. A handler-tree drift maps to no single file and so is never owned.
    current_snapshot is classify_drift's computed snapshot, reused on reconcile
    so write_snapshot skips a second compute. components is the ordered drifted-
    component list (surfaced on the abort path too so cmd_next can recognize an
    engine-ONLY abort and run the engine re-anchor / re-verify discriminator).
    """
    drift_ok, detail, components, current_snapshot = classify_drift(
        workspace_root, ticket, skill_root=_skill_root_from_script(), revision=revision
    )
    reconciled_label: str | None = None
    if (not drift_ok) and components:
        files = component_files(
            components, workspace_root=workspace_root, skill_root=_skill_root_from_script()
        )
        planned = _planned_files(td)
        if all(files[c] is not None and files[c] in planned for c in components):
            reconciled_label = ", ".join(components)
    if (not drift_ok) and reconciled_label is None:
        return {**_DRIFT_ABORT, "detail": detail}, None, None, components
    return None, reconciled_label, current_snapshot, components


def _guard_lease_ownership(
    td: Path,
    run_id: str,
    session_nonce: str | None = None,
    current_boot: str | None = None,
    hostname: str | None = None,
) -> tuple[int, dict[str, Any]] | None:
    """Confirm an existing lease is still ours. Returns an error tuple, or None if ok.

    A run with no lease (legacy / direct test call) proceeds without one. A
    LeaseLost means another run took over (a rotated session_nonce, a changed
    run_id/boot/host, or a gone lock); a bare LeaseError means a corrupt
    run.lock we cannot read, so ownership is unconfirmable and the caller must
    stop before mutating state. current_boot/hostname default to probing when
    None; cmd_next passes its precomputed pair so the hot path pays one
    boot_id probe (a sysctl subprocess on macOS) instead of two.
    """
    try:
        if lease.read_lease(td) is not None:
            lease.assert_lease_still_mine(
                td,
                run_id,
                current_boot=lease.boot_id() if current_boot is None else current_boot,
                hostname=socket.gethostname() if hostname is None else hostname,
                session_nonce=session_nonce,
            )
    except lease.LeaseLost as exc:
        return lease.EXIT_LEASE_LOST, {
            "error": "lost lease",
            "detail": str(exc),
            "hint": "FLOW workspace repair",
        }
    except lease.LeaseError as exc:
        return lease.EXIT_LEASE_LOST, {
            "error": "corrupt run.lock",
            "detail": str(exc),
            "hint": "FLOW workspace repair <target>",
        }
    return None


def _resolve_engine_drift(
    workspace_root: Path, ticket: str, revision: str | None
) -> tuple[tuple[int, dict[str, Any]] | None, dict[str, bool]]:
    """Re-verify / re-anchor an engine-ONLY drift abort (flow-p9sc).

    Called only after the lease guard confirms ownership, so a re-anchor write
    never precedes the lease check. Re-runs a FRESH classify_drift (re-reading
    the first pass would reproduce the drift): re-verify clean → transient
    concurrent-read race, proceed with NO mutation (engine_drift_reverified);
    still engine-only AND the engine working tree clean vs HEAD → a committed
    lagging-main / marketplace advance, re-anchor the snapshot (engine_reanchored);
    a dirty (uncommitted) engine tree or a now-non-engine/mixed re-verify →
    fail-closed abort (PRESERVED GUARD). Returns (error_tuple_or_None, markers);
    error_tuple short-circuits cmd_next, markers ride the descriptor payload.
    """
    rok, rdetail, rcomps, rsnapshot = classify_drift(
        workspace_root, ticket, skill_root=_skill_root_from_script(), revision=revision
    )
    if rok:
        sys.stderr.write(f"dispatch: engine drift re-verified clean (transient) for {ticket}\n")
        return None, {"engine_drift_reverified": True}
    if rcomps == ["engine"] and engine_tree_clean(_skill_root_from_script()):
        try:
            write_snapshot(
                workspace_root,
                ticket,
                skill_root=_skill_root_from_script(),
                snapshot=rsnapshot,
                revision=revision,
            )
        except Exception:
            return (1, {**_DRIFT_ABORT, "detail": "drift: engine"}), {}
        sys.stderr.write(
            f"dispatch: auto-reconciled owned drift (engine re-anchored) for {ticket}\n"
        )
        return None, {"engine_reanchored": True}
    return (1, {**_DRIFT_ABORT, "detail": rdetail}), {}


def _reconcile_post_lease(
    workspace_root: Path,
    ticket: str,
    revision: str | None,
    *,
    engine_abort: bool,
    owned_reconcile: str | None,
    drift_snapshot: dict[str, Any] | None,
) -> tuple[tuple[int, dict[str, Any]] | None, dict[str, bool]]:
    """Settle a deferred drift AFTER the lease guard confirms ownership.

    An engine-only abort runs the re-verify / re-anchor discriminator
    (_resolve_engine_drift). An owned drift (every component maps to a planned
    file) reloads the snapshot baseline so later dispatch calls verify against
    the intended workspace.toml. The two are mutually exclusive (an engine-only
    abort is never owned). Returns (error_tuple_or_None, markers).
    """
    if engine_abort:
        return _resolve_engine_drift(workspace_root, ticket, revision)
    if owned_reconcile:
        try:
            write_snapshot(
                workspace_root,
                ticket,
                skill_root=_skill_root_from_script(),
                snapshot=drift_snapshot,
                revision=revision,
            )
        except Exception:
            return (1, {**_DRIFT_ABORT, "detail": f"drift: {owned_reconcile}"}), {}
        sys.stderr.write(
            f"dispatch: auto-reconciled owned drift ({owned_reconcile}) for {ticket}\n"
        )
    return None, {}


def cmd_next(
    workspace_root: Path,
    ticket: str,
    session_nonce: str | None = None,
    revision: str | None = None,
) -> tuple[int, dict[str, Any]]:
    result, snapshot = vw.validate(workspace_root)
    if snapshot is None:
        return 1, {
            "error": "validate-workspace failed",
            "violations": result.violations,
        }
    td = run_dir(workspace_root, ticket, revision)
    ts, exit_code = state.read(td)
    if ts is None:
        if exit_code == 2:
            return 1, {"error": f"unrecoverable state.json at {td}"}
        return 2, {"error": f"no state.json at {td}; run `dispatch init` first"}
    recovery = {"state_recovered_from_backup": True} if exit_code == 1 else {}

    # TOCTOU: refuse if workspace.toml / registry / a handler plugin drifted
    # since the run started. EXCEPTION: an owned drift whose changed component(s)
    # all map to planned files auto-reconciles (snapshot reload) AFTER the lease
    # guard confirms us. SECOND EXCEPTION: an engine-ONLY abort defers past the
    # lease guard, then re-verifies / re-anchors (flow-p9sc) so a committed
    # lagging-main / marketplace advance or a transient concurrent-read race
    # self-heals; a dirty (uncommitted) engine tree still fail-closes.
    abort_payload, owned_reconcile, drift_snapshot, components = _gate_drift(
        workspace_root, ticket, td, revision
    )
    engine_abort = abort_payload is not None and components == ["engine"]
    if abort_payload is not None and not engine_abort:
        return 1, abort_payload
    boot, host = lease.boot_id(), socket.gethostname()
    guard = _guard_lease_ownership(td, ts.run_id, session_nonce, current_boot=boot, hostname=host)
    if guard is not None:
        return guard

    reconcile_err, engine_markers = _reconcile_post_lease(
        workspace_root,
        ticket,
        revision,
        engine_abort=engine_abort,
        owned_reconcile=owned_reconcile,
        drift_snapshot=drift_snapshot,
    )
    if reconcile_err is not None:
        return reconcile_err

    failed = state.find_failed(ts)
    if failed is not None:
        record = ts.stages[failed]
        return 0, {
            "done": False,
            "blocked_by": failed,
            "reason": record.failure_detail or "stage failed",
            **recovery,
        }

    next_stage = state.pick_next_pending(ts, snapshot.stages)
    if next_stage is None:
        return 0, {"done": True, **recovery}

    head_sha = _git_head_sha(workspace_root)

    # Assemble the full descriptor BEFORE mutating state. If descriptor assembly raises, the stage
    # must stay pending rather than be stuck in_progress.
    registry_path = _skill_root_from_script() / _STAGE_REGISTRY_RELATIVE
    stage_meta = registry_by_name(registry_path).get(next_stage)
    handler_descriptor = _parse_handler(snapshot.handlers[next_stage])
    output_path = td / "stages" / f"{next_stage}.out"
    payload: dict[str, Any] = {
        "done": False,
        "stage": next_stage,
        "timeout_min": stage_meta.default_timeout_min if stage_meta else 10,
        "head_sha": head_sha,
        "ticket_dir": str(td),
        "output_path": str(output_path),
        "roles": stage_meta.roles if stage_meta else [],
        **handler_descriptor,
    }
    # Attach reference_doc regardless of handler type so the do-loop can pass it
    # to a spawned subagent (and to inline / skill / none handlers alike).
    if stage_meta is not None and stage_meta.reference_doc:
        payload["reference_doc"] = stage_meta.reference_doc
    if owned_reconcile:
        payload["reconciled_drift"] = owned_reconcile
    payload.update(engine_markers)
    if exit_code == 1:
        payload["state_recovered_from_backup"] = True

    # Refresh the lease to cover this stage's timeout window before marking it
    # in_progress, so a multi-minute stage does not self-expire the lease.
    if lease.read_lease(td) is not None:
        ttl = _stage_ttl_seconds(stage_meta)
        try:
            lease.refresh(
                td,
                ts.run_id,
                ttl,
                utcnow_iso(),
                stage=next_stage,
                current_boot=boot,
                hostname=host,
                cwd=str(workspace_root),
                session_nonce=session_nonce,
            )
        except lease.LeaseLost as exc:
            return lease.EXIT_LEASE_LOST, {
                "error": "lost lease",
                "detail": str(exc),
                "hint": "FLOW workspace repair",
            }

    # Shadow-write the fleet liveness ledger (epic flow-8by2.2): an upsert that
    # registers + heartbeats this run under the shared .flow/fleet/. Maintainer-
    # gated and fail-open: nothing reads it authoritatively yet (child-3 cuts the
    # readers over), so a shadow-ledger fault must never break dispatch. Per-key
    # flock contention is minimal (one writer per key; only a launch-register vs
    # the first next briefly serialize).
    with contextlib.suppress(Exception):
        fleet.register_run(
            workspace_root,
            ticket,
            ts.run_id,
            now=utcnow_iso(),
            hostname=host,
            boot_id=boot,
        )

    state.begin_stage(td, next_stage, head_sha)
    return 0, payload


def cmd_finish(
    workspace_root: Path,
    ticket: str,
    stage_name: str,
    status_value: str,
    output_path: str | None = None,
    skill_output: dict[str, Any] | None = None,
    failure_detail: str | None = None,
    session_nonce: str | None = None,
    revision: str | None = None,
) -> tuple[int, dict[str, Any]]:
    if status_value not in ("completed", "failed"):
        return 1, {"error": f"--status must be completed|failed, got {status_value!r}"}
    status = cast(state.StageStatus, status_value)
    td = run_dir(workspace_root, ticket, revision)
    ts, exit_code = state.read(td)
    if ts is None:
        if exit_code == 2:
            return 1, {"error": f"unrecoverable state.json at {td}"}
        return 2, {"error": f"no state.json at {td}; run `dispatch init` first"}
    recovery = {"state_recovered_from_backup": True} if exit_code == 1 else {}

    guard = _guard_lease_ownership(td, ts.run_id, session_nonce)
    if guard is not None:
        return guard

    if output_path is not None:
        p = Path(output_path).expanduser()
        if not p.is_absolute():
            p = workspace_root / p
        if not p.is_file():
            return 1, {
                "error": f"--output-path names a missing file: {p}",
                "hint": "write the stage output file first, then re-run finish/advance",
            }

    head_sha = _git_head_sha(workspace_root)
    try:
        new_state = state.finish_stage(
            td,
            stage_name,
            status,
            head_sha,
            output_path=output_path,
            skill_output=skill_output,
            failure_detail=failure_detail,
        )
    except (ValueError, state.StateUnrecoverable) as exc:
        return 1, {"error": str(exc), **recovery}

    _, snapshot = vw.validate(workspace_root)
    next_pending: str | None = None
    if snapshot is not None and state.find_failed(new_state) is None:
        next_pending = state.pick_next_pending(new_state, snapshot.stages)

    # Run finished cleanly (last stage completed, nothing pending or failed):
    # drop the lease. A failed run keeps its lease so FLOW workspace repair can act.
    if (
        status_value == "completed"
        and snapshot is not None
        and next_pending is None
        and state.find_failed(new_state) is None
    ):
        with contextlib.suppress(Exception):
            lease.release(td, new_state.run_id, session_nonce)
        # Positive-deregister from the fleet ledger so a completed run drops out of
        # the reconciled liveness read at once, rather than lingering until the
        # heartbeat-staleness window (epic flow-8by2.3). Maintainer-gated + fail-open:
        # a shadow-ledger fault must never break a clean finish.
        with contextlib.suppress(Exception):
            fleet.deregister_run(workspace_root, ticket, run_id=new_state.run_id)

    return 0, {
        "stage": stage_name,
        "status": status_value,
        "next_pending": next_pending,
        **recovery,
    }


def cmd_advance(
    workspace_root: Path,
    ticket: str,
    stage_name: str,
    status_value: str,
    output_path: str | None = None,
    skill_output: dict[str, Any] | None = None,
    failure_detail: str | None = None,
    session_nonce: str | None = None,
    revision: str | None = None,
) -> tuple[int, dict[str, Any]]:
    """Finish the current stage and return the next descriptor in one call.

    Composes cmd_finish + cmd_next so the do-loop spends one script round-trip per
    stage instead of two. The returned payload spreads cmd_next's output at the top
    level (so it parses identically to `next`: {done} / descriptor / {blocked_by})
    and adds a `finished` object confirming the prior stage closed. On a finish
    error it returns immediately without advancing.
    """
    finish_rc, finish_payload = cmd_finish(
        workspace_root,
        ticket,
        stage_name,
        status_value,
        output_path=output_path,
        skill_output=skill_output,
        failure_detail=failure_detail,
        session_nonce=session_nonce,
        revision=revision,
    )
    if finish_rc != 0:
        return finish_rc, finish_payload
    next_rc, next_payload = cmd_next(workspace_root, ticket, session_nonce, revision)
    merged = {"finished": {"stage": stage_name, "status": status_value}, **next_payload}
    if finish_payload.get("state_recovered_from_backup"):
        merged["state_recovered_from_backup"] = True
    return next_rc, merged


def cmd_status(
    workspace_root: Path, ticket: str, revision: str | None = None
) -> tuple[int, dict[str, Any]]:
    td = run_dir(workspace_root, ticket, revision)
    ts, exit_code = state.read(td)
    if ts is None:
        if exit_code == 2:
            return 1, {"error": f"unrecoverable state.json at {td}"}
        return 2, {"error": f"no state.json at {td}"}
    return exit_code, asdict(ts)


def cmd_release(
    workspace_root: Path,
    ticket: str,
    session_nonce: str | None = None,
    revision: str | None = None,
) -> tuple[int, dict[str, Any]]:
    td = run_dir(workspace_root, ticket, revision)
    ts, _ = state.read(td)
    released = False
    if ts is not None:
        try:
            released = lease.release(td, ts.run_id, session_nonce)
        except lease.LeaseError as exc:
            # SKILL.md step 5 calls release unconditionally on every exit path,
            # so a corrupt run.lock must yield released=false, not a traceback.
            # The corrupt lock stays for confirmed target repair to quarantine.
            return 0, {"ticket": ticket, "released": False, "detail": str(exc)}
    return 0, {"ticket": ticket, "released": released}


# ─── CLI ─────────────────────────────────────────────────────────────────────


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Flow dispatcher state machine.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--ticket", required=True)
    common.add_argument("--workspace-root", default=".")
    common.add_argument(
        "--session-nonce",
        default=None,
        help="Per-session lease nonce carried from init; omit it if lost (run_id fallback).",
    )

    # A revision sub-run redirect (flow-kx17.2): drives the sub-run at
    # runs/<ticket>/revisions/<id>/ instead of the ticket-level run. init proper
    # is NOT a revision target (revise-open seeds the revision), so it omits this.
    revision_help = (
        "drive the revision sub-run at runs/<ticket>/revisions/<id>/ (flow-kx17.2); "
        "default = the ticket-level run"
    )

    p_init = sub.add_parser("init", parents=[common], help="Initialize per-ticket state.json.")
    p_init.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing state with a fresh all-pending run.",
    )

    p_revise = sub.add_parser(
        "revise-open", parents=[common], help="Open a revision sub-run under a terminal run."
    )
    p_revise.add_argument(
        "--stages",
        default=None,
        help="comma-separated stage subset for the revision; each must be a workspace "
        "pipeline stage and execution follows the pipeline order (default: the built-in "
        "subset intersected with the workspace's stages)",
    )

    p_next = sub.add_parser("next", parents=[common], help="Pick next pending stage.")
    p_next.add_argument("--revision", default=None, help=revision_help)
    p_release = sub.add_parser("release", parents=[common], help="Release the run lease.")
    p_release.add_argument("--revision", default=None, help=revision_help)

    p_advance = sub.add_parser(
        "advance", parents=[common], help="Finish current stage AND return next descriptor."
    )
    p_advance.add_argument("--stage", required=True)
    p_advance.add_argument(
        "--status", dest="status_value", choices=("completed", "failed"), required=True
    )
    p_advance.add_argument("--output-path", default=None)
    p_advance.add_argument("--skill-output", default=None)
    p_advance.add_argument("--failure-detail", default=None)
    p_advance.add_argument("--revision", default=None, help=revision_help)

    return parser.parse_args(argv)


def _parse_skill_output_arg(raw: str | None) -> tuple[dict[str, Any] | None, str | None]:
    """Parse the --skill-output JSON arg. Returns (parsed_or_None, error_or_None)."""
    if not raw:
        return None, None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"--skill-output not JSON: {exc}"
    if not isinstance(parsed, dict):
        return None, "--skill-output must be a JSON object"
    return parsed, None


def cli_main(argv: list[str]) -> int:
    args = _parse_args(argv)
    workspace_root = Path(args.workspace_root).expanduser().resolve()

    if args.cmd == "init":
        rc, payload = cmd_init(
            workspace_root, args.ticket, force=args.force, session_nonce=args.session_nonce
        )
    elif args.cmd == "revise-open":
        rev_stages = (
            [s.strip() for s in args.stages.split(",") if s.strip()] if args.stages else None
        )
        rc, payload = cmd_revise_open(workspace_root, args.ticket, stages=rev_stages)
    elif args.cmd == "next":
        rc, payload = cmd_next(workspace_root, args.ticket, args.session_nonce, args.revision)
    elif args.cmd == "advance":
        skill_output, err = _parse_skill_output_arg(args.skill_output)
        if err:
            sys.stderr.write(f"dispatch advance: {err}\n")
            return 1
        rc, payload = cmd_advance(
            workspace_root,
            args.ticket,
            args.stage,
            args.status_value,
            output_path=args.output_path,
            skill_output=skill_output,
            failure_detail=args.failure_detail,
            session_nonce=args.session_nonce,
            revision=args.revision,
        )
    elif args.cmd == "release":
        rc, payload = cmd_release(workspace_root, args.ticket, args.session_nonce, args.revision)
    else:
        sys.stderr.write(f"unknown subcommand {args.cmd!r}\n")
        return 1

    sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    if rc != 0:
        if "violations" in payload:
            for v in payload["violations"]:
                sys.stderr.write(v + "\n")
        elif "error" in payload:
            sys.stderr.write(str(payload["error"]) + "\n")
    return rc


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = [
    "cli_main",
    "cmd_advance",
    "cmd_finish",
    "cmd_init",
    "cmd_next",
    "cmd_release",
    "cmd_revise_open",
    "cmd_status",
    "run_dir",
]
