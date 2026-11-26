# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A percentile of a duration, computed from mergeable per-piece summaries.

A measured over-cap query asks for an approximate percentile of a duration,
grouped by one column, windowed on one time column:

    SELECT key, approx_percentile(date_diff('second', a, b), ARRAY[0.9, 0.99])
    FROM t WHERE <window on b> GROUP BY key

``approx_percentile`` cannot be added up from pieces, so the exact-COUNT split
refuses it. But Athena engine 3's ``qdigest_agg`` builds a quantile digest per
group that ``merge`` combines and ``value_at_quantile`` / ``values_at_quantiles``
read (verified live on the data account's workgroup, ``s40/engine-check*.log``). A qdigest
also survives as a serialized result cell between separate queries, so each
piece can return one digest per group and a later query (or the merge step)
combines them.

So the connector plans pieces exactly as for COUNT, each piece returning a
qdigest per group, merges per group, and reads the requested quantiles. The
result is APPROXIMATE, the same kind as ``approx_percentile`` -- the answer and
the record say "approximate percentile from N pieces", never "exact".

Driven against ``FakeLake`` (SQLite behind boto3-shaped clients, with qdigest
stand-in functions that keep the exact small-data quantile). Offline, synthetic
names only.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

import agents.athena_tools as at

from ._athena_split_fakes import FakeLake

_COLUMNS = {"line": "varchar", "started": "timestamp(6)", "completed": "timestamp(6)",
            "created": "timestamp(6)"}
_CFG = {"workgroup": "wg", "database": "lake_db", "region": "us-east-1",
        "exact_split_max_pieces": 40}
_CUTOFF = 900_000
_START = datetime(2026, 9, 1)
_LINES = ("L1", "L2", "L3")


def _rows() -> list[tuple]:
    """20 days; each row has a start and a completion, a duration that varies
    by line and index, on one of three lines."""
    rows = []
    for day in range(20):
        for i in range(12):
            created = _START + timedelta(days=day, minutes=37 * i)
            line = _LINES[(day + i) % 3]
            dur = 60 * ((i * 7 + day * 3) % 50 + 1)  # 60..3000 seconds
            completed = created + timedelta(seconds=dur)
            rows.append((line, created.strftime("%Y-%m-%d %H:%M:%S"),
                         completed.strftime("%Y-%m-%d %H:%M:%S"),
                         created.strftime("%Y-%m-%d %H:%M:%S")))
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


@pytest.fixture
def lake():
    return _lake()


def _exact(lake: FakeLake, sql: str, cfg: dict | None = None) -> dict:
    return at.run_athena_query_exact(sql, {**_CFG, **(cfg or {})}, source_system="lake-a",
                                     client=lake, glue_client=lake.glue)


def _refused(lake: FakeLake, sql: str, cfg: dict | None = None) -> dict:
    with pytest.raises(at.AthenaEngineScanLimitError) as info:
        _exact(lake, sql, cfg)
    return at._tool_error(info.value)["exact_split"]


def _pieces(lake: FakeLake) -> list[str]:
    return [q for q in lake.data_queries() if "FOR VERSION AS OF" in q]


_DUR = "date_diff('second', started, completed)"
_WINDOW = ("completed >= TIMESTAMP '2026-09-04 00:00:00' "
           "AND completed < TIMESTAMP '2026-09-14 00:00:00'")
_PCTL = (f"SELECT line, approx_percentile({_DUR}, ARRAY[0.9, 0.99]) AS p "
         f"FROM events WHERE {_WINDOW} GROUP BY line ORDER BY line")
_PCTL_SCALAR = (f"SELECT approx_percentile({_DUR}, 0.9) AS p "
                f"FROM events WHERE {_WINDOW}")


# --------------------------------------------------------------------------- #
# The percentile is computed from mergeable summaries
# --------------------------------------------------------------------------- #
def test_a_grouped_percentile_is_computed_from_pieces(lake):
    result = _exact(lake, _PCTL)
    # Same groups and (approximate) quantiles as the whole query would give.
    assert [r[0] for r in result["rows"]] == sorted({l for l in _LINES})
    assert result["rows"] == lake.reference(_PCTL)
    split = result["exact_split"]
    assert split["applied"] is True and split["pieces"] > 1
    assert split["approximate"] is True


def test_the_result_says_approximate_from_n_pieces(lake):
    result = _exact(lake, _PCTL)
    note = result["note"].lower()
    assert "approximate" in note and "percentile" in note
    # It may say "not exact"; it must never claim the result IS exact.
    assert "is exact" not in note and "exact total" not in note
    assert str(result["exact_split"]["pieces"]) in result["note"]


def test_a_scalar_percentile_is_computed_from_pieces(lake):
    result = _exact(lake, _PCTL_SCALAR)
    assert result["rows"] == lake.reference(_PCTL_SCALAR)
    assert result["exact_split"]["approximate"] is True


def test_each_piece_returns_a_digest_not_approx_percentile(lake):
    _exact(lake, _PCTL)
    pieces = _pieces(lake)
    assert pieces
    # The pieces build a mergeable digest; they do not call approx_percentile
    # (which cannot be combined across pieces).
    assert all("qdigest_agg" in p for p in pieces)
    assert not any("approx_percentile" in p for p in pieces)


def test_values_at_quantiles_array_is_preserved(lake):
    result = _exact(lake, _PCTL)
    # Each group's value is the two-element array the query asked for.
    for row in result["rows"]:
        assert row[1].startswith("[") and row[1].endswith("]")
        assert len(row[1].strip("[]").split(",")) == 2


# --------------------------------------------------------------------------- #
# The existing guarantees still hold
# --------------------------------------------------------------------------- #
def test_one_snapshot_across_the_percentile_pieces(lake):
    _exact(lake, _PCTL)
    snaps = {p.split("FOR VERSION AS OF ")[1].split()[0].rstrip(",") for p in _pieces(lake)}
    assert snaps == {str(lake.snapshot)}


def test_a_failed_piece_gives_no_percentile():
    lake = _lake()
    lake.fail_piece_containing = "FOR VERSION AS OF"
    split = _refused(lake, _PCTL)
    assert split["applied"] is False
    assert split["reason"] in {"piece_failed", "piece_refused", "unsplittable"}


def test_avg_and_other_aggregates_stay_refused(lake):
    for agg in ("avg(date_diff('second', started, completed))",
                "stddev(date_diff('second', started, completed))"):
        sql = f"SELECT line, {agg} FROM events WHERE {_WINDOW} GROUP BY line"
        assert _refused(lake, sql)["reason"] in {"not_decomposable"}


def test_the_percentile_result_is_one_logical_result(lake):
    result = _exact(lake, _PCTL)
    assert "do not add" in result["note"].lower()


# --------------------------------------------------------------------------- #
# A COUNT beside the percentile (the real dry-run shape, query 33b4efdd):
# COUNT merges by arithmetic, the percentile by its digest, in ONE result.
# --------------------------------------------------------------------------- #
_MIXED = (f"SELECT line, COUNT(*) AS n, approx_percentile({_DUR}, ARRAY[0.9, 0.99]) AS p "
          f"FROM events WHERE {_WINDOW} GROUP BY line ORDER BY n DESC, line")


def test_a_count_beside_the_percentile_is_computed(lake):
    result = _exact(lake, _MIXED)
    assert result["rows"] == lake.reference(_MIXED)
    split = result["exact_split"]
    assert split["applied"] is True and split["approximate"] is True
    assert split["pieces"] > 1


def test_the_count_and_percentile_columns_are_both_present(lake):
    result = _exact(lake, _MIXED)
    # Each row is [line, count, [p90, p99]].
    for row in result["rows"]:
        assert str(row[1]).isdigit()
        assert row[2].startswith("[") and row[2].endswith("]")


def test_a_weighted_percentile_stays_refused(lake):
    # The 3-argument (weighted) approx_percentile is not read.
    sql = (f"SELECT line, approx_percentile({_DUR}, 1, 0.9) AS p "
           f"FROM events WHERE {_WINDOW} GROUP BY line")
    assert _refused(lake, sql)["reason"] == "not_decomposable"


def test_two_percentiles_stay_refused(lake):
    sql = (f"SELECT approx_percentile({_DUR}, 0.9) AS p9, approx_percentile({_DUR}, 0.5) AS p5 "
           f"FROM events WHERE {_WINDOW}")
    assert _refused(lake, sql)["reason"] == "not_decomposable"
