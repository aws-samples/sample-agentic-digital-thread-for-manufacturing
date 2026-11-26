# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Provision and the graph rebuild pick the same described fields.

The record has caps (``field_descriptions.MAX_ITEMS`` fields and
``TOTAL_CHARS`` characters). Provision builds it from its mappings, the
startup restore and a Save and Reconnect rebuild it from the graph's stored
field types. Both apply the caps in one canonical order (table, then column,
which the graph can give back from what it stores), so a field described
right after provision is still described after a restart or a reconnect,
whatever order discovery returned the fields in.

Checked on the type-text shapes of five connectors (i3X, SiteWise, REST,
MCP, Tulip), at the caps and over them, in reversed and shuffled mapping
order, with undescribed fields mixed in; and on a schema whose undescribed
typed fields alone would fill the graph read's row limit.

The rebuild reads the graph in pages, in that order, until each record is
full or the rows run out: a type that holds a separator but no meaning
(``decimal (12, 4)``) is a candidate row, never a description, so a crowd of
them cannot hide a described field. A read its budget cuts short, or whose
rows come back out of the canonical order, is not used: the carried or
current record is kept instead.

Fully offline: the graph is a fake that answers the read's filters.
"""
from __future__ import annotations

import random
import time

import pytest

import agents.onboard as onboard
import agents.system_agent as sa
from agents import field_descriptions as fd

_DASH = "\u2014"
#: (table names, type text of a described field, type text of a bare one)
_SHAPES = {
    "i3x": (("Machine", "Line", "Station"), "string " + _DASH + " maps to concept: c{i}", "string"),
    "sitewise": (("Mill Model", "Press Model", "Line Model"),
                 "DOUBLE (unit{i}) " + _DASH + " sitewise property", "DOUBLE"),
    "rest": (("GET /orders", "GET /lines", "POST /checks"), "response_string (meaning {i})",
             "query_string"),
    "mcp": (("get_line_status", "list_alarms", "read_tags"), "string (meaning {i})", "number"),
    "tulip": (("Work Orders", "Defects", "Stations"), "text - meaning {i}", "text"),
}


def _columns(shape: str, described: int, *, bare: int = 10, width: int = 0) -> list[tuple]:
    """``(table, column, data_type)`` of a schema of ``shape``: ``described``
    described fields (``c000``...) spread over its tables, ``bare`` undescribed
    ones; ``width`` pads each description to that many characters."""
    tables, described_type, bare_type = _SHAPES[shape]
    out = []
    for i in range(described):
        text = described_type.format(i=i)
        out.append((tables[i % len(tables)], f"c{i:03d}", text + "x" * max(0, width - len(text))))
    out += [(tables[i % len(tables)], f"b{i:03d}", bare_type) for i in range(bare)]
    return out


def _mappings(columns, order: str) -> list[dict]:
    """The provision mappings of ``columns`` in mapping ``order``."""
    rows = [{"table": t, "field": f"{t}.{c}", "concept": "skip", "data_type": d}
            for t, c, d in columns]
    if order == "reversed":
        rows.reverse()
    elif order == "shuffled":
        random.Random(1023).shuffle(rows)
    return rows


class _Graph:
    """The graph as provision leaves it: one Field per mapped column with its
    type. Answers the record read as the graph would: the filters the read
    sends (the system ids, the type texts a description can follow), in
    (system, table, column) order, cut at the read's row limit."""

    def __init__(self, system_id, columns=()):
        self.rows = [{"sid": system_id, "tbl": t, "col": c, "dtype": d} for t, c, d in columns]
        self.reads = 0

    def add(self, system_id, columns):
        self.rows += [{"sid": system_id, "tbl": t, "col": c, "dtype": d} for t, c, d in columns]
        return self

    def __call__(self, cypher, params=None):
        self.reads += 1
        params = params or {}
        separators = [v for k, v in params.items() if k.startswith("sep")]
        after = (params.get("after_sid"), params.get("after_tbl"), params.get("after_col"))
        rows = [r for r in self.rows if r["sid"] in params.get("sids", [])
                and (not separators or any(s in r["dtype"] for s in separators))
                and (None in after or (r["sid"], r["tbl"], r["col"]) > after)]
        rows.sort(key=lambda r: (r["sid"], r["tbl"], r["col"]))
        limit = int(cypher.rsplit("LIMIT", 1)[1])
        return [dict(r) for r in rows[:limit]]


def _both(columns, order: str):
    """(the record provision builds, the record the graph rebuild gives back)."""
    provision = onboard._field_description_items(_mappings(columns, order))
    rebuild = fd.read_from_graph("sys", query=_Graph("sys", columns))
    return provision, rebuild


def _canonical(columns) -> list[str]:
    """Every described field, in the canonical (table, column) order."""
    return [f"{t}.{c}" for t, c, d in sorted(columns) if fd.type_description(d)]


@pytest.mark.parametrize("order", ["reversed", "shuffled", "as discovered"])
@pytest.mark.parametrize("described", [fd.MAX_ITEMS - 1, fd.MAX_ITEMS, fd.MAX_ITEMS + 1,
                                       fd.MAX_ITEMS + 2])
@pytest.mark.parametrize("shape", sorted(_SHAPES))
def test_provision_and_rebuild_pick_the_same_fields_at_the_item_cap(shape, described, order):
    columns = _columns(shape, described)
    provision, rebuild = _both(columns, order)
    assert provision == rebuild
    assert [it["field"] for it in provision] == _canonical(columns)[:fd.MAX_ITEMS]


