# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The identity rules a resolve emits do not depend on which system resolved
last, nor on the identifier/correlate role the mapper gave a business key whose
table holds the same concept as an identifier (ADR-0087 amendment 2026-10-18).

Rehearsal account, 2026-10-18: all seven systems were provisioned in seed
order (ERP first, then the plant MESs), and every MES carried the two exact
batch hops to the ERP, each written by the MES's own resolve. Then the ERP alone
was re-provisioned. Its provision deleted every rule touching it (by design) and
its own resolve re-created the hops for ONE MES only: the one whose mapper run
had given ``work_orders.work_order_number`` and ``production_runs.run_number``
the identifier role. The other three had mapped the surrogate keys beside them
as the identifier and the business keys as correlates of the same concepts.
Their own resolves admit such a column as an exact-only candidate (the
2026-10-02 amendment), but the ERP's resolve read peer columns with the
identifier role only, so it never considered them.

Fixtures are synthetic (``sys-*`` ids, generic plants); the graph is a stub
that returns the MAPS_TO edges whose role the query names.
"""
import logging
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))
os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.identity_affinity as aff  # noqa: E402
import agents.onboard as onboard  # noqa: E402
import agents.resolve_evidence as resolve_evidence  # noqa: E402

ERP = "sys-erp"
MES_ID = "sys-mes-a"     # the mapper gave the business keys the identifier role
MES_DEM = "sys-mes-b"    # the mapper gave the surrogates the identifier role
LEFT, RIGHT = "sys-left", "sys-right"   # generic pair for the cap and concept cases

#: Two plants, distinct numbering: each plant's work orders and runs.
_DIGIT = {MES_ID: 1, MES_DEM: 2}


def _work_orders(sid):
    d = _DIGIT[sid]
    return [f"WO-2026100{d}-9{d}{k:03d}" for k in range(8)]


def _runs(sid):
    d = _DIGIT[sid]
    return [f"RUN-2026100{d}-9{d}{k:03d}" for k in range(8)]


def _columns():
    """(system, table, column) -> sampled values. The ERP's lot-tracking rows
    carry every plant's work-order numbers and its shipment lines every plant's
    run numbers; the surrogates are 1..N."""
    out = {}
    for sid in _DIGIT:
        out[(sid, "work_orders", "work_order_number")] = _work_orders(sid)
        out[(sid, "work_orders", "work_order_id")] = [str(i) for i in range(1, 9)]
        out[(sid, "work_orders", "production_run_id")] = [str(i) for i in range(1, 9)]
        out[(sid, "production_runs", "run_number")] = _runs(sid)
        out[(sid, "production_runs", "production_run_id")] = [str(i) for i in range(1, 9)]
    out[(ERP, "lot_mes_tracking", "batch_id")] = [v for sid in _DIGIT for v in _work_orders(sid)]
    out[(ERP, "shipment_items", "batch_id")] = [v for sid in _DIGIT for v in _runs(sid)]
    return out


def _mapping(table, column, *roles):
    """One mapper row: ``roles`` = (concept, role, confidence) entries."""
    return {"table": table, "field": f"{table}.{column}", "concept": roles[0][0],
            "confidence": roles[0][2],
            "mappings": [{"concept": c, "role": r, "confidence": conf} for c, r, conf in roles]}


def _erp_mappings():
    return [_mapping("lot_mes_tracking", "batch_id", ("batch", "identifier", 0.9)),
            _mapping("shipment_items", "batch_id", ("batch", "identifier", 0.8))]


def _mes_mappings(sid):
    """The two assignments the rehearsal account held (batch-fields-now.json)."""
    if sid == MES_ID:
        return [_mapping("work_orders", "work_order_number", ("work-order", "identifier", 0.95)),
                _mapping("production_runs", "run_number", ("production-run", "identifier", 0.95))]
    return [_mapping("work_orders", "work_order_id", ("work-order", "identifier", 0.95)),
            _mapping("work_orders", "work_order_number", ("work-order", "correlate", 0.95)),
            _mapping("work_orders", "production_run_id", ("production-run", "correlate", 0.8)),
            _mapping("production_runs", "production_run_id", ("production-run", "identifier", 0.95)),
            _mapping("production_runs", "run_number", ("production-run", "correlate", 0.9))]


_MAPPINGS = {ERP: _erp_mappings, MES_ID: lambda: _mes_mappings(MES_ID),
             MES_DEM: lambda: _mes_mappings(MES_DEM)}
_ROLE_NAMED = re.compile(r"r\.role = '(\w+)'")


class _Graph:
    """The MAPS_TO edges each system's provision wrote from its mapper rows,
    read like Neptune: one row per edge of a field of another system whose role
    the query's WHERE names, with every concept of that field and each edge's
    confidence."""

    def __init__(self, mappings_of):
        self.edges = []
        for sid, mappings in mappings_of.items():
            for m in mappings:
                column = m["field"].split(".", 1)[1]
                for rm in m["mappings"]:
                    self.edges.append((sid, m["table"], column, rm["concept"], rm["role"],
                                       rm["confidence"]))
        self.reads = []

    def __call__(self, cypher, params=None):
        self.reads.append(cypher)
        roles = set(_ROLE_NAMED.findall(cypher))
        sid = (params or {}).get("sid")
        rows = []
        for s, table, column, concept, role, _conf in self.edges:
            if s == sid or role not in roles:
                continue
            same = [e for e in self.edges if e[:3] == (s, table, column)]
            rows.append({"sys_id": s, "sys_schema": f"s_{s}", "table_name": table,
                         "field_name": column, "concept": concept, "role": role,
                         "concepts": sorted({e[3] for e in same}),
                         "concept_scores": [{"concept": e[3], "confidence": e[5]} for e in same],
                         "removal_incomplete": False})
        return rows


class _Catalog:
    _KINDS = {ERP: ("ERP", "All Sites"), MES_ID: ("MES", "Plant A"), MES_DEM: ("MES", "Plant B"),
              LEFT: ("ERP", "All Sites"), RIGHT: ("MES", "Plant A")}

    def __init__(self, systems):
        self._systems = set(systems)

    def get(self, sid):
        if sid not in self._systems:
            return None
        kind, plant = self._KINDS[sid]
        return {"id": sid, "system_type": kind, "plant": plant, "schema": f"s_{sid}",
                "connection_type": "PostgreSQL"}

    def get_onboarded_ids(self):
        return set(self._systems)


def _world(monkeypatch, systems, mappings_of=None, columns=None):
    """Stub the resolve seam's graph, catalog, sampler and probe over
    ``systems``; ``mappings_of`` (sid -> mapper rows) and ``columns`` default
    to the batch-hop fixture."""
    columns = _columns() if columns is None else columns
    mappings_of = mappings_of or {sid: _MAPPINGS[sid]() for sid in systems}
    monkeypatch.setattr(onboard, "catalog", _Catalog(systems))
    graph = _Graph(mappings_of)
    graph.mappings_of = mappings_of
    monkeypatch.setattr(onboard, "neptune_query", graph)
    monkeypatch.setattr(onboard.self_learning, "_gate_cross_site_namespace", lambda *_a: ("", ""))
    monkeypatch.setattr(resolve_evidence, "sample_field_values",
                        lambda *, system_id, table, field, **_kw: list(columns.get((system_id, table, field), [])))
    monkeypatch.setattr(resolve_evidence, "probe_field_values",
                        lambda *, system_id, table, field, values, **_kw:
                        sorted(set(values) & set(columns.get((system_id, table, field), []))))
    return graph


def _resolve(system_id, graph=None, llm_rules=()):
    mappings = graph.mappings_of[system_id] if graph is not None else _MAPPINGS[system_id]()
    body = SimpleNamespace(system_id=system_id, mappings=mappings)
    return onboard._augment_with_affinity_rules(body, {"rules": list(llm_rules)})["rules"]


def _exact(system_id, rules):
    return {(system_id, r["source_field"], r["target_system"], r["target_field"])
            for r in rules if r["method"] == aff.EXACT_METHOD}


def _semantics(system_id, rules):
    """Each rule's direction-free meaning: its two endpoints, each with its
    pattern, concept and kind (method, transform, shared values, measured
    shape)."""
    return {(frozenset({(system_id, r["source_field"]), (r["target_system"], r["target_field"])}),
             r["concept"], r.get("method") or "", r.get("transform"), r.get("shared_count"),
             aff.exact_shape_from_reasoning(r.get("reasoning")),
             frozenset({((system_id, r["source_field"]), r.get("source_pattern") or ""),
                        ((r["target_system"], r["target_field"]), r.get("target_pattern") or "")}))
            for r in rules}


#: (ERP field, MES field) of the two true batch hops.
_HOPS = {("lot_mes_tracking.batch_id", "work_orders.work_order_number"),
         ("shipment_items.batch_id", "production_runs.run_number")}


def test_a_peer_business_key_the_mapper_demoted_is_an_exact_only_candidate(monkeypatch):
    """The peer read admits the same columns the peer's own resolve would: its
    identifier fields as full candidates, a correlate whose concept another
    column of its table holds as identifier as an exact-only one."""
    _world(monkeypatch, [ERP, MES_DEM])
    fields = onboard._affinity_existing_fields(ERP)
    assert sorted((f.table, f.field, f.exact_only) for f in fields) == [
        ("production_runs", "production_run_id", False),
        ("production_runs", "run_number", True),
        ("work_orders", "work_order_id", False),
        ("work_orders", "work_order_number", True)]
    own = onboard._affinity_new_fields(MES_DEM, _mes_mappings(MES_DEM))
    assert sorted((f.table, f.field, f.exact_only) for f in own) == sorted(
        (f.table, f.field, f.exact_only) for f in fields)


def test_a_peer_correlate_without_an_identifier_twin_stays_out(monkeypatch):
    """``work_orders.production_run_id`` is a correlate of ``production-run``
    and no column of ``work_orders`` holds that concept as identifier: not a
    candidate on either side, exactly as the 2026-10-02 amendment decided."""
    _world(monkeypatch, [ERP, MES_DEM])
    peer = {(f.table, f.field) for f in onboard._affinity_existing_fields(ERP)}
    own = {(f.table, f.field) for f in onboard._affinity_new_fields(MES_DEM, _mes_mappings(MES_DEM))}
    assert ("work_orders", "production_run_id") not in peer | own


def test_a_field_with_an_identifier_edge_is_read_once_as_a_full_candidate(monkeypatch):
    """A column mapped identifier for one concept and correlate for another is
    a full candidate, read from its identifier edge only (never also exact-only)."""
    both = _mapping("production_runs", "run_number", ("production-run", "identifier", 0.95),
                    ("batch", "correlate", 0.85))
    monkeypatch.setitem(_MAPPINGS, MES_ID, lambda: [both])
    _world(monkeypatch, [ERP, MES_ID])
    (field,) = onboard._affinity_existing_fields(ERP)
    assert (field.field, field.concept, field.exact_only) == ("run_number", "production-run", False)
    assert field.concepts == frozenset({"production-run", "batch"})


def test_the_erp_resolve_finds_both_hops_for_every_mes_whatever_its_role_output(monkeypatch, caplog):
    """The reproduction: the ERP re-provisioned after the MESs gets the two
    batch hops for BOTH plants, not only the one whose business keys were
    tagged identifier. The rules are the same for both plants."""
    _world(monkeypatch, [ERP, MES_ID, MES_DEM])
    with caplog.at_level(logging.INFO, logger=onboard.logger.name):
        rules = _resolve(ERP)
    found = _exact(ERP, rules)
    assert found == {(ERP, src, mes, tgt) for mes in (MES_ID, MES_DEM) for src, tgt in _HOPS}
    shape = {mes: sorted((r["source_field"], r["target_field"], r["concept"], r["transform"])
                         for r in rules if r["target_system"] == mes and r["method"] == aff.EXACT_METHOD)
             for mes in (MES_ID, MES_DEM)}
    assert shape[MES_ID] == shape[MES_DEM]
    assert "qualified=4 emitted=4" in caplog.text


@pytest.mark.parametrize("mes", [MES_ID, MES_DEM])
def test_the_hops_do_not_depend_on_which_system_resolves(monkeypatch, mes):
    """Re-provisioning either endpoint deletes the hops and its own resolve
    must write them back: the ERP's resolve and the MES's resolve write the
    same rules (endpoints, concept, kind), not only the same field pairs."""
    _world(monkeypatch, [ERP, mes])
    from_erp = _semantics(ERP, _resolve(ERP))
    from_mes = _semantics(mes, _resolve(mes))
    assert {(pair, concept) for pair, concept, *_kind in from_mes} == {
        (frozenset({(mes, tgt), (ERP, src)}), "batch") for src, tgt in _HOPS}
    assert from_erp == from_mes


def test_an_exact_only_peer_never_reaches_the_pattern_passes(monkeypatch):
    """A demoted peer column joins by the exact pass only, as on the new side:
    a value that would fit a pattern rule against it yields none."""
    _world(monkeypatch, [ERP, MES_DEM])
    calls = []
    real = aff._pattern_rule
    monkeypatch.setattr(aff, "_pattern_rule", lambda s, t: calls.append((s, t)) or real(s, t))
    _resolve(ERP)
    assert calls and not [t for _s, t in calls if t.exact_only]


_REFS = [f"REF-2026100{d}-{k:04d}" for d in (1, 2) for k in range(1, 10)]


def _both_directions(monkeypatch, mappings_of, columns, llm_rules_of=None):
    """{resolving system: the normalized rules its resolve writes};
    ``llm_rules_of`` gives each resolve's LLM rules."""
    graph = _world(monkeypatch, [LEFT, RIGHT], mappings_of, columns)
    return {sid: _semantics(sid, _resolve(sid, graph, (llm_rules_of or {}).get(sid, ())))
            for sid in (LEFT, RIGHT)}


