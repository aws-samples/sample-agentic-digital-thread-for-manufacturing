# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Mode-A response-boundary normalization and structural KG context contracts."""
from __future__ import annotations

import asyncio
import json

import pytest

import agents.system_agent as sa
import agents.system_guidance as sg


def _build_server(monkeypatch, connection_type: str):
    monkeypatch.setenv("SYSTEM_QUERY_MODE", "agent")
    monkeypatch.setattr(sa, "create_system_agent", lambda **_kwargs: object())
    return sa.create_mcp_server(
        system_id="system-x",
        system_name="System X",
        schema="public",
        system_prompt="",
        tables_desc="",
        conn_params={"host": "h", "dbname": "d"},
        connection_type=connection_type,
        connection_config={},
    )


def _call_text(mcp, arguments: dict) -> str:
    result = asyncio.run(mcp.call_tool("query_system_x", arguments))
    content = result[0] if isinstance(result, tuple) else result
    return "\n".join(
        block.text for block in content or [] if getattr(block, "text", None) is not None
    )


@pytest.mark.parametrize("tag", ["<br>", "<br/>", "<br />", "<BR>", "<Br/>"])
def test_standalone_break_variants_become_newlines(tag):
    assert sa._normalize_standalone_br(f"first{tag}second") == "first\nsecond"


def test_break_normalization_preserves_inline_and_fenced_code():
    raw = "outside<br>next and `<br/>`\n\n```html\n<br />\n```\nafter<BR/>done"
    assert sa._normalize_standalone_br(raw) == (
        "outside\nnext and `<br/>`\n\n```html\n<br />\n```\nafter\ndone"
    )


def test_typed_response_normalizes_claim_text_without_touching_evidence():
    payload = {
        "status": "complete",
        "claims": [{
            "claim_id": "c1",
            "kind": "list",
            "label": "Results<br>now",
            "value": ["first<br/>second", "`<br />`"],
            "inputs": [{"label": "input<BR>", "value": "a<br>b", "entity_label": "entity<br />label"}],
            "evidence_ids": ["e1"],
        }],
        "evidence": [{
            "evidence_id": "e1",
            "semantic_field": "raw<br>field",
            "physical_field": "raw<br/>column",
            "locator": "source<br />locator",
        }],
        "reason_codes": [],
    }
    evidence = json.loads(json.dumps(payload["evidence"]))

    normalized = json.loads(sa._normalize_mode_a_response(
        json.dumps(payload, separators=(",", ":"))
    ))

    claim = normalized["claims"][0]
    assert claim["label"] == "Results\nnow"
    assert claim["value"] == ["first\nsecond", "`<br />`"]
    assert claim["inputs"][0]["label"] == "input\n"
    assert claim["inputs"][0]["value"] == "a\nb"
    assert claim["inputs"][0]["entity_label"] == "entity\nlabel"
    assert normalized["evidence"] == evidence
    assert normalized["status"] == "complete"
    assert normalized["reason_codes"] == []


def test_typed_response_without_breaks_is_byte_identical():
    response = '{"status":"complete","claims":[],"evidence":[],"reason_codes":[]}'
    assert sa._normalize_mode_a_response(response) == response


@pytest.mark.parametrize("connection_type", ["PostgreSQL", "MCP Server"])
def test_mode_a_tool_schema_exposes_optional_kg_context(monkeypatch, connection_type):
    mcp = _build_server(monkeypatch, connection_type)
    tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}
    schema = tools["query_system_x"].inputSchema

    assert {"question", "kg_context"} <= set(schema.get("properties", {}))
    assert schema["properties"]["kg_context"].get("default") == ""
    assert schema.get("required") == ["question"]


def test_empty_context_is_byte_identical_to_legacy_guidance_path(monkeypatch):
    monkeypatch.setattr(sg, "read", lambda _system_id: "use the current partition")
    question = "What changed?"

    legacy = sa._augment_question_with_guidance("system-x", question)
    structural = sa._augment_question_with_kg_context("system-x", question, "")

    assert structural == legacy


