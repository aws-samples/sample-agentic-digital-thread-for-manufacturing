# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ADR audit P12 — the save_value_mapping method-field writer leak.

Audited defect (pilot-env row f8fef055): the MCP tool folded ``[method=...]`` into
the ``query_context`` string instead of the typed ``method`` field — so the
``hierarchy_position`` never-auto-accept check (ADR-0051 P4) could not see it,
and a ``method=None`` call serialized the literal string ``"None"`` into the
stored context.

Pinned here: ``method`` lands ONLY in the typed field; ``query_context`` is the
plain ``reason`` (also passed through the typed ``reason`` evidence field);
``None``/empty method normalizes to typed-empty, never the string ``"None"``.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

import agents.digital_thread_server as dt_server


@pytest.fixture(autouse=True)
def _fresh_burst_state():
    dt_server._reset_value_mapping_burst_state()
    yield
    dt_server._reset_value_mapping_burst_state()


def _call(method, reason="matched by structure walk"):
    with patch.object(dt_server.self_learning, "log_proposed_mapping",
                      return_value="m-1") as spy:
        out = json.loads(dt_server.save_value_mapping(
            from_system="sys-a", from_field="records.external_id",
            from_value="ENTITY-7",
            to_system="sys-b", to_field="entities.canonical_id",
            to_value="entity-7-uuid",
            method=method, confidence="medium", reason=reason,
        ))
    return out, spy.call_args.kwargs


def test_method_lands_in_typed_field_not_context():
    out, kwargs = _call("hierarchy_position")
    assert out["status"] == "logged"
    assert kwargs["method"] == "hierarchy_position"
    assert kwargs["query_context"] == "matched by structure walk"
    assert "[method=" not in kwargs["query_context"]
    assert "hierarchy_position" not in kwargs["query_context"]
    # reason rides the typed evidence field too (per _build_item's contract).
    assert kwargs["reason"] == "matched by structure walk"


def test_method_none_never_serializes_into_context():
    # The pilot-env f8fef055 defect: method=None must be typed-empty — the string
    # "None" must appear NOWHERE in the stored evidence.
    out, kwargs = _call(None)
    assert out["status"] == "logged"
    assert kwargs["method"] == ""
    assert "None" not in kwargs["query_context"]
    assert "[method=" not in kwargs["query_context"]


def test_empty_reason_yields_empty_context():
    _out, kwargs = _call("value_format_match", reason="")
    assert kwargs["query_context"] == ""
    assert kwargs["method"] == "value_format_match"


# --------------------------------------------------------------------------- #
# A STAGED first observation is never reported as "dropped".
# --------------------------------------------------------------------------- #
def _call_with_outcome(mapping_id, outcome):
    with patch.object(dt_server.self_learning, "log_proposed_mapping",
                      return_value=mapping_id), \
         patch.object(dt_server.self_learning, "last_propose_outcome",
                      return_value=outcome):
        return json.loads(dt_server.save_value_mapping(
            from_system="sys-a", from_field="records.external_id",
            from_value="ENTITY-7",
            to_system="sys-b", to_field="entities.canonical_id",
            to_value="entity-7-uuid",
            method="semantic_similarity", confidence="medium",
            reason="cross-referenced by name",
        ))


def test_staged_first_observation_reports_staged_not_dropped():
    """Reporting 'dropped' for a staged observation invited LLM
    reword-retries that defeat the ≥2-independent-observations bar
    (ADR-0051 P5). The LLM-facing text is pinned VERBATIM."""
    out = _call_with_outcome(None, "staged")
    assert out["status"] == "staged"
    assert out["reason"] == "recorded; needs a second independent observation"


def test_gate_dropped_proposal_still_reports_dropped():
    out = _call_with_outcome(None, "")
    assert out["status"] == "dropped"
    assert out["reason"] == "not stored — filtered by learning gates"
