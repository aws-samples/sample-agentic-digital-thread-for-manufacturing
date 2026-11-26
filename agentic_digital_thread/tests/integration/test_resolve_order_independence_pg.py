# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The two batch hops do not depend on which system resolves, on the REAL
seeded data (ADR-0087 amendment 2026-10-18, PG lane).

After db-init (site MES views + ``erp_data``) and the live-story history, the
engine's own resolve seam (``onboard._augment_with_affinity_rules``: its
sampler, its peer read and its membership probe) runs once with the ERP as the
system being resolved and once with the live site MES, against a graph stub
holding the MAPS_TO edges each system's provision writes. The MES is mapped in
the two ways rehearsal mapper runs produced (2026-10-18): its business keys as
the identifiers, or its surrogate keys as the identifiers and the business keys
as correlates of the same concepts. Every resolve must derive exactly the two
true hops (work-order number -> ``lot_mes_tracking.batch_id``, run number ->
``shipment_items.batch_id``) in its own direction, and both directions must
write the same rules: endpoints, concept, kind, shared values and shape. A
third, synthetic assignment gives each hop's two fields two shared concepts
ranked oppositely by their two mappers, so the concept a rule names is checked
for direction independence too.

Run (from ``agentic_digital_thread/``)::

    ERP_SIM_TEST_PG_DSN=postgresql://postgres:<pw>@127.0.0.1:<port>/postgres \\
      PYTHONPATH=. python -m pytest tests/integration/test_resolve_order_independence_pg.py -q
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from agents import identity_affinity as aff  # noqa: E402
from agents.demo import systems, workshop  # noqa: E402
from agents.join_discovery import JOIN_MIN_OVERLAP_COUNT  # noqa: E402
from tests.integration import story_pg_support as support  # noqa: E402

pytestmark = pytest.mark.integration

_DSN = os.environ.get("ERP_SIM_TEST_PG_DSN")
_MES = workshop.LIVE_MES_SYSTEM["id"]
_ERP = "erp"
_HOPS = {frozenset({(_ERP, "lot_mes_tracking.batch_id"), (_MES, "work_orders.work_order_number")}),
         frozenset({(_ERP, "shipment_items.batch_id"), (_MES, "production_runs.run_number")})}


def _mapping(table, column, concept, role, confidence, *more):
    """A mapper row; ``more`` adds (concept, role, confidence) entries."""
    roles = ((concept, role, confidence), *more)
    return {"table": table, "field": f"{table}.{column}", "concept": concept,
            "confidence": confidence,
            "mappings": [{"concept": c, "role": r, "confidence": conf} for c, r, conf in roles]}


_ERP_MAPPINGS = [_mapping("lot_mes_tracking", "batch_id", "batch", "identifier", 0.9),
                 _mapping("shipment_items", "batch_id", "batch", "identifier", 0.8)]
#: The two assignments rehearsal mapper runs gave the MES (batch-fields-now.json).
_MES_MAPPINGS = {
    "business-keys-identifier": [
        _mapping("work_orders", "work_order_number", "work-order", "identifier", 0.95),
        _mapping("production_runs", "run_number", "production-run", "identifier", 0.95)],
    "surrogates-identifier": [
        _mapping("work_orders", "work_order_id", "work-order", "identifier", 0.95),
        _mapping("work_orders", "work_order_number", "work-order", "correlate", 0.95),
        _mapping("production_runs", "production_run_id", "production-run", "identifier", 0.95),
        _mapping("production_runs", "run_number", "production-run", "correlate", 0.9)],
    "shared-concepts-opposite": [
        _mapping("work_orders", "work_order_number", "work-order", "identifier", 0.95,
                 ("batch", "correlate", 0.6)),
        _mapping("production_runs", "run_number", "production-run", "identifier", 0.95,
                 ("batch", "correlate", 0.6))],
}
#: The ERP side of each assignment: the live one, except the synthetic case,
#: where the ERP's mapper ranks ``batch`` above the MES concept it also maps.
_ERP_OF = {"shared-concepts-opposite": [
    _mapping("lot_mes_tracking", "batch_id", "batch", "identifier", 0.9,
             ("work-order", "correlate", 0.7)),
    _mapping("shipment_items", "batch_id", "batch", "identifier", 0.8,
             ("production-run", "correlate", 0.7))]}
