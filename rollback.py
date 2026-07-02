"""Component 2 - rollback via ``_aud``: restore the silver/base table to a watermark (ASP-1615).

Input : a Component 1 ``WatermarkResult`` (the max ``modifiedon`` per table = the "known version N").
Output: a ``RollbackResult`` - per table, a summary of the ``_aud`` change rows recorded *after* that
watermark (removed/updated/inserted) and, on ``apply``, the silver rebuild that rolls them back.

The CDCv2 model (corrected in SESSION_5): silver ``<name>`` and gold ``<name>_aud`` are independent
Iceberg tables. Silver is the *current state* (one row per primary key, latest ``ind``); ``_aud`` is
the *audit append-log* - the full row image at every change, with ``ind`` ('D' = delete, else
insert/update), ``changebatchid`` and ``modifiedon``. Because ``_aud`` keeps every image, silver's
state as of any watermark N is exactly reconstructable from it ("Reading 2", validated read-only in
SESSION_5). Rolling back to N therefore means rebuilding silver from the log:

    -- (1) summary (always, read-only): what a rollback would remove
    SELECT ind, COUNT(*) FROM "<db>"."<t>_aud" WHERE modifiedon > N GROUP BY ind

    -- (2) the rollback (apply): restore silver to its state as of N
    DELETE FROM "<db>"."<t>";
    INSERT INTO "<db>"."<t>"
    SELECT <cols> FROM (SELECT <cols>, ROW_NUMBER() OVER (PARTITION BY <pk>
                        ORDER BY modifiedon DESC, changebatchid DESC) rn
                        FROM "<db>"."<t>_aud" WHERE modifiedon <= N) WHERE rn = 1

    -- (3) optional (--truncate-aud): also truncate the audit log back to N
    DELETE FROM "<db>"."<t>_aud" WHERE modifiedon > N

After (2), a Component 1 re-run on silver reproduces the original max timestamp - the ASP-1616
round-trip invariant ``output_1a == output_1b``. Statement (3) is optional (the audit log keeping
post-N history does not affect the invariant) and is currently rejected by Athena on the real ``_aud``
tables (redundant year+month+day(modifiedon) partition transforms - SESSION_4/5); its failure is
recorded per table, never fatal.

The rebuild needs each table's **primary key** (entity identity for "latest image per entity"; the
framework's ``PrimaryKey`` config, possibly composite). It is supplied explicitly per table
(``--pk db.table=col1,col2``). No PK -> the table is still summarized, but ``apply`` is refused for it.

Execution model (mirrors Component 1's caching):

* Dry-run (default) - the summary (1) plus a reconstruction row-count preview when a PK is known;
  never mutates. ``applied`` stays False.
* ``apply=True`` (``--apply``, requires ``mode='record'``) - execute the live silver rebuild (2).
  DELETE+INSERT is not atomic: the pre-rebuild Iceberg snapshot id is recorded first as the
  time-travel recovery point (``SELECT ... FOR VERSION AS OF <id>``).
* ``record`` / ``replay`` / ``auto`` cache modes let the summary be produced/tested offline exactly
  like Component 1 (``cache/rollback_<env>.json``; only ``row_source`` differs between live and cache).

``_raw`` / ``_stg`` tables are out of rollback scope (per the ticket) and are skipped with a reason.
Per-table failures land in ``TableRollback.error`` so one bad table doesn't abort a batch.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path

import util
import watermark as wm

# Layers the ticket puts out of scope for rollback (raw / staging).
_EXCLUDED_SUFFIXES = ("_raw", "_stg")


# --- ind -> removed/updated/inserted bucketing -----------------------------------------------------


def bucket_ind(counts: dict[str, int]) -> dict[str, int]:
    """Map raw ``{ind: count}`` from the ``_aud`` log into removed/updated/inserted/total.

    'D' -> removed, 'I' -> inserted, 'U' (or any other non-'D' indicator) -> updated. Bucketing every
    non-delete that isn't an explicit insert as an update means no change row is dropped from ``total``.
    """
    removed = updated = inserted = 0
    for ind, n in counts.items():
        code = (ind or "").strip().upper()
        if code == util.DELETE_IND:
            removed += n
        elif code == "I":
            inserted += n
        else:
            updated += n
    return {"removed": removed, "updated": updated, "inserted": inserted,
            "total": removed + updated + inserted}


# --- serializable I/O contract ---------------------------------------------------------------------


@dataclass
class RollbackRequest:
    """Component 2 input: the Component 1 output to roll back to, plus per-table primary keys.

    Reuses C1's ``TableWatermark`` rows verbatim - each already carries the ``TableRef``, the watermark
    ``timestamp_column``, and the ``max_timestamp`` cutoff. ``primary_keys`` maps a qualified
    'database.table' name to its PK column list (composite keys supported); the rebuild is only
    possible for tables present here.
    """
    env_code: str
    watermarks: list[wm.TableWatermark] = field(default_factory=list)
    primary_keys: dict[str, list[str]] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"env_code": self.env_code,
                "watermarks": [w.to_dict() for w in self.watermarks],
                "primary_keys": {k: list(v) for k, v in self.primary_keys.items()}}

    @classmethod
    def from_dict(cls, d: dict) -> "RollbackRequest":
        return cls(env_code=d["env_code"],
                   watermarks=[wm.TableWatermark.from_dict(w) for w in d.get("watermarks", [])],
                   primary_keys={k: list(v) for k, v in d.get("primary_keys", {}).items()})

    @classmethod
    def from_watermark_result(cls, result: wm.WatermarkResult,
                              primary_keys: dict[str, list[str]] | None = None) -> "RollbackRequest":
        """Chain directly off a Component 1 result (its ``watermarks`` are the rollback targets)."""
        return cls(env_code=result.env_code, watermarks=list(result.watermarks),
                   primary_keys=dict(primary_keys or {}))


@dataclass
class TableRollback:
    """One result: the post-watermark ``_aud`` summary and (on apply) the silver rebuild outcome."""
    table: wm.TableRef
    aud_table: str
    watermark_column: str
    watermark: str | None            # ISO-8601 cutoff from Component 1 (None -> skipped)
    removed: int
    updated: int
    inserted: int
    total: int
    applied: bool                    # True only when the live silver rebuild ran for this table
    row_source: str                  # provenance of the counts: "aws" | "cache"
    recorded_at: str
    skipped_reason: str | None = None
    # Reading-2 rebuild fields (all optional so pre-rewrite caches still parse).
    silver_table: str = ""           # the table the rollback restores
    primary_key: list[str] | None = None
    recon_rows: int | None = None            # reconstruction row count as of the watermark
    silver_rows_before: int | None = None    # silver COUNT(*) captured just before the rebuild
    silver_rows_after: int | None = None     # silver COUNT(*) after the rebuild (== recon_rows)
    silver_snapshot_before: str | None = None  # Iceberg snapshot id = time-travel recovery point
    aud_truncated: bool = False      # True when the optional _aud truncate (3) also ran
    error: str | None = None         # per-table failure/refusal detail (batch keeps going)

    def to_dict(self) -> dict:
        return {
            "table": self.table.to_dict(),
            "aud_table": self.aud_table,
            "silver_table": self.silver_table,
            "watermark_column": self.watermark_column,
            "watermark": self.watermark,
            "primary_key": self.primary_key,
            "removed": self.removed,
            "updated": self.updated,
            "inserted": self.inserted,
            "total": self.total,
            "recon_rows": self.recon_rows,
            "silver_rows_before": self.silver_rows_before,
            "silver_rows_after": self.silver_rows_after,
            "silver_snapshot_before": self.silver_snapshot_before,
            "applied": self.applied,
            "aud_truncated": self.aud_truncated,
            "row_source": self.row_source,
            "recorded_at": self.recorded_at,
            "skipped_reason": self.skipped_reason,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TableRollback":
        return cls(
            table=wm.TableRef.from_dict(d["table"]),
            aud_table=d["aud_table"],
            silver_table=d.get("silver_table", ""),
            watermark_column=d["watermark_column"],
            watermark=d.get("watermark"),
            primary_key=list(d["primary_key"]) if d.get("primary_key") else None,
            removed=d.get("removed", 0),
            updated=d.get("updated", 0),
            inserted=d.get("inserted", 0),
            total=d.get("total", 0),
            recon_rows=d.get("recon_rows"),
            silver_rows_before=d.get("silver_rows_before"),
            silver_rows_after=d.get("silver_rows_after"),
            silver_snapshot_before=d.get("silver_snapshot_before"),
            applied=d.get("applied", False),
            aud_truncated=d.get("aud_truncated", False),
            row_source=d.get("row_source", "cache"),
            recorded_at=d.get("recorded_at", ""),
            skipped_reason=d.get("skipped_reason"),
            error=d.get("error"),
        )

    @property
    def skipped(self) -> bool:
        return self.skipped_reason is not None


@dataclass
class RollbackResult:
    """Component 2 output: the rollback summary for every input table."""
    env_code: str
    applied: bool = False            # True if any table's silver rebuild was actually executed
    rollbacks: list[TableRollback] = field(default_factory=list)
    recorded_at: str = ""

    def to_dict(self) -> dict:
        return {
            "env_code": self.env_code,
            "applied": self.applied,
            "recorded_at": self.recorded_at,
            "rollbacks": [r.to_dict() for r in self.rollbacks],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RollbackResult":
        return cls(
            env_code=d["env_code"],
            applied=d.get("applied", False),
            recorded_at=d.get("recorded_at", ""),
            rollbacks=[TableRollback.from_dict(r) for r in d.get("rollbacks", [])],
        )

    def summary(self) -> dict[str, int]:
        """Totals across all non-skipped tables - the ticket's 'summary of removed/updated data'."""
        active = [r for r in self.rollbacks if not r.skipped]
        return {
            "tables": len(active),
            "skipped": sum(1 for r in self.rollbacks if r.skipped),
            "removed": sum(r.removed for r in active),
            "updated": sum(r.updated for r in active),
            "inserted": sum(r.inserted for r in active),
            "total": sum(r.total for r in active),
            "applied": sum(1 for r in active if r.applied),
            "errors": sum(1 for r in active if r.error),
        }

    def by_table(self) -> dict[str, int]:
        """Convenience view: {'database.table': total_changes_after_watermark}."""
        return {r.table.qualified: r.total for r in self.rollbacks}


