# Session 4 — Component 2 first live run: precision bug fixed, apply blocked by table partitioning

**Date:** 2026-07-02
**Ticket:** ASP-1613 → **ASP-1615** (`_aud` rollback) + **ASP-1616** (round-trip checkpoint)
**Branch:** `feat/asp-1615-rollback-aud`
**Env:** live AWS `dev`, profile `cdcv2-dev`, account `484438948628`, Athena workgroup `dev3`
**Outcome:** Component 2's live path exercised for the first time against 3 real `_aud` gold tables.
**One real defect found and fixed** (watermark comparison lost microsecond precision). The live
`--apply` (ASP-1616) is **blocked by an Athena/Iceberg limitation on the real `_aud` tables** — a
critical finding to escalate. No real data was modified (the failed DELETE was atomic). Suite 43/43.

---

## Goal

SESSION_3 built `rollback.py` but never ran its live path. This session: run C2 live against real
`_aud` tables in dev, validate the dry-run summary (ASP-1615), and attempt one bounded `--apply`
round-trip (ASP-1616).

**Target tables:** `molecular_vms_beakon.contractor`, `domain_core_curriculum.class`,
`molecular_vpermit.molecular_user_permit` (all with proven non-null watermarks).

---

## What we did & found

### Phase 0 — auth (the SESSION_2 gotcha, resolved properly)
STS preflight failed with `ExpiredToken`. Root cause confirmed: `~/.aws/credentials` held a **stale
static `[cdcv2-dev]` block** shadowing the auto-refreshing `credential_process` in `~/.aws/config`.
**Fix applied:** removed the static block (backup saved to
`~/.aws/credentials.bak-session4-*`; `default`/`uon-nonprod` preserved). `credential_process` then
self-healed silently (cached Okta token still valid) — STS OK, workgroup `dev3`.

### Phase 1 — C1 watermarks (live record) — OK
`python -m watermark --tables <3 tables> --env dev --mode record` → 3 non-null ISO-8601 watermarks
(`contractor` `2026-06-11T05:47:38.312726+00:00`, `class` `2026-05-26T16:06:50.699391+00:00`,
`molecular_user_permit` `2025-07-07T07:02:39.563137+00:00`).

### Phase 2 — C2 dry-run (ASP-1615) — surfaced a precision defect
First `python -m rollback --from-watermark cache/watermark_dev.json --mode record` reported **non-zero
counts at the current watermark** (contractor 2, class 3, permit 19) — impossible if the watermark is
the true max. A probe (`adhoc_tools/s4_probe.py`, `s4_precision.py`, `s4_precision2.py`) proved why:

> **DEFECT:** `util.after_watermark_sql` used `from_iso8601_timestamp('<wm>')`, which in Trino/Athena
> returns **`timestamp(3)` (millisecond)**. A microsecond watermark `...312726` truncates to `...312`,
> so rows sitting **exactly at** the watermark (`...312726 > ...312`) compare as strictly *after* it and
> get counted (and would be deleted). This directly breaks the ASP-1616 invariant — a rollback would
> over-delete the boundary rows and lower the max.

Verified live on `contractor_aud` (2 boundary rows at the exact max, both `ind='U'`):
`from_iso8601_timestamp` → count 2 (wrong); `from_iso8601_timestamp_nanos` / `CAST(... AS
timestamp(6) with time zone)` / `TIMESTAMP` literal → count 0 (correct).

> **FIX:** `after_watermark_sql` now uses `from_iso8601_timestamp_nanos` (timestamp(9), accepts our
> normalized ISO string verbatim, same `from_iso8601` family as the framework convention).
> `tests/test_rollback.py::test_after_watermark_sql_shape_and_escaping` updated as the regression guard.

After the fix, the dry-run reports **`total: 0` for all 3 tables** at their current watermark — the
correct baseline, and proof that a freshly-recorded C1 watermark is a genuine no-op checkpoint (the
ASP-1616 precondition). The tz comparison itself works: `modifiedon` is `timestamp(6) with time zone`
and the nanos parse compares cleanly (the SESSION_3 tz-naive worry did not materialise).

