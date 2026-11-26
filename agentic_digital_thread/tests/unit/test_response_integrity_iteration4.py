# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Adversarial regressions for deterministic response integrity."""
from __future__ import annotations

import ast
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError
from pydantic import ValidationError

from agents import graph_tools
from agents.chat_client import AgentCoreChatClient
from agents.events import DoneEvent, ErrorEvent
from agents.response_integrity import (
    AnswerClaim,
    ClaimInput,
    ClaimKind,
    Completeness,
    FinalAnswer,
    FinalAnswerDraft,
    IntegrityContext,
    LearningProposal,
    LearningProposalKind,
    ReasonCode,
    RelationshipProof,
    SystemAnswerDraft,
    capture_orchestrator_tool_result,
    capture_trusted_tool_result,
    system_answer_from_result,
    validate_final_answer,
)
from agents.tool_result_capture import InvocationToolResultCapture

UTC = timezone.utc
NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
SQL_SCHEMA = {
    "readings": {
        "relation_kind": "base_table",
        "columns": {
            "measure": "integer",
            "observed_at": "timestamp",
            "asset_id": "varchar",
            "state": "varchar",
        },
    }
}


def _answer(state: dict, *, value=7, field="measure", locator="row:0"):
    result = SimpleNamespace(structured_output=SystemAnswerDraft(claims=(AnswerClaim(
        claim_id="claim", kind=ClaimKind.VALUE, label="value", value=value,
        inputs=(ClaimInput(label="value", value=value, semantic_field=field, locator=locator),),
    ),)))
    answer = system_answer_from_result(result, state)
    assert answer is not None
    return answer


def _capture_sql(payload: dict, query: str) -> dict:
    state: dict = {}
    capture_trusted_tool_result(
        {"content": [{"json": payload}]}, state,
        system_id="system-a", boundary="athena", tool_name="athena_run_query",
        queried_at=NOW, executed_query=query, physical_schema=SQL_SCHEMA,
    )
    return state


def _hook_answer(payload: dict, query: str):
    state: dict = {}
    hook = InvocationToolResultCapture(
        system_id="system-a", boundary="athena", physical_schema=SQL_SCHEMA,
    )
    hook._on_invocation_start(SimpleNamespace(invocation_state=state))
    event = SimpleNamespace(
        invocation_state=state,
        selected_tool=SimpleNamespace(tool_name="athena_run_query"),
        tool_use={"name": "athena_run_query", "input": {"sql": query}},
        result={"status": "success", "content": [{"json": {
            **payload, "_adt_executed_query": query,
        }}]},
        exception=None,
    )
    hook._on_after_tool_call(event)
    return _answer(state, value=payload["rows"][0][0], field=payload["columns"][0])


@pytest.mark.parametrize("query,payload", [
    ("SELECT 999 AS measure FROM readings", {"columns": ["measure"], "rows": [[999]], "row_count": 1}),
    ("SELECT 'forged' AS asset_id FROM readings", {"columns": ["asset_id"], "rows": [["forged"]], "row_count": 1}),
    ("SELECT measure + 1 AS measure FROM readings", {"columns": ["measure"], "rows": [[8]], "row_count": 1}),
    ("SELECT (SELECT 999) AS measure FROM readings", {"columns": ["measure"], "rows": [[999]], "row_count": 1}),
])
def test_hook_rejects_non_physical_sql_projections(query, payload):
    answer = _hook_answer(payload, query)
    assert answer.claims == ()
    assert ReasonCode.STRUCTURAL_LINEAGE_MISMATCH in answer.reason_codes


def test_hook_rejects_current_timestamp_reserved_alias():
    payload = {
        "columns": ["measure", "__adt_source_time__measure"],
        "rows": [[7, NOW.isoformat()]], "row_count": 1,
    }
    answer = _hook_answer(
        payload,
        "SELECT measure, CURRENT_TIMESTAMP AS __adt_source_time__measure FROM readings",
    )
    assert answer.claims == ()