# JSON (de)serialization helpers ---------------------------------------------------------------------

def dumps(result: RollbackResult, *, indent: int = 2) -> str:
    return json.dumps(result.to_dict(), indent=indent)


def loads(text: str) -> RollbackResult:
    return RollbackResult.from_dict(json.loads(text))


# --- Component 2 logic -----------------------------------------------------------------------------


def _skip_reason(ref: wm.TableRef, watermark: str | None) -> str | None:
    """Why (if at all) this table is skipped for rollback."""
    if ref.table.endswith(_EXCLUDED_SUFFIXES):
        return "excluded layer (_raw/_stg not in rollback scope)"
    if watermark is None:
        return "null watermark (table empty at checkpoint; nothing to roll back)"
    return None


def _add_error(rb: TableRollback, msg: str) -> None:
    rb.error = f"{rb.error}; {msg}" if rb.error else msg


def _rebuild_columns(source: "util.AwsWatermarkSource", database: str, silver_table: str,
                     aud_table: str, pk: list[str], ts_column: str) -> list[str]:
    """Select-list for the rebuild: silver ∩ aud columns in silver order, with the PK/ordering
    columns verified present (a wrong PK spec must fail loudly before any write)."""
    silver_cols = [c for c, _ in source.describe_columns(database, silver_table)]
    aud_cols = {c for c, _ in source.describe_columns(database, aud_table)}
    columns = [c for c in silver_cols if c in aud_cols]
    missing = [c for c in pk if c not in columns]
    if missing:
        raise ValueError(f"primary-key column(s) {missing} not present in both "
                         f"{silver_table} and {aud_table}")
    for required in (ts_column, "changebatchid"):
        if required not in aud_cols:
            raise ValueError(f"required ordering column '{required}' not present in {aud_table}")
    return columns


