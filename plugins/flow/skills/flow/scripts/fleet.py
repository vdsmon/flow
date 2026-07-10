"""Fleet liveness ledger: one registration + heartbeat record per launched run.

Library + thin CLI. Stdlib-only. The spike (epic flow-8by2, child-1) found run
liveness today is a 5-source eventually-consistent join (leases, launch-ledger TTL
markers, PR/branch refs, bd status, jobs-dir scan) reassembled at ~6 sites. This
ledger is the single authority those readers will collapse onto in child-3. THIS
module (child-2) only writes it; nothing reads it authoritatively yet, so a wrong
or stale entry cannot affect a run (the shadow-write window).

Storage: one JSON file per key at `<shared .flow>/fleet/<key>.json`, where the shared `.flow` is
resolved by `_memory_paths.resolve_memory_base`, the SAME worktree->main redirect the memory store
uses (the gitignored `.flow/memory-root` sibling written at worktree bootstrap). So a per-stage
heartbeat from inside a worktree run and a register from the drain's main session both land in the
MAIN checkout's `.flow/fleet/`, durable across worktree teardown. This is the reason we do NOT
resolve via `maintainer.resolve_maintainer_repo`: in self-target mode that returns the WORKTREE (its
workspace.toml is a byte copy carrying self_target), so a heartbeat would write into the doomed
worktree inode. `resolve_maintainer_repo` is still used, but only as the maintainer GATE (off in
user projects).

Per-key flock on `<key>.lock` spans read->decide->atomic write, the `lease.py`
idiom; reads are lock-free over `atomic_write_text` (os.replace => old-or-new,
never torn). `register` is an idempotent upsert: it preserves `registered_at` and
bumps only `heartbeat_at`, so a launch register (run_id="") followed by per-stage
re-registers (real run_id) keeps the original launch time while refreshing
liveness. There is no separate run_id-gated heartbeat: gating the refresh would
no-op forever after a launch register set run_id="" (false-dead).

`live_keys` is the heartbeat-staleness fallback (a spike non-negotiable): a crashed run stops
refreshing, ages past STALE_AFTER_S, and drops from "live", so the drain never blocks forever on a
dead run. `deregister`/`deregister_run` is the positive removal leg: child-3 wires `deregister_run`
into dispatch_stage.cmd_finish so a cleanly-finished run drops out of the reconciled liveness read
at once instead of lingering until the staleness window; DNF/crashed runs (which keep their lease
but stop heartbeating) are still covered by `live_keys`' staleness fallback. The reconciled read
itself lives in `_evolve_common.fleet_live_keys` (lease | fleet).

CLI:
  fleet.py register   --key <K> [--run-id <R> --hostname <H> --boot-id <B>] --workspace-root <dir>
  fleet.py deregister --key <K> [--run-id <R>] --workspace-root <dir>
  fleet.py live-keys  --workspace-root <dir> [--json]
  fleet.py prune      --workspace-root <dir>
  fleet.py list       --workspace-root <dir> [--json]

Exit codes:
  0 = ok
  4 = not a maintainer setup (dormant; nothing to do)
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from pathlib import Path
from typing import cast

import lease
from _atomicio import atomic_write_text
from _locking import flock_blocking
from _memory_paths import resolve_memory_base
from _timeutil import parse_iso, ts_token, utcnow_iso
from maintainer import resolve_maintainer_repo

# A run that stops refreshing for longer than this ages out of live_keys even if it never
# deregistered (the staleness fallback). CAVEAT, load-bearing for child-3: the heartbeat fires only
# at cmd_next (stage TRANSITIONS), so a long intra-stage gap with no transition can exceed this flat
# window and read a LIVE run as dead, notably the merge stage's CI re-wait, which holds a session
# 20-40+ min between dispatch calls (flow-72d9, the bug this epic exists to kill). So child-3 must
# reconcile live_keys against the lease, never trust it alone; the real fix is an expiry-based
# staleness (lease's stage_timeout*60 + buffer) or an intra-stage heartbeat. 1800 also bounds the
# launch->init window now that the retired launch_ledger no longer carries its own TTL for it
# (flow-8by2.5).
STALE_AFTER_S = 1800


class NotMaintainer(Exception):
    """Raised when the run is not in maintainer mode. Exit 4."""


# ─── Paths ───────────────────────────────────────────────────────────────────


def _entry_path(fleet_dir: Path, key: str) -> Path:
    return fleet_dir / f"{key}.json"


def _lock_path(fleet_dir: Path, key: str) -> Path:
    return fleet_dir / f"{key}.lock"


def _entry_paths(fleet_dir: Path) -> list[Path]:
    try:
        return [p for p in fleet_dir.glob("*.json") if p.is_file()]
    except OSError:
        return []


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _age_seconds(now: str, ts: object) -> float | None:
    now_dt = parse_iso(now)
    ts_dt = parse_iso(ts)
    if now_dt is None or ts_dt is None:
        return None
    return (now_dt - ts_dt).total_seconds()


def _load(path: Path) -> dict[str, object] | None:
    """Read+parse an entry: the dict, or None (absent OR present-but-unparseable).

    Lock-free safe: atomic_write_text uses os.replace, so a concurrent reader
    sees the old or new file, never a torn one. `register` distinguishes an absent
    file from a corrupt one it should quarantine via `path.exists()`.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    return cast("dict[str, object]", data) if isinstance(data, dict) else None