def test_hook_rejects_duplicate_alias_laundering():
    payload = {
        "columns": ["measure", "__adt_source_time__measure", "__adt_source_time__measure"],
        "rows": [[7, NOW.isoformat(), NOW.isoformat()]], "row_count": 1,
    }
    answer = _hook_answer(
        payload,
        "SELECT measure, observed_at AS __adt_source_time__measure, "
        "CURRENT_TIMESTAMP AS __adt_source_time__measure FROM readings",
    )
    assert answer.claims == ()
    assert ReasonCode.SOURCE_PARTIAL in answer.reason_codes


def test_direct_allowlisted_projection_and_timestamp_lineage_are_accepted():
    payload = {
        "columns": ["measure", "__adt_source_time__measure"],
        "rows": [[7, NOW.isoformat()]], "row_count": 1,
    }
    answer = _hook_answer(
        payload,
        "SELECT r.measure AS measure, r.observed_at AS __adt_source_time__measure "
        "FROM readings AS r",
    )
    assert answer.claims and answer.claims[0].value == 7
    assert answer.evidence[0].source_observed_end == NOW


def test_sql_capture_without_executed_query_fails_closed():
    state: dict = {}
    capture_trusted_tool_result(
        {"columns": ["measure"], "rows": [[7]], "row_count": 1}, state,
        system_id="system-a", boundary="sql", tool_name="query", physical_schema=SQL_SCHEMA,
    )
    answer = _answer(state)
    assert answer.claims == ()
    assert ReasonCode.SOURCE_PARTIAL in answer.reason_codes


def test_join_path_can_never_verify_row_identity(monkeypatch):
    called = False

    def fake_query(*_args, **_kwargs):
        nonlocal called
        called = True
        return [{"graph_record_id": "jp-1"}]

    monkeypatch.setattr(graph_tools, "neptune_query", fake_query)
    result = json.loads(graph_tools.verify_relationship(
        "system-a", "arbitrary-a", "asset_id",
        "system-b", "arbitrary-b", "equipment_id", "join_path",
    ))
    assert result == {
        "verified": False, "records": [], "reason": "relationship_type_not_allowed",
    }
    assert called is False


def test_value_mapping_proof_uses_stored_record_endpoints(monkeypatch):
    stored = {
        "graph_record_id": "vm-1",
        "source_system": "system-a", "source_identifier": "stored-a",
        "source_field": "asset_id", "target_system": "system-b",
        "target_identifier": "stored-b", "target_field": "equipment_id",
        "mapping_semantics": "explicit_pair",
    }
    monkeypatch.setattr(graph_tools, "neptune_query", lambda *_args, **_kwargs: [stored])
    rejected = json.loads(graph_tools.verify_relationship(
        "system-a", "request-a", "asset_id",
        "system-b", "request-b", "equipment_id", "value_mapping",
    ))
    assert rejected["verified"] is False
    assert rejected["reason"] == "stored_endpoint_mismatch"

    accepted = json.loads(graph_tools.verify_relationship(
        "system-a", "stored-a", "asset_id",
        "system-b", "stored-b", "equipment_id", "value_mapping",
    ))
    assert accepted["records"][0]["source_identifier"] == "stored-a"
    assert accepted["records"][0]["target_identifier"] == "stored-b"
    assert accepted["records"][0]["graph_record_id"] == "vm-1"


def test_relationship_proof_schema_excludes_join_path():
    with pytest.raises(ValidationError):
        RelationshipProof(
            graph_record_id="jp-1", source_system="system-a",
            source_identifier="a", source_field="asset_id",
            target_system="system-b", target_identifier="b",
            target_field="equipment_id", relationship_type="join_path",
            direction="outbound",
        )


def _proof() -> RelationshipProof:
    return RelationshipProof(
        graph_record_id="vm-1", source_system="system-a",
        source_identifier="A-1", source_field="asset_id",
        target_system="system-b", target_identifier="B-1",
        target_field="equipment_id", relationship_type="value_mapping",
        direction="outbound",
    )