def _rollback_one(w: wm.TableWatermark, env_code: str, *, source: "util.AwsWatermarkSource",
                  pk: list[str] | None, apply: bool, truncate_aud: bool, force: bool,
                  recorded_at: str) -> TableRollback:
    ref = w.table
    aud_table = util.aud_table_name(ref.table)
    silver_table = util.silver_table_name(ref.table)
    rb = TableRollback(
        table=ref, aud_table=aud_table, silver_table=silver_table,
        watermark_column=w.timestamp_column, watermark=w.max_timestamp, primary_key=pk,
        removed=0, updated=0, inserted=0, total=0,
        applied=False, row_source="aws", recorded_at=recorded_at,
    )
    reason = _skip_reason(ref, w.max_timestamp)
    if reason:
        rb.skipped_reason = reason
        return rb

    database = util.schema_with_env(ref.database, env_code)
    try:
        # (1) summary: what a rollback to the watermark would remove. Needs no PK.
        counts = source.count_changes_since(database, aud_table, w.timestamp_column, w.max_timestamp)
        b = bucket_ind(counts)
        rb.removed, rb.updated, rb.inserted, rb.total = (
            b["removed"], b["updated"], b["inserted"], b["total"])

        columns: list[str] = []
        if pk:
            columns = _rebuild_columns(source, database, silver_table, aud_table,
                                       pk, w.timestamp_column)
            rb.recon_rows = source.reconstruction_count(
                database, aud_table, columns, pk, w.timestamp_column, w.max_timestamp)

        if apply:
            if not pk:
                _add_error(rb, "apply refused: no primary key for this table "
                               "(pass --pk db.table=col1[,col2...])")
            elif rb.total > 0 or force:
                # total == 0 -> nothing in the _aud log after the watermark, so silver is presumed
                # already at N and the rebuild is skipped. `force` overrides that presumption for
                # changes that bypassed the pipeline (e.g. a direct silver write, the ASP-1616 seed):
                # the _aud log never saw them, but the rebuild still restores silver to N.
                # (2) the rollback: rebuild silver from the _aud log. Capture the recovery
                # handles first - DELETE+INSERT is not atomic.
                rb.silver_rows_before = source.count_rows(database, silver_table)
                rb.silver_snapshot_before = source.latest_snapshot_id(database, silver_table)
                source.rebuild_silver(database, silver_table, aud_table, columns, pk,
                                      w.timestamp_column, w.max_timestamp)
                rb.silver_rows_after = source.count_rows(database, silver_table)
                rb.applied = True
                if rb.silver_rows_after != rb.recon_rows:
                    _add_error(rb, f"post-rebuild verification failed: silver has "
                                   f"{rb.silver_rows_after} rows, reconstruction expected "
                                   f"{rb.recon_rows} (recover via snapshot "
                                   f"{rb.silver_snapshot_before})")
            if truncate_aud and rb.applied:
                # (3) optional _aud truncate - known blocked by the partition spec; never fatal.
                try:
                    source.delete_changes_since(database, aud_table,
                                                w.timestamp_column, w.max_timestamp)
                    rb.aud_truncated = True
                except Exception as exc:  # noqa: BLE001
                    _add_error(rb, f"_aud truncate failed: {exc}")
    except Exception as exc:  # noqa: BLE001 - one bad table must not abort the batch
        _add_error(rb, f"{type(exc).__name__}: {exc}")
    return rb


