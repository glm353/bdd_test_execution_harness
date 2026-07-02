"""Session 4 throwaway probe: compare silver vs _aud max(modifiedon) and list top _aud rows.

Confirms the silver-lags-_aud hypothesis and picks an exact earlier boundary W_a for the
bounded ASP-1616 round-trip. NOT part of C1/C2.
"""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import util

cfg = util.load_config("dev")
src = util.AwsWatermarkSource(cfg)
db = "molecular_vms_beakon_dev"

silver_max = src.run_athena('SELECT MAX("modifiedon") FROM "%s"."contractor"' % db, fetch=True)
aud_max = src.run_athena('SELECT MAX("modifiedon") FROM "%s"."contractor_aud"' % db, fetch=True)
print("silver contractor      max(modifiedon):", silver_max[0][0] if silver_max else None)
print("gold   contractor_aud  max(modifiedon):", aud_max[0][0] if aud_max else None)

print("\ntop 8 contractor_aud rows by modifiedon DESC (modifiedon, ind):")
rows = src.run_athena(
    'SELECT "modifiedon", "ind" FROM "%s"."contractor_aud" ORDER BY "modifiedon" DESC LIMIT 8' % db,
    fetch=True,
)
for i, r in enumerate(rows):
    print(f"  [{i}] {r[0]}   ind={r[1]!r}")

# W_a candidate = 6th-newest (index 5) -> exactly 5 rows strictly after it
if len(rows) > 5:
    print("\nW_a (index 5, raw):", rows[5][0])
    print("W_a normalized     :", util.normalize_timestamp(rows[5][0]))
