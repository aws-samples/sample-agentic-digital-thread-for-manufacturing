# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Mode A response extraction never reports byte-empty success after tool work."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

import agents.system_agent as sa


def _result(*, content=None, structured_output=None, stop_reason="end_turn"):
    return SimpleNamespace(
        message={"role": "assistant", "content": content or []},
        structured_output=structured_output,
        interrupts=None,
        stop_reason=stop_reason,
    )


def test_normal_final_text_is_private_and_returns_metadata_only_error():
    result = _result(content=[{"text": "Grounded answer with person@example.com."}])
    out = json.loads(sa._extract_mode_a_result(result, {}))
    assert out["status"] == "error"
    assert out["error_type"] == "incomplete"
    assert "Grounded answer" not in json.dumps(out)
    assert "person@example.com" not in json.dumps(out)


def test_no_text_with_safe_success_metadata_returns_typed_partial():
    state = {
        sa._TOOL_RESULTS_STATE_KEY: [{
            "tool": "read_values",
            "transport_status": "success",
            "app_status": "success",
            "row_count": 3,
            "_has_success": True,
        }]
    }
    out = json.loads(sa._extract_mode_a_result(_result(content=[{"toolUse": {}}]), state))
    assert out["status"] == "partial"
    assert out["error_type"] == "incomplete"
    assert out["tool_results"][0]["row_count"] == 3
    assert "content" not in out["tool_results"][0]
    assert "no final text" in out["message"].lower()


def test_no_text_and_no_usable_result_returns_typed_error():
    # A max_tokens stop now carries its own fixed text (test_mode_a_output_limit).
    out = json.loads(sa._extract_mode_a_result(_result(stop_reason="end_turn"), {}))
    assert out["status"] == "error"
    assert out["error_type"] == "incomplete"
    assert out["stop_reason"] == "end_turn"
    assert "no usable tool result" in out["message"].lower()


class _StructuredAnswer(BaseModel):
    total: int


def test_arbitrary_structured_output_cannot_bypass_system_answer_contract():
    out = json.loads(sa._extract_mode_a_result(
        _result(structured_output=_StructuredAnswer(total=7)), {}
    ))
    assert out["status"] == "error"
    assert out["error_type"] == "incomplete"
    assert "total" not in out


def test_tool_result_capture_discards_arbitrary_text_content():
    capture = sa._InvocationToolResultCapture(max_results=1, max_chars=32)
    state = {}
    capture._on_invocation_start(SimpleNamespace(invocation_state=state))
    event = SimpleNamespace(
        invocation_state=state,
        selected_tool=SimpleNamespace(tool_name="read_values"),
        tool_use={"name": "read_values"},
        result={"status": "success", "content": [{"text": "x" * 200}]},
        exception=None,
    )
    capture._on_after_tool_call(event)
    captured = state[sa._TOOL_RESULTS_STATE_KEY]
    assert len(captured) == 1
    assert captured[0]["tool"] == "read_values"
    assert captured[0]["app_status"] == "unknown"
    assert "content" not in captured[0]


def _call_text(mcp, tool_name):
    result = asyncio.run(mcp.call_tool(tool_name, {"question": "What happened?"}))
    content = result[0] if isinstance(result, tuple) else result
    return "\n".join(
        block.text for block in content or [] if getattr(block, "text", None) is not None
    )


@pytest.mark.parametrize("connection_type", ["PostgreSQL", "MCP Server"])
def test_both_mode_a_wrappers_use_shared_extractor(monkeypatch, connection_type):
    monkeypatch.setenv("SYSTEM_QUERY_MODE", "agent")
    # Enforce-path semantics (ADR-0046) — opt in per ADR-0047.
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "enforce")
    seen_states = []

    class _FakeAgent:
        def __call__(self, prompt, *, invocation_state=None, limits=None,
                     structured_output_model=None):
            assert structured_output_model is sa.SystemAnswerDraft
            seen_states.append(invocation_state)
            invocation_state[sa._TOOL_RESULTS_STATE_KEY] = [{
                "tool": "read_values",
                "transport_status": "success",
                "app_status": "success",
                "item_count": 1,
                "_has_success": True,
            }]
            return _result(content=[])

    monkeypatch.setattr(sa, "create_system_agent", lambda **_kwargs: _FakeAgent())
    mcp = sa.create_mcp_server(
        system_id="system-x",
        system_name="System X",
        schema="public",
        system_prompt="",
        tables_desc="",
        conn_params={"host": "h", "dbname": "d"},
        connection_type=connection_type,
        connection_config={},
    )
    out = json.loads(_call_text(mcp, "query_system_x"))
    assert out["status"] == "partial"
    assert out["tool_results"][0]["item_count"] == 1
    assert "content" not in out["tool_results"][0]
    assert len(seen_states) == 1
    assert isinstance(seen_states[0], dict)


