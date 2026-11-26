# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""More adversarial regressions for deterministic response integrity."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.unit import _funclen_diff

from agents import onboard, query_router, server
from agents.chat_client import _CloudStreamState, ThinkingFilter
from agents.events import DoneEvent, ErrorEvent
from agents.response_integrity import (
    AnswerClaim,
    ClaimInput,
    ClaimKind,
    FinalAnswerDraft,
    IntegrityContext,
    LearningProposal,
    ReasonCode,
    SystemAnswerDraft,
    capture_orchestrator_tool_result,
    capture_trusted_tool_result,
    gate_final_result,
    render_final_answer,
    system_answer_from_result,
    validate_final_answer,
)
from agents.response_integrity_sql import validate_direct_projections
from agents.sql_safety import apply_row_cap

UTC = timezone.utc
NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
BASE_SCHEMA = {
    "readings": {
        "relation_kind": "base_table",
        "columns": {
            "measure": "integer", "observed_at": "timestamp",
            "asset_id": "varchar", "state": "varchar",
        },
    },
}


def _capture_sql(rows, *, columns=("measure",), query="SELECT measure FROM readings", current=False):
    state: dict = {}
    capture_trusted_tool_result(
        {"columns": list(columns), "rows": rows, "row_count": len(rows),
         "_adt_executed_query": query},
        state, system_id="system-a", boundary="sql", tool_name="query",
        queried_at=NOW, current_intent=current, executed_query=query,
        physical_schema=BASE_SCHEMA,
    )
    return state


def _system_answer(state, claims, status="complete"):
    result = SimpleNamespace(structured_output=SystemAnswerDraft(status=status, claims=tuple(claims)))
    answer = system_answer_from_result(result, state)
    assert answer is not None
    return answer


def _value_claim(value, *, field="measure", locator="row:0", claim_id="value"):
    return AnswerClaim(
        claim_id=claim_id, kind=ClaimKind.VALUE, label="ignored", value=value,
        inputs=(ClaimInput(label="ignored", value=value, semantic_field=field, locator=locator),),
    )


def _context(answer, prompt="Show historical values from last year"):
    context = IntegrityContext(prompt)
    capture_orchestrator_tool_result(
        "system-a___query", {"content": [{"json": answer.model_dump(mode="json")}]}, context,
    )
    return context


@pytest.mark.parametrize("operation", ["sum", "count", "average", "difference"])
def test_system_rejects_duplicate_structural_operands(operation):
    state = _capture_sql([[7]])
    repeated = ClaimInput(label="x", value=7, semantic_field="measure", locator="row:0")
    answer = _system_answer(state, (AnswerClaim(
        claim_id="aggregate", kind=ClaimKind.AGGREGATE, label="ignored",
        operation=operation, inputs=(repeated, repeated), value=14,
    ),))
    assert answer.claims == ()
    assert ReasonCode.DISTINCT_EVIDENCE_OPERANDS_REQUIRED in answer.reason_codes


def test_system_ranking_rejects_one_observation_reused_twice():
    state = _capture_sql([["A-1", 7]], columns=("asset_id", "measure"),
                         query="SELECT asset_id, measure FROM readings")
    repeated = ClaimInput(label="x", value=7, semantic_field="measure", locator="row:0")
    answer = _system_answer(state, (AnswerClaim(
        claim_id="rank", kind=ClaimKind.RANKING, label="ignored", operation="rank_desc",
        inputs=(repeated, repeated),
    ),))
    assert answer.claims == ()
    assert ReasonCode.DISTINCT_EVIDENCE_OPERANDS_REQUIRED in answer.reason_codes


def test_final_rejects_duplicate_structural_operands_even_if_system_claims_are_forged():
    state = _capture_sql([[7]])
    source = _system_answer(state, (_value_claim(7),))
    context = _context(source)
    repeated = source.claims[0].inputs[0]
    final = validate_final_answer(FinalAnswerDraft(claims=(AnswerClaim(
        claim_id="sum", kind=ClaimKind.AGGREGATE, label="ignored", operation="sum",
        inputs=(repeated, repeated),
    ),)), context)
    assert final.claims == ()
    assert ReasonCode.DISTINCT_EVIDENCE_OPERANDS_REQUIRED in final.reason_codes


