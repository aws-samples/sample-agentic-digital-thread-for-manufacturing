# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Identifier-core affinity in the onboarding resolve job + provision provenance.

Event 15: ``mes-ind.production_runs.batch_number`` (concept ``batch``) and
``sys-b.orders.lot_ref`` (concept ``lot-number``) share no concept, so
``_do_resolve`` never paired them and the rule had to be pre-taught. The
affinity pass runs on the resolve job AROUND ``_do_resolve`` — so it also
fires when the LLM path found nothing — and its rules carry an evidence
method that provision persists (tier 1 in the route seed, not
``onboarding-llm``).
"""
import os
import sys
from pathlib import Path
from types import SimpleNamespace

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))
os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.onboard as onboard  # noqa: E402
import agents.resolve_evidence as resolve_evidence  # noqa: E402
from agents.identity_affinity import AFFINITY_METHOD  # noqa: E402

_MES = [f"B-{n}" for n in range(2800, 2951)]
_LOTS = [f"LOT-2026-0{n}" for n in range(2847, 2897)]


class _Catalog:
    def __init__(self):
        self.entries = {
            "mes-ind": {"connection_type": "PostgreSQL", "schema": "mes_ind", "connection_config": {}},
            "sys-b": {"connection_type": "PostgreSQL", "schema": "sys_b", "connection_config": {}},
        }

    def get(self, sid):
        return self.entries.get(sid)

    def get_onboarded_ids(self):
        return {"sys-b", "erp"}


def _install(monkeypatch, *, samples, existing_rows):
    monkeypatch.setattr(onboard, "catalog", _Catalog())
    monkeypatch.setattr(onboard, "neptune_query",
                        lambda cypher, params=None: existing_rows if "r.role = 'identifier'" in cypher else [])

    def _sample(*, system_id, table, field, **_kw):
        return samples.get((system_id, table, field), [])
    monkeypatch.setattr(resolve_evidence, "sample_field_values", _sample)


def _body():
    return SimpleNamespace(system_id="mes-ind", mappings=[
        {"table": "production_runs", "field": "batch_number", "concept": "batch",
         "confidence": 0.95, "mappings": [{"concept": "batch", "role": "identifier"}]},
        {"table": "production_runs", "field": "good_units", "concept": "actual-quantity",
         "confidence": 0.9, "mappings": [{"concept": "actual-quantity", "role": "measure"}]},
    ])


def test_affinity_adds_the_batch_lot_rule_when_no_concept_is_shared(monkeypatch):
    _install(monkeypatch,
             samples={("mes-ind", "production_runs", "batch_number"): _MES,
                      ("sys-b", "orders", "lot_ref"): _LOTS,
                      ("erp", "orders", "order_id"): ["RUN-20260210-00001"]},
             existing_rows=[
                 {"sys_id": "sys-b", "sys_schema": "sys_b", "table_name": "orders",
                  "field_name": "lot_ref", "concept": "lot-number"},
                 {"sys_id": "erp", "sys_schema": "erp", "table_name": "orders",
                  "field_name": "order_id", "concept": "order"},
                 {"sys_id": "plm", "sys_schema": "plm", "table_name": "parts",
                  "field_name": "part_no", "concept": "part"},  # not onboarded → skipped
             ])

    result = onboard._augment_with_affinity_rules(_body(), {"rules": [], "evidence": []})

    assert len(result["rules"]) == 1
    rule = result["rules"][0]
    assert rule["source_field"] == "production_runs.batch_number"
    assert rule["source_pattern"] == "B-{id}"
    assert rule["target_system"] == "sys-b"
    assert rule["target_field"] == "orders.lot_ref"
    assert rule["target_pattern"] == "LOT-2026-0{id}"
    assert rule["method"] == AFFINITY_METHOD
    assert result["evidence"] == []  # the rest of the resolve result is untouched


def test_affinity_never_duplicates_an_llm_rule_for_the_same_pair(monkeypatch):
    _install(monkeypatch,
             samples={("mes-ind", "production_runs", "batch_number"): _MES,
                      ("sys-b", "orders", "lot_ref"): _LOTS},
             existing_rows=[{"sys_id": "sys-b", "sys_schema": "sys_b", "table_name": "orders",
                             "field_name": "lot_ref", "concept": "lot-number"}])
    llm = {"concept": "batch", "source_field": "production_runs.batch_number",
           "target_system": "sys-b", "target_field": "orders.lot_ref",
           "source_pattern": "B-{id}", "target_pattern": "LOT-2026-0{id}", "confidence": 0.8}

    result = onboard._augment_with_affinity_rules(_body(), {"rules": [llm]})

    assert result["rules"] == [llm]


def test_affinity_is_best_effort(monkeypatch):
    monkeypatch.setattr(onboard, "catalog", _Catalog())

    def _boom(*_a, **_k):
        raise RuntimeError("graph down")
    monkeypatch.setattr(onboard, "neptune_query", _boom)
    monkeypatch.setattr(resolve_evidence, "sample_field_values", _boom)

    result = onboard._augment_with_affinity_rules(_body(), {"rules": [{"concept": "x"}]})

    assert result == {"rules": [{"concept": "x"}]}


def test_provision_persists_the_evidence_method_and_uses_the_rules_table(monkeypatch):
    from tests.unit._rule_check_db import measured_as_sent
    measured_as_sent(monkeypatch)  # the writer under test, not the re-measurement
    recorded = []
    monkeypatch.setattr(onboard, "neptune_write", lambda c, p=None: recorded.append((c, p or {})))
    monkeypatch.setattr(onboard, "neptune_query", lambda *_a, **_k: [{"tbl": "orders"}])
    sampled = []

    def _pg(schema, sql, params=None, **_kw):
        sampled.append(sql)
        return {"rows": [["B-2847"]]}
    monkeypatch.setattr(onboard, "pg", _pg)

    class _Cat:
        def get(self, _sid):
            return None

        def get_onboarded_ids(self):
            return {"sys-b"}
    monkeypatch.setattr(onboard, "catalog", _Cat())
    rule = {"concept": "batch", "source_field": "production_runs.batch_number",
            "source_table": "production_runs", "source_pattern": "B-{id}",
            "target_system": "sys-b", "target_field": "orders.lot_ref",
            "target_table": "orders", "target_pattern": "LOT-2026-0{id}",
            "confidence": 0.85, "method": AFFINITY_METHOD,
            "reasoning": "50 of 50 sampled value pairs share an identifier core"}
    ctx = onboard._ProvisionState(
        body=SimpleNamespace(value_mappings=[]), system_id="mes-ind", system_name="MES",
        plant="", schema="mes_ind", rules=[rule], mappings=[], connection_type="PostgreSQL",
        cm_items=[], committable={"batch"})
    ctx.tables_seen.update({"equipment", "production_runs"})

    onboard._dp_identity_rules(ctx)

    ir = [(c, p) for c, p in recorded if c.startswith("MERGE (ir:IdentityRule")]
    assert len(ir) == 1
    cypher, params = ir[0]
    assert "ir.method = $method" in cypher and "ir.origin_context = $octx" in cypher
    assert params["method"] == AFFINITY_METHOD
    assert params["octx"].startswith("50 of 50 sampled")
    assert params["src_pat"] == "B-{id}" and params["tgt_pat"] == "LOT-2026-0{id}"
    # The live sample check queried the rule's OWN table, not an arbitrary one.
    assert sampled and "FROM production_runs" in sampled[0]
    assert ctx.rules_rejected == []


def test_rules_without_a_method_leave_the_method_to_the_stamp(monkeypatch):
    """A checkpoint rule without its own method: the MERGE sets no method (the
    post-provision stamp sets onboarding-llm) and states its origin. Before
    2026-10-21 it set no origin_context either, so a node written again kept
    another run's evidence."""
    recorded = []
    monkeypatch.setattr(onboard, "neptune_write", lambda c, p=None: recorded.append((c, p or {})))
    monkeypatch.setattr(onboard, "neptune_query", lambda *_a, **_k: [{"tbl": "orders"}])
    monkeypatch.setattr(onboard, "pg", lambda *_a, **_k: {"rows": []})

    class _Cat:
        def get(self, _sid):
            return None

        def get_onboarded_ids(self):
            return {"sys-b"}
    monkeypatch.setattr(onboard, "catalog", _Cat())
    rule = {"concept": "batch", "source_field": "batch_number", "source_table": "runs",
            "source_pattern": "", "target_system": "sys-b", "target_field": "lot",
            "target_pattern": "", "confidence": 0.9}
    ctx = onboard._ProvisionState(
        body=SimpleNamespace(value_mappings=[]), system_id="mes-ind", system_name="MES",
        plant="", schema="", rules=[rule], mappings=[], connection_type="MCP Server",
        cm_items=[], committable=set(), rules_from_checkpoint=True,
        checkpoint_rule_ids=frozenset({id(rule)}))  # in the Resolve result

    onboard._dp_identity_rules(ctx)

    cypher, params = [(c, p) for c, p in recorded if c.startswith("MERGE (ir:IdentityRule")][0]
    assert "ir.method" not in cypher  # the post-provision stamp still owns it
    assert params["method"] == ""
    assert "ir.origin_context = $octx" in cypher
    assert params["octx"].startswith(onboard._ONBOARDING_RULE_ORIGIN + "; ")
    # The MCP source's pair could not be measured, so it is counted unchecked.
    assert "not re-checked at provision" in params["octx"]
    assert len(ctx.rules_unchecked) == 1


