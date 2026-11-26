# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The chat's resolve judges a rule on measured values, like the wizard's.

``resolve_identities`` (the onboarding chat's resolve) showed the model five
sampled values per field and then kept every rule its own-source, category,
site and storability gates allowed: it measured nothing. A guess that two
unrelated identifier columns match unchanged was stored on the session and
provisioned. Now the chat runs the wizard's measurement (the same sampler,
measured-empty pairs, the target search and the translation check) and the
wizard's gate pass, one shared function, over the chat model's rules. Its
own prompt is unchanged. Fixture names are generic and differ from the
wizard tests'.
"""
from __future__ import annotations

import copy
import json

import pytest

import agents.onboard_agent as oa
import agents.resolve_evidence as rev
import agents.self_learning as self_learning
from tests.unit.test_chat_provision_is_the_wizard_provision import (  # noqa: F401 - fixture
    SID, world)

PEER = "dock-ledger"
TICKET_TAG = "Ent/Site/Line1/StationA/ticket_code"
VOUCHERS = ["VCH-5501", "VCH-5502", "VCH-5517"]


def _setup(world, *, source_values, target_values, patterns=("{id}", "{id}"), probe=None,
           rule_extra=None):
    plant = world.catalog.get(SID)["plant"]
    world.catalog.add({"id": PEER, "name": "Dock Ledger", "status": "onboarded", "plant": plant,
                       "system_type": "ERP", "connection_type": "PostgreSQL", "schema": "s_dock"})
    rows = [{"sys_id": PEER, "sys_name": "Dock Ledger", "sys_schema": "s_dock",
             "field_name": "voucher_no", "concept": "work-order", "table_name": "receipts",
             "removal_incomplete": False}]
    world.mp.setattr(oa, "neptune_query", lambda cypher, params=None: (
        copy.deepcopy(rows) if "MAPS_TO" in cypher else []))
    world.mp.setattr(self_learning, "_neptune_query", lambda *_a, **_k: [])
    world.discover_tables[0]["columns"].append({"name": TICKET_TAG, "data_type": "str"})
    samples = {(SID, "get_tags"): list(source_values), (PEER, "receipts"): list(target_values)}

    def sampler(**kw):
        field = str(kw.get("field") or "")
        if kw["system_id"] == SID and not field.endswith("ticket_code"):
            return []
        return list(samples.get((kw["system_id"], kw["table"]), []))

    world.mp.setattr(rev, "sample_field_values", sampler)
    calls: list = []
    world.mp.setattr(rev, "probe_field_values",
                     lambda **kw: calls.append(kw) or (probe(**kw) if probe else []))

    def model(prompt, *_a, **_k):
        if "Tables and columns:" in prompt:
            block = prompt.split("Tables and columns:\n", 1)[1].split("\n\n", 1)[0]
            out = []
            for line in block.splitlines():
                table, _, cols = line.strip().partition(": ")
                for col in [c for c in cols.split(", ") if c]:
                    concept = ("work-order", "identifier") if col.endswith("ticket_code") else (
                        "sensor-reading", "measure")
                    out.append({"field": f"{table}.{col}", "table": table,
                                "mappings": [{"concept": concept[0], "role": concept[1],
                                              "confidence": 0.9}], "reasoning": "x"})
            return json.dumps(out)
        return json.dumps({"rules": [
            {"concept": "work-order", "source_system": SID, "source_field": f"get_tags.{TICKET_TAG}",
             "source_table": "get_tags", "source_pattern": patterns[0], "target_system": PEER,
             "target_field": "receipts.voucher_no", "target_table": "receipts",
             "target_pattern": patterns[1], "confidence": 0.8, "reasoning": "both identify it",
             **(rule_extra or {})}],
            "value_mappings": []})

    world.mp.setattr(oa, "bedrock_call", model)
    assert "error" not in oa.discover(SID)
    assert "error" not in oa.map_fields(SID)
    return calls


def test_a_chat_guess_on_disjoint_identifier_samples_is_withheld(world):
    calls = _setup(world, source_values=["TK-1", "TK-2", "TK-3"], target_values=VOUCHERS)
    resolved = oa.resolve_identities(SID)
    assert resolved["rules"] == []
    (withheld,) = resolved["rejected_rules"]
    assert withheld["rejected_reason"] == (
        f"measured, nothing shared: none of the 3 values sampled from get_tags.{TICKET_TAG} is "
        f"among the 3 sampled from {PEER} receipts.voucher_no; a search of {PEER} "
        f"receipts.voucher_no for all 3 found none")
    assert len(calls) == 1
    assert oa._store().get_session(SID)["rules"] == []


def test_a_chat_rule_on_shared_values_is_kept(world):
    _setup(world, source_values=VOUCHERS, target_values=VOUCHERS, patterns=("", ""))
    resolved = oa.resolve_identities(SID)
    assert [r["target_field"] for r in resolved["rules"]] == ["receipts.voucher_no"]


def test_a_chat_translation_is_measured_like_the_wizards(world):
    _setup(world, source_values=["TK-5501", "TK-5502", "TK-5517"], target_values=VOUCHERS,
           patterns=("TK-{n}", "VCH-{n}"))
    resolved = oa.resolve_identities(SID)
    assert [r["target_field"] for r in resolved["rules"]] == ["receipts.voucher_no"]


@pytest.mark.parametrize("target_values", [[]], ids=["peer-unsampled"])
def test_a_chat_pair_one_side_could_not_sample_keeps_the_name_pass(world, target_values):
    _setup(world, source_values=["TK-1", "TK-2", "TK-3"], target_values=target_values)
    resolved = oa.resolve_identities(SID)
    assert [r["target_field"] for r in resolved["rules"]] == ["receipts.voucher_no"]
