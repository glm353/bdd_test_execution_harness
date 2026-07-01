"""Phase 2/3 - resilient batch watermark scan + validation (ad hoc, siloed live-AWS).

Runs Component 1's real code path (`discover_watermarks`, mode='record') against a list of tables,
ONE TABLE AT A TIME wrapped in try/except - so a single failure (no timestamp column, missing table,
Athena error) doesn't abort the batch the way `_record`'s loop would. NOT part of Component 1.

Emits:
  * a per-table status matrix   (ok | empty-null | no-timestamp-column | error)
  * coverage stats              (how many chose `modifiedon` vs a different column, nulls, skips)
  * an Athena cost tally        (DataScannedInBytes per query + total)
and writes the successful rows to the real cache (cache/watermark_<env>.json) so the record->replay
equality check (Phase 3) can run offline afterwards.

Usage:
  python adhoc_tools/scan_watermarks.py                 # default smoke set (~10 tables)
  python adhoc_tools/scan_watermarks.py db.table ...    # explicit tables
"""
from __future__ import annotations

import os
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import util
import watermark

# Deliberately spans layers/families: base, gold(_g), silver(_s), a view(_vw), and a raw(_raw) edge.
SMOKE_SET = [
    "molecular_vms_beakon.contractor",                    # base (known real)
    "domain_core_curriculum.class",                       # base
    "domain_foundation_person.account_details_g",         # gold
    "domain_core_employee.employee_details_g",            # gold
    "business_finance_techone.ascender_costcollectors_g", # gold
    "molecular_hr_ascender.ah_applicant_s",               # silver
    "business_lms_canvas.cohort",                         # base
    "molecular_fm_archibus.assign_room_resource",         # base
    "domain_core_curriculum.class_vw",                    # view (edge)
    "molecular_vms_beakon.contractor_raw",                # raw (edge - may lack a ts column)
]


class StatsSource(util.AwsWatermarkSource):
    """AwsWatermarkSource that records DataScannedInBytes for each Athena query it runs."""
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.last_bytes = 0
        self.total_bytes = 0

    def run_athena(self, sql: str, *, fetch: bool):  # noqa: D401 - mirrors base, adds stats capture
        athena = self._client("athena")
        kwargs = {"QueryString": sql, "WorkGroup": self._discover_workgroup()}
        if self._output:
            kwargs["ResultConfiguration"] = {"OutputLocation": self._output}
        qid = athena.start_query_execution(**kwargs)["QueryExecutionId"]
        deadline = time.time() + self.cfg.athena_timeout_s
        while True:
            ex = athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]
            state = ex["Status"]["State"]
            if state in util._TERMINAL_QUERY_STATES:
                break
            if time.time() > deadline:
                raise TimeoutError(f"Athena query {qid} still {state}")
            time.sleep(2)
        self.last_bytes = ex.get("Statistics", {}).get("DataScannedInBytes", 0)
        self.total_bytes += self.last_bytes
        if state != "SUCCEEDED":
            reason = ex["Status"].get("StateChangeReason", "")
            raise RuntimeError(f"Athena query {state}: {reason}\nSQL:\n{sql}")
        if not fetch:
            return []
        res = athena.get_query_results(QueryExecutionId=qid)
        rows = res["ResultSet"]["Rows"][1:]
        return [tuple(c.get("VarCharValue") for c in r["Data"]) for r in rows]


def _mb(n: int) -> str:
    return f"{n / 1_000_000:.2f} MB"


def run_scan(specs: list[str], cfg) -> list[watermark.TableWatermark]:
    """Resiliently record watermarks for `specs`, print a report, persist good rows to the cache."""
    source = StatsSource(cfg)
    scratch = Path(util.CACHE_DIR) / "_scan_scratch"  # per-table record scratch (avoid clobber)

    rows = []           # (spec, status, column, max_ts, mb, note)
    good: list[watermark.TableWatermark] = []
    print(f"[env={cfg.env_code}] scanning {len(specs)} tables (mode=record, workgroup auto)\n")
    for spec in specs:
        req = watermark.WatermarkRequest.from_specs([spec], env_code=cfg.env_code)
        before = source.total_bytes
        try:
            res = watermark.discover_watermarks(req, mode="record", source=source, cache_dir=scratch)
            w = res.watermarks[0]
            scanned = source.total_bytes - before
            note = ""
            if w.max_timestamp is None:
                status = "empty-null"
            else:
                status = "ok"
            if w.timestamp_column.lower() != util.DEFAULT_WATERMARK_COLUMN:
                note = f"col!=modifiedon ({w.timestamp_column})"
            rows.append((spec, status, w.timestamp_column, w.max_timestamp, _mb(scanned), note))
            good.append(w)
        except ValueError as e:
            rows.append((spec, "no-timestamp-column", "-", None, _mb(0), str(e).split(".")[0]))
        except Exception as e:  # noqa: BLE001 - harness: classify, don't abort
            rows.append((spec, "error", "-", None, _mb(source.total_bytes - before),
                         f"{type(e).__name__}: {e}"))
            traceback.print_exc()

    # --- report -------------------------------------------------------------------------------
    print("\n=== status matrix ===")
    print(f"{'table':<52} {'status':<20} {'column':<16} {'max_timestamp':<22} {'scanned':>10}  note")
    for spec, status, col, ts, mb, note in rows:
        print(f"{spec:<52} {status:<20} {col:<16} {str(ts):<22} {mb:>10}  {note}")

    n = len(rows)
    ok = sum(1 for r in rows if r[1] == "ok")
    empty = sum(1 for r in rows if r[1] == "empty-null")
    no_col = sum(1 for r in rows if r[1] == "no-timestamp-column")
    err = sum(1 for r in rows if r[1] == "error")
    modon = sum(1 for r in rows if r[2] == util.DEFAULT_WATERMARK_COLUMN)
    other_col = sum(1 for r in rows if r[1] in ("ok", "empty-null") and r[2] != util.DEFAULT_WATERMARK_COLUMN)
    print("\n=== coverage ===")
    print(f"  total={n}  ok={ok}  empty-null={empty}  no-timestamp-column={no_col}  error={err}")
    print(f"  column: modifiedon={modon}  other-timestamp-col={other_col}")
    print(f"  Athena total scanned: {_mb(source.total_bytes)}")

    # --- persist good rows to the real cache for the Phase 3 replay-equality check -------------
    if good:
        result = watermark.WatermarkResult(env_code=cfg.env_code, watermarks=good,
                                           recorded_at=util.now_iso())
        path = util.write_cache(result.to_dict(), cfg.env_code)
        print(f"\nwrote {len(good)} rows to {path} (for replay-equality check)")
    return good


def main(argv: list[str]) -> int:
    cfg = util.load_config(os.environ.get("WATERMARK_ENV", util.DEFAULT_ENV_CODE))
    run_scan(argv or SMOKE_SET, cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