def test_one_holder_against_two_writes_the_same_rules_from_either_side(monkeypatch):
    """One field of LEFT holds the values two fields of RIGHT hold. Both joins
    passed their probes, so resolving LEFT and resolving RIGHT both write the
    same two rules (not one from one side and two from the other)."""
    mappings_of = {
        LEFT: [_mapping("orders", "order_ref", ("sales-order", "identifier", 0.9))],
        RIGHT: [_mapping("lines", "order_ref", ("sales-order", "identifier", 0.9)),
                _mapping("archive", "order_ref", ("sales-order", "identifier", 0.8))]}
    columns = {(LEFT, "orders", "order_ref"): _REFS, (RIGHT, "lines", "order_ref"): _REFS,
               (RIGHT, "archive", "order_ref"): _REFS}
    found = _both_directions(monkeypatch, mappings_of, columns)
    assert found[LEFT] == found[RIGHT]
    assert {(pair, concept, method) for pair, concept, method, *_rest in found[LEFT]} == {
        (frozenset({(LEFT, "orders.order_ref"), (RIGHT, "archive.order_ref")}), "sales-order",
         aff.EXACT_METHOD),
        (frozenset({(LEFT, "orders.order_ref"), (RIGHT, "lines.order_ref")}), "sales-order",
         aff.EXACT_METHOD)}


