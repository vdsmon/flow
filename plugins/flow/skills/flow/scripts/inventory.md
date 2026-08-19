# inventory: API/contract reference

> **Navigation.** The CURRENT script map is `MODULE.md`. This file keeps the
> API/contract tables (Jira REST mapping, beads CLI surface, `state.json` schema)
> a reader needs assembled in one place. Build history
> lives in git.

Contract sections (grep the heading):

- §Jira API inventory + §Status normalization mapping + §HTTP error → exception / TransitionResult mapping
- §Forge (PR host) surface: operation surface, `[forge]`, and optional `[models]` workspace schemas
- §Workspace bootstrap — handler composition, transactional markers, postconditions
- §Beads CLI surface — subcommands, state normalization, transition synthesis, is_shipped contract
- §Dispatcher state machine — stage lifecycle, `state.json` schema, atomic-write contract, quarantine, exit codes, handler-descriptor shape, revision sub-run, TOCTOU invariant
- §Memory cohort — `memory_append` / `recall` / `memory_embed` + `[memory.semantic]` config
- §Integration layer — `tracker_cli` per-backend contract, descriptor extension

## Jira API inventory

Source: the MCP Atlassian call set the Jira adapter replaced. The originating `jira-workflow` skill is gone; this table is now the only record of the mapping.

Distinct MCP Atlassian functions exercised: **7**.
Direct REST replacements listed below.
A Protocol method with no MCP predecessor is marked **NEW** — implemented for tracker-seam parity (a jira and a beads workspace answer the same Protocol) and covered by mocked tests only.

## Calls used by jira-workflow

| # | jira-workflow MCP function                 | REST endpoint                                                              | Tracker Protocol method                       |
|---|--------------------------------------------|----------------------------------------------------------------------------|-----------------------------------------------|
| 1 | `getAccessibleAtlassianResources`          | `GET https://api.atlassian.com/oauth/token/accessible-resources`           | constructor-time helper (not a Protocol method) |
| 2 | `atlassianUserInfo`                        | `GET /rest/api/3/myself`                                                   | constructor-time helper (not a Protocol method) |
| 3 | `getJiraIssue`                             | `GET /rest/api/3/issue/{issueIdOrKey}?fields=...`                          | `get(key) -> Ticket`                          |
| 4 | `searchJiraIssuesUsingJql`                 | `POST /rest/api/3/search/jql` (v3 paginated)                               | `list_assigned(filter)`, subtasks (folded into `get` ticket build) |
| 5 | `getJiraIssueRemoteIssueLinks`             | `GET /rest/api/3/issue/{issueIdOrKey}/remotelink`                          | folded into `get(key).links` field            |
| 6 | `getTransitionsForJiraIssue`               | `GET /rest/api/3/issue/{issueIdOrKey}/transitions?expand=transitions.fields` | `list_transitions(key) -> list[Transition]`  |
| 7 | `transitionJiraIssue`                      | `POST /rest/api/3/issue/{issueIdOrKey}/transitions`                        | `transition(key, transition_id, fields) -> TransitionResult` |

JQL used:
- assigned filter: `assignee = currentUser() AND statusCategory != Done ORDER BY updated DESC`
- subtasks: `parent = <KEY>`
- linked: `issue in linkedIssues(<KEY>)`

## Tracker Protocol surface with no MCP predecessor

Required so a jira and a beads workspace answer the same Protocol calls; written from the Atlassian REST API v3 + Agile REST API docs.

| Protocol method            | REST endpoint                                                                  | Notes |
|----------------------------|--------------------------------------------------------------------------------|-------|
| `create`                   | `POST /rest/api/3/issue`                                                        | Body: `fields: {project, issuetype, summary, description (ADF), parent, labels, assignee, priority}`. |
| `comment(body)`            | `POST /rest/api/3/issue/{key}/comment` `{body: <ADF>}`                          | ADF v3 required |
| `link(from,to,kind)`       | `POST /rest/api/3/issueLink` `{type:{name:<mapped>}, inwardIssue:from, outwardIssue:to}` | seam kind→Jira name: `blocks`/`depends_on`→`Blocks`, `relates`→`Relates`; unknown passes raw. Direction: `inwardIssue`=from=the blocked/dependent issue, `outwardIssue`=to=the blocker. |
| `state(key)`               | `GET /rest/api/3/issue/{key}?fields=status,resolution`                          | derives `TicketState` with normalized + diagnostic |
| `project_requires_pr()`    | `GET /rest/api/3/workflow/search?projectKey=<P>&expand=transitions.rules` (workflow scheme) | flag iff any transition to Done category has linked-PR validator. **Conservative default = False** if endpoint unauthorized. |
| `is_shipped(key)`          | PURE READ: frozen `.flow/memory/<ns>/ship-events/<key>.json` → return shipped; else `state()` + ship predicate | adapter MUST NOT write |
| `set_sprint(key, sprint_id)` | `POST /rest/agile/1.0/sprint/{sprintId}/issue` `{issues:[key]}`                | cap-gated: `NotSupported` on beads |
| `list_sprints(project)`    | `GET /rest/agile/1.0/board/{boardId}/sprint?state=active,future,closed` (needs board lookup) | cap-gated: `NotSupported` on beads |
| `get_attachments(key)`     | `GET /rest/api/3/issue/{key}?fields=attachment`                                 | cap-gated: `NotSupported` on beads |

