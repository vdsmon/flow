# inventory: API/contract reference + build log

> **Navigation.** The CURRENT script map is `MODULE.md`. Build status / release notes are in `dev-history.md`. This file keeps the API/contract tables (Jira REST mapping, beads CLI surface, `.flow-bundle.toml` schema, `state.json` schema) plus the phase-by-phase build narrative. The "Phase X" / "Known holes" sections below are archived history, not current status — read them as the build log, not as a description of how flow works today.

Live contract sections (grep the heading; everything else here is build log):

- §Jira API inventory + §Status normalization mapping + §HTTP error → exception / TransitionResult mapping
- §Forge (PR host) surface: operation surface, `[forge]`, `[agents]`, and legacy `[models]` workspace schemas
- §`.flow-bundle.toml` schema — discovery contract, composition rules, bootstrap markers
- §Beads CLI surface — subcommands, state normalization, transition synthesis, is_shipped contract
- §Dispatcher state machine — stage lifecycle, `state.json` schema, atomic-write contract, quarantine, exit codes, handler-descriptor shape, revision sub-run, TOCTOU invariant
- §Memory cohort — `memory_append` / `recall` / `memory_embed` + `[memory.semantic]` config
- §Integration layer — `tracker_cli` per-backend contract, descriptor extension, verb router

## Jira API inventory

Source: `~/.claude/skills/jira-workflow/{SKILL.md,references/*.md}` — the proven 8-stage pipeline that JiraAdapter must replicate as REST calls.

Distinct MCP Atlassian functions exercised: **7**.
Direct REST replacements listed below.
Anything in the Tracker Protocol not exercised by jira-workflow is marked **NEW** — implemented for cross-backend completeness and validated via mocks (no live jira-workflow precedent).

## Calls used by jira-workflow

| # | jira-workflow MCP function                 | call sites (refs/*.md)             | REST endpoint                                                              | Tracker Protocol method                       |
|---|--------------------------------------------|------------------------------------|----------------------------------------------------------------------------|-----------------------------------------------|
| 1 | `getAccessibleAtlassianResources`          | preflight.md:55 (init bootstrap)   | `GET https://api.atlassian.com/oauth/token/accessible-resources`           | constructor-time helper (not a Protocol method) |
| 2 | `atlassianUserInfo`                        | preflight.md:16 (init bootstrap)   | `GET /rest/api/3/myself`                                                   | constructor-time helper (not a Protocol method) |
| 3 | `getJiraIssue`                             | ticket.md:52, ticket.md:61         | `GET /rest/api/3/issue/{issueIdOrKey}?fields=...`                          | `get(key) -> Ticket`                          |
| 4 | `searchJiraIssuesUsingJql`                 | ticket.md:35, ticket.md:53, ticket.md:55 | `POST /rest/api/3/search/jql` (v3 paginated)                         | `list_assigned(filter)`, subtasks (folded into `get` ticket build) |
| 5 | `getJiraIssueRemoteIssueLinks`             | ticket.md:54                       | `GET /rest/api/3/issue/{issueIdOrKey}/remotelink`                          | folded into `get(key).links` field            |
| 6 | `getTransitionsForJiraIssue`               | planning.md:11                     | `GET /rest/api/3/issue/{issueIdOrKey}/transitions?expand=transitions.fields` | `list_transitions(key) -> list[Transition]`  |
| 7 | `transitionJiraIssue`                      | planning.md:11                     | `POST /rest/api/3/issue/{issueIdOrKey}/transitions`                        | `transition(key, transition_id, fields) -> TransitionResult` |

JQL used:
- assigned filter: `assignee = currentUser() AND statusCategory != Done ORDER BY updated DESC`
- subtasks: `parent = <KEY>`
- linked: `issue in linkedIssues(<KEY>)`

## Tracker Protocol surface NOT exercised by jira-workflow

These are required by the Tracker Protocol for cross-backend parity.
No reference in jira-workflow — implemented from Atlassian REST API v3 docs + Agile REST API.

| Protocol method            | REST endpoint                                                                  | Notes |
|----------------------------|--------------------------------------------------------------------------------|-------|
| `create`                   | `POST /rest/api/3/issue`                                                        | Body: `fields: {project, issuetype, summary, description (ADF), parent, labels, assignee, priority}`. |
| `comment(body)`            | `POST /rest/api/3/issue/{key}/comment` `{body: <ADF>}`                          | ADF v3 required |
| `link(from,to,kind)`       | `POST /rest/api/3/issueLink` `{type:{name:<mapped>}, inwardIssue:from, outwardIssue:to}` | seam kind→Jira name: `blocks`/`depends_on`→`Blocks`, `relates`→`Relates`; unknown passes raw. Direction: `inwardIssue`=from=the blocked/dependent issue, `outwardIssue`=to=the blocker. |
| `state(key)`               | `GET /rest/api/3/issue/{key}?fields=status,resolution`                          | derives `TicketState` with normalized + diagnostic |
| `project_requires_pr()`    | `GET /rest/api/3/workflow/search?projectKey=<P>&expand=transitions.rules` (workflow scheme) | flag iff any transition to Done category has linked-PR validator. **Conservative default = False** if endpoint unauthorized. |
| `is_shipped(key)`          | PURE READ: frozen `.flow/<ns>/ship-events/<key>.json` → return shipped; else `state()` + ship predicate | adapter MUST NOT write |
| `set_sprint(key, sprint_id)` | `POST /rest/agile/1.0/sprint/{sprintId}/issue` `{issues:[key]}`                | capability: `sprints` |
| `list_sprints(project)`    | `GET /rest/agile/1.0/board/{boardId}/sprint?state=active,future,closed` (needs board lookup) | capability: `sprints` |
| `get_attachments(key)`     | `GET /rest/api/3/issue/{key}?fields=attachment`                                 | capability: `attachments` |

## Capabilities advertised by JiraAdapter

Closed enum (`tracker.py:CAPABILITY_ENUM`).
All `supported=true` for Jira Cloud:

```
comments_adf=true, comments_markdown=false, attachments=true, watchers=true,
sprints=true, fix_versions=true, components=true, epic_link=true,
pr_links=true, ci_links=true, boards=true, custom_fields=true,
transitions_with_validators=true, resolutions=true
```

`comments_markdown=false` is intentional.
Jira Cloud's comment API requires ADF; markdown round-trips lose formatting.
Callers MUST send either:

- `Content{fmt="adf"}` — body is a pre-built ADF JSON string. Adapter parses + sends as-is.
- `Content{fmt="plain"}` — adapter wraps as single-paragraph ADF: `{"type":"doc","version":1,"content":[{"type":"paragraph","content":[{"type":"text","text":body}]}]}`.

`Content{fmt="md"}` is COERCED to plain text (lossy): the adapter wraps the raw markdown body as a single ADF paragraph, same as `fmt="plain"`.
No heuristic md→ADF conversion; markdown syntax (headings, lists, code fences) renders verbatim in the Jira UI. Lossy rendering is accepted so autonomous flow comments (which wrap bodies as `fmt="md"`) don't hard-fail on Jira.

## Status normalization mapping

`TicketState.normalized` is derived from Jira's `status.statusCategory.key` (the 3-bucket category: `new` / `indeterminate` / `done`) combined with native status string heuristics:

| Jira statusCategory.key | Jira native status (case-insensitive) | NORMALIZED_STATES |
|-------------------------|---------------------------------------|--------------------|
| `new`                   | *                                     | `open`             |
| `indeterminate`         | contains "block" / "hold" / "wait"    | `blocked`          |
| `indeterminate`         | contains "review" / "qa" / "merge"    | `in_review`        |
| `indeterminate`         | *                                     | `in_progress`      |
| `done`                  | resolution == "Won't Do" / "Cancelled" / "Duplicate" / "Won't Fix" | `cancelled` |
| `done`                  | *                                     | `done`             |

`adapter_mapping_diagnostic` records which rule fired (e.g.
`"category=indeterminate + native='In Review' matched in_review heuristic"`)
so dashboards can audit unexpected categorizations.

## Authentication

**Basic auth with API token**, per user decision.
Adapter reads:

- `ATLASSIAN_EMAIL` — Atlassian account email (the username for basic auth)
- `ATLASSIAN_API_TOKEN` — token from `https://id.atlassian.com/manage-profile/security/api-tokens`

Auth header: `Authorization: Basic base64(email:token)`.

Adapter raises `TrackerConfigError` at construction if either env var is missing or empty.

`cloud_id` is taken from `workspace.toml` ([tracker.jira].cloud_id) — cached at init time via `getAccessibleAtlassianResources`.
Not re-queried per request.

## HTTP error → exception / TransitionResult mapping

All `_request()` responses flow through one classifier.
This table is the contract — every Jira REST call returns one of these outcomes.