@pytest.mark.parametrize("freshness", ["stale", "unknown"])
def test_current_request_omits_noncurrent_claim_but_discloses_safe_latest_time(freshness):
    if freshness == "stale":
        columns = ("measure", "__adt_source_time__measure")
        rows = [[7, (NOW - timedelta(days=2)).isoformat()]]
        query = "SELECT measure, observed_at AS __adt_source_time__measure FROM readings"
    else:
        columns, rows, query = ("measure",), [[7]], "SELECT measure FROM readings"
    state = _capture_sql(rows, columns=columns, query=query, current=True)
    system_answer = _system_answer(state, (_value_claim(7),))
    assert system_answer.claims == ()
    context = _context(system_answer, "Show current inventory")
    text, final = gate_final_result(SimpleNamespace(structured_output=FinalAnswerDraft()), context)
    assert final.claims == () and "**measure**: 7" not in text
    if freshness == "stale":
        assert "2026-07-14T12:00:00Z" in text
        assert ReasonCode.STALE_CURRENT_DATA in final.reason_codes
    else:
        assert "source_observed_at=unknown" in text
        assert ReasonCode.CURRENT_EVIDENCE_REQUIRED in final.reason_codes


def test_mixed_inventory_and_restock_request_requires_current_evidence():
    assert IntegrityContext("Show inventory and explain what was restocked").current_intent is True
    assert IntegrityContext("Explain what was restocked yesterday").current_intent is False


@pytest.mark.parametrize("marker", [
    {"@odata.nextLink": "/next"}, {"LastEvaluatedKey": {"id": "next"}},
    {"IsTruncated": True}, {"NextContinuationToken": "next"},
    {"NEXT-TOKEN": "next"}, {"has.more": True},
])
def test_normalized_pagination_markers_fail_partial(marker):
    state: dict = {}
    capture_trusted_tool_result(
        {"items": [{"measure": 7}], **marker}, state,
        system_id="system-a", boundary="rest", tool_name="read",
    )
    answer = _system_answer(state, ())
    assert answer.status == "partial"
    assert ReasonCode.SOURCE_PARTIAL in answer.reason_codes


def test_orchestrator_thirty_third_unavailable_fragment_marks_budget_and_partial():
    context = IntegrityContext("Show current inventory")
    content = [{"json": {"status": "complete", "claims": [], "evidence": [], "reason_codes": []}}
               for _ in range(32)]
    content.append({"json": {"status": "unavailable", "claims": [], "evidence": [],
                              "reason_codes": ["SOURCE_UNAVAILABLE"]}})
    capture_orchestrator_tool_result("system-a___query", {"status": "success", "content": content}, context)
    assert ReasonCode.TRAVERSAL_BUDGET_EXCEEDED in context.capture_reason_codes
    assert ReasonCode.SOURCE_PARTIAL in context.capture_reason_codes


@pytest.mark.parametrize("raw", [
    {"status": "success", "content": [{"text": "{"}]},
    {"status": "success", "content": []},
    {"status": "success", "content": [{"text": "x" * 300_000}]},
])
def test_completed_system_call_with_unparseable_empty_or_oversized_envelope_is_partial(raw):
    context = IntegrityContext("Show current inventory")
    capture_orchestrator_tool_result("system-a___query", raw, context)
    assert ReasonCode.SOURCE_PARTIAL in context.capture_reason_codes


@pytest.mark.parametrize("sql,expected", [
    ("SELECT 'LIMIT 1' AS note FROM readings", "SELECT 'LIMIT 1' AS note FROM readings LIMIT 5"),
    ("SELECT measure FROM readings -- LIMIT 1", "SELECT measure FROM readings LIMIT 5 -- LIMIT 1"),
    ("SELECT * FROM (SELECT measure FROM readings LIMIT 1) q", "SELECT * FROM (SELECT measure FROM readings LIMIT 1) q LIMIT 5"),
    ("WITH q AS (SELECT measure FROM readings LIMIT 1) SELECT * FROM q", "WITH q AS (SELECT measure FROM readings LIMIT 1) SELECT * FROM q LIMIT 5"),
    ("SELECT measure FROM readings LIMIT 99", "SELECT measure FROM readings LIMIT 5"),
    ("SELECT measure FROM readings LIMIT 3", "SELECT measure FROM readings LIMIT 3"),
])
def test_outer_query_cap_ignores_literals_comments_and_inner_limits(sql, expected):
    capped, _ = apply_row_cap(sql, 5)
    assert capped == expected


