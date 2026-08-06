# Investigate

`FLOW investigate [<report> ...]` root-causes a reported error or incident from real evidence, then proposes a fix and offers the ticket pipeline. Someone reported a problem; the job is the *actual* cause traced through logs, state, and code, not a plausible one. The report text is whatever followed the command: pasted text, a stack trace, a tracker key, a chat or PR link. The problem can live in any repo or runtime system, not just the one the session happens to be in; follow the evidence wherever it goes. (Ported from the standalone `investigate` skill, 2026-08-06, human ruling A: one entry point, with the delivery handoff built in.)

The command is read-only end to end: an investigation never applies its fix, mutates a tracker, or edits a repository. The only writes it leads to are the ones the human explicitly takes through the handoff below.

## The driver runs it, not an agent

The investigation lives in the driver conversation. Its one non-negotiable behavior (stop and raise to the human when a source is unreachable) only works where the human can answer mid-dig; a spawned agent can only return. The driver MAY fan out read-only agents over reachable, independent sources and synthesize their findings, but parsing the report, raising blockers, and the final synthesis stay with the driver. A fan-out agent that hits a wall reports the wall; the driver raises it.

## The prime directive: never infer past a missing source

If a piece of evidence you need is unreachable (no access, a link you cannot open, an ambiguous reference, a log you cannot pull), STOP and raise it to the human. Ask for the access or the clarity. Do not fill the gap with a guess.

Inferring past a wall is the single failure this command exists to prevent: a guessed root cause looks like an answer, sends the human chasing it, and is usually wrong, while a raised blocker costs one message and gets the truth. Letter equals spirit: "I'll note it's probably X and move on" is inferring, and "based on the error name it's likely..." without reading the log is inferring. A cause sentence not backed by something actually read means stop, that is the directive firing.

The classic rationalizations, each wrong the same way:

- "The error message makes the cause obvious." Error messages name the symptom, not the cause; pull the log.
- "I can't reach prod, but it's probably the same as dev." Prod ran prod data and prod config; get the prod evidence.
- "Asking for access is annoying." A wrong inference costs far more round trips than one access ask.
- "I'm fairly confident, I'll flag it as a guess." A flagged guess still gets chased as if real; if it's a guess, the investigation is not done.
- "The ticket is vague but they probably mean the nightly run." Assuming the target is inferring; ask which run, job, date, environment.
- "The missing piece won't change the answer." Then it is cheap to confirm; if it could change the answer, it must be confirmed.
- "The identifiers are close enough to the code's." Close-but-wrong mappings send the fix to the wrong code path; confirm the real identity.
- "My credentials expired, so the human should fetch it." An auth the session can perform itself (an SSO login that opens a browser and returns) is not a blocker; authenticate and pull. Raise only for access genuinely unobtainable.
- "My local checkout is the code that ran." The deployed system builds from the merged branch, not the working copy. `git fetch`, then require `git rev-list --count HEAD..origin/<branch>` to print `0` before trusting `git log`, `git blame`, Read, or a local repro; ancestry of one fix commit (`merge-base --is-ancestor`) does NOT prove the checkout is current. Until the count is 0, read deployed code with `git show origin/<branch>:<path>`.

## Steps

1. **Parse the report.** Extract the symptom (exact error, failing job/DAG/task/endpoint, what the reporter observed); every referenced system (job/run ids, storage paths, table names, ticket keys, threads, log groups, commit/PR refs, repos); and what is not yet known (which run, which date, which environment, whose account, which repo). Sanity-check the identifiers against the real platform before digging: reporters paraphrase, and `git log --all -S '<identifier>'` distinguishes "never existed" from "renamed". Identifiers that do not resolve are a raise, not a guess.

2. **Build the access ledger, then batch-raise.** List every system step 1 says the dig needs and mark each reachable or blocked. Resolve the blocked ones as ONE batch with a single question through the host input surface before digging (one interruption, not ten); new blockers found mid-dig are raised just-in-time the moment they appear. Be specific in the ask: name the exact log, run, or table and why it changes the answer. Authenticate anything the session can authenticate itself. Two chronically blocked shapes: a prod orchestrator whose logs are web-UI-only (ask for a paste of the FULL failing task log, since the traceback discriminates the cause), and a system with no loaded connection at all.

3. **Dig wide across reachable sources.** Several angles at once, since an incident usually leaves evidence in more than one place: code and git history (sync first, per the checkout rationalization above); runtime and cloud logs for the real exception in context; state (query what the job actually saw: row counts, nulls, schema drift, the specific records); history (tracker for prior occurrences, chat for what people already noticed); domain sources when format or business logic is in play. Form falsifiable hypotheses before chasing them ("if X is the cause, the log will show Y"), rank them, and let evidence confirm or kill each. When the failure reproduces outside prod, build a fast pass/fail loop on the exact ref the failing system built from (a detached worktree at `origin/<branch>`), and bisect; a repro on a stale checkout proves nothing. Keep an evidence trail per system: checked, found (clean findings rule things out and are half the deliverable).

4. **Deliver the report.** (a) Root cause, each claim tied to specific evidence: this log line, this commit, this row count. Unreached certainty is stated as exactly what is unconfirmed and what evidence would close it, never rounded up to a conclusion. (b) The evidence trail, clean checks included. (c) A proposed fix as a concrete diff or step plan, NOT applied: the human decides. If the prime directive left a blocked source unresolved, the report says so plainly.

## Handoff to delivery

The handoff is conditional on the verdict, and the human always chooses; investigation never starts a run on its own.

- **No real issue.** "Nothing is actually wrong" is a first-class outcome, not a failed investigation: expected behavior, stale report, already fixed upstream, or reporter misread. Say so with the evidence that rules it out, and offer nothing. A ticket offered on a non-issue teaches the human to ignore the offer.
- **Real issue, in a flow workspace:** offer `FLOW ticket create --request "<the root cause and proposed fix>"`, and note the new key then runs as an ordinary `FLOW <key>` delivery, in this conversation or a fresh one. When the human instead says fix it inline here, the investigation conversation simply continues into the named target's ordinary lifecycle, exactly the FT-1569 shape (2026-08-04: investigate found the defect, the human said "do it inline here", and the same session delivered the PR).
- **Real issue, outside any flow workspace** (the command is workspace-optional): deliver the report and, when the fix belongs in a repo that IS an initialized flow workspace, say so and name the `FLOW ticket create` invocation to run there. No workspace means no tracker write is offered at all.

**The investigation IS the grounding.** When delivery continues in this conversation, planning does not start over: the dig already read the code, pulled the logs, and queried the state, so the delivery-plan grounding step draws on that evidence instead of re-deriving it (no second pass over the same tables for what is already in context; re-probe only what could have changed since the dig or what the plan needs and the dig never touched). The proposed fix from the report seeds the plan draft nearly whole. Everything else about the gate is unchanged: the plan is still written out complete, still assessed, and still presented through the native plan gate for explicit approval; a fresh session running the new key later gets none of this context and grounds normally.

A cross-system finding that implicates flow's own machinery is not this command's to file: leave it in the report for the scrutiny sweep's evidence trail.
