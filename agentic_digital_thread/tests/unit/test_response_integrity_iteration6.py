# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Response-integrity regressions (sixth set)."""
from __future__ import annotations

import asyncio
from decimal import Decimal
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.unit import _funclen_diff

from agents import onboard, runtime_app, server
from agents.response_integrity import (
    AnswerClaim, ClaimInput, ClaimKind, IntegrityContext, SystemAnswerDraft,
    capture_trusted_tool_result, system_answer_from_result,
)
from agents.response_integrity_query import (
    MAX_QUERY_COLUMNS, MAX_QUERY_RESULT_BYTES, bounded_query_result,
)
from agents.response_integrity_sql import validate_direct_projections
from agents.response_integrity_proposals import proposal_actionability_reason
from agents.runtime_integrity import public_scalar_metadata
from agents.stream_capture import CaptureAccessDenied


BASE_SCHEMA = {
    "readings": {
        "relation_kind": "base_table",
        "columns": {"measure": "numeric", "asset_id": "varchar"},
    },
    "other": {
        "relation_kind": "base_table",
        "columns": {"measure": "numeric"},
    },
}


def _value_claim(value):
    return AnswerClaim(
        claim_id="v", kind=ClaimKind.VALUE, label="ignored",
        inputs=(ClaimInput(
            label="ignored", value=value, semantic_field="measure", locator="row:0",
        ),),
    )


def test_runtime_intent_uses_raw_question_not_server_temporal_preamble():
    payload = {
        "prompt": "[Current temporal context: 2026-07-16T12:00:00Z] What happened yesterday?",
        "rawPrompt": "What happened yesterday?",
    }
    assert runtime_app._integrity_prompt(payload) == "What happened yesterday?"
    assert IntegrityContext(runtime_app._integrity_prompt(payload)).current_intent is False


@pytest.mark.parametrize("sql", [
    "SELECT r.measure FROM readings r, other o",
    "SELECT r.measure FROM readings r CROSS JOIN other o",
    "SELECT r.measure FROM readings r JOIN other o ON true",
])
def test_sql_lineage_rejects_every_multi_relation_shape(sql):
    proof = validate_direct_projections(sql, BASE_SCHEMA)
    assert proof.valid is False
    assert proof.reason == "multiple_relations"


def test_sql_lineage_defaults_missing_relation_kind_to_unknown():
    proof = validate_direct_projections(
        "SELECT measure FROM readings", {"readings": {"columns": {"measure": "numeric"}}},
    )
    assert proof.valid is False and proof.reason == "non_base_relation"


class _RecordingJobs:
    def __init__(self):
        self.data = {}
        self.puts = []

    def __setitem__(self, key, value):
        self.data[key] = json.loads(json.dumps(value))
        self.puts.append((key, json.loads(json.dumps(value))))

    def get(self, key, default=None):
        return json.loads(json.dumps(self.data.get(key, default)))


def test_map_metadata_is_written_back_through_shared_job_store(monkeypatch):
    jobs = _RecordingJobs()
    monkeypatch.setattr(onboard, "_map_jobs", jobs)

    def semantic(job_id, _system_id, _tables):
        jobs[job_id] = {"status": "done", "result": {"mappings": [
            {"table": "readings", "field": "readings.measure"},
        ]}}

    monkeypatch.setattr(onboard, "_run_map_job_legacy", semantic)
    onboard._run_map_job("j", "system-a", [{
        "name": "readings", "relation_kind": "view", "schema_revision": "a" * 32,
        "columns": [{"name": "measure", "data_type": "numeric"}],
    }])
    assert len(jobs.puts) == 2
    saved = jobs.puts[-1][1]["result"]["mappings"][0]
    assert saved["relation_kind"] == "view" and saved["data_type"] == "numeric"