def test_athena_fetches_max_plus_one_and_marks_partial_without_next_token():
    from agents.athena_tools import _collect_results

    class Athena:
        def get_query_results(self, **_kwargs):
            return {
                "ResultSet": {
                    "ResultSetMetadata": {"ColumnInfo": [{"Name": "measure"}]},
                    "Rows": [
                        {"Data": [{"VarCharValue": "measure"}]},
                        *({"Data": [{"VarCharValue": str(index)}]} for index in range(6)),
                    ],
                },
            }

    result = _collect_results(Athena(), "qid", 5)
    assert result["row_count"] == 5 and len(result["rows"]) == 5
    assert result["truncated"] is True


def test_deployed_query_proxy_surface_is_removed():
    paths = {getattr(route, "path", "") for route in server.app.routes}
    assert "/api/query/{system_id}" not in paths
    assert not hasattr(server, "proxy_query")
    assert not hasattr(server, "_execute_proxy_query")


def test_query_router_never_logs_raw_sql_or_traceback(monkeypatch, caplog):
    secret_sql = "SELECT 'ghp_abcdefghijklmnopqrstuvwxyz1234567890'"  # gitleaks:allow — intentional fake token (never-mint/classifier fixture)  # nosec B105 - not a credential: field/env/secret NAME, sentinel, or offline test fixture
    monkeypatch.setattr(query_router.catalog, "get", lambda _sid: {"connection_type": "PostgreSQL", "schema": "s"})
    monkeypatch.setattr(query_router, "pg", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError(secret_sql)))
    result = query_router.query_system("system-a", secret_sql)
    assert result == {"columns": [], "rows": [], "row_count": 0}
    assert secret_sql not in caplog.text and "Traceback" not in caplog.text


def test_hook_trusts_only_server_returned_executed_query():
    from agents.tool_result_capture import InvocationToolResultCapture

    state: dict = {}
    hook = InvocationToolResultCapture(system_id="system-a", boundary="sql", physical_schema=BASE_SCHEMA)
    hook._on_invocation_start(SimpleNamespace(invocation_state=state))
    hook._on_after_tool_call(SimpleNamespace(
        invocation_state=state, selected_tool=SimpleNamespace(tool_name="query"),
        tool_use={"name": "query", "input": {"sql": "SELECT measure FROM readings"}},
        result={"status": "success", "content": [{"json": {
            "columns": ["measure"], "rows": [[7]], "row_count": 1,
        }}]}, exception=None,
    ))
    answer = _system_answer(state, (_value_claim(7),))
    assert answer.claims == ()
    assert ReasonCode.STRUCTURAL_LINEAGE_MISMATCH in answer.reason_codes


def test_view_relation_cannot_launder_physical_value_evidence():
    view_schema = {"readings": {"relation_kind": "view", "columns": {"measure": "integer"}}}
    proof = validate_direct_projections("SELECT measure FROM readings", view_schema)
    assert proof.valid is False and proof.reason == "non_base_relation"
    state: dict = {}
    capture_trusted_tool_result(
        {"columns": ["measure"], "rows": [[7]], "row_count": 1,
         "_adt_executed_query": "SELECT measure FROM readings"},
        state, system_id="system-a", boundary="sql", tool_name="query",
        executed_query="SELECT measure FROM readings", physical_schema=view_schema,
    )
    assert _system_answer(state, (_value_claim(7),)).claims == ()


class _Log:
    def info(self, *_a, **_k):
        pass

    def error(self, *_a, **_k):
        pass


def test_runtime_error_frame_becomes_fixed_error_before_done():
    state = _CloudStreamState(
        ThinkingFilter(), integrity_mode=True, log=_Log(), owner_id="u", session_id="s",
        runtime_session_id="runtime-resource-id-secret",
    )
    events = state.events_for({"error": "traceback ghp_abcdefghijklmnopqrstuvwxyz1234567890 resource-123"})  # gitleaks:allow — intentional fake token (never-mint/classifier fixture)
    events.extend(state.completion_events())
    assert isinstance(events[0], ErrorEvent)
    assert isinstance(events[-1], DoneEvent)
    rendered = " ".join(getattr(event, "message", "") + getattr(event, "details", "") for event in events)
    assert "ghp_" not in rendered and "traceback" not in rendered.lower() and "resource-123" not in rendered


