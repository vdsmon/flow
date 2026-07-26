# codebase-memory-mcp: bounded trial verdict (July 2026)

One-experiment trial of [DeusData/codebase-memory-mcp](https://github.com/DeusData/codebase-memory-mcp) v0.9.0 on brinta-data-platform (~330k first-party Python LOC, PYTHONPATH-rooted package layout), following the desk evaluation that skipped it for flow itself (PR #446: Python call graph blind to flow's bare/aliased flat-dir imports, their issue #875). Data-platform was the fairer test: package-qualified imports, real blast-radius questions ("this `specifics_lib` change touches which form jobs?"). Verdict: **skip here too**.

## Protocol

Throwaway binary from the release tarball (no installer, no agent config writes), `CBM_CACHE_DIR` pointed at a scratch dir, `auto_watch`/`auto_index` off, `.cbmignore` excluding `.flow/`, `.claude/`, `.codex/`, `.venv`, build/output dirs. Ground truth: `classify_icms_adjustment_types` (changed by FT-1348, commit 61682ed6f) — grep shows 1 definition, 2 references in `jobs/filing/bra/efd_fiscal/form_generation.py` (package-qualified from-import + direct call), 3 in its test file.

## Results

- Indexing is genuinely good engineering: 14,369 nodes / 86,982 edges in 4.7 s wall, layered ignores honored, clean CLI.
- **`trace_path --direction callers`: 0 results** on the target function, despite the node itself showing `in_degree: 2` and the call site being the easiest possible case — an unaliased package-qualified from-import. Cause fits the known resolution weakness: data-platform imports as `from specifics.spark...` (PYTHONPATH root) while the graph roots qualified names at the repo (`libraries.specifics_lib.specifics.spark...`), so inbound CALLS edges never connect.
- **Callee attribution actively misleads**: `withColumn` (pyspark) credited to a local test `FakeDataFrame`, a `logger.info` credited to an unrelated service's `StructuredLogger`.
- **`detect_changes`**: an unrecognized `--since` ref fails silently (returned only the working-tree `.cbmignore` diff); with a resolvable ref it returns "every symbol in every changed file" (394 impacted symbols across 20 files) — a superset git diff + grep already provide, with none of the caller-edge precision that was the point.

## Conclusion

The one capability worth adopting (diff → impacted-caller mapping) depends on exactly the graph edges that fail on this repo's import layout, and the misattributed callee edges make the output worse than no answer. Skip for both flow and data-platform. Re-evaluate only if their Python import resolution learns PYTHONPATH-style package roots and alias attribution; the re-test is this same one-hour protocol.