def _quarantine_locked(fleet_dir: Path, key: str) -> Path | None:
    """Rename a corrupt entry to `<key>.json.quarantine.<ts>`. Caller holds flock."""
    src = _entry_path(fleet_dir, key)
    if not src.exists():
        return None
    dst = fleet_dir / f"{key}.json.quarantine.{ts_token()}"
    os.replace(src, dst)
    return dst


# ─── Core (dir-explicit; each mutation under the per-key flock) ───────────────


def register(
    fleet_dir: Path,
    key: str,
    run_id: str,
    *,
    now: str,
    hostname: str = "",
    boot_id: str = "",
) -> None:
    """Upsert the entry for `key`: preserve `registered_at`, bump `heartbeat_at`.

    Serves both launch (run_id="") and every per-stage refresh (run_id=real,
    last-writer-wins). A corrupt prior entry is quarantined, then treated as
    absent (a fresh `registered_at`).
    """
    path = _entry_path(fleet_dir, key)
    with flock_blocking(_lock_path(fleet_dir, key)):
        existing = _load(path)
        if existing is None and path.exists():
            # present but unparseable: quarantine for forensics, treat as absent.
            _quarantine_locked(fleet_dir, key)
        registered_at = now
        if existing is not None:
            prior = existing.get("registered_at")
            if isinstance(prior, str) and prior:
                registered_at = prior
        entry = {
            "key": key,
            "run_id": run_id,
            "registered_at": registered_at,
            "heartbeat_at": now,
            "hostname": hostname,
            "boot_id": boot_id,
        }
        atomic_write_text(path, json.dumps(entry, sort_keys=True))


def deregister(fleet_dir: Path, key: str, *, run_id: str | None = None) -> None:
    """Unlink the entry. `run_id`-gated: a stale run never drops a successor's
    registration (a non-empty mismatching run_id is a no-op). `run_id=None`
    unconditionally removes."""
    path = _entry_path(fleet_dir, key)
    with flock_blocking(_lock_path(fleet_dir, key)):
        if run_id is not None:
            existing = _load(path)
            if isinstance(existing, dict):
                cur = existing.get("run_id")
                if isinstance(cur, str) and cur and cur != run_id:
                    return
        path.unlink(missing_ok=True)


def live_keys(fleet_dir: Path, *, now: str, stale_after_s: int = STALE_AFTER_S) -> set[str]:
    """Keys whose `heartbeat_at` age < stale_after_s. The staleness fallback.

    Robust to a missing dir (empty) and to a corrupt/garbage entry (skipped).
    """
    live: set[str] = set()
    for path in _entry_paths(fleet_dir):
        d = _load(path)
        if not isinstance(d, dict):
            continue
        key = d.get("key")
        age = _age_seconds(now, d.get("heartbeat_at"))
        if isinstance(key, str) and key and age is not None and age < stale_after_s:
            live.add(key)
    return live


def prune(fleet_dir: Path, *, now: str, stale_after_s: int = STALE_AFTER_S) -> list[str]:
    """Unlink stale entries; return the pruned keys.

    Re-verifies staleness under the per-key flock before unlinking, so a key
    re-registered between the scan and the unlink is not dropped.
    """
    pruned: list[str] = []
    for path in _entry_paths(fleet_dir):
        d = _load(path)
        if not isinstance(d, dict):
            continue
        key = d.get("key")
        if not (isinstance(key, str) and key):
            continue
        age = _age_seconds(now, d.get("heartbeat_at"))
        if age is None or age < stale_after_s:
            continue
        with flock_blocking(_lock_path(fleet_dir, key)):
            fresh = _load(_entry_path(fleet_dir, key))
            if isinstance(fresh, dict):
                fage = _age_seconds(now, fresh.get("heartbeat_at"))
                if fage is not None and fage >= stale_after_s:
                    _entry_path(fleet_dir, key).unlink(missing_ok=True)
                    pruned.append(key)
    return pruned


