# Session 5 — Component 2 QA + rollback semantics review: the real teardown is a silver rebuild, and it's *not* blocked

**Date:** 2026-07-02
**Ticket:** ASP-1613 → **ASP-1615** (`_aud` rollback) + **ASP-1616** (round-trip checkpoint)
**Branch:** `feat/asp-1615-rollback-aud`
**Env:** live AWS `dev`, profile `cdcv2-dev`, account `484438948628`, Athena workgroup `dev3`
**Outcome:** C1+C2 QA'd green live on `molecular_vms_beakon.contractor`. A documentation review
corrected the CDC model that Sessions 1–4 assumed, which reframed the whole rollback. **Decision: the
rollback is "Reading 2" — restore the silver/base table *from* `_aud`, not truncate `_aud`.** The
critical finding: **silver is unpartitioned, so the SESSION_4 write blocker does NOT apply to the
silver rebuild** — the round-trip is achievable after all. Reconstruction logic validated read-only
(exact match). No writes performed this session. Implementation deferred to Session 6.

---

## Goal

QA Components 1 & 2 live (incl. rollback), then debug the SESSION_4 `--apply` blocker. Target table:
`molecular_vms_beakon.contractor`.

---

## Part A — C1 + C2 QA (live dev, all green)

| Step | Command | Result |
|---|---|---|
| 1. C1 record | `python -m watermark --tables molecular_vms_beakon.contractor --env dev --mode record` | ✅ `max=2026-06-11T05:47:38.312726+00:00` (µs), `row_source=aws` |
| 2. C2 dry-run @ current WM | `python -m rollback --from-watermark cache/watermark_dev.json --mode record` | ✅ **`total:0`** — clean no-op checkpoint (nanos precision fix from S4 holds) |
| 3. C2 dry-run @ earlier WM | edit input to `2026-06-11T00:00:00` → `--mode record` | ✅ **`total:4`** (2 U + 2 I) in the ~6h window; `2026-05-01` cutoff → `total:68`. Live count/predicate/`ind`-bucketing all correct, read-only |

C2's live summary path (`count_changes_since`, `after_watermark_sql`, `ind` bucketing, `_aud`
resolution) is validated end-to-end. `applied:false` throughout.

---

## Part B — debugging `--apply`: read-only investigation

Kept strictly read-only (no DELETE). `SHOW CREATE TABLE` is unsupported on Iceberg via this engine;
used `$partitions` + Glue metadata instead.

### B1 — partition spec confirmed *and extended*
`contractor_aud$partitions` shows the spec is
**`year(modifiedon), month(modifiedon), day(modifiedon), changebatchid`** — **three** time-transforms
of one column (SESSION_4 only saw year+month). This is definitively why Athena's Iceberg writer rejects
any write producing data/delete files (`INVALID_TABLE_PROPERTY: Cannot add redundant partition`). The
block is symmetric: it stops **both** `INSERT` (seed) and row-matching `DELETE` (rollback) on `_aud`.

### B2 — the CDC model, corrected against the framework docs
Reviewed `int-CDCv2-BaseTemplate` + the `uon-integration` masterfile markdowns
(FRAMEWORK-OVERVIEW / GLOSSARY / NAMING-CONVENTIONS). **Sessions 1–4 (and CLAUDE.md/SESSION_3) assumed
"silver is a view over `_aud` (`WHERE ind<>'D'`)". That is wrong.** The real model:

| | Silver `<name>` (`contractor`) | Gold `<name>_aud` (`contractor_aud`) |
|---|---|---|
| Role (docs) | "Cleaned/transformed business view; **where CDC and the `ind` flag apply**" | "**Audit** layer capturing changed data each run" |
| Shape | **Current state**: 1 row per PK, latest `ind` | **Append-log**: every change event, all runs |
| Live rows | 157 (D3 / I118 / U36) | 247 (D3 / I157 / U87) |
| Columns | identical 16-col schema (incl. `ind`, `changebatchid`, `modifiedon`, `modifiedcolumns`) | identical |
| Storage | **independent** Iceberg table (`…/contractor/`), **unpartitioned** | **independent** Iceberg table (`…/contractor_aud/`), partitioned (B1) |

Both are materialized independently by the Glue job each run — neither is a view of the other.
Consumers read silver, or `_current`/`_vw` views filtering `ind <> 'D'` (confirmed in BaseTemplate SQL).
`changebatchid` = per-run batch id.

### B3 — rollback semantics decision (user): **Reading 2**
Two coherent readings of "look at `_aud` to rollback changes made to the gold/base table":
- **Reading 1** (what `rollback.py` implements today): truncate the audit log — `DELETE _aud WHERE
  modifiedon > N`. Only restores the `_aud` history; silver stays dirty.
- **Reading 2** (chosen): **restore the silver/base table** by replaying `_aud` up to N. Matches the
  ASP-1615 comment *"should be possible in 2-3 SQL statements"* and the "base table" wording.

