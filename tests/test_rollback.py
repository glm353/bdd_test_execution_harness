"""Offline tests for Component 2 (ASP-1615). No AWS / boto3 / Okta required.

Covers:
  * model round-trip serialization (the C1->C2 contract must survive JSON)
  * chaining: a Component 1 WatermarkResult -> a Component 2 RollbackRequest
  * ind -> removed/updated/inserted bucketing and the _aud name / SQL helpers
  * dry-run vs apply (apply issues the DELETE; dry-run never mutates)
  * _raw/_stg and null-watermark tables are skipped with a reason
  * cache record -> replay equality (a fake source drives 'record'; 'replay' reads it back)
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


# --- fakes -----------------------------------------------------------------------------------------


class FakeRollbackSource:
    """Stands in for util.AwsWatermarkSource: canned ind counts + a record of DELETE calls."""

    def __init__(self, counts: dict[tuple[str, str], dict[str, int]]):
        self._counts = counts          # {(database, aud_table): {ind: n}}
        self.counted: list[tuple[str, str, str, str]] = []
        self.deleted: list[tuple[str, str, str, str]] = []

    def count_changes_since(self, database, aud_table, column, watermark):
        self.counted.append((database, aud_table, column, watermark))
        return self._counts.get((database, aud_table), {})

    def delete_changes_since(self, database, aud_table, column, watermark):
        self.deleted.append((database, aud_table, column, watermark))


def _request(*specs_and_watermarks: tuple[str, str | None], env_code: str = "dev") -> rb.RollbackRequest:
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
    return rb.RollbackRequest.from_watermark_result(c1)


# --- helpers: names, SQL, bucketing ----------------------------------------------------------------


def test_aud_table_name_is_idempotent():
    assert util.aud_table_name("contractor") == "contractor_aud"
    assert util.aud_table_name("contractor_aud") == "contractor_aud"


def test_after_watermark_sql_shape_and_escaping():
    sql = util.after_watermark_sql("modifiedon", "2026-06-11T05:47:38+00:00")
    assert sql == '"modifiedon" > from_iso8601_timestamp(\'2026-06-11T05:47:38+00:00\')'
    # single quotes in the value are doubled (defensive, though our watermarks never contain them)
    assert util.after_watermark_sql("c", "a'b") == '"c" > from_iso8601_timestamp(\'a\'\'b\')'


def test_bucket_ind_maps_operations():
    assert rb.bucket_ind({"D": 2, "U": 5, "I": 3}) == {
        "removed": 2, "updated": 5, "inserted": 3, "total": 10}
    # lower-case, whitespace, and unknown non-'D' indicators all fold into 'updated'
    assert rb.bucket_ind({"d": 1, " u ": 2, "X": 4}) == {
        "removed": 1, "updated": 6, "inserted": 0, "total": 7}
    assert rb.bucket_ind({}) == {"removed": 0, "updated": 0, "inserted": 0, "total": 0}


# --- model serialization ---------------------------------------------------------------------------


def test_request_roundtrip_and_chaining_from_c1():
    req = _request(("molecular_vms_beakon.contractor", "2026-06-11T05:47:38+00:00"))
    assert req.env_code == "dev"
    assert [w.table.qualified for w in req.watermarks] == ["molecular_vms_beakon.contractor"]
    assert rb.RollbackRequest.from_dict(req.to_dict()).to_dict() == req.to_dict()


def test_result_json_roundtrip_is_identical():
    result = rb.RollbackResult(
        env_code="dev", applied=False, recorded_at="2026-07-01T00:00:00+00:00",
        rollbacks=[rb.TableRollback(
            table=wm.TableRef("domain_core_curriculum", "class"), aud_table="class_aud",
            watermark_column="modifiedon", watermark="2026-05-26T16:06:50+00:00",
            removed=0, updated=1, inserted=0, total=1, applied=False, row_source="aws",
            recorded_at="2026-07-01T00:00:00+00:00")],
    )
    assert rb.loads(rb.dumps(result)).to_dict() == result.to_dict()


# --- dry-run vs apply ------------------------------------------------------------------------------


def test_dry_run_counts_but_never_deletes(tmp_path):
    request = _request(("molecular_vms_beakon.contractor", "2026-06-11T05:47:38+00:00"))
    source = FakeRollbackSource({("molecular_vms_beakon_dev", "contractor_aud"): {"D": 2, "U": 5}})

    result = rb.rollback_aud(request, mode="record", cache_dir=tmp_path, source=source)

    (row,) = result.rollbacks
    assert row.aud_table == "contractor_aud"
    assert (row.removed, row.updated, row.inserted, row.total) == (2, 5, 0, 7)
    assert row.applied is False and result.applied is False
    assert source.deleted == []                       # dry-run must not mutate
    assert source.counted == [(
        "molecular_vms_beakon_dev", "contractor_aud", "modifiedon", "2026-06-11T05:47:38+00:00")]


def test_apply_executes_delete(tmp_path):
    request = _request(("molecular_vms_beakon.contractor", "2026-06-11T05:47:38+00:00"))
    source = FakeRollbackSource({("molecular_vms_beakon_dev", "contractor_aud"): {"D": 2, "U": 5}})

    result = rb.rollback_aud(request, mode="record", apply=True, cache_dir=tmp_path, source=source)

    (row,) = result.rollbacks
    assert row.applied is True and result.applied is True
    assert source.deleted == [(
        "molecular_vms_beakon_dev", "contractor_aud", "modifiedon", "2026-06-11T05:47:38+00:00")]


def test_apply_skips_delete_when_nothing_to_roll_back(tmp_path):
    request = _request(("molecular_vms_beakon.contractor", "2026-06-11T05:47:38+00:00"))
    source = FakeRollbackSource({})  # no post-watermark rows

    result = rb.rollback_aud(request, mode="record", apply=True, cache_dir=tmp_path, source=source)
    assert result.rollbacks[0].total == 0
    assert result.rollbacks[0].applied is False
    assert source.deleted == []


def test_apply_requires_record_mode(tmp_path):
    request = _request(("molecular_vms_beakon.contractor", "2026-06-11T05:47:38+00:00"))
    with pytest.raises(ValueError, match="requires mode='record'"):
        rb.rollback_aud(request, mode="replay", apply=True, cache_dir=tmp_path)


# --- skip rules ------------------------------------------------------------------------------------


def test_raw_and_stg_tables_are_skipped(tmp_path):
    request = _request(
        ("molecular_vms_beakon.contractor_raw", "2026-06-11T05:47:38+00:00"),
        ("molecular_vms_beakon.contractor_stg", "2026-06-11T05:47:38+00:00"),
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
        ("molecular_vms_beakon.contractor", "2026-06-11T05:47:38+00:00"),
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

    other = _request(("molecular_vms_beakon.contractor", "2026-06-11T05:47:38+00:00"))
    with pytest.raises(KeyError):
        rb.rollback_aud(other, mode="replay", cache_dir=tmp_path)


# --- summary ---------------------------------------------------------------------------------------


def test_summary_totals_across_tables(tmp_path):
    request = _request(
        ("molecular_vms_beakon.contractor", "2026-06-11T05:47:38+00:00"),
        ("molecular_vms_beakon.contractor_raw", "2026-06-11T05:47:38+00:00"),  # skipped
    )
    source = FakeRollbackSource({("molecular_vms_beakon_dev", "contractor_aud"): {"D": 2, "U": 5, "I": 1}})
    result = rb.rollback_aud(request, mode="record", cache_dir=tmp_path, source=source)
    assert result.summary() == {
        "tables": 1, "skipped": 1, "removed": 2, "updated": 5, "inserted": 1, "total": 8}


# --- end-to-end against the checked-in fixture cache, chained off the C1 fixture --------------------


def test_replay_against_fixture_chained_from_c1_fixture():
    # Chain: read the Component 1 fixture output, turn it into a Component 2 request, replay C2 cache.
    c1 = wm.discover_watermarks(
        wm.WatermarkRequest.from_specs(
            ["molecular_vms_beakon.contractor", "domain_core_curriculum.class"], env_code="dev"),
        mode="replay", cache_dir=FIXTURE_CACHE,
    )
    request = rb.RollbackRequest.from_watermark_result(c1)
    result = rb.rollback_aud(request, mode="replay", cache_dir=FIXTURE_CACHE)
    assert result.by_table() == {
        "molecular_vms_beakon.contractor": 10, "domain_core_curriculum.class": 1}
    assert all(r.row_source == "cache" for r in result.rollbacks)


def test_fixture_cache_is_valid_result_json():
    data = json.loads((FIXTURE_CACHE / "rollback_dev.json").read_text())
    result = rb.RollbackResult.from_dict(data)
    assert result.env_code == "dev"
    assert len(result.rollbacks) == 2