### Phase 3 — live `--apply` (ASP-1616) — **BLOCKED (critical)**
Plan was a net-zero seed→rollback on a table we control. Every write path into the governed
environment failed:

| Write attempt | Result |
|---|---|
| `INSERT` into real `contractor_aud` | ❌ `INVALID_TABLE_PROPERTY: Cannot add redundant partition: modifiedon_year: year(15) conflicts with modifiedon_month: month(15)` |
| `CREATE TABLE`/CTAS scratch Iceberg (DataZone bucket) | ❌ `Insufficient Lake Formation permission(s)` on the location |
| 0-row `DELETE` on `contractor_aud` | ✅ succeeds (writes no delete files) |
| **row-matching `DELETE`** (the actual rollback, 2 rows) | ❌ **same `redundant partition` error as INSERT** |

> **CRITICAL FINDING:** The CDCv2 `_aud` gold tables are **DataZone-managed Iceberg tables partitioned
> by both `year(modifiedon)` AND `month(modifiedon)`**. Athena's Iceberg writer rejects that redundant
> transform spec on **any write that produces data/delete files** — so a real rollback `DELETE` (which
> writes positional delete files) **cannot execute on these tables via Athena**, even though the SQL and
> the dry-run counts are correct. The 0-row DELETE only "passed" because it wrote nothing.

This blocks ASP-1616's live apply for the whole feature as currently targeted (the partitioning is
uniform across DataZone-managed `_aud` tables, so this is almost certainly systemic, not
contractor-specific — worth a read-only audit of `<t>_aud$partitions` across tables to confirm).

**Guard worked / no data harmed:** the pre-apply dry-run confirmed exactly 2 rows; the DELETE failed
atomically; a post-failure C1 re-run confirmed `contractor_aud` max is **unchanged**
(`2026-06-11T05:47:38.312726+00:00`).

---

## Code changes (committed-worthy)
- `util.after_watermark_sql`: `from_iso8601_timestamp` → `from_iso8601_timestamp_nanos` (+ docstring
  explaining the precision reason). The one-line fix at the documented single tuning point.
- `tests/test_rollback.py`: predicate assertion updated to the nanos form (regression guard). 43/43.

## Housekeeping
- `adhoc_tools/s4_*.py` — throwaway live probes (auth/precision/schema/scratch/seed). git-ignored JSON.
- `wm_rollback_input.json` — throwaway C2 input; treat as disposable.
- **Minor orphan cleanup (needs S3 perms we lack):** 3 failed-CTAS staging prefixes were left under
  `s3://amazon-datazone-484438948628-.../sys/athena/tables/<uuid>/` (76cbc872…, 12f0c48d…, e433eae4…)
  and `.../sys/athena/s4_rollback_scratch/`. Athena won't auto-delete them; harmless but tidy up if
  bucket hygiene matters. No Glue table was created (all CTAS failed).

## Worth flagging / follow-ups
1. **ASP-1616 is blocked on real `_aud` tables** by the year()+month() partition-transform limitation.
   Remediation options for a follow-up ticket: (a) run the DELETE via a Spark/Iceberg path instead of
   Athena; (b) get the `_aud` partition spec changed to drop the redundant `year()` (month implies
   year); (c) newer Athena engine version if it handles multi-transform-of-one-column specs. Confirm
   the spec is uniform across `_aud` tables (read `"<db>"."<t>_aud$partitions"`).
2. **C2 error handling:** a per-table Athena failure currently raises and aborts the whole run. Consider
   classifying errors per table (like the SESSION_2 scan scripts) so one bad table doesn't kill a batch.
3. **Lake Formation write scope:** the role can `SELECT`/read but not create tables or write data into
   the governed locations tried — so any future seed/scratch strategy needs a sandbox db + writable S3.
4. **Silver == `_aud` max** held for `contractor` (both `05:47:38.312726`); no silver-lag observed.

## Deferred (recap)
- ASP-1616 live round-trip — **now demonstrably blocked pending a non-Athena DELETE path or a table
  partition-spec change** (see #1). Dry-run + precision fix stand.
- Merge `rollback.py` into `watermark.py`; Behave vocabulary handler (ASP-1617/1618); exec/IO
  separation (ASP-1619).
