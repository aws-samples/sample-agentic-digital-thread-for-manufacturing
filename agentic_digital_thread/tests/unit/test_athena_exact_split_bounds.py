# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A grouped split stays inside the result bounds, and says what it shows.

A grouped query's pieces read every group they hold. Two things followed:

* those rows were paged with no cell, byte or column bound: twenty cells of
  16,385 bytes came back whole, over the 16,384-byte cell cap and the
  200,000-byte result cap, and up to 20,001 rows of each of up to 16 pieces
  were kept and merged in memory before the model-facing bound ran;
* ``groups_shown`` was counted before that bound, so a result whose rows the
  bound cut still said every group was shown.

Now one budget is shared by every page of every piece: a cell over the cell
cap, more columns than the column cap, or more bytes in all than
``GROUP_BYTES_BUDGET`` stops the fetch and the split refuses with
``group_bytes`` before any merge. The group and piece knobs have hard
ceilings, a caller's ``CapsLimit`` can lower the group cap, and
``groups_shown`` and the note come from the bounded result itself.

Offline (``FakeLake``, small fakes), synthetic names only.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

import pytest

import agents.athena_exact_merge as merge
import agents.athena_exact_split as sp
import agents.athena_tools as at
from agents.response_integrity_query import (MAX_QUERY_CELL_BYTES, MAX_QUERY_COLUMNS,
                                             strict_json_dumps)

from ._athena_split_fakes import FakeLake

_COLUMNS = {"site": "varchar", "batch": "varchar", "items": "varchar",
            "created": "timestamp(6)", "completed": "timestamp(6)"}
_CFG = {"workgroup": "wg", "database": "lake_db", "region": "us-east-1",
        "exact_split_max_pieces": 40}
_START = datetime(2026, 9, 1)
_BATCHES = "SELECT batch, COUNT(*) AS n FROM events GROUP BY batch ORDER BY n DESC, batch"


def _rows(batch=lambda day, i: f"b-{day:02d}-{i:02d}") -> list[tuple]:
    rows = []
    for day in range(30):
        for i in range(20):
            created = _START + timedelta(days=day, minutes=41 * i)
            rows.append(("site-a", batch(day, i), "[]", created.strftime("%Y-%m-%d %H:%M:%S"),
                         (created + timedelta(hours=i % 30)).strftime("%Y-%m-%d %H:%M:%S")))
    return rows


@pytest.fixture(autouse=True)
def _fresh_caches():
    at.clear_schema_cache()
    at._PARTITION_COLUMNS_CACHE.clear()
    yield
    at.clear_schema_cache()
    at._PARTITION_COLUMNS_CACHE.clear()


def _exact(lake: FakeLake, sql: str, cfg: dict | None = None) -> dict:
    return at.run_athena_query_exact(sql, {**_CFG, **(cfg or {})}, source_system="lake-a",
                                     client=lake, glue_client=lake.glue)


def _refused(lake: FakeLake, sql: str, cfg: dict | None = None) -> dict:
    with pytest.raises(at.AthenaEngineScanLimitError) as info:
        _exact(lake, sql, cfg)
    return at._tool_error(info.value)["exact_split"]


class _Pages:
    """``get_query_results`` for one finished query: ``columns`` and ``rows``
    in pages of ``page`` rows (the header row first, as Athena sends it)."""

    def __init__(self, columns: list[str], rows: list[list], page: int = 5):
        self.columns, self.rows, self.page, self.calls = columns, rows, page, 0

    def get_query_results(self, QueryExecutionId, MaxResults=1000, NextToken=None):
        self.calls += 1
        start = int(NextToken or 0)
        body = self.rows[start:start + self.page]
        data = [{"Data": [{"VarCharValue": c} for c in row]} for row in body]
        if start == 0:
            data = [{"Data": [{"VarCharValue": c} for c in self.columns]}] + data
        out = {"ResultSet": {"ResultSetMetadata": {"ColumnInfo": [
            {"Name": c, "Type": "varchar"} for c in self.columns]}, "Rows": data}}
        if start + self.page < len(self.rows):
            out["NextToken"] = str(start + self.page)
        return out