def test_relation_kind_flows_through_discover_and_untrusted_map(monkeypatch):
    body = SimpleNamespace(connection_type="PostgreSQL", system_id="system-a")
    result = {"tables": [
        {"name": "physical", "schema_revision": "a" * 32,
         "columns": [{"name": "measure", "data_type": "integer"}]},
        {"name": "laundered", "schema_revision": "a" * 32,
         "columns": [{"name": "measure", "data_type": "integer"}]},
    ]}
    monkeypatch.setattr(onboard, "_postgres_relation_kinds", lambda _body: {
        "physical": "base_table", "laundered": "view",
    })
    annotated = onboard._annotate_discovered_relations(body, result)
    assert [table["relation_kind"] for table in annotated["tables"]] == ["base_table", "view"]

    job_id = "relation-kind-regression"
    def semantic_mapper(job, _system, _tables):
        onboard._map_jobs[job] = {"status": "done", "result": {"mappings": [
            {"table": "physical", "field": "physical.measure"},
            {"table": "laundered", "field": "laundered.measure"},
        ]}}
    monkeypatch.setattr(onboard, "_run_map_job_legacy", semantic_mapper)
    onboard._run_map_job(job_id, "system-a", annotated["tables"])
    mappings = onboard._map_jobs[job_id]["result"]["mappings"]
    assert [mapping["relation_kind"] for mapping in mappings] == ["base_table", "view"]
    assert [mapping["data_type"] for mapping in mappings] == ["integer", "integer"]


def test_relation_kind_persists_and_restores_in_compact_schema(monkeypatch):
    writes = []
    monkeypatch.setattr(
        onboard, "neptune_write",
        lambda query, params: writes.append((query, params)) or True,
    )
    onboard._persist_schema_relation_kinds("system-a", [
        {"table": "physical", "field": "physical.measure", "relation_kind": "base_table", "data_type": "integer"},
        {"table": "laundered", "field": "laundered.measure", "relation_kind": "view", "data_type": "integer"},
    ])
    assert [params["kind"] for _query, params in writes if "kind" in params] == ["base_table", "view"]
    assert [params["dtype"] for _query, params in writes if "dtype" in params] == ["integer", "integer"]

    rows = [
        {"tbl": "physical", "relation_kind": "base_table", "col": "measure", "dtype": "integer"},
        {"tbl": "laundered", "relation_kind": "view", "col": "measure", "dtype": "integer"},
    ]
    monkeypatch.setattr(server, "neptune_query", lambda *_a, **_k: rows)
    restored = server._sql_tables_desc_from_graph("system-a")
    assert "physical [base_table](measure: integer)" in restored
    assert "laundered [view](measure: integer)" in restored
    from agents.response_integrity_sql import parse_physical_schema
    schema = parse_physical_schema(restored)
    assert validate_direct_projections("SELECT measure FROM physical", schema).valid
    assert validate_direct_projections("SELECT measure FROM laundered", schema).reason == "non_base_relation"


def test_runtime_setup_failure_is_fixed_error_then_done(monkeypatch):
    monkeypatch.setattr("agents.chat_client.boto3.client", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("secret traceback")))
    from agents.chat_client import AgentCoreChatClient
    from agents.events import public_error_message
    events = list(AgentCoreChatClient(runtime_arn="arn:secret", region="us-east-1").stream("prompt-secret", "s", "u"))
    assert [type(event) for event in events] == [ErrorEvent, DoneEvent]
    # Fixed per-category text from the ONE message table — never the exception.
    assert events[0].message == public_error_message("runtime_unavailable")
    assert events[0].category == "runtime_unavailable"
    assert events[0].correlation_id == "s"
    assert "secret" not in events[0].message
    assert events[0].details == ""


