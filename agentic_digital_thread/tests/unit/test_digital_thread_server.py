# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit tests for the `digital-thread` MCP tool server (Wave 1 T1).

Neptune Analytics is VPC-private (ADR-0024) and unreachable from the test host, so
`graph_tools.neptune_query` is mocked. These tests assert:
  - the server exposes EXACTLY the two graph tools, with the canonical docstrings as
    descriptions (the semantic-search ranking text on the gateway must not drift);
  - the shared tool bodies (single source of truth in graph_tools) return the
    expected JSON shapes;
  - the tool names match what the orchestrator/prompt reference, so the
    gateway-prefixed names (`digital-thread___<tool>`) are predictable.
"""

import asyncio
import json
import logging
import threading
from unittest.mock import patch

import pytest

from agents import graph_tools
from agents import digital_thread_server as dt_server
from agents.digital_thread_server import create_digital_thread_server, TARGET_NAME


def setup_function():
    dt_server._reset_value_mapping_burst_state()
    dt_server._reset_client_instructions_cache()


def _list_tools(mcp):
    """Enumerate the server's registered MCP tools (list_tools is async)."""
    return asyncio.run(mcp.list_tools())


def _tool_result_text(result) -> str:
    """Extract the tool's returned text from FastMCP.call_tool's return value across
    versions (it returns a (content, structured) tuple; older ones a content list)."""
    content = result[0] if isinstance(result, tuple) else result
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        text = getattr(block, "text", None)
        if text is not None:
            parts.append(text)
    return "\n".join(parts)


def test_target_name_is_digital_thread():
    # Drives the gateway tool prefix: digital-thread___<tool>.
    assert TARGET_NAME == "digital-thread"


def test_server_exposes_the_graph_and_writeback_tools():
    mcp = create_digital_thread_server()
    names = {t.name for t in _list_tools(mcp)}
    assert names == {
        "explore_graph",
        "get_system_schema",
        "save_value_mapping",
        "save_correlation",
        "get_query_workflow",
        "get_client_instructions",
        "verify_relationship",
    }


def test_read_tools_carry_closed_world_read_only_annotations():
    tools = {t.name: t for t in _list_tools(create_digital_thread_server())}
    for name in (
        "explore_graph",
        "get_system_schema",
        "get_query_workflow",
        "get_client_instructions",
    ):
        payload = tools[name].model_dump(by_alias=True, exclude_none=True)
        assert payload["annotations"] == {
            "readOnlyHint": True,
            "openWorldHint": False,
        }


def test_write_tools_do_not_carry_read_only_annotations():
    tools = {t.name: t for t in _list_tools(create_digital_thread_server())}
    for name in ("save_value_mapping", "save_correlation"):
        payload = tools[name].model_dump(by_alias=True, exclude_none=True)
        assert "annotations" not in payload


def test_query_workflow_tool_is_a_meta_guidance_tool_not_a_query_tool():
    """`get_query_workflow` must be discoverable as GUIDANCE only — a meta name and a
    description that says it executes nothing — so it cannot be confused with or shadow
    the real query tools (explore_graph / get_system_schema / run_query / save_*)."""
    tools = {t.name: t for t in _list_tools(create_digital_thread_server())}
    desc = tools["get_query_workflow"].description or ""
    assert "guidance only" in desc.lower()
    assert "executes nothing" in desc.lower()
    assert "call this first" in desc.lower()
    # The meta name is distinct from every real tool name.
    assert "get_query_workflow" not in {
        "explore_graph", "get_system_schema", "run_query",
        "save_value_mapping", "save_correlation",
    }


