# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The "storage bound, not asked for" line also reads the connector's own
storage columns.

Measured on an analytics deployment: the SQL system agent windowed its
queries only on an Iceberg table's partition column (a record-creation time).
Every result carried the connector's ``storage_bound`` record naming that
column, but the line and its FINAL-ANSWER REQUIREMENTS block read storage
columns only from the baked schema. An Iceberg table's partition sources are
not catalog keys, so that map was empty, the block never fired, and the final
answer named the window without the creation-time bound.

``storage_bounds.with_result_columns`` adds the columns the records name; the
baked map stays a source too. A question that names the column still gets
neither. Fully offline; synthetic names.
"""
from __future__ import annotations

import pytest

import agents.athena_tools as at
from agents import storage_bounds

from .test_storage_bound_disclosure import _P_ONLY, _mode_a, _state_after
from .test_storage_bound_refusal import _CFG, _WINDOW, _Lake

_ASKED_CREATED = "How many rows were created since 2026-01-01?"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    at.clear_schema_cache()
    at._PARTITION_COLUMNS_CACHE.clear()
    storage_bounds.reset()
    yield
    at.clear_schema_cache()
    at._PARTITION_COLUMNS_CACHE.clear()
    storage_bounds.reset()


def _run(lake, sql):
    return at.run_athena_query_exact(sql, _CFG, source_system="system-a", client=lake)


def test_a_captured_iceberg_storage_bound_marks_the_bound_with_no_baked_map():
    assert storage_bounds.storage_map_for("system-a") == {}
    answer = _mode_a("42 rows are done.", _state_after(_run(_Lake(), _P_ONLY)))
    assert "storage bound, not asked for" in answer
    assert "created in Q1 (a storage partition column of events)" in answer
    assert "----- BEGIN FINAL-ANSWER REQUIREMENTS -----" in answer
    assert "the storage bound on created that the question did not ask for" in answer


def test_a_question_naming_the_column_gets_neither_the_line_nor_the_block():
    answer = _mode_a("42 rows.", _state_after(_run(_Lake(), _P_ONLY)), question=_ASKED_CREATED)
    assert "storage bound, not asked for" not in answer
    assert "FINAL-ANSWER REQUIREMENTS" not in answer


def test_the_baked_map_stays_a_source_beside_the_captured_records():
    sql = f"{_P_ONLY} AND region = 'r1'"
    state = _state_after(_run(_Lake(), sql))      # the connector's record names created
    storage_bounds.remember("system-a", {"events": frozenset({"region"})})
    answer = _mode_a("42 rows are done.", state)
    assert "created in Q1" in answer and "region in Q1" in answer
    assert "the storage bound on created and region" in answer


def test_a_result_with_no_storage_bound_adds_nothing():
    answer = _mode_a("42 rows.", _state_after(_run(_Lake(), (
        f"SELECT COUNT(*) AS n FROM events WHERE {_WINDOW}"))))
    assert "storage bound, not asked for" not in answer
    assert "FINAL-ANSWER REQUIREMENTS" not in answer


def test_result_records_merge_by_table_name_case_insensitively():
    rows = [{"query_text": "q", "storage_bound": {"table": "lake_db.Events",
                                                  "columns": ["created"]}},
            {"query_text": "q", "storage_bound": {"table": "other", "columns": []}},
            {"query_text": "q"}]
    merged = storage_bounds.with_result_columns({"events": frozenset({"region"})}, rows)
    assert merged == {"events": frozenset({"created", "region"})}
    assert storage_bounds.with_result_columns({}, []) == {}