_CODES = [f"CUS-{n:04d}" for n in range(1001, 1019)]
_VENDOR_CODES = [f"SUP-{n:04d}" for n in range(5001, 5019)]   # no digit core shared with _CODES


def test_a_base_table_key_and_its_one_to_one_extension_join_from_either_side(monkeypatch):
    """LEFT keeps customers in a base table and a one-to-one extension
    keyed by the same code; RIGHT's account code joins both. Both rules are
    written whichever side resolves."""
    mappings_of = {
        LEFT: [_mapping("customers", "customer_code", ("customer", "identifier", 0.9)),
               _mapping("customer_credit", "customer_code", ("customer", "identifier", 0.85))],
        RIGHT: [_mapping("accounts", "customer_code", ("customer", "identifier", 0.9))]}
    columns = {(LEFT, "customers", "customer_code"): _CODES,
               (LEFT, "customer_credit", "customer_code"): _CODES,
               (RIGHT, "accounts", "customer_code"): _CODES}
    found = _both_directions(monkeypatch, mappings_of, columns)
    assert found[LEFT] == found[RIGHT]
    assert {pair for pair, *_rest in found[LEFT]} == {
        frozenset({(LEFT, "customers.customer_code"), (RIGHT, "accounts.customer_code")}),
        frozenset({(LEFT, "customer_credit.customer_code"), (RIGHT, "accounts.customer_code")})}