def test_client_instructions_tool_is_setup_only_and_executes_nothing():
    """`get_client_instructions` must be discoverable as a SETUP-time instructions
    fetch — not a query tool and not a per-conversation call: its description says
    to call it once during integration setup, never during normal conversations,
    and that it executes nothing."""
    tools = {t.name: t for t in _list_tools(create_digital_thread_server())}
    desc = tools["get_client_instructions"].description or ""
    lowered = desc.lower()
    assert "once during integration setup" in lowered
    assert "do not call it during normal query conversations" in lowered
    assert "executes nothing" in lowered
    assert "read-only" in lowered
    # It advertises WHAT it returns: the complete mode-matched client setup prompt.
    assert "complete system prompt" in lowered
    assert "query mode" in lowered


def test_guidance_tool_names_cover_both_guidance_tools():
    # The orchestrator exclusion seam keys off this set; the singular alias stays
    # importable for pre-plural callers.
    assert dt_server.GUIDANCE_TOOL_NAMES == frozenset({
        "get_query_workflow",
        "get_client_instructions",
    })
    assert dt_server.GUIDANCE_TOOL_NAME == "get_query_workflow"
    assert dt_server.GUIDANCE_TOOL_NAME in dt_server.GUIDANCE_TOOL_NAMES


# --- deployed-mode awareness (SYSTEM_QUERY_MODE) ------------------------------

def test_get_query_workflow_is_mode_matched(monkeypatch):
    import agents.orchestrator as orch
    from agents.workflow_guidance import MODE_B_WORKFLOW_GUIDANCE

    monkeypatch.setenv("SYSTEM_QUERY_MODE", "orchestrator")
    assert dt_server.get_query_workflow() == MODE_B_WORKFLOW_GUIDANCE

    monkeypatch.delenv("SYSTEM_QUERY_MODE", raising=False)
    assert dt_server.get_query_workflow() == orch._MODE_A_DELEGATE_BLOCK


def test_get_client_instructions_is_mode_matched(monkeypatch):
    from agents.workflow_guidance import compose_quick_system_prompt

    monkeypatch.setenv("SYSTEM_QUERY_MODE", "orchestrator")
    dt_server._reset_client_instructions_cache()
    assert dt_server.get_client_instructions() == compose_quick_system_prompt("orchestrator")

    monkeypatch.delenv("SYSTEM_QUERY_MODE", raising=False)
    dt_server._reset_client_instructions_cache()
    assert dt_server.get_client_instructions() == compose_quick_system_prompt("agent")


def test_get_client_instructions_memoizes_per_resolved_mode(monkeypatch):
    import agents.workflow_guidance as wg

    calls = []
    real_compose = wg.compose_quick_system_prompt

    def counting_compose(mode):
        calls.append(mode)
        return real_compose(mode)

    monkeypatch.setattr(wg, "compose_quick_system_prompt", counting_compose)
    monkeypatch.delenv("SYSTEM_QUERY_MODE", raising=False)
    dt_server._reset_client_instructions_cache()

    first = dt_server.get_client_instructions()
    second = dt_server.get_client_instructions()

    assert first == second
    assert calls == ["agent"]  # composed exactly once; the repeat hit the memo

    # A different resolved mode composes once more (per-mode memo, not global).
    monkeypatch.setenv("SYSTEM_QUERY_MODE", "orchestrator")
    dt_server.get_client_instructions()
    dt_server.get_client_instructions()
    assert calls == ["agent", "orchestrator"]


def test_client_instructions_carry_no_secret_shaped_strings(monkeypatch):
    """F14: the composed setup prompt for BOTH deployed modes must never carry a
    secret-shaped string — reuses the repo's pinned secret-shape regexes
    (``agents.untrusted_text.SECRET_SHAPE_PATTERNS``, ADR-0054 D4)."""
    from agents.untrusted_text import SECRET_SHAPE_PATTERNS

    for env_value, label in ((None, "agent"), ("orchestrator", "orchestrator")):
        if env_value is None:
            monkeypatch.delenv("SYSTEM_QUERY_MODE", raising=False)
        else:
            monkeypatch.setenv("SYSTEM_QUERY_MODE", env_value)
        dt_server._reset_client_instructions_cache()
        text = dt_server.get_client_instructions()
        assert text  # non-empty prompt composed
        for pattern in SECRET_SHAPE_PATTERNS:
            match = pattern.search(text)
            assert match is None, (
                f"secret-shaped string in {label}-mode client instructions: "
                f"{match.group(0)[:60]!r} (pattern {pattern.pattern!r})"
            )


