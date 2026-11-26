# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Asset coverage rides on top of the fitted route seed, after every reserve.

The seed is fitted with room kept for the identifier tail (owners block,
translations, wildcard note) and, as a last resort, unrelated tier-1 rules
yield to a translation. Coverage is an additive allowance: it is taken out
before any of that runs and put back last. So, in every workshop state and
for a question with or without an identifier, the seed with its coverage keys
removed is byte-identical to the seed built with no coverage at all, and the
tail is the same text.
"""
from __future__ import annotations

import json
import logging

import pytest

from agents import asset_coverage as ac
from agents import identifier_translation as it
from agents import orchestrator as orch
from tests.unit import _route_seed_workshop_fixture as wf
from tests.unit import test_route_seed_final_budget as fb

#: Four generic sites shaped like the workshop SiteWise tree (1 area, 3 lines
#: each; 134 assets), as the role pass stores it on the System node.
_COVERAGE = json.dumps({"anchor": "site", "sites": [
    {"name": "Site Alpha1", "total": 40, "levels": {"area": 1, "line": 3, "equipment": 36}},
    {"name": "Site Br", "total": 25, "levels": {"area": 1, "line": 3, "equipment": 21}},
    {"name": "Site Charlie", "total": 16, "levels": {"area": 1, "line": 3, "equipment": 12}},
    {"name": "Site Delt", "total": 49, "levels": {"area": 1, "line": 3, "equipment": 45}}]},
    separators=(",", ":"))


def _with_coverage(query, system_id: str):
    """``query`` with ``system_id``'s inventory row carrying the coverage."""
    def covered(cypher, *args, **kwargs):
        rows = query(cypher, *args, **kwargs)
        if cypher == orch._ROUTE_SEED_INVENTORY_CYPHER:
            rows = [dict(row, asset_coverage=_COVERAGE) if row["system_id"] == system_id
                    else row for row in rows]
        return rows
    return covered


def _split(seed: str) -> "tuple[str, dict, str, str]":
    """(header, payload, encoded payload, tail) of a seed."""
    head, body = seed.split("RESULT:\n", 1)
    payload, end = json.JSONDecoder().raw_decode(body)
    return head + "RESULT:\n", payload, body[:end], body[end:]


def _strip(payload: dict) -> str:
    """``payload`` without any coverage key, encoded as the seed encodes."""
    value = json.loads(json.dumps(payload))
    value.pop("asset_coverage_omitted", None)
    for system in value.get("systems") or []:
        system.pop("asset_coverage", None)
    return json.dumps(value, default=str, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def _assert_coverage_is_additive(plain: str, covered: str, system_id: str):
    p_head, _p_payload, p_body, p_tail = _split(plain)
    c_head, c_payload, c_body, c_tail = _split(covered)
    assert c_head == p_head
    assert _strip(c_payload) == p_body
    assert c_tail == p_tail
    systems = {s["id"]: s for s in c_payload["systems"]}
    assert systems[system_id]["asset_coverage"] == ac.render_coverage(_COVERAGE)
    assert len(plain) <= orch._ROUTE_SEED_SERIALIZED_BUDGET
    added = len(covered) - len(plain)
    assert 0 < added <= orch._ROUTE_SEED_ASSET_COVERAGE_TOTAL_CAP + len(',"asset_coverage":""')


@pytest.mark.parametrize("question", [None, wf.IDENTIFIER_QUESTION])
@pytest.mark.parametrize("state", sorted(wf.STATES))
def test_coverage_leaves_every_workshop_fit_and_tail_unchanged(state, question):
    ids = wf.STATES[state]
    plain = orch.build_route_seed(query=wf.graph(ids), question=question)
    covered = orch.build_route_seed(
        query=_with_coverage(wf.graph(ids), wf.SENSOR_ID), question=question)
    _assert_coverage_is_additive(plain, covered, wf.SENSOR_ID)
    if question:  # the owners block the reserve bought is still delivered
        assert "IDENTIFIER OWNERS" in _split(plain)[3]


@pytest.mark.parametrize("state", sorted(wf.STATES))
def test_the_owner_reserve_is_decided_without_coverage(monkeypatch, state):
    ids = wf.STATES[state]
    budgets = []
    real = orch._held_fit

    def spy(payload, budget, **kwargs):
        budgets.append(budget)
        return real(payload, budget, **kwargs)
    monkeypatch.setattr(orch, "_held_fit", spy)
    orch.build_route_seed(query=wf.graph(ids), question=wf.IDENTIFIER_QUESTION)
    plain_budgets, budgets[:] = list(budgets), []
    orch.build_route_seed(query=_with_coverage(wf.graph(ids), wf.SENSOR_ID),
                          question=wf.IDENTIFIER_QUESTION)
    assert budgets == plain_budgets


@pytest.mark.usefixtures("route_seed_pressure_budget")
def test_coverage_on_top_of_a_translation_yield(monkeypatch):
    """The last-resort yield (unrelated tier-1 rules give way to a compact
    translation) runs exactly as without coverage; coverage goes on after.

    Budget pinned to 9,700: the route-seed header grew, so the four-system wall
    at 10,000 now leaves room for the full translation form; 9,700 reproduces
    the compact-via-yield path this test exercises, wall fixture unchanged."""
    monkeypatch.setattr(orch, "_ROUTE_SEED_SERIALIZED_BUDGET", 9_700)
    query = fb._four_system_wall(29, 20)
    plain = orch.build_route_seed(query=query, question=fb.TRANSLATION_ONLY_QUESTION)
    assert it.COMPACT_HEADER in plain  # the yield path ran
    covered = orch.build_route_seed(query=_with_coverage(query, fb._C),
                                    question=fb.TRANSLATION_ONLY_QUESTION)
    _assert_coverage_is_additive(plain, covered, fb._C)


def test_a_seed_that_does_not_match_its_payload_gets_no_coverage(caplog):
    payload = {"systems": [{"id": "a", "_asset_coverage": "Site: 1 line"}]}
    seed = orch._route_seed_header(payload) + '{"not":"this payload"}'
    with caplog.at_level(logging.WARNING, logger=orch.__name__):
        out = orch._with_asset_coverage(payload, seed, "\nTAIL")
    assert out == seed + "\nTAIL"
    assert "asset coverage omitted" in caplog.text
