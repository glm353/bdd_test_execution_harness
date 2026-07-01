"""Offline tests for Component 1 (ASP-1614). No AWS / boto3 / Okta required.

Covers:
  * model round-trip serialization (the C1/C2 contract must survive JSON)
  * cache record -> replay equality (a fake source drives 'record'; 'replay' reads it back)
  * timestamp-column auto-detection (prefers 'modifiedon')
  * end-to-end discover_watermarks against a checked-in fixture cache
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import util
import watermark as wm

FIXTURE_CACHE = Path(__file__).parent / "fixtures"


# --- fakes -----------------------------------------------------------------------------------------


class FakeSource:
    """Stands in for util.AwsWatermarkSource so the live ('record') path needs no real AWS."""

    def __init__(self, schemas: dict[str, list[tuple[str, str]]],
                 maxes: dict[tuple[str, str, str], str | None]):
        self._schemas = schemas          # {"db.table": [(col, glue_type), ...]}
        self._maxes = maxes              # {(db, table, column): max_ts}
        self.queried: list[tuple[str, str, str]] = []

    def describe_columns(self, database: str, table: str) -> list[tuple[str, str]]:
        return self._schemas[f"{database}.{table}"]

    def max_timestamp(self, database: str, table: str, column: str) -> str | None:
        self.queried.append((database, table, column))
        return self._maxes[(database, table, column)]


# --- model serialization ---------------------------------------------------------------------------


def test_table_ref_from_string_and_roundtrip():
    ref = wm.TableRef.from_string("molecular_vms_beakon.contractor")
    assert ref.database == "molecular_vms_beakon"
    assert ref.table == "contractor"
    assert ref.qualified == "molecular_vms_beakon.contractor"
    assert wm.TableRef.from_dict(ref.to_dict()) == ref


def test_result_json_roundtrip_is_identical():
    result = wm.WatermarkResult(
        env_code="dev",
        recorded_at="2026-07-01T00:00:00+00:00",
        watermarks=[
            wm.TableWatermark(
                table=wm.TableRef("domain_core_curriculum", "holiday"),
                timestamp_column="modifiedon",
                max_timestamp="2026-06-30T13:01:05",
                row_source="aws",
                recorded_at="2026-07-01T00:00:00+00:00",
            )
        ],
    )
    # dumps -> loads must reproduce the same object (guards the C1/C2 boundary).
    assert wm.loads(wm.dumps(result)).to_dict() == result.to_dict()


def test_request_roundtrip_and_from_specs():
    req = wm.WatermarkRequest.from_specs(
        ["molecular_vms_beakon.contractor", "domain_core_curriculum.holiday"], env_code="dev"
    )
    assert [t.qualified for t in req.tables] == [
        "molecular_vms_beakon.contractor", "domain_core_curriculum.holiday"
    ]
    assert wm.WatermarkRequest.from_dict(req.to_dict()).to_dict() == req.to_dict()


# --- watermark-column auto-detection ---------------------------------------------------------------


def test_pick_watermark_column_prefers_modifiedon():
    cols = [("id", "string"), ("createdon", "timestamp"), ("modifiedon", "timestamp")]
    assert util.pick_watermark_column(cols) == "modifiedon"


def test_pick_watermark_column_falls_back_to_first_timestamp():
    cols = [("id", "string"), ("createdon", "timestamp")]
    assert util.pick_watermark_column(cols) == "createdon"


def test_pick_watermark_column_none_when_no_timestamp():
    assert util.pick_watermark_column([("id", "string"), ("name", "varchar(50)")]) is None


# --- record -> replay ------------------------------------------------------------------------------


def test_record_then_replay_are_equal(tmp_path):
    request = wm.WatermarkRequest.from_specs(
        ["molecular_vms_beakon.contractor", "domain_core_curriculum.holiday"], env_code="dev"
    )
    source = FakeSource(
        schemas={
            "molecular_vms_beakon_dev.contractor": [("id", "string"), ("modifiedon", "timestamp")],
            "domain_core_curriculum_dev.holiday": [("modifiedon", "timestamp")],
        },
        maxes={
            ("molecular_vms_beakon_dev", "contractor", "modifiedon"): "2026-06-30T09:15:00",
            ("domain_core_curriculum_dev", "holiday", "modifiedon"): "2026-06-29T22:00:00",
        },
    )

    recorded = wm.discover_watermarks(request, mode="record", cache_dir=tmp_path, source=source)
    assert util.cache_path("dev", tmp_path).exists()
    assert recorded.by_table() == {
        "molecular_vms_beakon.contractor": "2026-06-30T09:15:00",
        "domain_core_curriculum.holiday": "2026-06-29T22:00:00",
    }
    assert all(w.row_source == "aws" for w in recorded.watermarks)

    replayed = wm.discover_watermarks(request, mode="replay", cache_dir=tmp_path)
    # Same timestamps + columns; only the provenance differs.
    assert replayed.by_table() == recorded.by_table()
    assert {w.timestamp_column for w in replayed.watermarks} == {"modifiedon"}


def test_auto_mode_uses_cache_when_present(tmp_path):
    request = wm.WatermarkRequest.from_specs(["domain_core_curriculum.holiday"], env_code="dev")
    source = FakeSource(
        schemas={"domain_core_curriculum_dev.holiday": [("modifiedon", "timestamp")]},
        maxes={("domain_core_curriculum_dev", "holiday", "modifiedon"): "2026-06-29T22:00:00"},
    )
    wm.discover_watermarks(request, mode="record", cache_dir=tmp_path, source=source)

    # A source that would explode if touched proves 'auto' replayed from cache instead of querying.
    boom = FakeSource(schemas={}, maxes={})
    result = wm.discover_watermarks(request, mode="auto", cache_dir=tmp_path, source=boom)
    assert result.by_table() == {"domain_core_curriculum.holiday": "2026-06-29T22:00:00"}
    assert boom.queried == []


def test_explicit_timestamp_column_skips_autodetect(tmp_path):
    ref = wm.TableRef.from_string("domain_core_curriculum.holiday", timestamp_column="createdon")
    request = wm.WatermarkRequest(tables=[ref], env_code="dev")
    source = FakeSource(
        schemas={},  # describe_columns must NOT be called when an override is given
        maxes={("domain_core_curriculum_dev", "holiday", "createdon"): "2026-01-01T00:00:00"},
    )
    result = wm.discover_watermarks(request, mode="record", cache_dir=tmp_path, source=source)
    assert result.watermarks[0].timestamp_column == "createdon"


def test_replay_missing_table_raises(tmp_path):
    seed = wm.WatermarkRequest.from_specs(["domain_core_curriculum.holiday"], env_code="dev")
    source = FakeSource(
        schemas={"domain_core_curriculum_dev.holiday": [("modifiedon", "timestamp")]},
        maxes={("domain_core_curriculum_dev", "holiday", "modifiedon"): "2026-06-29T22:00:00"},
    )
    wm.discover_watermarks(seed, mode="record", cache_dir=tmp_path, source=source)

    other = wm.WatermarkRequest.from_specs(["molecular_vms_beakon.contractor"], env_code="dev")
    with pytest.raises(KeyError):
        wm.discover_watermarks(other, mode="replay", cache_dir=tmp_path)


# --- end-to-end against the checked-in fixture cache -----------------------------------------------


def test_discover_against_fixture_cache():
    request = wm.WatermarkRequest.from_specs(
        ["molecular_vms_beakon.contractor", "domain_core_curriculum.holiday"], env_code="dev"
    )
    result = wm.discover_watermarks(request, mode="replay", cache_dir=FIXTURE_CACHE)
    assert result.by_table() == {
        "molecular_vms_beakon.contractor": "2026-06-30T09:15:00",
        "domain_core_curriculum.holiday": "2026-06-29T22:00:00",
    }
    # Fixture is an offline snapshot -> provenance is 'cache'.
    assert all(w.row_source == "cache" for w in result.watermarks)


def test_fixture_cache_is_valid_result_json():
    data = json.loads((FIXTURE_CACHE / "watermark_dev.json").read_text())
    result = wm.WatermarkResult.from_dict(data)
    assert result.env_code == "dev"
    assert len(result.watermarks) == 2
