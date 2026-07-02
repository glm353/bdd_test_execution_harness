"""Session 4 throwaway: find the cleanest micro-precision predicate from the normalized ISO string."""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import util

cfg = util.load_config("dev")
src = util.AwsWatermarkSource(cfg)
tbl = '"molecular_vms_beakon_dev"."contractor_aud"'
iso = "2026-06-11T05:47:38.312726+00:00"
sp_off = iso.replace("T", " ")                 # '2026-06-11 05:47:38.312726+00:00'
sp_gap = sp_off[:-6] + " " + sp_off[-6:]        # '... .312726 +00:00'

preds = {
    "G from_iso8601_timestamp_nanos(iso)      ": f"\"modifiedon\" > from_iso8601_timestamp_nanos('{iso}')",
    "E CAST('.. +00:00' ts(6) wtz)            ": f"\"modifiedon\" > CAST('{sp_gap}' AS timestamp(6) with time zone)",
    "F CAST('..+00:00'  ts(6) wtz)            ": f"\"modifiedon\" > CAST('{sp_off}' AS timestamp(6) with time zone)",
}
for label, pred in preds.items():
    try:
        rows = src.run_athena(f"SELECT COUNT(*) FROM {tbl} WHERE {pred}", fetch=True)
        print(f"{label} -> count_after = {rows[0][0]}   pred={pred}")
    except Exception as e:
        print(f"{label} -> ERROR: {type(e).__name__}: {str(e)[:110]}")
