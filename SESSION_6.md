# Session 6 — Component 2 rewritten to Reading 2 (silver rebuild from `_aud`)

**Date:** 2026-07-02
**Ticket:** ASP-1613 → **ASP-1615** (rollback via `_aud`) + **ASP-1616** (round-trip — **PROVEN LIVE**)
**Branch:** `feat/asp-1615-rollback-aud`
**Env:** live AWS `dev`, profile `cdcv2-dev`, account `484438948628`, Athena workgroup `dev3`
**Outcome:** `rollback.py` rewritten from Reading 1 (`_aud` truncate) to **Reading 2** (restore the
silver table by replaying `_aud` up to the watermark), per the SESSION_5 decision. Suite 43 → **59**
offline tests, all green. **ASP-1616 round-trip executed live and PASSED** on
`molecular_vms_beakon.contractor` (user-driven; `output_1a == output_1b`). Two milestone live
findings: **silver IS writable via Athena** (Lake Formation permits it — the last unproven S5
assumption) and the full C1→seed→C2→C1 checkpoint closes exactly.

---

## Design decisions (user-confirmed this session)

- **PK sourcing: explicit per-table** — new `--pk db.table=col1,col2` CLI flag /
  `RollbackRequest.primary_keys`; composite keys supported (framework `PrimaryKey` can be
  `"emplid,name_type,effdt"`). PKs come from the process config / Confluence docs. No DynamoDB
  dependency (unreachable in S5).
- **Reading 2 without a PK is impossible in general** — the rebuild is "latest `_aud` image *per
  entity* ≤ N" and entity identity *is* the PK; every alternative still needs to match "same entity"
  across `_aud` rows. What needs no PK: the dry-run **summary** (counts). So: tables without a PK are
  summarized normally; `--apply` is refused per-table (`error` field), never fatally.

## What changed

- **`util.py`**: `upto_watermark_sql` (`<=` companion, same `from_iso8601_timestamp_nanos` precision),
  `reconstruction_sql` (port of the S5-validated recon SELECT), `silver_table_name`, `parse_pk_spec`;
  `AwsWatermarkSource.count_rows` / `reconstruction_count` / `latest_snapshot_id` (from `$history`,
  best-effort) / `rebuild_silver` (DELETE + INSERT). `delete_changes_since` kept for the optional
  truncate.
- **`rollback.py`**: module docstring = the Reading-2 spec. `RollbackRequest.primary_keys`;
  `TableRollback` gains `silver_table`, `primary_key`, `recon_rows`, `silver_rows_before/after`,
  `silver_snapshot_before` (Iceberg time-travel recovery point — DELETE+INSERT is not atomic),
  `aud_truncated`, `error` (all defaulted → pre-rewrite caches still parse). Per-table flow:
  summary (1) → recon preview when PK known → on `--apply`: PK/schema pre-checks (PK must exist in
  both silver & `_aud`; select-list = silver ∩ aud in silver order), capture count + snapshot,
  rebuild (2), post-verify count == recon. `--truncate-aud` (requires `--apply`) = optional stage (3),
  known blocked by the `_aud` partition spec; its failure and any per-table exception land in
  `error` — one bad table no longer aborts a batch (S4 follow-up done). `summary()` now also reports
  `applied`/`errors`. **`--force`** (requires `--apply`): rebuild even when the `_aud` log shows 0
  post-watermark changes — needed when silver was changed *without* going through the pipeline (the
  change never reached `_aud`), which is exactly the ASP-1616 seed scenario.
- **`adhoc_tools/s6_seed_silver.py`**: the ASP-1616 seed (step 2). Copies one silver row overriding
  PK (type-aware synthetic value, verified unused) / `ind='I'` / `modifiedon=current_timestamp`.
  Prints preflight + SQL only by default; `--go` executes. Also prints the targeted cleanup DELETE
  (the abort path) and the pre-seed snapshot id.
