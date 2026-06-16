# group verb

`/flow group [<key> ...] [--mine] [--filter open]`. Read-only. Proposes run-level
groupings (lead + covers) for the `covers` mechanism. It NEVER executes — the
output is the planning input a human approves, then runs via `spec --covers`.

The covers mechanism (one run co-delivers sibling tickets that are one coherent
change) needs a decision first: which tickets belong together, which is the lead,
which are duplicates. `group` produces that decision as a proposal; `spec`
consumes it. It is the front half of the arc:
`group` → `spec --covers` → `do` (one PR closing all) → `reflect`.

1. Resolve candidates + duplicate hints:
   ```bash
   python3 ${CLAUDE_SKILL_DIR}/scripts/group_candidates.py [<key> ...] [--mine] --workspace-root .
   ```
   - explicit keys → exactly those tickets.
   - `--mine` (no keys) → your assigned tickets matching `--filter` (default `open`).
   - Exit 0 → parse the JSON bundle `{candidates[], dup_hints[]}`.
   - Exit 1 → tracker read error; surface stderr, stop.
   - Exit 2 → workspace not initialized; surface the `/flow init` hint, stop.
   - Exit 3 → neither keys nor `--mine`, or the selector resolved nothing; ask which tickets to consider.

2. **Cluster** the candidates into coherent pieces of work. A cluster = tickets
   that would touch the SAME code or share a dependency — tickets that want ONE
   PR. Signals, strongest first:
   - explicit `links` of kind `blocks` / `depends_on` → a hard dependency EDGE
     (orders the cluster; a blocker is the lead or ships first).
   - shared `parent` (epic), or a shared `relates` link target — the delivery-ticket
     pattern (several tickets all relate to one umbrella task).
   - same subsystem named in the summaries/bodies: a shared form / entity / sheet,
     a shared component or path prefix.
   Tickets with no shared signal stay SOLO — independent work is sequential runs,
   not a group. Do not cluster by label or project alone.

3. **Pick the LEAD** per cluster: prefer `In Progress` > has a WIP branch > the
   most substantive body. The lead owns the run identity (lease / state / branch /
   memory); the rest become its `covers`. Order the covers by the dependency edges
   from step 2 — but note `covers` is a SET, not a sequence: the mechanism does not
   enforce per-cover order. A grouped run is ONE plan → ONE diff → ONE commit, so
   the edit ORDER within the run lives in the PLAN (settled at the gate), not in the
   covers list. If a cluster has a strict, load-bearing intra-order (edit A must land
   before B, each independently verifiable), that is the signal to NOT group it —
   stack sequential PRs instead. Grouping is for co-equal coupled changes that ship
   as one reviewable PR; strict ordering wants a stack.

4. **Resolve `dup_hints`.** Each hint is `{key, duplicate_of, title_overlap}` — an
   empty-body ticket whose title strongly overlaps a sibling. Confirm by reading
   both: an empty/near-empty body whose scope sits fully inside the sibling is a
   duplicate. Propose CLOSING it (link `duplicates <sibling>`, transition to the
   tracker's done state) — not covering it. A hint you cannot confirm stays a
   normal candidate.

5. **Verify file-overlap before proposing a group — the load-bearing check.** The
   coupling that matters most, shared files, is NOT in the tracker. For each
   proposed cluster, confirm the members really touch overlapping code: read the
   bodies for named paths/entities, and grep the repo for the form / entity /
   module they name. If they do NOT overlap, split them back to solo — grouping
   non-overlapping tickets buys nothing and bloats one PR for no reason. When you
   cannot confirm overlap, say so; never present an unverified group as ready.

6. **Emit the proposal** (read-only; the human runs it):
   - one table per cluster: lead, covers, dependency order, the coupling evidence.
   - the residual SOLO tickets (run individually).
   - the dup-close list (link `<dup> duplicates <keeper>` + transition the dup to
     the tracker's done state — a one-time terminal action the human runs in the
     tracker; not auto-executed).
   - a ready-to-run line per cluster:
     ```
     /flow <lead> --covers <c1>,<c2>,...
     ```
   Never invoke `spec` / `do` yourself. `group` proposes; the human approves and
   runs. The proposal is advice grounded in tracker structure + a file-overlap
   check, not an automatic launch.

7. **Persist the decision when you are NOT acting on it now (defer path).** A
   proposal you do not run immediately would otherwise live only in this output —
   and the costly part (which lead, which dups confirmed, file-overlap verified) is
   lost by next session. Record the cover set durably on the lead so `spec` picks
   it up later:
   ```bash
   python3 ${CLAUDE_SKILL_DIR}/scripts/group_persist.py persist \
     --lead <LEAD> --covers <c1>,<c2>,... --workspace-root .
   ```
   This writes a `flow-group covers:` marker comment on the lead (idempotent —
   re-persisting the same set is a no-op). It survives sessions / machines and is
   team-visible. Later, `/flow spec <LEAD>` with NO `--covers` auto-derives the set
   from that marker (verb-spec.md). On the inline path (you run `spec --covers` in
   this same session) persistence is unnecessary — the frontmatter `covers` the
   bootstrap stamps is the durable record from that point on.
