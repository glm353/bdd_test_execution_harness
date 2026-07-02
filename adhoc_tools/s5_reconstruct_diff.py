"""Session 5 throwaway (READ-ONLY): prove Reading-2 statement (2) is correct WITHOUT writing.

At the current watermark N (= max modifiedon), the reconstruction "latest _aud row per PK where
modifiedon <= N" must equal current silver row-for-row (silver *is* the current state). Diff via
EXCEPT both directions; 0 mismatches => the silver-rebuild logic is validated. No mutation.
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

COLS = ['beakon_record_number', 'first_name', 'last_name', 'company_name', 'skills', 'role',
        'location', 'type', 'mobile_number', 'primary_email_address', 'user_status',
        'compliance_status', 'ind', 'changebatchid', 'modifiedon', 'modifiedcolumns']
cols = ", ".join(f'"{c}"' for c in COLS)

sql = f'''
WITH recon AS (
  SELECT {cols},
         ROW_NUMBER() OVER (PARTITION BY "{PK}"
                            ORDER BY "modifiedon" DESC, "changebatchid" DESC) AS rn
  FROM "{db}"."contractor_aud"
  WHERE "modifiedon" <= (SELECT MAX("modifiedon") FROM "{db}"."contractor_aud")
),
recon1 AS (SELECT {cols} FROM recon WHERE rn = 1)
SELECT
  (SELECT COUNT(*) FROM recon1)                                            AS recon_rows,
  (SELECT COUNT(*) FROM "{db}"."contractor")                              AS silver_rows,
  (SELECT COUNT(*) FROM (SELECT {cols} FROM recon1
                         EXCEPT SELECT {cols} FROM "{db}"."contractor"))  AS in_recon_not_silver,
  (SELECT COUNT(*) FROM (SELECT {cols} FROM "{db}"."contractor"
                         EXCEPT SELECT {cols} FROM recon1))               AS in_silver_not_recon
'''

recon_rows, silver_rows, a, b = src.run_athena(sql, fetch=True)[0]
print(f"reconstruction rows : {recon_rows}")
print(f"silver rows         : {silver_rows}")
print(f"in recon not silver : {a}")
print(f"in silver not recon : {b}")
print("\nRESULT:", "PASS - silver is exactly reconstructable from _aud" if a == "0" and b == "0"
      else "MISMATCH - investigate")
