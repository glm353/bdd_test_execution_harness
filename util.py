"""Utility classes and functions for the watermark-discovery component (ASP-1614).

Everything here is supporting plumbing - AWS connectivity, SQL execution, schema
introspection, table-name helpers and the JSON cache. The component's own logic
(the serializable models + ``discover_watermarks``) lives in ``watermark.py``.

The AWS / auth / naming code is adapted from the proven patterns in
``v2 Tooling/poc-pythonbdd`` (``backends/aws.py``, ``backends/aws_auth.py``,
``backends/aws_config.py``, ``derivation.py``) so this component stays consistent
with the rest of the V2 tooling.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# boto3 is only needed for the live ("record") path. Import lazily so the offline
# "replay" path (local testing) works with no AWS SDK / credentials present.
try:  # pragma: no cover - trivial import guard
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError
except ImportError:  # pragma: no cover
    boto3 = None
    BotoCoreError = ClientError = NoCredentialsError = Exception

# --- paths / constants -----------------------------------------------------------------------------

PKG_DIR = Path(__file__).resolve().parent
CACHE_DIR = PKG_DIR / "cache"
DEFAULT_ENV_CODE = "dev"
DEFAULT_REGION = "ap-southeast-2"  # UoN's AWS region
DEFAULT_WATERMARK_COLUMN = "modifiedon"  # CDCv2 CDC/audit timestamp column
IND_COLUMN = "ind"          # CDCv2 _aud operation indicator ('D' = delete, else insert/update)
DELETE_IND = "D"            # value of IND_COLUMN marking a deleted row

_TERMINAL_QUERY_STATES = {"SUCCEEDED", "FAILED", "CANCELLED"}
# Glue/Athena types we treat as usable watermark columns.
_TIMESTAMP_GLUE_TYPES = {"timestamp", "timestamp with time zone", "timestamptz"}


def now_iso() -> str:
    """UTC timestamp as an ISO-8601 string (used for cache/record metadata)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Athena renders timestamps as 'YYYY-MM-DD HH:MM:SS[.ffffff]' with a space separator and, for
# `timestamp with time zone`, a trailing zone (' UTC' or an offset) - NOT ISO-8601. A bare `date`
# column comes back as 'YYYY-MM-DD'. This regex captures those shapes so we can normalise them.
_ATHENA_TS_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})"
    r"(?:[ T](?P<time>\d{2}:\d{2}:\d{2}(?:\.\d+)?))?"
    r"(?:\s*(?P<tz>UTC|Z|[+-]\d{2}:?\d{2}))?$"
)


def normalize_timestamp(raw: str | None) -> str | None:
    """Normalise an Athena timestamp/date string to ISO-8601.

    'YYYY-MM-DD HH:MM:SS.ffffff UTC' -> 'YYYY-MM-DDTHH:MM:SS.ffffff+00:00' (T separator, offset).
    A bare date ('YYYY-MM-DD') is already valid ISO-8601 and returned unchanged. Anything that
    doesn't match a recognised Athena shape is returned verbatim - we normalise, never drop data.
    """
    if raw is None:
        return None
    s = raw.strip()
    if not s:
        return None
    m = _ATHENA_TS_RE.match(s)
    if not m:
        return raw
    date, time_, tz = m.group("date"), m.group("time"), m.group("tz")
    if time_ is None:
        return date  # date-only is already ISO-8601
    iso = f"{date}T{time_}"
    if tz:
        if tz in ("UTC", "Z"):
            iso += "+00:00"
        else:  # ±HHMM or ±HH:MM -> ±HH:MM
            iso += tz if ":" in tz else f"{tz[:3]}:{tz[3:]}"
    return iso


