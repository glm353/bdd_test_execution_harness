"""Session 5 throwaway (READ-ONLY): silver contractor partition spec — does the base table carry the
same year+month+day(modifiedon) redundant transforms that block Athena writes on _aud?"""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import util

cfg = util.load_config("dev")
src = util.AwsWatermarkSource(cfg)
db = "molecular_vms_beakon_dev"

for tbl in ("contractor", "contractor_aud"):
    rows = src.run_athena(f'SELECT * FROM "{db}"."{tbl}$partitions" LIMIT 3', fetch=True)
    ncols = len(rows[0]) if rows else 0
    print(f"\n{tbl}$partitions: {len(rows)} rows, {ncols} columns")
    for r in rows:
        print("   full row:", r)