| Status | Endpoint family            | Body signal                                                | Outcome                                                                                  |
|--------|----------------------------|------------------------------------------------------------|------------------------------------------------------------------------------------------|
| 2xx    | any                        | —                                                          | success — return parsed JSON                                                             |
| 401    | any                        | —                                                          | raise `TrackerConfigError("invalid credentials: check ATLASSIAN_EMAIL/ATLASSIAN_API_TOKEN")` |
| 403    | `/transitions` (POST)      | —                                                          | return `TransitionResult{success=False, failure_kind="permission_denied", failure_detail=msg}` |
| 403    | other                      | —                                                          | raise `TrackerError("forbidden: {endpoint}: {msg}")`                                     |
| 404    | `/issue/{key}` (any)       | —                                                          | raise `TrackerError("ticket not found: {key}")`                                          |
| 404    | other                      | —                                                          | raise `TrackerError("endpoint not found: {path}")`                                       |
| 400    | `/transitions` (POST)      | `errorMessages` contains "transition" + "not valid"        | return `TransitionResult{failure_kind="wrong_source_state"}`                             |
| 400    | `/transitions` (POST)      | `errors` has required-field keys                           | return `TransitionResult{failure_kind="missing_required_field", failure_detail=keys}`    |
| 400    | `/transitions` (POST)      | `errorMessages` contains "validator" / "validation"        | return `TransitionResult{failure_kind="validator_failed"}`                               |
| 400    | `/transitions` (POST)      | other 400                                                  | return `TransitionResult{failure_kind="validator_failed", failure_detail=raw_message}` (catch-all) |
| 409    | mutation (PUT/POST)        | —                                                          | raise `TrackerError("conflict: {body}")` — caller writes to `pending-mutations.jsonl`    |
| 429    | any                        | `Retry-After` header                                       | sleep + retry up to 3× then raise `TrackerError("rate-limited after 3 retries")`         |
| 5xx    | any                        | —                                                          | retry up to 2× (exponential 1s/3s); raise `TrackerError("upstream 5xx: {status}")` if persists |

`ambiguous_transition` is a CLIENT-side classification: when `list_transitions()` returns multiple entries sharing the same `name`, callers see them all and MUST select by id.
If a caller passes a `name` that resolves to >1 id, that's a client-side error; the Protocol contract is strictly id-keyed (see tracker.py docstring for `Transition.id`).
The Jira REST call itself never reports "ambiguous_transition" — it just runs whichever id was sent.

Status normalization to `TransitionFailureKind` happens in `_classify_transition_error(response_json) -> TransitionFailureKind`.
Regex patterns for 400-body signal detection:

```python
_RE_WRONG_SOURCE  = re.compile(r"(?i)\btransition\b.*\b(not valid|invalid|cannot be applied)\b")
_RE_VALIDATOR     = re.compile(r"(?i)\bvalidat(or|ion)\b.*\b(fail|error|reject)\b")
_RE_REQUIRED_HINT = re.compile(r"(?i)\b(required|must be)\b")
```

`errors` dict (key-by-fieldname) takes precedence over `errorMessages` list when both are present — `errors` is structured and unambiguously identifies missing fields.

## Board strategy for `list_sprints(project)`

Jira sprints belong to boards, not projects.
Adapter resolves:

1. `GET /rest/agile/1.0/board?projectKeyOrId={project}&type=scrum`
2. Pick the **first active scrum board** returned.
3. `GET /rest/agile/1.0/board/{boardId}/sprint?state=active,future,closed&maxResults=50`

If step 1 returns zero boards → raise `NotSupported("no scrum board configured for project={project}")`.
If multiple boards exist → adapter picks first, logs a diagnostic.
Callers needing deterministic board selection should set `tracker.jira.board_id` in `workspace.toml` (future enhancement; not phase 3).

## Forge (PR host) surface

Pluggable PR-host seam (`forge.py` Protocol + `forge_cli.py` + `forge_github.py` + `forge_bitbucket.py`), structural twin of the tracker seam. Selected by `[forge] backend` in `workspace.toml`; the block is OPTIONAL (absent = no forge, `create_pr`/`review_loop` stay `none`).

`create_pr` takes an authored PR body from the stage (`references/stage-create_pr.md`) via `--body-file`: it runs `pr_body.scrub` (em-dash → punctuation, sentence-case `# Heading`, flatten `- **Term:**` bullets) as a de-AI floor, on a bitbucket forge flattens `<details>` wrappers to `###` headings (`pr_body.flatten_details`; Bitbucket renders no raw HTML in markdown) and appends the deterministic `Closes` footer (`pr_body.closes_footer`, extracted from the HEAD commit trailer), then runs `pr_body.enforce_cap` as a deterministic size net (shrink largest fenced blocks → drop `<details>` bodies keeping `<summary>` → hard-truncate; cap ~32000, the stricter forge floor) so an oversized `## Evidence` body can never fail `open_pr`. With no `--body-file` it falls back to the old commit-derived body (`pr_body.build_body`: strip the `ticket:`/`files:` trailer, keep `Closes <KEY>` as a footer, unwrap prose hard-wraps). On first open it calls `set_default_reviewers` (swallowing `NotSupported` + any `ForgeError` so a reviewer hiccup never fails an open PR). The `default_reviewers` capability is `True` on Bitbucket, `False` on GitHub (the first `supported=false` capability in a live adapter).

### Operation surface (forge_cli subcommand → gh / bkt)

| Op (Protocol / `forge_cli`) | GitHub (`gh`) | Bitbucket (`bkt`) |
|------|------|------|
| `detect_pr` / `detect-pr` | `gh pr list --head B --state open --json number,url,isDraft,baseRefName,headRefName,state` | `bkt api 2.0/repositories/WS/RS/pullrequests?state=OPEN` + filter `source.branch.name` |
| `pr_info` / `pr-info` | `gh pr view PR --json number,url,isDraft,baseRefName,headRefName,state` (PR-number reverse lookup, ANY state — revise reads `head`+`state`/detects MERGED; None on empty/garbage JSON, ForgeError on absent PR) | `bkt api .../pullrequests/PR` → `_pr_from_api` (None on empty body) |
| `open_pr` (lib-only; no forge_cli subcommand — create_pr.py drives it) | `gh pr create --base --head --title --body [--draft]` | `bkt api .../pullrequests -X POST -d {title,source,destination,draft,description}` |
| `ci_rollup` / `ci-rollup` | `gh pr view PR --json statusCheckRollup` (green = non-empty + every check COMPLETED-SUCCESS) | `bkt pr checks PR` → Pipeline line state (SUCCESSFUL→green, INPROGRESS→pending, FAILED/STOPPED/ERROR→failed) |
| `review_threads` / `review-threads` | `gh api graphql` — unresolved threads, normalized (drops resolved) | CodeRabbit actionable inline findings via paginated `.../comments`, unresolved only |
| `bot_review_present` / `review-status` | **NotSupported** (no review bot on the GitHub self-target; degrades to `{"supported": false}`) | `bkt pr checks` CodeRabbit line → true on any terminal state (SUCCESSFUL/FAILED/STOPPED/ERROR = the review bot has finished); the mandatory pre-thread-poll gate in `stage-review_loop.md` §3 |
| `post_reply` / `post-reply` | `gh api graphql addPullRequestReviewThreadReply` | `bkt api .../comments -X POST -d {content.raw, parent.id}` |
| `resolve_thread` / `resolve-thread` | `gh api graphql resolveReviewThread`; returns bool `isResolved` | `POST .../comments/CID/resolve` then re-fetch + verify `.resolution != null` |
| `mark_ready` / `mark-ready` | `gh pr ready PR` | `bkt api .../pullrequests/PR -X PUT -d {draft:false}` |
| `merge` / `merge` | `gh pr merge PR --squash` | `bkt api .../pullrequests/PR/merge -X POST -d {merge_strategy:squash}` |
| `delete_branch` / `delete-branch` | `git push origin --delete B` | `git push origin --delete B` |
| `set_default_reviewers` (no `forge_cli` subcommand; `create_pr` calls the adapter directly) | **NotSupported** (solo repo, CODEOWNERS covers reviewers) | `GET 2.0/user` (resolve author) + `GET .../default-reviewers`, drop author by `account_id`, `PUT .../pullrequests/PR -d {reviewers:[{uuid}...]}` |

Cap-gated ops (`review-threads`/`review-status`/`post-reply`/`resolve-thread`/`mark-ready`/`delete-branch`) degrade on `NotSupported` to `{"supported": false}` exit 0. Exit codes: 0 ok / 1 transient forge error / 2 config invalid (incl. no `[forge]`) or malformed argv (argparse) / 3 adapter-rejected argument value.

### Bitbucket comment-resolve gotchas (ported from ship-it; do NOT re-derive)

- `POST .../comments/<CID>/resolve` is the resolve endpoint; the `links.resolve` rel is often absent — never gate on it.
- Success returns a `comment_resolution` object with NO top-level `resolved:true`. Judge success by re-fetching the comment and testing `.resolution != null`.
- Only top-level inline comments (`parent == null`) can be resolved; replies cannot.

### `[forge]` workspace schema

```toml
[forge]
backend = "github"   # or "bitbucket"

[forge.github]        # github needs no sub-keys

[forge.bitbucket]     # bitbucket REQUIRES both
workspace = "ws"
repo_slug = "rs"
```

`validate_workspace.py` validates the block only when present (`KNOWN_FORGE_BACKENDS = ("github", "bitbucket")`); github needs no sub-keys, bitbucket requires `workspace` + `repo_slug`.

### `[agents]` route schema

Every explicit route is a complete `harness`, `model`, and `effort` triple. Public
harness values are `claude_code` and `codex`. A profile defines either one common
route or a `by_owner` table; mixing them or omitting a field is invalid.