def test_graph_provenance_is_written_only_after_async_schema_creation(monkeypatch):
    order = []
    monkeypatch.setattr(onboard, "_provision_jobs", _RecordingJobs())
    monkeypatch.setattr(onboard, "_trusted_provision_body", lambda body: body)
    monkeypatch.setattr(onboard, "_do_provision", lambda body: order.append("schema") or {"status": "provisioned"})
    monkeypatch.setattr(onboard, "_persist_schema_relation_kinds", lambda *_a: order.append("provenance"))
    body = SimpleNamespace(system_id="system-a", mappings=[])
    onboard._run_provision_job("j", body)
    assert order == ["schema", "provenance"]
    assert onboard._provision_jobs.get("j")["status"] == "done"


def test_initial_agent_schema_carries_relation_kind_and_type_and_defaults_unknown():
    details = {"physical": [("measure", "numeric")], "missing": [("measure", "numeric")]}
    enriched = onboard._table_details_with_provenance(details, [
        {"table": "physical", "field": "physical.measure", "relation_kind": "base_table", "data_type": "numeric"},
    ])
    assert enriched["physical"]["relation_kind"] == "base_table"
    assert enriched["missing"]["relation_kind"] == "unknown"
    assert enriched["physical"]["columns"] == [("measure", "numeric")]


def test_internal_query_proxy_auth_and_helper_surface_are_removed():
    from agents import system_agent
    from agents.auth.middleware import AuthMiddleware

    assert not hasattr(system_agent, "_make_proxy_query_tool")
    assert not hasattr(system_agent, "_make_query_tool_impl")
    assert not hasattr(AuthMiddleware, "_internal_query")


def test_exact_decimal_lineage_and_arithmetic_never_round_through_float():
    state = {}
    query = "SELECT measure FROM readings"
    exact = Decimal("0.123456789012345678")
    capture_trusted_tool_result(
        {"columns": ["measure"], "rows": [[exact]], "row_count": 1,
         "_adt_executed_query": query},
        state, system_id="system-a", boundary="sql", tool_name="query_readings",
        executed_query=query, physical_schema=BASE_SCHEMA,
    )
    result = SimpleNamespace(structured_output=SystemAnswerDraft(claims=(
        AnswerClaim(
            claim_id="sum", kind=ClaimKind.AGGREGATE, label="ignored", operation="sum",
            inputs=(
                ClaimInput(label="a", value="0.123456789012345678", semantic_field="measure", locator="row:0"),
            ),
        ),
    )))
    answer = system_answer_from_result(result, state)
    assert answer is not None
    assert answer.claims[0].value == exact
    assert isinstance(answer.claims[0].value, Decimal)


def test_query_bounds_cap_columns_cells_total_bytes_and_disclose_safe_prefix():
    columns = [f"c{i}" for i in range(MAX_QUERY_COLUMNS + 1)]
    rows = [["ok"] * MAX_QUERY_COLUMNS + ["not-disclosed"], ["x" * MAX_QUERY_RESULT_BYTES] * len(columns)]
    result = bounded_query_result(columns, rows, truncated=False)
    assert len(result["columns"]) == MAX_QUERY_COLUMNS
    assert result["rows"] == [["ok"] * MAX_QUERY_COLUMNS]
    assert result["truncated"] is True
    assert result["partial_reason"] in {"column_limit", "cell_limit", "byte_limit"}
    assert len(json.dumps(result).encode()) <= MAX_QUERY_RESULT_BYTES


def test_public_metadata_uses_exact_value_allowlists_not_key_only_allowlists():
    assert public_scalar_metadata({
        "app_status": "password=hunter2", "safe_error_category": "secret-token",
        "row_count": 3,
    }) == {"row_count": 3}
    assert public_scalar_metadata({
        "verdict": "password=hunter2", "completeness": "complete", "claim_count": 1,
    }, integrity=True) == {"completeness": "complete", "claim_count": 1}


def test_unregistered_tool_cannot_mint_evidence_or_public_identity():
    from agents.tool_result_capture import InvocationToolResultCapture
    state = {}
    hook = InvocationToolResultCapture(
        system_id="system-a", boundary="sql", physical_schema=BASE_SCHEMA,
        registered_tools={"query_readings"},
    )
    hook._on_invocation_start(SimpleNamespace(invocation_state=state))
    hook._on_after_tool_call(SimpleNamespace(
        invocation_state=state, selected_tool=SimpleNamespace(tool_name="forged_tool"),
        tool_use={"name": "forged_tool", "input": {}},
        result={"content": [{"json": {"columns": ["measure"], "rows": [[7]], "row_count": 1,
                                         "_adt_executed_query": "SELECT measure FROM readings"}}]},
        exception=None,
    ))
    assert not state["_adt_private_source_evidence"].observations
    assert state["_adt_successful_tool_results"] == []


