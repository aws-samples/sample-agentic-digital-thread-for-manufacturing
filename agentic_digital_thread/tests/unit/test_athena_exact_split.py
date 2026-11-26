# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Exact COUNT / SUM / MIN / MAX past a per-query scan cap, end to end offline.

An Athena workgroup can enforce a per-query bytes-scanned cutoff the app
cannot raise. Before this, a COUNT the engine refused for size came back as
the refusal only: the system agent was told to split the window by hand and
forbidden to add the pieces, so the honest answer listed pieces with no total.
The connector now computes the exact answer from pieces over the table's
partition source column and returns ONE result.

The tests drive the real connector against ``FakeLake`` (an Iceberg-like table
in SQLite that refuses a query reading more than ``cutoff`` bytes) and pin:

1. red-on-old: the guarded system-agent tool returns the exact total of a
   refused COUNT (it returned only the refusal before);
2. differential: one query run whole equals the tiled result, for COUNT, SUM,
   MIN, MAX, with and without GROUP BY, NULL keys, all-NULL SUM, rows on every
   boundary, rows with a NULL partition value, ORDER BY + LIMIT after merge;
3. refusals: each non-decomposable shape, a failed piece, a piece over the
   group cap, the piece and byte caps, an unreadable file list or catalog, a
   non-Iceberg table: the honest error with a reason, never a partial total
   (a grouped query's LIMIT is applied once, after the merge);
4. snapshot pinning, bisection of a refused piece, the model's SQL unchanged
   apart from the one conjunct and ``FOR VERSION AS OF``;
5. disclosure: the executed-filters line names ONE query with its piece
   count and the model's own WHERE; no per-piece figure reaches the result.

Fully offline (no AWS, no network); synthetic names only.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

import pytest

import agents.athena_tools as at
import agents.system_agent as sa
from agents import executed_filters
from agents import tool_result_capture as trc

from ._athena_split_fakes import FakeLake

_COLUMNS = {"site": "varchar", "state": "varchar", "batch": "varchar", "qty": "bigint",
            "weight": "double", "created": "timestamp(6)", "completed": "timestamp(6)"}
_CFG = {"workgroup": "wg", "database": "lake_db", "region": "us-east-1",
        "exact_split_max_pieces": 40}
#: One column of one row is 10 kB in the fake; the whole table is ~6 MB per
#: column read, so any whole-table query (and the 10-day window below) is over this cutoff
#: and any one day fits.
_CUTOFF = 3_000_000
_START = datetime(2026, 9, 1)


def _rows() -> list[tuple]:
    """30 days of rows; the first row of each day sits exactly on its
    midnight boundary; some rows have a NULL site, qty or created value."""
    rows = []
    sites = ["site-a", "site-b", "site-c", None]
    states = ["DONE", "OPEN", "HOLD"]
    for day in range(30):
        for i in range(18 + day % 5):
            created = _START + timedelta(days=day, minutes=0 if i == 0 else 37 * i)
            completed = created + timedelta(hours=(i * 7) % 40)
            rows.append((sites[(day + i) % 4], states[(i + day) % 3], created.strftime("%Y-%m-%d"),
                         None if i % 11 == 5 else (i * 3 + day) % 50, (i % 7) * 0.5,
                         created.strftime("%Y-%m-%d %H:%M:%S"),
                         completed.strftime("%Y-%m-%d %H:%M:%S")))
    for i in range(9):
        rows.append(("site-b", "DONE", None, i, 1.5, None,
                     (_START + timedelta(days=i)).strftime("%Y-%m-%d %H:%M:%S")))
    return rows


@pytest.fixture(autouse=True)
def _fresh_caches():
    at.clear_schema_cache()
    at._PARTITION_COLUMNS_CACHE.clear()
    yield
    at.clear_schema_cache()
    at._PARTITION_COLUMNS_CACHE.clear()


@pytest.fixture
def lake():
    return FakeLake(columns=_COLUMNS, rows=_rows(), part_col="created", cutoff=_CUTOFF)


def _exact(lake: FakeLake, sql: str, cfg: dict | None = None, source: str = "lake-a") -> dict:
    return at.run_athena_query_exact(sql, {**_CFG, **(cfg or {})}, source_system=source,
                                     client=lake, glue_client=lake.glue)


def _refused(lake: FakeLake, sql: str, cfg: dict | None = None) -> dict:
    with pytest.raises(at.AthenaEngineScanLimitError) as info:
        _exact(lake, sql, cfg)
    payload = at._tool_error(info.value)
    assert payload["error_type"] == "scan_budget_exceeded"
    assert "rows" not in payload
    return payload["exact_split"]


def _same_rows(got: list, want: list) -> bool:
    key = lambda r: json.dumps(r)  # noqa: E731 - a sort key for rows holding None
    return sorted(got, key=key) == sorted(want, key=key)


# --------------------------------------------------------------------------- #
# 1. Red on the old code: the guarded tool answers with the exact total
# --------------------------------------------------------------------------- #
_P1 = ("SELECT COUNT(*) FROM events WHERE state = 'DONE' "
       "AND completed >= TIMESTAMP '2026-09-10 00:00:00' "
       "AND completed < TIMESTAMP '2026-09-20 00:00:00'")


def test_guarded_tool_returns_the_exact_total_of_a_refused_count(lake, monkeypatch):
    monkeypatch.setattr(at, "boto3_client_for",
                        lambda service, *_a, **_k: lake.glue if service == "glue" else lake)
    payload = sa._scoped_athena_attempt(_P1, at._resolve_config(_CFG), {}, "lake-a")
    assert payload.get("rows") == lake.reference(_P1), payload
    assert payload["exact_split"]["applied"] is True
    assert payload["exact_split"]["pieces"] > 1
    assert "partition pieces" in payload["note"]


# --------------------------------------------------------------------------- #
# 2. Differential: one query run whole == the tiled result
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("sql", [
    _P1,
    "SELECT COUNT(*), COUNT(qty), SUM(qty), MIN(completed), MAX(qty) FROM events",
    "SELECT site, COUNT(*), SUM(qty), MIN(completed), MAX(weight) FROM events GROUP BY site",
    "SELECT state, site, COUNT(1) AS n FROM events WHERE qty > 10 GROUP BY state, site",
    "SELECT SUM(qty), COUNT(*), MIN(qty), MAX(qty) FROM events WHERE state = 'NONE_SUCH'",
    "SELECT site, SUM(qty) FROM events WHERE state = 'NONE_SUCH' GROUP BY site",
    "SELECT COUNT(*) FROM events WHERE created >= TIMESTAMP '2026-09-05 00:00:00'",
])
def test_tiled_equals_whole(lake, sql):
    result = _exact(lake, sql)
    assert result["exact_split"]["pieces"] > 1
    assert _same_rows(result["rows"], lake.reference(sql))
    assert result["exact_split"]["exact"] is True


def test_order_by_and_limit_are_applied_after_the_merge(lake):
    sql = ("SELECT batch, COUNT(*) AS n, SUM(qty) FROM events GROUP BY batch "
           "ORDER BY n DESC, batch LIMIT 8")
    result = _exact(lake, sql)
    assert result["rows"] == lake.reference(sql)
    assert len(result["rows"]) == 8
    assert result["exact_split"]["pieces"] > 2


def test_nulls_ordering_after_merge(lake):
    sql = "SELECT site, COUNT(*) AS n FROM events GROUP BY site ORDER BY site NULLS FIRST"
    result = _exact(lake, sql)
    assert result["rows"][0][0] is None
    assert result["rows"] == lake.reference(sql)


def test_float_sum_is_added_but_not_called_exact(lake):
    sql = "SELECT SUM(weight) FROM events"
    result = _exact(lake, sql)
    assert float(result["rows"][0][0]) == float(lake.reference(sql)[0][0])
    assert result["exact_split"]["exact"] is False
    assert "floating-point" in result["note"]


def test_every_row_counted_once_across_boundaries_and_null_partition(lake):
    result = _exact(lake, "SELECT COUNT(*) FROM events")
    assert result["rows"] == [[str(len(_rows()))]]
    labels = result["exact_split"]["ranges"]
    # Open-ended at both ends; the lowest piece also holds the NULL partition.
    assert labels[0].startswith("created before ") and labels[0].endswith(" or null")
    assert labels[-1].startswith("created from ")


# --------------------------------------------------------------------------- #
# 3. Refusals: the honest error with a reason, never a partial total
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("sql", [
    "SELECT AVG(qty) FROM events",
    "SELECT COUNT(DISTINCT site) FROM events",
    "SELECT site, COUNT(*) FROM events GROUP BY site HAVING COUNT(*) > 3",
    "SELECT e.site, COUNT(*) FROM events e JOIN sites s ON e.site = s.id GROUP BY e.site",
    "SELECT site, COUNT(*) OVER (PARTITION BY site) FROM events",
    "SELECT COUNT(*) * 2 FROM events",
    "SELECT * FROM events",
    "SELECT COUNT(*) FROM (SELECT site FROM events)",
])
def test_non_decomposable_shapes_keep_the_refusal(lake, sql):
    split = _refused(lake, sql)
    assert split["applied"] is False and split["reason"] == "not_decomposable"
    assert len(lake.data_queries()) == 1  # only the refused query ran


def test_a_failed_piece_gives_no_total(lake):
    lake.fail_piece_containing = "FOR VERSION AS OF"
    assert _refused(lake, _P1)["reason"] == "piece_failed"


def test_a_grouped_limit_is_applied_after_the_merge_not_in_each_piece(lake):
    # Each piece reads every group it holds; a piece's own top 2 would not add up.
    sql = "SELECT state, COUNT(*) FROM events GROUP BY state LIMIT 2"
    result = _exact(lake, sql)
    counts = dict(map(tuple, lake.reference("SELECT state, COUNT(*) FROM events GROUP BY state")))
    assert len(result["rows"]) == 2 and all(counts[s] == n for s, n in result["rows"])
    assert result["exact_split"]["groups"] == 3


def test_a_piece_over_the_group_cap_gives_no_total(lake):
    split = _refused(lake, "SELECT batch, COUNT(*) FROM events GROUP BY batch",
                     {"exact_split_max_groups": 1})
    assert split["reason"] == "group_cap"


def test_piece_cap(lake):
    # No plan of one piece exists (the whole query was refused).
    assert _refused(lake, _P1, {"exact_split_max_pieces": 1})["reason"] == "piece_cap"


def test_refused_pieces_count_toward_the_piece_cap():
    # Estimates far too low: the first pieces are refused and re-planned, and
    # with room for only six pieces in all the split stops without a total.
    lake = FakeLake(columns=_COLUMNS, rows=_rows(), part_col="created", cutoff=1_000_000,
                    estimate_factor=0.3)
    split = _refused(lake, _P1, {"exact_split_max_pieces": 6})
    assert split["reason"] == "piece_cap"
    pieces = [q for q in lake.data_queries() if "FOR VERSION AS OF" in q]
    assert len(pieces) <= 6 and any(lake.scan_bytes(p) > lake.cutoff for p in pieces)


def test_byte_cap(lake):
    assert _refused(lake, _P1, {"exact_split_max_total_bytes": 300_000})["reason"] == "byte_cap"


def test_wall_time_cap(lake):
    # One second leaves no time for a piece once the plan is read.
    assert _refused(lake, _P1, {"exact_split_max_seconds": 1})["reason"] == "wall_time"


def test_unreadable_file_list(lake):
    lake.files_fail = True
    assert _refused(lake, _P1)["reason"] == "files_unreadable"
    assert len(lake.data_queries()) == 1


def test_unreadable_catalog(lake):
    lake.glue_fails = True
    assert _refused(lake, _P1)["reason"] == "catalog_unreadable"


def test_non_iceberg_table(lake):
    lake.iceberg = False
    assert _refused(lake, _P1)["reason"] == "not_iceberg"


def test_a_piece_still_refused_and_unsplittable_gives_no_total():
    rows = [("site-a", "DONE", "b", 1, 1.0, "2026-09-02 01:00:00", "2026-09-02 02:00:00")] * 300
    rows += [("site-a", "DONE", "b", 1, 1.0, "2026-09-03 01:00:00", "2026-09-03 02:00:00")] * 20
    lake = FakeLake(columns=_COLUMNS, rows=rows, part_col="created", cutoff=250_000)
    # One day partition alone is over the cutoff and no statistic splits it.
    assert _refused(lake, "SELECT COUNT(*) FROM events")["reason"] in {
        "unsplittable", "piece_refused", "piece_cap"}


def test_an_unexpected_split_fault_keeps_the_honest_refusal(lake, monkeypatch):
    from agents import athena_exact_split
    monkeypatch.setattr(athena_exact_split, "_layout",
                        lambda *a, **k: (_ for _ in ()).throw(KeyError("boom")))
    assert _refused(lake, _P1)["reason"] == "split_failed"


def test_refusal_text_avoids_retry_markers(lake):
    from agents.orchestrator import _AUTH_TOOL_ERROR_MARKERS, _TRANSIENT_TOOL_ERROR_MARKERS
    lake.files_fail = True
    detail = _refused(lake, _P1)["detail"].lower()
    assert not any(m in detail for m in (*_TRANSIENT_TOOL_ERROR_MARKERS, *_AUTH_TOOL_ERROR_MARKERS))


def test_a_query_under_the_limit_runs_once_unchanged(lake):
    sql = "SELECT COUNT(*) FROM events WHERE \"created\" >= TIMESTAMP '2026-09-29 00:00:00'"
    result = _exact(lake, sql)
    assert "exact_split" not in result and lake.data_queries() == [sql + " LIMIT 500"]


# --------------------------------------------------------------------------- #
# 4. Bisection, snapshot pinning, the model's SQL left as written
# --------------------------------------------------------------------------- #
def test_an_underestimated_piece_is_halved_and_still_exact():
    # Column sizes report a third of what the engine reads and the whole
    # query is far over the cutoff, so the refusal's lower bound is weak: the
    # first pieces are refused, re-planned at the raised calibration, and
    # the total is still exact.
    lake = FakeLake(columns=_COLUMNS, rows=_rows(), part_col="created", cutoff=1_000_000,
                    estimate_factor=0.3)
    result = _exact(lake, _P1)
    assert result["exact_split"]["refused_pieces"] > 0
    assert result["rows"] == lake.reference(_P1)


def test_every_piece_reads_the_pinned_snapshot(lake):
    result = _exact(lake, _P1)
    split = result["exact_split"]
    assert split["snapshot_id"] == 4242 and split["atomic"] is True and split["snapshot_pinned"]
    pieces = lake.data_queries()[1:]
    assert len(pieces) == split["pieces"]
    assert all(p.startswith("SELECT COUNT(*) FROM events FOR VERSION AS OF 4242 WHERE ") for p in pieces)


def _snapshots_answer(lake, monkeypatch, outcome):
    real = lake._run_meta

    def answer(sql):
        return outcome if "$snapshots" in sql else real(sql)
    monkeypatch.setattr(lake, "_run_meta", answer)


def test_an_unreadable_snapshot_list_gives_no_total_and_runs_no_piece(lake, monkeypatch):
    # Unpinned pieces read a table that commits while they run: rows can be
    # missed or counted twice, so no snapshot means no split at all.
    _snapshots_answer(lake, monkeypatch, {"state": "FAILED", "reason": "ACCESS_DENIED: no", "scanned": 0})
    split = _refused(lake, _P1)
    assert split["reason"] == "snapshot_unreadable"
    assert lake.data_queries() == [_P1 + " LIMIT 500"]
    assert not any("$files" in q for q in lake.started)          # refused before planning


@pytest.mark.parametrize("rows", [[], [[None]], [["not-a-snapshot"]], [["12.5"]]],
                         ids=["no snapshot", "null id", "text id", "fraction id"])
def test_a_snapshot_id_that_cannot_be_pinned_gives_no_total(lake, monkeypatch, rows):
    from ._athena_split_fakes import _ok
    _snapshots_answer(lake, monkeypatch, _ok(["snapshot_id"], ["bigint"], rows))
    split = _refused(lake, _P1)
    assert split["reason"] == "snapshot_unpinnable"
    assert lake.data_queries() == [_P1 + " LIMIT 500"]


_BOX_TERM = re.compile(r'"(created|completed)"\s*(<|>=)\s*TIMESTAMP \'[^\']+\'|"(created|completed)" IS NULL')


def test_piece_sql_is_the_model_sql_plus_one_conjunct(lake):
    _exact(lake, _P1)
    where = _P1.split(" WHERE ", 1)[1]
    pattern = re.compile(
        r"^SELECT COUNT\(\*\) FROM events FOR VERSION AS OF 4242 WHERE (?P<pred>.+) AND \("
        + re.escape(where) + r"\) LIMIT 500$")
    pieces = lake.data_queries()[1:]
    preds = [pattern.match(p).group("pred") for p in pieces]
    # The injected predicate is only comparisons on the split columns.
    for pred in preds:
        assert not re.sub(r"[()\s]|AND|OR", "", _BOX_TERM.sub("", pred)), pred


# --------------------------------------------------------------------------- #
# 5. Disclosure: one logical query, the model's WHERE, the piece count
# --------------------------------------------------------------------------- #
def _engine_line(payload: dict) -> str:
    state: dict = {}
    # The tool result exactly as the capture hook sees it (strands shape).
    result = {"toolUseId": "t1", "status": "success", "content": [{"text": json.dumps(payload)}]}
    executed = trc._trusted_executed_query(result, "", 200_000)
    trc.record_sidecar_statement(state, executed_query=executed, metadata={}, result=result)
    return executed_filters.executed_filters_line(trc.collect_sidecar_statements(state))


def test_executed_filters_line_shows_one_query_with_its_piece_count(lake):
    payload = _exact(lake, _P1)
    line = _engine_line(payload)
    pieces = payload["exact_split"]["pieces"]
    assert f"Q1 (1 row, computed from {pieces} partition pieces): WHERE state = 'DONE'" in line
    assert "Q2" not in line and '"created"' not in line
    assert payload["_adt_executed_query"] == _P1 + " LIMIT 500"


def test_no_per_piece_figure_reaches_the_result(lake):
    payload = _exact(lake, "SELECT COUNT(*) FROM events")
    total = int(payload["rows"][0][0])
    partials = [int(lake.reference(piece)[0][0]) for piece in lake.data_queries()[1:]]
    assert sum(partials) == total and len(partials) > 1
    # The rows carry the merged total only; the lineage lists per piece only
    # what each piece is (range, query id), how many rows it returned and what
    # it scanned, never a piece's own COUNT, SUM, MIN or MAX.
    split = payload["exact_split"]
    per_piece = {k for k, v in split.items() if isinstance(v, list)} - {"split_columns"}
    assert per_piece == {"ranges", "query_ids", "row_counts", "scanned_bytes"}
    assert split["row_counts"] == [1] * split["pieces"]
    assert payload["rows"] == [[str(total)]]
    assert not set(re.findall(r"\d+", payload["note"])) - {str(split["pieces"]), "4242"}
