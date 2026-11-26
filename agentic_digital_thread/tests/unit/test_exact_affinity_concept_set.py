# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Onboarding feeds the exact-agreement pass each field's FULL concept set
(ADR-0087): identifier role stays the CANDIDACY filter, but agreement is a
shared MAPS_TO concept of any role. Live evidence: the MES run number MAPS_TO
``production-run`` (identifier) and ``batch`` (correlate); the ERP shipment
line's ``batch_id`` MAPS_TO ``batch``. After the run view was widened the run
number MAPS_TO ``production-run`` only; the catalog-related identifier pair
still agrees. One bounded INFO line per run counts
the exact pass's outcomes (never values).
"""
import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))
os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.identity_affinity as aff  # noqa: E402
import agents.onboard as onboard  # noqa: E402
import agents.resolve_evidence as resolve_evidence  # noqa: E402

RUNS = [f"RUN-2026092{d}-9{d}{k:03d}" for d in (1, 2, 3) for k in range(6)]


class _Catalog:
    def __init__(self, types):
        self._types = types

    def get(self, sid):
        kind = self._types.get(sid)
        return None if kind is None else {"id": sid, "system_type": kind, "plant": "Plant 1",
                                          "schema": f"s_{sid}", "connection_type": "PostgreSQL"}

    def get_onboarded_ids(self):
        return set(self._types)


def _run_mapping():
    return {"table": "production_runs", "field": "production_runs.run_number",
            "concept": "production-run", "confidence": 0.95,
            "mappings": [{"concept": "production-run", "role": "identifier", "confidence": 0.95},
                         {"concept": "batch", "role": "correlate", "confidence": 0.85},
                         {"concept": "needs_review", "role": "correlate", "confidence": 0.1}]}


def test_new_fields_carry_every_mapped_concept_and_its_confidence(monkeypatch):
    monkeypatch.setattr(onboard, "catalog", _Catalog({"mes-sys": "MES"}))
    monkeypatch.setattr(resolve_evidence, "sample_field_values", lambda **_kw: ["RUN-1-1"])
    correlate_only = {"table": "t", "field": "note", "concept": "batch",
                      "mappings": [{"concept": "batch", "role": "correlate"}]}
    (field,) = onboard._affinity_new_fields("mes-sys", [_run_mapping(), correlate_only])
    assert field.concept == "production-run"
    assert field.concepts == frozenset({"production-run", "batch"})
    assert dict(field.concept_confidence) == {"production-run": 0.95, "batch": 0.85}


def test_new_fields_without_nested_mappings_keep_the_top_level_concept(monkeypatch):
    monkeypatch.setattr(onboard, "catalog", _Catalog({"mes-sys": "MES"}))
    monkeypatch.setattr(resolve_evidence, "sample_field_values", lambda **_kw: [])
    (field,) = onboard._affinity_new_fields("mes-sys", [
        {"table": "t", "field": "code", "concept": "batch", "confidence": "bad"}])
    assert (field.concept, field.concepts, field.concept_confidence) == ("batch", frozenset({"batch"}), ())


def test_existing_fields_carry_every_maps_to_concept(monkeypatch):
    monkeypatch.setattr(onboard, "catalog", _Catalog({"erp-sys": "ERP"}))
    seen = {}

    def query(cypher, params=None):
        seen["cypher"] = cypher
        return [{"sys_id": "erp-sys", "sys_schema": "s_erp", "table_name": "shipment_items",
                 "field_name": "batch_id", "concept": "batch",
                 "concepts": ["batch", "lot", "unknown", None]}]
    monkeypatch.setattr(onboard, "neptune_query", query)
    monkeypatch.setattr(resolve_evidence, "sample_field_values", lambda **_kw: ["RUN-1-1"])
    (field,) = onboard._affinity_existing_fields("mes-sys")
    assert "r.role = 'identifier'" in seen["cypher"]            # candidacy filter unchanged
    assert "OPTIONAL MATCH (f)-[o:MAPS_TO]->" in seen["cypher"]  # agreement set: every edge
    assert (field.concept, field.concepts) == ("batch", frozenset({"batch", "lot"}))


def test_existing_rows_without_a_concept_list_keep_their_concept(monkeypatch):
    monkeypatch.setattr(onboard, "catalog", _Catalog({"erp-sys": "ERP"}))
    monkeypatch.setattr(onboard, "neptune_query", lambda *_a, **_k: [
        {"sys_id": "erp-sys", "sys_schema": "s_erp", "table_name": "t", "field_name": "f",
         "concept": "batch"}])
    monkeypatch.setattr(resolve_evidence, "sample_field_values", lambda **_kw: [])
    (field,) = onboard._affinity_existing_fields("mes-sys")
    assert field.concepts == frozenset({"batch"})


def test_the_live_concept_assignment_yields_the_exact_rule_and_one_summary_line(monkeypatch, caplog):
    """End to end through the resolve seam: the correlate ``batch`` concept is
    shared, so the run-number hop is emitted, and the run logs ONE summary."""
    monkeypatch.setattr(onboard, "catalog", _Catalog({"mes-sys": "MES", "erp-sys": "ERP"}))
    monkeypatch.setattr(onboard.self_learning, "_gate_cross_site_namespace", lambda *_a: ("", ""))
    monkeypatch.setattr(onboard, "neptune_query", lambda *_a, **_k: [
        {"sys_id": "erp-sys", "sys_schema": "s_erp", "table_name": "shipment_items",
         "field_name": "batch_id", "concept": "batch", "concepts": ["batch"]}])
    columns = {("mes-sys", "production_runs", "run_number"): RUNS,
               ("erp-sys", "shipment_items", "batch_id"): RUNS + [f"RUN-2026090{i}-8{i}000" for i in range(1, 6)]}
    monkeypatch.setattr(resolve_evidence, "sample_field_values",
                        lambda *, system_id, table, field, **_kw: columns[(system_id, table, field)])
    monkeypatch.setattr(resolve_evidence, "probe_field_values",
                        lambda *, system_id, table, field, values, **_kw:
                        sorted(set(values) & set(columns[(system_id, table, field)])))
    body = SimpleNamespace(system_id="mes-sys", mappings=[_run_mapping()])
    with caplog.at_level(logging.INFO, logger=onboard.logger.name):
        out = onboard._augment_with_affinity_rules(body, {"rules": []})
    (rule,) = out["rules"]
    assert (rule["method"], rule["concept"], rule["target_field"]) == (
        aff.EXACT_METHOD, "batch", "shipment_items.batch_id")
    summaries = [r.getMessage() for r in caplog.records if "exact pass:" in r.getMessage()]
    assert summaries == ["[RESOLVE] mes-sys: exact pass: 1 pair(s) considered; refused concept=0 "
                         "shape=0 probe/cardinality=0; qualified=1 emitted=1"]
    assert not any(v in summaries[0] for v in RUNS)


def _run_alone(monkeypatch, concept):
    """The resolve seam with the run number mapped to ONE ``concept`` only
    (identifier), against the ERP shipment line's ``batch`` column."""
    monkeypatch.setattr(onboard, "catalog", _Catalog({"mes-sys": "MES", "erp-sys": "ERP"}))
    monkeypatch.setattr(onboard, "neptune_query", lambda *_a, **_k: [
        {"sys_id": "erp-sys", "sys_schema": "s_erp", "table_name": "shipment_items",
         "field_name": "batch_id", "concept": "batch", "concepts": ["batch"]}])
    monkeypatch.setattr(resolve_evidence, "sample_field_values", lambda **_kw: RUNS)
    monkeypatch.setattr(resolve_evidence, "probe_field_values", lambda *, values, **_kw: list(values))
    mapping = {**_run_mapping(), "concept": concept,
               "mappings": [{"concept": concept, "role": "identifier", "confidence": 0.95}]}
    return onboard._augment_with_affinity_rules(
        SimpleNamespace(system_id="mes-sys", mappings=[mapping]), {"rules": []})


