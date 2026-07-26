# flow

An autonomous, state-aware ticket→PR pipeline for Claude Code and OpenAI Codex — one engineer's software factory: spec in, working software out.

```
ME                          MACHINE                              ME
target ---> plan approval ---> worktree -> implement -> ... -> draft PR ---> PR review
               one gate            one rooted session                 the deliverable
```

Built for exactly one human (me), operated daily by many agent sessions. This repo is public as a reference, not a product: **read, don't run**. The harness encodes one person's judgment — the ideas travel (evidence-first dispatch, witnessed-failure guards, sealed self-evolution, enforced-true docs); the artifact is not meant to be installed by anyone else. [VISION.md](VISION.md) is what it is and what it refuses to become.

Three layers, one design: exact behavior lives in stdlib-only Python (`scripts/`), judgment lives in prose the agent executes (`SKILL.md` + `references/`), and host differences live at the adapter boundary. The dispatcher emits descriptors; the model acts on them.

Orientation (agents start here): [AGENTS.md](AGENTS.md), then `plugins/flow/skills/flow/scripts/MODULE.md` — the live map of the engine and the reference-docs index. Workspace setup and operations: `plugins/flow/skills/flow/references/command-workspace.md`. Experiment records behind the design bounds: [docs/research/](docs/research/).

Develop: `cd plugins/flow/skills/flow/scripts`, then `mise run lint` / `mise run test` / `mise run check:commands` / `python3 seam_check.py` — CI runs all four on every push. Runtime is stdlib-only `python3`.

MIT licensed.
