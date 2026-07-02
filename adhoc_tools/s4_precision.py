"""Session 4 throwaway: test predicate forms for the after-watermark comparison precision.

The watermark 2026-06-11T05:47:38.312726+00:00 equals the max modifiedon of contractor_aud
(2 rows at that exact microsecond). A correct '> watermark' predicate must count 0 of them.
"""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import util

cfg = util.load_config("dev")
src = util.AwsWatermarkSource(cfg)
tbl = '"molecular_vms_beakon_dev"."contractor_aud"'
iso = "2026-06-11T05:47:38.312726+00:00"      # normalized (T, +00:00)
spc = "2026-06-11 05:47:38.312726 UTC"          # athena render (space, UTC)

preds = {
    "A from_iso8601_timestamp(iso)      ": f"\"modifiedon\" > from_iso8601_timestamp('{iso}')",
    "B CAST(iso AS ts(6) w/ tz)         ": f"\"modifiedon\" > CAST('{iso}' AS timestamp(6) with time zone)",
    "C CAST(space AS ts(6) w/ tz)       ": f"\"modifiedon\" > CAST('{spc}' AS timestamp(6) with time zone)",
    "D TIMESTAMP 'space' literal        ": f"\"modifiedon\" > TIMESTAMP '{spc}'",
}
for label, pred in preds.items():
    try:
        rows = src.run_athena(f"SELECT COUNT(*) FROM {tbl} WHERE {pred}", fetch=True)
        print(f"{label} -> count_after = {rows[0][0]}")
    except Exception as e:
        print(f"{label} -> ERROR: {type(e).__name__}: {str(e)[:140]}")
