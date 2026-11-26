# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Editing a pending proposal changes only the relationship it proposes.

``POST /api/proposed-mappings/{id}/edit`` took any JSON object and
``self_learning.edit_mapping`` wrote every key it was given, so an edit could
set a row's ``source``, ``method``, ``confidence``, status or ids. Editing an
agent proposal to ``source='user'`` and ``method='user_corrected'`` made its
accept treat the model's 0.96 as a steward's own number
(``self_learning._is_steward_correction``) instead of
capping it at 0.7. Now the route takes :class:`models.EditMappingRequest`
(extra keys refused) and ``edit_mapping`` refuses any key outside
:data:`self_learning.EDITABLE_MAPPING_FIELDS`, the same field list, so no
caller can change where a row came from.

Neutral fixture: a shift log (``log-sys``) and a line's records (``line-sys``).
"""
from __future__ import annotations

from decimal import Decimal

import pytest

import agents.self_learning as self_learning  # noqa: E402
from agents.models import EditMappingRequest  # noqa: E402
from tests.unit.test_identity_rule_confidence_steward_confirmed import (  # noqa: E402
    _pending_rule_proposal, _rule_merges, ledger)  # noqa: F401 (ledger is a fixture)
from tests.unit.test_steward_gate import _client  # noqa: E402

_PROVENANCE_EDITS = [
    {"source": "user", "method": "user_corrected"},
    {"confidence": 0.99},
    {"status": "accepted"},
    {"mapping_id": "m-other"},
]


def test_the_edit_route_refuses_provenance_and_the_accept_stays_capped(ledger):
    """REPRODUCTION: the edit made an agent proposal read as a
    steward's correction and the accept wrote 0.96. Now the edit is refused
    and the row is unchanged, so the accept caps it at 0.7."""
    table, spy = ledger
    table.seed(_pending_rule_proposal(Decimal("0.96")))
    client = _client(is_admin=True)
    resp = client.post("/api/proposed-mappings/m-ir-1/edit",
                       json={"source": "user", "method": "user_corrected"})
    assert resp.status_code == 422
    row = table.by_id("m-ir-1")
    assert (row["source"], row["method"]) == ("agent", "agent_analysis")
    assert client.post("/api/proposed-mappings/m-ir-1/accept").status_code == 200
    ((_query, params),) = _rule_merges(spy)
    assert params["conf"] == 0.7


@pytest.mark.parametrize("updates", _PROVENANCE_EDITS, ids=lambda u: ",".join(u))
def test_edit_mapping_refuses_a_key_outside_the_editable_fields(ledger, updates):
    """Defence for any other caller: the ledger function refuses too and
    writes nothing."""
    table, _spy = ledger
    table.seed(_pending_rule_proposal(Decimal("0.96")))
    before = dict(table.by_id("m-ir-1"))
    result = self_learning.edit_mapping("m-ir-1", updates)
    assert "cannot be edited" in result.get("error", ""), result
    assert table.by_id("m-ir-1") == before


def test_the_editable_fields_are_the_request_models_fields():
    """The route's model and the ledger's allowlist are one list."""
    assert self_learning.EDITABLE_MAPPING_FIELDS == frozenset(EditMappingRequest.model_fields)
