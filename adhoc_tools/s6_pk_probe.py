"""Session 6 throwaway (READ-ONLY): empirically detect the likely primary key of each candidate table.

Same method used for beakon.contractor in SESSION_5: a column is a single-column PK candidate when it
is unique across every row (COUNT(DISTINCT col) == COUNT(*), with COUNT(*) > 0). For each table we run
ONE Athena query computing COUNT(*) plus COUNT(DISTINCT) for every business column (the CDC metadata
columns ind/changebatchid/modifiedon/modifiedcolumns are excluded - they are never the PK), so it is
a single table scan per table. Among the unique columns we rank by name (id/number/code/key/...) to
surface the most PK-looking one. No single unique column => likely a composite key (check Confluence).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import util  # noqa: E402

CANDIDATES = [
    "molecular_vms_beakon.contractor",
    "domain_core_curriculum.class",
    "molecular_vpermit.molecular_user_permit",
    "business_lms_canvas.cohort",
    "business_esm_servicenow.accounts",
    "business_hr_awams.staff",
    "business_research_nuro.grants",
    "business_lib_alma.student_details",
    "business_erp_techone.staff_details",
    "business_sis_nustar.staff_details",
]

CDC_META = {"ind", "changebatchid", "modifiedon", "modifiedcolumns"}
# Substrings that make a unique column look like a real PK, most-preferred first.
PK_HINTS = ["record_number", "emplid", "_id", "id", "number", "nbr", "code", "key", "uid", "guid"]


def rank(col: str) -> int:
    c = col.lower()
    for i, hint in enumerate(PK_HINTS):
        if hint in c:
            return i
    return len(PK_HINTS)


def probe(src: util.AwsWatermarkSource, spec: str) -> None:
    db_logical, table = util.split_qualified(spec)
    db = util.schema_with_env(db_logical, "dev")
    cols = [c for c, _ in src.describe_columns(db, table)]
    business = [c for c in cols if c.lower() not in CDC_META]
    if not business:
        print(f"{spec}: no business columns"); return

    selects = ['COUNT(*) AS n'] + [f'COUNT(DISTINCT "{c}") AS "{c}"' for c in business]
    sql = f'SELECT {", ".join(selects)} FROM "{db}"."{table}"'
    row = src.run_athena(sql, fetch=True)[0]
    n = int(row[0])
    if n == 0:
        print(f"{spec}: EMPTY (0 rows) -> null watermark, will be skipped by C2"); return

    distincts = {business[i]: int(row[i + 1]) for i in range(len(business))}
    unique = sorted([c for c, d in distincts.items() if d == n], key=rank)
    best = unique[0] if unique else None
    tag = f"PK candidate: {best}" if best else "NO single-column PK (likely composite -> Confluence)"
    print(f"{spec}: rows={n} | {tag}")
    if unique:
        print(f"    all unique cols (ranked): {unique}")
    else:
        # show the closest columns so a composite can be guessed
        near = sorted(distincts.items(), key=lambda kv: -kv[1])[:5]
        print(f"    top distinct counts: {[(c, d) for c, d in near]}")


def main() -> int:
    src = util.AwsWatermarkSource(util.load_config("dev"))
    for spec in CANDIDATES:
        try:
            probe(src, spec)
        except Exception as exc:  # noqa: BLE001
            print(f"{spec}: ERROR {type(exc).__name__}: {str(exc)[:160]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
