# DESIGN v10 (not yet implemented): opinionated model and effort defaults

Ticket: flow-0nnm. Nothing here is built. This file is the review subject, deliberately
untracked, and is not a repo artifact.

Review question: **is this the right thing to build?** Every factual claim is checkable
against this repository at `f8c4c4d`. Check them; do not accept them.

**Revision history**, each step forced by an independent Codex adversarial review:
v1's §3 refuted — a statically declared harness cannot track a launcher chosen at runtime;
v2 rewrote it as derive-from-launcher (§3.0). v2's registry structure refuted — the key
`roles` is already taken and load-bearing; v3 fixed §1 (§1.1). v3's §3.3 guarantee and its
universal effort default refuted — the first overclaimed, the second would have shipped a
policy that mostly cannot execute *and* a warning firing on flow's own defaults; v4 fixed
§3.3, §4, §5 and the warning's scope. v4's HANDLER re-read, CALLER channel and Codex
identifiers refuted — a dispatch/resolution TOCTOU, an overload of `FLOW_HARNESS`, and two
model names with no corroboration anywhere; v5 fixed §2, §3.1 and added four tests, and
v6 closed §2's identifier gap with maintainer-confirmed full names. v6's skippable CLI
probe and two unconsumed defaults refuted — a test that skips in CI validates nothing, and
two of the seven sites have no resolver call at all; v7 moves identifier validation to
`init` and adds the two missing calls plus a consumer-coverage guard. v7's accepted
unknown-handler risk and its init-time validation refuted — the first is a regression this
change introduces into a SUPPORTED configuration, the second cannot be performed at setup
without a remote inference call and misses the assessor path entirely; v8 makes unknown
launchers fail safe and moves identifier validation to a required Codex CI gate. v9 fixes
§4's REASON (raised by the maintainer, not a review pass): Claude Code has effort — a
session `effortLevel` and a per-call option on Workflow's `agent()` — but the Agent tool
flow dispatches through exposes no `effort` parameter. Conclusion unchanged, premise
corrected. v10 corrects it AGAIN, same section, same maintainer challenge: agent DEFINITION
frontmatter does carry `effort` (`claude-security/agents/patch-verifier.md` ships
`effort: xhigh`), and the Agent tool's own description says so. The exclusion is about the
CONFIG PATH only. Conclusion still unchanged across all three attempts; the reason was
wrong twice.

Untouched and unchallenged across seven passes: §1 location, §2's two-tier *structure*,
§3.1's launcher taxonomy, §6 resolution order, §7 driver rule, and the medium-not-high
argument. **The findings have moved from "wrong idea" to "wrong contract detail", which is
convergence.**

**Add a test for every refutation.** Each finding above was reachable by reading this
repository. If a claim here is not covered by a listed test, that is a gap, not a style
question.

---

## Problem

Flow ships no model or effort defaults. `[models]` in `workspace.toml` is override-only,
and its absence means "inherit the driver session model". Every unhinted launch site runs
on whatever the host happens to give it, and nobody chose that.

This repo's `[models]` has two bare-string entries, `implement = "sonnet"` and
`review_loop = "sonnet"`. Everything else inherits. The Codex reviewer and the plan
assessor therefore run at Codex's own defaults, observed during run flow-8b2o as
`gpt-5.6-sol` at `reasoning effort: medium`.

## Scope: seven launch sites, not ten stages

`validate_workspace._LAUNCH_SITES` is claimed authoritative and complete:

    plan/assessor, implement/implementer, code_review/reviewer, code_review/fixer,
    e2e/runner, review_loop/fixer, review_brief/author

Every other stage runs inline on the session model, where a default is inert. The driver
is an eighth decision but is not a stage.

## 1. Defaults live in `stage-registry.toml`, under `agent_defaults`

Beside `default_handler` / `default_timeout_min`. Entries are per launch role, because
roles within one stage differ.

    [[stage]]
    name = "code_review"
    roles = []                        # EXISTING field, untouched (see warning below)

    [stage.agent_defaults.reviewer]
    tier   = "deep"
    effort = "medium"        # reviewer can be Codex-launched, so effort is carriable

    [stage.agent_defaults.fixer]
    tier   = "standard"      # no effort: always native, and the Agent tool CALL carries
                             # no effort parameter, so a resolved value is dropped (§4)

