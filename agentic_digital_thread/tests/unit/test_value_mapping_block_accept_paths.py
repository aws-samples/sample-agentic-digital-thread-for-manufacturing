# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Which accept paths make a steward-confirmed ValueMapping the IDENTIFIER
VALUE MAPPINGS block may carry (ADR-0035 amendment 2026-10-18).

Every accept writes the same ``user_confirmed`` node under the same
``subj-`` id, so the method and id alone cannot tell a mapping a person
reviewed on its own from one a bulk or automatic accept let through. Only
the route a person uses to accept ONE proposal, and a steward's direct
correction, stamp the node with ``confirmation_mode`` and ``accepted_by``;
a replay keeps that stamp, and every other accept writes it empty (also over
an earlier stamp). These tests drive each real accept path end to end over
an in-memory proposal store and the in-memory graph of
``test_offboard_deletes_value_mappings``, then ask the block's own trust test
(``identifier_translation.value_mapping_trusted``) about the node the path
wrote.

Offline: no AWS, no network. Synthetic values only.
"""
from __future__ import annotations

import asyncio

import pytest

import agents.identifier_translation as it
import agents.onboard as onboard
import agents.proposal_batch as proposal_batch
import agents.proposal_scoring as proposal_scoring
import agents.self_learning as self_learning
import agents.server as server
from agents import schema_check
from tests.unit.test_offboard_deletes_value_mappings import graph  # noqa: F401 - fixture
from tests.unit.test_self_learning_lifecycle import FakeTable

STEWARD = "steward@example.test"


@pytest.fixture
def store(monkeypatch, graph):  # noqa: F811 - the graph fixture
    table = FakeTable()
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)
    monkeypatch.setattr(self_learning, "PROPOSAL_STAGING_MIN_COREFERENCES", 1)
    monkeypatch.delenv("SELF_LEARNING_AUTO_ACCEPT", raising=False)
    return table


def _propose(to_value="T-77") -> str:
    """A pending agent proposal: wo.machine M-77 in cmms is assets.asset_tag
    ``to_value`` in erp (a value mapping, so it accepts as a ValueMapping)."""
    return self_learning.log_proposed_mapping(
        "cmms", "wo.machine", "M-77", "erp", "assets.asset_tag", to_value,
        "equipment-id", 0.9, "ctx", proposed_type="value_mapping", source="agent")


def _node(graph, store, mapping_id) -> dict:  # noqa: F811
    node_id = store.by_id(mapping_id)["neptune_node_id"]
    assert node_id.startswith("subj-")
    return {**graph.value_mappings()[node_id], "id": node_id}


def _high_confidence(monkeypatch):
    """Make the sweep's gates pass: the scorer says high, nothing blocks."""
    monkeypatch.setattr(proposal_scoring, "score_proposal",
                        lambda _item: {"ai_confidence": "high"})
    monkeypatch.setattr(self_learning, "_auto_accept_block_reason", lambda _item: "")


# ---------------------------------------------------------------- the paths that stamp

def test_a_steward_accepting_one_proposal_makes_a_trusted_mapping(graph, store):  # noqa: F811
    mid = _propose()
    result = server._accept_proposed_mapping_sync(mid, STEWARD, None)
    assert result["status"] == "accepted"
    node = _node(graph, store, mid)
    assert node["method"] == "user_confirmed"
    assert (node["confirmation_mode"], node["accepted_by"]) == (it.SINGLE_ROW_CONFIRMATION, STEWARD)
    assert it.value_mapping_trusted(node)
    assert store.by_id(mid)["confirmation_mode"] == it.SINGLE_ROW_CONFIRMATION


def test_a_one_row_accept_that_names_nobody_is_not_trusted(graph, store):  # noqa: F811
    """With auth off the route has no actor: no person, no stamp."""
    mid = _propose()
    assert server._accept_proposed_mapping_sync(mid, "", None)["status"] == "accepted"
    node = _node(graph, store, mid)
    assert node["confirmation_mode"] == "" and not it.value_mapping_trusted(node)


def test_a_stewards_direct_correction_makes_a_trusted_mapping(graph, store, monkeypatch):  # noqa: F811
    monkeypatch.setattr(schema_check, "is_onboarded_system", lambda _sid: True)
    monkeypatch.setattr(schema_check, "column_exists", lambda *_a: None)
    body = server.UserDefinedMappingRequest(
        from_system="cmms", from_field="wo.machine", from_value="M-77", to_system="erp",
        to_field="assets.asset_tag", to_value="T-77", concept="equipment-id")
    result = server.user_define_mapping(body, True, STEWARD)
    assert result["status"] == "accepted"
    node = {**graph.value_mappings()[result["neptune_node_id"]], "id": result["neptune_node_id"]}
    assert (node["confirmation_mode"], node["accepted_by"]) == (it.SINGLE_ROW_CONFIRMATION, STEWARD)
    assert it.value_mapping_trusted(node)


def test_anyone_elses_assertion_writes_no_mapping(graph, store, monkeypatch):  # noqa: F811
    monkeypatch.setattr(schema_check, "is_onboarded_system", lambda _sid: True)
    body = server.UserDefinedMappingRequest(
        from_system="cmms", from_field="wo.machine", from_value="M-77", to_system="erp",
        to_field="assets.asset_tag", to_value="T-77", concept="equipment-id")
    assert server.user_define_mapping(body, False, "user@example.test")["status"] == "pending"
    assert graph.value_mappings() == {}