def _proposal(**updates) -> LearningProposal:
    data = {
        "proposal_id": "p-1", "kind": LearningProposalKind.VALUE_MAPPING,
        "from_system": "system-a", "from_field": "asset_id", "from_value": "A-1",
        "to_system": "system-b", "to_field": "equipment_id", "to_value": "B-1",
        "concept": "asset", "evidence_ids": ("ev-a", "ev-b"),
        "relationship_proof": _proof(), "relationship_type": "value_mapping",
        "direction": "outbound",
    }
    data.update(updates)
    return LearningProposal(**data)


def test_proposal_contract_requires_direction_and_relationship_type():
    data = _proposal().model_dump()
    data.pop("direction")
    with pytest.raises(ValidationError):
        LearningProposal.model_validate(data)
    data = _proposal().model_dump()
    data.pop("relationship_type")
    with pytest.raises(ValidationError):
        LearningProposal.model_validate(data)


def test_field_annotation_has_one_endpoint_or_exact_self_endpoint():
    with pytest.raises(ValidationError):
        LearningProposal(
            proposal_id="annotation", kind="field_annotation",
            from_system="system-a", from_field="readings.state", from_value="",
            to_system="system-b", to_field="other.state", to_value="",
            concept="state", evidence_ids=("ev-a",),
            relationship_type="field_annotation", direction="outbound",
        )


def test_persisted_value_mapping_keeps_safe_raw_proven_endpoints(monkeypatch):
    from agents import runtime_integrity

    calls = []
    monkeypatch.setattr("agents.self_learning.log_proposed_mapping",
                        lambda **kwargs: calls.append(kwargs) or "vm-stub")
    answer = FinalAnswer(
        claims=(), evidence=(), proposals=(_proposal(),), reason_codes=(),
        complete=True, learning_allowed=True, current_intent=False,
    )
    assert runtime_integrity.persist_typed_proposals(answer, tool_calls_count=2) == 1
    assert calls[0]["from_value"] == "A-1"
    assert calls[0]["to_value"] == "B-1"
    assert "sha256:" not in json.dumps(calls)


def test_unproven_or_sensitive_mapping_is_non_actionable(monkeypatch):
    from agents import runtime_integrity

    calls = []
    monkeypatch.setattr("agents.self_learning.log_proposed_mapping",
                        lambda **kwargs: calls.append(kwargs) or "vm-stub")
    secret = "ghp_abcdefghijklmnopqrstuvwxyz1234567890"  # gitleaks:allow — intentional fake token (never-mint/classifier fixture)  # nosec B105 - not a credential: field/env/secret NAME, sentinel, or offline test fixture
    unsafe = _proposal(
        from_value=secret,
        relationship_proof=_proof().model_copy(update={"source_identifier": secret}),
    )
    answer = FinalAnswer(
        claims=(), evidence=(), proposals=(unsafe,), reason_codes=(),
        complete=True, learning_allowed=True, current_intent=False,
    )
    assert runtime_integrity.persist_typed_proposals(answer, tool_calls_count=2) == 0
    assert calls == []


def test_thirty_third_content_fragment_marks_partial_and_disables_learning():
    fragments = [
        {"json": {"items": [{"measure": index}]}}
        for index in range(33)
    ]
    state: dict = {}
    capture_trusted_tool_result(
        {"content": fragments}, state,
        system_id="system-a", boundary="rest", tool_name="read",
    )
    answer = system_answer_from_result(
        SimpleNamespace(structured_output=SystemAnswerDraft()), state,
    )
    assert answer is not None and answer.status == "partial"
    assert ReasonCode.TRAVERSAL_BUDGET_EXCEEDED in answer.reason_codes


@pytest.mark.parametrize("marker", [
    {"has_more": True}, {"hasMore": True}, {"nextPageToken": "next"},
    {"next_page": 2}, {"cursor": "next"}, {"links": {"next": "/next"}},
    {"continuation": "next"},
])
def test_common_pagination_markers_are_partial(marker):
    state: dict = {}
    capture_trusted_tool_result(
        {"items": [{"measure": 7}], **marker}, state,
        system_id="system-a", boundary="rest", tool_name="read",
    )
    answer = system_answer_from_result(
        SimpleNamespace(structured_output=SystemAnswerDraft()), state,
    )
    assert answer is not None and answer.status == "partial"
    assert ReasonCode.SOURCE_PARTIAL in answer.reason_codes