def after_watermark_sql(column: str, watermark: str) -> str:
    """Athena WHERE predicate: `_aud` rows whose timestamp `column` is strictly after `watermark`.

    `watermark` is a normalized ISO-8601 string (see :func:`normalize_timestamp`), parsed with
    ``from_iso8601_timestamp_nanos`` - mirroring the framework's incremental-read convention (a
    ``from_iso8601`` parse) but at nanosecond precision. The plain ``from_iso8601_timestamp`` returns
    only ``timestamp(3)`` (milliseconds), which silently truncates a microsecond watermark like
    ``...312726`` to ``...312`` - so rows sitting exactly at the watermark (``...312726``) compare as
    strictly *after* it and get wrongly counted/deleted, breaking the ASP-1616 round-trip. Verified
    live in SESSION_4 (contractor_aud: 2 boundary rows counted with ``_timestamp``, 0 with ``_nanos``).
    ``modifiedon`` is ``timestamp(6) with time zone``; the nanos parse compares cleanly against it.

    This is the single place to tune the comparison. The value is single-quote-escaped; column/
    watermark otherwise come from our own models.
    """
    safe = watermark.replace("'", "''")
    return f'"{column}" > from_iso8601_timestamp_nanos(\'{safe}\')'


def upto_watermark_sql(column: str, watermark: str) -> str:
    """Athena WHERE predicate: rows whose timestamp `column` is at or before `watermark`.

    The ``<=`` companion of :func:`after_watermark_sql` (same ``from_iso8601_timestamp_nanos``
    precision - see that docstring for why nanos). Used by the Reading-2 silver rebuild: the
    reconstruction selects the latest ``_aud`` image per PK *up to and including* the watermark.
    Keep both predicates here so the comparison stays tuned in one place.
    """
    safe = watermark.replace("'", "''")
    return f'"{column}" <= from_iso8601_timestamp_nanos(\'{safe}\')'


def reconstruction_sql(database: str, aud_table: str, columns: list[str], pk_columns: list[str],
                       ts_column: str, watermark: str) -> str:
    """SELECT reproducing a table's state as of `watermark` from its ``_aud`` append-log.

    ``_aud`` stores the full row image at every change, so the latest image per primary key at or
    before the watermark *is* the row's state then (soft-deletes included: an ``ind='D'`` image comes
    back flagged, exactly as silver stores it). Validated read-only in SESSION_5 (contractor: 157
    reconstructed rows == 157 silver rows, EXCEPT both ways = 0). ``changebatchid`` breaks ties for
    same-timestamp images within a run.
    """
    cols = ", ".join(f'"{c}"' for c in columns)
    pk = ", ".join(f'"{c}"' for c in pk_columns)
    return (
        f"SELECT {cols} FROM ("
        f'SELECT {cols}, ROW_NUMBER() OVER (PARTITION BY {pk} '
        f'ORDER BY "{ts_column}" DESC, "changebatchid" DESC) AS rn '
        f'FROM "{database}"."{aud_table}" '
        f"WHERE {upto_watermark_sql(ts_column, watermark)}"
        f") WHERE rn = 1"
    )


# --- table-name helpers (logical <-> env-qualified) ------------------------------------------------
# Ported from poc-pythonbdd/bdd_poc/derivation.py.

def split_qualified(name: str) -> tuple[str, str]:
    """'schema.table' -> ('schema', 'table')."""
    schema, _, table = name.partition(".")
    if not table:
        raise ValueError(f"Expected a 'schema.table' name, got {name!r}")
    return schema, table


def schema_with_env(schema: str, env_code: str = DEFAULT_ENV_CODE) -> str:
    """'domain_core_curriculum' -> 'domain_core_curriculum_dev' (idempotent)."""
    suffix = f"_{env_code}"
    return schema if schema.endswith(suffix) else f"{schema}{suffix}"


AUD_SUFFIX = "_aud"  # CDCv2 gold table = <silver>_aud (see the BaseTemplate GoldTable convention)


def aud_table_name(table: str) -> str:
    """'contractor' -> 'contractor_aud' (idempotent).

    The CDCv2 gold/audit table for a logical table is the silver name with an ``_aud`` suffix. Passing
    an already-``_aud`` name returns it unchanged so Component 2 works whether the caller feeds silver
    names or the ``_aud`` tables directly.
    """
    return table if table.endswith(AUD_SUFFIX) else f"{table}{AUD_SUFFIX}"


def silver_table_name(table: str) -> str:
    """'contractor_aud' -> 'contractor' (idempotent). Inverse of :func:`aud_table_name`."""
    return table[: -len(AUD_SUFFIX)] if table.endswith(AUD_SUFFIX) else table