# --------------------------------------------------------------------------- #
# Adversarial capture: metadata only, bounded, and honest about app failures
# --------------------------------------------------------------------------- #
def _capture_event(capture, state, result, name="read_values"):
    capture._on_after_tool_call(SimpleNamespace(
        invocation_state=state,
        selected_tool=SimpleNamespace(tool_name=name),
        tool_use={"name": name},
        result=result,
        exception=None,
    ))


@pytest.mark.parametrize(
    ("payload", "category"),
    [
        ({"error": "Authorization: Bearer secret"}, "tool_error"),
        ({"error_type": "query_timeout", "query_id": "private"}, "query_timeout"),
        ({"error_type": "scan_budget_exceeded", "rows": [["private"]]}, "scan_budget_exceeded"),
        ({"error_type": "scope_required", "guidance": ["private"]}, "scope_required"),
        ({"status": "error", "message": "password=private"}, "tool_error"),
        ({"asset_prop": {"error": "ResourceNotFound: private"}}, "sitewise_error"),
    ],
)
def test_app_error_shapes_are_failures_and_raw_error_text_never_leaks(payload, category):
    capture = sa._InvocationToolResultCapture(max_results=4, max_chars=128)
    state = {}
    capture._on_invocation_start(SimpleNamespace(invocation_state=state))
    _capture_event(capture, state, {"status": "success", "content": [{"json": payload}]})
    out = json.loads(sa._extract_mode_a_result(_result(content=[]), state))
    rendered = json.dumps(out)
    assert out["status"] == "error"
    assert out["error_type"] == "incomplete"
    assert out["tool_results"][0]["app_status"] == "error"
    assert out["tool_results"][0]["safe_error_category"] == category
    for forbidden in ("Authorization", "Bearer secret", "password=", "private", "ResourceNotFound"):
        assert forbidden not in rendered


def test_sql_rows_and_sensitive_fields_are_reduced_to_allowlisted_counts():
    payload = {
        "columns": ["email", "external_id", "session_token"],
        "rows": [["person@example.com", "ext-private", "AKIAIOSFODNN7EXAMPLE"]],
        "row_count": 1,
        "password": "hunter2",
        "Authorization": "Bearer private",
        "phone": "+1-206-555-0199",
        "ssn": "123-45-6789",
    }
    capture = sa._InvocationToolResultCapture(max_results=4, max_chars=256)
    state = {}
    capture._on_invocation_start(SimpleNamespace(invocation_state=state))
    _capture_event(capture, state, {"status": "success", "content": [{"json": payload}]})
    out = json.loads(sa._extract_mode_a_result(_result(content=[]), state))
    rendered = json.dumps(out)
    assert out["status"] == "partial"
    assert out["tool_results"][0]["row_count"] == 1
    assert "content" not in out["tool_results"][0]
    for forbidden in (
        "person@example.com", "ext-private", "AKIA", "hunter2", "Bearer private",
        "+1-206-555-0199", "123-45-6789", "session_token", "external_id",
    ):
        assert forbidden not in rendered


def test_mixed_sitewise_result_is_partial_with_counts_not_values():
    payload = {
        "asset-a_prop-a": {"value": 42.5, "timestamp": 1, "quality": "GOOD"},
        "asset-b_prop-b": {"error": "access denied for person@example.com"},
    }
    capture = sa._InvocationToolResultCapture(max_results=4, max_chars=256)
    state = {}
    capture._on_invocation_start(SimpleNamespace(invocation_state=state))
    _capture_event(capture, state, {"status": "success", "content": [{"json": payload}]})
    out = json.loads(sa._extract_mode_a_result(_result(content=[]), state))
    meta = out["tool_results"][0]
    assert out["status"] == "partial"
    assert meta["app_status"] == "partial"
    assert meta["item_count"] == 2
    assert meta["success_count"] == 1
    assert meta["error_count"] == 1
    assert "42.5" not in json.dumps(out)
    assert "person@example.com" not in json.dumps(out)


def test_capture_prefers_most_recent_safe_successes_at_cap():
    capture = sa._InvocationToolResultCapture(max_results=2, max_chars=128)
    state = {}
    capture._on_invocation_start(SimpleNamespace(invocation_state=state))
    for count in range(1, 6):
        _capture_event(
            capture,
            state,
            {"status": "success", "content": [{"json": {"rows": [[]] * count, "row_count": count}}]},
        )
    out = json.loads(sa._extract_mode_a_result(_result(content=[]), state))
    assert [item["row_count"] for item in out["tool_results"]] == [4, 5]


