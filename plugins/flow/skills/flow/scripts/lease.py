"""Per-ticket run lease: a MUTEX preventing two concurrent /flow do on one ticket.

Library + thin CLI. Stdlib-only.

This is NOT a liveness checker. /flow dispatch runs as short subprocesses (init /
next / finish / release), each of which exits immediately, so there is no live
process to ping. Mutual exclusion comes from lease *identity* (the stable
per-ticket state.run_id, a per-acquire session_nonce, plus boot_id + hostname)
compared under a flock, not from pid liveness. The lease expiry is refreshed on
the dispatch calls the agent already makes; its TTL is tied to the current stage
timeout so it survives a multi-minute stage.

The session_nonce is the per-session component run_id alone cannot provide:
run_id is reused from state.json on resume, so a second /flow do on the same
ticket reads the same run_id and would otherwise re-acquire a LIVE lease as if it
were the owner. The nonce is minted fresh on each acquire (and rotated on a
force/takeover or an expired-owner resume) and carried by the dispatching session
across its own dispatch calls; it is NOT stored in state.json, so a second
session cannot present it. Owner re-acquire of a LIVE lease therefore requires the
matching nonce, and refresh/release/assert detect a rotated nonce as a takeover.

Lease file: `<ticket_dir>/run.lock` (JSON). Acquire/refresh/release serialize on
the sibling `<ticket_dir>/run.lock.lock` via a single blocking flock spanning
read -> decide -> atomic write, mirroring state.py's `_update`. `read_lease` is
lock-free on purpose: it is called from inside the held flock (flock is not
reentrant across fds under blocking LOCK_EX), and atomic_write_text uses
os.replace so a concurrent reader sees old-or-new, never a torn file.

Reboot handling: a stale-but-expired foreign lease is reboot-clearable (the holder
cannot exist after a reboot) only when it is from the SAME hostname AND its boot_id
differs from the current boot, so it is overwritten. The same-hostname requirement
keeps a live foreign host on shared .flow storage (different hostname, different
boot) from being mis-cleared. An expired foreign lease from the same boot, or from
a different host, needs human takeover via /flow recover unless `force` is passed.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from _atomicio import atomic_write_text
from _locking import flock_blocking
from _timeutil import iso_z, parse_iso, utcnow_iso

EXIT_LEASE_LOST = 7

Runner = Callable[[list[str]], str]


# ─── Types ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Lease:
    run_id: str
    boot_id: str
    hostname: str
    cwd: str
    acquired_at: str
    lease_expires_at: str
    stage: str | None = None
    pid: int = 0  # informational only; never used for liveness gating
    session_nonce: str = ""  # per-acquire; "" only for a pre-upgrade lease


class LeaseError(Exception):
    """Base for lease acquisition failures."""


class LeaseHeld(LeaseError):
    """A live lease holds this ticket: a different run_id, OR the same run_id
    re-acquired without the matching session_nonce (a second /flow do)."""

    def __init__(self, holder: Lease) -> None:
        super().__init__(f"ticket lease held by run_id={holder.run_id!r}")
        self.holder = holder


class LeaseExpiredForeign(LeaseError):
    """An expired foreign lease that is NOT reboot-clearable. Needs /flow recover."""

    def __init__(self, holder: Lease) -> None:
        super().__init__(f"expired foreign lease from run_id={holder.run_id!r}")
        self.holder = holder


class LeaseLost(LeaseError):
    """The lease is no longer ours (gone, or a different run_id/boot/hostname)."""


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _ts_token() -> str:
    # colon-free so it is usable in a filename (mirrors state._ts_token).
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _mint_nonce() -> str:
    return secrets.token_hex(8)


def _expiry_iso(now_iso: str, ttl_seconds: int) -> str:
    now = parse_iso(now_iso)
    if now is None:
        raise LeaseError(f"unparseable now_iso: {now_iso!r}")
    expires = now + timedelta(seconds=ttl_seconds)
    return iso_z(expires)


def _default_runner() -> Runner:
    def run(args: list[str]) -> str:
        return subprocess.run(args, capture_output=True, text=True, check=True).stdout

    return run


def boot_id(runner: Runner | None = None) -> str:
    """A boot-session identifier, or "" if unavailable.

    macOS: `sysctl -n kern.bootsessionuuid`. Linux:
    /proc/sys/kernel/random/boot_id. Any failure returns "" so a missing boot id
    falls through to force/else in acquire rather than silently stealing a lease.
    """
    runner = runner or _default_runner()
    try:
        if sys.platform == "darwin":
            return runner(["sysctl", "-n", "kern.bootsessionuuid"]).strip()
        if sys.platform.startswith("linux"):
            return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except (OSError, subprocess.SubprocessError):
        return ""
    return ""


def hostname() -> str:
    """The current host name, mirroring socket.gethostname()."""
    return socket.gethostname()


# ─── Paths ───────────────────────────────────────────────────────────────────


def run_lock_path(ticket_dir: Path) -> Path:
    return ticket_dir / "run.lock"


def _flock_path(ticket_dir: Path) -> Path:
    return ticket_dir / "run.lock.lock"


# ─── Serialization ───────────────────────────────────────────────────────────


def _serialize(lease: Lease) -> str:
    return json.dumps(asdict(lease), indent=2, sort_keys=True) + "\n"


def _deserialize(raw: str) -> Lease:
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise LeaseError("run.lock root is not an object")
    return Lease(
        run_id=str(data["run_id"]),
        boot_id=str(data.get("boot_id", "")),
        hostname=str(data.get("hostname", "")),
        cwd=str(data.get("cwd", "")),
        acquired_at=str(data["acquired_at"]),
        lease_expires_at=str(data["lease_expires_at"]),
        stage=data.get("stage"),
        pid=int(data.get("pid", 0)),
        session_nonce=str(data.get("session_nonce", "")),
    )


# ─── Read (lock-free; callers hold the flock) ─────────────────────────────────


def read_lease(ticket_dir: Path) -> Lease | None:
    """Read run.lock. Returns None if absent. Raises LeaseError if present but corrupt.

    Lock-free by design: callers inside acquire/refresh/release already hold the
    flock, and a second blocking flock would deadlock. os.replace in the writer
    makes this read see old-or-new, never torn.
    """
    path = run_lock_path(ticket_dir)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        return _deserialize(raw)
    except (KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise LeaseError(f"corrupt run.lock at {path}: {exc}") from exc


def is_expired(lease: Lease, now_iso: str) -> bool:
    """True when now >= lease_expires_at. Equality counts as expired."""
    now = parse_iso(now_iso)
    expires = parse_iso(lease.lease_expires_at)
    if now is None or expires is None:
        return True
    return now >= expires


# ─── Public API ──────────────────────────────────────────────────────────────


def acquire(
    ticket_dir: Path,
    run_id: str,
    ttl_seconds: int,
    now_iso: str,
    *,
    stage: str | None = None,
    current_boot: str,
    hostname: str,
    cwd: str,
    session_nonce: str | None = None,
    force: bool = False,
) -> Lease:
    """Acquire (or owner-re-acquire) the ticket lease under the flock.

    Branch order matters. An owner (matching run_id) splits on expiry + nonce:
      - force -> rotate the nonce and overwrite (explicit reset/takeover).
      - LIVE owner -> re-acquire ONLY when the caller presents the matching
        session_nonce; otherwise LeaseHeld. This is the per-session guard: a
        second /flow do reuses run_id from state.json but cannot present the
        live owner's nonce, so it is blocked instead of silently re-acquiring.
      - EXPIRED owner -> legitimate resume of our own dead run; rotate the nonce
        (the prior session is gone) and preserve acquired_at.
    Foreign cases are unchanged: live -> LeaseHeld (checked BEFORE force, so
    force never steals a live foreign lease); expired and boot differs (both
    boot ids truthy, same host) -> reboot-clearable overwrite; expired and force
    -> overwrite; else expired -> LeaseExpiredForeign. A fresh write (and any
    overwrite/rotate) mints a new nonce.

    Raises:
        LeaseHeld, LeaseExpiredForeign, LeaseError
    """
    ticket_dir.mkdir(parents=True, exist_ok=True)
    expires_at = _expiry_iso(now_iso, ttl_seconds)
    with flock_blocking(_flock_path(ticket_dir)):
        existing = read_lease(ticket_dir)

        if existing is None:
            return _write_lease(
                ticket_dir,
                run_id=run_id,
                boot_id=current_boot,
                hostname=hostname,
                cwd=cwd,
                acquired_at=now_iso,
                lease_expires_at=expires_at,
                stage=stage,
                session_nonce=_mint_nonce(),
            )

        if existing.run_id == run_id:
            if force:
                # explicit reset/takeover: rotate the nonce, fresh acquired_at.
                return _write_lease(
                    ticket_dir,
                    run_id=run_id,
                    boot_id=current_boot,
                    hostname=hostname,
                    cwd=cwd,
                    acquired_at=now_iso,
                    lease_expires_at=expires_at,
                    stage=stage,
                    session_nonce=_mint_nonce(),
                )
            if not is_expired(existing, now_iso):
                # LIVE owner: re-acquire only with the matching nonce, else block.
                if session_nonce and session_nonce == existing.session_nonce:
                    return _write_lease(
                        ticket_dir,
                        run_id=run_id,
                        boot_id=current_boot,
                        hostname=hostname,
                        cwd=cwd,
                        acquired_at=existing.acquired_at,
                        lease_expires_at=expires_at,
                        stage=stage,
                        session_nonce=existing.session_nonce,
                    )
                raise LeaseHeld(existing)
            # EXPIRED owner: resume our own dead run; rotate the nonce.
            return _write_lease(
                ticket_dir,
                run_id=run_id,
                boot_id=current_boot,
                hostname=hostname,
                cwd=cwd,
                acquired_at=existing.acquired_at,
                lease_expires_at=expires_at,
                stage=stage,
                session_nonce=_mint_nonce(),
            )

        # foreign lease.
        if not is_expired(existing, now_iso):
            raise LeaseHeld(existing)

        reboot_clearable = (
            bool(existing.boot_id)
            and bool(current_boot)
            and (existing.boot_id != current_boot)
            and existing.hostname == hostname
        )
        if reboot_clearable or force:
            return _write_lease(
                ticket_dir,
                run_id=run_id,
                boot_id=current_boot,
                hostname=hostname,
                cwd=cwd,
                acquired_at=now_iso,
                lease_expires_at=expires_at,
                stage=stage,
                session_nonce=_mint_nonce(),
            )
        raise LeaseExpiredForeign(existing)


def refresh(
    ticket_dir: Path,
    run_id: str,
    ttl_seconds: int,
    now_iso: str,
    *,
    stage: str | None = None,
    current_boot: str,
    hostname: str,
    cwd: str,
    session_nonce: str | None = None,
) -> Lease:
    """Refresh our own lease (move expiry/stage). LeaseLost if it is not ours.

    The nonce is checked both-non-empty (mirrors the boot_id rule): a rotated
    on-disk nonce against our carried one means a force/takeover happened and we
    lost the lease. An empty nonce on either side (a pre-upgrade lease, or a
    caller that lost its nonce across a compaction) falls back to run_id-only.
    The on-disk nonce is preserved across the refresh.

    Raises:
        LeaseLost, LeaseError
    """
    expires_at = _expiry_iso(now_iso, ttl_seconds)
    with flock_blocking(_flock_path(ticket_dir)):
        existing = read_lease(ticket_dir)
        if existing is None or existing.run_id != run_id:
            raise LeaseLost(f"lease no longer held by run_id={run_id!r}")
        if existing.session_nonce and session_nonce and existing.session_nonce != session_nonce:
            raise LeaseLost(
                f"session_nonce mismatch: on-disk {existing.session_nonce!r} != {session_nonce!r}"
            )
        return _write_lease(
            ticket_dir,
            run_id=run_id,
            boot_id=current_boot,
            hostname=hostname,
            cwd=cwd,
            acquired_at=existing.acquired_at,
            lease_expires_at=expires_at,
            stage=stage,
            session_nonce=existing.session_nonce,
        )


def assert_lease_still_mine(
    ticket_dir: Path,
    run_id: str,
    *,
    current_boot: str | None = None,
    hostname: str | None = None,
    session_nonce: str | None = None,
) -> None:
    """Raise LeaseLost if the lease is gone or no longer identifies as ours.

    Does NOT check expiry: the owner may legitimately resume a stage past
    expiry. Boot/hostname/nonce are checked only when provided. An empty/unknown
    value on either side (e.g. a sandbox that blocked the boot probe, or a nonce
    lost across a compaction) makes that check inconclusive and is skipped,
    falling back to the run_id identity. Mirrors the both-non-empty rule in
    classify and acquire. A rotated session_nonce means a force/takeover evicted
    us.

    Raises:
        LeaseLost, LeaseError
    """
    lease = read_lease(ticket_dir)
    if lease is None:
        raise LeaseLost("run.lock is gone")
    if lease.run_id != run_id:
        raise LeaseLost(f"run_id mismatch: on-disk {lease.run_id!r} != {run_id!r}")
    if lease.boot_id and current_boot and lease.boot_id != current_boot:
        raise LeaseLost(f"boot_id mismatch: on-disk {lease.boot_id!r} != {current_boot!r}")
    if hostname is not None and lease.hostname != hostname:
        raise LeaseLost(f"hostname mismatch: on-disk {lease.hostname!r} != {hostname!r}")
    if lease.session_nonce and session_nonce and lease.session_nonce != session_nonce:
        raise LeaseLost(
            f"session_nonce mismatch: on-disk {lease.session_nonce!r} != {session_nonce!r}"
        )


def release(ticket_dir: Path, run_id: str, session_nonce: str | None = None) -> bool:
    """Remove run.lock iff it is ours. Returns True if removed, False otherwise.

    The nonce is checked both-non-empty (mirrors refresh): a rotated on-disk
    nonce means a force/takeover evicted us, so we must NOT drop the new owner's
    lease. An empty nonce on either side falls back to run_id-only.
    """
    with flock_blocking(_flock_path(ticket_dir)):
        existing = read_lease(ticket_dir)
        if existing is None or existing.run_id != run_id:
            return False
        if existing.session_nonce and session_nonce and existing.session_nonce != session_nonce:
            return False
        run_lock_path(ticket_dir).unlink(missing_ok=True)
        return True


def classify(
    ticket_dir: Path,
    now_iso: str,
    *,
    current_boot: str | None = None,
    hostname: str | None = None,
) -> dict[str, object]:
    """Describe the lease for /flow recover.

    state is one of: free | live | expired_reboot_clearable | expired_foreign |
    corrupt. holder is the lease as a dict, or None when free or corrupt.

    Non-mutating: a corrupt run.lock yields {"state": "corrupt"} but is never
    touched here. Remediation lives in takeover_clear, which classifies and
    mutates under one flock for the human-driven recover takeover.
    """
    try:
        lease = read_lease(ticket_dir)
    except LeaseError:
        return {"state": "corrupt", "holder": None}
    if lease is None:
        return {"state": "free", "holder": None}
    holder = asdict(lease)
    if not is_expired(lease, now_iso):
        return {"state": "live", "holder": holder}
    if (
        lease.boot_id
        and current_boot
        and lease.boot_id != current_boot
        and lease.hostname == hostname
    ):
        return {"state": "expired_reboot_clearable", "holder": holder}
    return {"state": "expired_foreign", "holder": holder}


def _quarantine_locked(ticket_dir: Path) -> Path | None:
    """Rename run.lock to run.lock.quarantine.<ts>. Caller MUST hold the flock.

    Extracted so takeover_clear can quarantine inside its own flock span:
    flock_blocking opens a fresh fd per call and LOCK_EX blocks across fds even
    within one process, so nesting the public quarantine_corrupt_lock would
    self-deadlock. Returns the quarantine dst Path, or None when absent.
    """
    src = run_lock_path(ticket_dir)
    if not src.exists():
        return None
    dst = ticket_dir / f"run.lock.quarantine.{_ts_token()}"
    os.replace(src, dst)
    return dst


def quarantine_corrupt_lock(ticket_dir: Path) -> Path | None:
    """Rename a still-corrupt run.lock to run.lock.quarantine.<ts> for forensics.

    Re-verifies corruption under the flock before renaming: any classification
    the caller did outside the flock is stale by the time the rename runs (a
    concurrent acquire may have replaced the corrupt file with a valid live
    lease). A lock that is absent or parses as a valid Lease is left alone and
    None is returned; only a lock that still raises LeaseError is renamed.
    """
    with flock_blocking(_flock_path(ticket_dir)):
        try:
            read_lease(ticket_dir)
        except LeaseError:
            return _quarantine_locked(ticket_dir)
        return None


def takeover_clear(
    ticket_dir: Path,
    now_iso: str,
    *,
    current_boot: str | None = None,
    hostname: str | None = None,
    force: bool = False,
    on_cleared: Callable[[], object] | None = None,
) -> dict[str, object]:
    """Classify and remediate the lease for recover takeover/abort under ONE flock.

    Closes the classify-then-mutate TOCTOU: the decision and the remediation
    (quarantine-rename or unlink) happen inside a single flock span, so a
    concurrent acquire cannot land between them. classify is lock-free
    internally, so calling it with the flock held is safe.

    Returns {"cleared", "state", "holder", "quarantined"}: live -> cleared
    False with the holder (unless `force`); corrupt -> rename to quarantine;
    free / expired_* (and live when `force`) -> unlink.

    `force` overrides the live-refusal so an operator-explicit abort can release
    a lease that still looks live (the recover abort --force escape hatch).
    takeover never passes it, so its refuse-on-live guarantee is unchanged.

    `on_cleared`, when given, runs WHILE the flock is STILL held, on the cleared
    paths only (never on a refused-live). It lets recover takeover do its stage
    resets + snapshot atomically with the clear, so a concurrent acquire cannot
    land between the unlink and the resets and have its just-begun stage
    clobbered. Same teardown-under-flock contract as classify_then: on_cleared
    must NOT call any lease function that re-takes this flock (it would
    self-deadlock). Its return value is discarded.
    """
    with flock_blocking(_flock_path(ticket_dir)):
        info = classify(ticket_dir, now_iso, current_boot=current_boot, hostname=hostname)
        lock_state = info["state"]
        if lock_state == "live" and not force:
            return {
                "cleared": False,
                "state": lock_state,
                "holder": info["holder"],
                "quarantined": None,
            }
        if lock_state == "corrupt":
            dst = _quarantine_locked(ticket_dir)
            if on_cleared is not None:
                on_cleared()
            return {"cleared": True, "state": lock_state, "holder": None, "quarantined": dst}
        run_lock_path(ticket_dir).unlink(missing_ok=True)
        if on_cleared is not None:
            on_cleared()
        return {
            "cleared": True,
            "state": lock_state,
            "holder": info["holder"],
            "quarantined": None,
        }


def classify_then(
    ticket_dir: Path,
    now_iso: str,
    teardown: Callable[[], object],
    *,
    current_boot: str | None = None,
    hostname: str | None = None,
) -> dict[str, object]:
    """Classify the lease under the flock; run teardown() while STILL holding it,
    iff the lease is non-live/non-corrupt. Closes the classify-then-mutate TOCTOU
    for an EXTERNAL teardown (e.g. `git worktree remove`): the decision and the
    mutation share one flock span, so a concurrent acquire cannot land between
    them. Returns {"torn_down": bool, "state": str, "holder": dict|None,
    "result": <teardown return>|absent}.

    Two non-obvious invariants the caller must respect:

    (i) flock non-reentrancy — teardown must NOT call any lease function that
    re-takes the flock (flock_blocking opens a fresh fd per call and LOCK_EX
    blocks across fds even within one process, so re-entering self-deadlocks).
    reap's teardown only runs a git subprocess, which is safe.

    (ii) doomed-inode — when teardown deletes the worktree containing the lock
    file, the held flock fd survives the unlink (POSIX): the file is gone from
    the namespace but the open fd keeps the inode alive until close. A later
    acquirer that recreates the lock path lands on a fresh inode and is no longer
    mutually excluded from this span, but that is safe: classify already observed
    non-live under the lock and the worktree is being torn down regardless.
    """
    with flock_blocking(_flock_path(ticket_dir)):
        info = classify(ticket_dir, now_iso, current_boot=current_boot, hostname=hostname)
        state = info["state"]
        if state in ("live", "corrupt"):
            return {"torn_down": False, "state": state, "holder": info["holder"]}
        result = teardown()
        return {"torn_down": True, "state": state, "holder": info["holder"], "result": result}


# ─── Internal write (flock already held) ──────────────────────────────────────


def _write_lease(
    ticket_dir: Path,
    *,
    run_id: str,
    boot_id: str,
    hostname: str,
    cwd: str,
    acquired_at: str,
    lease_expires_at: str,
    stage: str | None,
    session_nonce: str,
) -> Lease:
    lease = Lease(
        run_id=run_id,
        boot_id=boot_id,
        hostname=hostname,
        cwd=cwd,
        acquired_at=acquired_at,
        lease_expires_at=lease_expires_at,
        stage=stage,
        pid=os.getpid(),
        session_nonce=session_nonce,
    )
    atomic_write_text(run_lock_path(ticket_dir), _serialize(lease))
    return lease


# ─── CLI ─────────────────────────────────────────────────────────────────────


def _parse_args(argv: list[str]) -> argparse.Namespace:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--ticket-dir", required=True)

    parser = argparse.ArgumentParser(description="Per-ticket run lease (mutex).")
    sub = parser.add_subparsers(dest="command", required=True)

    p_acq = sub.add_parser("acquire", parents=[common])
    p_acq.add_argument("--run-id", required=True)
    p_acq.add_argument("--ttl-seconds", type=int, required=True)
    p_acq.add_argument("--stage", default=None)
    p_acq.add_argument("--now", default=None)
    p_acq.add_argument("--force", action="store_true")

    p_ref = sub.add_parser("refresh", parents=[common])
    p_ref.add_argument("--run-id", required=True)
    p_ref.add_argument("--ttl-seconds", type=int, required=True)
    p_ref.add_argument("--stage", default=None)
    p_ref.add_argument("--now", default=None)

    p_rel = sub.add_parser("release", parents=[common])
    p_rel.add_argument("--run-id", required=True)

    p_cls = sub.add_parser("classify", parents=[common])
    p_cls.add_argument("--now", default=None)

    p_stat = sub.add_parser("status", parents=[common])
    p_stat.add_argument("--now", default=None)

    return parser.parse_args(argv)


def _holder_payload(lease: Lease) -> dict[str, object]:
    return asdict(lease)


def cli_main(argv: list[str]) -> int:
    args = _parse_args(argv)
    ticket_dir = Path(args.ticket_dir).resolve()
    now_iso = getattr(args, "now", None) or utcnow_iso()

    if args.command == "acquire":
        try:
            lease = acquire(
                ticket_dir,
                args.run_id,
                args.ttl_seconds,
                now_iso,
                stage=args.stage,
                current_boot=boot_id(),
                hostname=socket.gethostname(),
                cwd=os.getcwd(),
                force=args.force,
            )
        except LeaseHeld as exc:
            sys.stdout.write(
                json.dumps({"error": "lease_held", "holder": _holder_payload(exc.holder)}) + "\n"
            )
            return 1
        except LeaseExpiredForeign as exc:
            sys.stdout.write(
                json.dumps({"error": "expired_foreign", "holder": _holder_payload(exc.holder)})
                + "\n"
            )
            return 5
        except LeaseError as exc:
            sys.stderr.write(f"lease acquire: {exc}\n")
            return 3
        sys.stdout.write(_serialize(lease))
        return 0

    if args.command == "refresh":
        try:
            lease = refresh(
                ticket_dir,
                args.run_id,
                args.ttl_seconds,
                now_iso,
                stage=args.stage,
                current_boot=boot_id(),
                hostname=socket.gethostname(),
                cwd=os.getcwd(),
            )
        except LeaseLost as exc:
            sys.stderr.write(f"lease refresh: {exc}\n")
            return EXIT_LEASE_LOST
        except LeaseError as exc:
            sys.stderr.write(f"lease refresh: {exc}\n")
            return 3
        sys.stdout.write(_serialize(lease))
        return 0

    if args.command == "release":
        try:
            removed = release(ticket_dir, args.run_id)
        except LeaseError as exc:
            sys.stderr.write(f"lease release: {exc}\n")
            return 3
        sys.stdout.write(json.dumps({"released": removed}) + "\n")
        return 0

    if args.command in ("classify", "status"):
        try:
            result = classify(
                ticket_dir, now_iso, current_boot=boot_id(), hostname=socket.gethostname()
            )
        except LeaseError as exc:
            sys.stderr.write(f"lease {args.command}: {exc}\n")
            return 3
        sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))


__all__ = [
    "EXIT_LEASE_LOST",
    "Lease",
    "LeaseError",
    "LeaseExpiredForeign",
    "LeaseHeld",
    "LeaseLost",
    "Runner",
    "acquire",
    "assert_lease_still_mine",
    "boot_id",
    "classify",
    "classify_then",
    "cli_main",
    "hostname",
    "is_expired",
    "quarantine_corrupt_lock",
    "read_lease",
    "refresh",
    "release",
    "run_lock_path",
    "takeover_clear",
]
