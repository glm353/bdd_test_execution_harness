"""Session 4 throwaway: contractor_aud column schema (Glue metadata) to build a valid seed INSERT."""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import util

cfg = util.load_config("dev")
src = util.AwsWatermarkSource(cfg)
db, tbl = "molecular_vms_beakon_dev", "contractor_aud"
cols = src.describe_columns(db, tbl)
print(f"{len(cols)} columns in {db}.{tbl}:\n")
for name, gtype in cols:
    print(f"  {name:32} {gtype}")