def test_twenty_megabytes_of_arbitrary_tool_text_never_leaks_and_output_is_bounded():
    secret_block = (
        "Authorization: Bearer token password=hunter2 external_id=private "  # nosec B105 - not a credential: field/env/secret NAME, sentinel, or offline test fixture
        "AKIAIOSFODNN7EXAMPLE session_token=private person@example.com "
        "+1-206-555-0199 123-45-6789 "
    )
    huge = (secret_block + "x" * 1000) * 900  # approximately 1 MiB
    result = {"status": "success", "content": [{"text": huge} for _ in range(20)]}
    capture = sa._InvocationToolResultCapture(max_results=4, max_chars=128)
    state = {}
    capture._on_invocation_start(SimpleNamespace(invocation_state=state))
    _capture_event(capture, state, result)
    rendered = sa._extract_mode_a_result(_result(content=[]), state)
    assert len(rendered) < 2048
    assert json.loads(rendered)["status"] == "error"
    for forbidden in ("Authorization", "hunter2", "external_id", "AKIA", "session_token",
                      "person@example.com", "+1-206-555-0199", "123-45-6789"):
        assert forbidden not in rendered


def test_arbitrary_transport_status_text_is_not_reflected():
    capture = sa._InvocationToolResultCapture(max_results=1, max_chars=128)
    state = {}
    capture._on_invocation_start(SimpleNamespace(invocation_state=state))
    _capture_event(capture, state, {
        "status": "Authorization-Bearer-private",
        "content": [{"json": {"row_count": 1, "rows": [["secret"]]}}],
    })
    rendered = sa._extract_mode_a_result(_result(content=[]), state)
    assert "Authorization" not in rendered
    assert "private" not in rendered


def test_sitewise_partial_envelope_remains_partial_not_complete_success():
    capture = sa._InvocationToolResultCapture(max_results=1, max_chars=256)
    state = {}
    capture._on_invocation_start(SimpleNamespace(invocation_state=state))
    _capture_event(capture, state, {"status": "success", "content": [{"json": {
        "a_p": {"status": "partial", "truncated": True,
                "reason": "page_limit", "points": [{"value": "private"}]},
    }}]})
    out = json.loads(sa._extract_mode_a_result(_result(content=[]), state))
    assert out["status"] == "partial"
    assert out["tool_results"][0]["app_status"] == "partial"
    assert "private" not in json.dumps(out)


def test_list_shaped_all_application_errors_are_classified_as_error():
    payload = [
        {"error": "secret one", "error_type": "invalid_request"},
        {"status": "failed", "message": "secret two"},
    ]
    capture = sa._InvocationToolResultCapture(max_results=1, max_chars=256)
    state = {}
    capture._on_invocation_start(SimpleNamespace(invocation_state=state))
    _capture_event(capture, state, {"status": "success", "content": [{"json": payload}]})
    out = json.loads(sa._extract_mode_a_result(_result(content=[]), state))
    assert out["status"] == "error"
    meta = out["tool_results"][0]
    assert meta["app_status"] == "error"
    assert meta["error_count"] == 2
    assert "secret" not in json.dumps(out)


def _capture_list_metadata(payload):
    capture = sa._InvocationToolResultCapture(max_results=1, max_chars=256)
    state = {}
    capture._on_invocation_start(SimpleNamespace(invocation_state=state))
    _capture_event(capture, state, {"status": "success", "content": [{"json": payload}]})
    return json.loads(sa._extract_mode_a_result(_result(content=[]), state))


def test_list_shaped_mixed_success_and_error_is_partial():
    out = _capture_list_metadata([
        {"error": "private failure"},
        {"value": "private data"},
    ])
    meta = out["tool_results"][0]
    assert out["status"] == "partial"
    assert meta["app_status"] == "partial"
    assert meta["success_count"] == 1
    assert meta["error_count"] == 1
    assert "private" not in json.dumps(out)


def test_list_shaped_data_is_success_metadata_only():
    out = _capture_list_metadata([{"value": "private"}, ["also private"], 42])
    meta = out["tool_results"][0]
    assert out["status"] == "partial"  # successful tool work, but no final agent text
    assert meta["app_status"] == "success"
    assert meta["item_count"] == 3
    assert meta["success_count"] == 3
    assert "private" not in json.dumps(out)


def test_large_list_inspects_only_bounded_prefix_and_never_serializes_items():
    class _ExplodingItem(dict):
        def get(self, *_args, **_kwargs):
            raise AssertionError("unbounded list item was inspected")

    payload = [{"value": "private"} for _ in range(256)] + [_ExplodingItem() for _ in range(1000)]
    out = _capture_list_metadata(payload)
    rendered = json.dumps(out)
    meta = out["tool_results"][0]
    assert meta["app_status"] == "success"
    assert meta["item_count"] == 1256
    assert meta["success_count"] == 256
    assert meta["truncated"] is True
    assert len(rendered) < 2048
    assert "private" not in rendered
