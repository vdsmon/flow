---
name: bd
description: Core bd (beads) CLI surface for issue tracking. Use whenever running a bd command to find ready work, show/create/update an issue, wire a dependency, or sync issue state. Covers the six recurring verbs (ready, show, create, dep, update, sync), the local-Dolt plus refs/dolt/data sync model, and the .beads/issues.jsonl passive-export caveat. A lean reference, not the full bd command set; do not trigger on unrelated git or shell work.
allowed-tools: Bash(bd:*)
---

# bd (beads)

A dependency-aware issue tracker. Issues live in a **local Dolt database** (`.beads/`). Run `bd prime` once per session for the project's full workflow context; this skill is the lean re-orientation for the verbs you reach for every session.

## The six verbs

| verb | usage | what it does |
| --- | --- | --- |
| **ready** | `bd ready` | List open work with no active blockers. `--claim` atomically claims the first match; filter with `-a/--assignee`, `-p/--priority`, `-t/--type`. |
| **show** | `bd show <id>` | Show issue details. `--short` for one-line, `--long` for all fields. Accepts multiple IDs. |
| **create** | `bd create "title" -t <type> -p <pri> -d "desc"` | Create an issue. `--type` is one of `bug\|feature\|task\|epic\|chore\|decision` (default `task`); `--priority` 0–4 (0 = highest, default 2); `--deps 'blocks:<id>'` wires dependencies at creation. |
| **dep** | `bd dep add <issue> <depends-on>` | Wire a dependency: `<issue>` depends on (is blocked by) `<depends-on>`. Equivalent flag forms: `bd dep add <issue> --blocked-by <id>` / `--depends-on <id>`, or the reversed shorthand `bd dep <blocker> --blocks <blocked>`. |
| **update** | `bd update <id> --claim` | Update one or more issues. `--claim` sets assignee=you + status=in_progress; `-s/--status <status>`, `--add-label`, `-p/--priority` for other edits. |
| **sync** | `bd dolt push` / `bd dolt pull` | There is **no bare `bd sync`**. Issue state syncs through Dolt version control — see Sync below. |

Lifecycle in one line: `bd create` → `bd update <id> --claim` → work → `bd close <id>`.

## `--json` field & flag map

The schema quirks every session re-discovers when parsing bd output. Keys first:

| you expect | bd actually uses |
| --- | --- |
| `type` | `issue_type` (`type` is the DEPENDENCY kind, not the issue type) |
| `closure_reason` | `close_reason` |
| `summary` (headline) | `title` (`bd list --json`) |
| `labels: []` | key OMITTED when empty — read `item.get("labels") or []` |
| comment `body` | `text` — and comments appear only with `bd show --include-comments` |

Flag/visibility quirks:

- `bd list` defaults to **limit 50** and sorts by priority — any join/dedup over the full set needs `--limit 0`.
- `bd list` **hides closed** issues by default: `--status closed` or `--all` to see them.
- `bd ready` **excludes deferred** issues — deferring in place is how you drop a bead from a drain loop.
- A leading-dash title aborts (`bd create "--foo"` parses as a flag): use `--title="--foo"`.
- Comment ORDER is not guaranteed newest-last; pick "newest" by max timestamp, not last element.

## Gotcha: `bd edit` is banned

`bd edit` opens `$EDITOR` and blocks an agent forever (waits on stdin). Never run it. Use `bd update <id> --<field> <value>` to change any field non-interactively.

## Sync — two distinct paths

These are separate mechanisms. Do not conflate them.

### 1. Dolt data sync (the live truth)

Issues live in a local Dolt DB. Sync uses **`refs/dolt/data`** on your git remote:

```
bd dolt push    # push issue commits to the Dolt remote
bd dolt pull    # pull issue commits from the Dolt remote
```

The shared Dolt DB is the **local source of truth** for issue state.

### 2. `.beads/issues.jsonl` passive export (git-portable)

`.beads/issues.jsonl` is a **passive, git-portable export** — a snapshot, not the live DB. Regenerate and commit it on a branch/PR:

```
bd export -o .beads/issues.jsonl
# then commit it on a branch/PR — NEVER push main
```

The jsonl is the export; the Dolt DB is the truth. Keep the two paths separate: `bd dolt push/pull` moves the live data, `bd export` produces the committable file.

## flow's bd gotchas

beads-specific behaviors flow has hit and worked around.

### bd init is invasive

Bare `bd init --prefix <prefix>` generates an AGENTS.md plus a `.claude/settings.json` (a bd-prime SessionStart/PreCompact hook) and **auto-commits** them. Those changes are unwanted in a shared repository and irrelevant to non-Claude harnesses.

flow invokes it headless instead:

```
bd init --prefix <prefix> --skip-agents --non-interactive
```

`--skip-agents` suppresses the AGENTS.md + Claude settings generation; `--non-interactive` is headless/background safety (flow often runs detached).

### Ticket stage must transition the backend, not just frontmatter

Stamping `.flow/tickets/<KEY>.md` frontmatter `status: in_progress` does **not** move the bd issue. On beads it stays `open`, so `bd ready` still lists the in-flight ticket and a parallel fleet agent can double-pick it.

Fix: the ticket stage runs a best-effort backend transition `open → in_progress` (e.g. `bd update <id> --claim`).

### No in_review state; commit must NOT auto-close via done

beads exposes only `in_progress | blocked | closed` — there is **no** `in_review` state. The commit stage transitions the ticket to `in_review`; on beads that transition is unavailable (tracker exit 3).

Do **NOT** fall back to `--to-state done`: closing the ticket at commit is premature in a PR-based flow (the PR is not merged yet, and `create_pr` / `review_loop` still run after this stage). Instead leave the ticket `in_progress`, log a warning naming the missing `in_review` transition, and continue — a human or a later merge step closes the ticket.

See `references/stage-commit.md` exit-3 for the authoritative rule.