def test_unknown_pagination_and_mixed_shapes_are_partial():
    state: dict = {}
    capture_trusted_tool_result(
        {"items": [{"measure": 7}], "pageInfo": {"opaque": "unknown"}}, state,
        system_id="system-a", boundary="rest", tool_name="read",
    )
    answer = system_answer_from_result(
        SimpleNamespace(structured_output=SystemAnswerDraft()), state,
    )
    assert answer is not None and answer.status == "partial"


@pytest.mark.parametrize("prompt", [
    "Inventory levels", "How many open work orders?", "Show stock remaining",
    "Is this item available?", "Show source system facts",
    "Compare last month with this month",
])
def test_source_fact_intent_defaults_current(prompt):
    assert IntegrityContext(prompt).current_intent is True


@pytest.mark.parametrize("prompt", [
    "What was the inventory level on 2024-01-01?",
    "Show the historical work orders from last year",
])
def test_explicit_historical_only_intent_is_not_current(prompt):
    assert IntegrityContext(prompt).current_intent is False


def test_secret_and_free_text_policy_default_denies_strings():
    payload = {
        "items": [{
            "asset_id": "A-1", "state": "ready", "note": "neutral note",
            "value": "neutral value", "github": "".join(("ghp_", "abcdefghijklmnopqrstuvwxyz1234567890")),  # gitleaks:allow — intentional fake token (never-mint/classifier fixture)
            "stripe": "".join(("sk_", "live_abcdefghijklmnopqrstuvwxyz")), "slack": "".join(("xoxb-", "123-456-secret")),  # gitleaks:allow — intentional fake token (never-mint/classifier fixture)
            "credentials": {"asset_id": "nested-secret"},
        }]
    }
    state: dict = {}
    capture_trusted_tool_result(
        payload, state, system_id="system-a", boundary="rest", tool_name="read",
    )
    private = state["_adt_private_source_evidence"]
    values = {item.canonical_value for item in private.observations}
    assert "A-1" in values and "ready" in values
    assert not ({"neutral note", "neutral value", "nested-secret"} & values)
    assert not any("ghp_" in str(v) or "sk_live_" in str(v) or "xoxb-" in str(v) for v in values)


def test_cloud_setup_client_error_is_safe_and_secret_free(monkeypatch, caplog):
    secret = "ghp_abcdefghijklmnopqrstuvwxyz1234567890"  # gitleaks:allow — intentional fake token (never-mint/classifier fixture)  # nosec B105 - not a credential: field/env/secret NAME, sentinel, or offline test fixture

    def fail_client(*_args, **_kwargs):
        raise ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": secret}},
            "InvokeAgentRuntime",
        )

    client = AgentCoreChatClient(runtime_arn="arn:runtime", region="us-east-1")
    caplog.clear()
    monkeypatch.setattr("agents.chat_client.boto3.client", fail_client)
    events = list(client.stream(
        "private prompt", "session", "user",
    ))
    assert len(events) == 2
    assert isinstance(events[0], ErrorEvent)
    assert isinstance(events[1], DoneEvent)
    # Fixed per-category text from the ONE message table — never the exception.
    from agents.events import public_error_message
    assert events[0].category == "gateway_auth"
    assert events[0].message == public_error_message("gateway_auth")
    assert secret not in events[0].message
    assert events[0].details == ""
    assert secret not in caplog.text and "private prompt" not in caplog.text


def test_orphan_capture_cleanup_scans_directory_mtime(monkeypatch, tmp_path):
    from agents import stream_capture

    monkeypatch.setattr(stream_capture, "_CAPTURE_DIR", tmp_path)
    orphan = tmp_path / "capture-orphan.jsonl"
    orphan.write_text("metadata-only\n", encoding="utf-8")
    old = stream_capture.time.time() - stream_capture._CAPTURE_TTL_SECONDS - 1
    stream_capture.os.utime(orphan, (old, old))
    stream_capture._cleanup_expired()
    assert not orphan.exists()


