# new verb

`/flow new`. The full ticket-authoring front door: collect the problem and create a rich ticket through the tracker seam (`tracker_cli.py`, never the Atlassian MCP). Tracker-agnostic — Jira gets epics + sprints + a type vocab; beads degrades (no epic list, no sprint, the bead type enum). PROBLEM-capture only: collect the problem, no solution or plan (planning is the spec stage's job). Routed from SKILL.md's argument table.

All tracker calls go through the tracker CLI seam (the `tracker_cli.py` subcommands shown in the fenced blocks below), never the Atlassian MCP. Every subcommand named below is a real one (seam-checked).

## Step 0: Fetch tracker context

Run these two reads (Jira returns real data; beads degrades to the bead type enum + empty epics):

```bash
.flow/flow tracker --workspace-root . list-types
.flow/flow tracker --workspace-root . list-epics
```

- `list-types` → JSON `[{name, hierarchyLevel}]`. The hierarchy-1 entry is the epic/parent type; the hierarchy-0 entries are the type vocab.
- `list-epics` → JSON `[{key, summary}]`. Empty on beads (no epic-parent picker).

Also detect the current branch for a summary suggestion:

```bash
git branch --show-current
```

## Step 1: Infer the type (no ask)

Default to the first hierarchy-0 type whose name matches `task` (case-insensitive), else the first hierarchy-0 type returned. Bump to a `bug`-like type if the summary signals a defect (`fix`, `bug`, `broken`, `crash`, `error`, `regression`), or to a `feature`/`story`-like type on a clear feature signal (`add`, `implement`, `support`, `new`) — but ONLY pick a type whose `name` is actually in the fetched `list-types` set (beads has `feature`, not `story`; never invent a name). When in doubt, stay on the default. Do NOT ask. The chosen type shows in the Step 3 preview, and Edit is the correction gate.

## Step 2: Ask the author through the adapter

Use the adapter's user-input capability for exactly these fields. Claude Code may batch
them in its structured question tool (at most four); Codex and generic adapters ask
plainly and wait. Preserve the answers in orchestration context, not shell variables:

1. **Summary** — free-text (user picks "Other" to type). Offer 2 contextual suggestions: derive one from the branch name when on a feature branch (e.g. `fix/FT-500-login` → "Fix login bug"); derive the other from what was just being discussed in the conversation. Always include a placeholder so the user is prompted to type.
2. **Description** — free-text. Offer "Include git branch reference" (auto: ``Branch: `<branch-name>` ``), "Write custom description", "No description".
3. **Epic / parent** — single-select from `list-epics`, each shown as `<KEY> — <summary>`. Always include a **None** option (no parent). If exactly one epic exists, mark it the recommended default. Allow "Other" to type a key manually. On beads (empty list) this collapses to None — skip the question or present only None.
4. **Add to current sprint?** — Yes (default / recommended) / No. On beads this is a no-op (note it); you may still ask but it will degrade at Step 5.

Assignee, status, and labels are NOT asked:
- **assignee** = self by default, from `workspace.toml [tracker.<backend>] assignee_account_id` (see Step 4). No ask.
- **status** = To Do (set post-create, Step 5).
- **labels** = none (omit entirely).

## Step 3: Humanize the description (before create)

IF the `humanize` skill is present in this session, run the description through it now and use the rewritten text. This MUST happen before create: `tracker_cli create` wraps the body as a Content payload that is md→ADF-coerced at the Jira seam (flow-4op4), so a post-create humanize pass would be too late. Skip silently if the skill is not present. Because `new` is inline/orchestrator-run, don't end the turn on the rewrite: carry the rewritten text straight into the Step 4 preview in the same reply (the create itself still waits behind that confirm gate); see the inline-skill turn-continuation rule in `references/verb-do.md`.

## Step 4: Preview and confirm (the single correction gate)

Show:

```
## New ticket preview

| Field       | Value                |
|-------------|----------------------|
| Project     | <project / prefix>   |
| Type        | <inferred type>      |
| Epic        | <KEY — summary, or —>|
| Summary     | <summary>            |
| Description | <description or —>   |
| Assignee    | <self, or —>         |
| Status      | To Do                |
| Sprint      | <Yes / No>           |
```

Ask: "Create this ticket?" — Create (recommended) / Edit / Cancel. On Edit, go back to Step 1. On Cancel, stop.

Resolve the self assignee account id from config before the preview (omit `--assignee`
entirely when the key is absent; this covers Jira-without-config AND beads). Capture stdout
as the logical `assignee_account_id`; it must survive the preview's user-input boundary:

```bash
python3 -c 'import tomllib,sys
d=tomllib.load(open(".flow/workspace.toml","rb"))
tk=d.get("tracker",{}); b=tk.get("backend")
print((tk.get(b,{}) or {}).get("assignee_account_id","") or "")')
```

## Step 5: Create + post-create ops

Create (pass `--parent` only if an epic was chosen; pass `--assignee` only if the
logical `assignee_account_id` is non-empty). Materialize the approved values as
same-call shell variables, then build the optional flags as an array. The
assignments below are literal materializations of the approved logical values, not
state inherited from an earlier shell call:

```bash
EPIC="<approved epic key, or empty>"
ASSIGNEE_ACCOUNT_ID="<logical assignee_account_id, or empty>"
_create_args=(--summary "<summary>" --description "<description>" --type "<type>")
[ -n "$EPIC" ] && _create_args+=(--parent "$EPIC")
[ -n "$ASSIGNEE_ACCOUNT_ID" ] && _create_args+=(--assignee "$ASSIGNEE_ACCOUNT_ID")
.flow/flow tracker --workspace-root . create "${_create_args[@]}"
```

`create` writes JSON `{"key": "<newkey>"}` to stdout; parse `.key`. Non-zero exit → surface stderr and stop.

Then, in order (each best-effort; a degraded backend must not abort the run):

1. **Status → To Do** (best-effort):
   ```bash
   .flow/flow tracker --workspace-root . transition \
     --key "<newkey>" --to-state "To Do"
   ```
   The `transition` subcommand resolves the name→id itself. Tolerate exit 3 (no such transition / already there — e.g. beads, or a project already defaulting to To Do): log and continue.

2. **Sprint** (only if the author chose Yes):
   ```bash
   .flow/flow tracker --workspace-root . list-sprints
   ```
   Pick the entry with `state == "active"`. Then:
   ```bash
   .flow/flow tracker --workspace-root . set-sprint \
     --key "<newkey>" --sprint-id "<active sprint id>"
   ```
   On beads both calls return `{"supported": false, ...}` (exit 0) — nothing to do, continue.

## Step 6: Handoff

Print:

```
Created <KEY>: <summary>
Next: /flow <KEY>
```

If the description contains plan-like headings (case-insensitive line-anchored regex `^##\s+(Recommended\s+fix|Plan|Hardening|Fix|Implementation\s+plan)\b`), append:

```
Ticket body has a fix plan — /flow <KEY> will offer to use it as the spec-stage plan (skip PLANNING).
```

Then **offer to start the pipeline now** through the adapter's user-input capability;
authoring a ticket is usually a prelude to running it, so make the common next step one
keystroke: "Start the pipeline for `<KEY>` now?" Options: **Start now** (recommended) /
**Not yet**.

- **Start now** → route into the `spec` verb for `<KEY>` in this SAME session, exactly
  as if the user had made the host-appropriate Flow request: follow SKILL.md's adapter
  plan boundary, fetch the ticket, design the plan WITH the user, wait for explicit
  approval, then run the tail. The ticket you just created is the spec input.
- **Not yet** → stop. Surface the host-appropriate invocation (`/flow <KEY>` on Claude
  Code, `$flow:flow <KEY>` on Codex) for later.

Planning still happens in `spec`, never in `new` — the offer only chains into spec; it does not plan, write code, or invoke the pipeline without the user's yes.
