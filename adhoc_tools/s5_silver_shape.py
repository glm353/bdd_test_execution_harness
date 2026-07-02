"""Session 5 throwaway (READ-ONLY): does SILVER contractor carry ind/changebatchid (current-state
model), and how does its ind breakdown compare to the gold _aud append-log?"""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import util

cfg = util.load_config("dev")
src = util.AwsWatermarkSource(cfg)
db = "molecular_vms_beakon_dev"

cols = src.describe_columns(db, "contractor")
names = [c[0] for c in cols]
print("silver contractor has ind? ", "ind" in names,
      "| changebatchid?", "changebatchid" in names,
      "| modifiedcolumns?", "modifiedcolumns" in names)

for tbl in ("contractor", "contractor_aud"):
    has_ind = "ind" in [c[0] for c in src.describe_columns(db, tbl)]
    if not has_ind:
        print(f"{tbl}: no ind column")
        continue
    rows = src.run_athena(f'SELECT "ind", COUNT(*) FROM "{db}"."{tbl}" GROUP BY "ind" ORDER BY 1',
                          fetch=True)
    print(f"{tbl} ind breakdown:", {ind: int(n) for ind, n in rows})