@pytest.mark.parametrize("right_order", ["accounts-first", "suppliers-first"])
def test_two_joins_on_one_column_name_keep_both_concepts(monkeypatch, right_order):
    """LEFT ``customers.external_id`` joins RIGHT ``accounts.external_id``
    (customer) and LEFT ``vendors.external_id`` joins RIGHT
    ``suppliers.external_id`` (supplier). The two rules share their column
    names but not their tables; both are written from either side, whatever
    order RIGHT's mapper listed its tables in."""
    right = [_mapping("accounts", "external_id", ("customer", "identifier", 0.9)),
             _mapping("suppliers", "external_id", ("supplier", "identifier", 0.9))]
    mappings_of = {
        LEFT: [_mapping("customers", "external_id", ("customer", "identifier", 0.9)),
               _mapping("vendors", "external_id", ("supplier", "identifier", 0.9))],
        RIGHT: right if right_order == "accounts-first" else right[::-1]}
    columns = {(LEFT, "customers", "external_id"): _CODES, (RIGHT, "accounts", "external_id"): _CODES,
               (LEFT, "vendors", "external_id"): _VENDOR_CODES,
               (RIGHT, "suppliers", "external_id"): _VENDOR_CODES}
    found = _both_directions(monkeypatch, mappings_of, columns)
    assert found[LEFT] == found[RIGHT]
    assert {(pair, concept) for pair, concept, *_rest in found[LEFT]} == {
        (frozenset({(LEFT, "customers.external_id"), (RIGHT, "accounts.external_id")}), "customer"),
        (frozenset({(LEFT, "vendors.external_id"), (RIGHT, "suppliers.external_id")}), "supplier")}


