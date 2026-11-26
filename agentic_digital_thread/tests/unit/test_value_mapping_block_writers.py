# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Which ValueMapping writers the IDENTIFIER VALUE MAPPINGS block trusts
(ADR-0035 amendment 2026-10-18).

The block treats a stored mapping as the translation, so only two kinds of
row may reach it: one a deterministic onboarding match wrote, and one a
steward accepted one row at a time (the node's one-row stamp; which accept
paths write it is in ``test_value_mapping_block_accept_paths.py``). The method alone cannot say which: the resolve step's model
fills its rows' ``method`` (``vm-xsys-`` ids), so a model can claim
``value_format_match``, and may claim ``user_confirmed`` (the steward tier).
These tests write rows through the REAL writers, on the
in-memory graph of ``test_offboard_deletes_value_mappings``, and check that
the trust test (``identifier_translation.value_mapping_trusted``) keys on the
writer as well as the method, and that the resolve writer stores a model's
claim of a steward confirmation as a model proposal.

Offline: the graph is the fake; no AWS, no network.
"""
from __future__ import annotations

import pytest

import agents.identifier_translation as it
import agents.onboard as onboard
import agents.self_learning as self_learning
from tests.unit.test_offboard_deletes_value_mappings import (  # noqa: F401 - graph is a fixture
    _c3, _cross_vm, _equipment, _learned_item, _steward_accepted, graph)


def _stored(graph, vm_id) -> dict:
    return {**graph.value_mappings()[vm_id], "id": vm_id}


@pytest.mark.parametrize("claimed", ["user_confirmed", "crosswalk_confirmed", "made_up"])
def test_resolve_stores_a_models_claim_of_a_steward_tier_as_a_model_proposal(graph, claimed):
    written, _ctx = _c3(graph, "cmms", _cross_vm("cmms", "erp", "U1", method=claimed))
    assert written
    row = _stored(graph, "vm-xsys-cmms-erp-0")
    assert row["method"] == "semantic_similarity"
    assert not it.value_mapping_trusted(row)


@pytest.mark.parametrize("method", ["semantic_similarity", "hierarchy_position",
                                    "value_format_match"])
def test_resolve_keeps_the_models_own_method_and_the_block_never_trusts_it(graph, method):
    written, _ctx = _c3(graph, "cmms", _cross_vm("cmms", "erp", "U2", method=method))
    assert written
    row = _stored(graph, "vm-xsys-cmms-erp-0")
    assert row["method"] == method
    assert not it.value_mapping_trusted(row)


def test_the_deterministic_equipment_writer_is_trusted(graph):
    vm_id = _equipment("cmms", "sitewise-direct", "E1")
    assert vm_id.startswith(onboard._EQUIPMENT_VM_PREFIX)
    assert it.value_mapping_trusted(_stored(graph, vm_id))


#: The one-row accept's stamp as the ledger item carries it.
ONE_ROW = {"confirmation_mode": "single_row", "accepted_by": "steward@example.test"}


@pytest.mark.parametrize("stamp, trusted", [(ONE_ROW, True), ({}, False),
                                            ({"accepted_by": "steward@example.test"}, False)])
@pytest.mark.parametrize("kind, method", [("value_mapping", "user_confirmed"),
                                          ("crosswalk", "crosswalk_confirmed")])
def test_a_steward_accept_is_trusted_only_with_the_one_row_stamp(graph, kind, method, stamp,
                                                                 trusted):
    vm_id = f"subj-accept{kind}"
    self_learning._apply_value_mapping(
        {"from_system": "cmms", "from_field": "parts.part_no", "from_value": "P-1",
         "to_system": "erp", "to_field": "items.item", "to_value": "I-1", "confidence": 0.9,
         **stamp}, vm_id, 1, "part-number", kind)
    row = _stored(graph, vm_id)
    assert row["method"] == method
    assert (row["confirmation_mode"], row["accepted_by"]) == (
        (it.SINGLE_ROW_CONFIRMATION, "steward@example.test") if trusted else ("", ""))
    assert it.value_mapping_trusted(row) is trusted
    assert self_learning._subject_id("any subject key").startswith(
        it.CONFIRMED_VALUE_MAPPING_ID_PREFIXES)
    assert _steward_accepted("cmms", "erp", "S2").startswith(it.CONFIRMED_VALUE_MAPPING_ID_PREFIXES)


@pytest.mark.parametrize("stamp, trusted", [(ONE_ROW, True), ({}, False)])
@pytest.mark.parametrize("node_id", ["subj-replayR1", None])
def test_a_replayed_steward_accept_keeps_the_one_row_stamp(graph, node_id, stamp, trusted):
    item = {**_learned_item("cmms", "erp", "R1"), **stamp}
    if node_id is None:
        del item["neptune_node_id"]
    assert onboard._dp_replay_learned_vm(item)
    (vm_id,) = graph.value_mappings()
    assert vm_id.startswith(it.CONFIRMED_VALUE_MAPPING_ID_PREFIXES)
    assert it.value_mapping_trusted(_stored(graph, vm_id)) is trusted


def test_the_trusted_id_prefixes_are_the_writers_own():
    assert it.DETERMINISTIC_VALUE_MAPPING_ID_PREFIXES == (onboard._EQUIPMENT_VM_PREFIX,)
    assert not any("vm-xsys-".startswith(p) or p.startswith("vm-xsys-")
                   for p in (*it.DETERMINISTIC_VALUE_MAPPING_ID_PREFIXES,
                             *it.CONFIRMED_VALUE_MAPPING_ID_PREFIXES))
