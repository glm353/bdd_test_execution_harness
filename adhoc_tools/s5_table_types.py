"""Session 5 throwaway (READ-ONLY): Glue TableType + Iceberg partition spec for silver vs _aud.

Is silver `contractor` a VIRTUAL_VIEW over _aud (round-trip holds) or an independent table?
And what exactly is contractor_aud partitioned by?
"""
from __future__ import annotations
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import util

cfg = util.load_config("dev")
src = util.AwsWatermarkSource(cfg)
glue = src._client("glue")
db = "molecular_vms_beakon_dev"

for tbl in ("contractor", "contractor_aud"):
    meta = glue.get_table(DatabaseName=db, Name=tbl)["Table"]
    print("\n" + "=" * 80)
    print(f"{db}.{tbl}")
    print("-" * 80)
    print("TableType        :", meta.get("TableType"))
    params = meta.get("Parameters", {})
    print("table_type param :", params.get("table_type"))
    print("PartitionKeys    :", [(k["Name"], k.get("Type")) for k in meta.get("PartitionKeys", [])])
    # Iceberg partition spec lives in Parameters for Glue-registered Iceberg tables.
    for key in ("metadata_location", "partition_spec", "spec"):
        if key in params:
            print(f"{key:17}:", params[key])
    vot = meta.get("ViewOriginalText")
    if vot:
        print("ViewOriginalText :", vot[:500])
