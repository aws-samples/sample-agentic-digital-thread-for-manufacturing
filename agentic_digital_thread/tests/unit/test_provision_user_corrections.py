# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Re-onboard must not clobber accepted USER concept-mapping corrections (bug 3).

Live incident (2026-07-13, pilot env): a system reset + re-onboard re-ran the LLM
mapper and freshly MERGEd MAPS_TO edges without consulting accepted USER
corrections — the accepted machine-speed correction was silently reverted. The
fix: at provision mapping-write time, load the accepted USER-sourced
``concept_mapping`` rows (``self_learning.accepted_user_concept_mappings``, built
on the existing ``get_accepted_for_system`` seam), SKIP the LLM edge write for any
user-corrected concept, and re-apply the user's mapping through the ONE lifecycle
repoint (``self_learning._repoint_maps_to``). When the repoint fail-louds (the
corrected column is not in the fresh schema) the deferred LLM edge is written as
fallback so the concept stays covered.

Offline — seams mocked per ``test_provision_athena_schema_write`` /
``test_onboard_provision_node_write`` patterns. No AWS, no Neptune, no network.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.onboard as onboard  # noqa: E402
import agents.self_learning as self_learning  # noqa: E402
from agents.models import ProvisionRequest  # noqa: E402


class _FakeAgentManager:
    _agents: dict = {}

    def start_agent(self, **_kwargs):
        return "mcp://test-runtime/invoke"

    def get_mcp_url(self, _system_id):
        return "mcp://test-runtime/invoke"


class _Boom:
    def __getattr__(self, _name):
        raise RuntimeError("AWS disabled in unit tests")


_SID = "example-athena-x"

# The accepted USER correction: cycle-time was corrected to a DIFFERENT column
# than the one the LLM maps below.
_USER_ROW = {
    "mapping_id": "m-1", "discovered_at": "2026-07-13T10:00:00+00:00",
    "proposed_type": "concept_mapping", "source": "user", "status": "accepted",
    "from_system": "", "from_field": "", "from_value": "",
    "to_system": _SID, "to_field": "counter_tbl.act_speed", "to_value": "",
    "concept": "cycle-time", "confidence": "0.95",
}

# LLM map output: cycle-time → a STALE column; availability → uncorrected control.
_MAPPINGS = [
    {"table": "machine_tbl", "field": "machine_tbl.target_speed",
     "concept": "cycle-time", "confidence": 0.7},
    {"table": "machine_tbl", "field": "machine_tbl.uptime_pct",
     "concept": "availability", "confidence": 0.8},
]


def _install(monkeypatch, *, accepted_rows, repoint):
    writes: list[tuple[str, dict]] = []
    monkeypatch.setattr(onboard, "neptune_write", lambda c, p=None: writes.append((c, p or {})))
    monkeypatch.setattr(onboard, "neptune_query", lambda *_a, **_k: [])
    monkeypatch.setattr(onboard, "bedrock_call", lambda *_a, **_k: "stub")
    monkeypatch.setattr(onboard, "pg", lambda *_a, **_k: {"rows": []})
    monkeypatch.setattr(onboard, "validate_identifier", lambda *_a, **_k: None)
    monkeypatch.setattr(onboard, "add_system_agent", lambda *_a, **_k: None)
    monkeypatch.setattr(onboard, "_persist_node_connection", lambda *_a, **_k: None)
    monkeypatch.setattr(onboard, "_agent_manager", _FakeAgentManager())
    monkeypatch.setattr(onboard, "_detect_internal_joinpaths", lambda *_a, **_k: [])
    monkeypatch.setattr("boto3.client", lambda *_a, **_k: _Boom())
    monkeypatch.setattr(self_learning, "get_accepted_for_system",
                        lambda _sid: [dict(r) for r in accepted_rows])
    monkeypatch.setattr(self_learning, "_repoint_maps_to", repoint)
    return writes