def test_diagnostics_contain_categories_counts_and_hashes_only():
    from agents.stream_capture import _event_metadata
    captured = _event_metadata("data: " + json.dumps({
        "current_tool_use": {"toolUseId": "secret-id", "name": "secret-tool"},
        "tool_result_metadata": {"toolUseId": "secret-id", "name": "secret-tool",
                                 "metadata": {"row_count": 2}},
    }))
    assert "secret-id" not in captured and "secret-tool" not in captured
    parsed = json.loads(captured)
    assert parsed["event_category"] == "tool_progress"
    assert parsed["tool_hash"].startswith("sha256:")
    assert parsed["row_count"] == 2


def test_capture_access_requires_exact_session_admin_and_owner(monkeypatch, tmp_path):
    from agents import stream_capture
    monkeypatch.setattr(stream_capture, "_CAPTURE_DIR", tmp_path)
    monkeypatch.setattr(stream_capture, "_enabled", lambda: True)
    stream_capture._REGISTRY.clear()
    capture = stream_capture.open_stream_capture(owner_id="owner", session_id="session-a")
    assert capture is not None
    capture.write('data: {"data":"redacted"}')
    capture.close()
    assert stream_capture.list_captures(
        requester_id="owner", session_id="session-a", is_admin=True,
    )[0]["capture_id"] == capture.capture_id
    for kwargs in (
        {"requester_id": "owner", "session_id": "wrong", "is_admin": True},
        {"requester_id": "other", "session_id": "session-a", "is_admin": True},
        {"requester_id": "owner", "session_id": "session-a", "is_admin": False},
    ):
        with pytest.raises(CaptureAccessDenied):
            stream_capture.read_capture(capture.capture_id, **kwargs)
    stream_capture.delete_capture(
        capture.capture_id, requester_id="owner", session_id="session-a", is_admin=True,
    )


def test_mapping_endpoints_require_safe_exact_evidence_bound_policy():
    from agents.response_integrity_contracts import LearningProposal
    unsafe = LearningProposal(
        proposal_id="p", kind="field_annotation", from_system="system-a",
        from_field="free_text", concept="state", evidence_ids=("ev-1",),
        relationship_type="field_annotation", direction="self",
    )
    assert proposal_actionability_reason(unsafe) == "unclassified_field"


def test_iteration6_identity_aware_diff_maintainability():
    root = Path(__file__).resolve().parents[3]
    base = _funclen_diff.require_base(root)
    assert _funclen_diff.maintainability_failures(root, base) == []


def test_iteration6_jobstore_backend_logs_exclude_exception_text(caplog):
    from agents.job_store import JobStore

    class FailingTable:
        def put_item(self, **_kwargs):
            raise RuntimeError("SECRET_SQL select * from payroll")

    caplog.set_level("WARNING", logger="agents.job_store")
    JobStore("map", table=FailingTable())["job-1"] = {
        "status": "error", "error": "SECRET_SQL select * from payroll",
    }
    assert "SECRET_SQL" not in caplog.text
    assert "RuntimeError" in caplog.text


def test_iteration6_provision_startup_filter_removes_traceback_text():
    import logging
    from agents.onboard import _ProvisionFailureLogFilter

    record = logging.makeLogRecord({
        "msg": "MCP agent start FAILED for system-a: SECRET_EXCEPTION\nTraceback...",
        "args": (),
    })
    assert _ProvisionFailureLogFilter().filter(record) is True
    assert record.getMessage() == "MCP agent start failed category=provision_failed"
    assert "SECRET_EXCEPTION" not in record.getMessage()
    assert "Traceback" not in record.getMessage()