def parse_pk_spec(spec: str) -> tuple[str, list[str]]:
    """Parse a CLI ``--pk`` spec: 'db.table=col1,col2' -> ('db.table', ['col1', 'col2']).

    The framework's ``PrimaryKey`` process-config value is a comma-separated column list (composite
    keys are real, e.g. 'emplid,name_type,effdt'), so the right-hand side mirrors that shape.
    """
    qualified, _, cols = spec.partition("=")
    qualified = qualified.strip()
    pk = [c.strip() for c in cols.split(",") if c.strip()]
    if not qualified or "." not in qualified or not pk:
        raise ValueError(
            f"Expected a 'database.table=col1[,col2...]' primary-key spec, got {spec!r}")
    return qualified, pk


# --- AWS config ------------------------------------------------------------------------------------
# Adapted from poc-pythonbdd/bdd_poc/backends/aws_config.py. Every value is env-overridable so the
# same component can target uat/prod later without code changes.

@dataclass(frozen=True)
class AwsConfig:
    env_code: str = DEFAULT_ENV_CODE
    profile: str = "default"
    region: str = DEFAULT_REGION
    athena_workgroup: str | None = None   # None -> discover at runtime
    athena_output: str | None = None      # None -> use the chosen workgroup's own OutputLocation
    athena_timeout_s: int = 120


def load_config(env_code: str = DEFAULT_ENV_CODE) -> AwsConfig:
    return AwsConfig(
        env_code=env_code,
        profile=os.environ.get("WATERMARK_AWS_PROFILE", "default"),
        region=os.environ.get("WATERMARK_AWS_REGION", DEFAULT_REGION),
        athena_workgroup=os.environ.get("WATERMARK_ATHENA_WORKGROUP") or None,
        athena_output=os.environ.get("WATERMARK_ATHENA_OUTPUT") or None,
    )


# --- AWS auth (Okta SSO) ---------------------------------------------------------------------------
# Ported from poc-pythonbdd/bdd_poc/backends/aws_auth.py. The recommended setup is a ~/.aws/config
# profile whose credential_process runs `okta-aws-cli web` (e.g. the `cdcv2-dev` profile), so boto3
# refreshes transparently. resolve_session only *verifies* creds with an sts preflight.

class AuthError(RuntimeError):
    pass


_CRED_ERROR_CODES = {
    "ExpiredToken", "ExpiredTokenException", "InvalidClientTokenId",
    "UnrecognizedClientException", "RequestExpired", "AccessDenied",
}
_CRED_ERROR_TYPES = {
    "SSOTokenLoadError", "UnauthorizedSSOTokenError",
    "TokenRetrievalError", "CredentialRetrievalError",
}


def resolve_region(region: str | None = None) -> str:
    return (region or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
            or DEFAULT_REGION)


def make_session(profile: str | None = None, region: str | None = None):
    if boto3 is None:
        raise AuthError("boto3 is not installed; live AWS access is unavailable. "
                        "Use mode='replay' for offline/cached runs, or `pip install boto3`.")
    profile = profile or os.environ.get("AWS_PROFILE") or None
    return boto3.Session(profile_name=profile, region_name=resolve_region(region))


def _is_credential_error(exc: Exception) -> bool:
    if isinstance(exc, NoCredentialsError):
        return True
    if isinstance(exc, ClientError):
        return exc.response.get("Error", {}).get("Code", "") in _CRED_ERROR_CODES
    return type(exc).__name__ in _CRED_ERROR_TYPES


def _expired_guidance(profile: str | None) -> str:
    p = profile or "<profile>"
    return (
        f"AWS credentials for profile '{p}' are missing or expired.\n"
        f"Refresh with okta-aws-cli (or let a credential_process profile refresh on next call), e.g.:\n"
        f"  okta-aws-cli web --write-aws-credentials --profile {p} \\\n"
        f"    --org-domain uon.okta.com --oidc-client-id <id> --aws-acct-fed-app-id <id>\n"
        f"...or call resolve_session(okta_login=True) to refresh automatically."
    )


