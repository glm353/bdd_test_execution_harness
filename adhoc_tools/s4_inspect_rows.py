"""Session 4 throwaway: dump the top contractor_aud rows in full so the user can identify test data."""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import util

cfg = util.load_config("dev")
src = util.AwsWatermarkSource(cfg)
db, tbl = "molecular_vms_beakon_dev", "contractor_aud"

cols = src.describe_columns(db, tbl)
names = [c[0] for c in cols]
print(f"{len(names)} columns:", names, "\n")

# Prefer identifying columns if present; else dump all.
prefer = [c for c in ("ind", "changebatchid", "modifiedon", "modifiedcolumns", "id",
                      "contractorid", "name", "firstname", "surname", "createdon") if c in names]
sel = prefer if prefer else names
collist = ", ".join(f'"{c}"' for c in sel)
sql = f'SELECT {collist} FROM "{db}"."{tbl}" ORDER BY "modifiedon" DESC LIMIT 8'
rows = src.run_athena(sql, fetch=True)
print("top 8 rows (DESC by modifiedon), columns =", sel, "\n")
for i, r in enumerate(rows):
    print(f"[{i}] " + " | ".join(f"{c}={v!r}" for c, v in zip(sel, r)))
