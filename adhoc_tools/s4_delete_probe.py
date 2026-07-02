"""Session 4 throwaway: is Athena DELETE structurally permitted on the DataZone Iceberg _aud table?

Runs SHOW CREATE TABLE (partition spec) and a 0-row DELETE (matches nothing, so it's safe) to see
whether the write path is blocked by the same year()+month() partition limitation that broke INSERT.
"""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import util

src = util.AwsWatermarkSource(util.load_config("dev"))
FQ = '"molecular_vms_beakon_dev"."contractor_aud"'

print("=== SHOW CREATE TABLE ===")
try:
    for r in src.run_athena(f'SHOW CREATE TABLE {FQ}', fetch=True):
        print(r[0])
except Exception as e:
    print(f"ERROR: {type(e).__name__}: {str(e)[:200]}")

print("\n=== 0-row DELETE test (WHERE changebatchid='__none__') ===")
try:
    src.run_athena(f"DELETE FROM {FQ} WHERE \"changebatchid\" = '__none__'", fetch=False)
    print("OK: DELETE executed successfully (0 rows matched).")
except Exception as e:
    print(f"ERROR: {type(e).__name__}: {str(e)[:300]}")