def _okta_login_command(profile: str | None) -> list[str]:
    """okta-aws-cli argv for an explicit refresh. OKTA_LOGIN_COMMAND overrides verbatim."""
    override = os.environ.get("OKTA_LOGIN_COMMAND")
    if override:
        return shlex.split(override)
    cmd = [
        "okta-aws-cli", "web", "--format", "aws-credentials", "--write-aws-credentials",
        "--org-domain", os.environ.get("OKTA_ORG_DOMAIN", "uon.okta.com"),
        "--oidc-client-id", os.environ.get("OKTA_OIDC_CLIENT_ID", ""),
        "--aws-acct-fed-app-id", os.environ.get("OKTA_AWS_ACCT_FED_APP_ID", ""),
    ]
    if profile:
        cmd += ["--profile", profile]
    return cmd


def _run_okta_login(profile: str | None, *, runner=subprocess.run) -> None:
    cmd = _okta_login_command(profile)
    print(f"refreshing AWS credentials via: {' '.join(cmd)}")
    try:
        result = runner(cmd)
    except FileNotFoundError as exc:
        raise AuthError(
            "okta-aws-cli not found on PATH. Install it: https://github.com/okta/okta-aws-cli"
        ) from exc
    if getattr(result, "returncode", 0) != 0:
        raise AuthError(f"okta-aws-cli exited with status {result.returncode}")


def resolve_session(*, profile: str | None = None, region: str | None = None,
                    okta_login: bool = False, runner=subprocess.run):
    """Return a credential-verified boto3 Session (sts get-caller-identity preflight)."""
    profile = profile or os.environ.get("AWS_PROFILE") or None
    region = resolve_region(region)

    session = make_session(profile, region)
    try:
        session.client("sts").get_caller_identity()
        return session
    except (BotoCoreError, ClientError) as exc:
        if not _is_credential_error(exc):
            raise
        if not okta_login:
            raise AuthError(_expired_guidance(profile)) from exc

    _run_okta_login(profile, runner=runner)
    session = make_session(profile, region)
    try:
        session.client("sts").get_caller_identity()
    except (BotoCoreError, ClientError) as exc:
        raise AuthError("AWS credentials still invalid after okta-aws-cli refresh.") from exc
    return session


# --- live AWS source: Athena queries + Glue schema introspection -----------------------------------
# Adapted from poc-pythonbdd/bdd_poc/backends/aws.py. A thin object so watermark.py can call
# describe_columns()/max_timestamp() without touching boto3 directly.

