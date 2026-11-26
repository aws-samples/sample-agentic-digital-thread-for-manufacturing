# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``POST /api/onboard/verify`` checks each connection type the way it can.

Two findings from the 2026-10-19 workshop walks:

* an MCP-type hub ended onboarding with "✓ Provisioned" then
  "⚠ schema is required Retry": the route demanded a schema before it looked
  at the connection type, and a system without a SQL schema never has one;
* a typed REST connector verified as "1 tables, 0 rows accessible" although
  its discovery had counted rows: the graph check hard-coded one table and
  zero rows, so the summary claimed a count it never made.

Now a non-SQL system (every ``NON_SQL_CONNECTION_TYPES`` member) is checked in
the knowledge graph with no schema required, and the answer says what it
checked: ``check`` names the check, ``rows_counted`` is false, the entity and
field counts are the graph's own, and no ``total_rows`` is claimed. A
PostgreSQL system still needs its schema and counts rows. Fixtures use made-up
system ids, so nothing here depends on the demo data.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

import agents.onboard as onboard  # noqa: E402
from agents import connection_types as ct  # noqa: E402
from agents.models import VerifyRequest  # noqa: E402

SID = "zeta-hub"


class _Graph:
    """Answers the verify route's System lookup from a fixed node."""

    def __init__(self, status="onboarded", entries=3, fields=17, present=True, fail=False):
        self.status, self.entries, self.fields = status, entries, fields
        self.present, self.fail = present, fail
        self.calls = []

    def __call__(self, cypher, params=None, *a, **k):
        self.calls.append((cypher, params))
        if self.fail:
            raise RuntimeError("graph down")
        if not self.present:
            return []
        return [{"status": self.status, "entries": self.entries, "fields": self.fields}]


def _no_pg(*a, **k):
    raise AssertionError("a non-SQL system must not take the PostgreSQL path")


def _run(monkeypatch, entry, schema="", graph=None):
    monkeypatch.setattr(onboard.catalog, "get", lambda sid: entry)
    monkeypatch.setattr(onboard, "neptune_query", graph or _Graph())
    return asyncio.run(onboard.onboard_verify(VerifyRequest(system_id=SID, schema=schema)))


@pytest.mark.parametrize("ctype", list(ct.NON_SQL_CONNECTION_TYPES))
def test_a_non_sql_system_verifies_without_a_schema(monkeypatch, ctype):
    monkeypatch.setattr(onboard, "_verify_pg_reachability", _no_pg)
    monkeypatch.setattr(onboard, "pg", _no_pg)
    result = _run(monkeypatch, {"id": SID, "connection_type": ctype, "schema": ""})
    assert not result.get("error"), result
    assert result["verified"] is True, result
    assert result["check"] == "knowledge_graph", result
    assert result["connection_type"] == ctype


def test_the_graph_check_reports_its_own_counts_and_claims_no_rows(monkeypatch):
    graph = _Graph(entries=4, fields=29)
    result = _run(monkeypatch, {"id": SID, "connection_type": ct.REST_API, "schema": ""},
                  graph=graph)
    assert result["tables_accessible"] == 4, result
    assert result["fields"] == 29, result
    assert result["rows_counted"] is False, result
    # A row total it never counted is not reported at all, not reported as 0.
    assert "total_rows" not in result, result
    assert graph.calls and graph.calls[0][1] == {"sid": SID}


def test_a_non_sql_system_with_no_graph_entities_still_verifies_as_onboarded(monkeypatch):
    result = _run(monkeypatch, {"id": SID, "connection_type": ct.MCP_SERVER},
                  graph=_Graph(entries=0, fields=0))
    assert result["verified"] is True, result
    assert result["tables_accessible"] == 0
    assert result["rows_counted"] is False


def test_a_non_sql_system_that_is_not_onboarded_is_an_issue(monkeypatch):
    result = _run(monkeypatch, {"id": SID, "connection_type": ct.TULIP},
                  graph=_Graph(status="provisioning"))
    assert result["verified"] is False, result
    assert result.get("error"), result
    assert "schema" not in result["error"]


def test_a_non_sql_system_missing_from_the_graph_is_an_issue(monkeypatch):
    result = _run(monkeypatch, {"id": SID, "connection_type": ct.SITEWISE_I3X},
                  graph=_Graph(present=False))
    assert result["verified"] is False, result
    assert SID in result["error"]


def test_a_graph_failure_is_reported_not_raised(monkeypatch):
    result = _run(monkeypatch, {"id": SID, "connection_type": ct.ATHENA},
                  graph=_Graph(fail=True))
    assert result["verified"] is False, result
    assert result["error"].startswith("Neptune check failed"), result


def test_postgresql_still_requires_its_schema(monkeypatch):
    monkeypatch.setattr(onboard, "_verify_pg_reachability", _no_pg)
    result = _run(monkeypatch, {"id": SID, "connection_type": ct.POSTGRESQL, "schema": ""})
    assert result.get("error") == "schema is required", result


def test_postgresql_counts_rows_and_says_so(monkeypatch):
    monkeypatch.setattr(onboard, "_verify_pg_reachability",
                        lambda conn_str, schema: (["alpha", "beta"], 12, None))
    result = _run(monkeypatch, {"id": SID, "connection_type": ct.POSTGRESQL,
                                "schema": "omega_sch", "connection_string": ""})
    assert result["verified"] is True, result
    assert result["check"] == "database"
    assert result["rows_counted"] is True
    assert result["tables_accessible"] == 2 and result["total_rows"] == 12


