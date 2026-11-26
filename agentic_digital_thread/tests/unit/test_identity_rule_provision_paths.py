# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Which provision paths stamp a rule as approved at the Identity Rules checkpoint.

The chat builds its provision request on the server from its own session's
rules (the ones its Identity Rules checkpoint showed) and says so; the wizard
sends ``rules_from_checkpoint`` (``buildProvisionBody``). A request to
``POST /provision`` that does not say so (a script, any API caller) has its
rules written with the method ``provision-request``, never stamped
``onboarding-llm`` "approved by the user at the Identity Rules checkpoint".
Every rule MERGE states its origin. Offline, on the fixture of
test_chat_provision_is_the_wizard_provision.py; fixture names are generic.
"""
from __future__ import annotations

import agents.onboard_agent as oa
from tests.unit.test_category_rule_write_paths import _direct_provision
from tests.unit.test_chat_provision_is_the_wizard_provision import (  # noqa: F401 - fixture
    SID, _agent_tools, _wait_for, world)
from tests.unit.test_chat_resolve_and_provision_spell_a_field_once import (
    TARGET, _chat_until_resolved_with_a_rule)


def _rule_merges(world) -> list:
    return [(c, p) for c, p in world.writes if c.startswith("MERGE (ir:IdentityRule")]


def test_the_chats_rules_are_stamped_from_the_checkpoint(world):
    """REPRODUCTION: the chat's rule is left for the onboarding stamp (no
    method in the MERGE) and its origin names the checkpoint. 0657e3dc set no
    origin_context on such a rule at all."""
    _chat_until_resolved_with_a_rule(world)
    started = _agent_tools().provision_system(SID)
    assert started.get("status") == "provisioning", started
    _wait_for(lambda: world.store.get_session(SID)["status"] == "onboarded")
    ((cypher, params),) = _rule_merges(world)
    assert "ir.method" not in cypher and "ir.origin_context = $octx" in cypher
    assert params["octx"].startswith("Proposed by onboarding schema analysis (LLM); "
                                     "approved by the user at the Identity Rules checkpoint; ")
    # Nothing could measure the MCP tag's pair, so the origin also says so.
    assert "not re-checked at provision" in params["octx"]


def test_a_direct_request_that_does_not_say_so_is_labelled(world):
    """REPRODUCTION: the same rule sent straight to POST /provision without
    ``rules_from_checkpoint`` is written with the method provision-request
    and an origin that says it was not reviewed at the checkpoint."""
    world.catalog.add({"id": TARGET, "name": "Plant MES", "status": "onboarded",
                       "plant": world.catalog.get(SID)["plant"], "system_type": "MES",
                       "connection_type": "REST API", "schema": ""})
    assert "error" not in oa.discover(SID)
    assert "error" not in oa.map_fields(SID)
    rows = world.store.get_session(SID)["mappings"]
    down = next(r["field"] for r in rows if r["field"].endswith("Down"))
    rule = {"concept": "downtime", "source_field": down, "source_table": down.split(".")[0],
            "source_pattern": "", "target_system": TARGET, "target_field": "stops.stop_code",
            "target_table": "stops", "target_pattern": "", "confidence": 0.8,
            "reasoning": "same stop", "method": "user_confirmed"}
    done = _direct_provision(world, rows, [rule])
    assert done["status"] == "done", done
    ((cypher, params),) = _rule_merges(world)
    assert "ir.method = $method" in cypher and params["method"] == "provision-request"
    assert params["octx"].startswith("Sent in a provision request that did not come from the "
                                     "Identity Rules checkpoint")