## Comment format on JiraAdapter

The adapter implements every method on the Protocol, cap-gated ones included; nothing is advertised, so an unsupported op is only ever a `NotSupported` raise.

Markdown comments are the one thing it refuses.
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

`adapter_mapping_diagnostic` records which rule fired (e.g. `"category=indeterminate + native='In Review' matched in_review heuristic"`), so a wrong mapping is diagnosable from the record itself.

## Authentication

**Basic auth with API token**, per the human's decision.
Adapter reads the brinta-ai credential store `<config-dir>/git-credentials.json`
(config dir: `$BRINTA_CONFIG_DIR` when set, else `~/.config/brinta`; written by
`brinta-ai setup`):

- `.atlassian.email` — Atlassian account email (the username for basic auth)
- `.atlassian.api_token` — token from `https://id.atlassian.com/manage-profile/security/api-tokens`

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
| 401    | any                        | —                                                          | raise `TrackerConfigError("invalid credentials: refresh the Atlassian token with `brinta-ai setup`")` |
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
_RE_WRONG_SOURCE = re.compile(r"(?i)\btransition\b.*\b(not valid|invalid|cannot be applied)\b")
_RE_VALIDATOR = re.compile(r"(?i)\bvalidat(or|ion)\b.*\b(fail|error|reject)\b")
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
Board selection is not configurable: the adapter takes the first active scrum board and logs a diagnostic when several exist (`tracker_jira._resolve_scrum_board`). Add a config key the day a project actually has two boards.

## Forge (PR host) surface

Pluggable PR-host seam (`forge.py` Protocol + `forge_cli.py` + `forge_github.py` + `forge_bitbucket.py`), structural twin of the tracker seam. Selected by `[forge] backend` in `workspace.toml`; the block is OPTIONAL (absent = no forge, `create_pr`/`review_loop` stay `none`).

`create_pr` takes an authored PR body from the stage (`references/stage-create_pr.md`) via `--body-file`: it runs `pr_body.scrub` (em-dash → punctuation, sentence-case `# Heading`, flatten `- **Term:**` bullets) as a de-AI floor, on a bitbucket forge flattens `<details>` wrappers to `###` headings (`pr_body.flatten_details`; Bitbucket renders no raw HTML in markdown) and appends the deterministic `Closes` footer (`pr_body.closes_footer`, extracted from the HEAD commit trailer), then runs `pr_body.enforce_cap` as a deterministic size net (shrink largest fenced blocks → drop `<details>` bodies keeping `<summary>` → hard-truncate; cap ~32000, the stricter forge floor) so an oversized `## Evidence` body can never fail `open_pr`. With no `--body-file` it falls back to the old commit-derived body (`pr_body.build_body`: strip the `ticket:`/`files:` trailer, keep `Closes <KEY>` as a footer, unwrap prose hard-wraps). On first open it calls `set_default_reviewers` (swallowing `NotSupported` + any `ForgeError` so a reviewer hiccup never fails an open PR). Bitbucket implements it; GitHub raises `NotSupported`.

### Operation surface (forge_cli subcommand → gh / brinta-ai)

| Op (Protocol / `forge_cli`) | GitHub (`gh`) | Bitbucket (`brinta-ai bitbucket`) |
|------|------|------|
| `detect_pr` / `detect-pr` | `gh pr list --head B --state open --json number,url,isDraft,baseRefName,headRefName,state` | `brinta-ai bitbucket GET repositories/WS/RS/pullrequests?state=OPEN` + filter `source.branch.name` |
| `pr_info` / `pr-info` | `gh pr view PR --json number,url,isDraft,baseRefName,headRefName,state` (PR-number reverse lookup, ANY state — revise reads `head`+`state`/detects MERGED; None on empty/garbage JSON, ForgeError on absent PR) | `brinta-ai bitbucket GET .../pullrequests/PR` → `_pr_from_api` (None on empty body) |
| `open_pr` (lib-only; no forge_cli subcommand — create_pr.py drives it) | `gh pr create --base --head --title --body [--draft]` | `brinta-ai bitbucket POST .../pullrequests '{title,source,destination,draft,description}'` |
| `ci_rollup` / `ci-rollup` | `gh pr view PR --json statusCheckRollup` (green = non-empty + every check COMPLETED-SUCCESS) | `brinta-ai bitbucket GET .../pullrequests/PR/statuses` → Pipeline entry state (SUCCESSFUL→green, INPROGRESS→pending, FAILED/STOPPED/ERROR→failed) |
| `review_threads` / `review-threads` | `gh api graphql` — unresolved threads, normalized (drops resolved) | CodeRabbit actionable inline findings via paginated `.../comments`, unresolved only |
| `bot_review_present` / `review-status` | **NotSupported** (no review bot on the GitHub self-target; degrades to `{"supported": false}`) | `.../pullrequests/PR/statuses` CodeRabbit entry → true on any terminal state (SUCCESSFUL/FAILED/STOPPED/ERROR = the review bot has finished); the mandatory pre-thread-poll gate in `stage-review_loop.md` §3 |
| `post_reply` / `post-reply` | `gh api graphql addPullRequestReviewThreadReply` | `brinta-ai bitbucket POST .../comments '{content.raw, parent.id}'` |
| `resolve_thread` / `resolve-thread` | `gh api graphql resolveReviewThread`; returns bool `isResolved` | `POST .../comments/CID/resolve` then re-fetch + verify `.resolution != null` |
| `mark_ready` / `mark-ready` | `gh pr ready PR` | `brinta-ai bitbucket PUT .../pullrequests/PR '{draft:false}'` |
| `merge` / `merge` | `gh pr merge PR --squash` | `brinta-ai bitbucket POST .../pullrequests/PR/merge '{merge_strategy:squash}'` |
| `delete_branch` / `delete-branch` | `git push origin --delete B` | `git push origin --delete B` |
| `set_default_reviewers` (no `forge_cli` subcommand; `create_pr` calls the adapter directly) | **NotSupported** (solo repo, CODEOWNERS covers reviewers) | `GET user` (resolve author) + `GET .../default-reviewers`, drop author by `account_id`, `PUT .../pullrequests/PR '{reviewers:[{uuid}...]}'` |

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

