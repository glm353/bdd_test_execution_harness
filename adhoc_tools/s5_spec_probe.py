"""Session 5 throwaway (READ-ONLY): confirm the contractor_aud partition spec and whether the
silver `contractor` table is a view over _aud.

Answers three questions before any --apply debug, issuing ZERO writes:
  1. SHOW CREATE TABLE contractor_aud   -> the Iceberg partition transforms (year()+month() redundancy).
  2. SHOW CREATE TABLE contractor        -> is silver a VIEW (WHERE ind <> 'D') or a materialized table?
  3. contractor_aud$partitions           -> the live partition rows / spec as materialized.
"""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import util

cfg = util.load_config("dev")
src = util.AwsWatermarkSource(cfg)
db = "molecular_vms_beakon_dev"


def show(title: str, sql: str) -> None:
    print("\n" + "=" * 90)
    print(title)
    print("-" * 90)
    try:
        rows = src.run_athena(sql, fetch=True)
    except Exception as exc:  # noqa: BLE001 - probe: classify, don't crash
        print(f"[error] {type(exc).__name__}: {exc}")
        return
    for r in rows:
        print("  " + " | ".join("" if v is None else str(v) for v in r))


show("1) SHOW CREATE TABLE contractor_aud (partition spec)",
     f'SHOW CREATE TABLE "{db}"."contractor_aud"')
show("2) SHOW CREATE TABLE contractor (silver: view or table?)",
     f'SHOW CREATE TABLE "{db}"."contractor"')
show("3) contractor_aud$partitions (materialized partitions, first 20)",
     f'SELECT * FROM "{db}"."contractor_aud$partitions" LIMIT 20')