def test_the_widened_view_assignment_agrees_through_the_catalog_relation(monkeypatch, caplog):
    """Live evidence (one-clock walk step 5, widened run view): the run number
    MAPS_TO ``production-run`` ONLY; the catalog declares ``production-run`` and
    ``batch`` interchangeable identifier concepts, so the hop is still emitted."""
    with caplog.at_level(logging.INFO, logger=onboard.logger.name):
        out = _run_alone(monkeypatch, "production-run")
    (rule,) = out["rules"]
    assert (rule["method"], rule["concept"], rule["target_field"]) == (
        aff.EXACT_METHOD, "batch", "shipment_items.batch_id")
    assert "catalog-related production-run" in rule["reasoning"]
    assert "exact pass: 1 pair(s) considered; refused concept=0 " in caplog.text


def test_an_unrelated_best_concept_is_still_refused(monkeypatch, caplog):
    """The concept refusal stands for a concept the catalog does not relate."""
    with caplog.at_level(logging.INFO, logger=onboard.logger.name):
        out = _run_alone(monkeypatch, "equipment")
    assert out["rules"] == []
    assert "exact pass: 1 pair(s) considered; refused concept=1 " in caplog.text


# ── production-order ~ production-run through the resolve seam ──────────────
# The Tulip connector's "ERP Order ID" column holds run numbers and MAPS_TO
# ``production-order``; a run-number column MAPS_TO ``production-run``.