```toml
[agents.planner]
harness = "codex"
model = "gpt-5.6-sol"
effort = "xhigh"

[agents.implementer.by_owner.claude_code]
harness = "claude_code"
model = "sonnet"
effort = "high"

[agents.implementer.by_owner.codex]
harness = "codex"
model = "gpt-5.6-luna"
effort = "high"
```

Resolution precedence is a complete per-run `--route` tuple, explicit workspace
route, standalone legacy mode, then built-in defaults. Bootstrap freezes the
canonical route snapshot. Claude Code activates a desired post-plan route only when
its structured native launch response accepts the exact model and effort. Current
Codex post-plan routes remain shadowed and inherit their owner model.

`agent_routes.py` owns resolution, snapshot digests, attestations, and the surgical
`migrate --check|--apply` operation. Migration leaves `[models]` bytes intact so
removing `[agents]` restores legacy behavior.

Configured, built-in, and overridden planner routes enter the strict read-only CLI
path. Exact capability, authentication, provider-schema acceptance, and launch receipt
evidence is required before activation. Failure stops visibly without selecting a
fallback route. `snapshot --workspace-config` resolves from bytes read at the fetched
base SHA instead of ambient checkout state.

### Pre-approval planning schemas

`planning_attempt.py` owns six canonical, digest-bearing artifacts:

| Schema | Purpose |
|---|---|
| `flow.plan-envelope/v1` | Complete planner result with attempt/version/parent CAS, base, route-bound author id, status, required review fields, typed questions, and incorporated feedback ids. Provider objects are closed. Duplicate lists are rejected by Python after provider parsing. |
| `flow.planning-attempt/v1` | Ephemeral review bundle with plan history, visible feedback ledger, assessment, and revalidation. Mutations lock the complete load/CAS/save transaction, and the bundle never stores a worker thread receipt. |
| `flow.plan-assessment/v1` | Author-separated assessor outcome and findings bound to the exact plan digest, actual author id, and required-fresh assessor launch receipt |
| `flow.plan-revalidation/v1` | Approved/latest base and exact changed/planned/context paths classified as unchanged, unrelated, relevant, or ambiguous |
| `flow.plan-gate/v1` | Plan version/digest, approved SHA, route digest, unique planner-launch receipt, feedback watermark, assessment, and revalidation digests |
| `flow.plan-approval/v1` | Host-native gate id, exact gate tuple, and approved plan-file digest consumed by bootstrap. Approval must present the exact digest returned by `gate`. |

Only `PLAN_READY` with no pending feedback, a passing policy-valid assessment, and an
unchanged or proven-unrelated revalidation may produce a gate tuple. Owner loss can
rehydrate from the complete bundle, but its physical Codex or Claude session id exists
only in live owner memory.

`flow.bootstrap-journal/v1` advances one approval digest through `prepared`,
`worktree_intended`, `worktree_created`, `run_seeded`, and `committed`. The journal rejects a different
tuple. An incomplete matching tuple is rolled back and retried under the existing claim;
a committed tuple returns the existing run only after state, route, approval, and plan
artifacts are re-verified. Its filename derives from the approval digest rather than a
planner-provided attempt id.
The intent phase records branch and worktree before Git mutation so every crash point
has deterministic rollback coordinates. Cleanup clears those coordinates only after
worktree and branch removal are proven.

`planner_worker.py` reports one record per physical launch: attempt number, exact
600-second soft and 2400-second hard budgets, deadline events, outcome, elapsed time,
and terminal acknowledgement. One fresh retry receives a new budget after process and
output closure. Aggregate wall time is a separate field rather than an attempt metric.

### Legacy `[models]` workspace schema

```toml
# The block is OPTIONAL. Omit it entirely for the default (routable stages = sonnet
# on full-lane runs). Each key is a routable stage; its value is the model to pin, or
# "off"/"none"/"false"/"" to inherit the session model for THAT stage.
[models]
implement   = "opus"     # per-stage pin: e.g. this stage self-edits the harness -> keep it strong
e2e         = "sonnet"   # run + observe the change -> cheap
# code_review, review_loop are also routable (default sonnet); unlisted stages inherit.

# work_model = "sonnet"  # DEPRECATED global fallback: one model for every routable
                         # stage without a per-stage key. A per-stage key always wins.
```

The compatibility wrapper remains byte-for-byte behavioral legacy: full-lane default,
stage-over-work-model precedence, express/light skip, OFF inheritance, fail-open
reads, and Codex inheritance. It is never coerced into an `AgentRoute`. Explicit
`[agents]` routes win as a separate mode; migration requires an explicit apply.

## `.flow-bundle.toml` schema

External plugins declare which flow stages they provide handlers for via a top-level `.flow-bundle.toml`.
`bundle-discover.py` walks `~/.claude/plugins/*/` and `<repo>/.claude/plugins/*/` (override: `FLOW_BUNDLE_SEARCH_ROOTS`, colon-separated) and parses each manifest.
Schema:

```toml
schema_version = 1     # closed enum: { 1 }; mismatch = invalid (warning unless --select)

[bundle]
name        = "ship-it"   # bundle slug, used by --bundle-name selectors
description = "Push branch + open draft PR + CI loop"

# One [skills.<stage>] table per stage the bundle provides. `stage` MUST be a
# closed-vocabulary flow stage (ticket | plan | implement | code_review | e2e |
# commit | create_pr | review_loop | reflect). Unknown stages = invalid manifest.
[skills.create_pr]
handler_string         = "skill:ship-it:create"   # required; MUST start with "skill:"
required_capabilities  = []                       # optional, list[str]; CAPABILITY_ENUM names
args_schema            = {}                       # optional, dict; opaque, validated by skill
required_outputs       = ["pr_url"]               # optional, list[str]
side_effects           = ["git push", "gh pr create"]   # optional, list[str]
stage_compatibility    = ["create_pr"]            # optional, list[str]; cross-check vs stage roles

[skills.review_loop]
handler_string = "skill:ship-it:feedback"
```

### Discovery contract

| Condition                                       | Result                                         |
|-------------------------------------------------|------------------------------------------------|
| Manifest absent                                 | not discovered; not an error                   |
| Manifest parses + schema valid                  | listed in `valid`                              |
| Manifest invalid + UNRELATED to selected bundle | listed in `invalid` (warning; `cli_main` exit 0)|
| Manifest invalid + IS the `--select`ed bundle   | `cli_main` exit 2; init.py exit 1              |
| Two valid manifests advertise the same stage    | listed in `duplicates`; `recommended` refuses  |

### Composition rules

- **bare**: every stage in `pipeline.stages` uses `stage-registry.toml`'s
  `default_handler`. Always available.
- **recommended**: discovered manifests' `handler_string` values override the
  defaults for every stage they advertise. Two-provider conflict on ANY stage
  rejects the whole `recommended` choice (caller must use `--bundle custom` to
  disambiguate). Day-1 design choice: don't try to auto-rank conflicting
  providers — surface the conflict.
- **custom**: caller supplies `--handler <stage>=<handler_string>` flags. Init
  validates handler strings against the closed grammar
  (`inline | none | subagent:<type> | skill:<name>[:<args>]`) and rejects
  unknown stages.

### Transactional bootstrap markers

| File                          | Lifecycle                                                  |
|-------------------------------|------------------------------------------------------------|
| `.flow/.initializing`         | created BEFORE any mutation; left in place on failure      |
| `.flow/.init-progress`        | append-only JSONL of completed phases; consumed by --resume |
| `.flow/.initialized`          | atomic rename from `.initializing` ONLY after postconditions pass |
| `~/.config/flow/checkpoint-manifest.jsonl` | append-only ledger of participating workspaces (one line per init / reconfigure) |

Pre-flight refusal:

| Marker state                        | Default behavior        | Override            |
|-------------------------------------|-------------------------|---------------------|
| `.initialized` present              | exit 4 (`InitPreflightError`) | `--reconfigure`     |
| `.initializing` present (no marker) | exit 4 (`InitPreflightError`) | `--resume` or `--reconfigure` |

### Postconditions (verified before atomic rename)

1. `.flow/workspace.toml` parses as valid TOML.
2. `[tracker]` block has `backend` matching the chosen backend.
3. `[pipeline.stages]` matches the computed stage list (drops `reflect` iff
   `memory.compounding = false`).
4. `[pipeline.handlers]` contains an entry for every stage in
   `[pipeline.stages]`.
5. `[memory]` block has `namespace`, `compounding`, `auto_recall`, `recall_by`,
   `recall_top_n`.
6. For backend=beads: `bd ready --json` returns parseable JSON.

## Beads CLI surface

`bd` is the local-only beads tracker (v1.0.4).
JSON output is supported globally via `--json`.
Adapter wraps a subprocess runner; tests inject a fake.

### Subcommands used by BeadsAdapter

