# e2e recipes — authoring guide

Read this at plan time — `verb-spec.md`'s recipe-settling step — the moment you settle a ticket's `e2e_recipe`.

## Why e2e matters

Every other stage reasons about the diff. The e2e stage is the only one that runs the change and watches it behave — the highest-leverage correctness signal in the pipeline, and the reason the stage defaults on (see `stage-e2e.md`). A recipe authored carelessly (a vague command, a missing fixture, an ambiguous pass signal) throws that leverage away just as surely as skipping the stage outright. Author it with the same care you'd want if you were the one debugging a red run at 2am.

## Anatomy of a recipe

A recipe is a single string, self-contained enough that the e2e stage can execute it unattended with no reinterpretation. It has four required parts plus an optional fifth:

- **Runner + exact command** — the tool and the literal invocation (`pytest tests/e2e/test_checkout.py -k full_flow`, `npm run e2e -- --grep smoke`, a `docker compose run e2e-suite`). Not "run the e2e tests" — the actual command. If a module is too heavy to run in one Bash call and must be chunked, author it to partition by node-id (`--collect-only -q`, then run each shard by explicit nodeids), never by `-k` class-name substrings which silently under-cover; `stage-e2e.md` Step 3 carries the operative protocol.
- **Env-prep** — anything the command needs before it runs: an auth refresh, a container/service bring-up, resource tuning. Must be non-interactive; a step that blocks on a prompt strands an unattended run. If credentials expire, name the refresh command, not "log in again."
- **Fixture** — the concrete input under test: a sample id, an account, a dataset. "Run the suite" without a fixture leaves the executor guessing which of many code paths actually gets exercised.
- **Expected pass signal** — how the executor tells green from red without judgment calls: an unambiguous token (`E2E_OK` / `E2E_FAIL`), a suite's green summary line, or an exit code. Red means the stage failed; a recipe with a fuzzy signal risks masking a real regression as a pass.
- **Evidence note** (optional) — what the e2e stage should preserve on the PR beyond the default. Absent, the stage keeps rung 1: the command plus a transcript tail. Present, it names which rungs to add on top: a baseline comparison to run and the scope of the expected delta (rung 2); a fingerprint spec — which output file, what counts as its sections/records, which before/after lines the ticket targets (rung 3); and, opt-in and human-authored only, a destination to upload the full artifact to (rung 4 — an `--auto` planner never writes one, and with no destination there is no upload). Keep it text-first: rungs 1-3 are local and cost nothing external. `stage-e2e.md` step 4 is where each rung renders.

## Sentinels

Two recipe values are not commands — they are conscious decisions the plan is allowed to make instead of a real recipe:

- `skip: <reason>` — this ticket has no meaningful e2e surface (a docs-only change, a change with nothing runnable to exercise). State the reason; the e2e stage reports it and finishes without executing anything. Never use this to dodge a real, runnable change — that's the convenient path this stage exists to close off.
- `test-ci-only` — run the repo's cheap no-frills CI/unit gate and nothing heavier. This is also the `--auto` floor when no cookbook exists yet (see below): never a silent skip, never a block, just the cheapest honest signal available.

## The cookbook convention

`<main-root>/.flow/e2e-recipes.md` is a per-repo decision table: "ticket touches X" -> runner/template, known-good fixtures, shared env-prep quirks (auth, containers). It is seeded by the first spec that settles a real recipe in a repo (post-gate, normal mode, per `verb-spec.md` step 6) and grows every time a new kind of change needs a new row. It is machine-local in user repos — `.flow/` is gitignored — and self-regenerating: a fresh machine with no cookbook just re-derives one via the same explore-propose path, it never blocks on the absence.

When the cookbook exists, author a ticket's recipe FROM it: match the row to what the ticket touches, fill in the fixture, confirm with the user. When it does not exist, explore the repo read-only (CI config, `mise`/`make`/`npm` tasks, `docker-compose`, test layout), propose 1-3 concrete candidate recipes, settle one, then seed the cookbook so the next ticket starts from a table instead of a blank repo.

Skeleton example:

```markdown
| Ticket touches         | Runner / template                  | Fixture              | Notes                        |
|------------------------|-------------------------------------|-----------------------|-------------------------------|
| Form generation         | `pytest tests/e2e -k forms` in container | sample company `ACME-01` | needs `docker compose up -d db` first |
| SQL transform            | `mise run e2e-sql` on host          | dataset `fixtures/sql/basic.csv` | no container needed |
| Docs-only                | n/a                                  | n/a                    | `skip: docs-only, no runnable surface` |
```

Real rows carry real commands and real fixtures for the repo they live in — the table above is a shape, not a template to paste verbatim.

`.flow/flow worktree create --e2e-recipe` is what seeds the settled recipe into frontmatter; it is the only flag this doc names.
