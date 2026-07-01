"""Ad hoc schema analysis - is 'modifiedon' the right watermark column for ASP-1614?

Crawls the Glue Catalog for every table in the _<env> databases (get_tables already returns full
column metadata, so this is Glue-only - no Athena, no data scanned) and tallies the timestamp-typed
columns, to sanity-check the 'prefer modifiedon' heuristic. NOT part of Component 1.

Env via WATERMARK_ENV (default dev).
Run: python adhoc_tools/analyze_schemas.py
"""
from __future__ import annotations

import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import util

# Table-name layer suffixes (medallion). 'base' = none of these.
_LAYERS = ("_vw", "_raw", "_stg", "_aud", "_s_stg", "_test_data", "_b", "_s", "_g")


def _layer(name: str) -> str:
    for s in _LAYERS:
        if name.endswith(s):
            return s
    return "base"


def _is_ts(gtype: str) -> bool:
    return gtype.split("(")[0].strip().lower() in util._TIMESTAMP_GLUE_TYPES


def main() -> int:
    env = os.environ.get("WATERMARK_ENV", util.DEFAULT_ENV_CODE)
    cfg = util.load_config(env)
    suffix = f"_{env}"
    glue = util.resolve_session(profile=cfg.profile, region=cfg.region).client("glue")

    db_names = []
    for page in glue.get_paginator("get_databases").paginate():
        db_names += [d["Name"] for d in page.get("DatabaseList", [])]
    env_dbs = sorted(n for n in db_names if n.endswith(suffix))

    total = 0
    with_any_ts = 0
    ts_name_freq: Counter[str] = Counter()          # ts-typed column name -> #tables
    modifiedon_types: Counter[str] = Counter()       # type of any column literally named 'modifiedon'
    no_modon_fallbacks: Counter[str] = Counter()     # ts cols on tables lacking a ts-typed modifiedon
    layer_stats: dict[str, list[int]] = {}           # layer -> [tables, tables_with_ts_modifiedon]

    for db in env_dbs:
        for page in glue.get_paginator("get_tables").paginate(DatabaseName=db):
            for t in page.get("TableList", []):
                total += 1
                name = t["Name"]
                layer = _layer(name)
                layer_stats.setdefault(layer, [0, 0])
                layer_stats[layer][0] += 1

                cols = t.get("StorageDescriptor", {}).get("Columns", [])
                cols += t.get("PartitionKeys", [])
                ts_cols = []
                has_ts_modon = False
                for c in cols:
                    cname, ctype = c["Name"], c.get("Type", "")
                    if cname.lower() == "modifiedon":
                        modifiedon_types[ctype.split("(")[0].strip().lower() or "?"] += 1
                    if _is_ts(ctype):
                        ts_cols.append(cname.lower())
                        if cname.lower() == "modifiedon":
                            has_ts_modon = True
                if ts_cols:
                    with_any_ts += 1
                    for cn in set(ts_cols):
                        ts_name_freq[cn] += 1
                if has_ts_modon:
                    layer_stats[layer][1] += 1
                elif ts_cols:  # has timestamp cols but none is a ts-typed 'modifiedon'
                    for cn in set(ts_cols):
                        no_modon_fallbacks[cn] += 1

    modon_ts = ts_name_freq.get("modifiedon", 0)
    print(f"env={env}  databases={len(env_dbs)}  tables={total}")
    print(f"tables with >=1 timestamp-typed column : {with_any_ts} ({with_any_ts/total:.0%})")
    print(f"tables with a timestamp-typed 'modifiedon': {modon_ts} "
          f"({modon_ts/total:.0%} of all, {modon_ts/max(with_any_ts,1):.0%} of ts-having)\n")

    print("=== top timestamp-TYPED column names (by #tables) ===")
    for name, n in ts_name_freq.most_common(25):
        print(f"  {name:<40} {n:>5}  ({n/total:.0%})")

    print("\n=== type of every column literally named 'modifiedon' ===")
    for typ, n in modifiedon_types.most_common():
        print(f"  {typ:<40} {n:>5}")

    print("\n=== fallback ts cols on tables WITHOUT a ts-typed modifiedon (top 20) ===")
    for name, n in no_modon_fallbacks.most_common(20):
        print(f"  {name:<40} {n:>5}")

    print("\n=== modifiedon (ts-typed) coverage by layer ===")
    print(f"  {'layer':<12} {'tables':>8} {'w/ ts modifiedon':>18} {'coverage':>10}")
    for layer in sorted(layer_stats, key=lambda k: -layer_stats[k][0]):
        tbls, modon = layer_stats[layer]
        print(f"  {layer:<12} {tbls:>8} {modon:>18} {modon/tbls:>9.0%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
