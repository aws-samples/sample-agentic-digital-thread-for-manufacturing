# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Source-boundary metadata tests for deterministic response integrity."""
from __future__ import annotations

import json

import agents.athena_tools as athena
import agents.sitewise_read as sitewise_read
import agents.system_agent as system_agent


def _tool(tools, name):
    return next(item for item in tools if item.tool_name == name)


class _Cursor:
    description = [("measure",), ("__adt_source_time__measure",)]

    def __init__(self):
        self._rows = iter(((7, "2026-01-02T12:00:00+00:00"),))

    def execute(self, *_args, **_kwargs):
        return None

    def fetchone(self):
        return next(self._rows, None)

    def close(self):
        return None


class _Connection:
    def set_session(self, **_kwargs):
        return None

    def cursor(self):
        return _Cursor()

    def close(self):
        return None


def test_postgres_mode_a_boundary_adds_attributed_source_metadata(monkeypatch):
    monkeypatch.setattr(system_agent.pgdriver, "connect", lambda **_kwargs: _Connection())
    query = system_agent._make_query_tool(
        "public", {"host": "example", "dbname": "db"}, system_id="system-sql",
    )
    result = json.loads(query._tool_func(sql="SELECT measure, observed_at FROM readings"))
    metadata = result["_adt_source"]
    assert metadata["system_id"] == "system-sql"
    assert metadata["boundary"] == "sql"
    assert metadata["source_observed_end"] == "2026-01-02T12:00:00Z"
    assert metadata["queried_at"] != metadata["source_observed_end"]
    assert "rows" not in metadata


class _Athena:
    def start_query_execution(self, **_kwargs):
        return {"QueryExecutionId": "query-id"}

    def get_query_execution(self, **_kwargs):
        return {"QueryExecution": {
            "Status": {"State": "SUCCEEDED"},
            "Statistics": {"DataScannedInBytes": 10},
        }}

    def get_query_results(self, **_kwargs):
        return {"ResultSet": {
            "ResultSetMetadata": {"ColumnInfo": [
                {"Name": "measure"}, {"Name": "__adt_source_time__measure"},
            ]},
            "Rows": [
                {"Data": [{"VarCharValue": "measure"}, {"VarCharValue": "__adt_source_time__measure"}]},
                {"Data": [{"VarCharValue": "8"}, {"VarCharValue": "2026-01-02T11:59:00Z"}]},
            ],
        }}


def test_athena_boundary_adds_attributed_source_metadata():
    result = athena.run_athena_query(
        "SELECT measure, event_time FROM readings",
        {"workgroup": "wg", "database": "db", "region": "us-east-1"},
        client=_Athena(), source_system="system-lake",
    )
    metadata = result["_adt_source"]
    assert metadata["system_id"] == "system-lake"
    assert metadata["boundary"] == "athena"
    assert metadata["source_observed_end"] == "2026-01-02T11:59:00Z"
    assert metadata["row_count"] == 1


def test_sitewise_boundary_uses_property_timestamp_not_query_time(monkeypatch):
    monkeypatch.setattr(
        sitewise_read,
        "batch_get_property_values",
        lambda *_args, **_kwargs: {
            "entry-a": {"value": 12.5, "timestamp": 1767355200, "quality": "GOOD"},
        },
    )
    tools = system_agent._make_sitewise_direct_tool(
        region="us-east-1", connection_config={}, system_id="system-telemetry",
    )
    batch = _tool(tools, "sitewise_batch_get_property_values")
    result = json.loads(batch._tool_func(
        entries_json='[{"assetId":"asset-a","propertyId":"property-a"}]',
    ))
    metadata = result["_adt_source"]
    assert metadata["system_id"] == "system-telemetry"
    assert metadata["boundary"] == "sitewise"
    assert metadata["source_observed_end"].startswith("2026-01-02T12:00:00")
    assert metadata["queried_at"] != metadata["source_observed_end"]
    assert metadata["item_count"] == 1


def _hook_event(state, payload, tool_name="read_values"):
    return type("Event", (), {
        "invocation_state": state,
        "selected_tool": type("Tool", (), {"tool_name": tool_name})(),
        "tool_use": {"name": tool_name},
        "result": {"status": "success", "content": [{"json": payload}]},
        "exception": None,
    })()


def test_external_mode_a_connector_hooks_mint_structural_evidence_without_freshness():
    from agents.response_integrity import (
        AnswerClaim, ClaimInput, ClaimKind, SystemAnswerDraft, system_answer_from_result,
    )
    from types import SimpleNamespace

    for boundary in ("i3x", "rest", "mcp"):
        state = {}
        hook = system_agent._InvocationToolResultCapture(
            system_id=f"system-{boundary}", boundary=boundary,
        )
        hook._on_invocation_start(type("Start", (), {"invocation_state": state})())
        hook._on_after_tool_call(_hook_event(
            state, {"items": [{"property": "temperature", "value": 21.5}]},
        ))
        answer = system_answer_from_result(SimpleNamespace(
            structured_output=SystemAnswerDraft(claims=(AnswerClaim(
                claim_id="value", kind=ClaimKind.VALUE, label="Temperature", value=21.5,
                inputs=(ClaimInput(
                    label="value", value=21.5, semantic_field="value", locator="items:0",
                ),),
            ),)),
        ), state)
        assert answer is not None and answer.claims, boundary
        assert answer.evidence[0].boundary == boundary
        assert answer.evidence[0].source_observed_end is None
        assert answer.evidence[0].freshness.value == "unknown"


def test_athena_mode_b_call_without_source_system_preserves_response_shape():
    result = athena.run_athena_query(
        "SELECT measure, event_time FROM readings",
        {"workgroup": "wg", "database": "db", "region": "us-east-1"},
        client=_Athena(),
    )
    assert set(result) == {"columns", "rows", "row_count"}
    assert "_adt_source" not in result


def test_athena_mode_a_injected_cap_is_partial_without_next_token():
    result = athena.run_athena_query(
        "SELECT measure, event_time FROM readings",
        {"workgroup": "wg", "database": "db", "region": "us-east-1"},
        client=_Athena(), source_system="system-lake", max_rows=1,
    )
    assert result["truncated"] is True
    assert result["capped_at_limit"] is True
    assert result["_adt_source"]["completeness"] == "partial"


def test_external_updated_at_does_not_bind_unrelated_value_freshness():
    from agents.response_integrity import (
        AnswerClaim, ClaimInput, ClaimKind, SystemAnswerDraft,
        capture_trusted_tool_result, system_answer_from_result,
    )
    from types import SimpleNamespace

    state = {}
    capture_trusted_tool_result(
        {"content": [{"json": {
            "items": [{"value": 21.5, "updated_at": "2026-01-02T12:00:00Z"}],
        }}]},
        state, system_id="system-rest", boundary="rest", tool_name="read_values",
    )
    answer = system_answer_from_result(SimpleNamespace(structured_output=SystemAnswerDraft(
        claims=(AnswerClaim(
            claim_id="value", kind=ClaimKind.VALUE, label="Temperature", value=21.5,
            inputs=(ClaimInput(
                label="value", value=21.5, semantic_field="value", locator="items:0",
            ),),
        ),),
    )), state)
    assert answer is not None and answer.claims
    assert answer.evidence[0].source_observed_end is None
    assert answer.evidence[0].freshness.value == "unknown"