def _provision(monkeypatch, *, accepted_rows, repoint):
    writes = _install(monkeypatch, accepted_rows=accepted_rows, repoint=repoint)
    body = ProvisionRequest(
        system_id=_SID, system_name="Example Athena", schema="example_db",
        connection_type="Athena",
        connection_config={"workgroup": "wg", "database": "example_db", "region": "us-east-1"},
        mappings=[dict(m) for m in _MAPPINGS], rules=[],
    )
    result = onboard._do_provision(body)
    return writes, result


def _llm_edge_writes(writes, concept):
    return [
        (c, p) for c, p in writes
        if "MERGE (f)-[r:MAPS_TO]->(c)" in c and p.get("cid") == concept
    ]


class _RepointSpy:
    def __init__(self, *, raises: Exception | None = None):
        self.calls: list[dict] = []
        self._raises = raises

    def __call__(self, item: dict) -> str:
        self.calls.append(dict(item))
        if self._raises is not None:
            raise self._raises
        return f"mapsto-{item['to_system']}-{item['concept']}"


def test_user_correction_skips_llm_edge_and_repoints(monkeypatch):
    spy = _RepointSpy()
    writes, result = _provision(monkeypatch, accepted_rows=[_USER_ROW], repoint=spy)

    # The lifecycle repoint re-applied the USER's mapping.
    assert len(spy.calls) == 1
    item = spy.calls[0]
    assert item["to_system"] == _SID
    assert item["concept"] == "cycle-time"
    assert item["to_field"] == "counter_tbl.act_speed"
    # The LLM's stale cycle-time edge was NOT written.
    assert _llm_edge_writes(writes, "cycle-time") == []
    # Uncorrected concepts keep the normal LLM edge write.
    availability = _llm_edge_writes(writes, "availability")
    assert len(availability) == 1
    assert availability[0][1]["fid"] == f"{_SID}.machine_tbl.uptime_pct"
    # Success ⇒ NO reapply-failed marker in the provision result.
    assert "user_correction_reapply_failed" not in result


def test_repoint_failure_logs_warning_and_falls_back_to_llm_edge(monkeypatch, caplog):
    err = ValueError(
        "corrected column 'counter_tbl.act_speed' is not in the discovered schema "
        f"for system '{_SID}' — correction not applied"
    )
    spy = _RepointSpy(raises=err)
    with caplog.at_level("WARNING"):
        writes, result = _provision(monkeypatch, accepted_rows=[_USER_ROW], repoint=spy)

    assert len(spy.calls) == 1  # the repoint WAS attempted
    # Fallback: the deferred LLM edge is written so the concept stays covered.
    fallback = _llm_edge_writes(writes, "cycle-time")
    assert len(fallback) == 1
    assert fallback[0][1]["fid"] == f"{_SID}.machine_tbl.target_speed"
    # And the operator can see why — including that the fallback was KEPT.
    assert any("not re-applied" in r.message and "cycle-time" in r.message
               for r in caplog.records)
    assert any("fallback" in r.message and "cycle-time" in r.message
               for r in caplog.records)
    # Machine-readable marker with the distinct reason.
    assert result["user_correction_reapply_failed"] == [
        {"concept": "cycle-time", "reason": "column_not_in_schema"}
    ]


def test_repoint_runtimeerror_takes_same_fallback_with_distinct_reason(monkeypatch, caplog):
    """M3: a graph-outage RuntimeError from the repoint (the M2 honesty guard /
    _neptune_write_loud) takes the SAME LLM-fallback path as ValueError, with a
    distinct machine-readable reason in the provision-result marker."""
    spy = _RepointSpy(raises=RuntimeError("graph unavailable — correction not applied"))
    with caplog.at_level("WARNING"):
        writes, result = _provision(monkeypatch, accepted_rows=[_USER_ROW], repoint=spy)

    assert len(spy.calls) == 1
    fallback = _llm_edge_writes(writes, "cycle-time")
    assert len(fallback) == 1
    assert fallback[0][1]["fid"] == f"{_SID}.machine_tbl.target_speed"
    assert any("not re-applied" in r.message and "graph_unavailable" in r.message
               for r in caplog.records)
    assert result["user_correction_reapply_failed"] == [
        {"concept": "cycle-time", "reason": "graph_unavailable"}
    ]


