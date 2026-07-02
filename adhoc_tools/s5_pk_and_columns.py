"""Session 5 throwaway (READ-ONLY): confirm the primary key + column overlap for the silver rebuild.

  1. Is beakon_record_number unique in silver (count == distinct)?  -> validates it as the PK.
  2. Try to read the PrimaryKey from the DynamoDB process-configuration table (best effort).
  3. Common columns between silver `contractor` and gold `contractor_aud` (what statement (2) selects).
"""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import util

cfg = util.load_config("dev")
src = util.AwsWatermarkSource(cfg)
db = "molecular_vms_beakon_dev"
PK = "beakon_record_number"

# 1. PK uniqueness in silver
rows = src.run_athena(
    f'SELECT COUNT(*), COUNT(DISTINCT "{PK}") FROM "{db}"."contractor"', fetch=True)
total, distinct = rows[0]
print(f"silver contractor: count={total} distinct({PK})={distinct} -> "
      f"{'PK OK (unique)' if total == distinct else 'NOT unique!'}")

# 2. common columns
scols = [c[0] for c in src.describe_columns(db, "contractor")]
acols = [c[0] for c in src.describe_columns(db, "contractor_aud")]
common = [c for c in scols if c in acols]
print(f"\nsilver-only columns : {[c for c in scols if c not in acols]}")
print(f"aud-only columns    : {[c for c in acols if c not in scols]}")
print(f"common columns ({len(common)}): {common}")

# 3. best-effort DynamoDB config lookup for the declared PrimaryKey
try:
    ddb = src.session.client("dynamodb")
    for tname in (f"uon-integration-process-configuration-dev",
                  f"uon-integration-process-configuration"):
        try:
            resp = ddb.scan(TableName=tname,
                            FilterExpression="contains(CatalogDatabase, :db)",
                            ExpressionAttributeValues={":db": {"S": "molecular_vms_beakon"}},
                            Limit=50)
            items = resp.get("Items", [])
            print(f"\nDynamoDB {tname}: {len(items)} matching items")
            for it in items:
                pid = it.get("ProcessId", {}).get("S", "?")
                pk = it.get("PrimaryKey", {}).get("S", "?")
                st = it.get("SilverTable", {}).get("S", "?")
                gt = it.get("GoldTable", {}).get("S", "?")
                print(f"   ProcessId={pid} SilverTable={st} GoldTable={gt} PrimaryKey={pk!r}")
            break
        except Exception as exc:  # noqa: BLE001
            print(f"\nDynamoDB {tname}: {type(exc).__name__}: {str(exc)[:120]}")
except Exception as exc:  # noqa: BLE001
    print(f"\nDynamoDB client error: {type(exc).__name__}: {exc}")
