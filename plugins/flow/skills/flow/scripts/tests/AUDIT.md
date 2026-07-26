# Test-deletion audit: read before deleting any test

This suite was minimized to a proven essential in the flow-qpgd pass (PR #555, ten
discovery+verify rounds; PR #557 then pinned every behavior the batteries showed was
unwatched). Every deletion in that history carries mutation-witness evidence in its commit
message, and the two files beside this one are the pass's living safety artifacts. They ship
with the plugin bundle deliberately (~100KB): the audit travels with the suite it protects.

## The deletion bar

A test may be deleted only when all three clauses hold, proven in a scratch copy of the
engine and recorded in the deleting commit's message:

1. **Named witness.** A concrete engine mutation that reds a *named* surviving test while
   the candidate's asserted behavior stays covered.
2. **No sole kill.** A search for any mutation of *existing* behavior that reds only the
   candidate. Finding one refutes the deletion — the candidate is that behavior's only
   witness. (Degenerate oracles that hardcode the candidate's own literals do not count,
   in either direction.)
3. **Post-deletion survival.** With the candidate removed, every mutation in the battery is
   still caught by a named test.

## The two artifacts

**`audit-survivor-registry.txt`** — every test cited as a covering witness in landed
deletion evidence. Deleting one silently orphans the deletions that cited it, so a registry
member needs the *entire citing chain* re-proven, not just its own bar. Entries are mostly
bare function names; later entries are `tests/file.py::name` qualified. Check a candidate
with a substring match on the function name:

    grep -F "test_the_candidate_name" audit-survivor-registry.txt

**`audit-refutation-record.txt`** — every adjudicated refusal, each with the distinguishing
mutation that saved it (e.g. `_truncate`'s width in [62,68] reds `test_colon_format_matches`
alone), plus the finder near-miss records from the final rounds. Re-proposing anything in
this record without new evidence is a hard miss — the burden is a fresh mutation battery
that overturns the recorded sole kill. The record cites `file.py:line` forms; line numbers
may have drifted, so anchor on names.

## Also protected

`tests/test_lease.py`, `tests/test_locking.py` (the witnessed-failure lease family),
`tests/test_harness_corpus.py` (frozen CI replay), every `test_live_*` pin, and any sole
coverage of the four correctness guards (run lease, snapshot TOCTOU guard, atomic
writes/quarantine, content-ownership commit gate) were never deletion-eligible in the pass
and remain off-limits without maintainer sign-off.

## Provenance

flow-qpgd (closed): suite 3,423 → 3,150 nodes across PR #555's evidence commits
(per-round convergence 5, 16, 11, 5, 2, 1, 2, 3, 2, 1); PR #557 added nine mutation-proven
pins for the zero-kill gaps the batteries exposed. The full protocol write-ups live in those
PR bodies and commit messages.
