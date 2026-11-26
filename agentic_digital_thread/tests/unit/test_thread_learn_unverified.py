# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A rule taught without its examples is not stamped as confirmed.

``POST /api/thread/learn`` compares the two identifier readers on the
identifier a rule was learned from and the value it matched when the request
carries them (the Thread panel always does), and otherwise only on a value
built from the pattern, which cannot expose a case or placeholder-width
difference. Both kinds of request were stamped ``user_confirmed``, the tier
every reader ranks first. A request without both examples is now written
with the method ``user_unverified``: tier 1 in every reader (an unconfirmed
rule with recorded provenance), an origin_context that says why, a note in
the answer, and kept by re-provision like a confirmed rule.

Neutral systems and values; the graph is a fake.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

from agents import identifier_translation, onboard, orchestrator, resolve, route_seed_trace  # noqa: E402
from agents.models import LearnRequest  # noqa: E402

SRC_SYS, TGT_SYS = "press-log", "tool-crib"


@pytest.fixture
def graph(monkeypatch):
    writes: list = []
    monkeypatch.setattr(resolve, "neptune_query", lambda q, p=None: [{"s.id": TGT_SYS}])
    monkeypatch.setattr(resolve, "get_schema", lambda sid: "")
    monkeypatch.setattr(resolve, "neptune_write", lambda q, p=None: writes.append((q, p)) or True)
    return writes


def _learn(**examples) -> dict:
    return resolve.thread_learn(LearnRequest(
        concept="die-set", source_system=SRC_SYS, source_field="die_code", source_pattern="D-{id}",
        target_system=TGT_SYS, target_field="crib_ref", target_pattern="CR-D-{id}", **examples))


def _created(writes) -> tuple:
    ((cypher, params),) = [(c, p) for c, p in writes if c.startswith("CREATE (ir:IdentityRule")]
    return cypher, params


def _stored(cypher, params) -> dict:
    """The node as a reader sees it: the CREATE's parameters under their
    property names (``method``, ``provenance``, ``origin_context``)."""
    node = {"method": params["method"], "provenance": "human-asserted"}
    if "origin_context: $octx" in cypher:
        node["origin_context"] = params["octx"]
    return node


def test_a_rule_taught_without_examples_is_written_unverified_at_tier_1(graph):
    """REPRODUCTION: a direct call with no examples got user_confirmed (tier 0)."""
    out = _learn()
    assert out["status"] == "created"
    cypher, params = _created(graph)
    assert "method: $method" in cypher and params["method"] == "user_unverified"
    assert "origin_context: $octx" in cypher
    assert params["octx"].startswith("Taught through /api/thread/learn without the identifier")
    assert "written as user_unverified" in out["unverified"]
    node = _stored(cypher, params)
    assert orchestrator._provenance_tier(node) == 1
    assert identifier_translation._tier(node) == 1


def test_one_example_alone_does_not_confirm_it(graph):
    """REPRODUCTION: the matched value is read through the target side; without
    it that side is checked only on a built value."""
    _learn(example_identifier="D-77")
    _cypher, params = _created(graph)
    assert params["method"] == "user_unverified"


def test_the_thread_panels_request_is_confirmed(graph):
    """Guard: the proposal the panel sends back carries both examples."""
    out = _learn(example_identifier="D-77", example_matched_value="CR-D-77")
    cypher, params = _created(graph)
    assert params["method"] == "user_confirmed" and "origin_context" not in cypher
    assert "octx" not in params and "unverified" not in out
    assert orchestrator._provenance_tier(_stored(cypher, params)) == 0


def test_re_provision_keeps_an_unverified_taught_rule():
    """REPRODUCTION: the onboarding replace deleted every ``ir-`` rule whose
    method is not user_confirmed, a taught ``ir-learned-`` one included."""
    where = onboard._ONBOARDING_RULE_WHERE
    assert "coalesce(ir.method, '') <> 'user_unverified'" in where
    assert "coalesce(ir.method, '') <> 'user_confirmed'" in where
    assert where in onboard._ONBOARDING_RULE_DELETE_CYPHER
    assert where in onboard._PROVISION_LEARNED_SNAPSHOT


def test_the_trace_says_how_an_unverified_rule_came_to_exist():
    """REPRODUCTION: the Knowledge Graph step said only 'recorded in the
    knowledge graph' for a method it did not know."""
    words = route_seed_trace._METHOD_WORDS[resolve.USER_UNVERIFIED_METHOD]
    assert "not checked on real values" in words