#: The concept each hop names. Live assignments: ``batch``, the concept main
#: writes from the MES resolve today. Synthetic: the higher combined confidence
#: of both mappings (work-order 0.95 + 0.7 over batch 0.6 + 0.9; production-run
#: 0.95 + 0.7 over batch 0.6 + 0.8).
_CONCEPT_OF = {"business-keys-identifier": {"work_orders.work_order_number": "batch",
                                            "production_runs.run_number": "batch"},
               "surrogates-identifier": {"work_orders.work_order_number": "batch",
                                         "production_runs.run_number": "batch"},
               "shared-concepts-opposite": {"work_orders.work_order_number": "work-order",
                                            "production_runs.run_number": "production-run"}}
_ROLE_NAMED = re.compile(r"r\.role = '(\w+)'")


def _schema_of() -> dict:
    (erp,) = [s for s in systems.DEMO_SYSTEMS if s["id"] == _ERP]
    return {_ERP: erp["schema"], _MES: workshop.LIVE_MES_SYSTEM["schema"]}


class _Catalog:
    def __init__(self, schema_of):
        self._entries = {_ERP: {"id": _ERP, "system_type": "ERP", "plant": "All Sites"},
                         _MES: {"id": _MES, "system_type": "MES",
                                "plant": workshop.LIVE_MES_SYSTEM["plant"]}}
        for sid, schema in schema_of.items():
            self._entries[sid].update(schema=schema, connection_type="PostgreSQL")

    def get(self, sid):
        return self._entries.get(sid)

    def get_onboarded_ids(self):
        return set(self._entries)


def _graph(mappings_of: dict, schema_of: dict):
    """Neptune stand-in for the peer read: one row per MAPS_TO edge of another
    system's field whose role the query's WHERE names."""
    edges = [(sid, m["table"], m["field"].split(".", 1)[1], rm["concept"], rm["role"],
              rm["confidence"])
             for sid, ms in mappings_of.items() for m in ms for rm in m["mappings"]]

    def query(cypher, params=None):
        roles = set(_ROLE_NAMED.findall(cypher))
        sid = (params or {}).get("sid")
        return [{"sys_id": s, "sys_schema": schema_of[s], "table_name": t, "field_name": f,
                 "concept": c, "role": role, "removal_incomplete": False,
                 "concepts": sorted({e[3] for e in edges if e[:3] == (s, t, f)}),
                 "concept_scores": [{"concept": e[3], "confidence": e[5]}
                                    for e in edges if e[:3] == (s, t, f)]}
                for s, t, f, c, role, _conf in edges if s != sid and role in roles]
    return query


@pytest.fixture(scope="module")
def live_db():
    if not _DSN:
        pytest.skip("set ERP_SIM_TEST_PG_DSN to run against PostgreSQL")
    pytest.importorskip("pg8000")
    pytest.importorskip("boto3")          # lambda-db-init imports it at module load
    with support.story_databases(_DSN, "roi") as (conns, plan):
        yield conns, plan


def _resolve_both_ways(monkeypatch, conns, assignment):
    """{resolving system: its exact rules} for the ERP and the MES."""
    import agents.db as db
    from agents import onboard
    schema_of = _schema_of()
    monkeypatch.setattr(db, "pg", support.routed_pg(conns))
    monkeypatch.setattr(onboard, "catalog", _Catalog(schema_of))
    mappings_of = {_ERP: _ERP_OF.get(assignment, _ERP_MAPPINGS), _MES: _MES_MAPPINGS[assignment]}
    monkeypatch.setattr(onboard, "neptune_query", _graph(mappings_of, schema_of))
    out = {}
    for sid, mappings in mappings_of.items():
        body = SimpleNamespace(system_id=sid, mappings=mappings)
        rules = onboard._augment_with_affinity_rules(body, {"rules": []})["rules"]
        out[sid] = [r for r in rules if r["method"] == aff.EXACT_METHOD]
    return out


