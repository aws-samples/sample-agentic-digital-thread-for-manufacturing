# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Production-shape regressions: gateway-qualified tool names in the runtime process.

Deterministic metadata-only replay of live smoke run
``e00048e38cc848f2a2f41a50cf658784`` (2026-07-17). The live
AgentCore Gateway emits TARGET-QUALIFIED tool names (``lake-athena___query_lake_athena``)
and streams one ``current_tool_use`` input-delta fragment per token (253 fragments
over 7 calls in capture ``session-6c7f3b5929f9485fb684ccf2878a``). The orchestrator
runs in the AgentCore RUNTIME process, where the ``catalog`` singleton is NEVER
hydrated (only ``server.py`` hydrates it) — known systems there come from the
TTL-cached graph read behind ``self_learning.known_systems()``.

Three live failure modes are pinned here, one variable each:

1. ``_registered_tool_system_ids`` built its mapping from the EMPTY catalog
   singleton, so every gateway-prefixed system tool result was rejected
   ``SYSTEM_TARGET_MISMATCH`` before parsing and every Mode A prompt failed
   closed (``NO_CURRENT_INVOCATION_EVIDENCE`` etc., 284–420 s retry latency).
2. Each input-delta fragment burned one unit of the bounded public-progress
   budget (identical redacted payloads), so a normal multi-call turn tripped
   ``TRAVERSAL_BUDGET_EXCEEDED`` which clears ALL claims.
3. The integrity runtime never emitted ``intelligence_summary``, so the smoke
   grounding gate always read ``system_queries == 0``.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from agents import runtime_app, self_learning
from agents.response_integrity import (
    AnswerClaim, ClaimInput, ClaimKind, FinalAnswerDraft, ReasonCode,
    SystemAnswerDraft, capture_trusted_tool_result, system_answer_from_result,
)

# Live inventory shape: gateway target names are the system ids with ``_``→``-``
# (metadata only — no dataset literals beyond registered system ids).
LIVE_TOOLS = {
    "digital-thread___explore_graph": "digital-thread",
    "lake-athena___query_lake_athena": "lake_athena",
    "telem-sitewise___query_telem_sitewise": "telem_sitewise",
}

BASE_SCHEMA = {
    "readings": {
        "relation_kind": "base_table",
        "columns": {"measure": "numeric", "state": "varchar"},
    },
}


def _graph_known_systems(monkeypatch) -> None:
    """Runtime-process shape: catalog singleton empty; systems live in the graph."""
    from agents.catalog import catalog

    monkeypatch.setattr(catalog, "get_all", lambda: [])
    monkeypatch.setattr(
        self_learning, "_known_systems_graph_read",
        lambda: [
            {"id": "lake_athena", "name": "Lake Athena"},
            {"id": "telem_sitewise", "name": "Telem SiteWise"},
        ],
    )
    self_learning._reset_known_systems_cache()


def _validated_system_answer(system_id: str):
    """A SystemAnswer minted through the trusted capture boundary (typed envelope)."""
    state: dict = {}
    query = "SELECT measure FROM readings"
    capture_trusted_tool_result(
        {"columns": ["measure"], "rows": [[7]], "row_count": 1,
         "_adt_executed_query": query},
        state, system_id=system_id, boundary="sql", tool_name="dynamic_read",
        executed_query=query, physical_schema=BASE_SCHEMA,
    )
    result = SimpleNamespace(structured_output=SystemAnswerDraft(claims=(AnswerClaim(
        claim_id="v", label="measure", kind=ClaimKind.VALUE,
        inputs=(ClaimInput(label="measure", value=7, semantic_field="measure", locator="row:0"),),
    ),)))
    answer = system_answer_from_result(result, state)
    assert answer is not None and answer.claims
    return answer


def test_gateway_qualified_mapping_works_with_empty_catalog_singleton(monkeypatch):
    """Mapping must bind live gateway names in the runtime process (empty catalog)."""
    _graph_known_systems(monkeypatch)
    mapping = runtime_app._registered_tool_system_ids(set(LIVE_TOOLS))
    assert mapping == LIVE_TOOLS


