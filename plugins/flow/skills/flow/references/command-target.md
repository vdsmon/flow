# Cockpit and target commands

This reference owns bare `FLOW`, `FLOW <target>...`, and `FLOW help`. The rooted
execution contract in `harness.md` applies throughout: `<facade>` means the absolute
`<run_root>/.flow/runtime/flow`, invoked with an explicit workdir and call-local
`FLOW_HARNESS` selector.

## Bare cockpit

Bare `FLOW` is a read-only operational view, not an implicit delivery. Gather
durable evidence and render only non-empty sections:

1. active and stuck base or revision runs, including current stage, lease state, and
   PR when present;
2. deferred or blocked tickets with their saved question;
3. pending tracker mutations;
4. open PRs with actionable review feedback;
5. the smallest set of useful next `FLOW ...` invocations.

Use run state, leases, tracker state, pending mutation files, and forge
state as evidence. Host process or agent handles are advisory and must never override
durable state. The cockpit performs no repairs, tracker writes, launches, or cleanup.
If workspace discovery fails, show `FLOW workspace setup`; if the workspace is
healthy and every section is empty, say so plainly.

Normalize the joined evidence to an absolute temporary JSON file with `runs`,
`deferred`, `pending`, and `feedback` arrays, then use the shared renderer:

```bash
FLOW_HARNESS="<harness>" "<facade>" cockpit render --evidence "<absolute-evidence-file>"
```

Delete the temporary file after rendering.

## Target forms and precedence

Accepted target forms are:

- keys matching the configured tracker key patterns;
- an HTTP(S) Jira browse URL ending in `/browse/<key>`, when the extracted key matches
  the configured tracker key patterns;
- `ticket:<key>`, required when the key's literal spelling collides with a static
  namespace;
- `pr:<positive-number>`;
- a supported forge pull-request URL.

`ticket`, `memory`, `measure`, `workspace`, `scrutinize`, and `help` are always parsed
as static roots first. An unrecognized first token is an error, even if the tracker
might accept arbitrary strings. Resolve a PR through the forge seam, derive the
ticket from the head branch, and feed that ticket into the ordinary classifier. Never
fork a second PR-specific lifecycle.

## Read-only evidence probe

Normalize these sources before choosing an action:

- tracker existence, liveness, terminal state, and most recent saved question or
  decision;
- base run existence, health, failed or in-progress stage, and terminal receipt;
- base and revision lease ownership, expiry, and corruption;
- snapshot integrity and configuration/engine drift;
- revision sub-run existence and health;
- forge PR identity, state, and actionable unresolved feedback;
- ship-event integrity.

Gather it with this card, not with `--help` archaeology: sessions have been witnessed
spending their first minutes re-deriving these spellings with six or more probe calls,
including a deliberately bogus subcommand run just to dump the listing. Every line below is
seam-checked against the real CLI; paste what applies, skip what the target makes moot:

```bash
FLOW_HARNESS="<harness>" "<facade>" status --workspace-root . --ticket "<key>" --json
FLOW_HARNESS="<harness>" "<facade>" tracker --workspace-root . state --key "<key>"
FLOW_HARNESS="<harness>" "<facade>" tracker --workspace-root . get --key "<key>"
FLOW_HARNESS="<harness>" "<facade>" tracker --workspace-root . is-shipped --key "<key>"
FLOW_HARNESS="<harness>" "<facade>" forge --workspace-root . detect-pr --branch "<branch>" --state open
```

`status --json` carries the run, worktree, and lease view in one shot; the two tracker calls
give liveness and content; `is-shipped` reads the frozen ship event; `detect-pr` (repeat with
`--state merged` when the open probe is empty) settles PR identity. Derive `<branch>` from the
run state or the managed worktree; a fresh ticket has none and skips the forge line.

Do not mutate while probing. Missing evidence is not contradictory by itself.
Contradictions include two live owners, terminal tracker state with an active run that
cannot be explained by a revision, mutually incompatible PR receipts, or old and new
memory stores both containing data.

Write normalized evidence and the presence of `--request` to an absolute temporary
JSON file and invoke the shared reducer:

```bash
FLOW_HARNESS="<harness>" "<facade>" lifecycle reduce --evidence "<absolute-evidence-file>"
```

Delete the temporary file after parsing the compact result. The reducer returns one
of the actions below. Use that result exactly; do not add a prose-only priority rule
around it.

## Lifecycle actions

### `start`

The ticket is live and has no run. Read `delivery-plan.md`, produce the plan, cross
the single approval gate, bootstrap an isolated worktree, and continue through
`delivery-loop.md` in the same driver conversation.

### `answer`

The ticket is deferred or blocked and has a durable saved question. Without
`--request`, show the question and stop. With `--request`:

1. show the exact answer comment and reopen transition;
2. obtain confirmation because both are writes;
3. write the answer comment before reopening so a transition failure cannot erase
   the decision;
4. clear the human-input label when the tracker supports it;
5. re-probe and continue in the same invocation.

The durable answer stem must remain recognizable to later target handling. Do not
overwrite or delete the original question.

### `resume`