def test_two_shared_concepts_name_the_same_concept_from_either_side(monkeypatch):
    """Both key fields map ``alpha`` and ``beta``; LEFT's mapper ranks alpha
    higher, RIGHT's beta. The rule names the concept with the higher combined
    confidence of both endpoints (alpha, 1.5 vs 1.3) whichever side resolves."""
    mappings_of = {
        LEFT: [_mapping("orders", "order_ref", ("alpha", "identifier", 0.9), ("beta", "correlate", 0.5))],
        RIGHT: [_mapping("lines", "order_ref", ("beta", "identifier", 0.8), ("alpha", "correlate", 0.6))]}
    columns = {(LEFT, "orders", "order_ref"): _REFS, (RIGHT, "lines", "order_ref"): _REFS}
    found = _both_directions(monkeypatch, mappings_of, columns)
    assert found[LEFT] == found[RIGHT]
    assert [concept for _pair, concept, *_rest in found[LEFT]] == ["alpha"]


def test_the_peer_read_carries_each_concepts_confidence(monkeypatch):
    """The confidence stored on every MAPS_TO edge of a peer field reaches its
    sampled field, so both sides of a pair rank concepts on the same data."""
    mappings_of = {
        LEFT: [_mapping("orders", "order_ref", ("alpha", "identifier", 0.9))],
        RIGHT: [_mapping("lines", "order_ref", ("beta", "identifier", 0.8), ("alpha", "correlate", 0.6))]}
    graph = _world(monkeypatch, [LEFT, RIGHT], mappings_of, {})
    (field,) = onboard._affinity_existing_fields(LEFT)
    assert dict(field.concept_confidence) == {"beta": 0.8, "alpha": 0.6}
    assert any("confidence" in cypher for cypher in graph.reads)


