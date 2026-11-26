# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A demoted identifier is an exact-pass candidate (ADR-0087 amendment
2026-10-02). Live evidence (fresh workshop account, 2026-10-02): in two of
four site MESs the model mapped the run view's surrogate ``production_run_id``
as the ``production-run`` identifier and the business key ``run_number`` as a
``production-run`` correlate, so the identifier-role candidacy filter dropped
the run number and the shipment-line join was never considered. In the other
two the surrogate stayed unmapped, the run number kept the identifier role
and the join was emitted. A correlate-only mapping to a concept another column
of its table holds as identifier now reaches the EXACT pass only.
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

RUNS = [f"RUN-2026100{d}-9{d}{k:03d}" for d in (1, 2, 3) for k in range(6)]
SURROGATES = [str(i) for i in range(1, len(RUNS) + 1)]


class _Catalog:
    def __init__(self, types):
        self._types = types

    def get(self, sid):
        kind = self._types.get(sid)
        return None if kind is None else {"id": sid, "system_type": kind, "plant": "Plant 1",
                                          "schema": f"s_{sid}", "connection_type": "PostgreSQL"}

    def get_onboarded_ids(self):
        return set(self._types)


def _mapping(table, field, concept, role, confidence=0.9):
    return {"table": table, "field": f"{table}.{field}", "concept": concept,
            "confidence": confidence,
            "mappings": [{"concept": concept, "role": role, "confidence": confidence}]}


def _live_run_view():
    """The 2026-10-02 assignment: surrogate key identifier, business key correlate."""
    return [_mapping("production_runs", "production_run_id", "production-run", "identifier", 0.95),
            _mapping("production_runs", "run_number", "production-run", "correlate", 0.9)]


def _sampled(monkeypatch, columns):
    monkeypatch.setattr(resolve_evidence, "sample_field_values",
                        lambda *, system_id, table, field, **_kw: columns.get((system_id, table, field), []))


def test_the_demoted_business_key_is_an_exact_only_candidate(monkeypatch):
    monkeypatch.setattr(onboard, "catalog", _Catalog({"mes-sys": "MES"}))
    _sampled(monkeypatch, {})
    fields = onboard._affinity_new_fields("mes-sys", _live_run_view())
    assert [(f.field, f.exact_only) for f in fields] == [
        ("production_run_id", False), ("run_number", True)]


def test_a_correlate_without_an_identifier_twin_in_its_table_stays_out(monkeypatch):
    """A correlate whose concept no column of ITS table holds as identifier
    (another table's identifier does not count) is not a candidate."""
    monkeypatch.setattr(onboard, "catalog", _Catalog({"mes-sys": "MES"}))
    _sampled(monkeypatch, {})
    fields = onboard._affinity_new_fields("mes-sys", [
        _mapping("production_runs", "production_run_id", "production-run", "identifier"),
        _mapping("work_orders", "production_run_id", "production-run", "correlate"),
        _mapping("production_runs", "status", "work-order", "correlate"),
        _mapping("production_runs", "planned_quantity", "production-run", "measure")])
    assert [(f.table, f.field) for f in fields] == [("production_runs", "production_run_id")]


def test_a_mapping_with_both_roles_stays_a_full_candidate(monkeypatch):
    monkeypatch.setattr(onboard, "catalog", _Catalog({"mes-sys": "MES"}))
    _sampled(monkeypatch, {})
    both = {**_mapping("production_runs", "run_number", "production-run", "identifier"),
            "mappings": [{"concept": "production-run", "role": "identifier", "confidence": 0.95},
                         {"concept": "batch", "role": "correlate", "confidence": 0.85}]}
    (field,) = onboard._affinity_new_fields("mes-sys", [both])
    assert field.exact_only is False


