# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline route tests for the ADVISORY degeneracy verdict on
``GET /api/proposed-mappings`` (Task 2.1, Design decision 5).

The verdict is server-computed in the enrichment loop by
``proposal_scoring.degeneracy_flags`` and attached ADDITIVELY:
``degeneracy: [flags...]`` + ``likely_low_value: True`` ONLY when the flag
list is non-empty — BOTH keys absent otherwise (the ``evidence_classes``
idiom). Applies to every status. Best-effort: a raising classifier leaves the
route serving rows without the keys. Advisory only: no route acts on the keys
and ``_maybe_auto_accept`` never reads them.

No AWS, no Neptune, no network — the route's mapping/concept/schema reads are
monkeypatched (harness idiom of ``test_proposed_mappings_schema_enrichment``).
Placeholder system ids only — no demo literals (ADR-0044).
"""
from __future__ import annotations

import asyncio
import inspect

import agents.schema_check as schema_check


# --------------------------------------------------------------------------- #
# Harness — same idiom as test_proposed_mappings_schema_enrichment.py
# --------------------------------------------------------------------------- #
def _run_get_proposed_mappings(monkeypatch, mappings, schema_for=None, known=None):
    """Drive get_proposed_mappings_route offline: stub the mapping/concept reads, the
    per-system schema loader, and the catalog read."""
    import agents.self_learning as self_learning
    import agents.concept_layer as concept_layer

    schema_for = schema_for or {}
    monkeypatch.setattr(self_learning, "get_all_mappings", lambda: mappings)
    monkeypatch.setattr(self_learning, "known_systems", lambda: list(known or []))
    monkeypatch.setattr(concept_layer, "concept_descriptions", lambda _ids: {})
    monkeypatch.setattr(concept_layer, "concept_layer_of", lambda _c: "field")
    monkeypatch.setattr(schema_check, "load_system_schema",
                        lambda sid: schema_for.get(sid))

    from agents.server import get_proposed_mappings_route
    return asyncio.run(get_proposed_mappings_route())


def _echo_row(**overrides):
    """A pending SAME-system, different-field, equal-trimmed-value
    value_mapping — the echo shape. Placeholder ids only."""
    row = {
        "mapping_id": "map-0001",
        "status": "pending",
        "proposed_type": "value_mapping",
        "from_system": "sysA",
        "from_field": "table_one.col_a",
        "from_value": " VAL7 ",
        "to_system": "sysA",
        "to_field": "table_two.col_b",
        "to_value": "VAL7",
        "concept": "concept-x",
    }
    row.update(overrides)
    return row


def _clean_cross_system_row(**overrides):
    """A clean CROSS-system value_mapping with a real transform."""
    row = {
        "mapping_id": "map-0002",
        "status": "pending",
        "proposed_type": "value_mapping",
        "from_system": "sysA",
        "from_field": "table_one.col_a",
        "from_value": "VAL7",
        "to_system": "sysB",
        "to_field": "table_three.col_c",
        "to_value": "Value Seven",
        "concept": "concept-x",
    }
    row.update(overrides)
    return row


# --------------------------------------------------------------------------- #
# Pin 1 — pending echo-shaped row gets the verdict
# --------------------------------------------------------------------------- #
def test_pending_echo_row_gets_degeneracy_and_low_value(monkeypatch):
    out = _run_get_proposed_mappings(monkeypatch, [_echo_row()])
    assert "error" not in out
    m = out["mappings"][0]
    assert m["degeneracy"] == ["equal_literal_echo"]
    assert m["likely_low_value"] is True


def test_pending_cross_system_echo_row_gets_cross_system_flag(monkeypatch):
    """The LIVE degenerate shape: a CROSS-system pair
    with differing column tokens and equal trimmed values gets the advisory
    ``cross_system_equal_literal`` flag + ``likely_low_value``."""
    row = _echo_row(
        mapping_id="map-0005",
        to_system="sysB",
        from_field="table_one.col_a",
        to_field="table_two.col_b",
        from_value="VAL7",
        to_value="VAL7",
    )
    out = _run_get_proposed_mappings(monkeypatch, [row])
    assert "error" not in out
    m = out["mappings"][0]
    assert m["degeneracy"] == ["cross_system_equal_literal"]
    assert m["likely_low_value"] is True


def test_verdict_applies_to_every_status(monkeypatch):
    """Accepted rows keep their history honest — the badge is not
    pending-only (legacy echo rows in any status get flagged)."""
    out = _run_get_proposed_mappings(
        monkeypatch,
        [_echo_row(mapping_id="map-0003", status="accepted")],
    )
    m = out["mappings"][0]
    assert m["degeneracy"] == ["equal_literal_echo"]
    assert m["likely_low_value"] is True


# --------------------------------------------------------------------------- #
# Pin 2 — clean cross-system row has NEITHER key (absent, not null/false)
# --------------------------------------------------------------------------- #
def test_clean_cross_system_row_has_neither_key(monkeypatch):
    out = _run_get_proposed_mappings(monkeypatch, [_clean_cross_system_row()])
    m = out["mappings"][0]
    assert "degeneracy" not in m
    assert "likely_low_value" not in m


# --------------------------------------------------------------------------- #
# Pin 3 — insight row has NEITHER key
# --------------------------------------------------------------------------- #
def test_insight_row_has_neither_key(monkeypatch):
    insight = {
        "mapping_id": "map-0004",
        "status": "pending",
        "proposed_type": "insight",
        "from_system": "sysA",
        "description": "a narrative finding",
        "concept": "concept-x",
    }
    out = _run_get_proposed_mappings(monkeypatch, [insight])
    m = out["mappings"][0]
    assert "degeneracy" not in m
    assert "likely_low_value" not in m


# --------------------------------------------------------------------------- #
# Pin 4 — best-effort: a raising classifier never breaks the route
# --------------------------------------------------------------------------- #
def test_raising_classifier_serves_rows_without_keys(monkeypatch):
    import agents.proposal_scoring as proposal_scoring

    def _boom(_m):
        raise RuntimeError("classifier down")

    monkeypatch.setattr(proposal_scoring, "degeneracy_flags", _boom)
    out = _run_get_proposed_mappings(
        monkeypatch, [_echo_row(), _clean_cross_system_row()])
    assert "error" not in out
    assert out["count"] == 2
    for m in out["mappings"]:
        assert "degeneracy" not in m
        assert "likely_low_value" not in m
    # The rest of the enrichment still landed (the route kept serving).
    assert all("consequence" in m for m in out["mappings"])


# --------------------------------------------------------------------------- #
# Pin 5 — advisory only: no behavioral coupling anywhere
# --------------------------------------------------------------------------- #
def test_auto_accept_never_reads_the_keys():
    """``_maybe_auto_accept``'s deny logic is untouched — greppable pin that
    its source never references the advisory keys."""
    import agents.self_learning as self_learning

    src = inspect.getsource(self_learning._maybe_auto_accept)
    assert "degeneracy" not in src
    assert "likely_low_value" not in src


def test_steward_action_routes_never_read_the_keys():
    """accept/reject/requeue handlers are unchanged — no route acts on the
    advisory verdict."""
    import agents.server as server

    for handler in (server.accept_proposed_mapping,
                    server.reject_proposed_mapping,
                    server.requeue_proposed_mapping):
        src = inspect.getsource(handler)
        assert "degeneracy" not in src
        assert "likely_low_value" not in src
