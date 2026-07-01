"""Component 1 - serializable watermark discovery (ASP-1614).

Input : a set of ``database.table`` references.
Output: the max timestamp *per table* (not per row).

The input/output are clean, self-contained dataclasses that serialize to/from plain JSON, so this
component can be chained into Component 2 (the ``_aud`` rollback) without leaking any AWS handles.

Three run modes support local testing (see the ``--mode`` CLI flag / ``discover_watermarks``):

* ``record``  - query live AWS (Athena + Glue) and write the result to ``cache/watermark_<env>.json``.
* ``replay``  - read that cached JSON only; no AWS/boto3/Okta needed (offline local testing).
* ``auto``    - replay if a cache file exists, else record. The default.

Because the output dataclasses are identical regardless of mode (only ``row_source`` differs),
a replayed result is directly comparable to a live one - which is what makes the local cache useful
for testing Component 1 (and, later, the C1/C2/C1 round-trip checkpoint).
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

import util

# --- serializable I/O contract ---------------------------------------------------------------------


@dataclass(frozen=True)
class TableRef:
    """One input table. ``timestamp_column`` is an optional per-table watermark-column override."""
    database: str
    table: str
    timestamp_column: str | None = None

    @classmethod
    def from_string(cls, spec: str, timestamp_column: str | None = None) -> "TableRef":
        """Build from a 'database.table' string."""
        database, table = util.split_qualified(spec)
        return cls(database=database, table=table, timestamp_column=timestamp_column)

    @property
    def qualified(self) -> str:
        return f"{self.database}.{self.table}"

    def to_dict(self) -> dict:
        return {"database": self.database, "table": self.table,
                "timestamp_column": self.timestamp_column}

    @classmethod
    def from_dict(cls, d: dict) -> "TableRef":
        return cls(database=d["database"], table=d["table"],
                   timestamp_column=d.get("timestamp_column"))


@dataclass
class WatermarkRequest:
    """Component 1 input: the set of tables to discover watermarks for."""
    tables: list[TableRef]
    env_code: str = util.DEFAULT_ENV_CODE

    def to_dict(self) -> dict:
        return {"env_code": self.env_code, "tables": [t.to_dict() for t in self.tables]}

    @classmethod
    def from_dict(cls, d: dict) -> "WatermarkRequest":
        return cls(
            env_code=d.get("env_code", util.DEFAULT_ENV_CODE),
            tables=[TableRef.from_dict(t) for t in d.get("tables", [])],
        )

    @classmethod
    def from_specs(cls, specs: list[str], env_code: str = util.DEFAULT_ENV_CODE) -> "WatermarkRequest":
        """Build from a list of 'database.table' strings (e.g. CLI args)."""
        return cls(tables=[TableRef.from_string(s) for s in specs], env_code=env_code)


@dataclass
class TableWatermark:
    """One result: the max timestamp of a single table."""
    table: TableRef
    timestamp_column: str
    max_timestamp: str | None          # ISO-8601 string, or None for an empty table
    row_source: str                    # "aws" | "cache"
    recorded_at: str

    def to_dict(self) -> dict:
        return {
            "table": self.table.to_dict(),
            "timestamp_column": self.timestamp_column,
            "max_timestamp": self.max_timestamp,
            "row_source": self.row_source,
            "recorded_at": self.recorded_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TableWatermark":
        return cls(
            table=TableRef.from_dict(d["table"]),
            timestamp_column=d["timestamp_column"],
            max_timestamp=d.get("max_timestamp"),
            row_source=d.get("row_source", "cache"),
            recorded_at=d.get("recorded_at", ""),
        )


@dataclass
class WatermarkResult:
    """Component 1 output: watermarks for every input table. This is the object Component 2 consumes."""
    env_code: str
    watermarks: list[TableWatermark] = field(default_factory=list)
    recorded_at: str = ""

    def to_dict(self) -> dict:
        return {
            "env_code": self.env_code,
            "recorded_at": self.recorded_at,
            "watermarks": [w.to_dict() for w in self.watermarks],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "WatermarkResult":
        return cls(
            env_code=d["env_code"],
            recorded_at=d.get("recorded_at", ""),
            watermarks=[TableWatermark.from_dict(w) for w in d.get("watermarks", [])],
        )

    def by_table(self) -> dict[str, str | None]:
        """Convenience view: {'database.table': max_timestamp}."""
        return {w.table.qualified: w.max_timestamp for w in self.watermarks}


# JSON (de)serialization helpers for the whole result -----------------------------------------------

def dumps(result: WatermarkResult, *, indent: int = 2) -> str:
    return json.dumps(result.to_dict(), indent=indent)


def loads(text: str) -> WatermarkResult:
    return WatermarkResult.from_dict(json.loads(text))


# --- Component 1 logic -----------------------------------------------------------------------------


def _record(request: WatermarkRequest, *, source: "util.AwsWatermarkSource",
            cache_dir: Path) -> WatermarkResult:
    """Live path: query AWS for each table, then persist the result to the JSON cache."""
    recorded_at = util.now_iso()
    watermarks: list[TableWatermark] = []
    for ref in request.tables:
        database = util.schema_with_env(ref.database, request.env_code)
        column = ref.timestamp_column
        if column is None:
            columns = source.describe_columns(database, ref.table)
            column = util.pick_watermark_column(columns)
            if column is None:
                raise ValueError(
                    f"{ref.qualified}: no timestamp-typed column found in the Glue schema and no "
                    f"timestamp_column override given. Columns: {[c[0] for c in columns]}"
                )
            # Auto-detect fell back off 'modifiedon' -> surface the choice; a wrong silent pick here
            # would poison the watermark. Set TableRef.timestamp_column to make it explicit.
            if column.lower() != util.DEFAULT_WATERMARK_COLUMN.lower():
                candidates = util.timestamp_columns(columns)
                print(f"[warn] {ref.qualified}: no '{util.DEFAULT_WATERMARK_COLUMN}' column; "
                      f"auto-selected '{column}' from timestamp candidates {candidates}. "
                      f"Pass timestamp_column to override.", file=sys.stderr)
        max_ts = source.max_timestamp(database, ref.table, column)
        watermarks.append(TableWatermark(
            table=ref, timestamp_column=column, max_timestamp=max_ts,
            row_source="aws", recorded_at=recorded_at,
        ))
    result = WatermarkResult(env_code=request.env_code, watermarks=watermarks, recorded_at=recorded_at)
    util.write_cache(result.to_dict(), request.env_code, cache_dir)
    return result


def _replay(request: WatermarkRequest, *, cache_dir: Path) -> WatermarkResult:
    """Offline path: rebuild the result from the cached JSON, filtered to the requested tables."""
    cached = WatermarkResult.from_dict(util.read_cache(request.env_code, cache_dir))
    # This read came from the cache: stamp provenance honestly regardless of how it was recorded.
    for w in cached.watermarks:
        w.row_source = "cache"
    wanted = {ref.qualified for ref in request.tables}
    if not wanted:  # empty request -> return the whole cached snapshot
        return cached
    picked = [w for w in cached.watermarks if w.table.qualified in wanted]
    missing = wanted - {w.table.qualified for w in picked}
    if missing:
        raise KeyError(
            f"Tables not present in the cache for env '{request.env_code}': {sorted(missing)}. "
            f"Re-run with mode='record' to refresh the cache."
        )
    return WatermarkResult(env_code=cached.env_code, watermarks=picked, recorded_at=cached.recorded_at)


def discover_watermarks(request: WatermarkRequest, *, mode: str = "auto",
                        cache_dir: Path = util.CACHE_DIR,
                        source: "util.AwsWatermarkSource | None" = None,
                        okta_login: bool = False) -> WatermarkResult:
    """Run Component 1. See the module docstring for the three modes.

    ``source`` is injectable so tests can drive the live path without real AWS.
    """
    cache_dir = Path(cache_dir)
    if mode == "replay":
        return _replay(request, cache_dir=cache_dir)
    if mode == "auto" and util.cache_path(request.env_code, cache_dir).exists():
        return _replay(request, cache_dir=cache_dir)
    if mode not in ("record", "auto"):
        raise ValueError(f"Unknown mode {mode!r} (expected 'record', 'replay' or 'auto').")
    # record (or auto with no cache yet)
    source = source or util.AwsWatermarkSource(util.load_config(request.env_code), okta_login=okta_login)
    return _record(request, source=source, cache_dir=cache_dir)


# --- CLI -------------------------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="watermark",
        description="Component 1: discover the max timestamp per database.table (ASP-1614).",
    )
    p.add_argument("--tables", nargs="+", metavar="DB.TABLE", required=True,
                   help="One or more 'database.table' references.")
    p.add_argument("--env", default=util.DEFAULT_ENV_CODE, help="Environment code (default: dev).")
    p.add_argument("--mode", choices=["record", "replay", "auto"], default="auto",
                   help="record=live AWS+cache, replay=offline cache only, auto=replay if cached "
                        "else record (default).")
    p.add_argument("--okta-login", action="store_true",
                   help="On credential failure, shell out to okta-aws-cli to refresh and retry.")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    request = WatermarkRequest.from_specs(args.tables, env_code=args.env)
    result = discover_watermarks(request, mode=args.mode, okta_login=args.okta_login)
    print(dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