def entries(fleet_dir: Path) -> list[dict[str, object]]:
    """All valid entries (corrupt ones skipped), for status/debug."""
    out: list[dict[str, object]] = []
    for path in _entry_paths(fleet_dir):
        d = _load(path)
        if isinstance(d, dict):
            out.append(d)
    return out


def read(fleet_dir: Path, key: str) -> dict[str, object] | None:
    d = _load(_entry_path(fleet_dir, key))
    return d if isinstance(d, dict) else None


# ─── Resolution + maintainer gate ─────────────────────────────────────────────


def resolve_fleet_dir(workspace_root: Path) -> Path:
    """The fleet dir under the shared (main) `.flow`, via the memory-root redirect."""
    return resolve_memory_base(workspace_root) / "fleet"


def _resolve(workspace_root: Path) -> Path:
    if resolve_maintainer_repo(workspace_root) is None:
        raise NotMaintainer("not a flow maintainer setup; no fleet ledger")
    return resolve_fleet_dir(workspace_root)


def register_run(
    workspace_root: Path,
    key: str,
    run_id: str,
    *,
    now: str | None = None,
    hostname: str | None = None,
    boot_id: str | None = None,
) -> bool:
    """Producer entry point (dispatch heartbeat + CLI register): maintainer-gated.

    Returns True if an entry was written, False when not in maintainer mode (a
    user project has no fleet). Raises only on a real IO error; the dispatch
    caller wraps this in a fail-open guard so a shadow-ledger fault can never
    break a run.
    """
    if resolve_maintainer_repo(workspace_root) is None:
        return False
    register(
        resolve_fleet_dir(workspace_root),
        key,
        run_id,
        now=now or utcnow_iso(),
        hostname=hostname or "",
        boot_id=boot_id or "",
    )
    return True


def deregister_run(workspace_root: Path, key: str, *, run_id: str | None = None) -> bool:
    """Clean-exit positive dereg (dispatch_stage cmd_finish + CLI): maintainer-gated.

    Returns True if a removal was attempted, False when not in maintainer mode.
    `run_id`-gated like the low-level deregister: a stale run never drops a
    successor's entry. The dispatch caller wraps this in a fail-open guard.
    """
    if resolve_maintainer_repo(workspace_root) is None:
        return False
    deregister(resolve_fleet_dir(workspace_root), key, run_id=run_id)
    return True


def is_live(workspace_root: Path, key: str) -> bool:
    """Fresh act-time liveness re-check for the drain's irreversible acts (flow-8by2.3):
    True if the key's lease classifies live/corrupt. Fail-safe: ANY read error returns
    True, so a re-check before reap merge+close / session-cleanup stop+rm never green-
    lights destroying a run that acquired a lease in the classify->act gap.

    LEASE-ONLY by design (NOT lease|fleet). The reap (merge a dead orphan) and cleanup (stop a done
    session) sites are always POST-lease, so the only liveness fleet adds over the lease is the
    launch->init window, which neither site is ever in. What an OR with the fleet term WOULD add
    here is harm: fleet's flat staleness (1800s) lingers long after a dead orphan's lease expired
    (~stage_timeout+buffer, ~900s), so it would read a reapable dead orphan as live and skip the
    very reap that exists to merge it.
    The lease's per-stage TTL is the accurate signal: a run that went live in the gap has
    a fresh lease and is caught here. (True atomic read+act under one flock is impossible
    for a prose `gh pr merge`; the child-4 merge-token that would have closed it was
    built then reverted, flow-8by2.4. This re-check NARROWS the worst TOCTOU from
    select-time to act-time, it does not close it.)

    Imports `lease` directly + inlines the worktree-pool glob (mirrors
    _evolve_common.run_dir_for, both `feat-`/legacy `feature-` dir prefixes) to
    avoid a fleet<->_evolve_common import cycle. The pool root resolves via
    `resolve_memory_base` (the same worktree->main redirect fleet storage uses),
    NOT `resolve_maintainer_repo`: in self-target mode the latter returns the
    WORKTREE (its workspace.toml byte copy carries self_target), whose own
    `.flow/worktrees/` is always empty, so a call from inside a worktree would
    read a possibly-live run as provably-not-live and invert the fail-safe.
    """
    # memory base is `<main>/.flow`; its parent is the main root, off which both
    # pool bases hang (`.claude/worktrees` mint + `.flow/worktrees` legacy).
    main = resolve_memory_base(workspace_root).parent
    bases = (main / ".claude" / "worktrees", main / ".flow" / "worktrees")
    matches = [
        m
        for base in bases
        for pat in (f"feat-{key}*", f"feature-{key}*")
        for m in glob.glob(str(base / pat))
    ]
    try:
        for wt in sorted(matches):
            run_dir = Path(wt) / ".flow" / "runs" / key
            if run_dir.exists():
                if lease.classify(run_dir, utcnow_iso()).get("state") in ("live", "corrupt"):
                    return True
                break
    except Exception:
        return True  # uncertain -> fail-safe toward "live"
    return False


