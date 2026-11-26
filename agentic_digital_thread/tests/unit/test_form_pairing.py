# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The pairing system's side of the engine's form crossing (``form_pairing``).

A reserved-argument request on the delegation tool asks the system that
pairs two identifier forms for the other form of listed values. No model
runs. The request carries only a stored JoinPath id and the values; the
runtime decides everything else from its own graph and schema:

* the JoinPath must be a stored, active, internal per-item pairing of two of
  its own tables (its key columns are the join);
* the two columns read are the measured form endpoints on those tables (the
  exact-agreement pass's joins to one other system for one concept), and
  every value must have one of their measured shapes; a read past the
  endpoint cap is refused, never cut;
* no column, as the graph spells it or as it is read, is named like a
  person-valued field of the catalog's own vocabulary
  (``catalog.PERSON_VALUED_FIELDS``: an assignee, a badge, a requester or
  approver, a customer, supplier or vendor), matched as whole words after
  camelCase, PascalCase, digits and separators are split;
* concepts, from the graph's own mappings and a server-owned allowlist of
  plant-generated production records: every concept each form's exact join
  records and every endpoint and key column's stored concepts must be on
  ``catalog.CROSSABLE_CONCEPTS`` and not in ``catalog.PERSON_CONCEPTS`` (the
  second check), each column with an identifier or correlate mapping; any
  other concept (customer, shift, engineering change, a purchase order or a
  part, a person, unknown or custom concept) or an unmapped column refuses,
  whatever the columns are named;
* each endpoint column's own stored concept is exactly the concept its
  form's exact join records for it, never another concept on the list;
* the query path's scope holds (own physical columns, no declared row
  exclusion), and, as extra layers, no column named like personal data and
  every returned value matching its column's measured shape, the identifier
  grammar and the value-level redaction, or its pair is dropped and counted.

Any other request gets a fixed refusal and no data is read. At the runtime's
public MCP request path the reserved argument is accepted only on the
engine's call shape (the one query tool name this runtime registered, bare
as the cloud entry point serves it or prefixed as local mode does, the fixed
pairing question alone, one call per POST) and refused otherwise.