class _SitedCatalog(_Catalog):
    """``_Catalog`` with a plant per system: ``{sid: (system_type, plant)}``."""

    def __init__(self, entries):
        super().__init__({sid: kind for sid, (kind, _plant) in entries.items()})
        self._plants = {sid: plant for sid, (_kind, plant) in entries.items()}

    def get(self, sid):
        entry = super().get(sid)
        return None if entry is None else {**entry, "plant": self._plants[sid]}


_PEER_RUNS = RUNS + [f"RUN-2026090{i}-8{i}000" for i in range(1, 6)]


def _seam(monkeypatch, entries, new, peer):
    """Resolve ``new`` against the already-onboarded ``peer`` through the real
    resolve seam. Each side is ``(system id, table, field, concept, values)``;
    ``new`` is mapped identifier, ``peer`` is read back as an identifier edge."""
    new_sid, new_table, new_field, new_concept, new_values = new
    peer_sid, peer_table, peer_field, peer_concept, peer_values = peer
    monkeypatch.setattr(onboard, "catalog", _SitedCatalog(entries))
    monkeypatch.setattr(onboard.self_learning, "_gate_cross_site_namespace", lambda *_a: ("", ""))
    monkeypatch.setattr(onboard, "neptune_query", lambda *_a, **_k: [
        {"sys_id": peer_sid, "sys_schema": f"s_{peer_sid}", "table_name": peer_table,
         "field_name": peer_field, "concept": peer_concept, "concepts": [peer_concept]}])
    columns = {(new_sid, new_table, new_field): list(new_values),
               (peer_sid, peer_table, peer_field): list(peer_values)}
    monkeypatch.setattr(resolve_evidence, "sample_field_values",
                        lambda *, system_id, table, field, **_kw: columns[(system_id, table, field)])
    monkeypatch.setattr(resolve_evidence, "probe_field_values",
                        lambda *, system_id, table, field, values, **_kw:
                        sorted(set(values) & set(columns[(system_id, table, field)])))
    mapping = {"table": new_table, "field": f"{new_table}.{new_field}",
               "concept": new_concept, "confidence": 0.95,
               "mappings": [{"concept": new_concept, "role": "identifier", "confidence": 0.95}]}
    return onboard._augment_with_affinity_rules(
        SimpleNamespace(system_id=new_sid, mappings=[mapping]), {"rules": []})


_TULIP_ORDER = ("mes-tul", "production_orders", "erp_order_id", "production-order", RUNS)


def _order_resolve(monkeypatch, entries, peer):
    """Resolve ``mes-tul``'s order-id column (``production-order``, identifier)
    against ``peer``'s run-number column (``production-run``), both holding
    the same run numbers."""
    return _seam(monkeypatch, entries, _TULIP_ORDER,
                 (peer, "production_runs", "run_number", "production-run", _PEER_RUNS))