| bd subcommand           | flags used                                         | --json | mutates | Protocol method(s)                          |
|-------------------------|----------------------------------------------------|--------|---------|---------------------------------------------|
| `bd version`            | —                                                  | ✗      | ✗       | constructor preflight                       |
| `bd show <key>`         | `--json`                                           | ✓      | ✗       | `get`, `state`, `is_shipped`, post-write verify |
| `bd list`               | `--status`, `--assignee`, `--json`                 | ✓      | ✗       | `list_assigned`                             |
| `bd create`             | `--title`, `--description`, `--type`, `--parent`, `--labels`, `--assignee`, `--json` | ✓ | ✓ | `create` |
| `bd update <key>`       | `--status`                                         | ✗      | ✓       | `transition` (non-close)                    |
| `bd close <key>`        | —                                                  | ✗      | ✓       | `transition` to closed                      |
| `bd reopen <key>`       | —                                                  | ✗      | ✓       | `transition` to open from closed            |
| `bd comment <key>`      | `--stdin`                                          | ✗      | ✓       | `comment` (markdown via stdin)              |
| `bd dep add <from> <to>` | `--type`                                          | ✗      | ✓       | `link`; seam kind→bd type: `depends_on`→`blocks`, `relates`→`related`, `blocks` native, unknown raw. `from` depends on / is blocked by `to`. |
| `git symbolic-ref`      | `--short refs/remotes/origin/HEAD`                 | ✗      | ✗       | `is_shipped` default-ref resolution         |
| `git fetch`             | `--quiet origin <branch>`                          | ✗      | (.git)  | `is_shipped` best-effort ref refresh        |
| `git log`               | `<origin/default> --grep=<key> --format=%H%x00%B%x1e -n 50` | ✗      | ✗       | `is_shipped` default-branch ship probe (word-boundary re-checked) |
| `bd history <key>`      | `--json`, `--limit 0`                              | ✓      | ✗       | `metric.revert-rate` status-timeline read (not via adapter) |

### State normalization

| bd native      | NORMALIZED_STATES |
|----------------|-------------------|
| open           | open              |
| in_progress    | in_progress       |
| blocked        | blocked           |
| deferred       | cancelled         |
| closed         | done              |

Unknown natives default to `open` with an `adapter_mapping_diagnostic` flagging the fallback so dashboards can surface the unfamiliar status.

### Transition synthesis

bd has no `list_transitions` subcommand; the workflow is "any state → any other state".
Adapter advertises the legal target set per current native status:

| current native | available targets                 |
|----------------|-----------------------------------|
| open           | in_progress, blocked, closed      |
| in_progress    | open, blocked, closed             |
| blocked        | open, in_progress, closed         |
| deferred       | open, closed                      |
| closed         | open  (via `bd reopen`)           |

`Transition.id` is `"bd:to:<target>"`.
The `transition` method routes:
- `bd:to:closed` → `bd close <key>`
- `bd:to:open` from `closed` → `bd reopen <key>`; otherwise `bd update --status open`
- everything else → `bd update --status <target>`

Postcondition: re-read `bd show --json` and assert the normalized state moved to the requested target.

### Stderr → failure_kind classification

| stderr pattern                         | TransitionFailureKind |
|----------------------------------------|-----------------------|
| `Error: no beads database found`       | wrong_source_state    |
| `Error: issue not found`               | wrong_source_state    |
| `permission denied` / `forbidden`      | permission_denied     |
| anything else (non-zero exit)          | validator_failed      |

### Capability advertisement

14 entries; only `comments_markdown` (bd accepts markdown via `bd comment --stdin`) and `resolutions` (bd records `closure_reason` on `bd close`) flip true.
Every other capability is false → `set_sprint`, `list_sprints`, `get_attachments`, `download_attachment` raise `NotSupported`.

### is_shipped contract (PURE READ; never writes under `.flow/`)