def test_the_live_assignment_yields_the_run_number_hop(monkeypatch, caplog):
    """End to end through the resolve seam: the surrogate is refused on shape
    (a 1..N enumeration), the demoted run number joins the shipment line."""
    monkeypatch.setattr(onboard, "catalog", _Catalog({"mes-sys": "MES", "erp-sys": "ERP"}))
    monkeypatch.setattr(onboard.self_learning, "_gate_cross_site_namespace", lambda *_a: ("", ""))
    monkeypatch.setattr(onboard, "neptune_query", lambda *_a, **_k: [
        {"sys_id": "erp-sys", "sys_schema": "s_erp", "table_name": "shipment_items",
         "field_name": "batch_id", "concept": "batch", "concepts": ["batch"]}])
    columns = {("mes-sys", "production_runs", "production_run_id"): SURROGATES,
               ("mes-sys", "production_runs", "run_number"): RUNS,
               ("erp-sys", "shipment_items", "batch_id"): RUNS + [f"RUN-2026090{i}-8{i}000" for i in range(1, 6)]}
    _sampled(monkeypatch, columns)
    monkeypatch.setattr(resolve_evidence, "probe_field_values",
                        lambda *, system_id, table, field, values, **_kw:
                        sorted(set(values) & set(columns[(system_id, table, field)])))
    body = SimpleNamespace(system_id="mes-sys", mappings=_live_run_view())
    with caplog.at_level(logging.INFO, logger=onboard.logger.name):
        out = onboard._augment_with_affinity_rules(body, {"rules": []})
    (rule,) = out["rules"]
    assert (rule["method"], rule["source_field"], rule["target_field"]) == (
        aff.EXACT_METHOD, "production_runs.run_number", "shipment_items.batch_id")
    assert "catalog-related production-run" in rule["reasoning"]
    assert ("exact pass: 2 pair(s) considered; refused concept=0 shape=1 "
            "probe/cardinality=0; qualified=1 emitted=1") in caplog.text


def test_without_the_identifier_twin_the_run_number_is_never_considered(monkeypatch, caplog):
    """The same correlate alone (no identifier column in its table) is not
    admitted: the widening is the demotion case only."""
    monkeypatch.setattr(onboard, "catalog", _Catalog({"mes-sys": "MES", "erp-sys": "ERP"}))
    monkeypatch.setattr(onboard, "neptune_query", lambda *_a, **_k: [
        {"sys_id": "erp-sys", "sys_schema": "s_erp", "table_name": "shipment_items",
         "field_name": "batch_id", "concept": "batch", "concepts": ["batch"]}])
    _sampled(monkeypatch, {("mes-sys", "production_runs", "run_number"): RUNS,
                           ("erp-sys", "shipment_items", "batch_id"): RUNS})
    monkeypatch.setattr(resolve_evidence, "probe_field_values", lambda *, values, **_kw: list(values))
    body = SimpleNamespace(system_id="mes-sys", mappings=[_live_run_view()[1]])
    with caplog.at_level(logging.INFO, logger=onboard.logger.name):
        out = onboard._augment_with_affinity_rules(body, {"rules": []})
    assert out["rules"] == []
    assert "exact pass: 0 pair(s) considered" in caplog.text


def _field(system_id, values, exact_only=False):
    return aff.SampledField(system_id, "t", "f", "batch", tuple(values),
                            concepts=frozenset({"batch"}), exact_only=exact_only)


def test_an_exact_only_side_never_reaches_the_pattern_passes(monkeypatch):
    calls = []
    monkeypatch.setattr(aff, "_pattern_rule", lambda *_a: calls.append(_a) or {"rule": "pattern"})
    probe = lambda _field, values: list(values)  # noqa: E731
    src, tgt = _field("a", RUNS, exact_only=True), _field("b", RUNS)
    rule = aff.affinity_rule(src, tgt, probe)
    assert calls == [] and rule["method"] == aff.EXACT_METHOD
    assert aff.affinity_rule(tgt, src, probe)["method"] == aff.EXACT_METHOD
    assert calls == []
    assert aff.affinity_rule(src, tgt) is None   # no probe: nothing may join it


def test_full_candidates_still_run_the_pattern_passes_first(monkeypatch):
    monkeypatch.setattr(aff, "_pattern_rule", lambda *_a: {"rule": "pattern"})
    assert aff.affinity_rule(_field("a", RUNS), _field("b", RUNS), lambda _f, v: list(v)) == {
        "rule": "pattern"}


def test_exact_only_is_not_part_of_field_identity():
    assert _field("a", RUNS, exact_only=True) == _field("a", RUNS)
