# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The exact split keeps a table's row-exclusion predicate in every piece.

A query that carries the source owner's exclusion (``row_exclusion``) and is
over the workgroup's per-query scan cutoff is computed exactly from pieces
(``athena_exact_split``). Each piece is the model's SQL with one injected box
conjunct, so the predicate must survive in every piece, in the single split and
in the OR-of-states combine, or the total would count rows the owner says are
not records. Proven on the real connector against ``FakeLake`` (SQLite): every
piece the engine ran is read by ``row_exclusion.decide`` as carrying the
predicate, and the total equals the whole query run at once (and differs from
the total without the predicate, so the predicate mattered).

Fully offline; synthetic names.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

import agents.athena_tools as at
from agents import row_exclusion as rx

from ._athena_split_fakes import FakeLake

_COLUMNS = {"pipeline": "varchar", "run_ref": "varchar", "step_kind": "varchar",
            "state": "varchar", "done_at": "timestamp(6)", "failed_at": "timestamp(6)",
            "created": "timestamp(6)"}
_CFG = {"workgroup": "wg", "database": "lake_db", "region": "us-east-1",
        "exact_split_max_pieces": 40}
_CUTOFF = 1_500_000
_START = datetime(2026, 9, 1)
_KINDS = ("ASSEMBLE", "PACK", "PREPARE", "PREPARE_BUFFER")
_FACT = rx.exclusion_from("shadow copies", [
    {"column": "pipeline", "op": "not_empty"},
    {"column": "run_ref", "op": "not_empty"},
    {"column": "step_kind", "op": "not_in", "values": ["ASSEMBLE", "PACK"]}])
_PRED = rx.predicate_sql(_FACT)


def _rows() -> list[tuple]:
    """20 days of rows in two states; pipeline and run_ref are each empty, NULL
    or set on a cycle, so some rows match the exclusion and some do not."""
    rows = []
    for day in range(20):
        for i in range(12):
            created = _START + timedelta(days=day, minutes=37 * i)
            stamp = created.strftime("%Y-%m-%d %H:%M:%S")
            state = "FAILED" if (day + i) % 2 else "DONE"
            pipeline = (None, "", "LINE_A", "LINE_B")[i % 4]
            run_ref = ("", f"r{day}-{i}", None)[(day + i) % 3]
            rows.append((pipeline, run_ref, _KINDS[(day * 3 + i) % 4], state,
                         stamp if state == "DONE" else None,
                         stamp if state == "FAILED" else None, stamp))
    return rows


@pytest.fixture(autouse=True)
def _fresh_caches():
    at.clear_schema_cache()
    at._PARTITION_COLUMNS_CACHE.clear()
    yield
    at.clear_schema_cache()
    at._PARTITION_COLUMNS_CACHE.clear()


def _lake() -> FakeLake:
    return FakeLake(columns=_COLUMNS, rows=_rows(), part_col="created", cutoff=_CUTOFF)


def _exact(lake: FakeLake, sql: str) -> dict:
    return at.run_athena_query_exact(sql, dict(_CFG), source_system="lake-a",
                                     client=lake, glue_client=lake.glue)


def _pieces(lake: FakeLake) -> list[str]:
    return [q for q in lake.data_queries() if "FOR VERSION AS OF" in q]


def _same_rows(got, want) -> bool:
    key = lambda r: json.dumps(r)  # noqa: E731 - a sort key for rows holding None
    return sorted(got, key=key) == sorted(want, key=key)


def _window(column: str) -> str:
    return (f"{column} >= TIMESTAMP '2026-09-03 00:00:00' "
            f"AND {column} < TIMESTAMP '2026-09-15 00:00:00'")


def test_every_piece_of_a_single_split_keeps_the_predicate():
    lake = _lake()
    sql = (f"SELECT step_kind, COUNT(*) AS n FROM events WHERE state = 'FAILED' "
           f"AND {_window('failed_at')} AND {_PRED} GROUP BY step_kind")
    assert rx.decide(sql, "events", _FACT).applied is True
    result = _exact(lake, sql)
    assert result["exact_split"]["applied"] is True
    pieces = _pieces(lake)
    assert len(pieces) > 1
    for piece in pieces:
        found = rx.decide(piece, "events", _FACT)
        assert found is not None and found.applied is True, piece
    assert _same_rows(result["rows"], lake.reference(sql))
    without = sql.replace(f" AND {_PRED}", "")
    assert not _same_rows(lake.reference(sql), lake.reference(without))


def test_every_piece_of_an_or_of_states_combine_keeps_the_predicate():
    lake = _lake()
    branches = (f"(state = 'DONE' AND {_window('done_at')}) OR "
                f"(state = 'FAILED' AND {_window('failed_at')})")
    sql = (f"SELECT state, COUNT(*) AS n FROM events WHERE {_PRED} AND ({branches}) "
           "GROUP BY state")
    result = _exact(lake, sql)
    assert result["exact_split"].get("branches") == 2, result["exact_split"]
    pieces = _pieces(lake)
    assert len(pieces) >= 2
    for piece in pieces:
        found = rx.decide(piece, "events", _FACT)
        assert found is not None and found.applied is True, piece
    assert _same_rows(result["rows"], lake.reference(sql))
