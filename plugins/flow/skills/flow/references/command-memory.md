# Memory commands

Flow memory is append-only source data plus derived indexes. Runtime layout v2 stores
each namespace under `.flow/memory/<namespace>/`. Search is read-only; pruning writes
supersession records after confirmation; rebuilding replaces only derived index data.

## `FLOW memory search [<query>]`

Translate the public options to the internal search seam:

```bash
FLOW_HARNESS="<harness>" "<facade>" recall ["<query>"] \
  [--tickets <comma-separated-keys>] \
  [--label <facet:value>] [--digest] \
  [--semantic] [--threshold <float>] \
  [--branch <branch>] [--top-n <limit>] \
  --workspace-root .
```

Rules:

- Repeatable public `--ticket` values become one comma-separated `--tickets` value.
- `--label` hard-filters before ranking. A label-only search returns the entire live
  cluster, newest first; a miss is an empty result, not an error.
- `--digest` requires `--label` and renders a human-readable card grouped by memory
  type. Without it, format the returned JSON compactly.
- `--semantic` forces the semantic overlay. `--threshold` is a cosine floor, not a
  top-k substitute. If embeddings are unavailable, report the fallback and retain
  lexical results.
- `--branch` scopes relevance and `--limit` caps ordinary ranked results. Label-only
  retrieval remains exhaustive.
- A multiline query must be written with the host's exact-write primitive and passed
  internally through `--query-file`; never interpolate arbitrary user text into a
  shell command.

Search excludes superseded records. It never records usage merely because a user
searched interactively; delivery planning records pending usage only after approval
and only in the bound worktree.

## `FLOW memory prune`

Pruning is attended and confirm-gated. It retires disproved or fully superseded
knowledge by appending tombstones; it never rewrites the source corpus.

1. Produce a bounded usage-ranked worklist:

   ```bash
   FLOW_HARNESS="<harness>" "<facade>" sweep-knowledge propose \
     --type all --with-usage --workspace-root . > <absolute-worklist>
   ```

2. Inspect at most the first 30 candidates. Verify every assertion against current
   default-branch code and linked delivery evidence. Unused is a review signal, not
   proof of obsolescence. Recent entries and entries associated with a recorded
   search miss receive a strong keep bias.
3. Build a manifest containing the superseded id, superseding ticket or maintenance
   receipt, and concrete rationale.
4. Show counts, representative entries, and exact rationales. Confirm before the
   first write.
5. Apply idempotently:

   ```bash
   FLOW_HARNESS="<harness>" "<facade>" sweep-knowledge apply \
     --manifest <absolute-manifest> --workspace-root .
   ```

6. Report applied, already-superseded, and failed records separately. Never treat a
   partial apply as an all-or-nothing failure.

If the harness exposes a separate project-memory store, it may be reviewed in a
second, explicitly confirmed phase. Back it up outside the store first. Never remove
feedback-type records, and verify any “captured in repository” claim by reading the
repository before deletion.

## `FLOW memory rebuild [--full]`

Rebuild the derived semantic sidecar:

```bash
FLOW_HARNESS="<harness>" "<facade>" recall --reindex [--full] --workspace-root .
```

Without `--full`, embed only source records not present in the current sidecar. With
`--full`, regenerate the sidecar from all live source records. A rebuild must not
modify `knowledge.jsonl`, usage records, friction, ship events, or supersession
history. First-time semantic enablement needs one full rebuild. Report lexical
fallback clearly when the embedding backend is unavailable.

## Delivery-time memory use

Planning searches with the ticket title, body, and a short intent preamble. That
front half is read-only. After approval and worktree bootstrap, record the recalled
ids against the exact feature branch and bound worktree so dispatcher initialization
can promote them into the run receipt. See `delivery-plan.md`; interactive search does
not perform this write.