def _external_id_world(right_order):
    right = [_mapping("accounts", "external_id", ("customer", "identifier", 0.9)),
             _mapping("suppliers", "external_id", ("supplier", "identifier", 0.9))]
    mappings_of = {
        LEFT: [_mapping("customers", "external_id", ("customer", "identifier", 0.9)),
               _mapping("vendors", "external_id", ("supplier", "identifier", 0.9))],
        RIGHT: right if right_order == "accounts-first" else right[::-1]}
    columns = {(LEFT, "customers", "external_id"): _CODES, (RIGHT, "accounts", "external_id"): _CODES,
               (LEFT, "vendors", "external_id"): _VENDOR_CODES,
               (RIGHT, "suppliers", "external_id"): _VENDOR_CODES}
    return mappings_of, columns


def _llm_customer_rule(source, target, source_table, target_table):
    """The model's rule for the customer pair, in the resolver's output shape
    (table-qualified fields plus the tables), with a transforming pattern."""
    return {"concept": "customer", "source_field": f"{source_table}.external_id",
            "source_table": source_table, "source_pattern": "C-{id}", "target_system": target,
            "target_field": f"{target_table}.external_id", "target_table": target_table,
            "target_pattern": "{id}", "confidence": 0.8, "reasoning": "model"}


@pytest.mark.parametrize("right_order", ["accounts-first", "suppliers-first"])
def test_an_llm_rule_for_one_table_pair_keeps_the_other_pairs_exact_rule(monkeypatch, right_order):
    """Each resolve's model proposed a transforming rule for the customer
    tables only. It wins over the customer exact rule (the model's own pattern
    for its pair) and must leave the supplier exact rule, on other tables,
    in place from either side."""
    mappings_of, columns = _external_id_world(right_order)
    llm_rules_of = {LEFT: [_llm_customer_rule(LEFT, RIGHT, "customers", "accounts")],
                    RIGHT: [_llm_customer_rule(RIGHT, LEFT, "accounts", "customers")]}
    found = _both_directions(monkeypatch, mappings_of, columns, llm_rules_of)
    supplier = frozenset({(LEFT, "vendors.external_id"), (RIGHT, "suppliers.external_id")})
    customer = frozenset({(LEFT, "customers.external_id"), (RIGHT, "accounts.external_id")})
    for sid in (LEFT, RIGHT):
        assert {(pair, concept, method) for pair, concept, method, *_rest in found[sid]} == {
            (customer, "customer", ""), (supplier, "supplier", aff.EXACT_METHOD)}


_RUN_VALUES = [f"RUN-2026-{n:05d}" for n in range(12001, 12041)]
_BATCH_VALUES = [f"BATCH-{n:05d}" for n in range(12001, 12041)]


@pytest.mark.parametrize("left_concept,right_concept,named", [
    ("production-run", "batch", "batch"), ("alpha", "beta", "alpha")])
def test_a_pattern_rule_is_the_same_rule_from_either_side(monkeypatch, left_concept, right_concept,
                                                         named):
    """A run number and a batch id with different formats share their digit
    cores: the pattern pass writes one rule. Resolving either side writes the
    same rule: endpoints with their patterns, concept and method."""
    mappings_of = {
        LEFT: [_mapping("production_runs", "run_number", (left_concept, "identifier", 0.9))],
        RIGHT: [_mapping("shipment_items", "batch_id", (right_concept, "identifier", 0.9))]}
    columns = {(LEFT, "production_runs", "run_number"): _RUN_VALUES,
               (RIGHT, "shipment_items", "batch_id"): _BATCH_VALUES}
    found = _both_directions(monkeypatch, mappings_of, columns)
    assert found[LEFT] == found[RIGHT]
    ((_pair, concept, method, *_rest),) = found[LEFT]
    assert (concept, method) == (named, aff.AFFINITY_METHOD)