### 1.1 Why not `[stage.roles.<role>]` — v2's error, and the vocabulary trap behind it

**`roles` is already taken and it means something else.** Every registry entry assigns
`roles` as an array (`stage-registry.toml` lines 35, 45, 56, 66, 77, 88, 98, 108, 118,
128), `_registry.StageEntry.roles` is a `list[str]`, and dispatch passes it into the
descriptor — `implement` carries `roles = ["records_diff_baseline"]`, which is what arms
the owned-file baseline guard before the handler runs.

So v2's `[stage.roles.<role>]` had two ways to fail and no way to succeed: `tomllib`
rejects it outright (`Cannot declare ('stage','roles','implementer') twice`), and
"fixing" that by replacing the array would silently disarm a correctness guard.

The underlying trap is that **"role" is overloaded in flow**: a *launch role*
(`assessor`, `reviewer`, `fixer`, `author`) is a different concept from a *dispatch role
marker* (`records_diff_baseline`). They share a word and nothing else. `agent_defaults`
is deliberately named for the launch-role sense so the two never collide again.

Verified against the real file, not a toy string: the registry has zero existing
`agent_defaults` / `tier` / `effort` keys; injecting the proposed blocks into the actual
`stage-registry.toml` parses cleanly; and `implement.roles` still reads
`["records_diff_baseline"]` afterwards.

## 2. Two tiers only

    claude-code:  standard = "sonnet"           deep = "opus"
    codex:        standard = "gpt-5.6-terra"    deep = "gpt-5.6-sol"

A model rename edits one table instead of seven entries. No haiku tier.

**Identifiers are FULL, never bare.** v4 wrote `sol` and `terra`; neither string appears
anywhere in this repository, and `codex exec --help` documents `-m, --model <MODEL>`
without enumerating values, so the CLI cannot settle it either. `gpt-5.6-sol` is
corroborated twice (brinta's `[models.code_review].reviewer`, and the model banner
observed during run flow-8b2o); `gpt-5.6-terra` is maintainer-confirmed.

A rejected `-m` does not fail loudly in the reviewer: the call retries without the model,
so the policy silently does nothing while string-composition tests stay green. In the
assessor it is worse — one `codex exec` call, no retry, so a bad identifier blocks planning.

The tier map is therefore **maintainer-curated data validated in a required CI gate with an
authenticated Codex**, covering both Codex-capable paths. Not at `init` (which can only
check presence, and would need a remote inference call to check acceptance), and not in a
skippable unit test. See Tests.

## 3. Harness is DERIVED from the effective launcher, never declared

### 3.0 Why v1 was wrong

v1 declared `harness = "codex"` per role in the registry. That is refuted. The effective
launcher is chosen **at runtime**:

- `init.py:462` — `if shutil.which("codex") is None: return ""`, so with no codex on PATH
  `code_review` keeps the native default.
- `init.py:456-460` — requires Codex on PATH **and** a Claude Code harness.
- `delivery-plan.md:90` — "a host-native agent is the fallback when Codex is unavailable".
- Handler overrides (`--handler code_review=...`) are a supported operator action.

A static declaration cannot see any of that. It would resolve `deep` → `gpt-5.6-sol` and
hand a Codex model name to a native launcher, which rejects or drops it — so the deep
default disappears exactly when the environment is already degraded, silently.

v1's mitigation was also aimed at the wrong pair: it kept `default_harness` beside
`default_handler` in the registry, but **the registry's `default_handler` is not what
runs**. The effective handler is written into `workspace.toml` by the init-time PATH
probe. Adjacency there guards two values, neither of which is the truth.

### 3.1 Three launcher kinds, one static fact each

There is no `harness` key in config anywhere. Each site's launcher kind is a fact about
flow's architecture, so it lives in code beside `_LAUNCH_SITES`, which becomes a mapping
rather than a set:

    plan/assessor          CALLER    # codex-assessor preferred, native fallback at runtime
    implement/implementer  HANDLER
    code_review/reviewer   HANDLER
    e2e/runner             HANDLER
    code_review/fixer      NATIVE
    review_loop/fixer      NATIVE
    review_brief/author    NATIVE

Each kind resolves its harness one way:

- **HANDLER** — the launcher harness is **bound into the dispatch descriptor** by
  `dispatch_stage.cmd_next` and passed to resolution as an immutable value. It is NOT
  re-read from `workspace.toml` at hint time.

  v4 said "re-read `[pipeline.handlers][stage]`" and that is a TOCTOU bug.
  `dispatch_stage.py:702` already copies `snapshot.handlers[next_stage]` into the
  descriptor and returns it at :713; the agent resolves its hint later. Reconfigure the
  workspace in between and the *old* handler is still running while resolution reads the
  *new* one — a dispatched Codex reviewer gets `opus`, or a native reviewer gets `gpt-5.6-sol`.
  The engine already treats this exact class as load-bearing: `dispatch_stage.py:653`
  carries a "TOCTOU: refuse if workspace.toml / registry / a handler plugin drifted"
  guard. Re-reading at hint time would reintroduce the race that guard exists to close,
  and the next dispatch's drift check is too late for the stage already in flight.

  The mapping itself is unchanged: a handler in the Codex-shelling set → `codex`;
  everything else, including `inline` and every host-native `subagent:` type → the parent
  harness. Only *when* it is computed moves — once, at descriptor construction.
- **NATIVE** — always the parent harness. No lookup. Prose fixes these: "launch one fresh
  **native** fixer" (`stage-code_review.md:138`, `stage-review_loop.md:101`), "inline or
  in one fresh **native** agent" (`stage-review_brief.md:15`).
- **CALLER** — the caller passes the harness of the agent it is **about to launch**,
  through a NEW argument that is not `FLOW_HARNESS`. One site only.

  This distinction is load-bearing and v4 fudged it. `codex-assessor.md:83` today calls
  the resolver as `FLOW_HARNESS="<harness>" "<facade>" model --stage plan --role assessor`,
  where `<harness>` comes from the driver's rooted prompt block and names the **parent
  host**. Under Claude Code that value is `claude-code` — while the assessor is about to
  shell to `codex exec`. An implementation that reads "the caller passes its harness" and
  reuses the existing channel resolves `deep` → `opus` and hands it to Codex. Exactly the
  bug this whole section exists to prevent, reintroduced through the one site that has no
  handler to derive from.

  So: `FLOW_HARNESS` keeps meaning "which host am I running under" and is never overloaded.
  A separate `--launcher-harness` supplies "which engine am I about to invoke". The bundled
  Codex assessor passes `codex`; the native fallback passes the parent harness.

Resolution: **harness = (kind-specific derivation) → tier through that harness's map →
model name.**

### 3.2 The Codex-shelling handler set

Exactly one entry today: `subagent:flow:codex-reviewer`. It is the only *handler* whose
agent invokes `codex exec`. (`codex-assessor` also shells to Codex but is never a
handler — it is the CALLER site.)

This is still name-based knowledge, but it is one canonical set in code, next to the
ownership map flow already keeps (`init.py` `_BUNDLED_CODEX_REVIEWER`,
`_BUNDLED_STAGE_AGENTS`, `_FLOW_OWNED_HANDLERS`), and **there is no second field that can
disagree with it**. That is the whole difference from v1.

### 3.3 What this buys, stated without the overclaim v3 made

The two-sources-of-truth residual risk from v1 is **gone**, not mitigated. There is one
source: the effective handler. That is the whole benefit, and it is worth having.

**v3 claimed more than that and was wrong.** It said a degraded environment "resolves
correctly by construction — no codex on PATH means the handler is native". That holds only
at *init*. `shutil.which("codex")` appears exactly once in the engine
(`init.py:462`) and `dispatch_stage.py` never re-probes, so an already-initialized
workspace keeps `code_review = "subagent:flow:codex-reviewer"` after Codex is uninstalled
or the checkout moves machines. That claim is deleted.

**Why this is still not a blocker for this design, stated so it can be checked rather than
taken.** With a stale Codex handler and no Codex on PATH, the stage fails identically with
or without this change:

- today: no hint → the reviewer omits `-m` → `codex exec` fails → missing reviewer → the
  stage fails (fail-closed, `codex-reviewer.md:143`, `stage-code_review.md:131`)
- with this change: hint `gpt-5.6-sol` → `codex exec -m gpt-5.6-sol` → fails at the same call, same way

