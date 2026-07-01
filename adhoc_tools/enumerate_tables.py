"""Phase 1 - enumerate every database.table in the _dev Glue databases (ad hoc, siloed).

Cheap Glue-Catalog crawl only (no Athena, no data scanned) to produce the universe of
`database.table` refs we then run Component 1 against. NOT part of Component 1.

- Filters Glue databases to the env-suffixed ones (e.g. `_dev`), matching the component's
  `schema_with_env` convention.
- Strips the `_<env>` suffix so the emitted refs use the *logical* database name the component
  expects (it re-applies the suffix at query time; `schema_with_env` is idempotent).
- Paginates both get_databases and get_tables (the component's own get_table reads one page only).

Writes adhoc_tools/enumerated_<env>_tables.json and prints a per-database count summary.

Run: python adhoc_tools/enumerate_tables.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import util

HERE = Path(__file__).resolve().parent


def main() -> int:
    cfg = util.load_config(os.environ.get("WATERMARK_ENV", util.DEFAULT_ENV_CODE))
    suffix = f"_{cfg.env_code}"
    session = util.resolve_session(profile=cfg.profile, region=cfg.region)
    glue = session.client("glue")

    # 1) all databases, env-filtered
    db_names: list[str] = []
    for page in glue.get_paginator("get_databases").paginate():
        db_names += [d["Name"] for d in page.get("DatabaseList", [])]
    env_dbs = sorted(n for n in db_names if n.endswith(suffix))
    print(f"Glue databases: {len(db_names)} total, {len(env_dbs)} matching '{suffix}'")

    # 2) tables per env database
    refs: list[dict] = []
    per_db: list[tuple[str, int]] = []
    for physical_db in env_dbs:
        logical_db = physical_db[: -len(suffix)]  # strip _dev -> logical name for TableRef
        tables: list[str] = []
        for page in glue.get_paginator("get_tables").paginate(DatabaseName=physical_db):
            tables += [t["Name"] for t in page.get("TableList", [])]
        per_db.append((physical_db, len(tables)))
        for t in sorted(tables):
            refs.append({"database": logical_db, "table": t, "physical_database": physical_db})

    print("\nper-database table counts:")
    for name, n in per_db:
        print(f"  {name:<45} {n}")
    print(f"\nTOTAL tables: {len(refs)} across {len(env_dbs)} databases")

    out = HERE / f"enumerated_{cfg.env_code}_tables.json"
    out.write_text(json.dumps({"env_code": cfg.env_code, "count": len(refs), "tables": refs},
                              indent=2), encoding="utf-8")
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
