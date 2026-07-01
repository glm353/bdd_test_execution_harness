# BDD Test Execution Harness — Component 1: Watermark Discovery

Part 1 of the V2 BDD Test Execution Harness (Jira **ASP-1613**, sub-task **ASP-1614**).

**Component 1** takes a set of `database.table` references and returns the **max timestamp per
table** (not per row). The result is a clean, serializable object that later components can chain
onto — Component 2 (the `_aud`-based rollback) will consume this output to restore the gold/base
table to a known watermark.

This is a standalone tool ("module") that runs side-by-side with other cloned V2 repos.

## Why a watermark?

A watermark is a "known good" snapshot marker: the newest change timestamp a table had at a point in
time. The planned test round-trip is:

```
C1 (output_1a)  →  make some changes  →  C2 (rollback via _aud)  →  C1 (output_1b)
expect: output_1a == output_1b
```

Component 1 is the measurement at both ends of that loop.

## Layout

```
watermark.py   THE COMPONENT — serializable dataclasses + discover_watermarks() + CLI
util.py        all supporting utilities: AWS auth (Okta SSO), Athena/Glue, naming, JSON cache
cache/         recorded AWS snapshots (git-ignored; used for offline local testing)
tests/         offline pytest suite + a committed fixture cache
adhoc_tools/   throwaway scripts for siloed live-AWS testing & validation (NOT part of the component)
```

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows (PowerShell: .venv\Scripts\Activate.ps1)
pip install -r requirements.txt
```

## Run modes (local testing via JSON cache)

The component can hit live AWS or replay a cached snapshot, so Component 1 is testable **locally**
without AWS/Okta each time:

| `--mode`  | Behaviour |
|-----------|-----------|
| `record`  | Query live AWS (Athena `MAX()` + Glue schema), then write `cache/watermark_<env>.json`. |
| `replay`  | Read that cached JSON only — **no AWS/boto3/Okta needed** (offline). |
| `auto`    | Replay if a cache file exists, else record. **Default.** |

Because the output is identical regardless of mode (only `row_source` differs: `aws` vs `cache`),
a replayed result is directly comparable to a live one.

## Usage

```bash
# Offline replay against a cached snapshot (no AWS):
python -m watermark --tables molecular_vms_beakon.contractor domain_core_curriculum.holiday --mode replay

# Live capture (needs an Okta-SSO AWS profile; writes the cache):
python -m watermark --tables molecular_vms_beakon.contractor --mode record --okta-login
```

As a library:

```python
from watermark import WatermarkRequest, discover_watermarks

req = WatermarkRequest.from_specs(["domain_core_curriculum.holiday"], env_code="dev")
result = discover_watermarks(req, mode="auto")
print(result.by_table())   # {"domain_core_curriculum.holiday": "2026-06-29T22:00:00"}
```

## AWS connectivity

Auth follows the V2 convention (see `util.resolve_session`): a `~/.aws/config` profile whose
`credential_process` runs `okta-aws-cli web` so boto3 refreshes transparently. Override the profile
with `WATERMARK_AWS_PROFILE`; region defaults to `ap-southeast-2`. Queries run on Athena.

### Watermark column

The watermark column **defaults to `modifiedon`** — the CDCv2 CDC/audit column. This component targets
the **`_aud` (gold/audit) tables** the CDCv2 framework writes each run, and a schema crawl of all dev
tables confirmed `modifiedon` is present on **100% of `_aud`, silver, and gold tables** (it's only
sparse on raw/staging layers, which aren't watermark targets). So `TableRef.timestamp_column` is
`"modifiedon"` unless you override it:

- **Per-table override** — pass `timestamp_column="<col>"` (e.g. `TableRef.from_string("db.t",
  timestamp_column="date_updated")`) to pin a different column.
- **Auto-detect** — pass `timestamp_column=None` to instead pick a timestamp-typed column from the
  Glue schema (still preferring `modifiedon`); a `[warn]` is emitted if a non-`modifiedon` column is
  chosen, since a silent wrong pick would poison the watermark.

## Tests

```bash
python -m pytest        # 12 offline tests; no AWS required
```

## `adhoc_tools/` — siloed testing & validation

Scripts in `adhoc_tools/` are **not part of Component 1**. They exist purely to exercise and validate
the component against **live AWS in isolation** ("siloed" testing) — separate from the offline pytest
suite, which never touches AWS. They are deliberately kept out of `watermark.py`/`util.py` so the
component stays lean (see CLAUDE.md's two-core-files rule); treat them as throwaway harness code, not a
supported API.

They cover the parts the offline tests can't reach — the real Glue + Athena + Okta path:

- **Enumeration** — crawl the Glue Catalog to list every `database.table` in the `_dev` databases
  (cheap; no Athena), producing the input set to run the component against.
- **Batch scan & validation** — run `discover_watermarks` (mode `record`) over that set one table at a
  time (so one failure doesn't abort the batch), and emit a status/coverage matrix + an Athena
  cost tally for spot-checking results against manual queries.

Because they hit live AWS, they need valid Okta-SSO credentials (`WATERMARK_AWS_PROFILE`) and are run
manually, on demand — never as part of `pytest`.

## Scope

In scope (this part): Component 1 only, plus the JSON cache for local testing.
Out of scope (future): Component 2 (`_aud` rollback), the C1/C2/C1 checkpoint, and the common Behave
vocabulary handler.