def test_postgresql_with_no_tables_is_an_issue(monkeypatch):
    monkeypatch.setattr(onboard, "_verify_pg_reachability",
                        lambda conn_str, schema: ([], 0, None))
    result = _run(monkeypatch, {"id": SID, "connection_type": ct.POSTGRESQL,
                                "schema": "omega_sch"})
    assert result["verified"] is False, result


def test_an_unreachable_registry_database_is_an_error_not_zero_tables(monkeypatch):
    """The no-DSN path reads through ``pg()``, which answers a failure with an
    empty result marked ``unavailable``. That used to read as a reachable
    schema with no tables."""
    monkeypatch.setattr(onboard, "pg", lambda *a, **k: {
        "columns": [], "rows": [], "row_count": 0,
        "unavailable": True, "error_category": "OperationalError"})
    tables, rows, err = onboard._verify_pg_reachability("", "omega_sch")
    assert (tables, rows) == ([], 0)
    assert err and err.startswith("Database connection failed"), err


def test_a_reachable_registry_database_still_lists_its_tables(monkeypatch):
    def _pg(schema, sql, params=None, **k):
        if "information_schema.tables" in sql:
            return {"columns": ["table_name"], "rows": [["alpha"], ["beta"]]}
        if "COUNT(*)" in sql:
            return {"rows": [[2]]}
        return {"rows": [["alpha", 5]]}
    monkeypatch.setattr(onboard, "pg", _pg)
    tables, rows, err = onboard._verify_pg_reachability("", "omega_sch")
    assert err is None
    assert tables == ["alpha", "beta"] and rows == 7


# ── later registry reads: every pg() answer is checked, not just the list ────

_UNAVAILABLE = {"columns": [], "rows": [], "row_count": 0,
                "unavailable": True, "error_category": "OperationalError"}


def _registry(monkeypatch, tables, estimate, counts):
    """A scripted ``pg()``: the table list succeeds with ``tables``; the
    planner-estimate read answers ``estimate``; each fallback ``COUNT(*)``
    answers ``counts[table]``. Records the tables it was asked to count."""
    counted = []

    def _pg(schema, sql, params=None, **k):
        if "information_schema.tables" in sql:
            return {"columns": ["table_name"], "rows": [[t] for t in tables]}
        if "reltuples" in sql:
            return estimate
        table = sql.rsplit('"."', 1)[1].rstrip('"')
        counted.append(table)
        return counts[table]
    monkeypatch.setattr(onboard, "pg", _pg)
    return counted


def _route_verify(monkeypatch):
    monkeypatch.setattr(onboard.catalog, "get", lambda sid: {
        "id": SID, "connection_type": ct.POSTGRESQL, "schema": "omega_sch",
        "connection_string": ""})
    return asyncio.run(onboard.onboard_verify(VerifyRequest(system_id=SID, schema="omega_sch")))


def test_an_unavailable_estimate_read_falls_back_to_exact_counts(monkeypatch):
    """Case (a): the estimate read fails, so every table is counted exactly,
    and its scripted rows (which a failed read must not be trusted for) are
    ignored."""
    counted = _registry(monkeypatch, ["alpha", "beta"],
                        {**_UNAVAILABLE, "rows": [["alpha", 999]]},
                        {"alpha": {"rows": [[3]]}, "beta": {"rows": [[4]]}})
    assert onboard._verify_pg_reachability("", "omega_sch") == (["alpha", "beta"], 7, None)
    assert counted == ["alpha", "beta"]
    result = _route_verify(monkeypatch)
    assert result["verified"] is True and result["total_rows"] == 7, result


@pytest.mark.parametrize("successful_before", [0, 1, 2])
def test_an_unavailable_fallback_count_is_an_error_not_a_partial_total(monkeypatch,
                                                                       successful_before):
    """Case (b): a fallback count fails after zero or more successful counts.
    The answer is the masked connection error, never the partial total."""
    names = ["alpha", "beta", "gamma"]
    counts = {n: {"rows": [[5]]} for n in names}
    counts[names[successful_before]] = _UNAVAILABLE
    _registry(monkeypatch, names, {"rows": []}, counts)
    tables, total, err = onboard._verify_pg_reachability("", "omega_sch")
    assert (tables, total) == ([], 0)
    assert err and err.startswith("Database connection failed"), err
    result = _route_verify(monkeypatch)
    assert result["verified"] is False, result
    assert result["total_rows"] == 0 and result["tables_accessible"] == 0, result
    assert "OperationalError" not in result["error"], result


@pytest.mark.parametrize("answer", [
    {"rows": []},                    # no row at all
    {"rows": [[1], [2]]},            # more than one row
    {"rows": [[1, 2]]},              # more than one column
    {"rows": [[None]]},              # no value
    {"rows": [["many"]]},            # not a number
    {"rows": [[-1]]},                # not a count
    {"rows": [[True]]},              # a boolean is not a count
])
def test_a_fallback_count_that_is_not_one_scalar_is_an_error(monkeypatch, answer):
    _registry(monkeypatch, ["alpha"], {"rows": []}, {"alpha": answer})
    tables, total, err = onboard._verify_pg_reachability("", "omega_sch")
    assert (tables, total) == ([], 0), (tables, total)
    assert err and err.startswith("Database connection failed"), err


def test_estimates_still_cover_tables_and_only_the_rest_are_counted(monkeypatch):
    counted = _registry(monkeypatch, ["alpha", "beta"], {"rows": [["alpha", 10]]},
                        {"beta": {"rows": [[2]]}})
    assert onboard._verify_pg_reachability("", "omega_sch") == (["alpha", "beta"], 12, None)
    assert counted == ["beta"]