# --------------------------------------------------------------------------- #
# 1. One budget across every page of every piece
# --------------------------------------------------------------------------- #
def test_twenty_cells_over_the_cell_cap_are_refused_while_paging():
    # The reviewer's repro: twenty 16,385-byte cells used to come back whole.
    pages = _Pages(["k", "n"], [["x" * 16_385 + str(i), "1"] for i in range(20)])
    with pytest.raises(sp.ExactSplitRefused) as info:
        at._collect_whole_rows(pages, "q1", 21, [], sp._RowBudget(sp.GROUP_BYTES_BUDGET))
    assert info.value.reason == "group_bytes"
    assert str(MAX_QUERY_CELL_BYTES) in info.value.detail
    assert pages.calls == 1             # stopped on the first page


def test_more_columns_than_the_column_cap_are_refused_while_paging():
    names = [f"c{i}" for i in range(MAX_QUERY_COLUMNS + 1)]
    pages = _Pages(names, [["1"] * len(names)])
    with pytest.raises(sp.ExactSplitRefused) as info:
        at._collect_whole_rows(pages, "q1", 10, [], sp._RowBudget(sp.GROUP_BYTES_BUDGET))
    assert info.value.reason == "group_bytes"
    assert str(MAX_QUERY_COLUMNS) in info.value.detail


def test_a_budget_is_shared_and_counts_compact_json_bytes():
    budget = sp._RowBudget(100)
    rows = [["a" * 20, "1"], ["b" * 20, "22"]]
    pages = _Pages(["k", "n"], rows)
    got = at._collect_whole_rows(pages, "q1", 10, [], budget)
    assert got["rows"] == rows and got["truncated"] is False
    assert budget.used == sum(len(strict_json_dumps(r)) for r in rows)
    with pytest.raises(sp.ExactSplitRefused):
        at._collect_whole_rows(_Pages(["k", "n"], rows), "q2", 10, [], budget)


def test_a_cell_over_the_cell_cap_refuses_the_split_before_the_merge(monkeypatch):
    long_key = lambda day, i: ("k" * 16_385 + f"{day:02d}") if i == 0 else f"b-{day:02d}-{i:02d}"  # noqa: E731
    lake = FakeLake(columns=_COLUMNS, rows=_rows(long_key), part_col="created", cutoff=3_000_000)
    merged: list = []
    real = merge.merge_pieces
    monkeypatch.setattr(merge, "merge_pieces", lambda *a, **k: merged.append(1) or real(*a, **k))
    split = _refused(lake, _BATCHES)
    assert split["reason"] == "group_bytes"
    assert merged == []


def test_the_byte_budget_is_cumulative_across_pieces(monkeypatch):
    lake = FakeLake(columns=_COLUMNS, rows=_rows(), part_col="created", cutoff=3_000_000)
    # How the rows fall into the pieces that returned them.
    done = [lake._queries[q] for q in _exact(lake, _BATCHES)["exact_split"]["query_ids"]]
    per_piece = [sum(len(strict_json_dumps(r)) for r in lake.reference(p.rsplit(" LIMIT ", 1)[0]))
                 for p in done]
    assert len(per_piece) > 1
    budget = max(per_piece) + 1         # every piece fits alone; all of them do not
    assert budget < sum(per_piece)
    monkeypatch.setattr(sp, "GROUP_BYTES_BUDGET", budget)
    lake = FakeLake(columns=_COLUMNS, rows=_rows(), part_col="created", cutoff=3_000_000)
    split = _refused(lake, _BATCHES)
    assert split["reason"] == "group_bytes"


def test_a_breakdown_within_the_budget_is_unchanged():
    lake = FakeLake(columns=_COLUMNS, rows=_rows(), part_col="created", cutoff=3_000_000)
    result = _exact(lake, _BATCHES)
    assert result["rows"] == lake.reference(_BATCHES)[:500]


# --------------------------------------------------------------------------- #
# 2. Hard ceilings on the knobs; a caller can lower the group cap
# --------------------------------------------------------------------------- #
def test_the_group_and_piece_knobs_are_clamped_to_their_ceilings(caplog):
    cfg = at._resolve_config({"workgroup": "wg", "database": "db",
                              "exact_split_max_groups": 10 ** 9, "exact_split_max_pieces": 10 ** 6})
    with caplog.at_level(logging.WARNING, logger=sp.__name__):
        caps = sp.resolve_caps(cfg)
    assert caps.max_groups == sp.MAX_GROUPS_CEILING
    assert caps.max_pieces == sp.MAX_PIECES_CEILING
    assert "exact_split_max_groups" in caplog.text and "exact_split_max_pieces" in caplog.text
    assert sp.DEFAULT_MAX_GROUPS <= sp.MAX_GROUPS_CEILING
    assert sp.DEFAULT_MAX_PIECES <= sp.MAX_PIECES_CEILING