def test_mode_b_public_stream_quota_is_not_applied(monkeypatch):
    class Stream:
        def iter_lines(self, **_kwargs):
            yield b'data: {"data":"abcdefgh"}'

    class Client:
        def invoke_agent_runtime(self, **_kwargs):
            return {"response": Stream()}

    monkeypatch.setattr("agents.chat_client.boto3.client", lambda *_a, **_k: Client())
    monkeypatch.setattr("agents.runtime_integrity.MAX_PUBLIC_STREAM_BYTES", 1)
    monkeypatch.setenv("SYSTEM_QUERY_MODE", "orchestrator")
    events = list(AgentCoreChatClient(runtime_arn="arn:runtime", region="us-east-1").stream(
        "prompt", "session", "user",
    ))
    assert any(getattr(event, "content", "") == "abcdefgh" for event in events)
    assert not any(type(event).__name__ == "ErrorEvent" for event in events)


def test_mode_a_aggregate_system_answer_budget_fails_closed():
    context = IntegrityContext("Show current values")
    envelope = {"status": "complete", "claims": [], "evidence": [], "reason_codes": []}
    for index in range(65):
        payload = dict(envelope)
        payload["evidence"] = [{
            "evidence_id": f"ev-{index}", "system_id": "system-a", "boundary": "rest",
            "queried_at": NOW.isoformat(), "freshness": "current", "completeness": "complete",
            "semantic_field": "asset_id", "locator": f"item:{index}", "canonical_type": "string",
        }]
        capture_orchestrator_tool_result(
            "system-a___query", {"content": [{"json": payload}]}, context,
        )
    assert ReasonCode.TRAVERSAL_BUDGET_EXCEEDED in context.capture_reason_codes


_POLICY_FILES = (
    "response_integrity_contracts.py", "response_integrity_capture.py",
    "response_integrity_validation.py", "response_integrity_rendering.py",
    "response_integrity_security.py", "response_integrity_sql.py",
    "runtime_integrity.py",
)
_REQUIRED_FUNCTIONS = {
    "graph_tools.py": {"verify_relationship"},
    "chat_client.py": {
        "stream", "_stream_impl", "_parse_event", "_generic_events",
        "_message_events", "_result_events", "_run_local_agent",
    },
    "runtime_app.py": {
        "_invoke_integrity_runtime", "_runtime_entrypoint", "consume",
        "final_events", "_tool_results", "_one_tool_result",
    },
    "athena_tools.py": {
        "run_athena_query", "_run_athena_query_impl", "_poll_athena_query",
    },
    "system_agent.py": {
        "_make_query_tool",
        "_make_sitewise_direct_tool", "_make_sitewise_direct_tool_impl",
        "_make_sitewise_current_tools", "_make_sitewise_history_tool",
        "_make_scoped_athena_run_query",
    },
    "config.py": {"_discover_orchestrator_arn", "_discover_memory_id"},
}