class AwsWatermarkSource:
    """Runs the MAX() watermark query on Athena and reads column schemas from Glue."""

    def __init__(self, config: AwsConfig | None = None, *, session=None, okta_login: bool = False):
        self.cfg = config or load_config()
        self.session = session or resolve_session(
            profile=self.cfg.profile, region=self.cfg.region, okta_login=okta_login
        )
        self._clients: dict = {}
        self._workgroup: str | None = self.cfg.athena_workgroup
        self._output: str | None = self.cfg.athena_output

    def _client(self, name: str):
        if name not in self._clients:
            self._clients[name] = self.session.client(name)
        return self._clients[name]

    def _discover_workgroup(self) -> str:
        """Pick an Athena workgroup with a configured OutputLocation (fall back to 'primary')."""
        if self._workgroup:
            return self._workgroup
        athena = self._client("athena")
        chosen = None
        for wg in athena.list_work_groups().get("WorkGroups", []):
            name = wg["Name"]
            cfg = athena.get_work_group(WorkGroup=name)["WorkGroup"].get("Configuration", {})
            if cfg.get("ResultConfiguration", {}).get("OutputLocation"):
                chosen = name
                break
        self._workgroup = chosen or "primary"
        print(f"[aws] Athena workgroup = {self._workgroup} "
              f"(output override = {self._output or '<workgroup-configured>'})")
        return self._workgroup

    def run_athena(self, sql: str, *, fetch: bool) -> list[tuple]:
        athena = self._client("athena")
        kwargs = {"QueryString": sql, "WorkGroup": self._discover_workgroup()}
        if self._output:  # only override when the user pinned one (else use the workgroup's own)
            kwargs["ResultConfiguration"] = {"OutputLocation": self._output}
        qid = athena.start_query_execution(**kwargs)["QueryExecutionId"]

        deadline = time.time() + self.cfg.athena_timeout_s
        while True:
            ex = athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]
            state = ex["Status"]["State"]
            if state in _TERMINAL_QUERY_STATES:
                break
            if time.time() > deadline:
                raise TimeoutError(f"Athena query {qid} still {state} after {self.cfg.athena_timeout_s}s")
            time.sleep(2)
        if state != "SUCCEEDED":
            reason = ex["Status"].get("StateChangeReason", "")
            raise RuntimeError(f"Athena query {state}: {reason}\nSQL:\n{sql}")
        if not fetch:
            return []

        res = athena.get_query_results(QueryExecutionId=qid)
        rows = res["ResultSet"]["Rows"][1:]  # drop the header row
        return [tuple(c.get("VarCharValue") for c in r["Data"]) for r in rows]

    def describe_columns(self, database: str, table: str) -> list[tuple[str, str]]:
        """Return [(column_name, glue_type), ...] for a table, from the Glue Catalog."""
        meta = self._client("glue").get_table(DatabaseName=database, Name=table)["Table"]
        cols = meta.get("StorageDescriptor", {}).get("Columns", [])
        return [(c["Name"], c.get("Type", "")) for c in cols]

    def max_timestamp(self, database: str, table: str, column: str) -> str | None:
        """Run SELECT MAX("column") FROM "database"."table" and return the scalar (or None)."""
        sql = f'SELECT MAX("{column}") FROM "{database}"."{table}"'
        rows = self.run_athena(sql, fetch=True)
        if not rows or rows[0][0] is None:
            return None
        return normalize_timestamp(rows[0][0])

    def count_changes_since(self, database: str, aud_table: str, column: str,
                            watermark: str) -> dict[str, int]:
        """Count `_aud` change rows appended after `watermark`, grouped by the `ind` indicator.

        Returns ``{ind_value: count}`` (e.g. ``{'D': 3, 'U': 10, 'I': 2}``); an empty dict when nothing
        changed. The caller (Component 2) maps `ind` to removed/updated/inserted. `watermark` is a
        normalized ISO-8601 string (see :func:`normalize_timestamp`).
        """
        sql = (f'SELECT "{IND_COLUMN}", COUNT(*) FROM "{database}"."{aud_table}" '
               f'WHERE {after_watermark_sql(column, watermark)} GROUP BY "{IND_COLUMN}"')
        counts: dict[str, int] = {}
        for ind, n in self.run_athena(sql, fetch=True):
            counts[(ind or "").strip()] = int(n)
        return counts

    def delete_changes_since(self, database: str, aud_table: str, column: str,
                             watermark: str) -> None:
        """Iceberg row-level DELETE of `_aud` rows appended after `watermark` (append-log truncate).

        The optional statement (3) of the Reading-2 rollback. NOTE: currently rejected by Athena on
        the real `_aud` tables - their year+month+day(modifiedon) partition spec makes any write that
        produces data/delete files fail with INVALID_TABLE_PROPERTY (SESSION_4/5). Kept for when the
        spec is fixed or a Spark DELETE path exists; only invoked on the guarded ``truncate_aud`` path.
        """
        sql = (f'DELETE FROM "{database}"."{aud_table}" '
               f'WHERE {after_watermark_sql(column, watermark)}')
        self.run_athena(sql, fetch=False)

    def count_rows(self, database: str, table: str) -> int:
        """SELECT COUNT(*) - used for the pre/post row counts around the silver rebuild."""
        rows = self.run_athena(f'SELECT COUNT(*) FROM "{database}"."{table}"', fetch=True)
        return int(rows[0][0])

    def reconstruction_count(self, database: str, aud_table: str, columns: list[str],
                             pk_columns: list[str], ts_column: str, watermark: str) -> int:
        """Row count of the Reading-2 reconstruction (state as of `watermark`), read-only.

        Reported on dry-runs as a preview and used as the post-rebuild verification target.
        """
        sql = (f"SELECT COUNT(*) FROM ("
               f"{reconstruction_sql(database, aud_table, columns, pk_columns, ts_column, watermark)}"
               f")")
        rows = self.run_athena(sql, fetch=True)
        return int(rows[0][0])

    def latest_snapshot_id(self, database: str, table: str) -> str | None:
        """Latest Iceberg snapshot id of `table` (from its ``$history`` metadata table), best-effort.

        Captured before the destructive DELETE+INSERT rebuild as the recovery handle: the pre-rebuild
        data stays readable via ``SELECT ... FOR VERSION AS OF <id>`` time travel. Returns None if the
        metadata read fails - the rebuild proceeds, just without a recorded recovery point.
        """
        sql = (f'SELECT snapshot_id FROM "{database}"."{table}$history" '
               f"ORDER BY made_current_at DESC LIMIT 1")
        try:
            rows = self.run_athena(sql, fetch=True)
        except Exception:  # noqa: BLE001 - metadata read is advisory, never blocks the rollback
            return None
        return rows[0][0] if rows else None

    def rebuild_silver(self, database: str, silver_table: str, aud_table: str, columns: list[str],
                       pk_columns: list[str], ts_column: str, watermark: str) -> None:
        """The Reading-2 rollback, statement (2): restore silver to its state as of `watermark`.

        DELETE FROM silver, then INSERT the reconstruction (latest ``_aud`` image per PK <= watermark).
        Silver is unpartitioned (SESSION_5), so both statements avoid the `_aud` partition-spec wall.
        NOT atomic: a failure between the two leaves silver empty - the caller records the pre-rebuild
        snapshot id (see :meth:`latest_snapshot_id`) as the time-travel recovery point.
        """
        self.run_athena(f'DELETE FROM "{database}"."{silver_table}"', fetch=False)
        cols = ", ".join(f'"{c}"' for c in columns)
        insert = (f'INSERT INTO "{database}"."{silver_table}" ({cols}) '
                  f"{reconstruction_sql(database, aud_table, columns, pk_columns, ts_column, watermark)}")
        self.run_athena(insert, fetch=False)


