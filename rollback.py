"""Component 2 - _aud rollback / gold-table teardown (ASP-1615).

Input : a Component 1 ``WatermarkResult`` (the max ``modifiedon`` per table = the "known version N").
Output: a ``RollbackResult`` - per table, a summary of the ``_aud`` change rows recorded *after* that
watermark (removed/updated/inserted), i.e. the changes that a rollback removes.

The CDCv2 ``_aud`` table is the gold table: an append-log of change records (Iceberg), one row per
change, carrying an ``ind`` operation indicator ('D' = delete, else insert/update) and a ``modifiedon``
timestamp. Rolling back to watermark N means deleting the rows appended after N:

    DELETE FROM "<db>"."<table>_aud" WHERE modifiedon > <watermark>

Removing those rows restores the table's state as of N, so a Component 1 re-run reproduces the original
max timestamp (the ASP-1616 round-trip invariant ``output_1a == output_1b``).

Execution model (mirrors Component 1's caching):

* Dry-run (default) - count what *would* be rolled back; never mutates. ``applied`` stays False.
* ``apply=True`` (``--apply``, requires ``mode='record'``) - execute the live Iceberg DELETE.
* ``record`` / ``replay`` / ``auto`` cache modes let the summary be produced/tested offline exactly
  like Component 1 (``cache/rollback_<env>.json``; only ``row_source`` differs between live and cache).

``_raw`` / ``_stg`` tables are out of rollback scope (per the ticket) and are skipped with a reason.
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
    """Component 2 input: the Component 1 output to roll back to.

    Reuses C1's ``TableWatermark`` rows verbatim - each already carries the ``TableRef``, the watermark
    ``timestamp_column``, and the ``max_timestamp`` cutoff, which is exactly what a rollback needs.
    """
    env_code: str
    watermarks: list[wm.TableWatermark] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"env_code": self.env_code, "watermarks": [w.to_dict() for w in self.watermarks]}

    @classmethod
    def from_dict(cls, d: dict) -> "RollbackRequest":
        return cls(env_code=d["env_code"],
                   watermarks=[wm.TableWatermark.from_dict(w) for w in d.get("watermarks", [])])

    @classmethod
    def from_watermark_result(cls, result: wm.WatermarkResult) -> "RollbackRequest":
        """Chain directly off a Component 1 result (its ``watermarks`` are the rollback targets)."""
        return cls(env_code=result.env_code, watermarks=list(result.watermarks))


@dataclass
class TableRollback:
    """One result: the change rows recorded after the watermark for a single ``_aud`` table."""
    table: wm.TableRef
    aud_table: str
    watermark_column: str
    watermark: str | None            # ISO-8601 cutoff from Component 1 (None -> skipped)
    removed: int
    updated: int
    inserted: int
    total: int
    applied: bool                    # True only when the live DELETE ran for this table
    row_source: str                  # provenance of the counts: "aws" | "cache"
    recorded_at: str
    skipped_reason: str | None = None

    def to_dict(self) -> dict:
        return {
            "table": self.table.to_dict(),
            "aud_table": self.aud_table,
            "watermark_column": self.watermark_column,
            "watermark": self.watermark,
            "removed": self.removed,
            "updated": self.updated,
            "inserted": self.inserted,
            "total": self.total,
            "applied": self.applied,
            "row_source": self.row_source,
            "recorded_at": self.recorded_at,
            "skipped_reason": self.skipped_reason,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TableRollback":
        return cls(
            table=wm.TableRef.from_dict(d["table"]),
            aud_table=d["aud_table"],
            watermark_column=d["watermark_column"],
            watermark=d.get("watermark"),
            removed=d.get("removed", 0),
            updated=d.get("updated", 0),
            inserted=d.get("inserted", 0),
            total=d.get("total", 0),
            applied=d.get("applied", False),
            row_source=d.get("row_source", "cache"),
            recorded_at=d.get("recorded_at", ""),
            skipped_reason=d.get("skipped_reason"),
        )

    @property
    def skipped(self) -> bool:
        return self.skipped_reason is not None


@dataclass
class RollbackResult:
    """Component 2 output: the rollback summary for every input table."""
    env_code: str
    applied: bool = False            # True if any table's rollback was actually executed
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


def _rollback_one(w: wm.TableWatermark, env_code: str, *, source: "util.AwsWatermarkSource",
                  apply: bool, recorded_at: str) -> TableRollback:
    ref = w.table
    aud_table = util.aud_table_name(ref.table)
    rb = TableRollback(
        table=ref, aud_table=aud_table, watermark_column=w.timestamp_column,
        watermark=w.max_timestamp, removed=0, updated=0, inserted=0, total=0,
        applied=False, row_source="aws", recorded_at=recorded_at,
    )
    reason = _skip_reason(ref, w.max_timestamp)
    if reason:
        rb.skipped_reason = reason
        return rb

    database = util.schema_with_env(ref.database, env_code)
    counts = source.count_changes_since(database, aud_table, w.timestamp_column, w.max_timestamp)
    b = bucket_ind(counts)
    rb.removed, rb.updated, rb.inserted, rb.total = b["removed"], b["updated"], b["inserted"], b["total"]
    if apply and rb.total > 0:
        source.delete_changes_since(database, aud_table, w.timestamp_column, w.max_timestamp)
        rb.applied = True
    return rb


def _record(request: RollbackRequest, *, source: "util.AwsWatermarkSource", cache_dir: Path,
            apply: bool) -> RollbackResult:
    """Live path: count (and optionally delete) post-watermark ``_aud`` rows, then cache the summary."""
    recorded_at = util.now_iso()
    rollbacks = [_rollback_one(w, request.env_code, source=source, apply=apply,
                               recorded_at=recorded_at) for w in request.watermarks]
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
                 cache_dir: Path = util.CACHE_DIR,
                 source: "util.AwsWatermarkSource | None" = None,
                 okta_login: bool = False) -> RollbackResult:
    """Run Component 2. See the module docstring for the modes and the dry-run/apply contract.

    ``source`` is injectable so tests can drive the live path without real AWS.
    """
    if apply and mode != "record":
        raise ValueError("apply=True requires mode='record' (a live AWS run); "
                         "replay/auto are read-only.")
    cache_dir = Path(cache_dir)
    if mode == "replay":
        return _replay(request, cache_dir=cache_dir)
    if mode == "auto" and util.cache_path(request.env_code, cache_dir, prefix="rollback").exists():
        return _replay(request, cache_dir=cache_dir)
    if mode not in ("record", "auto"):
        raise ValueError(f"Unknown mode {mode!r} (expected 'record', 'replay' or 'auto').")
    # record (or auto with no cache yet)
    source = source or util.AwsWatermarkSource(util.load_config(request.env_code), okta_login=okta_login)
    return _record(request, source=source, cache_dir=cache_dir, apply=apply)


# --- CLI -------------------------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rollback",
        description="Component 2: roll back _aud gold tables to a Component 1 watermark (ASP-1615).",
    )
    p.add_argument("--from-watermark", required=True, metavar="PATH",
                   help="Path to a Component 1 WatermarkResult JSON (its output) to roll back to.")
    p.add_argument("--env", default=None,
                   help="Environment code (default: the env_code in the watermark file).")
    p.add_argument("--mode", choices=["record", "replay", "auto"], default="auto",
                   help="record=live AWS+cache, replay=offline cache only, auto=replay if cached "
                        "else record (default).")
    p.add_argument("--apply", action="store_true",
                   help="Execute the live Iceberg DELETE (requires --mode record). "
                        "Default: dry-run (summary only, no mutation).")
    p.add_argument("--okta-login", action="store_true",
                   help="On credential failure, shell out to okta-aws-cli to refresh and retry.")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    c1_result = wm.loads(Path(args.from_watermark).read_text(encoding="utf-8"))
    request = RollbackRequest.from_watermark_result(c1_result)
    if args.env:
        request.env_code = args.env
    result = rollback_aud(request, mode=args.mode, apply=args.apply, okta_login=args.okta_login)
    print(dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
