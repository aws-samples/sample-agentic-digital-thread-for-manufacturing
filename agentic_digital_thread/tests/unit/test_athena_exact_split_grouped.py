# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A grouped COUNT past the scan limit gets every group, or says what it shows.

Measured in an answer eval: asked which identifier formats a field holds, the
system agent's complete breakdown (a COUNT per value shape, over the elements
of a JSON list) never landed on a large table. The engine refused the whole
query for scan size, and the exact split refused it too, for two reasons:

* it read ``CROSS JOIN UNNEST`` as a join, so a breakdown over list elements
  could not be split at all;
* every piece kept the query's own LIMIT (or the 500-row cap), and a piece
  holding more groups than that was refused, so a breakdown with many groups
  had no exact result however it was split.

The answer then came from a capped listing and a mid-sized family was missing.

Now a grouped query's pieces read every group (up to ``exact_split_max_groups``
each; past it the split refuses with ``group_cap``), the merge adds the
per-group counts (each row is in exactly one piece, so they add exactly) and
applies the query's ORDER BY and LIMIT once, and the result says how many
groups there are and which ones it shows. One ``CROSS JOIN UNNEST`` of a row
expression is admitted: a row's elements stay in that row's piece.

Driven against ``FakeLake`` (SQLite behind boto3-shaped clients). Offline,
synthetic names only.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

import pytest

import agents.athena_tools as at
import agents.system_agent as sa

from ._athena_split_fakes import FakeLake

_COLUMNS = {"site": "varchar", "batch": "varchar", "items": "varchar",
            "created": "timestamp(6)", "completed": "timestamp(6)"}
_CFG = {"workgroup": "wg", "database": "lake_db", "region": "us-east-1",
        "exact_split_max_pieces": 40}
#: Any whole-table query is over this cutoff; any one day fits.
_CUTOFF = 3_000_000
_START = datetime(2026, 9, 1)
_EXPAND = "CROSS JOIN UNNEST(CAST(json_parse(items) AS ARRAY(JSON))) AS u(el)"
_SHAPE = "regexp_replace(json_extract_scalar(el, '$.id'), '[0-9]', 'N')"
#: The element-shape breakdown the IDENTIFIER FORMAT rule asks for.
_FORMATS = (f"SELECT {_SHAPE} AS shape, COUNT(*) AS n, MIN(json_extract_scalar(el, '$.id')) "
            f"AS example FROM events {_EXPAND} WHERE site = 'site-a' GROUP BY 1 "
            "ORDER BY n DESC, shape")


def _items(day: int, i: int) -> str:
    """A frequent family on every row, a mid-sized one on every fifth row,
    and a rare one on one row in nine days."""
    out = [{"id": f"PR-{(i % 7) + 1:02d}", "unit": "printer"}]
    if i % 5 == 0:
        out.append({"id": f"STN_B_{i % 4:02d}", "unit": "station"})
    if i == 3 and day % 9 == 0:
        out.append({"id": f"RT_A_{day:02d}", "unit": "station"})
    return json.dumps(out)


def _rows() -> list[tuple]:
    rows = []
    for day in range(30):
        for i in range(20):
            created = _START + timedelta(days=day, minutes=41 * i)
            rows.append(("site-a" if i % 4 else "site-b", f"b-{day:02d}-{i:02d}", _items(day, i),
                         created.strftime("%Y-%m-%d %H:%M:%S"),
                         (created + timedelta(hours=i % 30)).strftime("%Y-%m-%d %H:%M:%S")))
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


def _exact(lake: FakeLake, sql: str, cfg: dict | None = None) -> dict:
    return at.run_athena_query_exact(sql, {**_CFG, **(cfg or {})}, source_system="lake-a",
                                     client=lake, glue_client=lake.glue)


def _refused(lake: FakeLake, sql: str, cfg: dict | None = None) -> dict:
    with pytest.raises(at.AthenaEngineScanLimitError) as info:
        _exact(lake, sql, cfg)
    return at._tool_error(info.value)["exact_split"]


def _pieces(lake: FakeLake) -> list[str]:
    return [q for q in lake.data_queries() if "FOR VERSION AS OF" in q]


# --------------------------------------------------------------------------- #
# A breakdown over the elements of a list column
# --------------------------------------------------------------------------- #
def test_an_element_shape_breakdown_is_computed_exactly(lake):
    result = _exact(lake, _FORMATS)
    assert result["exact_split"]["applied"] is True and result["exact_split"]["pieces"] > 1
    assert result["rows"] == lake.reference(_FORMATS)
    # Every family is there with its exact count, the rare one included.
    assert [r[0] for r in result["rows"]] == ["PR-NN", "STN_B_NN", "RT_A_NN"]


def test_the_guarded_tool_returns_the_breakdown(lake, monkeypatch):
    monkeypatch.setattr(at, "boto3_client_for",
                        lambda service, *_a, **_k: lake.glue if service == "glue" else lake)
    payload = sa._scoped_athena_attempt(_FORMATS, at._resolve_config(_CFG), {}, "lake-a")
    assert payload.get("rows") == lake.reference(_FORMATS), payload