def test_an_order_id_joins_another_systems_run_number_through_the_relation(monkeypatch, caplog):
    entries = {"mes-tul": ("MES", "Plant T"), "erp-sys": ("ERP", "All Sites")}
    with caplog.at_level(logging.INFO, logger=onboard.logger.name):
        out = _order_resolve(monkeypatch, entries, "erp-sys")
    (rule,) = out["rules"]
    assert (rule["method"], rule["concept"], rule["target_system"], rule["target_field"]) == (
        aff.EXACT_METHOD, "production-order", "erp-sys", "production_runs.run_number")
    assert "catalog-related production-run" in rule["reasoning"]
    assert ("exact pass: 1 pair(s) considered; refused concept=0 shape=0 probe/cardinality=0; "
            "qualified=1 emitted=1") in caplog.text


def test_two_mes_sites_are_never_joined_through_the_order_run_relation(monkeypatch, caplog):
    """The pair now agrees on concept and passes every measured gate, and the
    same-class cross-site guard still refuses it."""
    entries = {"mes-tul": ("MES", "Plant T"), "mes-oth": ("MES", "Plant O")}
    with caplog.at_level(logging.INFO, logger=onboard.logger.name):
        out = _order_resolve(monkeypatch, entries, "mes-oth")
    assert out["rules"] == []
    assert "qualified=1 emitted=1" in caplog.text
    assert "two mes systems at different sites are never joined" in caplog.text


def test_two_mes_systems_at_one_site_are_joined_through_the_order_run_relation(monkeypatch):
    entries = {"mes-tul": ("MES", "Plant T"), "mes-oth": ("MES", "Plant T")}
    (rule,) = _order_resolve(monkeypatch, entries, "mes-oth")["rules"]
    assert (rule["method"], rule["concept"]) == (aff.EXACT_METHOD, "production-order")


# ── production-order ~ batch through the resolve seam ───────────────────────
# The ERP holds its run numbers only in the shipment line's ``batch_id``
# (``batch``), so the Tulip "ERP Order ID" column (``production-order``)
# reaches the ERP through this pair.

_ERP_SHIP = ("erp-sys", "shipment_items", "batch_id", "batch", _PEER_RUNS)
_ERP_AND_TULIP = {"mes-tul": ("MES", "Plant T"), "erp-sys": ("ERP", "All Sites")}


def test_an_order_id_joins_the_erp_batch_column_that_holds_its_values(monkeypatch, caplog):
    with caplog.at_level(logging.INFO, logger=onboard.logger.name):
        out = _seam(monkeypatch, _ERP_AND_TULIP, _TULIP_ORDER, _ERP_SHIP)
    (rule,) = out["rules"]
    assert (rule["method"], rule["concept"], rule["target_system"], rule["target_field"]) == (
        aff.EXACT_METHOD, "batch", "erp-sys", "shipment_items.batch_id")
    assert "concept batch (catalog-related production-order)" in rule["reasoning"]
    assert rule["shared_count"] == len(RUNS)
    assert ("exact pass: 1 pair(s) considered; refused concept=0 shape=0 probe/cardinality=0; "
            "qualified=1 emitted=1") in caplog.text


def test_the_erp_resolve_joins_an_onboarded_order_id_column(monkeypatch):
    """The other side resolving: the ERP is the new system and the order
    column is an existing system's identifier edge. Same pair, same concept."""
    (rule,) = _seam(monkeypatch, _ERP_AND_TULIP, _ERP_SHIP, _TULIP_ORDER)["rules"]
    assert (rule["method"], rule["concept"], rule["target_system"], rule["target_field"]) == (
        aff.EXACT_METHOD, "batch", "mes-tul", "production_orders.erp_order_id")
    assert "catalog-related production-order" in rule["reasoning"]


def test_two_mes_sites_are_never_joined_through_the_order_batch_pair(monkeypatch, caplog):
    """An order column and another site's MES batch column holding the same
    values qualify in the pass and are dropped by the same-class cross-site guard."""
    entries = {"mes-tul": ("MES", "Plant T"), "mes-oth": ("MES", "Plant O")}
    batch = ("mes-oth", "batches", "batch_number", "batch", _PEER_RUNS)
    with caplog.at_level(logging.INFO, logger=onboard.logger.name):
        out = _seam(monkeypatch, entries, _TULIP_ORDER, batch)
    assert out["rules"] == []
    assert "qualified=1 emitted=1" in caplog.text
    assert "two mes systems at different sites are never joined" in caplog.text