def test_live_stream_replay_accepts_system_answer_and_reports_system_queries(monkeypatch):
    """Replay the live event shape end-to-end at the runtime integrity seam."""
    _graph_known_systems(monkeypatch)
    registered = set(LIVE_TOOLS)
    mapping = runtime_app._registered_tool_system_ids(registered)
    invocation = runtime_app._RuntimeIntegrityInvocation(
        "historical value", registered, mapping,
    )

    # 253 input-delta fragments over 3 calls (live capture magnitude): the
    # duplicates carry identical redacted public payloads and must not burn
    # the bounded progress budget — exactly ONE public progress event per call.
    fragment_counts = {
        "digital-thread___explore_graph": 53,
        "lake-athena___query_lake_athena": 100,
        "telem-sitewise___query_telem_sitewise": 100,
    }
    progress_events = 0
    for index, (name, fragments) in enumerate(fragment_counts.items()):
        tool_id = f"tooluse_live_{index}"
        for step in range(fragments):
            progress_events += len(invocation.consume({"current_tool_use": {
                "toolUseId": tool_id, "name": name, "input": "{" * (step + 1),
            }}))
    assert progress_events == len(fragment_counts)
    assert ReasonCode.TRAVERSAL_BUDGET_EXCEEDED not in invocation.context.capture_reason_codes

    # The system agent's typed SystemAnswer envelope arrives as gateway
    # ``toolResult`` text content under the QUALIFIED tool name.
    answer = _validated_system_answer("lake_athena")
    events = invocation.consume({"message": {"content": [{"toolResult": {
        "toolUseId": "tooluse_live_1", "status": "success",
        "content": [{"text": answer.model_dump_json()}],
    }}]}})
    assert events, "tool result must produce a public metadata event"
    assert ReasonCode.SYSTEM_TARGET_MISMATCH not in invocation.context.capture_reason_codes
    assert len(invocation.context.system_answers) == 1
    assert invocation.context.system_answers[0].evidence[0].system_id == "lake_athena"

    # Final gate: a claim citing the captured evidence must be ACCEPTED and the
    # runtime must report grounded system_queries for the smoke gate.
    bound = invocation.context.system_answers[0].claims[0].inputs[0]
    invocation.agent_result = SimpleNamespace(structured_output=FinalAnswerDraft(
        claims=(AnswerClaim(
            claim_id="f", label="measure", kind=ClaimKind.VALUE,
            inputs=(ClaimInput(
                label="measure", value=bound.value, semantic_field=bound.semantic_field,
                locator=bound.locator, evidence_id=bound.evidence_id,
            ),),
        ),),
    ))
    monkeypatch.setattr(
        "agents.runtime_integrity.persist_typed_proposals", lambda *a, **k: 0,
    )
    final_events = invocation.final_events()
    integrity = next(e["response_integrity"] for e in final_events if "response_integrity" in e)
    assert integrity["verdict"] == "accepted", integrity
    summary = next(e["intelligence_summary"] for e in final_events if "intelligence_summary" in e)
    assert summary["total_tool_calls"] == 3
    assert summary["neptune_queries"] == 1
    assert summary["system_queries"] == 2


def test_unmapped_gateway_tool_still_fails_closed(monkeypatch):
    """Contract intact: an unknown qualified target is still rejected, never trusted."""
    _graph_known_systems(monkeypatch)
    registered = set(LIVE_TOOLS) | {"rogue-target___query_rogue_target"}
    mapping = runtime_app._registered_tool_system_ids(registered)
    assert "rogue-target___query_rogue_target" not in mapping
    invocation = runtime_app._RuntimeIntegrityInvocation("historical value", registered, mapping)
    invocation.consume({"current_tool_use": {
        "toolUseId": "tooluse_rogue", "name": "rogue-target___query_rogue_target", "input": "",
    }})
    answer = _validated_system_answer("lake_athena")
    invocation.consume({"message": {"content": [{"toolResult": {
        "toolUseId": "tooluse_rogue", "status": "success",
        "content": [{"text": answer.model_dump_json()}],
    }}]}})
    assert ReasonCode.SYSTEM_TARGET_MISMATCH in invocation.context.capture_reason_codes
    assert invocation.context.system_answers == []
