"""Session 4 throwaway: seed / verify / manual-cleanup for the ASP-1616 round-trip on a REAL _aud table.

Usage:
  python adhoc_tools/s4_seed.py seed     # INSERT 3 marker rows (ind I/U/D) above the current max
  python adhoc_tools/s4_seed.py verify   # show marker-row count + current max(modifiedon)
  python adhoc_tools/s4_seed.py cleanup  # safety net: delete ONLY our marker rows by changebatchid

The rollback itself is done by `python -m rollback --apply` (the real feature), not here. This only
injects the change rows the round-trip removes, all tagged changebatchid='S4-ASP1616-TEST'.
"""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import util

DB, TBL = "molecular_vms_beakon_dev", "contractor_aud"
TAG = "S4-ASP1616-TEST"
FQ = f'"{DB}"."{TBL}"'


def _ts(s: str) -> str:
    return f"CAST('{s}' AS timestamp(6) with time zone)"


def seed(src: util.AwsWatermarkSource) -> None:
    sql = (
        f'INSERT INTO {FQ} '
        f'(beakon_record_number, first_name, last_name, company_name, ind, changebatchid, modifiedon, modifiedcolumns) '
        f"VALUES "
        f"('S4-TEST-0001','s4','test','ASP-1616','I','{TAG}',{_ts('2026-07-02 00:00:01.000001 UTC')},'seed'),"
        f"('S4-TEST-0002','s4','test','ASP-1616','U','{TAG}',{_ts('2026-07-02 00:00:02.000002 UTC')},'seed'),"
        f"('S4-TEST-0003','s4','test','ASP-1616','D','{TAG}',{_ts('2026-07-02 00:00:03.000003 UTC')},'seed')"
    )
    print("[seed] INSERT 3 marker rows (ind I/U/D)...")
    src.run_athena(sql, fetch=False)
    print("[seed] done.")


def verify(src: util.AwsWatermarkSource) -> None:
    n = src.run_athena(f"SELECT COUNT(*) FROM {FQ} WHERE \"changebatchid\" = '{TAG}'", fetch=True)
    mx = src.run_athena(f'SELECT MAX("modifiedon") FROM {FQ}', fetch=True)
    print(f"[verify] marker rows (changebatchid={TAG!r}) = {n[0][0]}")
    print(f"[verify] current MAX(modifiedon)            = {mx[0][0] if mx else None}")


def cleanup(src: util.AwsWatermarkSource) -> None:
    print("[cleanup] deleting ONLY marker rows by changebatchid (safety net)...")
    src.run_athena(f"DELETE FROM {FQ} WHERE \"changebatchid\" = '{TAG}'", fetch=False)
    print("[cleanup] done.")


def main() -> int:
    action = sys.argv[1] if len(sys.argv) > 1 else "verify"
    src = util.AwsWatermarkSource(util.load_config("dev"))
    {"seed": seed, "verify": verify, "cleanup": cleanup}[action](src)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