def test_warm_guidance_failure_logs_digit_masked_traceback(monkeypatch, caplog):
    """F10: the orchestrator runtime's OTel handler DISCARDS logger.exception's
    exc_info, so the traceback must be formatted into the MESSAGE —
    digit-masked so identifiers cannot leak through log text."""
    def _boom():
        raise RuntimeError("guidance warm exploded 12345")

    monkeypatch.setattr(dt_server, "get_query_workflow", _boom)
    with caplog.at_level(logging.ERROR, logger="agents.digital_thread_server"):
        with pytest.raises(RuntimeError):
            dt_server._warm_guidance_texts()
    message = caplog.records[-1].getMessage()
    assert "Traceback" in message      # the cause is inline in the message
    assert "#####" in message          # 12345 → ##### (digits masked)
    assert "12345" not in message


def test_registered_query_workflow_round_trips_mode_a(monkeypatch):
    """Round-trip the registered tool via mcp.call_tool on a Mode-A deployment."""
    import agents.orchestrator as orch

    monkeypatch.delenv("SYSTEM_QUERY_MODE", raising=False)
    mcp = create_digital_thread_server()
    result = asyncio.run(mcp.call_tool("get_query_workflow", {}))
    assert orch._MODE_A_DELEGATE_BLOCK in _tool_result_text(result)


def test_registered_client_instructions_round_trips_mode_b(monkeypatch):
    """Round-trip the registered tool via mcp.call_tool on a Mode-B deployment."""
    from agents.workflow_guidance import compose_quick_system_prompt

    monkeypatch.setenv("SYSTEM_QUERY_MODE", "orchestrator")
    dt_server._reset_client_instructions_cache()
    mcp = create_digital_thread_server()
    result = asyncio.run(mcp.call_tool("get_client_instructions", {}))
    assert compose_quick_system_prompt("orchestrator") in _tool_result_text(result)


def test_tool_descriptions_carry_the_canonical_docstrings():
    tools = {t.name: t for t in _list_tools(create_digital_thread_server())}
    qng_desc = tools["explore_graph"].description or ""
    schema_desc = tools["get_system_schema"].description or ""
    # The docstrings ARE the tool descriptions (single source of truth + the text
    # semantic search ranks on). Spot-check distinctive content from each.
    assert "openCypher" in qng_desc and "GRAPH SCHEMA" in qng_desc
    assert "exact table and column names" in schema_desc
    assert "ALWAYS call this before querying" in schema_desc


def test_tool_names_match_canonical_function_names():
    # The server registers fn.__name__; the orchestrator wraps the same functions.
    # Keeping these stable is what makes the gateway-prefixed names predictable.
    assert graph_tools.explore_graph.__name__ == "explore_graph"
    assert graph_tools.get_system_schema.__name__ == "get_system_schema"


def test_explore_graph_body_wraps_neptune_query_result():
    with patch.object(graph_tools, "neptune_query_checked", return_value=[{"x": 1}]) as m:
        out = json.loads(graph_tools.explore_graph("MATCH (s:System) RETURN s"))
    assert out == {"results": [{"x": 1}]}
    m.assert_called_once()


def test_get_system_schema_groups_fields_and_concepts_by_table():
    rows = [
        {"table_name": "work_orders", "field_name": "wo_id", "concept": "work-order",
         "role": "id", "disambiguation": None},
        {"table_name": "work_orders", "field_name": "status", "concept": None,
         "role": None, "disambiguation": "enum: open/closed"},
    ]
    with patch.object(graph_tools, "neptune_query", return_value=rows):
        out = json.loads(graph_tools.get_system_schema("mes-mil"))
    assert out["work_orders"]["columns"] == ["wo_id", "status"]
    assert out["work_orders"]["concept_map"]["wo_id"] == {"concept": "work-order", "role": "id"}
    # disambiguation-only field still gets a concept_map entry carrying the note.
    assert out["work_orders"]["concept_map"]["status"]["note"] == "enum: open/closed"