def test_repoint_failure_without_fallback_edges_warns_concept_goes_dark(monkeypatch, caplog):
    """LOW (c): when the LLM produced NO edge for the corrected concept, the
    warning must say the concept goes dark (uncovered) — a materially different
    outcome from the fallback-kept case."""
    row = {**_USER_ROW, "concept": "scrap-rate", "to_field": "counter_tbl.scrap"}
    spy = _RepointSpy(raises=ValueError("corrected column 'counter_tbl.scrap' is not "
                                        f"in the discovered schema for system '{_SID}'"
                                        " — correction not applied"))
    with caplog.at_level("WARNING"):
        writes, result = _provision(monkeypatch, accepted_rows=[row], repoint=spy)

    # No LLM mapping existed for scrap-rate ⇒ nothing deferred ⇒ no fallback edge.
    assert _llm_edge_writes(writes, "scrap-rate") == []
    assert any("goes dark" in r.message and "scrap-rate" in r.message
               for r in caplog.records)
    assert result["user_correction_reapply_failed"] == [
        {"concept": "scrap-rate", "reason": "column_not_in_schema"}
    ]


def test_no_corrections_means_no_repoint_and_normal_edges(monkeypatch):
    spy = _RepointSpy()
    writes, result = _provision(monkeypatch, accepted_rows=[], repoint=spy)

    assert spy.calls == []
    assert len(_llm_edge_writes(writes, "cycle-time")) == 1
    assert len(_llm_edge_writes(writes, "availability")) == 1
    assert "user_correction_reapply_failed" not in result


# --------------------------------------------------------------------------- #
# The helper itself: accepted_user_concept_mappings filtering contract.
# --------------------------------------------------------------------------- #
def test_helper_filters_to_user_concept_mappings_for_system(monkeypatch):
    rows = [
        dict(_USER_ROW),
        # agent-sourced concept_mapping — ignored (user-only override).
        {**_USER_ROW, "mapping_id": "m-2", "source": "agent",
         "concept": "availability", "to_field": "machine_tbl.uptime_pct"},
        # user value_mapping — ignored (wrong proposed_type).
        {**_USER_ROW, "mapping_id": "m-3", "proposed_type": "value_mapping",
         "concept": "asset", "to_field": "machine_tbl.name"},
        # other system — ignored.
        {**_USER_ROW, "mapping_id": "m-4", "to_system": "other-sys",
         "concept": "batch", "to_field": "lots.lot_id"},
        # malformed to_field (no table.column) — skipped.
        {**_USER_ROW, "mapping_id": "m-5", "concept": "cost", "to_field": "plaincolumn"},
    ]
    monkeypatch.setattr(self_learning, "get_accepted_for_system", lambda _sid: rows)

    out = self_learning.accepted_user_concept_mappings(_SID)
    assert out == {"cycle-time": "counter_tbl.act_speed"}


def test_helper_newest_correction_wins(monkeypatch):
    older = {**_USER_ROW, "mapping_id": "m-old",
             "discovered_at": "2026-07-10T00:00:00+00:00",
             "to_field": "counter_tbl.old_speed"}
    newer = {**_USER_ROW, "mapping_id": "m-new",
             "discovered_at": "2026-07-13T00:00:00+00:00",
             "to_field": "counter_tbl.act_speed"}
    monkeypatch.setattr(self_learning, "get_accepted_for_system",
                        lambda _sid: [older, newer])

    out = self_learning.accepted_user_concept_mappings(_SID)
    assert out == {"cycle-time": "counter_tbl.act_speed"}


def test_helper_swallows_backend_failure(monkeypatch):
    def _boom(_sid):
        raise RuntimeError("dynamo down")
    monkeypatch.setattr(self_learning, "get_accepted_for_system", _boom)
    assert self_learning.accepted_user_concept_mappings(_SID) == {}


def test_helper_blank_system_id_returns_empty():
    assert self_learning.accepted_user_concept_mappings("") == {}
