"""Scale step - scan ONE representative table per _<env> database (ad hoc, siloed live-AWS).

Reads the enumeration written by enumerate_tables.py, picks a single representative table per
database (preferring gold `_g`, else a base table, avoiding views/raw/staging/audit layers), and runs
the resilient recorder from scan_watermarks.py over that set. Broad coverage of every domain without a
full 4,000-query sweep. NOT part of Component 1.

Env via WATERMARK_ENV (dev|test|uat, default dev). Requires adhoc_tools/enumerated_<env>_tables.json.

Run: WATERMARK_ENV=uat python adhoc_tools/scan_one_per_db.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import util
from scan_watermarks import run_scan

HERE = Path(__file__).resolve().parent

# Layer suffixes we'd rather NOT pick as the representative (views/raw/staging/audit/bronze/silver).
_LAYER_SUFFIXES = ("_vw", "_raw", "_stg", "_aud", "_s_stg", "_test_data", "_b", "_s", "_g")


def _is_base(name: str) -> bool:
    return not any(name.endswith(s) for s in _LAYER_SUFFIXES)


def pick_representative(tables: list[str]) -> str | None:
    """Prefer a gold (`_g`) table, else a base (no layer suffix), else the first alphabetically."""
    if not tables:
        return None
    gold = sorted(t for t in tables if t.endswith("_g"))
    if gold:
        return gold[0]
    base = sorted(t for t in tables if _is_base(t))
    if base:
        return base[0]
    return sorted(tables)[0]


def main() -> int:
    env = os.environ.get("WATERMARK_ENV", util.DEFAULT_ENV_CODE)
    cfg = util.load_config(env)
    enum_path = HERE / f"enumerated_{env}_tables.json"
    if not enum_path.exists():
        print(f"missing {enum_path}; run enumerate_tables.py with WATERMARK_ENV={env} first")
        return 2

    data = json.loads(enum_path.read_text(encoding="utf-8"))
    by_db: dict[str, list[str]] = {}
    for t in data["tables"]:
        by_db.setdefault(t["database"], []).append(t["table"])

    specs: list[str] = []
    for db in sorted(by_db):
        rep = pick_representative(by_db[db])
        if rep:
            specs.append(f"{db}.{rep}")
    print(f"[env={env}] {len(specs)} representative tables (1 per DB, {len(by_db)} DBs)\n")
    run_scan(specs, cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
