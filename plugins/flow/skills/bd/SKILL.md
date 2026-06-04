---
name: bd
description: Core bd (beads) CLI surface for issue tracking — the six verbs you re-discover each session (ready, show, create, dep, update, sync), the local-Dolt + refs/dolt/data sync model, and the .beads/issues.jsonl passive-export caveat. A lean reference, not the full bd command set.
when_to_use: Reach for this whenever you run a bd command — finding ready work, showing/creating/updating an issue, wiring a dependency, or syncing issue state (bd dolt push/pull, bd export to .beads/issues.jsonl). Fires on bd usage only, not on unrelated git or shell work.
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