- **Tests**: `tests/test_rollback.py` rewritten around a richer `FakeRollbackSource` (schemas, counts,
  snapshot, rebuild/truncate recording, injectable failures); fixture regenerated in the new shape;
  backward-compat test for the old cache shape. 57 passed offline.
- **Docs**: CLAUDE.md Component 2 section rewritten to Reading 2 (S5 deferred item 6); SESSION_3 got a
  correction banner (silver-as-view framing + Reading-1 semantics superseded).

## Live read-only QA (all green, no mutation)

| Step | Command | Result |
|---|---|---|
| 1. C1 record | `python -m watermark --tables molecular_vms_beakon.contractor --env dev --mode record` | ✅ max unchanged: `2026-06-11T05:47:38.312726+00:00` |
| 2. C2 dry-run @ current WM | `python -m rollback --from-watermark cache/watermark_dev.json --mode record --pk molecular_vms_beakon.contractor=beakon_record_number` | ✅ `total:0`, **`recon_rows:157`** (== live silver count), `applied:false` |
| 3. C2 dry-run @ `2026-06-11T00:00:00` | same, via scratch `wm_rollback_input.json` | ✅ `total:4` (2 U + 2 I), **`recon_rows:155`** = 157 − 2 post-cutoff inserts — internally consistent |

The recon-count preview (new) directly cross-checks the S5 finding: reconstruction at the current
watermark reproduces silver's exact row count.

## ASP-1616 live round-trip — RUNBOOK (user-driven; approved to be run by the user)

All-Athena (silver is unpartitioned → writable). Run from the repo root with the venv; first, once
per shell: `$env:WATERMARK_AWS_PROFILE = 'cdcv2-dev'`. Every command below is
`.venv\Scripts\python.exe ...`. Steps 2b and 4 write to dev silver `contractor`; everything else is
read-only.

| # | Command | Behind the scenes | Expected output |
|---|---|---|---|
| 1 | `python -m watermark --tables molecular_vms_beakon.contractor --env dev --mode record` then `Copy-Item cache\watermark_dev.json wm_baseline.json` | Athena `SELECT MAX(modifiedon)` on **silver**; result cached (the copy protects the baseline because step 3 overwrites the cache) | `max_timestamp` = baseline **N** (`2026-06-11T05:47:38.312726+00:00` as of this session) |
| 2a | `python adhoc_tools\s6_seed_silver.py` | Read-only preflight: row count, max, `typeof(pk/modifiedon)`, snapshot id, seed-PK clash check; prints the INSERT + cleanup SQL, executes **nothing** | `rows/max = 157 / N`, seed PK `'ZZ_BDD_TEST_ASP1616'` (or MAX+1000000 if numeric) verified unused, `DRY RUN - nothing executed` |
| 2b | `python adhoc_tools\s6_seed_silver.py --go` | The **first-ever silver write** (perms unproven; an INSERT failure is atomic — silver untouched). Copies 1 row overriding PK/`ind='I'`/`modifiedon=current_timestamp`; `_aud` is NOT touched | `SEEDED. rows 157 -> 158; max modifiedon N -> N′` (N′ ≈ now). If Lake Formation denies: stop here, nothing to clean up |
| 3 | `python -m watermark --tables molecular_vms_beakon.contractor --env dev --mode record` | Same MAX query | `max_timestamp` = **N′ > N** — C1 detects the change |
| 4 | `python -m rollback --from-watermark wm_baseline.json --mode record --apply --force --pk molecular_vms_beakon.contractor=beakon_record_number` | Summary first (`total: 0` — the seed bypassed `_aud`, hence `--force`), recon preview, then: capture count+snapshot → `DELETE FROM contractor` → `INSERT` the 157-row reconstruction ≤ N (the seeded row has no `_aud` image ≤ N, so it does not come back) → post-count verify | `total: 0`, `recon_rows: 157`, `silver_rows_before: 158`, `silver_rows_after: 157`, `silver_snapshot_before: <id>`, **`applied: true`**, `error: null` |
| 5 | `python -m watermark --tables molecular_vms_beakon.contractor --env dev --mode record` then the compare one-liner below | Same MAX query; then diff baseline vs fresh | `max_timestamp` == **N** exactly → `output_1a == output_1b` — **ASP-1616 invariant proven** |