Neutral schema (system cell-x; tables jobs and builds). Fully offline: the
graph read and the database are faked; ``tests/integration/
test_form_pairing_pg.py`` runs the read on PostgreSQL.
"""
from __future__ import annotations

import json

import pytest

from agents import form_pairing as fp
from agents import identity_affinity as ia

SID, HOLDER = "cell-x", "ledger-x"
JOB_SHAPE, BUILD_SHAPE = "AA-9999-999", "AAA99999"
PHYSICAL = {
    "jobs": {"relation_kind": "table", "columns": {
        "job_id": "uuid", "job_code": "text", "build_id": "uuid", "operator_email": "text",
        "operator_ref": "text"}},
    "builds": {"relation_kind": "table", "columns": {"build_id": "uuid", "build_code": "text"}},
}
PAIRING = {"id": "jp-int-1", "from_table": "s_cell.jobs", "from_field": "build_id",
           "to_table": "s_cell.builds", "to_field": "build_id", "method": "foreign_key_match",
           "basis": "declared_foreign_key"}


#: The exact joins' agreed concept: the exact pass agreed each form with the
#: other system's column through a catalog-related identifier pair, so each
#: join records this concept and, in its label, the form's own concept.
JOIN_CONCEPT = "batch"
#: Each form's own concept, as its exact join's label records it.
FORM_CONCEPT = {"jobs": "work-order", "builds": "production-run"}
#: The query tool name the cloud entry point registers for this system.
BARE_TOOL, PREFIXED_TOOL = "query_cell_x", "cell-x___query_cell_x"


@pytest.fixture(autouse=True)
def registered(monkeypatch):
    """This runtime's own query tool, registered as the cloud entry point
    registers it (``create_mcp_server`` with no prefix)."""
    monkeypatch.setattr(fp, "_QUERY_TOOLS", {SID: BARE_TOOL})


def _reasoning(shape, concept=JOIN_CONCEPT, related=""):
    """The exact pass's reasoning (``identity_affinity._exact_reasoning``),
    with the catalog-related label when ``related`` is set."""
    label = f"{concept} (catalog-related {related})" if related else concept
    return (f"40 exact values exist (membership-probed); both columns shape {shape}; "
            f"concept {label}; 50 and 90 distinct values sampled")


def _exact(table, column, shape, *, peer=HOLDER, concept=JOIN_CONCEPT, related=None,
           outbound=True, **over):
    """A measured exact join as the graph stores it: written by this system's
    resolve (``outbound``) or by the peer's. On the agreed concept its label
    names the form's own concept (:data:`FORM_CONCEPT`) unless ``related``
    says otherwise."""
    if related is None:
        related = FORM_CONCEPT.get(table, "") if concept == JOIN_CONCEPT else ""
    own = {"system": SID, "table": f"s_cell.{table}", "field": column}
    other = {"system": peer, "table": "s_led.log", "field": "unit_ref"}
    first, second = (own, other) if outbound else (other, own)
    row = {"from_system": first["system"], "from_table": first["table"],
           "from_field": first["field"], "to_system": second["system"],
           "to_table": second["table"], "to_field": second["field"],
           "transform": "identity", "provenance": _reasoning(shape, concept, related)}
    row.update(over)
    return row


ENDPOINTS = [_exact("jobs", "job_code", JOB_SHAPE),
             _exact("builds", "build_code", BUILD_SHAPE, outbound=False)]


def _mapped(table, column, concept, role="identifier", retired=False):
    """One stored concept mapping (onboarding's ``Field-[:MAPS_TO]->Concept``)."""
    return {"field": f"{SID}.{table}.{column}", "concept": concept, "role": role,
            "retired": retired}


#: The four columns' stored concepts: both endpoints and both key columns.
CONCEPTS = [_mapped("jobs", "job_code", "work-order"),
            _mapped("builds", "build_code", "production-run"),
            _mapped("jobs", "build_id", "production-run", role="correlate"),
            _mapped("builds", "build_id", "production-run")]


class _Graph:
    """This runtime's graph: the pairing JoinPath by id, its exact joins, the
    columns' stored concept mappings."""

    def __init__(self, pairing=(PAIRING,), endpoints=ENDPOINTS, concepts=CONCEPTS):
        self.pairing, self.endpoints = list(pairing), list(endpoints)
        self.concepts, self.seen = list(concepts), []

    def __call__(self, cypher, params):
        self.seen.append((cypher, dict(params)))
        if cypher == fp.PAIRING_CYPHER:
            return [dict(r) for r in self.pairing if r.get("id") == params["id"]]
        if cypher == fp.ENDPOINTS_CYPHER:
            assert params == {"sid": SID, "exact": ia.EXACT_METHOD}
            return [dict(r) for r in self.endpoints]
        if cypher == fp.CONCEPTS_CYPHER:
            assert params["sid"] == SID
            return [dict(r) for r in self.concepts if r["field"].lower() in params["fields"]]
        raise AssertionError(cypher)


@pytest.fixture
def database(monkeypatch):
    """The read-only database: records each read, answers ``database.rows``."""
    class _Database:
        def __init__(self):
            self.rows = [("JB-4471-001", "BLD00412")]
            self.executed = []

        def execute(self, schema, conn_params, statement, values_a, values_b):
            self.executed.append((schema, statement, list(values_a), list(values_b)))
            if isinstance(self.rows, Exception):
                raise self.rows
            return list(self.rows)
    db = _Database()
    monkeypatch.setattr(fp, "_execute", db.execute)
    return db


def _raw(values=("JB-4471-001",), join="jp-int-1", **extra):
    return json.dumps({"v": 2, "join": join, "values": list(values), **extra})


def _answer(raw, graph=None, connection_type="PostgreSQL", physical=PHYSICAL):
    graph = graph or _Graph()
    reply = fp.answer(raw, system_id=SID, connection_type=connection_type, schema="s_cell",
                      physical=physical, conn_params=lambda: {"host": "h"}, read_graph=graph)
    return json.loads(reply), graph


def _refused(reason):
    return {"engine": "pair_forms", "v": 2, "status": "refused", "reason": reason}


# --------------------------------------------------------------------------- #
# The read
# --------------------------------------------------------------------------- #
def test_the_runtime_picks_both_columns_from_its_own_stored_joins(database):
    reply, graph = _answer(_raw(("JB-4471-001", "BLD00412")))
    assert reply == {"engine": "pair_forms", "v": 2, "status": "ok",
                     "fields": ["jobs.job_code", "builds.build_code"],
                     "pairs": [["JB-4471-001", "BLD00412"]], "cut": False, "dropped": 0}
    assert graph.seen[0] == (fp.PAIRING_CYPHER, {"id": "jp-int-1", "sid": SID})
    schema, statement, values_a, values_b = database.executed[0]
    assert schema == "s_cell" and values_a == ["JB-4471-001"] and values_b == ["BLD00412"]
    text = repr(statement)
    for name in ("jobs", "builds", "job_code", "build_code", "build_id"):
        assert f"Identifier('{name}')" in text
    assert "operator" not in text


def test_the_stored_pairing_may_run_either_way(database):
    database.rows = [("BLD00412", "JB-4471-001")]
    reversed_join = {**PAIRING, "from_table": "s_cell.builds", "to_table": "s_cell.jobs"}
    reply, _rd = _answer(_raw(), graph=_Graph(pairing=(reversed_join,)))
    assert reply["fields"] == ["builds.build_code", "jobs.job_code"]
    assert reply["pairs"] == [["BLD00412", "JB-4471-001"]]


def test_the_read_returns_every_row_sharing_a_value_with_a_matched_row():
    """One hop out, so the caller sees when a value pairs with more than one."""
    from agents.pgdriver import sql as psql
    plan = fp.Plan(fp.Endpoint("jobs", "job_code", JOB_SHAPE),
                   fp.Endpoint("builds", "build_code", BUILD_SHAPE), "build_id", "build_id")
    statement = fp.statement(plan)
    assert isinstance(statement, psql.Composed)
    text = repr(statement)
    assert "WITH m AS" in text and "IN (SELECT x FROM m)" in text and "IN (SELECT y FROM m)" in text
    assert text.count("%s") == 3


def test_the_connection_is_read_only_with_a_connect_timeout(monkeypatch):
    from agents import pgdriver
    seen = {}

    class _Conn:
        def set_session(self, **kwargs):
            seen["session"] = kwargs
    monkeypatch.setattr(pgdriver, "connect", lambda **kw: seen.update(kw) or _Conn())
    fp.connect({"host": "h", "dbname": "d"})
    assert seen["connect_timeout"] == fp.CONNECT_TIMEOUT_S
    assert seen["session"] == {"readonly": True, "autocommit": True}


# --------------------------------------------------------------------------- #
# Nothing the caller sends picks a column or a join
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("raw", [
    _raw(**{"from": "jobs.operator_email"}),
    _raw(to="builds.build_code"),
    json.dumps({"v": 1, "values": ["JB-4471-001"], "from": "jobs.job_code",
                "to": "builds.build_code", "join": "jp-int-1"}),
    "not json", json.dumps([1]), _raw(values=[]), _raw(values=["a b"]),
    _raw(values=[f"V-{i}" for i in range(fp.MAX_VALUES + 1)]), _raw(join="bad id!"),
    _raw(join=""), "x" * (fp.MAX_REQUEST_CHARS + 1), None,
])
def test_a_request_that_is_not_only_a_join_id_and_values_is_refused_before_any_read(
        database, raw):
    reply, graph = _answer(raw)
    assert reply == _refused("bad_request")
    assert database.executed == [] and graph.seen == []


@pytest.mark.parametrize("pairing", [
    {**PAIRING, "method": "value_overlap"},
    {**PAIRING, "basis": "column_name_pattern"},
    {**PAIRING, "method": "onboarding-llm"},
    {**PAIRING, "to_table": "s_cell.jobs", "to_field": "job_id"},
    None,
])
def test_a_join_that_is_not_a_stored_per_item_pairing_of_two_own_tables_is_refused(
        database, pairing):
    graph = _Graph(pairing=() if pairing is None else (pairing,))
    reply, _rd = _answer(_raw(), graph=graph)
    assert reply == _refused("no_stored_pairing") and database.executed == []


def test_two_stored_rows_under_one_id_are_refused(database):
    reply, _rd = _answer(_raw(), graph=_Graph(pairing=(PAIRING, PAIRING)))
    assert reply == _refused("no_stored_pairing") and database.executed == []


@pytest.mark.parametrize("endpoints, reason", [
    ([ENDPOINTS[0]], "no_form_endpoints"),                                      # one side
    ([*ENDPOINTS, _exact("jobs", "operator_ref", "AAA-999")], "no_form_endpoints"),  # two on a table
    ([ENDPOINTS[0], {**ENDPOINTS[1], "provenance": "measured earlier"}], "no_form_endpoints"),
    ([ENDPOINTS[0], {**ENDPOINTS[1], "transform": "pattern:a->b"}], "no_form_endpoints"),
    ([*ENDPOINTS, _exact("jobs", "job_code", "AA-99-9", peer="other-sys")],
     "ambiguous_form_endpoints"),                                                 # two shapes
    ([*ENDPOINTS, _exact("jobs", "operator_ref", JOB_SHAPE, peer="other-sys"),
      _exact("builds", "build_code", BUILD_SHAPE, peer="other-sys")], "ambiguous_form_endpoints"),
    # The same two columns, measured against two peers, record two different
    # concepts for one form.
    ([*ENDPOINTS, _exact("jobs", "job_code", JOB_SHAPE, peer="other-sys",
                         related="production-order"),
      _exact("builds", "build_code", BUILD_SHAPE, peer="other-sys")], "ambiguous_form_endpoints"),
])
def test_columns_other_than_the_measured_form_endpoints_are_refused(database, endpoints, reason):
    reply, _rd = _answer(_raw(), graph=_Graph(endpoints=endpoints))
    assert reply == _refused(reason) and database.executed == []


def _filler(n):
    """``n`` measured exact joins on tables the pairing does not join."""
    return [_exact(f"other_{i}", "code", "AA-9999") for i in range(n)]


def test_an_endpoint_read_past_the_cap_is_refused_and_reads_nothing(database):
    """Row 201 adds a second column on one of the pairing's tables for the
    same peer and concept: with the first 200 rows alone the endpoints look
    unique, so a cut read would pick a pair the full read makes ambiguous."""
    hidden = _exact("jobs", "operator_ref", "AAA-999")
    rows = [*ENDPOINTS, *_filler(fp.MAX_ENDPOINT_JOINS - len(ENDPOINTS)), hidden]
    assert len(rows) == fp.MAX_ENDPOINT_JOINS + 1
    first_only, _rd = _answer(_raw(), graph=_Graph(endpoints=rows[:fp.MAX_ENDPOINT_JOINS]))
    assert first_only["status"] == "ok"
    database.executed.clear()
    reply, graph = _answer(_raw(), graph=_Graph(endpoints=rows))
    assert reply == _refused("endpoint_limit") and database.executed == []
    assert [c for c, _p in graph.seen] == [fp.PAIRING_CYPHER, fp.ENDPOINTS_CYPHER]


def test_the_endpoint_read_asks_for_one_row_past_the_cap():
    assert f"LIMIT {fp.MAX_ENDPOINT_JOINS + 1}" in fp.ENDPOINTS_CYPHER
    assert f"LIMIT {fp.MAX_CONCEPT_ROWS + 1}" in fp.CONCEPTS_CYPHER


# --------------------------------------------------------------------------- #
# Concepts: the graph's own mappings against the catalog's allowlist
# --------------------------------------------------------------------------- #
def _with(concepts_replaced=None, extra=()):
    rows = [dict(r) for r in CONCEPTS]
    for i, row in (concepts_replaced or {}).items():
        rows[i] = row
    return _Graph(concepts=[*rows, *extra])


def test_the_read_looks_up_the_four_columns_stored_concepts(database):
    _reply, graph = _answer(_raw())
    params = next(p for c, p in graph.seen if c == fp.CONCEPTS_CYPHER)
    assert params == {"sid": SID, "fields": sorted([
        f"{SID}.builds.build_code", f"{SID}.builds.build_id", f"{SID}.jobs.build_id",
        f"{SID}.jobs.job_code"])}


@pytest.mark.parametrize("graph, reason", [
    # An endpoint whose name says nothing personal maps to a person concept.
    (_with({0: _mapped("jobs", "job_code", "personnel")}), "person_concept"),
    # A key column maps to a person concept as well as its key concept.
    (_with(extra=[_mapped("jobs", "build_id", "operator", role="correlate")]), "person_concept"),
    # Retired or not, a person concept refuses.
    (_with(extra=[_mapped("builds", "build_code", "operator", retired=True)]), "person_concept"),
    # A core concept off the allowlist, on an endpoint or on a key column.
    (_with({0: _mapped("jobs", "job_code", "customer")}), "concept_not_allowed"),
    (_with(extra=[_mapped("jobs", "build_id", "customer", role="correlate")]),
     "concept_not_allowed"),
    (_with({1: _mapped("builds", "build_code", "throughput")}), "concept_not_allowed"),
    # Records that can carry a counterparty reference are off the list too.
    *[(_with({0: _mapped("jobs", "job_code", concept)}), "concept_not_allowed")
      for concept in ("purchase-order", "sales-order", "shipment", "spare-part", "material",
                      "product", "asset", "equipment-id")],
    (_with({3: _mapped("builds", "build_id", "purchase-order")}), "concept_not_allowed"),
    # A column with no stored mapping (both key columns: one alone inherits,
    # below), a custom concept, only a measure role.
    (_with({2: _mapped("other", "x", "production-run"), 3: _mapped("other", "y", "production-run")}),
     "concept_unknown"),
    (_with({1: _mapped("builds", "build_code", "badge-ref")}), "concept_unknown"),
    (_with({3: _mapped("builds", "build_id", "production-run", role="measure")}),
     "concept_unknown"),
    (_with({0: _mapped("jobs", "job_code", "work-order", retired=True)}), "concept_unknown"),
    (_with(extra=[{"field": f"{SID}.jobs.job_code", "concept": None, "role": "identifier"}]),
     "concept_unknown"),
])
def test_a_person_or_unknown_column_concept_is_refused_and_reads_nothing(database, graph,
                                                                        reason):
    reply, _rd = _answer(_raw(), graph=graph)
    assert reply == _refused(reason) and database.executed == []


def _without(*indexes, extra=()):
    """:data:`CONCEPTS` without the rows at ``indexes``, plus ``extra``."""
    return _Graph(concepts=[*(r for i, r in enumerate(CONCEPTS) if i not in indexes), *extra])


@pytest.mark.parametrize("unmapped", [2, 3])
def test_an_unmapped_key_column_inherits_its_join_partners_concept(database, unmapped):
    """Onboarding is told to leave a random-UUID surrogate key unmapped, so one
    side of the stored key join often has no mapping (a fresh workshop seat,
    2026-11-11: ``production_runs.production_run_id``). The two key columns
    name one item, so the unmapped one takes its partner's mapping."""
    reply, _graph = _answer(_raw(), graph=_without(unmapped))
    assert reply["status"] == "ok" and reply["pairs"] == [["JB-4471-001", "BLD00412"]]
    assert len(database.executed) == 1


@pytest.mark.parametrize("partner, reason", [
    # The partner's mapping still passes every check of its own.
    (_mapped("jobs", "build_id", "operator", role="correlate"), "person_concept"),
    (_mapped("jobs", "build_id", "customer", role="correlate"), "concept_not_allowed"),
    (_mapped("jobs", "build_id", "production-run", role="measure"), "concept_unknown"),
    (_mapped("jobs", "build_id", "badge-ref"), "concept_unknown"),
])
def test_an_unmapped_key_inherits_nothing_its_partner_could_not_pass(database, partner,
                                                                     reason):
    reply, _graph = _answer(_raw(), graph=_without(2, 3, extra=[partner]))
    assert reply == _refused(reason) and database.executed == []


def test_a_key_column_with_any_stored_row_is_not_unmapped(database):
    """Only a column with no row at all inherits: a retired mapping keeps the
    column's own (empty) live mappings and refuses."""
    retired = _mapped("builds", "build_id", "production-run", retired=True)
    reply, _graph = _answer(_raw(), graph=_without(3, extra=[retired]))
    assert reply == _refused("concept_unknown") and database.executed == []


def test_an_endpoint_column_never_inherits(database):
    """The form endpoints must map their own form concept; only key columns inherit."""
    reply, _graph = _answer(_raw(), graph=_without(1))
    assert reply == _refused("concept_unknown") and database.executed == []


#: A stored pairing whose key on one side is also that side's form endpoint
#: (``jobs.build_id``): the column the read returns is a key column too.
_OVERLAP_ENDPOINTS = (fp.Endpoint("jobs", "build_id", JOB_SHAPE, JOIN_CONCEPT, "work-order"),
                      fp.Endpoint("builds", "build_code", BUILD_SHAPE, JOIN_CONCEPT,
                                  "production-run"))
_OVERLAP_KEYS = (("jobs", "build_id"), ("builds", "build_id"))


def test_a_returned_column_that_is_also_a_key_never_inherits():
    """An unmapped endpoint that is also one pairing key would take the
    partner key's mapping (here one the endpoint's form accepts) and then be
    returned. It refuses instead."""
    rows = [_mapped("builds", "build_code", "production-run"),
            _mapped("builds", "build_id", "work-order")]
    assert fp.concept_check(rows, SID, _OVERLAP_ENDPOINTS, _OVERLAP_KEYS) == "concept_unknown"


def test_a_returned_key_column_is_never_read_on_an_inherited_mapping(database):
    """The same overlap through the whole request: the stored joins make the
    key column ``jobs.build_id`` the jobs table's form endpoint, it has no
    mapping, and its partner key maps a concept that form accepts. Nothing is read."""
    graph = _Graph(endpoints=[_exact("jobs", "build_id", JOB_SHAPE),
                              _exact("builds", "build_code", BUILD_SHAPE, outbound=False)],
                   concepts=[_mapped("builds", "build_code", "production-run"),
                             _mapped("builds", "build_id", "work-order")])
    reply, _graph = _answer(_raw(), graph=graph)
    assert reply == _refused("concept_unknown") and database.executed == []


def test_a_returned_key_column_with_its_own_mapping_still_passes():
    rows = [_mapped("jobs", "build_id", "work-order"),
            _mapped("builds", "build_code", "production-run"),
            _mapped("builds", "build_id", "production-run")]
    assert fp.concept_check(rows, SID, _OVERLAP_ENDPOINTS, _OVERLAP_KEYS) is None


def _endpoint_case(table, column, concept=None, related=None):
    """A graph and schema whose measured endpoint on ``table`` is ``column``
    as the graph spells it, stored as an identifier of ``concept`` (the
    form's own concept by default); the other three columns as in
    :data:`CONCEPTS`. The schema holds the name lowercased, as
    ``parse_physical_schema`` gives it."""
    index, shape = (0, JOB_SHAPE) if table == "jobs" else (1, BUILD_SHAPE)
    endpoints, concepts = list(ENDPOINTS), [dict(r) for r in CONCEPTS]
    endpoints[index] = _exact(table, column, shape, outbound=index == 0, related=related)
    concepts[index] = _mapped(table, column.lower(), concept or FORM_CONCEPT[table])
    physical = {name: {"columns": {**spec["columns"], column.lower(): "text"}}
                for name, spec in PHYSICAL.items()}
    return _Graph(endpoints=endpoints, concepts=concepts), physical


def _ask_endpoint(table, column, concept=None, related=None):
    graph, physical = _endpoint_case(table, column, concept, related)
    value = "JB-4471-001" if table == "jobs" else "BLD00412"
    reply, _rd = _answer(_raw((value,)), graph=graph, physical=physical)
    return reply


@pytest.mark.parametrize("table, column, concept", [
    # snake_case and every other spelling of the catalog's person-valued
    # fields, under the form's own concept or any other.
    ("jobs", "assigned_to", None), ("jobs", "wo_assigned_to", None),
    ("jobs", "operator_badge", "shift"), ("jobs", "requested_by", "engineering-change"),
    ("builds", "approved_by", "engineering-change"), ("builds", "customer_id", None),
    ("jobs", "operatorBadge", None), ("jobs", "assignedTo", None),
    ("jobs", "requestedBy", None), ("builds", "approvedBy", None),
    ("builds", "customerId", None), ("builds", "CustomerID", None),
    ("jobs", "supplier_id", None), ("jobs", "vendorId", None),
    ("jobs", "supplier", None), ("jobs", "supplier_ref", None), ("builds", "VendorRef", None),
    # A supplier column mapped only to a record that carries suppliers.
    ("jobs", "supplier_id", "purchase-order"), ("jobs", "supplier", "spare-part"),
])
def test_a_column_named_for_a_person_or_counterparty_is_refused_and_reads_nothing(
        database, table, column, concept):
    from agents import pii
    assert not pii.column_looks_pii(column)
    assert _ask_endpoint(table, column, concept) == _refused("person_field")
    assert database.executed == []


@pytest.mark.parametrize("table, column, concept", [
    # Off the allowlist whatever the column is named.
    ("jobs", "account_ref", "customer"), ("jobs", "shift_code", "shift"),
    ("builds", "change_ref", "engineering-change"), ("jobs", "po_ref", "purchase-order"),
    ("builds", "part_ref", "spare-part"),
])
def test_a_column_off_the_allowlist_is_refused_and_reads_nothing(database, table, column,
                                                                 concept):
    assert _ask_endpoint(table, column, concept) == _refused("concept_not_allowed")
    assert database.executed == []


@pytest.mark.parametrize("key", ["assigned_to", "assignedTo"])
def test_an_assignee_key_column_under_a_work_order_is_refused(database, key):
    column = key.lower()
    physical = {"jobs": {"columns": {**PHYSICAL["jobs"]["columns"], column: "text"}},
                "builds": {"columns": {**PHYSICAL["builds"]["columns"], column: "text"}}}
    pairing = {**PAIRING, "from_field": key, "to_field": key}
    concepts = [*CONCEPTS[:2], _mapped("jobs", column, "work-order"),
                _mapped("builds", column, "work-order")]
    reply, _rd = _answer(_raw(), graph=_Graph(pairing=(pairing,), concepts=concepts),
                         physical=physical)
    assert reply == _refused("person_field") and database.executed == []


# --------------------------------------------------------------------------- #
# Each endpoint's own concept is exactly its form's
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("graph, reason", [
    # Another allowed concept than the one its exact join records for it.
    (_with({0: _mapped("jobs", "job_code", "production-run")}), "concept_mismatch"),
    (_with({1: _mapped("builds", "build_code", "work-order")}), "concept_mismatch"),
    (_with({0: _mapped("jobs", "job_code", "production-order")}), "concept_mismatch"),
    # The form's concept plus a second live mapping.
    (_with(extra=[_mapped("jobs", "job_code", "batch", role="correlate")]), "concept_mismatch"),
    (_with(extra=[_mapped("builds", "build_code", "production-order", role="measure")]),
     "concept_mismatch"),
])
def test_an_endpoint_mapped_to_another_concept_than_its_forms_is_refused(database, graph,
                                                                         reason):
    reply, _rd = _answer(_raw(), graph=graph)
    assert reply == _refused(reason) and database.executed == []


def test_an_endpoint_on_a_shared_concept_join_must_map_that_concept(database):
    """No catalog-related label: the join's concept is the form's, so a
    column mapped to another concept is refused."""
    endpoints = [_exact("jobs", "job_code", JOB_SHAPE, related=""), ENDPOINTS[1]]
    reply, _rd = _answer(_raw(), graph=_Graph(endpoints=endpoints))
    assert reply == _refused("concept_mismatch") and database.executed == []
    concepts = [_mapped("jobs", "job_code", JOIN_CONCEPT), *CONCEPTS[1:]]
    reply, _rd = _answer(_raw(), graph=_Graph(endpoints=endpoints, concepts=concepts))
    assert reply["status"] == "ok" and len(database.executed) == 1


@pytest.mark.parametrize("related, reason", [
    ("lot-number", "concept_unknown"),        # a pair the catalog does not declare
    ("shipment", "concept_not_allowed"),      # a label off the allowlist
    ("operator", "person_concept"),
    ("badge-ref", "concept_unknown"),
])
def test_a_forms_label_the_catalog_does_not_allow_is_refused(database, related, reason):
    reply = _ask_endpoint("jobs", "job_code", related, related=related)
    assert reply == _refused(reason) and database.executed == []


@pytest.mark.parametrize("concept, reason", [
    ("personnel", "person_concept"), ("operator", "person_concept"),
    ("customer", "concept_not_allowed"), ("shift", "concept_not_allowed"),
    ("engineering-change", "concept_not_allowed"), ("supplier", "concept_not_allowed"),
    ("purchase-order", "concept_not_allowed"), ("spare-part", "concept_not_allowed"),
    ("badge-ref", "concept_unknown"), ("", "concept_unknown"),
])
def test_a_join_concept_off_the_allowlist_is_refused(database, concept, reason):
    endpoints = [_exact("jobs", "job_code", JOB_SHAPE, concept=concept),
                 _exact("builds", "build_code", BUILD_SHAPE, outbound=False, concept=concept)]
    if not concept:
        endpoints = [{**e, "provenance": e["provenance"].replace("concept ; ", "")}
                     for e in endpoints]
    reply, _rd = _answer(_raw(), graph=_Graph(endpoints=endpoints))
    assert reply == _refused(reason) and database.executed == []


def test_a_measured_person_identifier_join_is_refused_though_no_name_says_so(database):
    """The review's case: a measured exact join on a person identifier whose
    column and values pass every name and value check."""
    from agents import catalog, pii
    physical = {"jobs": {"columns": {**PHYSICAL["jobs"]["columns"], "staff_ref": "text"}},
                "builds": PHYSICAL["builds"]}
    endpoints = [_exact("jobs", "staff_ref", "AAA-9999", concept="personnel"),
                 _exact("builds", "build_code", BUILD_SHAPE, outbound=False,
                        concept="personnel")]
    concepts = [_mapped("jobs", "staff_ref", "personnel"), *CONCEPTS[1:]]
    assert not pii.column_looks_pii("staff_ref") and not catalog.person_valued_field("staff_ref")
    assert fp.safe_value("EMP-1234", "AAA-9999")
    reply, _rd = _answer(_raw(("EMP-1234",)), graph=_Graph(endpoints=endpoints,
                                                         concepts=concepts),
                         physical=physical)
    assert reply == _refused("person_concept") and database.executed == []


def test_too_many_concept_rows_are_refused(database):
    extra = [_mapped("jobs", "job_code", "work-order", role="identifier")
             for _ in range(fp.MAX_CONCEPT_ROWS)]
    reply, _rd = _answer(_raw(), graph=_with(extra=extra))
    assert reply == _refused("concept_limit") and database.executed == []


def test_the_name_check_still_refuses_as_an_extra_layer(database):
    endpoints = [_exact("jobs", "operator_email", JOB_SHAPE), ENDPOINTS[1]]
    concepts = [_mapped("jobs", "operator_email", "work-order"), *CONCEPTS[1:]]
    reply, _rd = _answer(_raw(), graph=_Graph(endpoints=endpoints, concepts=concepts))
    assert reply == _refused("column_policy") and database.executed == []


#: Concepts taken off the crossing (they can carry counterparty references or
#: are not plant-generated production records); back only with a
#: classification source.
REMOVED = ("purchase-order", "sales-order", "shipment", "spare-part", "material", "product",
           "asset", "equipment-id")


def test_the_crossing_allowlist_is_the_catalogs_own():
    """Pinned: a concept joins the list only by a deliberate catalog change."""
    from agents import catalog
    assert catalog.CROSSABLE_CONCEPTS == {
        "production-run", "production-order", "work-order", "batch", "lot-number"}
    assert catalog.CROSSABLE_CONCEPTS <= set(catalog.CORE_CONCEPT_DEFINITIONS)
    assert catalog.PERSON_CONCEPTS == {"operator", "personnel"}
    for concept in catalog.PERSON_CONCEPTS:
        assert catalog.comparable_concepts(concept) <= catalog.PERSON_CONCEPTS
        assert concept not in catalog.CROSSABLE_CONCEPTS
    for concept in (JOIN_CONCEPT, *FORM_CONCEPT.values(), "lot-number", "production-order"):
        assert catalog.crossable_concept(concept)
    for concept in ("operator", "personnel", "customer", "supplier", "shift",
                    "engineering-change", "maintenance-event", *REMOVED, "badge-ref", "",
                    None, ["batch"]):
        assert not catalog.crossable_concept(concept)


def test_every_catalog_related_pair_is_crossable():
    """A form's label can only name a declared pair of production records."""
    from agents import catalog
    for concept, declaring in catalog.RELATED_IDENTIFIER_CONCEPTS:
        assert catalog.crossable_concept(concept) and catalog.crossable_concept(declaring)


def test_a_person_concept_on_the_allowlist_is_still_refused(monkeypatch):
    """The second check: a list edit that let a person concept in is not enough."""
    from agents import catalog
    monkeypatch.setattr(catalog, "CROSSABLE_CONCEPTS",
                        catalog.CROSSABLE_CONCEPTS | {"personnel"})
    assert not catalog.crossable_concept("personnel")
    form = fp.Endpoint("jobs", "job_code", JOB_SHAPE, JOIN_CONCEPT, "work-order")
    rows = [_mapped("jobs", "job_code", "personnel")]
    assert fp.concept_check(rows, SID, (form,), ()) == "person_concept"
    person_join = fp.Endpoint("jobs", "job_code", JOB_SHAPE, "personnel")
    assert fp.concept_check([], SID, (person_join,), ()) == "person_concept"


def test_the_person_valued_fields_are_the_catalogs_own_vocabulary():
    from agents import catalog
    fields = catalog.PERSON_VALUED_FIELDS
    for concept in (*catalog.PERSON_CONCEPTS, "customer"):
        assert catalog.definition_fields(concept) and catalog.definition_fields(concept) <= fields
    assert {"assigned_to", "assigned_team", "operator_badge", "requested_by", "approved_by",
            "supplier", "supplier_id", "supplier_name", "vendor", "vendor_id",
            "vendor_number", "customer"} <= fields
    vocabulary = [catalog.field_words(f) for c in catalog.CORE_CONCEPT_DEFINITIONS
                  for f in catalog.definition_fields(c)]
    for entry in fields:
        assert any(catalog.word_run(catalog.field_words(entry), v) for v in vocabulary), entry


@pytest.mark.parametrize("name, words", [
    ("operatorBadge2", ("operator", "badge", "2")), ("CustomerID", ("customer", "id")),
    ("HTTPRequestedBy", ("http", "requested", "by")), ("wo-assigned.to", ("wo", "assigned", "to")),
    ("VENDOR_ID", ("vendor", "id")), ("", ()), (None, ()),
])
def test_column_names_are_split_into_words(name, words):
    from agents import catalog
    assert catalog.field_words(name) == words


@pytest.mark.parametrize("name, expected", [
    *[(n, True) for n in (
        "assigned_to", "wo_assigned_to", "jobs.approved_by", "Operator_Badge", "operatorBadge",
        "OperatorBadge", "assignedTo", "requestedBy", "approvedBy", "customerId", "customerID",
        "supplier_id", "supplierRef", "supplier", "vendorId", "VENDOR_ID", "vendor_ref",
        "operator-badge", "operatorBadge2", "assigned.to", "technicianCode")],
    *[(n, False) for n in (
        "job_code", "build_id", "assigned_total", "AssignedTotal", "unassigned", "run_number",
        "supplied_qty", "vendorless", "work_order_number", "", None)],
])
def test_the_person_field_match_is_by_whole_words(name, expected):
    from agents import catalog
    assert catalog.person_valued_field(name) is expected


def test_the_forms_concepts_round_trip_through_the_exact_pass_reasoning():
    """The exact pass writes the agreed concept and, for a catalog-related
    pair, the form's own concept; ``exact_concepts_from_reasoning`` reads
    both back."""
    source = ia.SampledField(SID, "jobs", "job_code", "work-order", ("JB-4471-001",))
    target = ia.SampledField(HOLDER, "log", "unit_ref", "batch", ("JB-4471-001",))
    _concept, label = ia.agreed_concept(source, target)
    text = ia._exact_reasoning(source, target, JOB_SHAPE, {"JB-4471-001"}, label)
    assert ia.exact_concepts_from_reasoning(text) == ("batch", "work-order")
    assert ia.exact_concept_from_reasoning(text) == "batch"
    shared = ia._exact_reasoning(source, source, JOB_SHAPE, {"JB-4471-001"}, "work-order")
    assert ia.exact_concepts_from_reasoning(shared) == ("work-order", "")
    for text in ("joined by name, concept work-order", None):
        assert ia.exact_concepts_from_reasoning(text) is None


def test_a_value_of_neither_measured_shape_is_refused(database):
    reply, _rd = _answer(_raw(("JB-4471-001", "XYZ-1")))
    assert reply == _refused("value_shape") and database.executed == []


# --------------------------------------------------------------------------- #
# The query path's scope, and the extra name and value layers
# --------------------------------------------------------------------------- #
def test_an_endpoint_the_schema_does_not_hold_is_refused(database):
    physical = {"jobs": PHYSICAL["jobs"], "builds": {"columns": {"build_id": "uuid"}}}
    reply, _rd = _answer(_raw(), physical=physical)
    assert reply == _refused("column_policy") and database.executed == []


def test_a_personal_data_endpoint_is_refused(database):
    endpoints = [_exact("jobs", "operator_email", JOB_SHAPE), ENDPOINTS[1]]
    reply, _rd = _answer(_raw(), graph=_Graph(endpoints=endpoints))
    assert reply == _refused("column_policy") and database.executed == []


def test_a_system_with_a_declared_row_exclusion_is_refused(database, monkeypatch):
    from agents import row_exclusion
    monkeypatch.setattr(row_exclusion, "has_exclusions", lambda system_id: system_id == SID)
    reply, _rd = _answer(_raw())
    assert reply == _refused("column_policy") and database.executed == []


@pytest.mark.parametrize("row", [
    ("JB-4471-001", "BLD0041"),          # not the measured shape
    ("JB-4471-001", "BLD00412 x"),       # not the identifier grammar
    ("JB-4471-001", None),
])
def test_a_returned_value_outside_its_measured_shape_is_dropped_and_counted(database, row):
    database.rows = [("JB-4471-002", "BLD00413"), row]
    reply, _rd = _answer(_raw(("JB-4471-001", "JB-4471-002")))
    assert reply["pairs"] == [["JB-4471-002", "BLD00413"]] and reply["dropped"] == 1


def test_a_value_the_redaction_would_change_is_dropped(database, monkeypatch):
    from agents import pii
    monkeypatch.setattr(pii, "redact_pii", lambda text: "[REDACTED]" if text == "BLD00412"
                        else text)
    reply, _rd = _answer(_raw())
    assert reply["pairs"] == [] and reply["dropped"] == 1


def test_a_read_past_the_cap_says_it_was_cut_and_rows_are_distinct(database):
    database.rows = [("JB-4471-001", f"BLD{i:05d}") for i in range(fp.MAX_PAIRS + 1)]
    reply, _rd = _answer(_raw())
    assert reply["cut"] is True and len(reply["pairs"]) == fp.MAX_PAIRS


def test_a_failed_read_is_reported_by_type_and_never_raised(database):
    database.rows = RuntimeError("secret detail")
    reply, _rd = _answer(_raw())
    assert reply == {"engine": "pair_forms", "v": 2, "status": "failed", "error": "RuntimeError"}


def test_only_postgresql_systems_answer(database):
    reply, graph = _answer(_raw(), connection_type="Athena")
    assert reply["status"] == "unsupported" and graph.seen == [] and database.executed == []


def test_the_request_round_trips():
    raw = fp.request_arg("jp-1", ["A-1", "A-1", "A-2"])
    assert fp.parse_request(raw) == fp.Request("jp-1", ("A-1", "A-2"))


# --------------------------------------------------------------------------- #
# The runtime's ASGI seam: the engine's call shape only
# --------------------------------------------------------------------------- #
def _message(arguments, name=BARE_TOOL, rpc_id=1):
    return {"jsonrpc": "2.0", "method": "tools/call", "id": rpc_id,
            "params": {"name": name, "arguments": arguments}}


def _engine_call(rpc_id=1, name=BARE_TOOL):
    return _message({"question": fp.PAIRING_QUESTION, fp.RESERVED_ARG: _raw()}, name, rpc_id)


def test_the_admitted_name_is_the_one_this_runtime_registered():
    assert fp.query_tool_name(SID) == BARE_TOOL
    fp.register_query_tool(SID, PREFIXED_TOOL)
    assert fp.query_tool_name(SID) == PREFIXED_TOOL
    assert fp.query_tool_name("cell-y") is None and fp.query_tool_name(None) is None


@pytest.mark.parametrize("registered_name, other_spelling", [
    (BARE_TOOL, PREFIXED_TOOL),    # the cloud entry point: no prefix
    (PREFIXED_TOOL, BARE_TOOL),    # local mode: the gateway's prefix emulated
])
def test_each_spelling_is_admitted_only_against_its_own_registration(registered_name,
                                                                     other_spelling):
    from agents import runtime_asgi as ra
    fp.register_query_tool(SID, registered_name)
    deny, _rid, _changed = ra._process_one_mcp_message(_engine_call(name=registered_name), SID)
    assert deny is None and ra.pair_forms_var.get() == _raw()
    deny, _rid, _changed = ra._process_one_mcp_message(_engine_call(name=other_spelling), SID)
    assert deny == fp.SHAPE_REFUSAL and ra.pair_forms_var.get() is None


def test_a_runtime_that_registered_no_query_tool_admits_no_spelling(monkeypatch):
    from agents import runtime_asgi as ra
    monkeypatch.setattr(fp, "_QUERY_TOOLS", {})
    for name in (BARE_TOOL, PREFIXED_TOOL):
        deny, _rid, _changed = ra._process_one_mcp_message(_engine_call(name=name), SID)
        assert deny == fp.SHAPE_REFUSAL and ra.pair_forms_var.get() is None


def test_the_engines_call_shape_is_admitted():
    from agents import runtime_asgi as ra
    msg = _message({"question": fp.PAIRING_QUESTION, fp.RESERVED_ARG: _raw()})
    deny, _rid, changed = ra._process_one_mcp_message(msg, SID)
    assert deny is None and changed and ra.pair_forms_var.get() == _raw()
    assert msg["params"]["arguments"] == {"question": fp.PAIRING_QUESTION}
    ra._process_one_mcp_message(_message({"question": "q"}), SID)
    assert ra.pair_forms_var.get() is None


@pytest.mark.parametrize("arguments", [
    {"question": "Which units drew coil CL-77?", fp.RESERVED_ARG: _raw()},
    {"question": fp.PAIRING_QUESTION, "kg_context": "ROUTE:", fp.RESERVED_ARG: _raw()},
    {"question": fp.PAIRING_QUESTION, fp.RESERVED_ARG: {"v": 2}},
    {"question": fp.PAIRING_QUESTION, fp.RESERVED_ARG: ""},
    {fp.RESERVED_ARG: _raw()},
])
def test_any_other_call_carrying_the_reserved_argument_is_refused(arguments):
    from agents import runtime_asgi as ra
    deny, _rid, _changed = ra._process_one_mcp_message(_message(dict(arguments)), SID)
    assert deny == fp.SHAPE_REFUSAL and ra.pair_forms_var.get() is None


@pytest.mark.parametrize("name", [
    "query_other_system", "describe_schema", "cell-x___query_cell_x", "query_cell_x_", None,
])
def test_the_engines_shape_on_any_other_tool_is_refused(name):
    from agents import runtime_asgi as ra
    deny, _rid, _changed = ra._process_one_mcp_message(_engine_call(name=name), SID)
    assert deny == fp.SHAPE_REFUSAL and ra.pair_forms_var.get() is None


def _body(messages) -> bytes:
    return json.dumps(messages).encode()


@pytest.mark.parametrize("batch", [
    [_engine_call(1), _message({"question": "Which builds shipped?"}, rpc_id=2)],
    [_message({"question": "Which builds shipped?"}, rpc_id=1), _engine_call(2)],
    [_engine_call(1), _engine_call(2)],
    [_engine_call(1)],
])
def test_a_batch_carrying_the_pairing_argument_is_refused_whole(batch):
    from agents import runtime_asgi as ra
    ra.pair_forms_var.set("left over")
    raw = _body(batch)
    body, deny, rpc_id = ra._pop_identity_from_mcp_body(raw, SID)
    assert deny == fp.SHAPE_REFUSAL and body == raw
    carrier = next(m for m in batch if fp.RESERVED_ARG in m["params"]["arguments"])
    assert rpc_id == carrier["id"] and ra.pair_forms_var.get() is None


def test_a_batch_without_it_is_processed_as_before():
    from agents import runtime_asgi as ra
    raw = _body([_message({"question": "a"}, rpc_id=1), _message({"question": "b"}, rpc_id=2)])
    body, deny, _rid = ra._pop_identity_from_mcp_body(raw, SID)
    assert deny is None and body == raw and ra.pair_forms_var.get() is None


def test_one_engine_call_per_post_is_admitted():
    from agents import runtime_asgi as ra
    body, deny, _rid = ra._pop_identity_from_mcp_body(_body(_engine_call()), SID)
    assert deny is None and ra.pair_forms_var.get() == _raw()
    assert fp.RESERVED_ARG not in json.loads(body)["params"]["arguments"]


def test_the_identity_policy_still_runs_first(monkeypatch):
    from agents import runtime_asgi as ra
    monkeypatch.setenv(ra.REQUIRED_GROUP_ENV, "plant-ops")
    monkeypatch.delenv(ra.IDENTITY_CLAIM_SECRET_ARN_ENV, raising=False)
    msg = _message({"question": fp.PAIRING_QUESTION, fp.RESERVED_ARG: _raw()})
    deny, _rid, _changed = ra._process_one_mcp_message(msg, SID)
    assert deny == ra.IDENTITY_DENY_KEY_UNCONFIGURED.format(system=SID)
    assert ra.pair_forms_var.get() is None


# --------------------------------------------------------------------------- #
# Through the runtime's public MCP request path (FastMCP + the ASGI seam)
# --------------------------------------------------------------------------- #
CONN = {"host": "localhost", "port": 5432, "dbname": "cell", "user": "reader",
        "password": "test-only"}  # nosec B105 - never connected


def _tables_desc():
    from agents import schema_format
    return schema_format.format_tables_desc({
        "jobs": [("job_id", "uuid"), ("job_code", "text"), ("build_id", "uuid")],
        "builds": [("build_id", "uuid"), ("build_code", "text")]})


def _fake_agent(monkeypatch) -> list:
    """Fake the Mode A agent; returns the questions it was asked."""
    from agents import system_agent as sa
    ran = []

    async def _agent_ran(agent, system_id, question, kg_context="", schema=""):
        ran.append(question)
        return "the agent answered"
    monkeypatch.setattr(sa, "_build_mode_a_agent", lambda *a, **k: object())
    monkeypatch.setattr(sa, "_run_mode_a_query", _agent_ran)
    return ran


def _served_tool_names(mcp) -> list:
    import asyncio
    return [tool.name for tool in asyncio.run(mcp.list_tools())]


@pytest.mark.parametrize("prefix", ["", "cell-x___"])
def test_the_mode_a_server_registers_the_name_it_serves(monkeypatch, prefix):
    from agents import system_agent as sa
    monkeypatch.setattr(fp, "_QUERY_TOOLS", {})
    _fake_agent(monkeypatch)
    mcp = sa._create_mode_a_mcp_server(SID, "Cell X", "s_cell", "", _tables_desc(), CONN,
                                       "PostgreSQL", None, prefix, "127.0.0.1", 0)
    assert _served_tool_names(mcp) == [f"{prefix}{BARE_TOOL}"]
    assert fp.query_tool_name(SID) == f"{prefix}{BARE_TOOL}"


def test_the_cloud_entry_points_call_registers_the_bare_name(monkeypatch):
    """``runtime_app._run_system_agent`` calls ``create_mcp_server`` with no
    ``tool_name_prefix`` (its default is ``""``), so the served runtime
    registers ``query_<safe id>`` and the gateway adds ``<target>___``."""
    from agents import system_agent as sa
    monkeypatch.setattr(fp, "_QUERY_TOOLS", {})
    monkeypatch.delenv("SYSTEM_QUERY_MODE", raising=False)
    _fake_agent(monkeypatch)
    mcp = sa.create_mcp_server(system_id=SID, system_name="Cell X", schema="s_cell",
                               system_prompt="", tables_desc=_tables_desc(),
                               connection_type="PostgreSQL", connection_config=None,
                               conn_params=CONN, port=8000)
    assert _served_tool_names(mcp) == [BARE_TOOL] and fp.query_tool_name(SID) == BARE_TOOL


def test_local_mode_registers_the_prefixed_name(monkeypatch):
    """Local mode (``AgentManager._start_local``, no gateway) emulates the
    gateway's ``<target>___`` prefix, so that is the name it registers."""
    from agents import agent_manager as am
    import agents.business_context as bc
    monkeypatch.setattr(fp, "_QUERY_TOOLS", {})
    monkeypatch.delenv("SYSTEM_QUERY_MODE", raising=False)
    monkeypatch.setattr(bc, "read_business_context", lambda _sid: None)
    _fake_agent(monkeypatch)
    served = []

    class _Built:
        def __init__(self, _sid, _host, _port, build, _stop_wait):
            self.build = build

        def start(self, _timeout):
            served.append(self.build())
    monkeypatch.setattr(am, "LocalAgentServer", _Built)
    manager = am.AgentManager(mode="local")
    monkeypatch.setattr(manager, "_port_is_free", lambda _port: True)   # nothing binds
    manager._start_local(
        SID, "Cell X", "s_cell", "", _tables_desc(), dict(CONN))
    assert _served_tool_names(served[0]) == [PREFIXED_TOOL]
    assert fp.query_tool_name(SID) == PREFIXED_TOOL


def _serve(monkeypatch, prefix=""):
    """The assembled system runtime registering ``<prefix>query_cell_x``; the
    agent and the graph are faked."""
    from starlette.testclient import TestClient

    from agents import runtime_asgi as ra
    from agents import system_agent as sa
    ran = _fake_agent(monkeypatch)
    graph = _Graph()
    monkeypatch.setattr(fp, "_graph_read", graph)
    # The cloud entry point's default host (no Host-header check); in-process
    # ASGI only, nothing listens.
    mcp = sa._create_mode_a_mcp_server(SID, "Cell X", "s_cell", "", _tables_desc(), CONN,
                                       "PostgreSQL", None, prefix, "0.0.0.0", 0)  # nosec B104 - never served

    def call(arguments, name=f"{prefix}{BARE_TOOL}", rpc=None):
        rpc = rpc or {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                      "params": {"name": name, "arguments": arguments}}
        with TestClient(ra._assemble_mcp_asgi(mcp, SID)) as tc:
            resp = tc.post("/mcp/", content=json.dumps(rpc), headers={
                "accept": "application/json, text/event-stream",
                "content-type": "application/json"})
        assert resp.status_code == 200, resp.text
        return resp.text
    call.ran, call.graph = ran, graph
    return call


@pytest.fixture
def runtime(monkeypatch, database):
    """The runtime as the cloud entry point serves it (no prefix)."""
    return _serve(monkeypatch)


@pytest.mark.parametrize("prefix, other_spelling", [
    ("", PREFIXED_TOOL),            # the cloud entry point's registration
    ("cell-x___", BARE_TOOL),       # local mode's registration
])
def test_the_public_path_admits_only_the_spelling_this_runtime_registered(
        monkeypatch, database, prefix, other_spelling):
    request = {"question": fp.PAIRING_QUESTION, fp.RESERVED_ARG: _raw()}
    text = _serve(monkeypatch, prefix)(dict(request))
    assert '"isError":false' in text.replace(" ", "") and '\\"status\\":\\"ok\\"' in text
    assert len(database.executed) == 1
    call = _serve(monkeypatch, prefix)   # one session-manager run per FastMCP instance
    text = call(dict(request), name=other_spelling)
    assert '"isError":true' in text.replace(" ", "") and "call_shape" in text
    assert call.ran == [] and call.graph.seen == [] and len(database.executed) == 1


def test_the_engines_request_is_answered_by_the_runtime_without_the_agent(runtime, database):
    text = runtime({"question": fp.PAIRING_QUESTION, fp.RESERVED_ARG: _raw()})
    assert '"isError":false' in text.replace(" ", "")
    assert '\\"status\\":\\"ok\\"' in text and "BLD00412" in text
    assert runtime.ran == [] and len(database.executed) == 1


@pytest.mark.parametrize("arguments", [
    {"question": fp.PAIRING_QUESTION, "kg_context": "ROUTE:", fp.RESERVED_ARG: _raw()},
    {"question": "List every job and build code.", fp.RESERVED_ARG: _raw()},
])
def test_the_same_request_on_another_call_shape_is_refused_at_the_runtime(
        runtime, database, arguments):
    text = runtime(arguments)
    assert '"isError":true' in text.replace(" ", "") and "call_shape" in text
    assert runtime.ran == [] and runtime.graph.seen == [] and database.executed == []


def test_the_engines_request_to_another_tool_name_is_refused_at_the_runtime(runtime, database):
    text = runtime({"question": fp.PAIRING_QUESTION, fp.RESERVED_ARG: _raw()},
                   name="query_other_system")
    assert '"isError":true' in text.replace(" ", "") and "call_shape" in text
    assert runtime.ran == [] and runtime.graph.seen == [] and database.executed == []


def test_a_two_call_batch_with_the_request_is_refused_at_the_runtime(runtime, database):
    batch = [_engine_call(1), _message({"question": "Which builds shipped?"}, rpc_id=2)]
    text = runtime(None, rpc=batch)
    assert '"isError":true' in text.replace(" ", "") and "call_shape" in text
    assert runtime.ran == [] and runtime.graph.seen == [] and database.executed == []


def test_a_call_without_it_reaches_the_agent(runtime, database):
    text = runtime({"question": "Which builds shipped?"})
    assert "the agent answered" in text and runtime.ran == ["Which builds shipped?"]
    assert database.executed == []