def test_a_caps_limit_lowers_the_group_cap_and_never_raises_it():
    with sp.limited(sp.CapsLimit(max_groups=7)):
        assert sp.resolve_caps({}).max_groups == 7
    with sp.limited(sp.CapsLimit(max_groups=7)):
        assert sp.resolve_caps({"exact_split_max_groups": 5}).max_groups == 5
    assert sp.resolve_caps({}).max_groups == sp.DEFAULT_MAX_GROUPS


def test_a_lowered_group_cap_refuses_a_split_over_it():
    lake = FakeLake(columns=_COLUMNS, rows=_rows(), part_col="created", cutoff=3_000_000)
    with sp.limited(sp.CapsLimit(max_groups=3)):
        assert _refused(lake, _BATCHES)["reason"] == "group_cap"


# --------------------------------------------------------------------------- #
# 3. groups_shown and the note come from the bounded result
# --------------------------------------------------------------------------- #
def _lineage(groups: int) -> dict:
    return {"applied": True, "pieces": 2, "table": "events", "split_columns": ["created"],
            "snapshot_id": 4242, "exact": True, "total_scanned_bytes": 10,
            "groups": groups, "groups_shown": groups, "group_cap": 20_000, "ordered": True}


def test_a_first_key_over_the_cell_cap_is_shown_as_cut():
    # The reviewer's repro: the bound kept no row, the lineage said both were shown.
    merged = merge.Merged(["k", "n"], ["varchar", "bigint"],
                          [["x" * (MAX_QUERY_CELL_BYTES + 1), "1"], ["b", "1"]],
                          exact=True, cut=False, groups=2)
    result = sp._result(merged, _lineage(2), "", "SELECT k, COUNT(*) FROM events GROUP BY k", None)
    assert result["row_count"] == 0 and result["truncated"] is True
    assert result["exact_split"]["groups"] == 2
    assert result["exact_split"]["groups_shown"] == 0
    assert "The query has 2 groups in all; none of them are shown" in result["note"]
    assert "size limits" in result["note"]


def test_rows_the_byte_bound_cuts_are_not_counted_as_shown():
    # 120 groups of 3,000-byte keys: every cell fits, the whole result does not.
    key = lambda day, i: f"{day:02d}-{i % 4}-" + "k" * 3_000  # noqa: E731
    lake = FakeLake(columns=_COLUMNS, rows=_rows(key), part_col="created", cutoff=3_000_000)
    result = _exact(lake, _BATCHES)
    split = result["exact_split"]
    assert split["groups"] == 120
    assert result["partial_reason"] == "byte_limit"
    assert 0 < result["row_count"] < 120
    assert split["groups_shown"] == result["row_count"]
    shown = result["row_count"]
    assert f"The query has 120 groups in all; the first {shown} by its ORDER BY are shown" in result["note"]
    assert "did not fit the result's size limits" in result["note"]
    assert result["rows"] == lake.reference(_BATCHES)[:shown]


def test_columns_the_bound_drops_are_named_in_the_note():
    long_name = "c" * 300
    merged = merge.Merged(["k", long_name], ["varchar", "bigint"], [["a", "1"], ["b", "2"]],
                          exact=True, cut=False, groups=2)
    result = sp._result(merged, _lineage(2), "", "SELECT k, COUNT(*) FROM events GROUP BY k", None)
    assert result["row_count"] == 2
    assert result["exact_split"]["groups_shown"] == 2
    assert result["exact_split"]["columns_shown"] == 1
    assert "Only the first 1 of its 2 columns are shown" in result["note"]


def test_a_row_cap_cut_keeps_its_wording():
    merged = merge.Merged(["k", "n"], ["varchar", "bigint"], [[f"b{i}", "1"] for i in range(500)],
                          exact=True, cut=True, groups=600)
    result = sp._result(merged, _lineage(600), "", "SELECT k, COUNT(*) FROM events GROUP BY k", None)
    assert result["exact_split"]["groups_shown"] == 500
    assert result["note"].endswith("The query has 600 groups in all; the first 500 by its ORDER BY "
                                   "are shown.")
    assert json.dumps(result)  # serializable
