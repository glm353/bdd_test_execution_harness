"""Session 5 throwaway (READ-ONLY): compare MAX(modifiedon) on silver vs _aud, and _aud row count."""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import util

cfg = util.load_config("dev")
src = util.AwsWatermarkSource(cfg)
db = "molecular_vms_beakon_dev"

for tbl in ("contractor", "contractor_aud"):
    rows = src.run_athena(f'SELECT MAX("modifiedon"), COUNT(*) FROM "{db}"."{tbl}"', fetch=True)
    mx, cnt = rows[0]
    print(f"{tbl:16} max(modifiedon)={mx!r:40} count={cnt}")

# how many _aud rows carry each ind (whole table) — shows the append-log shape
rows = src.run_athena(
    f'SELECT "ind", COUNT(*) FROM "{db}"."contractor_aud" GROUP BY "ind" ORDER BY 1', fetch=True)
print("\ncontractor_aud ind breakdown (whole table):")
for ind, n in rows:
    print(f"  ind={ind!r:6} {n}")