Compare one-liner (step 5):
`python -c "import json; a=json.load(open('wm_baseline.json'))['watermarks'][0]['max_timestamp']; b=json.load(open('cache/watermark_dev.json'))['watermarks'][0]['max_timestamp']; print('ROUND-TRIP', 'PASS' if a==b else 'FAIL', '|', a, '|', b)"`

**Abort / recovery paths**
- After 2b, to back out *without* exercising the rollback: run the cleanup DELETE the seed script
  printed (`DELETE ... WHERE "beakon_record_number" = 'ZZ_BDD_TEST_ASP1616'`) — targeted, silver is
  unpartitioned so it should write fine.
- If step 4 fails **between** its DELETE and INSERT (silver left empty): recover with
  `INSERT INTO "molecular_vms_beakon_dev"."contractor" SELECT * FROM "molecular_vms_beakon_dev"."contractor" FOR VERSION AS OF <silver_snapshot_before>`
  (the snapshot id is in the step-4 output and the step-2a preflight).
- `_aud` is never written at any step — no partition-spec wall anywhere in this plan.

### Live execution — 2026-07-02 (user-run, PASSED)

Actual values from the live run on `molecular_vms_beakon_dev.contractor`:

| # | Actual result |
|---|---|
| 1 | baseline **N = `2026-06-11T05:47:38.312726+00:00`**, 157 rows; copied to `wm_baseline.json` |
| 2a | preflight OK — `pk=varchar` → seed `'ZZ_BDD_TEST_ASP1616'` unused; `modifiedon=timestamp(6) with time zone`; snapshot `4112052266039082386`; dry-run only |
| 2b | ✅ **silver write succeeded** — `rows 157 → 158`; **N′ = `2026-07-02 04:05:09.949000 UTC`**; seeded row `('ZZ_BDD_TEST_ASP1616','I','…04:05:09.949000 UTC')`. *Lake Formation permits Athena DML on silver — the S5 open question, answered.* |
| 3 | C1 → `max_timestamp = 2026-07-02T04:05:09.949000+00:00` (N′ > N), `row_source=aws` |
| 4 | ✅ `applied:true`, `error:null` — `total:0` (seed bypassed `_aud`, `--force` used), `recon_rows:157`, `silver_rows_before:158 → silver_rows_after:157`, `silver_snapshot_before:1370117840811984939` |
| 5 | C1 → `max_timestamp = 2026-06-11T05:47:38.312726+00:00` == baseline N → **ROUND-TRIP PASS** (`output_1a == output_1b`) |

**ASP-1616 invariant proven end-to-end against live dev.** Note: `current_timestamp` seeded at
millisecond precision (`.949000`); the baseline (µs, `.312726`) round-tripped exactly — the S4
nanos-precision predicate holds. `wm_baseline.json` and the scratch inputs are git-ignored/disposable.

## Housekeeping

- `wm_rollback_input.json` — scratch C2 input regenerated (git-ignored).
- Nothing committed this session yet; working tree holds the rewrite.

## Deferred (recap)

- ASP-1616 — **DONE** (proven live, above). Optional stronger variant for a future run: also UPDATE
  an existing silver row in the seed step to exercise the restore-prior-image path (this run only
  exercised drop-a-post-N-insert; the UPDATE path is covered by the S5 read-only recon diff and the
  offline tests, but not yet live).
- `_aud` truncate stays blocked (partition spec) — escalate separately if stage (3) ever becomes
  required; Spark DELETE or a spec change (drop redundant `year()`/`month()`) are the options.
- PKs for other tables (Confluence lookup) as rollout widens; merge `rollback.py` into
  `watermark.py`; Behave vocabulary (ASP-1617/1618); exec/IO split (ASP-1619).