_CODES = ["5101", "5202", "5303"]
_ITEMS = ["PRT-5101-R-A", "PRT-5101-R-B", "PRT-5101-R-C", "PRT-5202-R-A", "PRT-5202-R-B",
          "PRT-5303-R-A", *[f"PRT-{n}-R-A" for n in range(6001, 6024)]]


def test_one_to_many_samples_reach_the_resolve_job_as_a_wildcard_rule(monkeypatch):
    """ADR-0086: several target records share one identifier core, so the
    varying tail is a ``{*}`` slot — and it replaces an LLM constant for the
    same field pair, in place."""
    _install(monkeypatch,
             samples={("sys-a", "runs", "product_code"): _CODES,
                      ("sys-b", "items", "item_id"): _ITEMS},
             existing_rows=[{"sys_id": "sys-b", "sys_schema": "sys_b", "table_name": "items",
                             "field_name": "item_id", "concept": "part"}])
    body = SimpleNamespace(system_id="sys-a", mappings=[
        {"table": "runs", "field": "product_code", "concept": "product", "confidence": 0.9,
         "mappings": [{"concept": "product", "role": "identifier"}]}])
    llm = {"concept": "product", "source_field": "runs.product_code",
           "target_system": "sys-b", "target_field": "items.item_id",
           "source_pattern": "{id}", "target_pattern": "PRT-{id}-R-A", "confidence": 0.7}

    result = onboard._augment_with_affinity_rules(body, {"rules": [llm]})

    assert len(result["rules"]) == 1
    rule = result["rules"][0]
    assert rule["source_pattern"] == "{id}"
    assert rule["target_pattern"] == "PRT-{id}-R-{*}"
    assert rule["method"] == AFFINITY_METHOD
    assert rule["reasoning"].startswith("3 of 3 sampled identifier cores share one transform")
