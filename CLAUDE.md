# CLAUDE.md — BDD Test Execution Harness (Components 1 & 2)

Project conventions for this repo. Keep it lean; permanent facts only.

## What this is

Part 1 of the V2 BDD Test Execution Harness (ASP-1613). Two chained components:
- **Component 1** (ASP-1614): watermark discovery — input `database.table` refs, output the max
  timestamp per table.
- **Component 2** (ASP-1615): rollback via `_aud` — consume the C1 output and, per table, summarize
  the `_aud` change rows recorded after the watermark and (optionally, `--apply`) restore the silver
  table to its state as of that watermark by replaying the `_aud` log ("Reading 2", SESSION_5).

A standalone tool ("module") meant to run alongside other cloned V2 repos.

## Architecture (deliberately flat — core files)

- `watermark.py` — Component 1: serializable `@dataclass` I/O models + `discover_watermarks()` + CLI.
- `rollback.py` — Component 2: serializable models (`RollbackRequest`/`TableRollback`/`RollbackResult`)
  + `rollback_aud()` + CLI. Consumes a C1 `WatermarkResult` (reuses its `TableWatermark` rows as input).
- `util.py` — all supporting utilities (AWS auth/Athena/Glue, name helpers, JSON cache). Adapted from
  `../v2 Tooling/poc-pythonbdd` (`backends/aws.py`, `aws_auth.py`, `aws_config.py`, `derivation.py`).

Don't split these into a package without a reason — simplicity is a design goal here. (C2 was kept a
separate file from C1; revisit merging only if there's a reason.)

## Component 2 semantics (rollback via `_aud`)

- **CDC model (corrected in SESSION_5):** silver `<name>` and gold `<name>_aud` are **independent
  Iceberg tables**, both materialized by the Glue job each run. Silver = current state (1 row per PK,
  latest `ind`); `_aud` = the audit **append-log** (full row image per change). Key columns on both:
  `ind` (op indicator; `'D'`=delete, else insert/update), `changebatchid`, `modifiedcolumns`,
  `modifiedon` (the watermark column). Silver is **unpartitioned**; `_aud` is partitioned
  `year+month+day(modifiedon), changebatchid` — a spec Athena rejects for **any** write to `_aud`.
- **Rollback = silver rebuild ("Reading 2"):** `DELETE FROM silver` + `INSERT` the reconstruction —
  latest `_aud` image per **primary key** with `modifiedon <= watermark`
  (`util.reconstruction_sql`; validated read-only in SESSION_5, exact match). A C1 re-run on silver
  then reproduces the original max — the ASP-1616 invariant. The PK is supplied explicitly per table
  (`--pk db.table=col1,col2`, composite OK; from the framework's `PrimaryKey` config / Confluence).
  No PK → summarized only, apply refused for that table. The SQL predicates live in
  `util.after_watermark_sql` / `upto_watermark_sql` (the one place to tune the timestamp comparison;
  must stay `from_iso8601_timestamp_nanos` — SESSION_4 precision defect).
- **Dry-run by default** (summary + recon row-count preview); `--apply` (requires `mode='record'`)
  executes the rebuild. DELETE+INSERT is **not atomic** — the pre-rebuild Iceberg snapshot id is
  recorded first (`silver_snapshot_before`) as the time-travel recovery point.
- `--truncate-aud` (requires `--apply`) additionally truncates the `_aud` log past the watermark —
  optional, and currently **blocked** by the `_aud` partition spec; its failure lands in the
  per-table `error` field (per-table failures never abort a batch).
- `_raw`/`_stg` tables and null (empty-table) watermarks are skipped with a `skipped_reason`.
- `ind` bucketing (`bucket_ind`): `'D'`→removed, `'I'`→inserted, else→updated.

## Conventions

- **Serialization is the contract.** Every I/O type is a stdlib `@dataclass` with `to_dict()`/
  `from_dict()`; timestamps are ISO-8601 strings. No AWS handles/clients leak into the output — it must
  round-trip through JSON so C1's output chains into C2's input.
- **stdlib only for models** — no Pydantic. `boto3` is the only runtime dep (lazy-imported so the
  offline `replay` path works with no SDK/creds).
- **Env-overridable config** — see `util.load_config`; region defaults to `ap-southeast-2`.
- **AWS auth** — Okta SSO via a `credential_process` profile (`util.resolve_session` does an STS
  preflight). Profile via `WATERMARK_AWS_PROFILE`.
- **Watermark column** — defaults to `modifiedon` (the CDCv2 CDC/audit column; present on 100% of the
  `_aud` gold tables this targets). Override per-table via `TableRef.timestamp_column`, or pass `None`
  to auto-detect a timestamp-typed column from the Glue schema (still preferring `modifiedon`).
- **Naming** — logical schemas are env-suffixed at query time (`schema_with_env`, e.g. `_dev`).

## Caching / local testing

Three modes: `record` (live AWS → writes `cache/<prefix>_<env>.json`), `replay` (offline, reads that
JSON), `auto` (replay if cached else record; default). Cache prefix is `watermark` (C1) or `rollback`
(C2), selected via `util.cache_path(..., prefix=...)`. `cache/*.json` is git-ignored; committed
snapshots for tests live in `tests/fixtures/`.

## Testing

`python -m pytest` — offline only, no AWS. `pytest.ini` pins `--basetemp` to `%TEMP%` because the
OneDrive-synced tree denies unlinking pytest's `pytest-current` symlink (WinError 5). Add tests for any
new model/mode; keep the suite AWS-free (inject a fake source, as `tests/test_watermark.py` does).

## Out of scope (future sessions)

The C1/C2/C1 round-trip checkpoint (ASP-1616) is **done** — proven live on dev in SESSION_6 (silver
is writable via Athena; `output_1a == output_1b`). The common Behave vocabulary handler (ASP-1617) is
**in progress**: the vocabulary spec is `VOCABULARY.md` (SESSION_7, awaiting review); the handler
(`vocab.py`) is built here after sign-off. ASP-1618 (refactor poc-pythonbdd to consume it) is
**Cancelled** — that repo stays a read-only reference. Still out of scope: the execution/IO
separation (ASP-1619). Shared code is kept local to this module for now (no core library extraction
yet).
