<!-- flow:activation-truth:begin -->
# Robustness: the load-bearing safety machinery

## Cognitive-worker failure containment

Every physical worker is isolated from the authoritative checkout in a standalone
exact-SHA clone. Intent is durable before clone and process launch. Cancellation is
acknowledged only after direct-child reap, process-group absence, and both stream EOFs.
Ambiguous termination or a Git postcondition mismatch quarantines evidence and forbids
replacement. Validated read-only completion removes the capsule; cleanup failure is a
hard artifact failure. The disposable E2E writer runs a write-capable capsule seeded with
the ticket's uncommitted working state, captures the recipe's own mutations as report
evidence, imports nothing, and is discarded. The three importing writers (implementer,
review_fixer, revision_fixer) run a write-capable capsule whose validated binary-aware
patch is compare-and-swap imported into the authoritative worktree under a sole-writer
claim, then disposed. machinery_fixer runs a read-only capsule that derives a report of
anchored {file, old, new} edits and mutates nothing; reflect applies each edit through the
machinery_edit guard, never the CAS import path.

The index CLAUDE.md's "Robustness (do not erode)" paragraph points at. Each mechanism below accreted from a real incident; the "witnessed failure" column is the proof it is load-bearing, not incidental complexity — mined from the `fix:` history so a reader no longer needs git archaeology to know which clauses are safe to touch. Simplify presentation, never the machinery.

The taxonomy is typed: **four correctness guards** (they make a wrong outcome impossible), on **one substrate** (the primitive they share), plus **one feedstock** (not a guard — the evidence stream self-evolution runs on). Older prose disagreed on the five-item membership (friction logging vs flock); this list supersedes both readings.

## Correctness guards

| Mechanism | Threat defended | Lives in | Witnessed failure (why it exists) |
|-----------|-----------------|----------|-----------------------------------|
| Run lease | Two sessions driving the same ticket's run at once — state races, doubled commits, a reap tearing down a live worktree. | `lease.py`: acquire / refresh / release / expiry + takeover detection; identity compared under flock, refreshed per dispatch call; per-session nonce on every `next`/`advance`/`release`. | A 2nd `the delivery loop` on a live ticket could re-acquire until the per-session nonce blocked it (#270). Reap classify raced worktree-remove until both ran under one flock span (#235). Recover takeover classified then remediated in separate lock spans — TOCTOU closed by classify+remediate under one flock (#211); abort/takeover further tightened against lease de-mutex (#283). |
| Canonical-snapshot TOCTOU guard | The workspace changing between dispatch calls mid-run: a workspace.toml edit, a plugin reinstall swapping handler code, the engine checkout advancing under a live run. | `snapshot.py`: content hash captured at `init`, recomputed and compared on every `next`. Components: workspace.toml text, stage-registry.toml text, each skill handler's manifest + tree hash, the engine's own tree while the main checkout sits on a protected branch. | A partial snapshot write could pass the guard until sha-written-before-json made it fail closed (#116). A spurious engine-drift abort was healed by re-verify + clean-tree re-anchor rather than loosening the guard (#367). |
| Atomic writes + quarantine | Torn or corrupt state files after a crash mid-write; destroying the evidence while recovering from one. | `_atomicio.py` (mkstemp + fsync + `os.replace` + parent-dir fsync — the rename itself is crash-durable). Quarantine sites: `state.py` (state.json + `.bak` ladder), `lease.py` (run.lock, inside the caller's flock), `_jsonl.py` (malformed lines to a sidecar). Never-destroy invariant: corrupt artifacts are renamed aside, never deleted. | The dispatcher surfaces a state.json backup-rollback as a `state_recovered_from_backup` marker instead of silently resuming off stale state (#292). |
| Content-ownership commit gate | A run committing changes outside its planned file set — including a change smuggled in via a rogue `git commit` mid-implement. | `diff_extract.py` `check-ownership`: refuses when the branch delta (dirty tree AND commits since `baseline.head_sha`) leaves `planned_files`. This is the mechanism CLAUDE.md means by "content-ownership commit gate". | Went branch-wide after committed out-of-baseline changes slipped past the dirty-tree-only check (#393). Refuses an out-of-scope staged rename by collecting the unowned source side (#366). |

## Substrate

| Mechanism | Role | Lives in |
|-----------|------|----------|
| flock | The cross-process serialization primitive every guard above sits on: lease identity checks, state read-modify-write, frontmatter updates, memory appends, and `machinery_edit.py`'s whole-edit serialization all hold one. | `_locking.py` (flock retry). The recurring lesson of the lease fixes above: classify and act under ONE flock span, never two. |

## Feedstock

| Mechanism | Role | Lives in |
|-----------|------|----------|
| Friction logging | Not a correctness guard — the append-only evidence stream self-evolution runs on. Every reflect-stage harness fix, the recurrence detector, and the fix-efficacy metric join off this file; a workaround that skips the log is invisible to the next run. | `flow_friction.py` → `friction.jsonl`; consumed by `reflect_inputs.py`, `friction_recurrence.py`, `friction_escalate.py`. |

## Hot-change tie-in

A change touching any of these files is a hot change: `triage.py`'s `_GUARD_FILES` is the authoritative set, `triage.is_hot_change` the decider, and the maintainer/stage prose is seam-checked against it. Hot rides the full lane and the guard-property review regardless of labels — see `command-maintain.md`.