def test_postgres_restore_replaces_legacy_schema_with_graph_provenance(monkeypatch):
    manager = SimpleNamespace(start_agent=lambda **kwargs: kwargs)
    captured = {}
    def start_agent(**kwargs):
        captured.update(kwargs)
        return "mcp"
    manager.start_agent = start_agent
    monkeypatch.setattr(server, "_agent_manager", manager)
    monkeypatch.setattr(server, "_sql_tables_desc_from_graph", lambda _sid: "readings [view](measure: integer)")
    monkeypatch.setattr("agents.onboard.neptune_query_checked", lambda *_a, **_k: [])
    monkeypatch.setattr(server, "_restore_legacy", lambda: server._agent_manager.start_agent(
        system_id="system-a", tables_desc="readings(measure: integer)",
    ))
    server._restore()
    assert captured["tables_desc"] == "readings [view](measure: integer)"


def _field_annotation(answer, **updates):
    evidence_id = answer.evidence[0].evidence_id
    data = {
        "proposal_id": "annotation", "kind": "field_annotation",
        "from_system": "system-a", "from_field": "readings.state", "from_value": "RUNNING",
        "concept": "operating-state", "evidence_ids": (evidence_id,),
        "relationship_type": "field_annotation", "direction": "self",
    }
    data.update(updates)
    return LearningProposal(**data)


def test_non_actionable_proposal_is_dropped_during_validation_with_explicit_reason():
    state = _capture_sql([["RUNNING"]], columns=("state",), query="SELECT state FROM readings")
    answer = _system_answer(state, (_value_claim("RUNNING", field="state"),))
    final = validate_final_answer(FinalAnswerDraft(
        claims=(answer.claims[0],), proposals=(_field_annotation(answer),),
    ), _context(answer))
    assert final.proposals == () and final.learning_allowed is False
    assert ReasonCode.LEARNING_PROPOSAL_NON_ACTIONABLE in final.reason_codes


def test_actionable_field_annotation_remains_one_endpoint_and_persistable(monkeypatch):
    from agents import runtime_integrity

    calls = []
    monkeypatch.setattr("agents.self_learning.log_proposed_mapping",
                        lambda **kwargs: calls.append(kwargs) or "vm-stub")
    state = _capture_sql([["RUNNING"]], columns=("state",), query="SELECT state FROM readings")
    answer = _system_answer(state, (_value_claim("RUNNING", field="state"),))
    proposal = _field_annotation(answer, from_value="")
    final = validate_final_answer(FinalAnswerDraft(
        claims=(answer.claims[0],), proposals=(proposal,),
    ), _context(answer))
    assert final.proposals == (proposal,) and final.learning_allowed
    assert runtime_integrity.persist_typed_proposals(final, tool_calls_count=1) == 1
    assert len(calls) == 1


@pytest.mark.parametrize("field", ["clientSecret", "ClientSecret", "apiCredential", "accessToken", "passPhrase"])
def test_camelcase_and_credential_synonym_fields_are_sensitive(field):
    from agents.response_integrity_security import sensitive_field
    assert sensitive_field(field)


@pytest.mark.parametrize("secret", [
    "".join(("ghp_", "abcdefghijklmnopqrstuvwxyz1234567890")),  # gitleaks:allow — intentional fake token (never-mint/classifier fixture)
    "".join(("sk_", "live_abcdefghijklmnopqrstuvwxyz")),  # gitleaks:allow — intentional fake token (never-mint/classifier fixture)
    "".join(("xoxb-", "1234567890-1234567890-abcdefghijklmnop")),  # gitleaks:allow — intentional fake token (never-mint regression)
    "".join(("apiKey_", "abcdefghijklmnopqrstuvwxyz123456")),  # gitleaks:allow — intentional fake token (never-mint/classifier fixture)
])
def test_nested_sensitive_ancestor_and_token_formats_never_mint(secret):
    state: dict = {}
    capture_trusted_tool_result(
        {"clientSecret": {"asset_id": "opaque-nested"}, "asset_id": secret}, state,
        system_id="system-a", boundary="rest", tool_name="read",
    )
    private = state["_adt_private_source_evidence"]
    assert not private.observations


def test_final_output_secret_scan_replaces_forged_identity_output():
    from agents.response_integrity_contracts import FinalAnswer, RelationshipProof

    proof = RelationshipProof(
        graph_record_id="vm-1", source_system="system-a",
        source_identifier="ghp_abcdefghijklmnopqrstuvwxyz1234567890", source_field="asset_id",  # gitleaks:allow — intentional fake token (never-mint/classifier fixture)
        target_system="system-b", target_identifier="B-1", target_field="asset_id",
        relationship_type="value_mapping", direction="outbound",
    )
    answer = FinalAnswer(
        claims=(AnswerClaim(claim_id="i", kind="identity", label="ignored",
                            relationship_proof=proof),),
        evidence=(), proposals=(), reason_codes=(), complete=True,
        learning_allowed=False, current_intent=False,
    )
    text = render_final_answer(answer)
    assert "ghp_" not in text
    assert ReasonCode.OUTPUT_SENSITIVITY_REJECTED.value in text


