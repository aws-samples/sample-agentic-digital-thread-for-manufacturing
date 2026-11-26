# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A taught identity rule is checked on the identifiers it was taught from.

The Thread panel offers "Learn this pattern?" when the resolver found an
identifier the person typed by a fuzzy match in another system. Until now the
proposal and ``POST /api/thread/learn`` carried only the patterns, so the
reader-agreement check (``resolve.reader_disagreement``) ran on a value built
from the pattern, which never exposes a case or placeholder-width difference.
The proposal now carries the identifier the person typed and the value it
matched, the panel sends them back, and the route checks them (the typed one
through the rule's source side, the matched one through its target side) as
well as the built value. A request without them is handled as before.

Neutral systems and values; the graph is a fake.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

from agents import resolve  # noqa: E402
from agents.models import LearnRequest  # noqa: E402

SRC_SYS, TGT_SYS = "log-sys", "line-sys"


@pytest.fixture
def graph(monkeypatch):
    writes: list = []
    monkeypatch.setattr(resolve, "neptune_query",
                        lambda q, p=None: [{"s.id": TGT_SYS, "field_name": "code", "role": "identifier"}])
    monkeypatch.setattr(resolve, "get_schema", lambda sid: "")
    monkeypatch.setattr(resolve, "neptune_write", lambda q, p=None: writes.append((q, p)) or True)
    return writes


def _learn(src, tgt, **examples) -> dict:
    return resolve.thread_learn(LearnRequest(
        concept="lot-ref", source_system=SRC_SYS, source_field="code", source_pattern=src,
        target_system=TGT_SYS, target_field="ref", target_pattern=tgt, **examples))


def test_the_proposal_carries_the_typed_identifier_and_the_matched_value(graph):
    """REPRODUCTION: the proposal carried neither, so the panel had nothing
    to send back."""
    proposal = resolve._learn_proposal("lot-ref", "WO-9921", "MFG-WO-9921", SRC_SYS, TGT_SYS, "ref")
    assert (proposal["source_pattern"], proposal["target_pattern"]) == ("WO-{id}", "MFG-WO-{id}")
    assert (proposal["example_identifier"], proposal["example_matched_value"]) == ("WO-9921", "MFG-WO-9921")


def test_a_rule_taught_from_an_identifier_the_readers_read_differently_is_refused(graph):
    """REPRODUCTION (the reviewer's case): LOT-{id} -> B-{id} taught from
    LOT-A-B. The resolver translates it (B-A-B), the route seed does not (its
    core is one word); 3bc90f90 created the rule."""
    out = _learn("LOT-{id}", "B-{id}", example_identifier="LOT-A-B")
    assert "the two identifier readers would read value 'LOT-A-B' differently" in out.get("error", "")
    assert graph == []


def test_a_matched_value_the_readers_read_differently_is_refused(graph):
    """REPRODUCTION: the matched value is read through the target side; a
    lower-case b-12 is translated by the route seed (any case) only."""
    out = _learn("LOT-{id}", "B-{id}", example_identifier="LOT-12", example_matched_value="b-12")
    assert "would read value 'b-12' differently" in out.get("error", "")
    assert graph == []


def test_a_fitting_work_order_is_accepted(graph):
    """Guard: WO-{id} -> MFG-WO-{id} taught from WO-9921 / MFG-WO-9921."""
    out = _learn("WO-{id}", "MFG-WO-{id}", example_identifier="WO-9921",
                 example_matched_value="MFG-WO-9921")
    assert out["status"] == "created" and len(graph) == 3


def test_a_request_without_the_examples_behaves_as_before(graph):
    """Guard (backward compatible): no example fields, the built value only."""
    out = _learn("WO-{id}", "MFG-WO-{id}")
    assert out["status"] == "created"
