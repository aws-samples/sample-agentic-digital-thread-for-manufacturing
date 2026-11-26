# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The route seed after module 6 with the workshop's real HighByte entry.

The workshop-shaped fixture (``_route_seed_workshop_fixture``) uses a generic
MCP hub. Here the module 6 system set gains the entry the workshop package
actually registers when it ships the hub (``workshop.registered_systems(...,
highbyte_shipped=True)``): HighByte's own guidance (current values) and the
SiteWise guidance rewritten for a deployment with a second live sensor source
(trend and history). Both must reach the orchestrator whole, or routing loses
the line that keeps module 5 and 6's trend questions on SiteWise. Built with
and without an exact join between the hub's machine names and the SiteWise
asset names, which the identity pass writes only when the values match.
"""
from __future__ import annotations

import pytest

from agents import orchestrator as orch
from agents.demo import workshop
from agents.demo.systems import DEMO_SYSTEMS
from tests.unit import _route_seed_workshop_fixture as wf

HB = workshop.HIGHBYTE_SYSTEM["id"]
MANIFEST_HB = {s["id"]: dict(s) for s in
               [*DEMO_SYSTEMS, *workshop.registered_systems("live", highbyte_shipped=True)]}
MODULE_6_HB = (*wf.MODULE_6, HB)


@pytest.fixture(params=[False, True], ids=["no exact join", "one exact join"])
def highbyte_manifest(request, monkeypatch):
    """The fixture's graph built from the shipped-HighByte manifest. With the
    join, the fixture's hub pair (machine name -> sensor asset name) is written
    for the real HighByte id."""
    monkeypatch.setattr(wf, "MANIFEST", MANIFEST_HB)
    monkeypatch.setattr(wf, "HUB_ID", HB if request.param else "no-such-system")
    return request.param


def _systems(seed: str) -> dict:
    payload, _tail = wf.decode(seed)
    return {s["id"]: s for s in payload["systems"]}


def test_the_manifest_is_the_shipped_one():
    sensor = MANIFEST_HB[wf.SENSOR_ID]["query_guidance"]
    assert workshop._SITEWISE_WITH_HIGHBYTE_LEAD in sensor
    assert workshop._SITEWISE_ONLY_CLAIM not in sensor
    assert MANIFEST_HB[HB]["query_guidance"] == workshop.HIGHBYTE_SYSTEM["query_guidance"]


def test_highbyte_and_sitewise_guidance_survive_whole_after_module_6(highbyte_manifest):
    seed = wf.build_seed(MODULE_6_HB)
    systems = _systems(seed)

    assert len(seed) <= orch._ROUTE_SEED_SERIALIZED_BUDGET
    assert set(systems) == set(MODULE_6_HB)
    assert workshop.HIGHBYTE_SYSTEM["query_guidance"] in systems[HB]["query_guidance"]
    sensor = systems[wf.SENSOR_ID]["query_guidance"]
    assert MANIFEST_HB[wf.SENSOR_ID]["query_guidance"] in sensor
    # The module 6 note is a query hint: the SiteWise agent gets it with each
    # question, and the seed carries its count instead of its text.
    assert wf.SENSOR_NOTE not in seed
    assert systems[wf.SENSOR_ID]["query_hints"].startswith("1 query hint is delivered")
    for sid in wf.registration_ids(MODULE_6_HB):
        assert wf.base_guidance(sid) in systems[sid]["query_guidance"], sid


def test_an_identifier_question_keeps_both_whole(monkeypatch):
    """Without an exact hub join. With one, an identifier question's owners
    block takes the room first and the shrink pass cuts the plant MES and
    SiteWise bases: the same happens on the fixture's generic hub state
    (``module 6 + hub``), so it is the fit order the budget change chose
    (owners room before prose), not something HighByte's text adds."""
    monkeypatch.setattr(wf, "MANIFEST", MANIFEST_HB)
    monkeypatch.setattr(wf, "HUB_ID", "no-such-system")
    seed = wf.build_seed(MODULE_6_HB, wf.IDENTIFIER_QUESTION)
    systems = _systems(seed)

    assert len(seed) <= orch._ROUTE_SEED_SERIALIZED_BUDGET
    assert workshop.HIGHBYTE_SYSTEM["query_guidance"] in systems[HB]["query_guidance"]
    assert MANIFEST_HB[wf.SENSOR_ID]["query_guidance"] in systems[wf.SENSOR_ID]["query_guidance"]
