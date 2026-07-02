"""Offline tests for Component 2 (ASP-1615, Reading-2 silver rebuild). No AWS / boto3 / Okta required.

Covers:
  * model round-trip serialization (the C1->C2 contract must survive JSON), incl. primary_keys
  * chaining: a Component 1 WatermarkResult -> a Component 2 RollbackRequest
  * ind -> removed/updated/inserted bucketing and the name / SQL helpers (incl. reconstruction SQL)
  * dry-run vs apply (apply rebuilds silver from _aud; dry-run never mutates)
  * PK handling: --pk spec parsing, apply refused without a PK, wrong PK columns fail loudly
  * the optional _aud truncate (stage 3): guarded by apply, its failure is recorded, never fatal
  * per-table errors don't abort a batch; post-rebuild count mismatch is recorded
  * _raw/_stg and null-watermark tables are skipped with a reason
  * cache record -> replay equality, and pre-rewrite cache JSON still parses
  * end-to-end replay against a checked-in fixture, chained off the C1 fixture
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import rollback as rb
import util
import watermark as wm

FIXTURE_CACHE = Path(__file__).parent / "fixtures"

WM = "2026-06-11T05:47:38+00:00"

# Shared canned schema: what describe_columns returns for both silver and _aud (the real tables have
# identical column sets - SESSION_5). Includes the ordering columns the rebuild requires.
COLUMNS = ["beakon_record_number", "first_name", "ind", "changebatchid", "modifiedon",
           "modifiedcolumns"]


# --- fakes -----------------------------------------------------------------------------------------


class FakeRollbackSource:
    """Stands in for util.AwsWatermarkSource: canned counts/schemas + a record of every write call.

    A counts value that is an Exception is raised by count_changes_since (per-table error path).
    """

    def __init__(self, counts: dict[tuple[str, str], dict[str, int] | Exception], *,
                 columns: list[str] = COLUMNS, recon_rows: int = 100, silver_rows: int = 110,
                 rebuilt_rows: int | None = None, truncate_error: str | None = None):
        self._counts = counts          # {(database, aud_table): {ind: n} | Exception}
        self.columns = columns
        self.recon_rows = recon_rows
        self.silver_rows = silver_rows                 # COUNT(*) before a rebuild
        self.rebuilt_rows = rebuilt_rows               # COUNT(*) after (None -> == recon_rows)
        self.truncate_error = truncate_error
        self._rebuilt_tables: set[tuple[str, str]] = set()
        self.counted: list[tuple[str, str, str, str]] = []
        self.truncated: list[tuple[str, str, str, str]] = []
        self.rebuilds: list[tuple] = []
        self.described: list[tuple[str, str]] = []

    def count_changes_since(self, database, aud_table, column, watermark):
        self.counted.append((database, aud_table, column, watermark))
        value = self._counts.get((database, aud_table), {})
        if isinstance(value, Exception):
            raise value
        return value

    def delete_changes_since(self, database, aud_table, column, watermark):
        if self.truncate_error:
            raise RuntimeError(self.truncate_error)
        self.truncated.append((database, aud_table, column, watermark))

    def describe_columns(self, database, table):
        self.described.append((database, table))
        return [(c, "string") for c in self.columns]

    def count_rows(self, database, table):
        if (database, table) in self._rebuilt_tables:
            return self.recon_rows if self.rebuilt_rows is None else self.rebuilt_rows
        return self.silver_rows

    def reconstruction_count(self, database, aud_table, columns, pk_columns, ts_column, watermark):
        return self.recon_rows

    def latest_snapshot_id(self, database, table):
        return "1234567890"

    def rebuild_silver(self, database, silver_table, aud_table, columns, pk_columns,
                       ts_column, watermark):
        self.rebuilds.append((database, silver_table, aud_table, tuple(columns),
                              tuple(pk_columns), ts_column, watermark))
        self._rebuilt_tables.add((database, silver_table))


def _request(*specs_and_watermarks: tuple[str, str | None], env_code: str = "dev",
             primary_keys: dict[str, list[str]] | None = None) -> rb.RollbackRequest:
    """Build a RollbackRequest by chaining off a synthetic Component 1 result (exercises the contract)."""
    watermarks = [
        wm.TableWatermark(
            table=wm.TableRef.from_string(spec), timestamp_column=util.DEFAULT_WATERMARK_COLUMN,
            max_timestamp=mark, row_source="aws", recorded_at="2026-07-01T00:00:00+00:00",
        )
        for spec, mark in specs_and_watermarks
    ]
    c1 = wm.WatermarkResult(env_code=env_code, watermarks=watermarks,
                            recorded_at="2026-07-01T00:00:00+00:00")
    return rb.RollbackRequest.from_watermark_result(c1, primary_keys=primary_keys)


CONTRACTOR_PK = {"molecular_vms_beakon.contractor": ["beakon_record_number"]}


# --- helpers: names, SQL, bucketing ----------------------------------------------------------------


def test_aud_table_name_is_idempotent():
    assert util.aud_table_name("contractor") == "contractor_aud"
    assert util.aud_table_name("contractor_aud") == "contractor_aud"


def test_silver_table_name_is_inverse_of_aud():
    assert util.silver_table_name("contractor_aud") == "contractor"
    assert util.silver_table_name("contractor") == "contractor"


def test_after_watermark_sql_shape_and_escaping():
    # from_iso8601_timestamp_nanos (not ..._timestamp): the plain form is timestamp(3) and truncates
    # microsecond watermarks to millis, so boundary rows compare as "after" (SESSION_4 live finding).
    sql = util.after_watermark_sql("modifiedon", "2026-06-11T05:47:38.312726+00:00")
    assert sql == '"modifiedon" > from_iso8601_timestamp_nanos(\'2026-06-11T05:47:38.312726+00:00\')'
    # single quotes in the value are doubled (defensive, though our watermarks never contain them)
    assert util.after_watermark_sql("c", "a'b") == '"c" > from_iso8601_timestamp_nanos(\'a\'\'b\')'


def test_upto_watermark_sql_is_the_le_companion():
    sql = util.upto_watermark_sql("modifiedon", "2026-06-11T05:47:38.312726+00:00")
    assert sql == '"modifiedon" <= from_iso8601_timestamp_nanos(\'2026-06-11T05:47:38.312726+00:00\')'
    assert util.upto_watermark_sql("c", "a'b") == '"c" <= from_iso8601_timestamp_nanos(\'a\'\'b\')'


def test_reconstruction_sql_shape():
    sql = util.reconstruction_sql("db_dev", "contractor_aud", ["pk", "v", "modifiedon"],
                                  ["pk"], "modifiedon", WM)
    assert sql == (
        'SELECT "pk", "v", "modifiedon" FROM ('
        'SELECT "pk", "v", "modifiedon", ROW_NUMBER() OVER (PARTITION BY "pk" '
        'ORDER BY "modifiedon" DESC, "changebatchid" DESC) AS rn '
        'FROM "db_dev"."contractor_aud" '
        f'WHERE "modifiedon" <= from_iso8601_timestamp_nanos(\'{WM}\')'
        ') WHERE rn = 1'
    )
    # composite PK partitions on every key column
    composite = util.reconstruction_sql("d", "t_aud", ["a", "b"], ["a", "b"], "modifiedon", WM)
    assert 'PARTITION BY "a", "b"' in composite


def test_parse_pk_spec():
    assert util.parse_pk_spec("db.table=col1") == ("db.table", ["col1"])
    assert util.parse_pk_spec("db.table=emplid, name_type ,effdt") == (
        "db.table", ["emplid", "name_type", "effdt"])
    for bad in ("db.table", "db.table=", "table=col", "=col"):
        with pytest.raises(ValueError):
            util.parse_pk_spec(bad)


def test_bucket_ind_maps_operations():
    assert rb.bucket_ind({"D": 2, "U": 5, "I": 3}) == {
        "removed": 2, "updated": 5, "inserted": 3, "total": 10}
    # lower-case, whitespace, and unknown non-'D' indicators all fold into 'updated'
    assert rb.bucket_ind({"d": 1, " u ": 2, "X": 4}) == {
        "removed": 1, "updated": 6, "inserted": 0, "total": 7}
    assert rb.bucket_ind({}) == {"removed": 0, "updated": 0, "inserted": 0, "total": 0}


# --- model serialization ---------------------------------------------------------------------------


def test_request_roundtrip_and_chaining_from_c1():
    req = _request(("molecular_vms_beakon.contractor", WM), primary_keys=CONTRACTOR_PK)
    assert req.env_code == "dev"
    assert [w.table.qualified for w in req.watermarks] == ["molecular_vms_beakon.contractor"]
    assert req.primary_keys == CONTRACTOR_PK
    assert rb.RollbackRequest.from_dict(req.to_dict()).to_dict() == req.to_dict()


def test_result_json_roundtrip_is_identical():
    result = rb.RollbackResult(
        env_code="dev", applied=True, recorded_at="2026-07-01T00:00:00+00:00",
        rollbacks=[rb.TableRollback(
            table=wm.TableRef("domain_core_curriculum", "class"), aud_table="class_aud",
            silver_table="class", watermark_column="modifiedon",
            watermark="2026-05-26T16:06:50+00:00", primary_key=["class_nbr", "strm"],
            removed=0, updated=1, inserted=0, total=1, recon_rows=42, silver_rows_before=43,
            silver_rows_after=42, silver_snapshot_before="987", applied=True, aud_truncated=False,
            row_source="aws", recorded_at="2026-07-01T00:00:00+00:00", error=None)],
    )
    assert rb.loads(rb.dumps(result)).to_dict() == result.to_dict()


def test_pre_rewrite_cache_shape_still_parses():
    # A cache/fixture written before the Reading-2 rewrite has none of the rebuild fields.
    old = {
        "table": {"database": "d", "table": "t", "timestamp_column": "modifiedon"},
        "aud_table": "t_aud", "watermark_column": "modifiedon", "watermark": WM,
        "removed": 1, "updated": 2, "inserted": 3, "total": 6,
        "applied": False, "row_source": "cache", "recorded_at": "", "skipped_reason": None,
    }
    row = rb.TableRollback.from_dict(old)
    assert (row.silver_table, row.primary_key, row.recon_rows) == ("", None, None)
    assert (row.aud_truncated, row.error) == (False, None)


# --- dry-run vs apply ------------------------------------------------------------------------------


def test_dry_run_counts_but_never_mutates(tmp_path):
    request = _request(("molecular_vms_beakon.contractor", WM), primary_keys=CONTRACTOR_PK)
    source = FakeRollbackSource({("molecular_vms_beakon_dev", "contractor_aud"): {"D": 2, "U": 5}},
                                recon_rows=150)

    result = rb.rollback_aud(request, mode="record", cache_dir=tmp_path, source=source)

    (row,) = result.rollbacks
    assert (row.aud_table, row.silver_table) == ("contractor_aud", "contractor")
    assert (row.removed, row.updated, row.inserted, row.total) == (2, 5, 0, 7)
    assert row.recon_rows == 150                      # preview: silver's size as of the watermark
    assert row.applied is False and result.applied is False
    assert source.rebuilds == [] and source.truncated == []   # dry-run must not mutate
    assert source.counted == [("molecular_vms_beakon_dev", "contractor_aud", "modifiedon", WM)]


def test_dry_run_without_pk_still_summarizes(tmp_path):
    request = _request(("molecular_vms_beakon.contractor", WM))  # no primary_keys
    source = FakeRollbackSource({("molecular_vms_beakon_dev", "contractor_aud"): {"I": 4}})
    result = rb.rollback_aud(request, mode="record", cache_dir=tmp_path, source=source)
    (row,) = result.rollbacks
    assert row.total == 4 and row.recon_rows is None and row.error is None
    assert source.described == []                     # no PK -> no schema reads needed


def test_apply_rebuilds_silver(tmp_path):
    request = _request(("molecular_vms_beakon.contractor", WM), primary_keys=CONTRACTOR_PK)
    source = FakeRollbackSource({("molecular_vms_beakon_dev", "contractor_aud"): {"D": 2, "U": 5}},
                                recon_rows=150, silver_rows=157)

    result = rb.rollback_aud(request, mode="record", apply=True, cache_dir=tmp_path, source=source)

    (row,) = result.rollbacks
    assert row.applied is True and result.applied is True
    assert source.rebuilds == [(
        "molecular_vms_beakon_dev", "contractor", "contractor_aud",
        tuple(COLUMNS), ("beakon_record_number",), "modifiedon", WM)]
    # safety captures around the non-atomic DELETE+INSERT
    assert row.silver_rows_before == 157
    assert row.silver_snapshot_before == "1234567890"
    assert row.silver_rows_after == 150 == row.recon_rows
    assert row.error is None
    assert source.truncated == []                     # _aud untouched unless --truncate-aud


def test_apply_without_pk_is_refused_per_table(tmp_path):
    request = _request(("molecular_vms_beakon.contractor", WM))  # no primary_keys
    source = FakeRollbackSource({("molecular_vms_beakon_dev", "contractor_aud"): {"U": 7}})
    result = rb.rollback_aud(request, mode="record", apply=True, cache_dir=tmp_path, source=source)
    (row,) = result.rollbacks
    assert row.total == 7                             # still summarized
    assert row.applied is False and "no primary key" in row.error
    assert source.rebuilds == []


def test_apply_skips_rebuild_when_nothing_to_roll_back(tmp_path):
    request = _request(("molecular_vms_beakon.contractor", WM), primary_keys=CONTRACTOR_PK)
    source = FakeRollbackSource({})  # no post-watermark rows -> silver is already at the watermark

    result = rb.rollback_aud(request, mode="record", apply=True, cache_dir=tmp_path, source=source)
    assert result.rollbacks[0].total == 0
    assert result.rollbacks[0].applied is False
    assert source.rebuilds == []


def test_force_rebuilds_even_when_log_shows_nothing(tmp_path):
    # A change written straight to silver (bypassing the pipeline) never reaches _aud, so total == 0
    # although silver has drifted from the watermark state - the ASP-1616 seed scenario.
    request = _request(("molecular_vms_beakon.contractor", WM), primary_keys=CONTRACTOR_PK)
    source = FakeRollbackSource({}, recon_rows=157, silver_rows=158)  # log empty, silver drifted

    result = rb.rollback_aud(request, mode="record", apply=True, force=True,
                             cache_dir=tmp_path, source=source)
    (row,) = result.rollbacks
    assert row.total == 0 and row.applied is True
    assert (row.silver_rows_before, row.silver_rows_after) == (158, 157)
    assert row.error is None
    assert len(source.rebuilds) == 1


def test_force_requires_apply(tmp_path):
    request = _request(("molecular_vms_beakon.contractor", WM), primary_keys=CONTRACTOR_PK)
    with pytest.raises(ValueError, match="requires apply=True"):
        rb.rollback_aud(request, mode="record", force=True, cache_dir=tmp_path,
                        source=FakeRollbackSource({}))


def test_apply_requires_record_mode(tmp_path):
    request = _request(("molecular_vms_beakon.contractor", WM), primary_keys=CONTRACTOR_PK)
    with pytest.raises(ValueError, match="requires mode='record'"):
        rb.rollback_aud(request, mode="replay", apply=True, cache_dir=tmp_path)


def test_wrong_pk_column_fails_loudly_before_any_write(tmp_path):
    request = _request(("molecular_vms_beakon.contractor", WM),
                       primary_keys={"molecular_vms_beakon.contractor": ["not_a_column"]})
    source = FakeRollbackSource({("molecular_vms_beakon_dev", "contractor_aud"): {"U": 1}})
    result = rb.rollback_aud(request, mode="record", apply=True, cache_dir=tmp_path, source=source)
    (row,) = result.rollbacks
    assert "not_a_column" in row.error and row.applied is False
    assert source.rebuilds == []


def test_post_rebuild_count_mismatch_is_recorded(tmp_path):
    request = _request(("molecular_vms_beakon.contractor", WM), primary_keys=CONTRACTOR_PK)
    source = FakeRollbackSource({("molecular_vms_beakon_dev", "contractor_aud"): {"U": 1}},
                                recon_rows=150, rebuilt_rows=149)  # one row short after INSERT
    result = rb.rollback_aud(request, mode="record", apply=True, cache_dir=tmp_path, source=source)
    (row,) = result.rollbacks
    assert row.applied is True
    assert "post-rebuild verification failed" in row.error
    assert "1234567890" in row.error                  # points at the recovery snapshot


# --- the optional _aud truncate (stage 3) ----------------------------------------------------------


def test_truncate_aud_requires_apply(tmp_path):
    request = _request(("molecular_vms_beakon.contractor", WM), primary_keys=CONTRACTOR_PK)
    with pytest.raises(ValueError, match="requires apply=True"):
        rb.rollback_aud(request, mode="record", truncate_aud=True, cache_dir=tmp_path,
                        source=FakeRollbackSource({}))


def test_truncate_aud_runs_after_a_successful_rebuild(tmp_path):
    request = _request(("molecular_vms_beakon.contractor", WM), primary_keys=CONTRACTOR_PK)
    source = FakeRollbackSource({("molecular_vms_beakon_dev", "contractor_aud"): {"U": 2}})
    result = rb.rollback_aud(request, mode="record", apply=True, truncate_aud=True,
                             cache_dir=tmp_path, source=source)
    (row,) = result.rollbacks
    assert row.applied is True and row.aud_truncated is True
    assert source.truncated == [("molecular_vms_beakon_dev", "contractor_aud", "modifiedon", WM)]


def test_truncate_aud_failure_is_recorded_not_fatal(tmp_path):
    # The real _aud tables reject DELETE (redundant partition transforms) - must not kill the run.
    request = _request(("molecular_vms_beakon.contractor", WM), primary_keys=CONTRACTOR_PK)
    source = FakeRollbackSource({("molecular_vms_beakon_dev", "contractor_aud"): {"U": 2}},
                                truncate_error="INVALID_TABLE_PROPERTY: Cannot add redundant partition")
    result = rb.rollback_aud(request, mode="record", apply=True, truncate_aud=True,
                             cache_dir=tmp_path, source=source)
    (row,) = result.rollbacks
    assert row.applied is True                        # the rebuild itself succeeded
    assert row.aud_truncated is False
    assert "_aud truncate failed" in row.error and "redundant partition" in row.error


# --- per-table error isolation ---------------------------------------------------------------------


def test_one_bad_table_does_not_abort_the_batch(tmp_path):
    request = _request(
        ("molecular_vms_beakon.contractor", WM),
        ("domain_core_curriculum.class", "2026-05-26T16:06:50+00:00"),
    )
    source = FakeRollbackSource({
        ("molecular_vms_beakon_dev", "contractor_aud"): RuntimeError("Athena query FAILED: boom"),
        ("domain_core_curriculum_dev", "class_aud"): {"U": 1},
    })
    result = rb.rollback_aud(request, mode="record", cache_dir=tmp_path, source=source)
    bad, good = result.rollbacks
    assert "boom" in bad.error and bad.total == 0
    assert good.error is None and good.total == 1


# --- skip rules ------------------------------------------------------------------------------------


def test_raw_and_stg_tables_are_skipped(tmp_path):
    request = _request(
        ("molecular_vms_beakon.contractor_raw", WM),
        ("molecular_vms_beakon.contractor_stg", WM),
    )
    source = FakeRollbackSource({})
    result = rb.rollback_aud(request, mode="record", cache_dir=tmp_path, source=source)
    assert all(r.skipped for r in result.rollbacks)
    assert all("_raw/_stg" in r.skipped_reason for r in result.rollbacks)
    assert source.counted == []  # skipped tables are never queried


def test_null_watermark_is_skipped(tmp_path):
    request = _request(("domain_core_curriculum.class", None))
    source = FakeRollbackSource({})
    result = rb.rollback_aud(request, mode="record", cache_dir=tmp_path, source=source)
    (row,) = result.rollbacks
    assert row.skipped and "null watermark" in row.skipped_reason
    assert source.counted == []


# --- record -> replay ------------------------------------------------------------------------------


def test_record_then_replay_are_equal(tmp_path):
    request = _request(
        ("molecular_vms_beakon.contractor", WM),
        ("domain_core_curriculum.class", "2026-05-26T16:06:50+00:00"),
    )
    source = FakeRollbackSource({
        ("molecular_vms_beakon_dev", "contractor_aud"): {"D": 1, "U": 3, "I": 2},
        ("domain_core_curriculum_dev", "class_aud"): {"U": 1},
    })

    recorded = rb.rollback_aud(request, mode="record", cache_dir=tmp_path, source=source)
    assert util.cache_path("dev", tmp_path, prefix="rollback").exists()
    assert recorded.by_table() == {
        "molecular_vms_beakon.contractor": 6, "domain_core_curriculum.class": 1}
    assert all(r.row_source == "aws" for r in recorded.rollbacks)

    replayed = rb.rollback_aud(request, mode="replay", cache_dir=tmp_path)
    assert replayed.by_table() == recorded.by_table()
    assert all(r.row_source == "cache" for r in replayed.rollbacks)  # only provenance differs


def test_auto_mode_uses_cache_when_present(tmp_path):
    request = _request(("domain_core_curriculum.class", "2026-05-26T16:06:50+00:00"))
    source = FakeRollbackSource({("domain_core_curriculum_dev", "class_aud"): {"U": 1}})
    rb.rollback_aud(request, mode="record", cache_dir=tmp_path, source=source)

    boom = FakeRollbackSource({})  # would return {} (wrong) if touched -> proves the cache was used
    result = rb.rollback_aud(request, mode="auto", cache_dir=tmp_path, source=boom)
    assert result.by_table() == {"domain_core_curriculum.class": 1}
    assert boom.counted == []


def test_replay_missing_table_raises(tmp_path):
    seed = _request(("domain_core_curriculum.class", "2026-05-26T16:06:50+00:00"))
    source = FakeRollbackSource({("domain_core_curriculum_dev", "class_aud"): {"U": 1}})
    rb.rollback_aud(seed, mode="record", cache_dir=tmp_path, source=source)

    other = _request(("molecular_vms_beakon.contractor", WM))
    with pytest.raises(KeyError):
        rb.rollback_aud(other, mode="replay", cache_dir=tmp_path)


# --- summary ---------------------------------------------------------------------------------------


def test_summary_totals_across_tables(tmp_path):
    request = _request(
        ("molecular_vms_beakon.contractor", WM),
        ("molecular_vms_beakon.contractor_raw", WM),  # skipped
    )
    source = FakeRollbackSource({("molecular_vms_beakon_dev", "contractor_aud"): {"D": 2, "U": 5, "I": 1}})
    result = rb.rollback_aud(request, mode="record", cache_dir=tmp_path, source=source)
    assert result.summary() == {
        "tables": 1, "skipped": 1, "removed": 2, "updated": 5, "inserted": 1, "total": 8,
        "applied": 0, "errors": 0}


# --- CLI plumbing ----------------------------------------------------------------------------------


def test_cli_pk_flags_collect_into_primary_keys():
    args = rb._build_parser().parse_args(
        ["--from-watermark", "x.json",
         "--pk", "molecular_vms_beakon.contractor=beakon_record_number",
         "--pk", "molecular_sis_nustar.ps_names=emplid,name_type,effdt"])
    pks = dict(util.parse_pk_spec(s) for s in args.pk)
    assert pks == {"molecular_vms_beakon.contractor": ["beakon_record_number"],
                   "molecular_sis_nustar.ps_names": ["emplid", "name_type", "effdt"]}
    assert args.truncate_aud is False and args.apply is False


# --- end-to-end against the checked-in fixture cache, chained off the C1 fixture --------------------


def test_replay_against_fixture_chained_from_c1_fixture():
    # Chain: read the Component 1 fixture output, turn it into a Component 2 request, replay C2 cache.
    c1 = wm.discover_watermarks(
        wm.WatermarkRequest.from_specs(
            ["molecular_vms_beakon.contractor", "domain_core_curriculum.class"], env_code="dev"),
        mode="replay", cache_dir=FIXTURE_CACHE,
    )
    request = rb.RollbackRequest.from_watermark_result(c1, primary_keys=CONTRACTOR_PK)
    result = rb.rollback_aud(request, mode="replay", cache_dir=FIXTURE_CACHE)
    assert result.by_table() == {
        "molecular_vms_beakon.contractor": 10, "domain_core_curriculum.class": 1}
    assert all(r.row_source == "cache" for r in result.rollbacks)


def test_fixture_cache_is_valid_result_json():
    data = json.loads((FIXTURE_CACHE / "rollback_dev.json").read_text())
    result = rb.RollbackResult.from_dict(data)
    assert result.env_code == "dev"
    assert len(result.rollbacks) == 2