def test_diagnostic_capture_rebuilds_nested_metadata_from_scalar_allowlists():
    from agents.stream_capture import _event_metadata

    secret = "ghp_abcdefghijklmnopqrstuvwxyz1234567890"  # gitleaks:allow — intentional fake token (never-mint/classifier fixture)  # nosec B105 - not a credential: field/env/secret NAME, sentinel, or offline test fixture
    line = "data: " + json.dumps({
        "tool_result_metadata": {
            "toolUseId": "id", "name": "query",
            "metadata": {"row_count": 1, "forged": {"password": "hunter2"}, "token": secret},
        },
        "response_integrity": {
            "verdict": "rejected", "claim_count": 0,
            "forged": {"password": "hunter2"}, "token": secret,
        },
    })
    captured = _event_metadata(line)
    assert "hunter2" not in captured and secret not in captured
    parsed = json.loads(captured)
    assert parsed["event_category"] == "tool_result"
    assert parsed["row_count"] == 1
    assert parsed["tool_hash"].startswith("sha256:")
    assert parsed["integrity_verdict"] == "rejected"
    assert parsed["integrity_claim_count"] == 0


def test_mode_b_large_stream_and_event_shapes_remain_unchanged(monkeypatch):
    class Stream:
        def iter_lines(self, **_kwargs):
            yield b'data: {"data":"abcdefgh"}'

    class Client:
        def invoke_agent_runtime(self, **_kwargs):
            return {"response": Stream()}

    from agents.chat_client import AgentCoreChatClient
    monkeypatch.setattr("agents.chat_client.boto3.client", lambda *_a, **_k: Client())
    monkeypatch.setattr("agents.runtime_integrity.MAX_PUBLIC_STREAM_BYTES", 1)
    monkeypatch.setenv("SYSTEM_QUERY_MODE", "orchestrator")
    events = list(AgentCoreChatClient(runtime_arn="arn:r", region="us-east-1").stream("p", "s", "u"))
    assert any(getattr(event, "content", "") == "abcdefgh" for event in events)
    assert isinstance(events[-1], DoneEvent)


def test_iteration5_exhaustive_changed_production_maintainability():
    root = Path(__file__).resolve().parents[3]
    base = _funclen_diff.require_base(root)
    assert _funclen_diff.maintainability_failures(root, base) == []


def test_runtime_log_filter_removes_prompt_and_exception_previews():
    import logging
    from agents.runtime_app import _SafeRuntimeLogFilter
    secret = "ghp_abcdefghijklmnopqrstuvwxyz1234567890"  # gitleaks:allow — intentional fake token (never-mint/classifier fixture)  # nosec B105 - not a credential: field/env/secret NAME, sentinel, or offline test fixture
    records = [
        logging.LogRecord("runtime", logging.INFO, "", 0,
                          "=== ORCHESTRATOR INVOKED [v] === session=s prompt=%s", (secret,), None),
        logging.LogRecord("runtime", logging.WARNING, "", 0,
                          "Agent analysis failed: %s", (secret,), None),
    ]
    filter_ = _SafeRuntimeLogFilter()
    rendered = []
    for record in records:
        assert filter_.filter(record)
        rendered.append(record.getMessage())
    assert all(secret not in message for message in rendered)
    assert "prompt=" not in rendered[0]


async def _collect_sse(chunks):
    return [chunk async for chunk in server._fixed_public_sse(chunks)]


def test_server_public_sse_replaces_error_and_appends_done():
    async def chunks():
        yield b'event: error\ndata: {"text":"traceback ghp_secret resource-123"}\n\n'
    frames = asyncio.run(_collect_sse(chunks()))
    rendered = b"".join(frame if isinstance(frame, bytes) else frame.encode() for frame in frames)
    assert b"Query failed." in rendered
    assert b"ghp_secret" not in rendered and b"traceback" not in rendered
    assert b"event: done" in rendered
