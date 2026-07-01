# CLAUDE.md — BDD Test Execution Harness (Component 1)

Project conventions for this repo. Keep it lean; permanent facts only.

## What this is

Component 1 of the V2 BDD Test Execution Harness (ASP-1613 / ASP-1614): watermark discovery —
input a set of `database.table` refs, output the max timestamp per table. A standalone tool ("module")
meant to run alongside other cloned V2 repos.

## Architecture (deliberately flat — two core files)

- `watermark.py` — the component: serializable `@dataclass` I/O models + `discover_watermarks()` + CLI.
- `util.py` — all supporting utilities (AWS auth/Athena/Glue, name helpers, JSON cache). Adapted from
  `../v2 Tooling/poc-pythonbdd` (`backends/aws.py`, `aws_auth.py`, `aws_config.py`, `derivation.py`).

Don't split these into a package without a reason — simplicity is a design goal here.

## Conventions

- **Serialization is the contract.** Every I/O type is a stdlib `@dataclass` with `to_dict()`/
  `from_dict()`; timestamps are ISO-8601 strings. No AWS handles/clients leak into the output — it must
  round-trip through JSON so it can chain into Component 2.
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

Three modes: `record` (live AWS → writes `cache/watermark_<env>.json`), `replay` (offline, reads that
JSON), `auto` (replay if cached else record; default). `cache/*.json` is git-ignored; the committed
snapshot for tests lives in `tests/fixtures/`.

## Testing

`python -m pytest` — offline only, no AWS. `pytest.ini` pins `--basetemp` to `%TEMP%` because the
OneDrive-synced tree denies unlinking pytest's `pytest-current` symlink (WinError 5). Add tests for any
new model/mode; keep the suite AWS-free (inject a fake source, as `tests/test_watermark.py` does).

## Out of scope (future sessions)

Component 2 (`_aud` rollback), the C1/C2/C1 round-trip checkpoint, and the common Behave vocabulary
handler. Shared code is kept local to this module for now (no core library extraction yet).