The model hint is not on the failure path. This design neither causes nor worsens stale-
handler breakage, and fixing it means re-probing capability at dispatch or rebinding
Flow-owned handlers before launch — a change to when handlers are bound, which is a
different concern from what model they get. **Its own ticket** (see Out of scope), and it
should carry the test Codex proposed: init with Codex available, remove it from PATH
without reconfiguring, then require either an atomic native fallback or an explicit
pre-dispatch configuration failure.

## 4. Effort needs no tier indirection, but it is NOT universally consumable

`low|medium|high` needs no host mapping, so `effort = "medium"` resolves directly. That is
where the portability ends.

**Only a Codex launcher spends it.** `codex-reviewer.md:64-65` and `codex-assessor.md:87`
turn it into `-c model_reasoning_effort=<value>`.

Be precise about why the CONFIG path cannot reach a native site. Claude Code has effort in
at least three places, and two earlier drafts of this section got the reason wrong:

| mechanism | sets effort? | reachable from `[models]` / registry? |
|---|---|---|
| Agent tool **call** parameter | no — no such parameter | — |
| Agent **definition** frontmatter | **yes** (`effort: medium`) | no — static per agent type |
| Session `effortLevel` setting | yes, session-wide | no |
| Workflow `agent()` `effort` option | yes, per call | no — flow does not dispatch via Workflow |

Evidence for the frontmatter row, which is easy to miss: the Agent tool's own description
says "Each agent type's model, reasoning effort, and tools come from its definition
(`.claude/agents/*.md` frontmatter or SDK `agents`)", and a shipped example is
`claude-plugins-official/plugins/claude-security/agents/patch-verifier.md`, whose
frontmatter carries `effort: xhigh` beside `model: inherit`.

So the exclusion is about the **config path**, not about capability: flow resolves a hint
and then dispatches a `subagent:` type through the Agent tool, whose call takes no effort
parameter, so a resolved effort value has nowhere to go and is dropped
(`delivery-loop.md`: "apply a non-empty hint only when the current host supports it").

Three consequences:

1. Native sites are **not** at some unknown default. They inherit the driver session's
   `effortLevel`. On a session configured `high`, all five native sites run at `high` —
   the opposite of this design's medium intent.
2. **`plugins/flow/agents/implementer.md` and `e2e-runner.md` can fix that today, outside
   this design.** Both currently declare only `name` and `description`, so they inherit
   everything. Adding `effort: medium` to their frontmatter sets it for flow's two bundled
   native handlers. That is static per agent type rather than per workspace, so it is not
   a substitute for registry defaults — but it is the correct home for a fixed choice, and
   it is available now. Worth its own small ticket.
3. Frontmatter does **not** cover `subagent:general-purpose` (which brinta wires for both
   `implement` and `e2e`) or the prose-launched fixers and author, because flow owns no
   definition for those. Those sites remain session-inherited until the Agent tool call
   itself carries effort.

v3 set `effort = "medium"` on all seven sites anyway, which was incoherent in two ways at
once: most of the advertised policy would have had no runtime effect, and v3 *also*
proposed warning whenever an effort hint cannot apply — so flow's own shipped defaults
would have fired that warning permanently, on every native site, in every workspace. A
default that generates a standing warning about itself is a defect, not a policy.

**Effort defaults are therefore declared only where a Codex launcher is possible:**
`code_review/reviewer` and `plan/assessor`. Every other site omits `effort` entirely.

`code_review/reviewer` can still be native when an operator wires `code_review = "inline"`.
The hint is then dropped — but by the operator's own choice of handler, not by flow
shipping a default into a place that cannot use it. That asymmetry is the point, and it is
also why the warning must be scoped (see Also in scope).

## 5. Proposed defaults

No harness anywhere. Tier on all seven; effort only where a Codex launcher is possible:

| site | tier | effort |
|---|---|---|
| `plan/assessor` | `deep` | `medium` |
| `code_review/reviewer` | `deep` | `medium` |
| `implement/implementer` | `standard` | — |
| `e2e/runner` | `standard` | — |
| `code_review/fixer` | `standard` | — |
| `review_loop/fixer` | `standard` | — |
| `review_brief/author` | `standard` | — |

Note the consequence: `code_review/reviewer` at `deep` resolves to `gpt-5.6-sol` when the handler
is the Codex reviewer, and to `opus` when the handler is `inline` on Claude Code. Same
declared intent, correct vocabulary either way. That is the point.

## 6. Resolution order: four steps, three already exist

