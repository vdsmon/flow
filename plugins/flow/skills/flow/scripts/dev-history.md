# Build history (archive)

Historical record, off the navigation path. For the current script map see `MODULE.md`; for API/contract tables see `inventory.md`. The phase-by-phase build narrative and the per-phase "known holes" lists live in `inventory.md` (kept there as the original build log).

## Status (moved out of SKILL.md)

This was the `## Status` section of `SKILL.md`. It is release-notes, not invocation guidance, so it was pulled out of the trigger-time prompt.

Phases 1-4 + 6 + 7-mvp + 7-full + 8-mvp + 8b-mvp + 8c + 5-mvp + 5b complete.
Phase 5b wired skill-handler dispatch (via `resolve_handler.py`), subagent stage reference docs (plan / implement / e2e), and the SessionStart recall hook.
Phase 7-full added the run-lease lifecycle and the canonical-snapshot TOCTOU defense (init acquires the lease + writes the snapshot; next refreshes the lease + verifies the snapshot; release drops the lease post-loop).
Phase 8c added `/flow status` (read-only run/stage/lease report) and `/flow recover` (lease takeover, failed-stage retry/skip/abort, snapshot reload).
Hung detection is post-hoc: there is no live poller, so `/flow recover` reads the lease state after a stage returns or on demand.
Phase 8d added the work-mode quality gate: `recall.py --metric tickets-per-week` (+ `--checkpoint --mode`), `/flow sync` (drain + reconcile pending tracker mutations), `/flow baseline` (time-to-PR baseline file + stats), `validate_postmortem.py` (postmortem schema + week-over-week trend), the commit content-ownership gate (`diff_extract.py check-ownership`), and the init checkpoint-mode + backend alignment matrix.
The skill is feature-complete for end-to-end `/flow do <ticket>` against bare and skill-bundled workspaces.
The head/tail split was later collapsed into a single background-agnostic session: spec enters the seeded worktree (`EnterWorktree`) and flows into the `do` pipeline in the same conversation instead of handing off to a fresh `claude --bg`; backgrounding (`/bg`) is a harness-level choice, so the `.bg-autofire-enabled` marker and the `--notify` flag are gone.

Still pending (deliberately deferred, not blocking):
- Deep ship-event reconciliation (duplicate / corrupt ship-event files; `/flow recover` flags them via `ship_event_attention` but does not auto-fix).
- ~~Live `baseline_collect` ingestion from Jira changelog + Bitbucket PR history~~ (retired 2026-07: the ±30% gate never shipped and nothing read the baseline; `baseline_collect.py` and the `/flow baseline` verb were removed, `percentile` lives in `metric.py`).
- Cross-project `/flow status --all` dashboard.
- Hunk-level commit ownership (current gate is filename-level).

## 2026-06 restructure

- Added `seam_check.py` (prose↔CLI seam checker) + `tests/test_seam_check.py`.
- Split the 612-line `SKILL.md` into a 205-line router + per-verb references (`references/verb-*.md`) keeping the do-loop skeleton + spec gate inline.
- Elevated the self-modification loop into `references/self-evolution.md`.
- Added this `MODULE.md` (live script map) + `dev-history.md`; widened `.gitignore` and untracked stray `hooks/__pycache__` bytecode.
