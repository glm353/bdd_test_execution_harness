"""Session 6 (ASP-1616 step 2): seed ONE synthetic row into a silver table via Athena.

The round-trip needs a change that moves the C1 watermark forward. Silver is unpartitioned
(SESSION_5), so a plain Athena INSERT works - no Glue pipeline required. The seed copies an existing
row and overrides: the primary key -> an unused synthetic value, ind -> 'I', modifiedon ->
current_timestamp (guaranteed > the baseline max). Because the seed bypasses the pipeline it never
touches _aud, which is why the rollback (step 4) needs --force.

SAFE BY DEFAULT: running the script only prints the preflight + the exact SQL. Add --go to execute
the INSERT. The targeted cleanup DELETE (the abort path) is printed either way.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import util  # noqa: E402

SEED_PK_TEXT = "ZZ_BDD_TEST_ASP1616"  # used when the PK column is a varchar


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="molecular_vms_beakon_dev")
    ap.add_argument("--table", default="contractor")
    ap.add_argument("--pk", default="beakon_record_number")
    ap.add_argument("--go", action="store_true", help="Execute the INSERT (default: print only).")
    args = ap.parse_args()
    db, table, pk = args.db, args.table, args.pk

    src = util.AwsWatermarkSource(util.load_config("dev"))
    fq = f'"{db}"."{table}"'

    # --- preflight (all read-only) -------------------------------------------------------------
    (count, max_wm) = src.run_athena(
        f'SELECT COUNT(*), MAX("modifiedon") FROM {fq}', fetch=True)[0]
    (pk_type, wm_type) = src.run_athena(
        f'SELECT typeof("{pk}"), typeof("modifiedon") FROM {fq} LIMIT 1', fetch=True)[0]
    snapshot = src.latest_snapshot_id(db, table)
    print(f"table               : {fq}")
    print(f"rows / max modifiedon: {count} / {max_wm}")
    print(f"types               : {pk}={pk_type}, modifiedon={wm_type}")
    print(f"snapshot (recovery) : {snapshot}")

    if pk_type.startswith("varchar") or pk_type.startswith("char"):
        seed_pk_sql = f"'{SEED_PK_TEXT}'"
    elif any(pk_type.startswith(t) for t in ("integer", "bigint", "smallint", "tinyint")):
        (max_pk,) = src.run_athena(f'SELECT MAX("{pk}") + 1000000 FROM {fq}', fetch=True)[0]
        seed_pk_sql = str(max_pk)
    else:
        raise SystemExit(f"Unhandled PK type {pk_type!r} - extend the script before seeding.")
    (clash,) = src.run_athena(
        f'SELECT COUNT(*) FROM {fq} WHERE "{pk}" = {seed_pk_sql}', fetch=True)[0]
    if int(clash) != 0:
        raise SystemExit(f"Seed PK value {seed_pk_sql} already exists in {fq} - aborting.")
    print(f"seed PK value       : {seed_pk_sql} (verified unused)")

    # --- build the INSERT: copy one row, override pk / ind / modifiedon -------------------------
    cols = [c for c, _ in src.describe_columns(db, table)]
    overrides = {
        pk: seed_pk_sql,
        "ind": "'I'",
        "modifiedon": f"CAST(current_timestamp AS {wm_type})",
    }
    select_list = ", ".join(overrides.get(c, f'"{c}"') for c in cols)
    col_list = ", ".join(f'"{c}"' for c in cols)
    insert = f"INSERT INTO {fq} ({col_list}) SELECT {select_list} FROM {fq} LIMIT 1"
    cleanup = f'DELETE FROM {fq} WHERE "{pk}" = {seed_pk_sql}'

    print(f"\nINSERT SQL:\n  {insert}")
    print(f"\nabort/cleanup SQL (removes only the seeded row):\n  {cleanup}")

    if not args.go:
        print("\nDRY RUN - nothing executed. Re-run with --go to seed.")
        return 0

    src.run_athena(insert, fetch=False)
    (count2, max2) = src.run_athena(
        f'SELECT COUNT(*), MAX("modifiedon") FROM {fq}', fetch=True)[0]
    rows = src.run_athena(
        f'SELECT "{pk}", "ind", "modifiedon" FROM {fq} WHERE "{pk}" = {seed_pk_sql}', fetch=True)
    print(f"\nSEEDED. rows {count} -> {count2}; max modifiedon {max_wm} -> {max2}")
    print(f"seeded row          : {rows}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