def test_get_system_schema_reports_missing_system():
    with patch.object(graph_tools, "neptune_query", return_value=[]):
        out = json.loads(graph_tools.get_system_schema("nope"))
    assert "error" in out and "nope" in out["error"]


# --- write-back tools (T2) ---------------------------------------------------

def test_save_value_mapping_logs_pending_proposal_with_mapped_confidence():
    with patch.object(dt_server.self_learning, "log_proposed_mapping", return_value="m-123") as m:
        out = json.loads(dt_server.save_value_mapping(
            from_system="cmms", from_field="assets.serial_number", from_value="KF2000-MKE-01",
            to_system="sitewise", to_field="asset_id", to_value="uuid-abc",
            method="hierarchy_position", confidence="high", reason="matched by site + model",
        ))
    assert out == {"status": "logged", "mapping_id": "m-123", "type": "value_mapping"}
    kwargs = m.call_args.kwargs
    assert kwargs["proposed_type"] == "value_mapping"
    assert kwargs["source"] == "agent"          # agent-sourced → pending, cannot clobber user rules
    assert kwargs["confidence"] == 0.9           # "high" → 0.9
    assert kwargs["from_value"] == "KF2000-MKE-01" and kwargs["to_value"] == "uuid-abc"
    # ADR audit P12: method lands in the TYPED field; the [method=...] prefix
    # never contaminates query_context (the pilot-env f8fef055 defect family).
    assert kwargs["method"] == "hierarchy_position"
    assert kwargs["query_context"] == "matched by site + model"
    assert "[method=" not in kwargs["query_context"]


def test_save_value_mapping_rejects_empty_required_endpoints():
    base = {
        "from_system": "sys-a", "from_field": "records.id", "from_value": "A",
        "to_system": "sys-b", "to_field": "entities.id", "to_value": "B",
    }
    with patch.object(dt_server.self_learning, "log_proposed_mapping") as log:
        results = []
        for field in (
            "from_system", "from_field", "from_value",
            "to_system", "to_field", "to_value",
        ):
            payload = dict(base)
            payload[field] = " "
            results.append(json.loads(dt_server.save_value_mapping(**payload)))
    assert all(result["status"] == "rejected" for result in results)
    assert all("non-empty" in result["reason"] for result in results)
    log.assert_not_called()


def test_save_value_mapping_reports_gate_drop_and_does_not_spend_burst_slot():
    with patch.object(dt_server.self_learning, "log_proposed_mapping",
                      side_effect=[None] * 5 + [f"m-{i}" for i in range(10)]) as log:
        dropped = [json.loads(dt_server.save_value_mapping(
            from_system="sys-a", from_field="records.external_id", from_value=f"drop-{i}",
            to_system="sys-b", to_field="entities.canonical_id", to_value=f"ignored-{i}",
        )) for i in range(5)]
        logged = [json.loads(dt_server.save_value_mapping(
            from_system="sys-a", from_field="records.external_id", from_value=f"A-{i}",
            to_system="sys-b", to_field="entities.canonical_id", to_value=f"B-{i}",
        )) for i in range(10)]
        rejected = json.loads(dt_server.save_value_mapping(
            from_system="sys-a", from_field="records.external_id", from_value="A-10",
            to_system="sys-b", to_field="entities.canonical_id", to_value="B-10",
        ))

    assert all(result == {
        "status": "dropped",
        "reason": "not stored — filtered by learning gates",
    } for result in dropped)
    assert all(result["status"] == "logged" and result["mapping_id"]
               for result in logged)
    assert rejected["status"] == "rejected"
    assert log.call_count == 15