# ``FROM t, UNNEST(...)`` is the same list expansion as ``t CROSS JOIN
# UNNEST(...)`` in Trino (each row's list, in that row). The scan guard reads
# both; the split now admits both, and runs the model's own form in each piece.
_COMMA_FORMATS = _FORMATS.replace("FROM events CROSS JOIN UNNEST", "FROM events, UNNEST")


def _same_data_lake() -> FakeLake:
    return FakeLake(columns=_COLUMNS, rows=_rows(), part_col="created", cutoff=_CUTOFF)


def test_a_comma_expansion_is_split_exactly_like_cross_join():
    assert _COMMA_FORMATS != _FORMATS
    cross_lake, comma_lake = _same_data_lake(), _same_data_lake()
    cross, comma = _exact(cross_lake, _FORMATS), _exact(comma_lake, _COMMA_FORMATS)
    assert comma["exact_split"]["applied"] is True and comma["exact_split"]["pieces"] > 1
    assert comma["rows"] == cross["rows"] == cross_lake.reference(_FORMATS)
    assert [r[0] for r in comma["rows"]] == ["PR-NN", "STN_B_NN", "RT_A_NN"]
    # The same pieces: the same boxes, each the model's own text with the one
    # predicate and the snapshot; only the expansion's spelling differs.
    for key in ("pieces", "ranges", "split_columns", "groups"):
        assert comma["exact_split"][key] == cross["exact_split"][key], key
    assert sorted(p.replace("events FOR VERSION AS OF 4242, UNNEST",
                            "events FOR VERSION AS OF 4242 CROSS JOIN UNNEST")
                  for p in _pieces(comma_lake)) == sorted(_pieces(cross_lake))


@pytest.mark.parametrize("extra", ["", " AND created >= TIMESTAMP '2026-09-05 00:00:00'"])
def test_a_comma_expansion_with_an_alias_gives_the_same_total(extra):
    plain = ("SELECT COUNT(*) AS n, COUNT(el) AS elements FROM events {sep} "
             "UNNEST(CAST(json_parse(items) AS ARRAY(JSON))) AS u(el) WHERE site = 'site-a'" + extra)
    cross_lake, comma_lake = _same_data_lake(), _same_data_lake()
    cross = _exact(cross_lake, plain.format(sep="e CROSS JOIN"))
    comma = _exact(comma_lake, plain.format(sep="e,"))
    assert comma["exact_split"]["pieces"] > 1
    assert comma["rows"] == cross["rows"] == cross_lake.reference(plain.format(sep="e CROSS JOIN"))
    assert comma["exact_split"]["ranges"] == cross["exact_split"]["ranges"]
    # A second expansion, or an ordinality column, is refused in this form too.
    for refused in (_COMMA_FORMATS.replace(" WHERE", ", UNNEST(ARRAY[1, 2]) AS v(x) WHERE"),
                    _COMMA_FORMATS.replace("AS u(el)", "WITH ORDINALITY AS u(el, k)"),
                    "SELECT COUNT(*) FROM events, UNNEST(CAST(json_parse(items) AS ARRAY(JSON))) AS u"):
        assert _refused(_same_data_lake(), refused)["reason"] == "not_decomposable", refused


def test_the_guarded_tool_returns_the_comma_breakdown(lake, monkeypatch):
    monkeypatch.setattr(at, "boto3_client_for",
                        lambda service, *_a, **_k: lake.glue if service == "glue" else lake)
    payload = sa._scoped_athena_attempt(_COMMA_FORMATS, at._resolve_config(_CFG), {}, "lake-a")
    assert payload.get("rows") == lake.reference(_FORMATS), payload
    assert payload["exact_split"]["pieces"] > 1


def test_an_expanded_column_named_like_a_split_column_is_refused(lake):
    sql = ("SELECT created, COUNT(*) FROM events CROSS JOIN UNNEST(CAST(json_parse(items) "
           "AS ARRAY(JSON))) AS u(created) GROUP BY 1")
    split = _refused(lake, sql)
    assert split["reason"] == "not_decomposable"
    assert _pieces(lake) == []


@pytest.mark.parametrize("sql", [
    # A second expansion, an ordinality column, and an expansion of an aggregate.
    f"SELECT COUNT(*) FROM events {_EXPAND} CROSS JOIN UNNEST(ARRAY[1, 2]) AS v(x)",
    "SELECT COUNT(*) FROM events CROSS JOIN UNNEST(CAST(json_parse(items) AS ARRAY(JSON))) "
    "WITH ORDINALITY AS u(el, k)",
    "SELECT COUNT(*) FROM events CROSS JOIN UNNEST(ARRAY[COUNT(*)]) AS u(el)",
    # An expansion with no column name.
    "SELECT COUNT(*) FROM events CROSS JOIN UNNEST(CAST(json_parse(items) AS ARRAY(JSON))) AS u",
])
def test_other_expansions_keep_the_refusal(lake, sql):
    assert _refused(lake, sql)["reason"] == "not_decomposable"