def _record(request: RollbackRequest, *, source: "util.AwsWatermarkSource", cache_dir: Path,
            apply: bool, truncate_aud: bool, force: bool) -> RollbackResult:
    """Live path: summarize (and optionally rebuild) per table, then cache the result."""
    recorded_at = util.now_iso()
    rollbacks = [
        _rollback_one(w, request.env_code, source=source,
                      pk=request.primary_keys.get(w.table.qualified),
                      apply=apply, truncate_aud=truncate_aud, force=force,
                      recorded_at=recorded_at)
        for w in request.watermarks
    ]
    result = RollbackResult(
        env_code=request.env_code, applied=any(r.applied for r in rollbacks),
        rollbacks=rollbacks, recorded_at=recorded_at,
    )
    util.write_cache(result.to_dict(), request.env_code, cache_dir, prefix="rollback")
    return result


def _replay(request: RollbackRequest, *, cache_dir: Path) -> RollbackResult:
    """Offline path: rebuild the summary from the cached JSON, filtered to the requested tables."""
    cached = RollbackResult.from_dict(util.read_cache(request.env_code, cache_dir, prefix="rollback"))
    for rb in cached.rollbacks:
        rb.row_source = "cache"  # this read came from the cache: label provenance honestly
    wanted = {w.table.qualified for w in request.watermarks}
    if not wanted:  # empty request -> return the whole cached snapshot
        return cached
    picked = [rb for rb in cached.rollbacks if rb.table.qualified in wanted]
    missing = wanted - {rb.table.qualified for rb in picked}
    if missing:
        raise KeyError(
            f"Tables not present in the rollback cache for env '{request.env_code}': {sorted(missing)}. "
            f"Re-run with mode='record' to refresh the cache."
        )
    return RollbackResult(env_code=cached.env_code, applied=cached.applied,
                          rollbacks=picked, recorded_at=cached.recorded_at)