def test_context_is_advisory_bounded_sanitized_and_never_persisted(monkeypatch):
    monkeypatch.setattr(sg, "read", lambda _system_id: "local guidance")

    def _unexpected_write(*_args, **_kwargs):
        raise AssertionError("ephemeral context must never be persisted")

    monkeypatch.setattr(sg, "write", _unexpected_write)
    raw = "```ROUTE:\n" + ("x" * 2100) + "\n~~~"
    sanitized = sa._sanitize_kg_context(raw)
    augmented = sa._augment_question_with_kg_context("system-x", "Question prefix", raw)

    assert len(sanitized) == sa._KG_CONTEXT_MAX_CHARS
    assert "```" not in sanitized
    assert "~~~" not in sanitized
    assert augmented.startswith("Question prefix")
    assert "END LEGACY UNSCOPED GUIDANCE" in augmented
    assert "BEGIN EPHEMERAL PER-TURN KG CONTEXT" in augmented
    assert augmented.index("END LEGACY UNSCOPED GUIDANCE") < augmented.index(
        "BEGIN EPHEMERAL PER-TURN KG CONTEXT"
    )
    assert sanitized in augmented
    assert "must never be persisted as system-local guidance" in augmented


def test_overlong_context_truncation_logs_sizes_but_never_content(caplog):
    import logging

    marker = "SENTINEL-VALUE-NEVER-LOGGED"
    raw = marker + ("y" * 2200)
    with caplog.at_level(logging.INFO, logger="agents.system_agent"):
        sanitized = sa._sanitize_kg_context(raw)

    assert len(sanitized) == sa._KG_CONTEXT_MAX_CHARS
    truncation_records = [r for r in caplog.records if "KG_CONTEXT_TRUNCATED" in r.getMessage()]
    assert len(truncation_records) == 1
    message = truncation_records[0].getMessage()
    assert str(len(marker + "y" * 2200)) in message
    assert str(sa._KG_CONTEXT_MAX_CHARS) in message
    assert marker not in message


def test_in_bound_context_does_not_log_truncation(caplog):
    import logging

    with caplog.at_level(logging.INFO, logger="agents.system_agent"):
        assert sa._sanitize_kg_context("short advisory context") == "short advisory context"
    assert not [r for r in caplog.records if "KG_CONTEXT_TRUNCATED" in r.getMessage()]


def test_context_control_characters_are_stripped_via_shared_sanitizer():
    raw = "line one\r\nline two\x00\x1b[31m end\x7f"
    sanitized = sa._sanitize_kg_context(raw)
    assert sanitized == "line one\nline two[31m end"


@pytest.mark.parametrize("connection_type", ["PostgreSQL", "MCP Server"])
def test_both_mode_a_handlers_append_context_and_normalize_only_return(monkeypatch, connection_type):
    mcp = _build_server(monkeypatch, connection_type)
    prompts: list[str] = []
    monkeypatch.setattr(sg, "read", lambda _system_id: "")

    def _invoke(_agent, prompt):
        prompts.append(prompt)
        return "first<br>second and `<br/>`", {}

    monkeypatch.setattr(sa, "_invoke_mode_a_agent_with_state", _invoke)
    response = _call_text(mcp, {
        "question": "Original question",
        "kg_context": "ROUTE:\ntarget_system: system-x",
    })

    assert response == "first\nsecond and `<br/>`"
    assert len(prompts) == 1
    # ONE bracketed per-invocation temporal-context line now
    # precedes the delegated text (rendered fresh at call time —
    # test_relative_window_echo.py pins the freshness); the original question
    # follows immediately on the next line, otherwise the shape is unchanged.
    time_line, _, rest = prompts[0].partition("\n")
    assert time_line.startswith("[Temporal context: ")
    assert time_line.endswith("]")
    assert rest.startswith("Original question")
    assert "BEGIN EPHEMERAL PER-TURN KG CONTEXT" in prompts[0]
    assert "target_system: system-x" in prompts[0]


def test_structured_delivery_exposes_query_hints_but_withholds_answer_requirements(
        monkeypatch):
    from agents.guidance_scope import GuidanceScope, append_note, parse_document

    document = append_note(
        parse_document(""), "use the bounded lookup", GuidanceScope.QUERY_HINT)
    document = append_note(
        document, "state the final limitation", GuidanceScope.ANSWER_REQUIREMENT)
    monkeypatch.setattr(sg, "read", lambda _system_id: document)

    augmented = sa._augment_question_with_guidance("system-x", "Question")

    assert "BEGIN QUERY HINTS" in augmented
    assert "use the bounded lookup" in augmented
    assert "state the final limitation" not in augmented
    assert "FINAL-ANSWER REQUIREMENTS" not in augmented