def _pairs(sid, rules):
    return {frozenset({(sid, r["source_field"]), (r["target_system"], r["target_field"])})
            for r in rules}


def _semantics(sid, rules):
    """Each rule's direction-free meaning: endpoints with their patterns,
    concept, kind (method, transform), shared values and the measured shape."""
    return {(frozenset({(sid, r["source_field"]), (r["target_system"], r["target_field"])}),
             r["concept"], r["method"], r["transform"], r["shared_count"],
             aff.exact_shape_from_reasoning(r["reasoning"]),
             frozenset({((sid, r["source_field"]), r["source_pattern"]),
                        ((r["target_system"], r["target_field"]), r["target_pattern"])}))
            for r in rules}


@pytest.mark.parametrize("assignment", sorted(_MES_MAPPINGS))
def test_either_resolve_derives_both_hops(live_db, monkeypatch, assignment):
    conns, _plan = live_db
    found = _resolve_both_ways(monkeypatch, conns, assignment)
    assert _pairs(_MES, found[_MES]) == _HOPS
    assert _pairs(_ERP, found[_ERP]) == _HOPS
    for rule in found[_ERP]:
        assert rule["shared_count"] >= JOIN_MIN_OVERLAP_COUNT
        assert (rule["source_pattern"], rule["target_pattern"], rule["transform"]) == ("", "", "identity")
    for rule in found[_MES]:
        assert rule["concept"] == _CONCEPT_OF[assignment][rule["source_field"]]


@pytest.mark.parametrize("assignment", sorted(_MES_MAPPINGS))
def test_both_directions_write_the_same_rules(live_db, monkeypatch, assignment):
    """Same endpoints, concept, kind, shared count and shape from the ERP's
    resolve as from the MES's."""
    conns, _plan = live_db
    found = _resolve_both_ways(monkeypatch, conns, assignment)
    assert _semantics(_ERP, found[_ERP]) == _semantics(_MES, found[_MES])


def test_the_erp_rules_do_not_depend_on_the_mes_role_output(live_db, monkeypatch):
    """The ERP's rules for the two live MES assignments are the same rules."""
    conns, _plan = live_db
    seen = [_semantics(_ERP, _resolve_both_ways(monkeypatch, conns, assignment)[_ERP])
            for assignment in ("business-keys-identifier", "surrogates-identifier")]
    assert seen[0] == seen[1] and len(seen[0]) == 2


@pytest.mark.parametrize("assignment", ["business-keys-identifier", "surrogates-identifier"])
def test_the_batch_hop_rule_ids_are_unchanged(live_db, monkeypatch, assignment, capsys):
    """Each of the four directional batch-hop rules gets the id main's digest
    gives it (source column, target system, target column, concept ``batch``;
    no table twin, so the tables are not read), and the four ids are distinct."""
    from agents import onboard
    conns, _plan = live_db
    found = _resolve_both_ways(monkeypatch, conns, assignment)
    rules = [(sid, dict(rule)) for sid in found for rule in found[sid]]
    onboard._mark_table_twins([rule for _sid, rule in rules])
    ids = {}
    for sid, rule in rules:
        main_id = onboard._rule_content_digest(sid, rule["source_field"].split(".")[-1],
                                               rule["target_system"],
                                               rule["target_field"].split(".")[-1], "batch")
        assert onboard._rule_digest_for(sid, rule) == main_id
        ids[(sid, rule["source_field"])] = main_id
    assert len(set(ids.values())) == 4
    with capsys.disabled():
        print("\nbatch-hop rule digests:", sorted(ids.items()))