# ─── CLI ─────────────────────────────────────────────────────────────────────


def cli_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Fleet liveness ledger (run registration + heartbeat)."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_reg = sub.add_parser("register", help="register/heartbeat a run for a key (upsert)")
    p_reg.add_argument("--key", required=True)
    p_reg.add_argument("--run-id", default="")
    p_reg.add_argument("--hostname", default="")
    p_reg.add_argument("--boot-id", default="")
    p_reg.add_argument("--workspace-root", default=".")

    p_dereg = sub.add_parser("deregister", help="drop a key's entry (run-id-gated)")
    p_dereg.add_argument("--key", required=True)
    p_dereg.add_argument("--run-id", default=None)
    p_dereg.add_argument("--workspace-root", default=".")

    p_live = sub.add_parser("live-keys", help="print keys with a fresh heartbeat")
    p_live.add_argument("--workspace-root", default=".")
    p_live.add_argument("--json", action="store_true")

    p_prune = sub.add_parser("prune", help="drop stale (un-heartbeated) entries")
    p_prune.add_argument("--workspace-root", default=".")

    p_list = sub.add_parser("list", help="print all entries")
    p_list.add_argument("--workspace-root", default=".")
    p_list.add_argument("--json", action="store_true")

    p_islive = sub.add_parser(
        "is-live", help="exit 0 if key's lease is live (fail-safe), 1 if provably not"
    )
    p_islive.add_argument("--key", required=True)
    p_islive.add_argument("--workspace-root", default=".")

    args = parser.parse_args(argv)

    # is-live is the drain re-check: it works regardless of maintainer mode (the lease
    # side always applies) and returns 0=live / 1=not-live, so it bypasses the exit-4
    # maintainer gate below.
    if args.cmd == "is-live":
        return 0 if is_live(Path(args.workspace_root), args.key) else 1

    try:
        fleet_dir = _resolve(Path(args.workspace_root))
    except NotMaintainer as exc:
        print(str(exc), file=sys.stderr)
        return 4

    if args.cmd == "register":
        register(
            fleet_dir,
            args.key,
            args.run_id,
            now=utcnow_iso(),
            hostname=args.hostname,
            boot_id=args.boot_id,
        )
        print(args.key)
        return 0
    if args.cmd == "deregister":
        deregister(fleet_dir, args.key, run_id=args.run_id)
        print(args.key)
        return 0
    if args.cmd == "prune":
        print("\n".join(sorted(prune(fleet_dir, now=utcnow_iso()))))
        return 0
    if args.cmd == "live-keys":
        keys = sorted(live_keys(fleet_dir, now=utcnow_iso()))
        print(json.dumps(keys) if args.json else "\n".join(keys))
        return 0
    # list
    items = entries(fleet_dir)
    if args.json:
        print(json.dumps(items, sort_keys=True))
    else:
        print("\n".join(sorted(str(e.get("key", "")) for e in items)))
    return 0


__all__ = [
    "STALE_AFTER_S",
    "NotMaintainer",
    "deregister",
    "deregister_run",
    "entries",
    "is_live",
    "live_keys",
    "prune",
    "read",
    "register",
    "register_run",
    "resolve_fleet_dir",
]


if __name__ == "__main__":
    sys.exit(cli_main(sys.argv[1:]))