# --------------------------------------------------------------------------- #
# More groups than a piece's row cap
# --------------------------------------------------------------------------- #
_BATCHES = "SELECT batch, COUNT(*) AS n FROM events GROUP BY batch ORDER BY n DESC, batch"


def test_more_groups_than_the_row_cap_are_merged_and_the_result_says_how_many(lake):
    result = _exact(lake, _BATCHES)
    whole = lake.reference(_BATCHES)
    assert len(whole) == 600
    assert result["rows"] == whole[:500]
    split = result["exact_split"]
    assert split["groups"] == 600 and split["groups_shown"] == 500
    assert "600 groups" in result["note"] and "first 500 by its ORDER BY" in result["note"]
    assert result["capped_at_limit"] is True


def test_the_query_limit_is_applied_once_after_the_merge(lake):
    sql = _BATCHES + " LIMIT 12"
    result = _exact(lake, sql)
    assert result["rows"] == lake.reference(sql)
    assert result["exact_split"]["groups"] == 600
    assert "first 12 by its ORDER BY" in result["note"]
    # Every piece read all of its groups: none carries the query's LIMIT 12.
    assert _pieces(lake) and not any(p.endswith("LIMIT 12") for p in _pieces(lake))


def test_a_limit_without_order_by_says_the_rows_shown_are_in_no_set_order(lake):
    sql = "SELECT batch, COUNT(*) AS n FROM events GROUP BY batch LIMIT 5"
    result = _exact(lake, sql)
    counts = dict((r[0], r[1]) for r in lake.reference(
        "SELECT batch, COUNT(*) AS n FROM events GROUP BY batch"))
    assert len(result["rows"]) == 5 and all(counts[b] == n for b, n in result["rows"])
    assert "no set order" in result["note"]


def test_a_piece_holding_more_groups_than_the_row_cap_reads_them_all():
    # Three day pieces of 600 groups each: past the 500-row cap a piece used
    # to be refused (its rows were cut), so the breakdown had no exact result.
    rows = [("site-a", f"b-{n:05d}", "[]", (_START + timedelta(days=n % 3, minutes=n % 900))
             .strftime("%Y-%m-%d %H:%M:%S"), None) for n in range(1800)]
    lake = FakeLake(columns=_COLUMNS, rows=rows, part_col="created", cutoff=20_000_000)
    result = _exact(lake, _BATCHES)
    assert result["rows"] == lake.reference(_BATCHES)[:500]
    assert result["exact_split"]["groups"] == 1800
    assert max(result["exact_split"]["row_counts"]) > 500


def test_a_piece_with_more_groups_than_the_group_cap_gives_no_result(lake):
    split = _refused(lake, _BATCHES, {"exact_split_max_groups": 15})
    assert split["reason"] == "group_cap"
    assert "15 groups" in split["detail"]


def test_a_grouped_piece_is_the_model_sql_without_its_limit_plus_one_conjunct(lake):
    sql = "SELECT batch, COUNT(*) AS n FROM events WHERE site = 'site-a' GROUP BY batch LIMIT 7"
    _exact(lake, sql, {"exact_split_max_groups": 900})
    pattern = re.compile(r"^SELECT batch, COUNT\(\*\) AS n FROM events FOR VERSION AS OF 4242 "
                         r"WHERE (?P<pred>.+) AND \(site = 'site-a'\) GROUP BY batch LIMIT 901$")
    pieces = _pieces(lake)
    assert pieces and all(pattern.match(p) for p in pieces), pieces[:2]


def test_a_breakdown_with_few_groups_reports_them_all(lake):
    result = _exact(lake, "SELECT site, COUNT(*) AS n FROM events GROUP BY site ORDER BY site")
    assert result["exact_split"]["groups"] == result["exact_split"]["groups_shown"] == 2
    assert "groups" not in result["note"]


def test_the_group_cap_is_a_per_system_knob_that_fails_closed():
    from agents.athena_exact_split import DEFAULT_MAX_GROUPS, resolve_caps
    from agents.connection_resolver import _NODE_CONFIG_KEYS
    base = {"workgroup": "wg", "database": "db"}
    assert resolve_caps(at._resolve_config(base)).max_groups == DEFAULT_MAX_GROUPS
    assert resolve_caps(at._resolve_config({**base, "exact_split_max_groups": "300"})).max_groups == 300
    for bad in (0, -1, "abc", True):
        with pytest.raises(at.AthenaConfigError):
            at._resolve_config({**base, "exact_split_max_groups": bad})
    assert "exact_split_max_groups" in _NODE_CONFIG_KEYS


def test_whole_row_reads_are_internal_only(lake):
    import agents.athena_exact_split as sp
    with pytest.raises(ValueError):
        at.run_athena_query("SELECT site FROM events", _CFG, client=lake, source_system="lake-a",
                            _row_budget=sp._RowBudget(sp.GROUP_BYTES_BUDGET))
