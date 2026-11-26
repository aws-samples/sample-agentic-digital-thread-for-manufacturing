# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""``grouped_keys.read``: which outputs of a query's rows hold its grouped
values, for checks that read a result's rows. It reports positions only when
it can read them with certainty, and ``None`` otherwise.

Fully offline; synthetic names.
"""
from __future__ import annotations

import pytest

from agents.grouped_keys import GroupedKeys, read

_INNER = "SELECT kind, COUNT(*) AS n FROM readings GROUP BY kind"


@pytest.mark.parametrize(("sql", "expected"), [
    ("SELECT kind, AVG(v) AS mean FROM readings GROUP BY kind",
     GroupedKeys(2, ((0, "kind"),), "readings")),
    ("SELECT COUNT(*) AS n, r.kind FROM readings r GROUP BY 2",
     GroupedKeys(2, ((1, "kind"),), "readings")),
    ("SELECT lower(kind) AS k, site, COUNT(*) FROM readings GROUP BY lower(kind), site",
     GroupedKeys(3, ((0, None), (1, "site")), "readings")),
    ("SELECT DISTINCT site, kind FROM readings ORDER BY site LIMIT 5",
     GroupedKeys(2, ((0, "site"), (1, "kind")), "readings")),
    (f"WITH g AS ({_INNER}) SELECT n, kind AS k FROM g",
     GroupedKeys(2, ((1, "kind"),), "readings")),
    (f"WITH g AS ({_INNER}) SELECT * FROM g ORDER BY n DESC LIMIT 3",
     GroupedKeys(2, ((0, "kind"),), "readings")),
    (f"SELECT * FROM ({_INNER}) AS x", GroupedKeys(2, ((0, "kind"),), "readings")),
    ("SELECT a.kind, COUNT(*) FROM readings a JOIN sites b ON a.site = b.site GROUP BY a.kind",
     GroupedKeys(2, ((0, "kind"),), "")),
    ("-- a comment\nSELECT kind, COUNT(*) FROM readings GROUP BY kind;",
     GroupedKeys(2, ((0, "kind"),), "readings")),
])
def test_grouped_positions_are_read(sql, expected):
    assert read(sql) == expected


@pytest.mark.parametrize("sql", [
    None, "", "SELECT", "WITH", "SELECT kind FROM", "SELECT kind FROM readings GROUP BY",
    "SELECT kind FROM readings",
    "SELECT kind, COUNT(*) FROM readings GROUP BY 0",
    "SELECT kind, COUNT(*) FROM readings GROUP BY 3",
    "SELECT kind, COUNT(*) FROM readings GROUP BY CUBE (kind)",
    "SELECT * FROM readings GROUP BY kind",
    "(SELECT kind FROM readings GROUP BY kind)",
    "WITH RECURSIVE g AS (SELECT 1) SELECT * FROM g",
    f"WITH g (k, n) AS ({_INNER}) SELECT k, n FROM g",
    f"WITH g AS ({_INNER}) SELECT g.* FROM g",
    f"WITH g AS ({_INNER}), h AS (SELECT 1 AS z) SELECT kind, n FROM g, h",
    f"SELECT kind, n FROM ({_INNER}) x (kind, n)",
    "SELECT kind, rank() OVER (ORDER BY kind) FROM readings GROUP BY kind",
    "SELECT kind FROM readings GROUP BY kind /* unclosed",
])
def test_unreadable_positions_give_none(sql):
    assert read(sql) is None


@pytest.mark.parametrize(("sql", "expected"), [
    ("SELECT kind, COUNT(*) AS n FROM readings GROUP BY kind", (1,)),
    ("SELECT COUNT(1) AS n, kind FROM readings GROUP BY 2", (0,)),
    ("SELECT kind, COUNT(*) AS n, COUNT(1) AS m FROM readings GROUP BY kind", (1, 2)),
    ("SELECT kind, COUNT(v) AS n FROM readings GROUP BY kind", ()),
    ("SELECT kind, COUNT(DISTINCT v) AS n FROM readings GROUP BY kind", ()),
    ("SELECT kind, SUM(v) AS n FROM readings GROUP BY kind", ()),
    ("SELECT kind, COUNT(*) + 1 AS n FROM readings GROUP BY kind", ()),
    # a hop: the outer query's counts are not read
    (f"WITH g AS ({_INNER}) SELECT * FROM g", ()),
    ("SELECT DISTINCT kind FROM readings", ()),
    (None, ()), ("SELECT kind FROM readings", ()),
])
def test_row_count_outputs_are_read(sql, expected):
    from agents.grouped_keys import row_counts
    assert row_counts(sql) == expected
