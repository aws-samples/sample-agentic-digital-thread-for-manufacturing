# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Integer surrogate keys never reach Confirm Mappings with a measure tag.

Live onboards of a maintenance schema proposed two integer row-id columns
as a quality measure, despite the map prompt forbidding it. The map job now
strips non-identifier tags from integer ``id``/``*_id`` columns before concept
selection, so they arrive as ordinary unmapped rows. Synthetic data only.
"""
from __future__ import annotations

import json

import pytest

import agents.onboard as onboard
from agents import surrogate_keys as sk

_MEASURE = "measure-concept-a"
_ENTITY = "entity-concept-b"


def _fake_mapper(prompt: str, timeout: int = 90, model=None) -> str:
    return json.dumps([
        {"field": "records.record_id", "table": "records",
         "mappings": [{"concept": _MEASURE, "role": "measure", "confidence": 0.9}]},
        {"field": "records.owner_id", "table": "records",
         "mappings": [{"concept": _ENTITY, "role": "identifier", "confidence": 0.9},
                      {"concept": _MEASURE, "role": "measure", "confidence": 0.8}]},
        {"field": "records.external_id", "table": "records",
         "mappings": [{"concept": _ENTITY, "role": "identifier", "confidence": 0.9}]},
        {"field": "records.score", "table": "records",
         "mappings": [{"concept": _MEASURE, "role": "measure", "confidence": 0.9}]},
    ])


def _tables():
    return [{"name": "records", "relation_kind": "base_table", "schema_revision": "a" * 32,
             "row_count": 10, "sample_values": {},
             "columns": [{"name": "record_id", "data_type": "integer"},
                         {"name": "owner_id", "data_type": "bigint"},
                         {"name": "external_id", "data_type": "text"},
                         {"name": "score", "data_type": "integer"}]}]


def _run(monkeypatch):
    monkeypatch.setattr(onboard, "bedrock_call", _fake_mapper)
    monkeypatch.setattr(onboard, "get_all_concepts", lambda: [_MEASURE, _ENTITY])
    monkeypatch.setattr(onboard.catalog, "get", lambda sid: {"connection_type": "PostgreSQL"})
    onboard._map_jobs["skjob"] = {"status": "running"}
    onboard._run_map_job("skjob", "some-system", _tables())
    job = onboard._map_jobs.pop("skjob")
    assert job["status"] == "done", job
    return {m["field"]: m for m in job["result"]["mappings"]}


def test_map_job_clears_measure_tags_on_integer_id_columns(monkeypatch):
    rows = _run(monkeypatch)
    assert rows["records.record_id"]["concept"] == "needs_review"
    assert rows["records.record_id"]["surrogate_key_removed"] == [_MEASURE]
    # An identifier proposal on an integer id survives for the reviewer.
    assert rows["records.owner_id"]["concept"] == _ENTITY
    assert [c["concept"] for c in rows["records.owner_id"]["concepts"]] == [_ENTITY]
    # Non-integer ids and non-id integers are untouched.
    assert rows["records.external_id"]["concept"] == _ENTITY
    assert rows["records.score"]["concept"] == _MEASURE
    assert "surrogate_key_removed" not in rows["records.score"]


def test_is_surrogate_key_needs_id_name_and_integer_type():
    assert sk.is_surrogate_key("id", "integer")
    assert sk.is_surrogate_key("row_id", "BIGINT")
    assert sk.is_surrogate_key("row_id", "numeric(10,0)")
    assert not sk.is_surrogate_key("row_id", "character varying")
    assert not sk.is_surrogate_key("valid", "integer")      # 'id' suffix without underscore
    assert not sk.is_surrogate_key("quantity", "integer")
    # Unknown type falls back to the samples.
    assert sk.is_surrogate_key("row_id", "", ["1", "22", 303])
    assert not sk.is_surrogate_key("row_id", "unknown", ["A-1", "22"])
    assert not sk.is_surrogate_key("row_id", "", [])


def test_helper_leaves_undescribed_columns_and_pure_identifier_rows_alone():
    rows = [{"field": "other.row_id", "table": "other",
             "mappings": [{"concept": _MEASURE, "role": "measure"}]},
            {"field": "records.record_id", "table": "records",
             "mappings": [{"concept": _ENTITY, "role": "identifier"}]}]
    assert sk.drop_non_identifier_tags_on_surrogate_keys(rows, _tables()) == 0
    assert rows[0]["mappings"] and rows[1]["mappings"]


def _fake_overlap_upgrade(system_id, mappings, **_kw):
    """Stand-in for the value-overlap pass: small integers 1..N in a row-id
    column coincide with a peer's small-integer population, and the pass
    upgrades the row the way ``_apply_value_overlap_upgrade`` does."""
    for m in mappings:
        if m["field"] in ("records.record_id", "records.owner_id", "records.score"):
            m.update(concept=_MEASURE, confidence=0.79, suggestion_basis="value_overlap",
                     mappings=[{"concept": _MEASURE, "role": "identifier", "confidence": 0.79}],
                     concepts=[{"concept": _MEASURE, "confidence": 0.79}],
                     candidates=[{"concept": _MEASURE, "basis": "value_overlap"}])
    return mappings


def test_value_overlap_never_retags_an_integer_row_id(monkeypatch):
    # Live 2026-09-25: work_order_log.log_id came back
    # 'scrap-waste 79% VALUE MATCH' and asset_id columns 'bill-of-materials'
    # because the value-overlap pass runs AFTER the surrogate filter.
    import agents.concept_suggest as cs
    monkeypatch.setattr(cs, "suggest_concepts_by_value_overlap", _fake_overlap_upgrade)
    rows = _run(monkeypatch)
    assert rows["records.record_id"]["concept"] == "needs_review"
    assert rows["records.record_id"].get("suggestion_basis") != "value_overlap"
    assert _MEASURE in rows["records.record_id"]["surrogate_key_removed"]
    # The MODEL's identifier proposal survives for the steward to decide; only
    # the coincidental value-overlap upgrade is withdrawn.
    assert rows["records.owner_id"].get("suggestion_basis") != "value_overlap"
    # A non-id integer keeps whatever the evidence pass decided.
    assert rows["records.score"]["suggestion_basis"] == "value_overlap"


# --------------------------------------------------------------------------- #
# temporal columns are never identifiers
# --------------------------------------------------------------------------- #

def _fake_mapper_dates(prompt: str, timeout: int = 90, model=None) -> str:
    return json.dumps([
        {"field": "records.installed_on", "table": "records",
         "mappings": [{"concept": _ENTITY, "role": "identifier", "confidence": 0.85}]},
        {"field": "records.logged_at", "table": "records",
         "mappings": [{"concept": _ENTITY, "role": "identifier", "confidence": 0.7},
                      {"concept": _MEASURE, "role": "measure", "confidence": 0.6}]},
        {"field": "records.external_id", "table": "records",
         "mappings": [{"concept": _ENTITY, "role": "identifier", "confidence": 0.9}]},
    ])


def _tables_dates():
    return [{"name": "records", "relation_kind": "base_table", "schema_revision": "b" * 32,
             "row_count": 10,
             "sample_values": {"logged_at": ["2026-02-10T06:00:00", "2026-02-10T11:23:00"]},
             "columns": [{"name": "installed_on", "data_type": "date"},
                         {"name": "logged_at", "data_type": "unknown"},
                         {"name": "external_id", "data_type": "text"}]}]


def test_map_job_clears_identifier_tags_on_temporal_columns(monkeypatch):
    """Live 2026-09-26 (event 21): the proposer tagged a DATE column
    (``install_date``/``installed_date``) as the equipment identifier on two
    maintenance schemas, and the equipment bridge then wrote a ValueMapping
    whose from_value was ``2022-06-15``. A point in time is never the
    identifier another system shares; the tag is stripped before review."""
    monkeypatch.setattr(onboard, "bedrock_call", _fake_mapper_dates)
    monkeypatch.setattr(onboard, "get_all_concepts", lambda: [_MEASURE, _ENTITY])
    monkeypatch.setattr(onboard.catalog, "get", lambda sid: {"connection_type": "PostgreSQL"})
    onboard._map_jobs["tmjob"] = {"status": "running"}
    onboard._run_map_job("tmjob", "some-system", _tables_dates())
    job = onboard._map_jobs.pop("tmjob")
    assert job["status"] == "done", job
    rows = {m["field"]: m for m in job["result"]["mappings"]}
    assert rows["records.installed_on"]["concept"] == "needs_review"
    assert rows["records.installed_on"]["temporal_identifier_removed"] == [_ENTITY]
    # Type unknown, but every sample is a timestamp: same treatment; the
    # non-identifier tag on it survives for the reviewer.
    assert rows["records.logged_at"]["concept"] == _MEASURE
    assert rows["records.logged_at"]["temporal_identifier_removed"] == [_ENTITY]
    # A text column keeps its identifier.
    assert rows["records.external_id"]["concept"] == _ENTITY
    assert "temporal_identifier_removed" not in rows["records.external_id"]


def test_is_temporal_column_by_type_or_samples():
    assert sk.is_temporal_column("date")
    assert sk.is_temporal_column("timestamp without time zone")
    assert sk.is_temporal_column("DATETIME2")
    assert sk.is_temporal_column("time")
    assert not sk.is_temporal_column("character varying")
    assert not sk.is_temporal_column("integer")
    assert sk.is_temporal_column("", ["2022-06-15", "2023-01-02"])
    assert sk.is_temporal_column("unknown", ["2026-02-10T06:00:00Z"])
    assert not sk.is_temporal_column("", ["1001-CMX-441"])
    assert not sk.is_temporal_column("", ["20220615"])   # a bare number is not a date
    assert not sk.is_temporal_column("", [])


def test_identifier_named_concept_in_any_role_is_stripped_from_a_date_column():
    """Live 2026-10-03 (two workshop accounts): ``install_date`` reached the
    graph tagged ``equipment-id`` with role ``correlate``, which the identifier-
    role strip let through; the equipment registry then read the date column
    as the asset tag. A concept whose id names an identifier (``*-id``) is an
    identifier whatever role the proposer wrote; a measure or time concept in
    a correlate role on the same column still survives for the reviewer."""
    mappings = [
        {"field": "assets.install_date", "table": "assets",
         "mappings": [{"concept": "equipment-id", "role": "correlate", "confidence": 0.8},
                      {"concept": _MEASURE, "role": "correlate", "confidence": 0.5}]},
        {"field": "assets.asset_num", "table": "assets",
         "mappings": [{"concept": "equipment-id", "role": "correlate", "confidence": 0.9}]},
    ]
    tables = [{"name": "assets", "columns": [{"name": "install_date", "data_type": "date"},
                                              {"name": "asset_num", "data_type": "text"}]}]
    assert sk.drop_identifier_tags_on_temporal_columns(mappings, tables) == 1
    assert [r["concept"] for r in mappings[0]["mappings"]] == [_MEASURE]
    assert mappings[0]["temporal_identifier_removed"] == ["equipment-id"]
    assert [r["concept"] for r in mappings[1]["mappings"]] == ["equipment-id"]


# --------------------------------------------------------------------------- #
# a declared type that DESCRIBES dates (REST/MCP connectors)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("data_type", [
    "string (record number, the record's identity)",
    "string (plant code)",
    "integer (days until the update)",
    "string (updated by)",
    "text", "character varying", "string",
])
def test_a_described_type_that_names_no_date_format_is_not_temporal(data_type):
    assert not sk.is_temporal_column(data_type)


def test_a_described_date_column_loses_its_identifier_tag_and_an_id_column_keeps_it():
    mappings = [
        {"field": "Record.created", "table": "Record",
         "mappings": [{"concept": _ENTITY, "role": "identifier", "confidence": 0.8},
                      {"concept": _MEASURE, "role": "measure", "confidence": 0.4}]},
        {"field": "Record.record_no", "table": "Record",
         "mappings": [{"concept": _ENTITY, "role": "identifier", "confidence": 0.9}]},
    ]
    tables = [{"name": "Record", "columns": [
        {"name": "created", "data_type": "string (ISO-8601 date-time)"},
        {"name": "record_no", "data_type": "string (record number, the record's identity)"}]}]
    assert sk.drop_untrustworthy_tags(mappings, tables) == 1
    assert [r["concept"] for r in mappings[0]["mappings"]] == [_MEASURE]
    assert mappings[0]["temporal_identifier_removed"] == [_ENTITY]
    assert [r["concept"] for r in mappings[1]["mappings"]] == [_ENTITY]
    assert "temporal_identifier_removed" not in mappings[1]


