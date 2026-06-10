"""Per-ticket run lease: a MUTEX preventing two concurrent /flow do on one ticket.

Library + thin CLI. Stdlib-only.

This is NOT a liveness checker. /flow dispatch runs as short subprocesses (init /
next / finish / release), each of which exits immediately, so there is no live
process to ping. Mutual exclusion comes from lease *identity* (the stable
per-ticket state.run_id, plus boot_id + hostname) compared under a flock, not
from pid liveness. The lease expiry is refreshed on the dispatch calls the agent
already makes; its TTL is tied to the current stage timeout so it survives a
multi-minute stage.

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


class LeaseError(Exception):
    """Base for lease acquisition failures."""


class LeaseHeld(LeaseError):
    """A live lease with a different run_id holds this ticket."""

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
    force: bool = False,
) -> Lease:
    """Acquire (or owner-re-acquire) the ticket lease under the flock.

    Branch order matters: run_id-match is checked before expiry so an owner can
    resume past expiry. Foreign cases split: live -> LeaseHeld; expired and
    boot differs (both boot ids truthy) -> reboot-clearable overwrite; expired
    and force -> overwrite; else expired -> LeaseExpiredForeign.

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
            )

        if existing.run_id == run_id:
            # owner re-acquire / resume: preserve acquired_at, move expiry/stage.
            return _write_lease(
                ticket_dir,
                run_id=run_id,
                boot_id=current_boot,
                hostname=hostname,
                cwd=cwd,
                acquired_at=existing.acquired_at,
                lease_expires_at=expires_at,
                stage=stage,
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
) -> Lease:
    """Refresh our own lease (move expiry/stage). LeaseLost if it is not ours.

    Raises:
        LeaseLost, LeaseError
    """
    expires_at = _expiry_iso(now_iso, ttl_seconds)
    with flock_blocking(_flock_path(ticket_dir)):
        existing = read_lease(ticket_dir)
        if existing is None or existing.run_id != run_id:
            raise LeaseLost(f"lease no longer held by run_id={run_id!r}")
        return _write_lease(
            ticket_dir,
            run_id=run_id,
            boot_id=current_boot,
            hostname=hostname,
            cwd=cwd,
            acquired_at=existing.acquired_at,
            lease_expires_at=expires_at,
            stage=stage,
        )


def assert_lease_still_mine(
    ticket_dir: Path,
    run_id: str,
    *,
    current_boot: str | None = None,
    hostname: str | None = None,
) -> None:
    """Raise LeaseLost if the lease is gone or no longer identifies as ours.

    Does NOT check expiry: the owner may legitimately resume a stage past
    expiry. Boot/hostname are checked only when provided.

    Raises:
        LeaseLost, LeaseError
    """
    lease = read_lease(ticket_dir)
    if lease is None:
        raise LeaseLost("run.lock is gone")
    if lease.run_id != run_id:
        raise LeaseLost(f"run_id mismatch: on-disk {lease.run_id!r} != {run_id!r}")
    if current_boot is not None and lease.boot_id != current_boot:
        raise LeaseLost(f"boot_id mismatch: on-disk {lease.boot_id!r} != {current_boot!r}")
    if hostname is not None and lease.hostname != hostname:
        raise LeaseLost(f"hostname mismatch: on-disk {lease.hostname!r} != {hostname!r}")


def release(ticket_dir: Path, run_id: str) -> bool:
    """Remove run.lock iff it is ours. Returns True if removed, False otherwise."""
    with flock_blocking(_flock_path(ticket_dir)):
        existing = read_lease(ticket_dir)
        if existing is None or existing.run_id != run_id:
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
) -> dict[str, object]:
    """Classify and remediate the lease for recover takeover under ONE flock.

    Closes the classify-then-mutate TOCTOU: the decision and the remediation
    (quarantine-rename or unlink) happen inside a single flock span, so a
    concurrent acquire cannot land between them. classify is lock-free
    internally, so calling it with the flock held is safe.

    Returns {"cleared", "state", "holder", "quarantined"}: live -> cleared
    False with the holder; corrupt -> rename to quarantine; free / expired_*
    -> unlink.
    """
    with flock_blocking(_flock_path(ticket_dir)):
        info = classify(ticket_dir, now_iso, current_boot=current_boot, hostname=hostname)
        lock_state = info["state"]
        if lock_state == "live":
            return {
                "cleared": False,
                "state": lock_state,
                "holder": info["holder"],
                "quarantined": None,
            }
        if lock_state == "corrupt":
            dst = _quarantine_locked(ticket_dir)
            return {"cleared": True, "state": lock_state, "holder": None, "quarantined": dst}
        run_lock_path(ticket_dir).unlink(missing_ok=True)
        return {
            "cleared": True,
            "state": lock_state,
            "holder": info["holder"],
            "quarantined": None,
        }


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