def _function_lines(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    return int(node.end_lineno or node.lineno) - node.lineno + 1


def test_iteration4_maintainability_limits():
    agents = Path(__file__).resolve().parents[2] / "agents"
    failures = []
    for name in _POLICY_FILES:
        path = agents / name
        assert len(path.read_text(encoding="utf-8").splitlines()) <= 800, name
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _function_lines(node) > 50:
                failures.append(f"{name}:{node.name}:{_function_lines(node)}")
    for name, functions in _REQUIRED_FUNCTIONS.items():
        path = agents / name
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found = {function: [] for function in functions}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in found:
                found[node.name].append(_function_lines(node))
        for function, lengths in found.items():
            if not lengths or any(length > 50 for length in lengths):
                failures.append(f"{name}:{function}:{lengths}")
    assert failures == []


def test_numeric_athena_string_uses_physical_numeric_type():
    payload = {"columns": ["measure"], "rows": [["7"]], "row_count": 1}
    answer = _hook_answer(payload, "SELECT measure FROM readings")
    assert answer.claims and answer.claims[0].value == 7
    assert answer.evidence[0].canonical_type == "integer"


def test_one_endpoint_field_annotation_binds_to_field_evidence():
    state = _capture_sql(
        {"columns": ["state"], "rows": [["RUNNING"]], "row_count": 1},
        "SELECT state FROM readings",
    )
    system_answer = _answer(state, value="RUNNING", field="state")
    context = IntegrityContext("Historical state only")
    context.system_answers.append(system_answer)
    proposal = LearningProposal(
        proposal_id="annotation", kind="field_annotation",
        from_system="system-a", from_field="readings.state", from_value="",
        concept="operating state", evidence_ids=(system_answer.evidence[0].evidence_id,),
        relationship_type="field_annotation", direction="self",
    )
    final = validate_final_answer(FinalAnswerDraft(
        claims=(system_answer.claims[0],), proposals=(proposal,),
    ), context)
    assert final.proposals == (proposal,)


def test_tool_metadata_fragment_overflow_is_partial():
    from agents.tool_result_capture import safe_tool_result_metadata

    result = {"status": "success", "content": [
        {"json": {"status": "complete", "row_count": 1}} for _ in range(33)
    ]}
    metadata = safe_tool_result_metadata(result, tool_name="query", max_chars=1024)
    assert metadata["app_status"] == "partial"
    assert metadata["truncated"] is True


def test_config_discovery_error_never_logs_service_secret(monkeypatch, caplog):
    from agents import config

    secret = "ghp_abcdefghijklmnopqrstuvwxyz1234567890"  # gitleaks:allow — intentional fake token (never-mint/classifier fixture)  # nosec B105 - not a credential: field/env/secret NAME, sentinel, or offline test fixture
    error = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": f"denied {secret}"}},
        "ListAgentRuntimes",
    )
    monkeypatch.setattr("boto3.client", lambda *_args, **_kwargs: (_ for _ in ()).throw(error))
    config._discover_orchestrator_arn("us-east-1")
    assert secret not in caplog.text


def test_accept_mapping_rejects_hashed_executable_endpoints(monkeypatch):
    from agents import self_learning

    item = {
        "mapping_id": "m1", "discovered_at": NOW.isoformat(),
        "proposed_type": "value_mapping", "from_value": "sha256:abc",
        "to_value": "B-1", "subject_key": "subject",
    }
    table = SimpleNamespace(query=lambda **_kwargs: {"Items": [item]})
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)
    monkeypatch.setattr(
        self_learning, "_apply_to_neptune",
        lambda *_args, **_kwargs: pytest.fail("non-actionable mapping reached Neptune"),
    )
    assert "non-actionable" in self_learning.accept_mapping("m1")["error"]


def test_hook_prefers_exact_server_executed_query_over_invocation_input():
    state: dict = {}
    hook = InvocationToolResultCapture(
        system_id="system-a", boundary="athena", physical_schema=SQL_SCHEMA,
    )
    hook._on_invocation_start(SimpleNamespace(invocation_state=state))
    payload = {
        "columns": ["measure"], "rows": [[7]], "row_count": 1,
        "_adt_executed_query": "SELECT measure FROM readings LIMIT 1000",
    }
    event = SimpleNamespace(
        invocation_state=state,
        selected_tool=SimpleNamespace(tool_name="athena_run_query"),
        tool_use={"name": "athena_run_query", "input": {"sql": "SELECT 999 AS measure FROM readings"}},
        result={"status": "success", "content": [{"json": payload}]},
        exception=None,
    )
    hook._on_after_tool_call(event)
    answer = _answer(state)
    assert answer.claims and answer.claims[0].value == 7


def test_local_mode_a_progress_queue_saturation_marks_partial():
    import queue
    from agents.chat_client import _SSECallbackHandler

    context = IntegrityContext("Show current values")
    target = queue.Queue(maxsize=2)
    callback = _SSECallbackHandler(
        target, emit_tokens=False, integrity_context=context,
    )
    for index in range(20):
        callback(current_tool_use={"toolUseId": f"tool-{index}", "name": "query"})
    assert target.qsize() == 2
    assert ReasonCode.TRAVERSAL_BUDGET_EXCEEDED in context.capture_reason_codes
