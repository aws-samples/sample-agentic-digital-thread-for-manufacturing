# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Wave 3A (ADR-0040 D4): entity-anchored IdentityRule candidates.

Two seams under test:
  * ``onboard._do_resolve`` — after the shared-field-concept pass, an entity-anchored
    pass reads the entity (``SchemaEntry-[:MAPS_TO]->Concept``) + identity
    (``SchemaEntry-[:IDENTIFIED_BY]->Field``) edges from the graph in BOTH directions and
    feeds the shared-entity identity fields as join-key candidate pairs into the SAME
    single rule-proposal ``bedrock_call`` (no second LLM call).
  * ``resolve._try_entity_anchored`` — the resolve_entity fallback that searches identity
    fields of systems sharing an entity concept when no ``role='identifier'`` field matched.

All fixtures use SYNTHETIC identifiers (WO-*, entity ids) that mirror the SHAPE of real
data without literal customer/dataset values, reusing the injection pattern from
``test_resolve_evidence.py``.
"""
from unittest.mock import patch


_NEW = "sys-wh"


def _catalog_get(sid):
    return {
        _NEW: {"schema": "", "connection_type": "Athena",
               "connection_config": {"workgroup": "wg", "database": "db", "region": "us-west-2"}},
        "sys-erp": {"connection_type": "PostgreSQL", "schema": "erp_schema"},
    }.get(sid, {})


# =========================================================================
# _entity_anchored_pairs — pure-ish graph read (order-independent, both directions)
# =========================================================================

def test_entity_anchored_pairs_pairs_shared_entity_both_directions():
    from agents.onboard import _entity_anchored_pairs

    entity_rows = [
        {"sys_id": _NEW, "table_name": "records", "entity": "work-order", "id_fields": ["wo_ref"]},
        {"sys_id": "sys-erp", "table_name": "orders", "entity": "work-order", "id_fields": ["order_number"]},
        # An entity the new system does NOT share → must not produce a pair.
        {"sys_id": "sys-erp", "table_name": "assets", "entity": "equipment-id", "id_fields": ["asset_num"]},
    ]
    with patch("agents.onboard.neptune_query", return_value=entity_rows):
        pairs = _entity_anchored_pairs(_NEW)

    assert len(pairs) == 1
    p = pairs[0]
    assert p == {"entity": "work-order", "new_table": "records", "new_field": "wo_ref",
                 "tgt_system": "sys-erp", "tgt_table": "orders", "tgt_field": "order_number"}


def test_entity_anchored_pairs_empty_when_no_identity_or_entity():
    from agents.onboard import _entity_anchored_pairs
    # Rows lacking entity/id_fields (e.g. the field-concept query shape) yield nothing.
    with patch("agents.onboard.neptune_query", return_value=[
        {"sys_id": _NEW, "field_name": "wo_ref", "concept": "work-order", "table_name": "records"},
    ]):
        assert _entity_anchored_pairs(_NEW) == []
    with patch("agents.onboard.neptune_query", return_value=[]):
        assert _entity_anchored_pairs(_NEW) == []


# =========================================================================
# _do_resolve — entity-anchored pass rescues a novel table with no shared field concept
# =========================================================================

def _nq_side_effect(entity_rows, existing_fields):
    def _nq(query, params=None):
        if "IDENTIFIED_BY" in query:
            return entity_rows
        return existing_fields
    return _nq


def _sample_side_effect(**kw):
    sid, field = kw.get("system_id"), kw.get("field")
    if sid == _NEW and field == "wo_ref":
        return ["WO-1", "WO-2"]
    if sid == "sys-erp" and field == "order_number":
        return ["WO-1"]
    return []


def test_do_resolve_entity_anchored_rescues_when_no_shared_field_concept():
    from agents.onboard import _do_resolve
    from agents.models import OnboardResolveRequest

    # The existing system has a field-concept mapping, but it is NOT shared with the new
    # system (whose only mapping is unclassified), so the shared-field-concept pass finds
    # NOTHING. Only the entity edge links them.
    existing_fields = [
        {"sys_id": "sys-erp", "sys_name": "ERP", "sys_schema": "erp_schema",
         "field_name": "order_number", "concept": "work-order", "table_name": "orders",
         "role": "identifier"},
    ]
    entity_rows = [
        {"sys_id": _NEW, "table_name": "records", "entity": "work-order", "id_fields": ["wo_ref"]},
        {"sys_id": "sys-erp", "table_name": "orders", "entity": "work-order", "id_fields": ["order_number"]},
    ]
    body = OnboardResolveRequest(system_id=_NEW, mappings=[
        {"table": "records", "field": "wo_ref", "concept": "needs_review", "confidence": 0.3},
    ])
    bedrock_json = ('{"rules": [{"concept":"work-order","source_field":"records.wo_ref",'
                    '"target_system":"sys-erp","target_field":"orders.order_number",'
                    '"source_pattern":"WO-{n}","target_pattern":"WO-{n}","confidence":0.9}], '
                    '"value_mappings": []}')

    with patch("agents.onboard.neptune_query", side_effect=_nq_side_effect(entity_rows, existing_fields)), \
            patch("agents.onboard.catalog") as mock_cat, \
            patch("agents.onboard.bedrock_call", return_value=bedrock_json) as mock_bedrock, \
            patch("agents.connection_resolver.resolve_connection", return_value={}), \
            patch("agents.resolve_evidence.sample_field_values", side_effect=_sample_side_effect):
        mock_cat.get.side_effect = _catalog_get
        result = _do_resolve(body)

    # Exactly ONE rule-proposal LLM call (entity pairs fold into the same prompt).
    assert mock_bedrock.call_count == 1
    # Entity-anchored evidence recorded with the measured overlap on the shared synthetic key.
    ev = [e for e in result["evidence"] if e["concept"] == "work-order"]
    assert ev, result
    assert ev[0]["overlap_count"] >= 1
    assert "WO-1" in ev[0]["examples"]
    assert ev[0]["target_system"] == "sys-erp"
    # The rule the LLM proposed on the entity-anchored pair is returned.
    assert any(r.get("concept") == "work-order" for r in result["rules"])


def test_do_resolve_no_entity_edges_is_backward_compatible():
    """When the entity query returns nothing, the shared-field-concept path is unchanged:
    the entity-anchored pass is a pure no-op (no regression)."""
    from agents.onboard import _do_resolve
    from agents.models import OnboardResolveRequest

    existing_fields = [
        {"sys_id": "sys-erp", "sys_name": "ERP", "sys_schema": "erp_schema",
         "field_name": "order_number", "concept": "work-order", "table_name": "orders",
         "role": "identifier"},
    ]
    body = OnboardResolveRequest(system_id=_NEW, mappings=[
        {"table": "records", "field": "wo_ref", "concept": "work-order", "confidence": 0.9},
    ])

    def _sample(**kw):
        sid, field = kw.get("system_id"), kw.get("field")
        if sid == _NEW and field == "wo_ref":
            return ["WO-1"]
        if sid == "sys-erp" and field == "order_number":
            return ["WO-1"]
        return []

    with patch("agents.onboard.neptune_query", side_effect=_nq_side_effect([], existing_fields)), \
            patch("agents.onboard.catalog") as mock_cat, \
            patch("agents.onboard.bedrock_call",
                  return_value='{"rules": [{"concept":"work-order","source_field":"records.wo_ref",'
                               '"target_system":"sys-erp","target_field":"orders.order_number",'
                               '"confidence":0.9}], "value_mappings": []}') as mock_bedrock, \
            patch("agents.connection_resolver.resolve_connection", return_value={}), \
            patch("agents.resolve_evidence.sample_field_values", side_effect=_sample):
        mock_cat.get.side_effect = _catalog_get
        result = _do_resolve(body)

    assert mock_bedrock.call_count == 1
    # The shared FIELD-CONCEPT path still fires (evidence for the shared 'work-order').
    assert any(e["concept"] == "work-order" for e in result["evidence"])


# =========================================================================
# resolve._try_entity_anchored — resolve_entity fallback over identity edges
# =========================================================================

def test_try_entity_anchored_finds_via_identity_edge():
    from agents.resolve import _try_entity_anchored

    raw, resolution, unified = [], [], []
    queried: set = set()
    entity_rows = [{"sys_id": "sys-erp", "field_name": "order_number", "table_name": "orders"}]

    with patch("agents.resolve.neptune_query", return_value=entity_rows), \
            patch("agents.resolve.get_schema", return_value="erp_schema"), \
            patch("agents.resolve.get_name", return_value="ERP"), \
            patch("agents.resolve.catalog") as cat, \
            patch("agents.resolve.pg",
                  return_value={"columns": ["order_number", "status"], "rows": [["WO-1", "open"]]}):
        cat.get.return_value = {"connection_type": "PostgreSQL"}
        _try_entity_anchored("work-order", "WO-1", queried, raw, resolution, unified)

    assert "sys-erp" in queried
    assert resolution and "Entity match" in resolution[0]["rule"]
    assert unified and unified[0]["title"] == "ERP"


def test_try_entity_anchored_skips_already_queried_system():
    from agents.resolve import _try_entity_anchored

    raw, resolution, unified = [], [], []
    queried = {"sys-erp"}
    entity_rows = [{"sys_id": "sys-erp", "field_name": "order_number", "table_name": "orders"}]

    with patch("agents.resolve.neptune_query", return_value=entity_rows), \
            patch("agents.resolve.get_schema", return_value="erp_schema"), \
            patch("agents.resolve.get_name", return_value="ERP"), \
            patch("agents.resolve.catalog") as cat, \
            patch("agents.resolve.pg") as mock_pg:
        cat.get.return_value = {"connection_type": "PostgreSQL"}
        _try_entity_anchored("work-order", "WO-1", queried, raw, resolution, unified)

    mock_pg.assert_not_called()
    assert resolution == []
