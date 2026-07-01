# Session 3 — Component 2: `_aud` Rollback

**Date:** 2026-07-01
**Ticket:** ASP-1613 → sub-task **ASP-1615** *"Implement rollback component using `_aud` tables for
gold table teardown"*
**Branch:** `feat/asp-1615-rollback-aud` (off `main`)
**Outcome:** Component 2 built as a new file `rollback.py`, offline-tested (17 new tests; suite
26 → 43, all passing). No live AWS run this session (design-time only).

---

## Goal for this session

Build **Component 2**: consume Component 1's `WatermarkResult` and, per table, inspect the corresponding
`_aud` gold table to roll back changes made after the watermark, returning a summary of removed/updated
data. Keep utilities in `util.py`; put the component in a separate `.py` first, then reassess merging
into `watermark.py`.

---

## Research (before writing code)

- Pulled **ASP-1614** (C1, In Review) and its parent **ASP-1613**, then enumerated the sub-tasks:
  **ASP-1615** (C2, this session), **ASP-1616** (round-trip checkpoint test), **ASP-1617/1618** (Behave
  vocabulary), **ASP-1619** (execution/IO separation).
- Established the `_aud` change-capture model from `int-CDCv2-BaseTemplate`:
  - `_aud` **is the GoldTable** (`GoldTable = <silver>_aud`), an **Iceberg** table
    (`table_type = ICEBERG`) → Athena supports row-level `DELETE`.
  - It's an append-log of change records. Columns: `ind` (operation; `'D'`=delete, else insert/update),
    `changebatchid`, `modifiedcolumns`, `modifiedon` (the C1 watermark column).
  - Silver/base current-state views derive as `... WHERE ind <> 'D'`.
  - The framework's own incremental read uses `WHERE to_timestamp(modifiedon) > to_timestamp('<wm>')`.

## Key decisions (confirmed with user)

| Decision | Choice |
|----------|--------|
| Execution model | **Dry-run by default** (summary only, offline-testable) **+ `--apply`** for the live Iceberg `DELETE` (requires `--mode record`). |
| Rollback semantics | **Truncate the append-log after the watermark**: `DELETE FROM "<db>"."<t>_aud" WHERE modifiedon > <watermark>`. Restores state as of N → C1 re-run reproduces the original max. |
| File layout | New `rollback.py` (separate from `watermark.py`); reassess merge later. Utilities in `util.py`. |

## What we built

- **`rollback.py`** — Component 2:
  - Models: `RollbackRequest` (reuses C1's `TableWatermark` rows verbatim as input — the ref, column,
    and cutoff are exactly what a rollback needs), `TableRollback`, `RollbackResult` — all
    `to_dict`/`from_dict`, no AWS handles leak. `RollbackRequest.from_watermark_result()` chains C1→C2.
    `RollbackResult.summary()` = the ticket's removed/updated summary.
  - `rollback_aud(request, mode=..., apply=False, source=..., ...)` mirroring `discover_watermarks`:
    same `record`/`replay`/`auto` cache modes (`cache/rollback_<env>.json`). Per table: skip
    `_raw`/`_stg` and null watermarks (with a `skipped_reason`); **count** `_aud` rows after the
    watermark grouped by `ind`; on `apply`, execute the `DELETE`.
  - `python -m rollback --from-watermark <C1 output.json> [--apply] [--mode] [--env]` CLI; `--apply`
    guarded to `mode='record'`.
- **`util.py`** additions: `aud_table_name()` (idempotent `_aud` suffix), `after_watermark_sql()` (the
  one place the SQL comparison lives), `AwsWatermarkSource.count_changes_since()` /
  `delete_changes_since()`, and `cache_path/read_cache/write_cache` generalized with a `prefix=` arg
  (default `watermark`, so C1 is unchanged; C2 passes `rollback`).
- **Tests**: `tests/test_rollback.py` (17 tests) with a `FakeRollbackSource` (canned `ind` counts,
  records DELETE calls) — no AWS. Plus `tests/fixtures/rollback_dev.json`. Covers model round-trip,
  C1→C2 chaining, `ind` bucketing / name / SQL helpers, dry-run-never-deletes, apply-issues-DELETE,
  apply-requires-record, `_raw`/`_stg` + null-watermark skips, record→replay equality, summary totals,
  and an end-to-end replay chained off the C1 fixture.
- Docs: `README.md` + `CLAUDE.md` updated for two components; this file added.

## Verification done

- `python -m pytest` → **43 passed** (26 C1 + 17 C2), offline, no AWS.
- CLI dry-run replay against `tests/fixtures/` prints a `RollbackResult` with `applied=false` and a
  correct `summary()`.

## Worth flagging for later (untested / risks)

- **No live AWS run this session.** `count_changes_since` / `delete_changes_since` / the DML path have
  only been exercised with a fake source. First live step next session: a `--mode record` **dry-run**
  on one real `_aud` table, eyeball the `ind` breakdown, then a guarded `--apply` on a scratch table.
- **`after_watermark_sql` uses `from_iso8601_timestamp('<watermark>')`.** If a live `_aud` `modifiedon`
  is tz-naive (`timestamp` without zone) it may not compare cleanly against the tz-aware parse — this is
  the single spot to tune. Verify against a real column type early.
- **`ind` value domain assumed `I`/`U`/`D`.** Confirmed `'D'` = delete from the framework SQL; `I`/`U`
  are inferred. If the import layer uses different codes, `bucket_ind` mislabels updated vs inserted
  (totals stay correct — every non-`D` non-`I` folds into `updated`).
- **Null-watermark tables are skipped, not emptied.** A table empty at the C1 checkpoint (`max=None`)
  is left untouched with a reason. If "roll back to empty" is ever wanted, that's a deliberate follow-up
  (would delete all `_aud` rows).
- **DELETE row-count.** `delete_changes_since` doesn't return an affected-row count (Athena DML doesn't
  surface it simply); the summary reports the *pre-delete* count from `count_changes_since`.
- **`--apply` is one-way.** Truncating the `_aud` append-log past the watermark is destructive; there's
  no undo. The dry-run default and the `mode='record'` guard are the only safety rails.

## Deferred (recap)

- **ASP-1616** round-trip checkpoint (`C1 → changes → C2 --apply → C1` ⇒ `output_1a == output_1b`) —
  needs a live apply; this session makes it possible but doesn't run it.
- Whether to merge `rollback.py` into `watermark.py` (per user, reassess now that it exists).
- Common Behave vocabulary handler (ASP-1617/1618) and execution/IO separation (ASP-1619).