@pytest.mark.parametrize("order", ["reversed", "shuffled"])
@pytest.mark.parametrize("shape", sorted(_SHAPES))
def test_provision_and_rebuild_pick_the_same_fields_over_the_character_cap(shape, order):
    columns = _columns(shape, 80, width=fd.MAX_CHARS)
    provision, rebuild = _both(columns, order)
    assert provision == rebuild
    kept = [it["field"] for it in provision]
    assert 0 < len(kept) < 80
    assert kept == _canonical(columns)[:len(kept)]
    assert sum(len(it["field"]) + len(it["description"]) for it in provision) <= fd.TOTAL_CHARS


def test_reversed_fields_over_the_item_cap_drop_the_same_two():
    """152 described fields in reverse column order: provision used to keep
    the last 150 it was given (dropping c000 and c001) while the rebuild kept
    the first 150 by name (dropping c150 and c151)."""
    columns = [("T", f"c{i:03d}", f"text (meaning {i})") for i in range(152)]
    provision, rebuild = _both(columns, "reversed")
    assert provision == rebuild
    kept = {it["field"] for it in provision}
    assert {"T.c000", "T.c001"} <= kept and not kept & {"T.c150", "T.c151"}


def test_undescribed_typed_fields_never_crowd_the_described_ones_out():
    """More undescribed typed fields than the read's row limit, all sorting
    before the described ones: the rebuild still finds every described field
    provision kept."""
    columns = [("A_bulk", f"col{i:05d}", "integer") for i in range(3100)]
    columns += [("Z_desc", f"c{i:03d}", f"text (meaning {i})") for i in range(12)]
    provision, rebuild = _both(columns, "as discovered")
    assert len(provision) == 12 and provision == rebuild


# --------------------------------------------------------------------------- #
# The paged read
# --------------------------------------------------------------------------- #
_STOPPED = ("Z_events", "stopped_at", "text (when the machine stopped)")


def test_types_with_a_separator_and_no_meaning_never_crowd_a_description_out():
    """3000 ``decimal (12, 4)`` fields (a separator, no meaning) sort before
    the one described field: provision keeps it, and so does the rebuild."""
    columns = [("A_numeric", f"n{i:04d}", "decimal (12, 4)") for i in range(3000)]
    columns.append(_STOPPED)
    provision, rebuild = _both(columns, "as discovered")
    assert provision == rebuild == [{"field": "Z_events.stopped_at",
                                     "description": "when the machine stopped"}]


def test_the_read_stops_once_the_record_is_full():
    """2500 candidates with no meaning, then 2000 described fields: the third
    page fills the record (150 fields), so the last two are never read."""
    columns = [("A_numeric", f"n{i:04d}", "decimal (12, 4)") for i in range(2500)]
    columns += [("B_events", f"e{i:04d}", f"text (event {i})") for i in range(2000)]
    graph = _Graph("sys", columns)
    record = fd.read_from_graph("sys", query=graph)
    assert record == onboard._field_description_items(_mappings(columns, "shuffled"))
    assert len(record) == fd.MAX_ITEMS and graph.reads == 3


def test_one_startup_read_pages_across_systems():
    """Two systems in one read: the first is full after one page and is left
    out of the next, the second (behind a crowd of candidates) is read to
    its end."""
    first = [("T", f"c{i:04d}", f"text (meaning {i})") for i in range(1500)]
    second = [("A_numeric", f"n{i:04d}", "decimal (12, 4)") for i in range(1200)] + [_STOPPED]
    graph = _Graph("sys-a", first).add("sys-b", second)
    with fd.prefetched(["sys-b", "sys-a"], query=graph) as records:
        assert records["sys-a"] == onboard._field_description_items(_mappings(first, "reversed"))
        assert records["sys-b"] == [{"field": "Z_events.stopped_at",
                                     "description": "when the machine stopped"}]
    assert graph.reads == 3


_CURRENT = {fd.KEY: [{"field": "Z_events.kept", "description": "what provision wrote"}]}


def test_a_read_its_budget_cuts_short_keeps_the_current_record(monkeypatch):
    """Six slow pages do not fit the budget. The rows read are a prefix of
    the canonical order, but a record cut there would miss fields provision
    kept, so the current record is used instead."""
    monkeypatch.setattr(fd, "GRAPH_BUDGET_S", 0.4)
    columns = [("A_numeric", f"n{i:04d}", "decimal (12, 4)") for i in range(5500)] + [_STOPPED]
    graph = _Graph("sys", columns)

    def slow(cypher, params=None):
        time.sleep(0.15)
        return graph(cypher, params)
    out = fd.rebuilt("sys", {"base_url": "x"}, query=slow, current=lambda: _CURRENT)
    assert out == {"base_url": "x", **_CURRENT}
    assert graph.reads < 6


def test_rows_out_of_the_canonical_order_are_not_used():
    """A graph whose rows do not come back in the canonical order cannot show
    that a read stopped before its end holds the first fields: the read is
    not used, and the current record is kept."""
    def unordered(cypher, params=None):
        return [{"sid": "sys", "tbl": "B", "col": "x", "dtype": "text (second)"},
                {"sid": "sys", "tbl": "A", "col": "y", "dtype": "text (first)"}]
    out = fd.rebuilt("sys", {}, query=unordered, current=lambda: _CURRENT)
    assert out == _CURRENT