# ---------------------------------------------------------------- the paths that do not

def test_an_explicit_batch_accept_is_not_trusted(graph, store):  # noqa: F811
    mid = _propose()
    out = proposal_batch.adjudicate("accept", [mid], actor=STEWARD)
    assert out["succeeded"] == 1, out
    node = _node(graph, store, mid)
    assert node["confirmation_mode"] == "" and not it.value_mapping_trusted(node)


def test_accept_all_is_not_trusted(graph, store):  # noqa: F811
    mid = _propose()
    assert self_learning.accept_all_pending()["count"] == 1
    assert not it.value_mapping_trusted(_node(graph, store, mid))


def test_the_high_confidence_endpoint_is_not_trusted(graph, store, monkeypatch):  # noqa: F811
    _high_confidence(monkeypatch)
    mid = _propose()
    assert asyncio.run(server.accept_high_confidence_mappings())["accepted"] == 1
    assert not it.value_mapping_trusted(_node(graph, store, mid))


@pytest.mark.parametrize("source", ["periodic", "post-ask"])
def test_the_periodic_and_post_ask_sweep_is_not_trusted(graph, store, monkeypatch, source):  # noqa: F811
    _high_confidence(monkeypatch)
    mid = _propose()
    assert server._run_auto_accept_sweep(source)["accepted"] == 1
    assert not it.value_mapping_trusted(_node(graph, store, mid))


def test_a_fresh_log_auto_accept_is_not_trusted(graph, store, monkeypatch):  # noqa: F811
    _high_confidence(monkeypatch)
    monkeypatch.setenv("SELF_LEARNING_AUTO_ACCEPT", "1")
    mid = _propose()
    assert store.by_id(mid)["status"] == "accepted"
    assert not it.value_mapping_trusted(_node(graph, store, mid))


def test_a_bulk_accept_over_an_earlier_one_row_accept_clears_the_stamp(graph, store):  # noqa: F811
    """The same subject node: a later proposal to another value, accepted in
    bulk, rewrites the node, and the earlier person's stamp does not carry
    over to the new value."""
    first = _propose("T-77")
    server._accept_proposed_mapping_sync(first, STEWARD, None)
    assert it.value_mapping_trusted(_node(graph, store, first))
    second = _propose("T-78")
    assert self_learning.accept_all_pending()["count"] == 1
    node = _node(graph, store, second)
    assert node["id"] == _node(graph, store, first)["id"] and node["to_value"] == "T-78"
    assert (node["confirmation_mode"], node["accepted_by"]) == ("", "")
    assert not it.value_mapping_trusted(node)


# ---------------------------------------------------------------- the replay

def _replayed(graph, store, mid) -> dict:  # noqa: F811
    """Re-provision: the graph lost the node; the accepted ledger row replays."""
    item = store.by_id(mid)
    graph.nodes.pop(("ValueMapping", item["neptune_node_id"]))
    assert onboard._dp_replay_learned_vm(item)
    return {**graph.value_mappings()[item["neptune_node_id"]], "id": item["neptune_node_id"]}


def test_a_replay_keeps_the_one_row_stamp(graph, store):  # noqa: F811
    mid = _propose()
    server._accept_proposed_mapping_sync(mid, STEWARD, None)
    node = _replayed(graph, store, mid)
    assert (node["confirmation_mode"], node["accepted_by"]) == (it.SINGLE_ROW_CONFIRMATION, STEWARD)
    assert it.value_mapping_trusted(node)


def test_a_replay_of_a_bulk_accept_is_not_trusted(graph, store):  # noqa: F811
    mid = _propose()
    self_learning.accept_all_pending()
    assert not it.value_mapping_trusted(_replayed(graph, store, mid))


def test_a_replay_of_a_row_accepted_before_the_stamp_existed_is_not_trusted(graph, store):  # noqa: F811
    """A ledger row from before the stamp names its steward but no mode."""
    mid = _propose()
    server._accept_proposed_mapping_sync(mid, STEWARD, None)
    row = store.by_id(mid)
    del row["confirmation_mode"]
    store.put_item(row)
    node = _replayed(graph, store, mid)
    assert node["accepted_by"] == "" and not it.value_mapping_trusted(node)


def test_a_requeued_row_accepted_again_in_bulk_loses_the_stamp_for_the_replay(graph, store):  # noqa: F811
    """Requeue keeps the row's history; the bulk re-accept writes the mode
    empty on the ledger row, so a later replay does not bring the earlier
    person's stamp back."""
    mid = _propose()
    server._accept_proposed_mapping_sync(mid, STEWARD, None)
    assert self_learning.requeue_mapping(mid, actor=STEWARD)["status"] == "requeued"
    assert store.by_id(mid)["confirmation_mode"] == it.SINGLE_ROW_CONFIRMATION  # history kept
    assert self_learning.accept_all_pending()["count"] == 1
    assert store.by_id(mid)["confirmation_mode"] == ""
    assert not it.value_mapping_trusted(_node(graph, store, mid))
    assert not it.value_mapping_trusted(_replayed(graph, store, mid))