### Optional `[models]` workspace schema

A workspace may give a stage's agents a model/effort hint. Missing keys inherit the
driver session model, and a host that does not accept a hint ignores it. These are
preferences, not execution provenance.

```toml
[models]
implement = "sonnet"          # one string = every agent this stage launches

[models.code_review]          # or keyed by the ROLE the stage launches
reviewer = { model = "gpt-5.6-sol", effort = "high" }
fixer = "sonnet"
```

The resolver is `model_resolve.resolve_agent_hint(root, stage, role, field)` (facade:
`model --stage <s> [--role <r>] [--field model|effort]`). Values `off`, `none`,
`false`, and the empty string mean inherit. A role-keyed table naming exactly one
role resolves without `--role` (the generic launch recipe carries none); two or more
roles require the caller to name one. Liveness is judged against THIS workspace
(`validate_workspace._validate_stage_hint`): an inline/none-handled stage is checked
against `_LAUNCH_SITES` (the roles its prose launches), while a `subagent:`-wired
stage accepts a string hint anywhere — and a hint for a stage outside the pipeline
is rejected outright. There is no provider matrix, snapshot, attestation, or route
override.

### Planning handoff

Planning produces one human-approved Markdown file and records the inspected base SHA.
`flow-worktree create` resolves that base, writes the plan to `stages/plan.out`, and
marks the stage complete. The ticket claim, isolated worktree, atomic run state, and
planned-file ownership remain the bootstrap's durable safety boundaries.

## Workspace bootstrap

### Handler composition

- **bare**: every stage in `pipeline.stages` uses `stage-registry.toml`'s
  `default_handler`. Always available.
- **custom**: caller supplies `--handler <stage>=<handler_string>` flags. Init
  validates handler strings against the closed grammar
  (`inline | none | subagent:<type>`) and rejects unknown stages.
- On reconfigure, a prior handler that differs from the current registry default is
  preserved. Precedence: `--handler` > existing customization > default.

### Transactional bootstrap markers

| File                          | Lifecycle                                                  |
|-------------------------------|------------------------------------------------------------|
| `.flow/.initializing`         | created BEFORE any mutation; left in place on failure      |
| `.flow/.init-progress`        | append-only JSONL of completed phases; consumed by --resume |
| `.flow/.initialized`          | atomic rename from `.initializing` ONLY after postconditions pass |

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
5. `[memory]` block has `namespace` and `compounding`.
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

Unknown natives default to `open` with an `adapter_mapping_diagnostic` flagging the fallback, so an unfamiliar status is visible in the record instead of silently reading as open.

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

### Backend reach

bd accepts markdown comments (`bd comment --stdin`) and records `closure_reason` on `bd close`.
It is otherwise narrow: `set_sprint`, `list_sprints`, `get_attachments`, `download_attachment` raise `NotSupported`.

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

### state.json schema

```json
{
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
| dispatch_stage      | 5    | stale foreign lease (needs `FLOW workspace repair` takeover) |
| dispatch_stage      | 7    | lost lease (another run took over)               |

### Handler-descriptor JSON shape (`dispatch next` stdout)

```json
{
  "done": false,
  "stage": "plan",
  "handler_type": "subagent" | "inline" | "none",
  "subagent_type": "Plan",
  "reference_doc": "references/stage-plan.md",
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
The canonical-snapshot pattern is live: a content hash is captured once at `init` and compared on each `next` call via `snapshot.py`. The hash covers three components (see the snapshot.py module docstring): workspace.toml text, stage-registry.toml text, and the engine's own skill tree over the MAIN checkout.
