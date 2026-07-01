"""Phase 3 - validate the smoke-set watermarks (ad hoc, siloed live-AWS).

Two independent checks on the rows scan_watermarks.py wrote to cache/watermark_<env>.json:

1. record->replay fidelity: read the cache back through the component's replay path and confirm every
   max_timestamp round-trips unchanged (only row_source flips to 'cache').

2. independent spot-check: for a sample of tables, re-derive the newest timestamp with a DIFFERENT
   query shape - `SELECT <col> ... ORDER BY <col> DESC LIMIT 1` (+ COUNT(*)) - and confirm it equals
   the MAX() the component recorded. A different plan reaching the same value is real corroboration.

Run: python adhoc_tools/validate_watermarks.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import util
import watermark

SPOT_CHECK = [  # tables (with data) to independently corroborate
    "molecular_vms_beakon.contractor",
    "domain_core_curriculum.class",
    "business_finance_techone.ascender_costcollectors_g",
]


def main() -> int:
    cfg = util.load_config()

    # 1) record -> replay fidelity (offline; reads the cache the smoke scan just wrote) -----------
    cached = watermark.WatermarkResult.from_dict(util.read_cache(cfg.env_code))
    specs = [w.table.qualified for w in cached.watermarks]
    req = watermark.WatermarkRequest.from_specs(specs, env_code=cfg.env_code)
    replayed = watermark.discover_watermarks(req, mode="replay")

    rec_map = {w.table.qualified: w for w in cached.watermarks}
    print("=== record -> replay fidelity ===")
    fidelity_ok = True
    for w in replayed.watermarks:
        r = rec_map[w.table.qualified]
        same_ts = w.max_timestamp == r.max_timestamp
        is_cache = w.row_source == "cache"
        ok = same_ts and is_cache
        fidelity_ok &= ok
        print(f"  {'OK ' if ok else 'FAIL':5} {w.table.qualified:<52} ts_match={same_ts} "
              f"row_source={w.row_source}")
    print(f"  -> fidelity {'PASS' if fidelity_ok else 'FAIL'} ({len(replayed.watermarks)} rows)\n")

    # 2) independent spot-check via ORDER BY ... DESC LIMIT 1 -------------------------------------
    source = util.AwsWatermarkSource(cfg)
    print("=== independent spot-check (ORDER BY DESC LIMIT 1 vs recorded MAX) ===")
    spot_ok = True
    for spec in SPOT_CHECK:
        rec = rec_map.get(spec)
        if rec is None:
            print(f"  SKIP {spec} (not in cache)")
            continue
        db = util.schema_with_env(rec.table.database, cfg.env_code)
        col = rec.timestamp_column
        newest = source.run_athena(
            f'SELECT "{col}" FROM "{db}"."{rec.table.table}" '
            f'WHERE "{col}" IS NOT NULL ORDER BY "{col}" DESC LIMIT 1', fetch=True)
        count = source.run_athena(f'SELECT COUNT(*) FROM "{db}"."{rec.table.table}"', fetch=True)
        indep = newest[0][0] if newest else None
        n = count[0][0] if count else "?"
        match = indep == rec.max_timestamp
        spot_ok &= match
        print(f"  {'OK ' if match else 'FAIL':5} {spec:<52} rows={n}")
        print(f"        recorded MAX : {rec.max_timestamp}")
        print(f"        independent  : {indep}")

    print(f"\n  -> spot-check {'PASS' if spot_ok else 'FAIL'}")
    print(f"\nRESULT: fidelity={'PASS' if fidelity_ok else 'FAIL'}  "
          f"spot-check={'PASS' if spot_ok else 'FAIL'}")
    return 0 if (fidelity_ok and spot_ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