**The ~2–3 SQL statements (Reading 2):**
```sql
-- (1) SUMMARY (read-only, run first)
SELECT ind, COUNT(*) FROM "<db>"."<t>_aud"
WHERE modifiedon > from_iso8601_timestamp_nanos('<N>') GROUP BY ind;

-- (2) RESTORE silver to its state as of N: latest _aud image per PK (ind and all)
DELETE FROM "<db>"."<t>";                 -- or INSERT OVERWRITE / CREATE OR REPLACE
INSERT INTO "<db>"."<t>"
SELECT <cols> FROM (
  SELECT *, ROW_NUMBER() OVER (PARTITION BY <pk> ORDER BY modifiedon DESC, changebatchid DESC) rn
  FROM "<db>"."<t>_aud"
  WHERE modifiedon <= from_iso8601_timestamp_nanos('<N>')
) WHERE rn = 1;

-- (3) TRUNCATE _aud back to N   (OPTIONAL — see B4; this is the blocked one)
DELETE FROM "<db>"."<t>_aud" WHERE modifiedon > from_iso8601_timestamp_nanos('<N>');
```
Statement (2) works because `_aud` stores the **full row image** at every change (not just deltas), and
reproduces soft-deletes faithfully (latest ≤N event carrying `ind='D'` comes back flagged).

### B4 — **CRITICAL (good news): the write blocker only affects `_aud`, not silver**
`contractor$partitions` returns 4 columns with **no `partition` struct → silver is UNPARTITIONED**
(vs `_aud`'s 5 cols with the year/month/day/changebatchid struct). Therefore:

- **SESSION_4's "round-trip fully blocked" was too pessimistic** — S4 only ever tried to write to the
  *partitioned* `_aud`. Statement (2) writes to **unpartitioned silver → no redundant-partition wall.**
- Under Reading 2 with **C1 measuring silver**, statement (2) **alone** restores the base table *and*
  closes the round-trip (`C1b(silver)=N=C1a`). Statement (3) (`_aud` truncate) is **optional** and is
  the only piece still blocked by the partition spec.
- Lake Formation likely permits the silver DML: S4's 0-row `_aud` DELETE succeeded, so the role can
  issue DML on these tables; only the partition *writer* failed. (Silver write perms still **untested** —
  first thing to verify in S6, on a sandbox copy.)

### B5 — reconstruction validated **read-only** (no writes)
Confirmed PK `beakon_record_number` is unique in silver (157/157). Ran statement (2)'s SELECT at the
current watermark and diffed against live silver via `EXCEPT` both ways:
**157 recon rows = 157 silver rows, 0 mismatches either direction.** Silver is *exactly* reconstructable
from `_aud`. Reading 2's core logic is proven before a single byte is written.

---

## Decisions
- Rollback semantics = **Reading 2** (restore silver from `_aud`); implement next session.
- Current `rollback.py` = Reading 1 (only the `_aud` truncate) → must be reworked.
- For the round-trip, **C1 should measure the table C2 restores** — i.e. keep C1 on silver under
  Reading 2 (silver is both what advances on change and what gets restored).

## Deferred to Session 6 (implementation)
1. Rewrite `rollback.py` to Reading 2: statement (2) silver rebuild (per-PK latest `_aud` ≤ N), with
   the summary (1); make the `_aud` truncate (3) an optional, separately-guarded step.
2. Source the real `PrimaryKey` per table (DynamoDB `*-process-configuration-*` was **not reachable**
   under the names/perms tried — `beakon_record_number` validated empirically for contractor only).
3. Verify silver **write** permission (sandbox copy first — DELETE+INSERT on real silver is one-way).
4. Run the live ASP-1616 round-trip via the silver rebuild: C1(N) → seed change >N (needs the Glue
   pipeline or a Spark write; Athena `INSERT` to `_aud` is blocked) → C2 rebuild → C1 = N.
5. Escalate the `_aud` `year+month+day(modifiedon)` redundant-transform spec as its own blocker (only
   matters if statement (3) truncate is in scope): Spark/Iceberg DELETE path, or drop the redundant
   `year()`/`month()` transforms.
6. Correct the stale "silver = `_aud` view" framing in CLAUDE.md and SESSION_3.

## Housekeeping
- `adhoc_tools/s5_*.py` — throwaway read-only probes (spec/table-types/partitions/silver-shape/
  pk-columns/reconstruct-diff). Committed alongside the s4 set; their JSON output is git-ignored.
- `wm_rollback_input.json` — disposable scratch C2 input; added to `.gitignore`.
- No data modified this session — every live query was read-only.

## Deferred (recap)
- ASP-1616 live round-trip — now **plausibly unblocked** via the silver rebuild (Reading 2); to run in
  S6. The `_aud` truncate remains blocked but is optional under Reading 2.
- Merge `rollback.py` into `watermark.py`; Behave vocabulary (ASP-1617/1618); exec/IO split (ASP-1619).