def timestamp_columns(columns: list[tuple[str, str]]) -> list[str]:
    """Names of the timestamp-typed columns in ``columns`` ([(name, glue_type), ...])."""
    return [name for name, gtype in columns
            if gtype.split("(")[0].strip().lower() in _TIMESTAMP_GLUE_TYPES]


def pick_watermark_column(columns: list[tuple[str, str]],
                          preferred: str = DEFAULT_WATERMARK_COLUMN) -> str | None:
    """Choose a timestamp-typed column, preferring ``preferred`` (default 'modifiedon').

    ``columns`` is [(name, glue_type), ...]. Returns the column name, or None if the table has no
    timestamp-typed column at all.
    """
    ts_cols = timestamp_columns(columns)
    if not ts_cols:
        return None
    for name in ts_cols:
        if name.lower() == preferred.lower():
            return name
    return ts_cols[0]


# --- JSON cache (record / replay) ------------------------------------------------------------------
# Mirrors the shape of poc-pythonbdd/bdd_poc/cache/discovery_dev.json (top-level metadata + payload).
# `prefix` selects the component's cache file: 'watermark' (C1) or 'rollback' (C2).

def cache_path(env_code: str, cache_dir: Path = CACHE_DIR, *, prefix: str = "watermark") -> Path:
    return Path(cache_dir) / f"{prefix}_{env_code}.json"


def write_cache(payload: dict, env_code: str, cache_dir: Path = CACHE_DIR,
                *, prefix: str = "watermark") -> Path:
    path = cache_path(env_code, cache_dir, prefix=prefix)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def read_cache(env_code: str, cache_dir: Path = CACHE_DIR, *, prefix: str = "watermark") -> dict:
    path = cache_path(env_code, cache_dir, prefix=prefix)
    if not path.exists():
        raise FileNotFoundError(
            f"No {prefix} cache at {path}. Run with mode='record' (live AWS) first, "
            f"or point cache_dir at an existing snapshot."
        )
    return json.loads(path.read_text(encoding="utf-8"))
