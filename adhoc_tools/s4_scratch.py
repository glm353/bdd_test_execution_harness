"""Session 4 throwaway: manage a disposable Iceberg scratch _aud table seeded from real contractor_aud.

  create   CTAS an Iceberg table (10 newest rows copied from contractor_aud; no partitioning)
  inspect  list its rows (modifiedon, ind) DESC so we can pick a rollback boundary W0
  drop     DROP TABLE (cleanup) + confirm it's gone

The rollback itself is run via `python -m rollback --apply` (the real feature); this only sets up /
tears down the data it operates on. Source contractor_aud is read only (SELECT).
"""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import util

DB = "molecular_vms_beakon_dev"
SCRATCH = "s4_rollback_scratch_aud"
SRC = "contractor_aud"
LOC = ("s3://amazon-datazone-484438948628-ap-southeast-2-776011691312/"
       "dzd-5kb3268ewfy61u/datazone/55hvs12a0xx8o2/sys/athena/s4_rollback_scratch/")
FQ = f'"{DB}"."{SCRATCH}"'


def create(src: util.AwsWatermarkSource) -> None:
    # Managed Iceberg needs BOTH is_external=false AND an explicit location. No partitioning ->
    # no year()+month() INSERT/CTAS conflict (the limitation that blocks the real _aud tables).
    sql = (
        f'CREATE TABLE {FQ} '
        f"WITH (table_type='ICEBERG', is_external=false, location='{LOC}', format='PARQUET') AS "
        f'SELECT beakon_record_number, ind, changebatchid, modifiedon, modifiedcolumns '
        f'FROM "{DB}"."{SRC}" ORDER BY modifiedon DESC LIMIT 10'
    )
    print(f"[create] CTAS {FQ} from {SRC} (10 newest rows)...")
    src.run_athena(sql, fetch=False)
    print("[create] done.")


def inspect(src: util.AwsWatermarkSource) -> None:
    n = src.run_athena(f'SELECT COUNT(*) FROM {FQ}', fetch=True)
    print(f"[inspect] row count = {n[0][0]}")
    rows = src.run_athena(f'SELECT modifiedon, ind, changebatchid FROM {FQ} ORDER BY modifiedon DESC', fetch=True)
    for i, r in enumerate(rows):
        print(f"  [{i}] modifiedon={r[0]}  ind={r[1]!r}  changebatchid={r[2]!r}")


def drop(src: util.AwsWatermarkSource) -> None:
    print(f"[drop] DROP TABLE {FQ} ...")
    src.run_athena(f'DROP TABLE {FQ}', fetch=False)
    # confirm gone
    try:
        src.describe_columns(DB, SCRATCH)
        print("[drop] WARNING: table still present in Glue.")
    except Exception as e:
        print(f"[drop] confirmed gone ({type(e).__name__}).")


def main() -> int:
    action = sys.argv[1] if len(sys.argv) > 1 else "inspect"
    src = util.AwsWatermarkSource(util.load_config("dev"))
    {"create": create, "inspect": inspect, "drop": drop}[action](src)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