def test_save_value_mapping_rejects_eleventh_field_pair_write_in_window():
    with patch.object(dt_server.self_learning, "log_proposed_mapping",
                      side_effect=lambda **kwargs: f"m-{kwargs['from_value']}") as log:
        results = [json.loads(dt_server.save_value_mapping(
            from_system="sys-a", from_field="records.external_id", from_value=f"A-{i}",
            to_system="sys-b", to_field="entities.canonical_id", to_value=f"B-{i}",
        )) for i in range(11)]
    assert all(result["status"] == "logged" for result in results[:10])
    assert results[10]["status"] == "rejected"
    assert "aggregate" in results[10]["reason"]
    assert log.call_count == 10


def test_save_value_mapping_threaded_burst_reserves_before_persistence():
    worker_count = dt_server._VALUE_MAPPING_BURST_MAX + 1
    start = threading.Barrier(worker_count)
    persistence = threading.Barrier(worker_count)
    writes: list[str] = []
    results: list[dict] = []
    results_lock = threading.Lock()

    def log_mapping(**kwargs):
        # The broken check→write→charge implementation lets all workers reach
        # persistence. The fixed reserve→write path admits at most the cap, so
        # its admitted workers time out together and continue.
        try:
            persistence.wait(timeout=0.5)
        except threading.BrokenBarrierError:
            pass
        writes.append(kwargs["from_value"])
        return f"m-{kwargs['from_value']}"

    def worker(index: int) -> None:
        start.wait()
        result = json.loads(dt_server.save_value_mapping(
            from_system="sys-a", from_field="records.external_id",
            from_value=f"A-{index}", to_system="sys-b",
            to_field="entities.canonical_id", to_value=f"B-{index}",
        ))
        with results_lock:
            results.append(result)

    with patch.object(dt_server.self_learning, "log_proposed_mapping",
                      side_effect=log_mapping):
        threads = [threading.Thread(target=worker, args=(index,))
                   for index in range(worker_count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)

    assert all(not thread.is_alive() for thread in threads)
    assert len(writes) <= dt_server._VALUE_MAPPING_BURST_MAX
    assert sum(result["status"] == "logged" for result in results) <= dt_server._VALUE_MAPPING_BURST_MAX
    assert sum(result["status"] == "rejected" for result in results) == 1


def test_save_value_mapping_defaults_unknown_confidence_to_medium_float():
    with patch.object(dt_server.self_learning, "log_proposed_mapping", return_value="m-1") as m:
        dt_server.save_value_mapping(
            from_system="erp", from_field="o.order_number", from_value="O-1",
            to_system="mes-mil", to_field="w.work_order_id", to_value="WO-1",
            confidence="bogus",
        )
    assert m.call_args.kwargs["confidence"] == 0.7  # unknown → 0.7


def test_save_correlation_logs_insight_for_two_systems():
    with patch.object(dt_server.self_learning, "log_proposed_mapping", return_value="c-9") as m:
        out = json.loads(dt_server.save_correlation(
            pattern="downtime in CMMS correlates with quality drops in MES",
            observed="3 of 4 downtime events preceded a pass-rate dip",
            confidence="medium", systems=["cmms", "mes-mil"],
        ))
    assert out["status"] == "logged" and out["type"] == "insight"
    kwargs = m.call_args.kwargs
    assert kwargs["proposed_type"] == "insight"
    assert kwargs["from_system"] == "cmms" and kwargs["to_system"] == "mes-mil"
    assert kwargs["concept"] == "correlation"


def test_save_correlation_skips_when_fewer_than_two_systems():
    with patch.object(dt_server.self_learning, "log_proposed_mapping") as m:
        out = json.loads(dt_server.save_correlation(pattern="x", systems=["cmms"]))
    assert out["status"] == "skipped"
    m.assert_not_called()  # never write a one-sided correlation