def rollback_aud(request: RollbackRequest, *, mode: str = "auto", apply: bool = False,
                 truncate_aud: bool = False, force: bool = False,
                 cache_dir: Path = util.CACHE_DIR,
                 source: "util.AwsWatermarkSource | None" = None,
                 okta_login: bool = False) -> RollbackResult:
    """Run Component 2. See the module docstring for the modes and the dry-run/apply contract.

    ``source`` is injectable so tests can drive the live path without real AWS.
    """
    if apply and mode != "record":
        raise ValueError("apply=True requires mode='record' (a live AWS run); "
                         "replay/auto are read-only.")
    if truncate_aud and not apply:
        raise ValueError("truncate_aud=True requires apply=True (it is stage 3 of a live rollback).")
    if force and not apply:
        raise ValueError("force=True requires apply=True (it only overrides the total==0 "
                         "rebuild skip).")
    cache_dir = Path(cache_dir)
    if mode == "replay":
        return _replay(request, cache_dir=cache_dir)
    if mode == "auto" and util.cache_path(request.env_code, cache_dir, prefix="rollback").exists():
        return _replay(request, cache_dir=cache_dir)
    if mode not in ("record", "auto"):
        raise ValueError(f"Unknown mode {mode!r} (expected 'record', 'replay' or 'auto').")
    # record (or auto with no cache yet)
    source = source or util.AwsWatermarkSource(util.load_config(request.env_code), okta_login=okta_login)
    return _record(request, source=source, cache_dir=cache_dir, apply=apply,
                   truncate_aud=truncate_aud, force=force)


# --- CLI -------------------------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rollback",
        description="Component 2: restore silver tables to a Component 1 watermark by replaying "
                    "their _aud audit logs (ASP-1615).",
    )
    p.add_argument("--from-watermark", required=True, metavar="PATH",
                   help="Path to a Component 1 WatermarkResult JSON (its output) to roll back to.")
    p.add_argument("--env", default=None,
                   help="Environment code (default: the env_code in the watermark file).")
    p.add_argument("--pk", action="append", default=[], metavar="DB.TABLE=COL[,COL...]",
                   help="Primary key for a table (repeatable; composite keys comma-separated). "
                        "Required per table for --apply; without it a table is summarized only.")
    p.add_argument("--mode", choices=["record", "replay", "auto"], default="auto",
                   help="record=live AWS+cache, replay=offline cache only, auto=replay if cached "
                        "else record (default).")
    p.add_argument("--apply", action="store_true",
                   help="Execute the live silver rebuild (DELETE + INSERT from _aud; requires "
                        "--mode record). Default: dry-run (summary only, no mutation).")
    p.add_argument("--truncate-aud", action="store_true",
                   help="After a successful rebuild, also DELETE the post-watermark _aud rows "
                        "(stage 3; requires --apply). Currently blocked by the _aud partition spec - "
                        "the failure is recorded per table.")
    p.add_argument("--force", action="store_true",
                   help="Rebuild even when the _aud log shows 0 post-watermark changes (requires "
                        "--apply). Needed when silver was changed without going through the "
                        "pipeline, so the change never reached _aud (e.g. the ASP-1616 seed row).")
    p.add_argument("--okta-login", action="store_true",
                   help="On credential failure, shell out to okta-aws-cli to refresh and retry.")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    c1_result = wm.loads(Path(args.from_watermark).read_text(encoding="utf-8"))
    primary_keys = dict(util.parse_pk_spec(spec) for spec in args.pk)
    request = RollbackRequest.from_watermark_result(c1_result, primary_keys=primary_keys)
    if args.env:
        request.env_code = args.env
    result = rollback_aud(request, mode=args.mode, apply=args.apply,
                          truncate_aud=args.truncate_aud, force=args.force,
                          okta_login=args.okta_login)
    print(dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