The run is healthy and incomplete. Reject `--request` once active scope has been
approved. Locate the authoritative worktree, bind `run_root` and `facade`, and enter
`delivery-loop.md` at the next pending descriptor. Do not repeat the approval gate.

### `running`

A foreign live lease owns the base or revision run. Show ticket, run/revision id,
holder identity when known, and lease age. Stop without polling or takeover. Direct
the operator to `FLOW workspace repair <target>` when they have evidence the holder
is dead.

### `repair`

The run is failed, stale, drifted, or corrupt. Read `delivery-repair.md`. Offer only
repairs supported by the observed evidence, confirm each write, apply one, then
re-probe. Continue when the reducer returns a healthy action. Abort and unresolved
ship-event corruption stop rather than looping.

### `revise`

The ticket has an open PR with actionable feedback, or `--request` supplies a change
to an open PR. Read `delivery-revision.md`; update the same branch and PR through a
revision sub-run. An instruction on a merged or closed PR is an error and requires a
new ticket.

### `show`

Render either the ready open PR or the terminal delivery receipt. This action is
read-only. Include the ticket, run, branch, PR URL/state, verification result, and
terminal timestamps available from durable evidence. Do not start another run.

When the receipt shows a merged PR whose close-out is incomplete — the ticket is not
terminal, the worktree still exists, or no frozen ship event is recorded — end the
render with the one useful next invocation: `FLOW ticket finalize <key>`. The pointer
is prose only; show itself still writes nothing.

### `conflict`

Name the contradictory sources and their paths or external identifiers. Preserve
all evidence. Do not guess a resource owner, delete a store, reset state, or offer a generic
force operation.

## Target options

- `--unattended` asks the driver not to prompt during an already approved,
  bootstrapped delivery. It conflicts with `--verify`. A fresh target always stops
  at the human plan gate without mutation; unattended mode cannot authorize planning.
- `--verify express|light|full` fixes the attended verification lane; left unset, the driver proposes a lane at the plan gate from the ticket's class and the human confirms it there (`references/delivery-plan.md` section 2). The tiers are execution profiles, not moods. `express` is for a ticket that is one defect, one call site, one test: the assessment is skipped by default and the plan records `skip: <reason>` or a named smoke check as its e2e recipe. `light` keeps one assessment pass and the planned recipe. `full` runs everything the workspace wires. A lane never re-wires a handler: a stage the workspace sets to `none` stays skipped in every lane, and a lane cannot re-enable it. The hot and high-stakes clamps to `full` live in `references/delivery-plan.md` section 2, the one home of the lane rule.
- `--e2e "<recipe>"` supplies the approved end-to-end recipe; persist it with the
  plan.
  Bootstrap refuses a recipe that expects a run when the workspace wires no `e2e`
  handler, so settle `skip: <reason>` there.
- `--request "<additional intent>"` answers a saved question or requests a revision
  on an open PR. Reject it on an active approved run and on terminal delivery.
- `--together` requests one coherent grouped run. All targets must be fresh, live,
  distinct, non-epic tickets with verified coupling.
For multiple targets without `--together`, attended mode asks whether to deliver
sequentially or as one coherent group. Unattended mode errors because that choice
changes run identity and review shape. Sequential delivery means one complete gate
and run per target. Grouped delivery uses one lead ticket, one plan, one diff, one PR,
and one close record per covered ticket.

After reducing every target independently, validate the coordination through the
same facade:

For together delivery, write a second absolute JSON evidence file. It has
`coupling_verified` and a `targets` array whose entries contain the exact `key`,
tracker-derived `live` boolean, and tracker-derived `epic` boolean. Preserve request
order. Coupling is verified only when the tickets describe one inseparable diff and
one coherent review; proximity, shared labels, or the order the human listed them in
is insufficient.

```bash
FLOW_HARNESS="<harness>" "<facade>" lifecycle coordinate \
  --action "<action-for-target-1>" [--action "<action-for-target-2>" ...] \
  [--together] [--unattended] [--choice sequential|together] \
  [--groupability-evidence "<absolute-groupability-file>"]
```

Only an attended answer may supply `--choice`. `--together` and `--choice` are
mutually exclusive: `--together` carries the mode already chosen in the public
request, while `--choice` answers a prior `needs_choice` result. Never pass both.
`--together` and the attended `together` choice both require groupability evidence;
the reducer rejects duplicate, non-live, epic, or unverified groups. Delete the
temporary file after parsing. The returned closed disposition is `direct`,
`needs_choice`, `sequential`, or `together`; do not infer another mode.

## Help

`FLOW help` renders registry-generated help. `FLOW help ticket|memory|measure|workspace`
filters to that namespace. Bare or incomplete namespaces are unknown commands; help
has no implicit aliases. Render it with the loaded registry CLI (a direct-bootstrap
call, since help must work before any workspace facade exists) and display its
logical `FLOW` output as-is, substituting the host spelling only at the conversation
boundary:

```bash
python3 "<skill_root>/scripts/public_commands_cli.py" help [<topic>]
```

Help and the complete grammar block are generated from `public-commands.toml`; do
not maintain a second handwritten command list here.
