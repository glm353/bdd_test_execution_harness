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
with `WATERMARK_AWS_PROFILE`; region defaults to `ap-southeast-2`. Queries run on Athena; the
timestamp column is auto-detected from the Glue schema (preferring `modifiedon`) or set per-table.

## Tests

```bash
python -m pytest        # 12 offline tests; no AWS required
```

## Scope

In scope (this part): Component 1 only, plus the JSON cache for local testing.
Out of scope (future): Component 2 (`_aud` rollback), the C1/C2/C1 checkpoint, and the common Behave
vocabulary handler.