1. workspace `[models]` literal wins (unchanged)
2. `"off"` / `"none"` / `""` means inherit session — an explicit opt-out **from** the
   default (`OFF_VALUES` already exists)
3. key absent means registry default  ← **the only new step**
4. no registry default means inherit session (today's floor)

## 7. Driver tier is the manager's call, not config

Binds to the discriminator PR #602 shipped for the assessment: a design choice or two
plausible shapes → deep driver **and** run the assessment; one defect / one call site /
one test → standard driver **and** skip it. `manager.md` §Spawn's "never below opus for a
driver" is replaced.

## Why medium and not high

flow's Codex reviewer runs at Codex's default today, which is medium. At that effort it
found a case-sensitivity defect in a guard file with no witness anywhere in the suite, and
it found it with a case-fold mutation and a positive control rather than by reasoning
harder. The guardrails substitute for thinking budget. An earlier recommendation to raise
this to `high` was withdrawn on that evidence.

## Tests that must exist

- **absent vs `"off"`.** Both collapse to `""` in `model_resolve` today, and steps 6.2 and
  6.3 must distinguish them. Backwards means silently disabling a default or silently
  ignoring an opt-out; neither fails loudly. Both directions.
- **HANDLER derivation follows the workspace, not the registry.** Same registry
  `tier = "deep"`, two workspaces: `code_review = "subagent:flow:codex-reviewer"` resolves
  `gpt-5.6-sol`; `code_review = "inline"` under `claude-code` resolves `opus`. This is the test
  that would have caught v1.
- **NATIVE sites ignore the stage handler.** With `code_review = "subagent:flow:codex-reviewer"`,
  `code_review/fixer` still resolves through the parent harness, not Codex. A per-stage
  scheme passes every other test and fails this one.
- **Parent-harness sites actually follow the parent.** `implement` resolves `sonnet` under
  `claude-code` and `gpt-5.6-terra` under `codex`. Passes on unfixed code unless the two maps
  differ at that tier, so force the divergence.
- **Enforcement for the Codex-shelling set.** Every `plugins/flow/agents/*.md` that
  contains `codex exec` is either in the set or is a CALLER site. Today that grep returns
  exactly `codex-reviewer.md` and `codex-assessor.md`. This is a real test, unlike v1's
  adjacency.
- **CALLER, both branches, different results.** The bundled Codex assessor resolves `deep`
  → `gpt-5.6-sol`; the native fallback under `claude-code` resolves `deep` → `opus`. v4 had
  no CALLER test at all, so its own regression would have passed its own suite.
- **`FLOW_HARNESS` alone never selects the launcher vocabulary.** With
  `FLOW_HARNESS=claude-code` and `--launcher-harness codex`, resolution must return the
  Codex value. A implementation that overloads `FLOW_HARNESS` fails this and passes
  everything else.
- **Descriptor-bound harness survives reconfiguration.** Build a dispatch descriptor,
  rewrite `[pipeline.handlers]` in `workspace.toml`, then resolve: the active stage keeps
  the descriptor's vocabulary. This is the TOCTOU test; a re-read implementation passes
  every other test and fails this one.
- **Every shipped Codex identifier is accepted by the real CLI — validated where it cannot
  be skipped.** A string assertion cannot distinguish an accepted `-m` from one the CLI
  rejects and silently retries without. v6 proposed a test that skips when `codex` is absent
  from PATH; CI has no Codex, so that test would skip forever and the build would stay
  green while both identifiers went unchecked. A skippable check is the
  `scoped-gate-green` shape: exiting 0 proves nothing about what it read.

  v7 then moved validation to `init`, and that is also wrong for two reasons. First,
  `init.py:462` is only `shutil.which("codex")` — presence, not model acceptance — and
  `codex exec --help` exposes no model-validation command, so the only way to prove a model
  name at setup is a **remote inference call**. Normal workspace setup must not depend on
  one: it needs auth, costs money, and fails offline. Second, triggering on "the workspace
  wires the Codex reviewer" misses `plan/assessor`, which is an independent CALLER launch
  that prefers Codex **even when `code_review = "inline"`** — and `codex-assessor.md` makes
  exactly one `codex exec` call with no rejected-model retry, so a bad identifier there
  blocks planning outright.

  **Resolution: the tier map is maintainer-curated data, validated in a required CI gate
  with an authenticated Codex, covering every Codex-capable launch path — reviewer and
  assessor both.** Not at `init`, not in a skippable unit test. The unit suite keeps only
  the composition test, which is honest about what it covers. If that gate cannot be
  provided, the fallback is to ship the Codex tier map unset and let both sites inherit,
  rather than ship an unvalidated identifier.
- **Shipped defaults produce zero warnings.** Initialize a workspace with no `[models]`
  block at all, run the full validator, and assert the warning list is empty. This is the
  direct guard against v3's standing-noise defect: if flow's own defaults ever land
  somewhere that cannot consume them, this test says so instead of the operator's console.
- **Effort reaches the launch command, not just the resolver.** For `code_review/reviewer`
  with the Codex handler, assert `-c model_reasoning_effort=medium` appears in the composed
  `codex exec` invocation. Resolver-only assertions cannot tell a consumed hint from a
  dropped one, which is how v3's contradiction survived three drafts.
- **The existing `roles` array survives the schema change.** Parse the COMPLETE shipped
  `stage-registry.toml` (not a fragment) after adding every `agent_defaults` block, and
  assert `implement.roles == ["records_diff_baseline"]` still reaches the dispatch
  descriptor while defaults parse for all seven launch roles. This is the test that would
  have caught v2, and it must exercise the real file, because a fragment parses in
  isolation while colliding in place.

## Unknown launchers fail safe — no default at all

A workspace may wire a handler flow has never heard of (`code_review =
"subagent:acme:some-codex-thing"`). The handler grammar supports
`subagent:<plugin>:<type>`, so this is a **supported configuration, not operator error**.

Earlier drafts classified such a handler as native and accepted the wrong vocabulary as
residual risk. That was wrong, and it is worth being precise about why: today an unknown
handler receives **no hint at all**, because no `[models]` entry exists for it. Adding
registry defaults would newly inject `opus` into a handler that may expect a Codex
identifier. The change would *introduce* the breakage — a compatibility regression, not a
pre-existing gap it merely fails to fix. That is the opposite of the stale-handler case in
§3.3, where the failure is provably identical with and without this design.

**So: a handler whose launcher flow cannot classify gets NO registry default.** Resolution
falls through to step 4 — inherit the session model, exactly today's behaviour — and emits
a warning naming the handler. Flow-owned handlers are classified; anything else is unknown
until it declares itself.

If third-party Codex-shelling handlers ever need defaults, the extension point is an
explicit launcher-harness declaration on the handler, bound into the descriptor like any
other. Out of scope here, and not needed until someone wires one.

## Also in scope: two stage docs must learn to resolve their hint

**Two of the seven sites have no consumer today, so a default there would be dead config.**
Only `stage-code_review.md` invokes the resolver in prose (twice: reviewer and fixer).
Handler-launched sites are covered by `delivery-loop.md`'s generic recipe, and
`plan/assessor` by `codex-assessor.md:83`. But `review_loop/fixer`
(`stage-review_loop.md:101`, "fresh native fixer") and `review_brief/author`
(`stage-review_brief.md:15`, "one fresh native agent") name no hint role and run no
`model` command. Grep confirms zero resolver calls in either file.

Both gain the resolver call, mirroring `stage-code_review.md` exactly:

    FLOW_HARNESS="<harness>" "<facade>" model --workspace-root . --stage review_loop --role fixer
    FLOW_HARNESS="<harness>" "<facade>" model --workspace-root . --stage review_brief --role author

**And the generalizable guard, which matters more than the two fixes:** a check that every
site in `_LAUNCH_SITES` carrying a registry default has a consumer — a resolver invocation
in its stage doc, its agent definition, or the delivery loop. This is the third time in
this design that something declared turned out to be unconsumed (`required_fields`, then
effort on native sites, now these two). A declaration is not a consumer, and only a test
that looks for the consumer can tell them apart. `seam_check.py` already walks prose for
CLI invocations, so it is the natural home.

## Also in scope

`validate_workspace._warn_inline_stage_model` warns when a *model* pin cannot apply, and
nothing warns when an *effort* hint cannot. Add the symmetric warning, but **scope it to
explicit workspace configuration only — never to a registry default.** A warning exists to
tell an operator that something *they wrote* will not take effect; a shipped default
landing on a launcher that cannot use it is flow's problem to get right, not the
operator's to be nagged about. Unscoped, this warning plus §5's table is exactly the
standing-noise defect v3 walked into.

Also validate tier values against the closed set so a typo is a config error, not a silent
inherit.

## Out of scope

**Stale Flow-owned handlers after a capability change.** `shutil.which("codex")` runs only
at `init.py:462`; nothing re-probes. A workspace initialized with Codex keeps
`code_review = "subagent:flow:codex-reviewer"` after Codex is uninstalled or the checkout
moves machines, and the stage then fails closed with no runtime native fallback. Real, and
independent of this design (§3.3 shows the failure is identical with and without it).
Fixing it means changing *when* handlers are bound — re-probe at dispatch, or validate and
rebind Flow-owned handlers before launch. Its own ticket, with the test: init with Codex
available, remove it from PATH without reconfiguring, require either an atomic native
fallback or an explicit pre-dispatch configuration failure.

**No harness validation at dispatch.** `dispatch_stage.py` splits `subagent:<type>` and
passes `subagent_type` through unchecked, so a workspace wired for the wrong host fails at
spawn rather than at config time. Its own ticket.

*(v1 also listed "can a Codex driver resolve `subagent:flow:implementer`?" — resolved, not
a ticket. It cannot, and flow already handles it: `init.py:434-437` states Codex ships no
agents of its own, `.codex-plugin/plugin.json` declares `skills` only, and
`_compose_handlers` applies `_BUNDLED_STAGE_AGENTS` only under Claude Code, so a
Codex-hosted run keeps `subagent:general-purpose`.)*

## Hot

`stage-registry.toml` is in `triage._GUARD_FILES`, so every edit makes the diff HOT and
pulls the full verification lane plus the merge-time guard-property review.

---

## Verify against these files, do not reason about them

- `plugins/flow/skills/flow/scripts/model_resolve.py` — the entire current resolver
- `plugins/flow/skills/flow/scripts/_registry.py` — how `stage-registry.toml` is parsed.
  **Can it carry per-role tables? What does it do with unknown keys today?**
- `plugins/flow/skills/flow/stage-registry.toml` — an array of `[[stage]]` tables, every
  one already carrying a `roles` ARRAY. **Parse the real file with `agent_defaults` blocks
  injected and confirm both that it parses and that `implement.roles` is unchanged.** Are
  there other existing keys `agent_defaults`, `tier` or `effort` would shadow?
- `plugins/flow/skills/flow/scripts/validate_workspace.py` — `_LAUNCH_SITES`,
  `_validate_model_hints`, `_validate_stage_hint`, `_warn_inline_stage_model`
- `plugins/flow/skills/flow/scripts/init.py` — `_codex_reviewer_handler`,
  `_BUNDLED_STAGE_AGENTS`, `_FLOW_OWNED_HANDLERS`, `_compose_handlers`
- `plugins/flow/agents/codex-reviewer.md`, `codex-assessor.md` — how they spend the hint
- `plugins/flow/skills/flow/references/` — `stage-code_review.md` (reviewer branches at
  ~92, fixer at ~138), `stage-review_loop.md:101`, `stage-review_brief.md:15`,
  `delivery-plan.md:88-90`, `delivery-loop.md` model-hint section
- `.flow/workspace.toml` and
  `/Users/victordsm/bitbucket/brinta-data-platform/.flow/workspace.toml`

## Attack these specifically

1. Is the CALLER/HANDLER/NATIVE taxonomy in §3.1 complete and correct? Trace each of the
   seven sites to the code or prose that actually launches it. A site in the wrong bucket
   silently gets the wrong vocabulary.
2. Does `agent_defaults` shadow or collide with anything else the registry, `_registry.py`,
   or `validate_workspace.py` already reads? v2 died on exactly this class.
3. Can `resolve_agent_hint` express step 6 plus the kind-specific derivation without
   changing its return type, which existing callers depend on?
4. Is `[pipeline.handlers]` genuinely the effective launcher at resolve time, or can it
   diverge from what dispatch actually spawns?
5. Is the §7 driver rule checkable, or pure prose?
6. Do tier maps belong in a `_GUARD_FILES` file where every model rename makes a HOT diff?
7. Is two tiers enough, or does the binary lose something real?
8. Does resolution now need a workspace read on a path that previously did not have one,
   and does that break any caller's assumptions or performance?