1. `bd show <key> --json`.
2. If `status != closed` → `not_shipped` (evidence None, source none).
3. If closed: resolve the default ref (`git symbolic-ref --short
   refs/remotes/origin/HEAD`, else `origin/main`), best-effort `git fetch` it,
   then grep it for a commit naming the key as a WHOLE WORD (`git log
   <ref> --grep=<key> --format=%H%x00%B%x1e`, word-boundary re-checked so a
   parent key does not match a child's commit). The default-branch gate is what
   keeps a closed-but-unmerged bead (work commit only on a feature branch) from
   reading as shipped; the join is by key-in-message, not sha, because
   squash-merge makes the feature-branch tip a non-ancestor of main.
   - Commit on the default branch → `not_yet_observed` (evidence has tracker,
     status, commit_sha, closure_reason [bd's `close_reason`], closed_at; source
     `live_backend_query`).
   - No default-branch commit → `indeterminate` (evidence has tracker, status,
     commit_sha=null, closure_reason, closed_at; source none).
4. Workspace's `observe-ship-event.py` (phase ≥7) is the writer that promotes
   `not_yet_observed` into a frozen `<key>.json` ship-event record. Adapter
   never returns `state="shipped"` — that's the frozen-file reader's domain.

### Transient-failure handling (deferred to phase 8)

Plan line 990 calls for transient `bd` failures (network blips, lock contention) to append to `.flow/pending-mutations.jsonl` so `/flow sync` can retry.
`pending-mutations.py` is phase-8 work; the adapter currently surfaces the error as `_BeadsError(TrackerError)` and lets the dispatcher (phase 7) decide.

## Dispatcher state machine

The dispatcher is a state-machine driver — NOT an orchestrator.
It reads / writes `.flow/runs/<ticket>/state.json` and emits a handler-descriptor JSON for the SKILL.md prose layer to act on (call Agent, read reference doc, invoke a skill, or skip).

### Stage lifecycle

```
pending → in_progress → (completed | failed)
```

`next` writes `pending → in_progress`.
The handler runs between `next` and `finish`.
`finish` writes `in_progress → completed | failed`.

### state.json schema (`schema_version = 1`)

```json
{
  "schema_version": 1,
  "ticket": "FT-1234",
  "run_id": "0123456789abcdef",
  "backend": "jira",
  "started_at": "2026-05-28T12:00:00Z",
  "stages": {
    "ticket": {
      "status": "completed",
      "started_at_iso": "2026-05-28T12:00:01Z",
      "started_at_sha": "abc123",
      "finished_at_iso": "2026-05-28T12:00:05Z",
      "finished_at_sha": "abc123",
      "agent_id": null,
      "output_path": null,
      "skill_output": null,
      "failure_detail": null
    },
    "plan": { "status": "pending", "...": "..." }
  }
}
```

### Atomic-write contract

The one implementation is `_atomicio.atomic_write_bytes` (state.py delegates to it):

1. `tempfile.mkstemp` in the parent dir, write via `os.fdopen`, `fsync()` the temp file.
2. Preserve the destination's prior mode on the temp (new file: literal `0o644`, not umask-masked, so a restrictive umask can't reintroduce mkstemp's `0o600`).
3. `os.replace(tmp, final)`.
4. `fsync()` the parent directory, making the rename itself crash-durable.

state.py adds around it: acquire `state.json.lock` via `fcntl.flock(LOCK_EX)` for the read-modify-write sequence; copy old state.json to `state.json.<ts>.bak` before each write; trim backups to the last `BACKUP_RETENTION = 5` after.

### Quarantine (repo-wide pattern, best-effort)

Never-destroy invariant: a corrupt artifact is renamed or copied aside, never deleted. Four sites:

- **state.json** (state.py): malformed JSON on `state.read()` → move to `state.json.quarantine.<ts>` → try newest `.bak` (parses → restore + return, exit 1) → all `.bak` corrupt → exit 2, library raises `StateUnrecoverable`. Backups are checked for "parses as JSON with schema_version=1 + required top-level keys", not deep-schema-validated.
- **run.lock** (lease.py `_quarantine_locked`): rename to `run.lock.quarantine.<ts>`, inside the caller's flock span (classify + remediate under one lock).
- **JSONL lines** (`_jsonl.iter_jsonl`): a malformed line is appended as `{reason, raw}` to a quarantine sidecar and skipped; re-quarantine is idempotent; the main file is never rewritten. `read_jsonl_lenient` is the read-only twin (never writes the sidecar).
- **recall pending** (recall_pending.py): the promoting rewrite moves >24h entries to `.stale` rather than dropping them.

### Subprocess exit codes

| Script              | Exit | Action                                          |
|---------------------|------|-------------------------------------------------|
| state.py            | 0    | ok                                              |
| state.py            | 1    | quarantine triggered (loaded from .bak)         |
| state.py            | 2    | no valid backup; abort                          |
| validate_workspace  | 0    | ok                                              |
| validate_workspace  | 1    | schema invalid; stderr lists violations         |
| dispatch_stage      | 0    | ok                                              |
| dispatch_stage      | 1    | validate failed / state malformed / generic     |
| dispatch_stage      | 2    | no ticket dir / not yet initialized             |
| dispatch_stage      | 3    | revise-open: original run not terminal          |
| dispatch_stage      | 4    | revise-open: a revision is already live         |
| dispatch_stage      | 5    | stale foreign lease (needs /flow recover --takeover) |
| dispatch_stage      | 7    | lost lease (another run took over)               |

### Handler-descriptor JSON shape (`dispatch next` stdout)

```json
{
  "done": false,
  "stage": "plan",
  "handler_type": "subagent" | "inline" | "skill" | "none",
  "subagent_type": "Plan",
  "reference_doc": "references/stage-plan.md",
  "skill_name": "ship-it",
  "skill_args": "create",
  "timeout_min": 10,
  "head_sha": "<current git HEAD>",
  "ticket_dir": ".flow/runs/FT-1234",
  "output_path": ".flow/runs/FT-1234/stages/plan.out",
  "roles": []
}
```

Terminal shapes:
- `{"done": true}` — every stage completed.
- `{"done": false, "blocked_by": "<stage>", "reason": "<detail>"}` — a
  prior stage is failed.

### Revision sub-run (`revise-open`, flow-kx17.2)

`dispatch_stage.py revise-open --ticket T --workspace-root R [--stages a,b,c]` opens a
revision SUB-RUN under a terminal ticket run. A revision lives at
`runs/<ticket>/revisions/<rev-id>/` with its OWN lease/state/snapshot; the original
terminal run is NEVER mutated. Guards: the original must be terminal (exit 3), and only
one revision may be live per ticket at a time (exit 4); rev-id allocation + the live scan
+ state seed + lease acquire run under a single per-ticket `revise.claim` flock. Default
stage subset = `implement, code_review, e2e, commit, reflect, review_loop` intersected with
the workspace stages (ws order preserved); `--stages` overrides. Emits
`{ticket, rev_id, run_id, session_nonce, revision_dir, stages}`. The
`next`/`advance`/`release` subcommands take `--revision <id>` to drive
the sub-run (default = the ticket-level run, byte-identical to today).

`flow_worktree.py locate-or-reseed --ticket T --branch B --main-root R` is the revision's
worktree handle: it returns the ticket's registered `feature/<ticket>*` worktree
(`{worktree, reseeded:false}`, the norm — PR-open ⇒ worktree-present), or, when that
worktree was externally reaped, re-materializes it by checking out the EXISTING remote
branch (`git worktree add <path> <branch>`, no `-b`) and re-copying gitignored config via
the same helpers `bootstrap` uses (`{worktree, reseeded:true}`). Exit 1 on a git/worktree
error.

### TOCTOU invariant

`validate_workspace.validate()` runs on every `dispatch_stage` invocation (`init` and `next`).
Cheap (parses 2-3 small TOML files).
Catches mid-run workspace.toml edits.
The canonical-snapshot pattern is live: a content hash is captured once at `init` and compared on each `next` call via `snapshot.py`. The hash covers four components (see the snapshot.py module docstring): workspace.toml text, stage-registry.toml text, each `skill:` handler's manifest + plugin tree hash, and — only while the main checkout sits on a protected branch — the engine's own skill tree (the marketplace-tracks-main window where a mid-run checkout advance swaps engine code).

### Deferred to phase 7-full / 8

| Concern                                          | Phase     |
|--------------------------------------------------|-----------|
| ~~Lease-style run.lock (pid + boot_id + ...)~~ (shipped as lease.py: acquire/refresh/release/expiry + takeover detection) | 7-full ✓ |
| ~~Background lease refresher thread~~ (deliberately NOT built — mutual exclusion comes from lease identity under flock, refreshed per dispatch call + session nonce; see the lease.py module docstring) | 7-full ✓ |
| ~~`--emit-canonical-snapshot` content-tree hash~~ (shipped as snapshot.py + the dispatch-init write; the standalone flag was retired 2026-07) | 7-full ✓ |
| FS capability probe (flock detection)            | 7-full    |
| `lint-ticket.py` HARD GATE pre-stage             | 8-mvp ✓   |
| `branch-ticket.py` ticket resolution             | 8-mvp ✓   |
| `ticket-frontmatter.py` TOML r/w                 | 8-mvp ✓   |
| `diff-extract.py` baseline + since-stage         | 8-mvp ✓   |
| `compose-commit.py` skeleton emitter             | 8-mvp ✓   |
| `recover.py` takeover modes                      | 8c        |
| `memory-append.py` + `recall.py` + ship-event    | 8b        |
| `pending-mutations.py` + `sync.py`               | 8d        |
| Capability cross-check (handler vs adapter)      | 7-full    |
| Subagent / skill handler spawn harness           | 7-full    |

## Out-of-scope for phase 3

- `comments_markdown=true` (Jira would need a separate markdown wrapper; ADF
  satisfies all current call sites).
- Webhook subscription / live event push (the plan's ship-event observer is the
  workspace's job, not the adapter's).
- Bulk operations (`bulkCreateIssue`, `bulkEditIssues`). Adapter sticks to
  single-issue endpoints; the dispatcher batches client-side.
- Jira Server / Data Center (Cloud only — REST v3 + agile/1.0 differs on-prem).

---

## Bookkeeping helpers

Five bookkeeping scripts.
All stdlib-only, library + thin CLI shape, atomic writes where they touch files, `fcntl.flock` where they touch shared mutable state.
Built to be subprocess'd by `dispatch_stage.py` (phase 5 wiring) but shippable as standalone CLIs first.

### `branch_ticket.py`

Pure read.
Resolves ticket key from current git branch.
CLI surface: MODULE.md §Bootstrap (seam-checked there; this file keeps only the contract).

Backend-aware key regexes: jira `<PROJECT_KEY>-\d+`; beads `<prefix>-[0-9a-z]{3,}(\.\d+)*` (dotted child keys resolve too).
`--branch` resolves from an explicit branch (no git call) — the PR->ticket enabler for `/flow revise <pr#>`; absent = current branch.
Exit 0=match, 1=env-error, 3=no-match.

### `ticket_frontmatter.py`

TOML frontmatter r/w under flock + atomic rename.
Frontmatter delimiter is `+++` (deviation from plan-source "YAML" wording — locked at design review).

| Subcommand | Flags | Exits | Notes |
|------------|-------|-------|-------|
| `read <path>` | — | 0 always (on malformed: quarantine + warn + empty dict) | Emits JSON to stdout. |
| `update <path>` | `--set k=v` (repeatable) | 0=ok, 1=lock contention, 2=schema invalid, 3=I/O | `--set` parses: `null`→`""`, `true`/`false`→bool, `^-?\d+$`→int, `^\[.*\]$`→list, `NOW`→UTC ISO, else→string. |

### `lint_ticket.py`

HARD GATE pre-stage: validate required ticket frontmatter fields per stage.

| Flag | Description |
|------|-------------|
| `--stage <name>` | Stage name (matches stage-registry). |
| `--ticket-path <path>` | Path to ticket `.md` file. |
| `--workspace-root <dir>` | Override stage-registry source (default: plugin root). |

Exit 0=continue, 1=block (violations to stderr as `<key>: <reason>`).
Required fields per stage (8-mvp set, baked into stage-registry.toml):

- **universal** (every stage): `ticket`, `status`.
- `implement.required_fields = ["planned_files"]`
- `e2e.required_fields = ["e2e_recipe"]`
- `commit.required_fields = ["commit_type", "commit_summary"]`

Empty-string / empty-list / missing-key all count as violations.

### `diff_extract.py`

Git diff capture for implement / commit / reflect stages.
Flag surface: MODULE.md §Frontmatter / diff / commit (seam-checked there); this table keeps the exit/output contract.

| Subcommand | Exits | Output |
|------------|-------|--------|
| `since-stage` | 0=ok, 1=missing-state, 2=git-error | Reads `state.json` for `stages.<name>.started_at_sha`, diffs `<sha>..HEAD` → `{files_touched, insertions, deletions, binary}` JSON. |
| `record-baseline` | 0=ok, 2=git-error | Writes `<ticket-dir>/baseline.json` with `{stage, head_sha, planned_files, blobs}`. |
| `capture-implement-diff` | 0=ok, 1=missing-baseline / gitignored planned file, 2=git-error | Writes `<ticket-dir>/implement.diff` via `git diff --binary --raw`. |
| `check-ownership` | 0=ok, 3=ownership violation (unowned paths), 1=missing/malformed baseline, 2=git-error | `{ok, planned_files, changed, unowned_changes}` JSON. Branch-wide: scans the dirty working tree AND the committed delta `baseline.head_sha..HEAD`, so a rogue mid-implement commit is seen too. Wired as stage-commit step 2b. THIS is the content-ownership commit gate CLAUDE.md names. |

### `compose_commit.py`

Skeleton conventional-commit emitter.
Deterministic header; body is a template the LLM fills in.
Flag surface: MODULE.md §Frontmatter / diff / commit.

Contract: `--type` is one of `feat`, `fix`, `chore`, `docs`, `refactor`, `test`, `perf`, `style`, `build`, `ci`, `revert`. With `--scope`: `type(scope): summary`; without: `type: summary`. `--files` emits a `files:` block; `--covers` emits one `Closes <KEY>` trailer per cover.
Exit 0=ok, 1=empty/whitespace `--summary` or `--ticket`, 2=invalid `--type` or missing required flag (argparse usage error).

### `machinery_edit.py`

Concurrency-safe applier for reflect lens-B machinery fixes to flow's OWN source.
A fleet runs many `/flow` jobs at once; several can hit reflect together. The raw Edit tool has no cross-process serialization, so two concurrent machinery edits to the same file race (lost update, or a torn read that crashes a third run importing the half-written module). This tool holds a single blocking flock on `<skill-root>/.machinery.lock` across the whole read → replace → `atomic_write_text`, so writers serialize and any concurrent reader sees old-or-new. The flock auto-releases on process exit (no lease to clear). It also refuses `stage-registry.toml` (canonical-snapshot-pinned) and any path outside the skill tree.

Flag surface: MODULE.md §Self-evolution.
Payload contract: JSON `{file, old, new}` via `--payload <file>` or stdin; `file` is rel-to-skill-root or absolute; `old` must be a unique anchor.
Exit 0=applied or already_applied (idempotent), 1=usage/IO error, 2=refused (out-of-tree or snapshot-pinned), 3=anchor_not_found, 4=ambiguous (non-unique anchor).

## Known phase 8-mvp holes (deferred to 8b/8c/8d)

1. **TOML frontmatter scope** — flat scalars + string lists only. Nested tables
   on hand-edit trigger read-side quarantine; write-side aborts with exit 2.
2. **Content-ownership check on commit — RESOLVED (v0.25.18).** `diff_extract
   check-ownership` is now wired into the `commit` stage
   (`references/stage-commit.md`): it refuses changes outside the reconciled
   `planned_files` across the whole branch delta — the dirty working tree AND
   commits since `baseline.head_sha` — fail-safe (a clean exit-3 refusal, never
   a silent commit). Filename-level; a hunk-level ownership check stays a
   deeper future refinement.
3. **lint-ticket `required_fields`** — only 3 stages get non-empty lists. Other
   stages get universal-only.
4. **No retry knob** for ticket-frontmatter lock contention — hard-coded 3×1s.
   Sufficient for serial human use; 8b can pull from workspace.toml.
5. **`since`/`since-stage`** uses `--numstat`; renames surface only in
   `capture-implement-diff` (`--raw`).
6. **Dispatcher integration** — helpers ship as standalone CLIs. Subprocess
   wiring into `dispatch_stage.py` (with exit-code matrix) lands in phase 5
   or phase 8-glue.

---

## Memory cohort

Four stdlib-only scripts that own `.flow/<namespace>/knowledge.jsonl`, `.flow/<namespace>/ship-events/<ticket>.json`, and the reflect-stage input bundle.
Same library + thin-CLI shape as 8-mvp.
Shared `_memory_paths.py` module handles namespace resolution + path conventions.

### `_memory_paths.py` (shared helper)

Public API: `resolve_namespace(workspace_root) -> str`,
`resolve_memory_base(root) -> Path` (the `.flow/memory-root` sibling / `[memory].root` redirect),
`namespace_root(root, ns)`, `knowledge_path(root, ns)`, `knowledge_lock_path(root, ns)`,
`friction_path(root, ns)`, `friction_lock_path(root, ns)`,
`ship_events_dir(root, ns)`, `ship_event_path(root, ns, ticket)`,
`revert_events_dir(root, ns)`, `revert_event_path(root, ns, sha)`,
`load_semantic_config(root) -> dict` (the one `[memory.semantic]` reader).

### `memory_append.py`

Single-writer JSONL append.
Idempotency key: `sha256(namespace + ticket + type + normalized_body)[:16]` where `normalize(body) = NFKC + lowercase + collapse-ws + strip-trailing-punct`.

| Flag | Description |
|------|-------------|
| `--type` | One of: `LEARNED`, `DECISION`, `FACT`, `PATTERN`, `INVESTIGATION`, `DEVIATION`. |
| `--text` | Entry body (raw, not normalized — normalize is for id only). |
| `--branch` | Branch name. |
| `--ticket` | Ticket key. |
| `--supersedes` | Optional id of the live entry this one replaces (tombstone pointer, metadata only — never a hash input); the target must exist in `knowledge.jsonl`. |
| `--labels` | Optional CSV `facet:value` array (e.g. `form:iva_2083,area:vat`), comma-split with empties stripped; metadata only — never a hash input. Written as `entry["labels"]` ONLY when non-empty (mirrors `--supersedes`). |
| `--workspace-root` | Default `.`. |

Exit codes: 0=appended, 1=duplicate id (no-op), 2=lock contention,
3=invalid type, 4=I/O error / workspace config error, 5=unknown `--supersedes`
target id.

Locking: `fcntl.flock(LOCK_EX | LOCK_NB)` on `knowledge.jsonl.lock`, retry 3×1s.
Sidecar quarantine: malformed lines appended to `knowledge.jsonl.quarantine.<ts>` (one per invocation); main file untouched.

### `recall.py`

Hand-rolled BM25 ranker with an OPTIONAL semantic-fusion overlay.
`--metric` mode is live; `--metric <subcommand>` forwards to `metric.cli_main`.
`--reindex` dispatches to `memory_embed.cli_main(["reindex", ...])` (a real argparse
flag, NOT a `--metric`-style raw-argv intercept).

| Flag | Description |
|------|-------------|
| `<query>` | Positional, now optional (`nargs="?"`). Raw text; tokenized via `\b\w+\b` Unicode-NFKC-lowercase. |
| `--query-file` | Read the query from a file instead of the positional (the ticket title+body is passed this way, NOT as a shell positional — avoids the `"`/`\`/newline hazard). stdin is the third fallback. |
| `--branch` | Optional. Exact-match boost × 2.0. Case-insensitive. |
| `--tickets` | Optional CSV. Exact-match boost × 3.0 (any match in CSV). |
| `--ticket` | Ticket key for `--record-pending`. |
| `--label` | Optional exact `labels[]` match. HARD pre-filter (a WHERE clause, not a boost): entries lacking this exact value are dropped before ranking, in both `rank()` and `_semantic_rank()`. Bypasses `--top-n` (raised to corpus size — exhaustive cluster retrieval, never truncated). The query becomes optional; a label-only recall (no query) still ranks via the `ts DESC` tiebreak, and forces the deterministic BM25 path (no embed call for an empty query). |
| `--digest` | Renders the `--label` cluster as a human-readable markdown card instead of the raw JSON array: one section per entry `type` (canonical order DECISION, FACT, LEARNED, PATTERN, INVESTIGATION, DEVIATION, then any other type sorted alphabetically; only non-empty sections render), newest-first (`ts` DESC) within a section, one line per entry (`- <ts> · <ticket> · <first sentence of body>`). Requires `--label` — `parser.error` (exit 2) otherwise. The plain JSON path is byte-unchanged when `--digest` is absent. |
| `--top-n` | Default 5. Output cap; also drives the cosine top-K candidate pool (K = top_n × 2, min 20). Ignored when `--label` is set. |
| `--semantic` | Force the semantic path on (default follows `[memory.semantic].enabled`). |
| `--threshold` | Low cosine floor — drops non-positive (anti-correlated) cosines (default `[memory.semantic].threshold`, else 0.0). NOT the candidate gate; selection is rank-based top-K. |
| `--record-pending` | Append the recalled ids to `recall-pending` (needs `--branch` + `--ticket`). The post-gate producer that replaces the old SessionStart hook. Best-effort. |
| `--reindex` | Dispatch to `memory_embed reindex` (refresh the sidecar). `--full` forces a full rebuild. |
| `--workspace-root` | Default `.`. |

BM25 params (pinned): k1=1.5, b=0.75.
Field weights: body=1.0, type=0.5, branch=1.5, ticket=2.0, labels=2.0.
Tiebreak: ts DESC (ms precision via negated-codepoint sort key over ISO8601 string).
IDF scope: current namespace only.

**Label clusters (`labels[]` + `--label`).** An entry's optional `labels` array
(`memory_append --labels`) is a facet-tagging convention, e.g. `["form:iva_2083"]`.
`--label facet:value` restricts BOTH scoring paths to entries carrying that exact
value (membership over `labels`, not a substring match) BEFORE ranking, so the
retrieval is exhaustive over the whole live cluster rather than a relevance-ranked
top-N. A query token that happens to equal a label value still gets BM25 fuzzy
reach through the `labels` field weight (list values are space-joined then
tokenized) even without `--label` set; for a label-free corpus this field
contributes 0 (the `avgdl==0` guard), so scoring is byte-identical to before.

**Semantic fusion (gated by `[memory.semantic]`):** after `filter_superseded`, when
enabled AND the sidecar index loads AND its header model matches the configured model:
embed the query once (`memory_embed.embed`, a uvx subprocess), pure-Python cosine vs
each indexed live vector, select the top-K cosine candidates by RANK (K = top_n × 2,
min 20; a low floor drops non-positive cosines — no embedder-coupled absolute gate),
RRF-fuse that cosine ranking with the FULL BM25 ranking (`1/(k+rank)`, k=60), apply the exact-match bonuses,
cap at `--top-n`. Cosine-missing (unindexed) entries still rank via BM25 → graceful
partial-index behavior. ANY failure (embedder unavailable, index missing/empty, model
mismatch, exception) falls through to the unchanged BM25 `rank()` + a backend-status
line on stderr (`semantic-active model=<id> cosine_candidates=N`, or
`bm25-fallback reason=<...>`). `[memory.semantic]` absent/off → byte-identical pure BM25
(`rank()` is kept intact as the fallback).

Output: JSON array of top-N entries with `score` and `labels` fields appended
(`labels` is `[]` for an entry with no `labels`, not an omitted key).
Empty corpus returns `[]` exit 0.

Exit codes: 0=ok, 1=workspace invalid / namespace unresolvable OR no query supplied.

### `memory_embed.py`

Embedder seam + derived sidecar index for the semantic overlay. Pure stdlib —
never imports the embedding model (it lives ONLY inside the uvx subprocess).

**Embedder seam** = a configured command, shelled (batch: newline texts on stdin → a
JSON array of vectors on stdout). Resolution: `[memory.semantic].embedder` when set,
else the shipped default `uvx --with fastembed python embedder_fastembed.py
--model <id>` (runs in uvx's own cached env, independent of the runtime python3 which
cannot import it). Missing command / `uvx` absent / nonzero exit / unparseable /
wrong vector count → `_EmbedderUnavailable` (recall catches → BM25 fallback).

**Sidecar index** `.flow/<namespace>/knowledge.embed` (derived; `knowledge.jsonl` stays
the source of truth):
- line 1 header: `{"_header": {"model": "<id>", "dim": <int>, "ts": "<iso>"}}`
- body: `{"id": "<entry-id>", "v": [<float>, ...]}` per live entry.
Read via the quarantine-tolerant `iter_jsonl`; written under `knowledge.embed.lock`
(`flock_retry`) via an atomic temp-rename.

`reindex(workspace_root, namespace, incremental=True)`: read `knowledge.jsonl`
(supersede-filtered via `recall.filter_superseded`), diff live ids vs indexed ids, embed
the missing set (incremental) or all (`--full`), rewrite the sidecar keeping only live
ids (dead ids drop out). A header model-id ≠ the configured model forces a full rebuild.

| Subcommand | Description |
|------------|-------------|
| `reindex --workspace-root [--full --model --embedder]` | Refresh the sidecar. Prints a summary JSON `{model, dim, live, embedded, kept, full}`. |
| `embed [--workspace-root --model --embedder]` | stdin texts → JSON vectors (exercises the contract). |

Exit codes: 0=ok, 1=workspace invalid / namespace unresolvable, 2=embedder unavailable.

First-enable on an existing workspace starts with an EMPTY index, so plan-phase recall is
BM25-only until a one-time bulk backfill: `recall.py --reindex --workspace-root .` (or
`memory_embed.py reindex`). Document/run this when flipping `enabled = true`.

### `embedder_fastembed.py` (default) / `embedder_model2vec.py` (alt)

Two reference embedders, each run BY `uvx`, standalone subprocess entrypoints (imported
by nothing). Read newline texts on stdin, print `[[float, ...], ...]` JSON.
- **`embedder_fastembed.py`** — the shipped DEFAULT. `uvx --with fastembed`,
  `fastembed.TextEmbedding(<model>).embed(texts)`. ONNX runtime, no torch. Default model
  `BAAI/bge-small-en-v1.5` (384-dim). Empty stdin → `[]` (skips the model download).
- **`embedder_model2vec.py`** — lighter static ALTERNATIVE (select via
  `[memory.semantic].embedder`). `uvx --with model2vec[inference]`,
  `StaticModel.from_pretrained(<model>).encode(texts)`. Default `minishlab/potion-retrieval-32M`.

Both exit 0 ok, 1 on load/encode failure. **CI does not install either embedder**, so the
real path is NOT CI-exercised (tests guarded by `pytest.importorskip`); "tests green" ≠
"real embedder validated". The runtime-availability check (does the shipped uvx command
return vectors from the runtime python3 context) is manual + observable via recall's
stderr status line.

### `[memory.semantic]` config block

Optional `workspace.toml` block (off by default; absent → semantic off → pure BM25):

| Key | Default | Meaning |
|-----|---------|---------|
| `enabled` | `false` | turn the semantic overlay on. |
| `model` | `BAAI/bge-small-en-v1.5` | model id (must match the sidecar header or a full rebuild fires). |
| `threshold` | `0.0` | low cosine floor (drop non-positive cosines); candidates are selected by rank (top-K), not τ. |
| `embedder` | `""` | override the shipped uvx command; blank → default. |

`init.py` writes a commented template of this block. `recall_by` / `recall_top_n` in
`[memory]` are now UNREAD (the SessionStart recall path was removed; plan-phase recall
has its own `--top-n`/`--threshold`) — they stay harmless, postcondition #5 still expects
them so `init` keeps writing them.

### `[memory] label_facets` key

Optional `list[str]`, default `[]`. Names the facet(s) a knowledge entry can be
tagged with via `memory_append --labels <facet>:<value>` (e.g. `label_facets =
["form"]` -> `--labels form:iva_2083`). Unlike `recall_by`/`recall_top_n` above,
this key is forward-wired, not vestigial:
- `init.py` seeds `label_facets = []` into the generated `[memory]` block.
- `validate_workspace.py` type-checks it (`list[str]`) ONLY when present
  (absent is valid — mirrors the `root` optional-key pattern); a present
  non-list or non-str element is one violation, `memory.label_facets`.
- `reflect_inputs.py` surfaces it as its own top-level bundle key
  `label_facets` (read from `[memory]`, default `[]`; NOT folded into
  `reflect_config`, which is the `[reflect]` gates).
- `stage-reflect.md` step 3 reads the bundle key and tags new entries with
  `--labels` when a facet applies; step 3b carries a superseded entry's labels
  forward onto its successor.

The engine never hardcodes a facet name; a workspace with no natural facet
convention ships `label_facets = []` and the tagging step is a no-op.

### `recall_usage.py`

Recall observability (flow-nylh.2). Append-only `.flow/<ns>/recall-usage.jsonl`, two
record kinds, read by `metric.py recall-hit-rate`. Reflect drives both (stage-reflect
3d/3e); both are best-effort and deduped per-run so a `/flow recover` rerun never
double-counts.

| Subcommand | Description |
|------------|-------------|
| `record-usage --ticket --ticket-dir [--used-ids]` | One `{kind:usage,used}` record per surfaced id (the run's recall-log `returned_ids` = the denominator); `--used-ids` is the subset the run leaned on. Precision = used/surfaced. |
| `detect-misses --ticket --ticket-dir [--threshold]` | Flags `{kind:miss,type:RECALL_MISS}` near-dup re-learns: an entry written THIS run (`ticket==KEY` AND `ts >= state.started_at`) whose nearest live neighbor (cosine ≥ `--threshold`, default 0.90) was NOT in `returned_ids`. The new entries are embedded FRESH (not read from the sidecar — a stale reindex can't silently starve detection); the corpus vectors come from the sidecar. |

`detect-misses` is a no-op (nothing shelled, `[]`) when `[memory.semantic]` is off,
nothing was written this run, the sidecar is absent, or its header model != the configured
model (the post-swap reindex hazard — never compares mismatched-model vectors). It never
touches the hot `knowledge.jsonl` write path. Exit codes: 0 ok, 2 lock contention, 3 no
state.json, 4 I/O / memory-config error.

### `reflect_inputs.py`

Pure composition layer.
Bundles the reflect-stage's inputs into a single JSON payload for the reflect LLM.

| Flag | Description |
|------|-------------|
| `--ticket` | Ticket key. |
| `--ticket-dir` | `.flow/runs/<ticket>` directory. |
| `--ticket-frontmatter` | Optional path to ticket .md frontmatter file. |
| `--cwd` | Git repo working dir (for `diff_since_stage` call). Default `.`. |

Payload shape: `{ticket, run_id, state, ticket_frontmatter, final_diff, subagent_reports[], friction[], recalled_entries[], reflect_config, harness_eval}`.
`final_diff` is null when ticket stage never started.
Missing report files → `body: null` + warning to stderr (not fatal).

Exit codes: 0=ok, 1=state missing/corrupt, 2=diff-extract git error, 3=I/O.

Reuses: `state.read()`, `ticket_frontmatter.read()`,
`diff_extract.diff_since_stage()`.

### `revise_config.py`

Reader for the `[revise]` block of workspace.toml (revision sub-runs, epic flow-kx17).

| Subcommand | Description |
|------------|-------------|
| `apply-floor --workspace-root .` | Read a threads JSON array on stdin, bump every unresolved `minor` thread to the configured floor, print the floored array. The floor itself (default `"minor"`, validated against `forge.THREAD_SEVERITY`) is read internally. |

`plain_comment_severity(root) -> str` — the configured floor; missing/unparseable workspace.toml or an invalid value → `"minor"` + stderr warning (always exit 0, so the review_loop bash capture stays valid).

`apply_floor(threads, severity) -> list[dict]` — pure helper: bump every UNRESOLVED `minor` thread up to `severity`. Returns new dicts (input never mutated); no-op when `severity == "minor"`. Resolved/major/critical/nit threads pass through unchanged. The review_loop applies this loop-side so `forge_github._severity_from_state` stays pure of `[revise]` config.

Reuses: `_workspace.load_workspace_toml()`, `forge.THREAD_SEVERITY`.

### `observe_ship_event.py`

Sole writer of `<namespace>/ship-events/<ticket>.json`.
Atomic + crash-safe.

Flag surface: MODULE.md §Memory / recall.
Input contract: `--evidence-json` allows top-level keys `ticket`, `shipped_at`, `evidence` only (extras rejected; `--ticket` must match the `ticket` field). `--run-id` is the caller's 16-hex run_id, injected as `observed_by_run_id`. `--arm` is the experiment lane `{flow, control}` (default `flow`); `--tier` / `--acceptance-invariant` / `--lane` are captured at ship time (default `""`).

Two-phase write:
1. **Primary** via `os.open(O_CREAT | O_EXCL | O_WRONLY)`. Success → write +
   fsync file + fsync parent dir → exit 0.
2. **Dupe fallback on EEXIST** — under `<ticket>.json.dupe.lock` flock, pick
   next monotonic `n` from existing `.dupe.*.json` siblings (max + 1 or 1),
   then O_EXCL-create `<ticket>.json.dupe.<n>.json` with
   `superseded_by_dupe: false`. Exit 2.

Script-owned top-level keys (rejected as `--evidence-json` inputs): `observed_at`,
`observed_by_run_id`, `flow_attribution`, `arm`, `tier`, `acceptance_invariant`,
`lane`, `plugin_version` (self-read from
`plugins/flow/.claude-plugin/plugin.json`, `""` on any failure).

On non-EEXIST I/O error: write intent log to `<ticket>.json.quarantine-intent.<ts>.json` (best-effort) BEFORE re-raising.
`/flow recover` in phase 8c replays the intent log.

Exit codes: 0=primary success, 1=evidence JSON invalid, 2=dupe (informational),
3=I/O error (intent log written).

### `friction_escalate.py`

Propose-only recurrence escalation. Consumes `friction_recurrence.analyze` (untouched) and files
ONE deduped `recurrent`-labelled bead per `signature_classes` entry that recurred `>=K` times since
its LATEST claimed MACHINERY fix — not the detector's own `post_fix_count`, which grades against
the earliest fix and over-counts a class with several fix attempts.

Public API: `escalation_k(workspace_root) -> int`, `exempt_anchors(workspace_root) -> set[str]`,
`select_escalations(analyze_payload, k, exempt) -> list[dict]` (pure core, sorted by descending
count), `escalate(workspace_root, runner=None) -> dict`.

`[evolve]` workspace.toml knobs: `recurrence_escalation_k` (int, default 3), `recurrence_exempt_anchors`
(list[str], default `["planned_files"]`; an explicit `[]` means no exemptions, used verbatim).

| Flag | Description |
|------|-------------|
| `escalate` | The one subcommand. |
| `--workspace-root` | Default `.`. |

Dedup key = `recurrence-escalation-<anchor>` (no `::` separator), so only `flow_beads_create`'s
exact `evid:` net fires, never its fuzzy same-file pass. Labels = `recurrent` only (never `evolve`),
so `bd ready -l evolve` never surfaces these — propose-only holds unconditionally. Dormant outside
maintainer mode (`flow_beads_create.resolve_maintainer_repo` returns `None`, checked before any
friction/knowledge read) — returns/prints `{"maintainer": false, ...}` with nothing filed.

Exit codes: 0=ok (including the dormant no-op), 3=OSError, 4=`_memory_paths._MemoryConfigError`.

## Known phase 8b-mvp holes (deferred to 8c/8d)

1. **No cross-namespace IDF** — recall.py IDF is per-namespace.
2. **BM25 hand-rolled, not rank-bm25** — stdlib-only convention. Swap in if
   corpus > 10K entries per namespace.
3. **No recover.py dupe reconciliation** — `.dupe.<n>.json` files sit until 8c.
4. **No SessionStart hook script** — observe/recall/reflect are
   write/read primitives only. SessionStart prose = phase 5.
5. **No retry knob for memory-append flock** — hardcoded 3×1s.
6. **observe-ship-event intent log write-only** — phase 8c recover reads it.
7. **Idempotency formula collapses near-duplicates** — `"Foo."` and `"foo"`
   dedup. First-write wins; second gets exit 1 no-op.
8. **Dedup scan is O(N) per append** — fine for mvp corpus sizes. Swap in
   `.idx` sidecar if corpus grows.

---

## Integration layer

SKILL.md rewrite + 4 reference docs + `tracker_cli.py` + a small dispatcher descriptor extension.
`/flow do <ticket>` now runs end-to-end against a bare workspace.

### `tracker_cli.py`

CLI wrapper around the Tracker Protocol.
Lets reference-doc prose call `tracker.<method>()` from Bash.
Reads `.flow/workspace.toml` `[tracker]` block, flattens the per-backend sub-block (`tracker.jira` or `tracker.beads`) into the config dict `tracker.make_tracker()` expects.

Flag surface: MODULE.md §Tracker (seam-checked there); this table keeps the per-backend contract.

| Subcommand | Notes |
|------------|-------|
| `get` | `tracker.get(key)` → JSON |
| `state` | `tracker.state(key)` → JSON |
| `transition` | Looks up transition id by `to_normalized_state` / `to_state` / `name` (any match). `--field k=v` pairs string-only in mvp. `--enqueue-on-transient`: on a transient failure (exit 1), durably queue the transition to `.flow/pending-mutations.jsonl` for `/flow sync`. |
| `comment` | Wraps body as `{"body": text, "fmt": "md"}` (Content TypedDict: fmt in {md, adf, plain}). |
| `create` | `tracker.create(...)` → `{"key": new_key}` JSON. |
| `is-shipped` | `tracker.is_shipped(key)` → JSON. |
| `download-attachments` | Downloads ticket attachments to `--out <dir>`; skips files over `--max-bytes` (default 25 MiB). |
| `list-types` | `tracker.list_issue_types()` → `[{name, hierarchyLevel}]` JSON. Jira = createmeta issuetypes; beads = static `bd` type enum (epic→1). |
| `list-epics` | `tracker.list_epics()` → `[{key, summary}]` JSON. Jira = active hierarchy-1 issues (type name resolved, not hardcoded "Epic"); beads = `[]`. |
| `list-sprints` | `tracker.list_sprints(project)` → JSON array. On `NotSupported` (beads) emits `{"supported": false, "sprints": []}`, exit 0. |
| `set-sprint` | `tracker.set_sprint(key, sprint_id)` → `{ok, key, sprint_id}`. On `NotSupported` (beads) emits `{"supported": false, "key": ...}`, exit 0. |

Exit codes: 0=ok, 1=transient/unknown tracker error (network/auth/retryable/unknown failure_kind), 2=workspace config invalid, 3=invalid args, 4=hard transition failure (permission_denied / validator_failed / missing_required_field), 5=transition not applicable (wrong_source_state / ambiguous_transition).

Reuses: `tracker.make_tracker()` factory, `tracker.TrackerError` class.
Tests via injectable `tracker_factory` shim — no real tracker construction.

### Dispatcher descriptor extension

`dispatch_stage.py cmd_next` now surfaces the stage's `roles` list in its JSON descriptor (read from stage-registry.toml).
SKILL.md prose uses `roles` to know when to run the `records_diff_baseline` pre-handler hook (implement stage).
Without this, commit-stage's `capture_implement_diff` would fail with `_BaselineMissing`.

### SKILL.md verb router

Replaces the 28-line skeleton with ~250 lines of prose.
Verbs:

- `init` — AskUserQuestion-driven; writes answers to tmp JSON, calls
  `init.py --config <path>`.
- `do <ticket>` — orchestration loop: `branch_ticket` → `validate_workspace`
  → `dispatch_stage init` → loop(`next` → pre-handler-hook → handler
  dispatch → `git rev-parse HEAD` → `finish`).
- `recall <query>` — passthrough to `recall.py`.
- `status` / `recover` / `sync` / `baseline` — stubs with workaround
  hints.

Handler dispatch:
- `inline` → Read reference doc, follow prose.
- `subagent:<type>` → Spawn Agent, capture response, write to
  `<ticket-dir>/stages/<stage>.out`, pass `--output-path` to finish.
- `skill:<name>` → not implemented in 5-mvp; surface error + abort.
- `none` → skip; immediately finish with status=completed.

### Reference docs

Four files in `references/`:

| File | Stage | Purpose |
|------|-------|---------|
| `stage-ticket.md` | ticket | Resolve key, fetch ticket via tracker_cli, cache to ticket.json, stamp frontmatter. |
| `stage-code_review.md` | code_review | Inline main-agent diff review. No tracker calls. |
| `stage-commit.md` | commit | lint_ticket HARD GATE → capture-implement-diff → compose_commit → user fills body → git apply + commit → tracker transition. |
| `stage-reflect.md` | reflect | reflect_inputs bundle → extract knowledge per 6-type taxonomy → memory_append per entry → if shipped, observe_ship_event. Zero-novel-signal path documented. |

## Known phase 5-mvp holes (deferred to 5b / 7-full / 8c / 8d)

1. `/flow status` + `/flow recover` are stubs → 8c.
2. No `skill:<name>` handler dispatch → 5b.
3. No SessionStart recall hook (`recall-pending.jsonl` writer) → 5b.
4. No subagent stage reference docs (plan / implement) → 5b. Spawned
   agents work from stage name + ticket dir only.
5. `/flow do` orchestration is in prose, not a script. Cannot run
   unattended; Claude must be in the loop.
6. No retry/backoff on tracker-cli failures → tunable in later phase.
7. `tracker_cli` exit code 1 lumps network/auth/not-found → split in
   later phase.
8. `timeout_min` in handler descriptor is informational only. No
   enforcement; the progress producer (`write_progress` / the `write`
   CLI / `quarantine_stale`) was deleted as dead (flow-dwd) rather than
   wired, so there is no producer to enforce against. The read-only
   hung-detection remnant was deleted too (flow-qp7).
9. The do-loop bash prose uses `"<KEY>"` / `"$STAGE"` syntax — variable
   substitution into the actual Bash invocations is on Claude. Reference
   docs document the variable names; the loop in SKILL.md sets them from
   the descriptor JSON.
