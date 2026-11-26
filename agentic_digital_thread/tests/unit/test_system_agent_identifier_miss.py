# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ADR-0048 — system-agent IDENTIFIER-MISS marker contract (Task 1).

Fully offline (no Bedrock, no AWS, no DB, no sockets). Pins:

1. The IDENTIFIER-MISS protocol text is present in EVERY connection type's
   system prompt (report-only default), with the exact marker field names the
   orchestrator recovery keys on (``token`` / ``looked_in`` / ``my_format``).
2. ADR-0047 layering is preserved: report-only = ``base + MISS`` (no typed
   segment); enforce = ``base + MISS + TYPED`` — the enforce prompt still
   STARTS WITH the report-only prompt (the existing startswith pin).
3. Marker survival through the legacy answer path
   (``_normalize_mode_a_response``): the fenced block is byte-intact.
4. Marker survival through the annotate gate
   (``response_integrity_annotate.annotate_answer``): a miss answer emitted
   after real queries ran (payload seen) passes through un-stripped.
"""
import json

import pytest

import agents.system_agent as sa
from agents.connection_types import TULIP
from agents.response_integrity import capture_trusted_tool_result
from agents.response_integrity_annotate import annotate_answer
from agents.response_integrity_capture import initialize_integrity_state


# --------------------------------------------------------------------------- #
# Shared stubs (pattern: test_system_agent_self_recovery.py)
# --------------------------------------------------------------------------- #
class _FakeModel:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs


class _FakeAgent:
    """Captures what create_system_agent built without running an LLM."""

    def __init__(self, *args, **kwargs):
        self.system_prompt = kwargs.get("system_prompt", "")
        self.tools = kwargs.get("tools") or []


# Minimal, valid connection_config per type so create_system_agent builds its
# real internal toolset WITHOUT any network/secret lookup at construction time.
_CFG_BY_TYPE = {
    "PostgreSQL": {"host": "h", "dbname": "d"},
    "Athena": {"workgroup": "wg", "database": "db", "region": "us-east-1"},
    "SiteWise (Direct)": {},
    "SiteWise (i3X)": {"base_url": "http://i3x.example.com"},
    # A bare generic "REST API" system has NO
    # runtime toolset (RestVendorUnsupported) and cannot build an agent. Tulip
    # is now its own first-class type and is the representative REST-family
    # system that HAS tools — use it so per-type prompt coverage still exercises
    # the REST-family agent path.
    TULIP: {"base_url": "https://your-instance.tulip.co", "auth_scheme": "basic"},
    "MCP Server": {"url": "http://mcp.example.com"},
}


def _make_agent(monkeypatch, connection_type):
    monkeypatch.setattr(sa, "Agent", _FakeAgent)
    monkeypatch.setattr(sa, "_mcp_system_tools", lambda _config, _sid="": [])
    monkeypatch.setattr("strands.models.bedrock.BedrockModel", _FakeModel)
    cfg = _CFG_BY_TYPE[connection_type]
    conn_params = cfg if connection_type == "PostgreSQL" else None
    return sa.create_system_agent(
        system_id="sys-a",
        system_name="System A",
        schema="public",
        system_prompt="Base system prompt.",
        conn_params=conn_params,
        connection_type=connection_type,
        connection_config=cfg,
    )


# The exact fragments the orchestrator-side recovery (Task 2) keys on.
_PINNED_FRAGMENTS = (
    "IDENTIFIER-MISS (required protocol)",
    "IDENTIFIER-MISS:",
    "token:",
    "looked_in:",
    "my_format:",
    "WRONG VOCABULARY",
    "VALID SHAPE, ZERO ROWS",
    "for a genuinely empty result",
    # A zero-row identifier must not be answered with a neighbouring record
    # (live: a missing batch "is actually" another line's batch of the same
    # product).
    "NEVER present a different record",
    "as separate, labelled context",
    # FINAL SHAPE CHECK gate (live false-positive fix: honest zero on a
    # shape-matching token — e.g. a valid UUID against a UUID column — must
    # NEVER carry the marker; answer and block are mutually exclusive).
    "FINAL CHECK",
    "shape MATCHES your format",
    "mutually exclusive",
)


# --------------------------------------------------------------------------- #
# 1. The protocol is in EVERY connection type's prompt (report-only default)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("connection_type", list(_CFG_BY_TYPE))
def test_prompt_carries_identifier_miss_for_every_type(monkeypatch, connection_type):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    prompt = _make_agent(monkeypatch, connection_type).system_prompt
    for fragment in _PINNED_FRAGMENTS:
        assert fragment in prompt, (connection_type, fragment)
    # Report-only default: no typed-evidence segment.
    assert "TYPED EVIDENCE OUTPUT" not in prompt, connection_type


# --------------------------------------------------------------------------- #
# 2. ADR-0047 layering preserved: base + MISS (+ TYPED under enforce)
# --------------------------------------------------------------------------- #
def _compose(**overrides):
    kwargs = dict(
        system_id="sys-a", system_name="System A", schema="public",
        system_prompt="", tables_desc="", connection_type="PostgreSQL",
    )
    kwargs.update(overrides)
    return sa._compose_system_prompt(**kwargs)


def test_report_only_prompt_has_miss_but_not_typed(monkeypatch):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    prompt = _compose()
    assert "IDENTIFIER-MISS (required protocol)" in prompt
    assert "TYPED EVIDENCE OUTPUT" not in prompt
    # MISS rides AFTER the unchanged legacy base prompt.
    base = sa._base_compose_system_prompt(
        "sys-a", "System A", "public", "", "", "PostgreSQL",
    )
    assert prompt == base + sa._identifier_miss_prompt()


def test_enforce_prompt_starts_with_report_only_prompt(monkeypatch):
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "enforce")
    enforce_prompt = _compose()
    assert "TYPED EVIDENCE OUTPUT" in enforce_prompt
    assert "IDENTIFIER-MISS (required protocol)" in enforce_prompt
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    report_only_prompt = _compose()
    # The ADR-0047 layering contract: enforce startswith report-only.
    assert enforce_prompt.startswith(report_only_prompt)
    assert enforce_prompt == report_only_prompt + sa._TYPED_EVIDENCE_PROMPT


def test_miss_text_defers_to_typed_contract_and_stays_generic():
    text = sa._identifier_miss_prompt()
    # Enforce-mode deferral sentence (the typed contract supersedes the marker).
    assert "typed-output contract" in text
    # Honest-zero rule: never marker a genuinely empty result, never invent data.
    assert "NEVER invent data" in text
    # No demo dataset literals (project rule).
    for banned in ("Milwaukee", "Detroit", "Cedar Rapids", "Indianapolis",
                   "MKE", "DET", "CRR"):
        assert banned not in text, banned


# --------------------------------------------------------------------------- #
# 3. Marker survival — legacy answer normalization path
# --------------------------------------------------------------------------- #
_MISS_BLOCK = (
    "```\n"
    "IDENTIFIER-MISS:\n"
    "token: ORD-77-Q4\n"
    "looked_in: work_orders.order_ref\n"
    "my_format: WO-<4 digits>, e.g. WO-1001, WO-1002\n"
    "```"
)


def test_normalize_mode_a_response_keeps_fenced_miss_block_byte_intact():
    answer = (
        f"{_MISS_BLOCK}\n"
        "That identifier does not match this system's vocabulary.<br/>"
        "It may be another system's name for the same thing."
    )
    out = sa._normalize_mode_a_response(answer)
    # The fenced block is byte-intact (fence lines bypass br-normalization)...
    assert _MISS_BLOCK in out
    # ...while normalization DID run on the surrounding prose (non-JSON path).
    assert "<br/>" not in out


def test_normalize_mode_a_response_is_identity_without_html_breaks():
    answer = f"{_MISS_BLOCK}\nNo match found in this system."
    assert sa._normalize_mode_a_response(answer) == answer


# --------------------------------------------------------------------------- #
# 4. Marker survival — annotate gate (Management runs annotate live)
# --------------------------------------------------------------------------- #
def _state_after_real_queries(question: str) -> dict:
    """Integrity state for an agent that DID query (payload seen) but found no
    match — the exact situation in which the miss marker is emitted."""
    invocation_state: dict = {}
    initialize_integrity_state(invocation_state, question)
    payload = {
        "columns": ["order_ref", "status"],
        "rows": [["WO-1001", "OPEN"], ["WO-1002", "CLOSED"]],
        "complete": True,
    }
    result = {"toolUseId": "t1", "status": "success",
              "content": [{"text": json.dumps(payload)}]}
    capture_trusted_tool_result(
        result, invocation_state, system_id="sys-a", boundary="sql",
        tool_name="run_query",
        executed_query="SELECT order_ref, status FROM work_orders",
        physical_schema={"work_orders": {"relation_kind": "base_table",
                                         "columns": {"order_ref": "varchar",
                                                     "status": "varchar"}}},
    )
    return invocation_state


def test_annotate_answer_passes_miss_marker_through_unstripped():
    question = "what is the status of order ORD-77-Q4?"
    state = _state_after_real_queries(question)
    answer = (
        f"{_MISS_BLOCK}\n"
        "That identifier does not match this system's order vocabulary."
    )
    gated, report = annotate_answer(answer, question, state)
    assert report["blocked"] is False
    assert gated == answer            # byte-intact pass-through
    assert _MISS_BLOCK in gated


# --------------------------------------------------------------------------- #
# 5. Deterministic false-positive guard (_strip_false_identifier_miss)
#
# Live-verified failure (twice, Haiku agent): the model emitted the fenced
# block for a nonexistent-but-valid-shape UUID whose my_format samples were
# UUIDs of the exact same shape — while writing "formatted as a valid UUID"
# in prose. Prompt-side FINAL CHECK did not reliably fix it, so the shape
# comparison is enforced post-hoc in code. Honest zeros NEVER carry the
# marker; fail-open on parse failure.
# --------------------------------------------------------------------------- #
_UUID_TOKEN = "3fa85f64-5717-4562-b3fc-2c963f66afa6"  # gitleaks:allow — intentional fake UUID fixture (identifier-miss test)  # nosec B105 - not a credential: field/env/secret NAME, sentinel, or offline test fixture
_UUID_SAMPLE_A = "9bc21d33-8010-4341-a7dc-11e2b8f3c9d1"
_UUID_SAMPLE_B = "550e8400-e29b-41d4-a716-446655440000"

_UUID_MISS_BLOCK = (
    "```\n"
    "IDENTIFIER-MISS:\n"
    f"token: {_UUID_TOKEN}\n"
    "looked_in: equipment.equipment_id\n"
    f"my_format: UUIDs, e.g. {_UUID_SAMPLE_A}, {_UUID_SAMPLE_B}\n"
    "```"
)

_PO_TOKEN_MISS_BLOCK = (
    "```\n"  # nosec B105 - not a credential: field/env/secret NAME, sentinel, or offline test fixture
    "IDENTIFIER-MISS:\n"
    "token: PO-20260708-76827\n"
    "looked_in: work_orders.wo_number\n"
    "my_format: WO-<date>-<5 digits>, e.g. WO-20260707-00317, WO-20260701-00042\n"
    "```"
)

_WO_HONEST_ZERO_BLOCK = (
    "```\n"
    "IDENTIFIER-MISS:\n"
    "token: WO-20260708-76827\n"
    "looked_in: work_orders.wo_number\n"
    "my_format: WO-<date>-<5 digits>, e.g. WO-20260707-00317, WO-20260701-00042\n"
    "```"
)


def test_strip_kills_uuid_false_positive_and_keeps_prose():
    # Real live answer shape: fenced block + "does not exist" prose.
    answer = (
        f"{_UUID_MISS_BLOCK}\n"
        f"The equipment ID {_UUID_TOKEN} does not exist in this system. "
        "It is formatted as a valid UUID matching this system's identifier "
        "format."
    )
    out = sa._strip_false_identifier_miss(answer)
    assert "IDENTIFIER-MISS" not in out
    assert "```" not in out
    assert f"The equipment ID {_UUID_TOKEN} does not exist" in out


def test_strip_keeps_true_vocabulary_miss_po_vs_wo():
    answer = (
        f"{_PO_TOKEN_MISS_BLOCK}\n"
        "That identifier does not match this system's vocabulary."
    )
    assert sa._strip_false_identifier_miss(answer) == answer


def test_strip_removes_honest_zero_wo_vs_wo():
    answer = (
        f"{_WO_HONEST_ZERO_BLOCK}\n"
        "No work order with that number was found."
    )
    out = sa._strip_false_identifier_miss(answer)
    assert "IDENTIFIER-MISS" not in out
    assert "No work order with that number was found." in out


def test_strip_fails_open_on_malformed_block():
    # No token line — unparseable; the block must be KEPT.
    no_token = (
        "```\n"
        "IDENTIFIER-MISS:\n"
        "looked_in: work_orders.wo_number\n"
        f"my_format: e.g. {_UUID_SAMPLE_A}\n"
        "```\n"
        "Context sentence."
    )
    assert sa._strip_false_identifier_miss(no_token) == no_token
    # No usable my_format samples — also KEPT.
    no_samples = (
        "```\n"
        "IDENTIFIER-MISS:\n"
        f"token: {_UUID_TOKEN}\n"
        "looked_in: equipment.equipment_id\n"
        "my_format: short internal codes\n"
        "```\n"
        "Context sentence."
    )
    assert sa._strip_false_identifier_miss(no_samples) == no_samples


def test_strip_is_identity_without_any_block():
    answer = "Zero rows matched. That is the honest answer."
    assert sa._strip_false_identifier_miss(answer) is answer


def test_strip_multiple_blocks_only_shape_matching_one_removed():
    answer = (
        f"{_UUID_MISS_BLOCK}\n"
        "First identifier checked.\n\n"
        f"{_PO_TOKEN_MISS_BLOCK}\n"
        "Second identifier checked."
    )
    out = sa._strip_false_identifier_miss(answer)
    # The UUID false positive is gone...
    assert _UUID_TOKEN not in out
    # ...the genuine vocabulary miss (PO token vs WO samples) survives.
    assert "token: PO-20260708-76827" in out
    assert out.count("IDENTIFIER-MISS") == 1
    assert "First identifier checked." in out
    assert "Second identifier checked." in out


# --------------------------------------------------------------------------- #
# 6. _shape_signature unit cases
# --------------------------------------------------------------------------- #
def test_shape_signature_uuid_equals_uuid():
    assert sa._shape_signature(_UUID_TOKEN) == sa._shape_signature(_UUID_SAMPLE_A)


def test_shape_signature_uuid_differs_from_po_form():
    assert sa._shape_signature(_UUID_TOKEN) != sa._shape_signature("PO-20260708-76827")


def test_shape_signature_prefix_text_is_semantic_po_vs_wo():
    assert sa._shape_signature("PO-20260708-76827") != sa._shape_signature(
        "WO-20260708-76827"
    )
    # Case-insensitive on the prefix text.
    assert sa._shape_signature("wo-20260708-76827") == sa._shape_signature(
        "WO-20260708-76827"
    )


def test_shape_signature_digit_segment_length_mismatch_differs():
    assert sa._shape_signature("WO-1001") != sa._shape_signature("WO-00317")


def test_signatures_match_treats_same_length_digit_as_hex():
    # Two UUIDs whose final groups classify digit vs hex purely by luck of
    # the draw must still count as the same format.
    assert sa._signatures_match(
        sa._shape_signature(_UUID_TOKEN), sa._shape_signature(_UUID_SAMPLE_B)
    )
    # Length still gates: digit(4) never matches hex(5).
    assert not sa._signatures_match(
        sa._shape_signature("WO-1001"), sa._shape_signature("WO-1a3f5")
    )


def test_shape_signature_all_hex_letter_segment_classifies_as_hex():
    # 'dead'/'beef' are purely alpha but composed only of hex letters (a-f):
    # they classify as (len, hex), NOT as semantic prefix TEXT.
    assert sa._shape_signature("dead") == ((), ((4, "hex"),))
    # A short alpha segment with a NON-hex letter keeps the TEXT rule.
    assert sa._shape_signature("ORD") == ((), (("prefix", "ord"),))


def test_signatures_match_hex_letter_uuid_matches_real_uuid():
    # Live Probe B regression: an honest-zero probe UUID whose middle groups
    # are all hex LETTERS must still shape-match a genuine UUID sample.
    assert sa._signatures_match(
        sa._shape_signature("00000000-dead-beef-0000-000000000000"),
        sa._shape_signature("0292d6db-98e4-4f6d-8791-e2ebd53a1288"),
    )


def test_shape_signature_non_hex_prefixes_stay_semantic():
    # PO vs WO both contain non-hex letters -> TEXT rule -> still DIFFERENT.
    assert not sa._signatures_match(
        sa._shape_signature("PO-20260708-76827"),
        sa._shape_signature("WO-20260707-00317"),
    )


def test_strip_kills_hex_letter_uuid_false_positive_live_probe_b_shape():
    # EXACT live Probe B answer shape: prose + fenced block whose my_format
    # line lists real-shape UUID examples.
    token = "00000000-dead-beef-0000-000000000000"  # nosec B105 - not a credential: field/env/secret NAME, sentinel, or offline test fixture
    answer = (
        f"The equipment ID {token} does not exist in this system's records. "
        "I checked the equipment table and found no matching row.\n"
        "```\n"
        "IDENTIFIER-MISS:\n"
        f"token: {token}\n"
        "looked_in: equipment.equipment_id\n"
        "my_format: UUID in standard format (e.g., "
        "0292d6db-98e4-4f6d-8791-e2ebd53a1288, "
        "cabf7728-1c3d-4a5e-9f00-6b7c8d9e0f11, "
        "8e70f384-2b4c-4d6e-8a90-1f2e3d4c5b6a)\n"
        "```"
    )
    out = sa._strip_false_identifier_miss(answer)
    assert "IDENTIFIER-MISS" not in out
    assert "```" not in out
    assert f"The equipment ID {token} does not exist" in out
    assert "no matching row" in out


# --------------------------------------------------------------------------- #
# 6b. Fix-pass regressions
# --------------------------------------------------------------------------- #
def test_contrastive_not_token_in_my_format_keeps_genuine_miss():
    # A contrastive my_format line ("..., not <token>") must not
    # make the token its own sample — the genuine miss is KEPT.
    block = (
        "```\n"
        "IDENTIFIER-MISS:\n"
        "token: PO-20260708-76827\n"
        "looked_in: work_orders.wo_number\n"
        "my_format: WO-<date>-<5 digits>, e.g. WO-20260707-00317, "
        "not PO-20260708-76827\n"
        "```"
    )
    answer = f"{block}\nThat identifier is not this system's vocabulary."
    assert sa._strip_false_identifier_miss(answer) == answer


def test_parse_excludes_token_case_insensitive_and_bounds_at_next_label():
    # Token exclusion is case-insensitive; the my_format sweep
    # stops at the next protocol field (context) so its identifiers never
    # leak in.
    block = (
        "```\n"
        "IDENTIFIER-MISS:\n"
        "token: PO-20260708-76827\n"
        "my_format: WO-<date>-<5 digits>, e.g. WO-20260707-00317, "
        "not po-20260708-76827\n"
        "context: user asked about PO-20260708-11111 too\n"
        "```"
    )
    token, samples = sa._parse_identifier_miss_block(block)
    assert token == "PO-20260708-76827"  # nosec B105 - not a credential: field/env/secret NAME, sentinel, or offline test fixture
    assert samples == ["WO-20260707-00317"]


def test_long_alpha_prefix_order_vs_wkord_keeps_genuine_miss():
    # Alpha segments longer than 4 chars compare by TEXT —
    # ORDER-12345 vs WKORD-12345 is a vocabulary difference, KEPT.
    block = (
        "```\n"
        "IDENTIFIER-MISS:\n"
        "token: ORDER-12345\n"
        "looked_in: work_orders.wo_number\n"
        "my_format: WKORD-<5 digits>, e.g. WKORD-12345, WKORD-67890\n"
        "```"
    )
    answer = f"{block}\nThat identifier is not this system's vocabulary."
    assert sa._strip_false_identifier_miss(answer) == answer


def test_shape_signature_long_alpha_segments_compare_by_text():
    # No length cap on the TEXT rule (case-insensitive).
    assert not sa._signatures_match(
        sa._shape_signature("ORDER-12345"), sa._shape_signature("WKORD-12345")
    )
    assert sa._signatures_match(
        sa._shape_signature("order-12345"), sa._shape_signature("ORDER-12345")
    )


def test_unclosed_malformed_block_then_wellformed_false_positive():
    # An UNCLOSED malformed first block must not eat the second
    # block's opening fence — the well-formed false positive strips cleanly,
    # the malformed block is KEPT intact (fail-open).
    malformed = (
        "```\n"
        "IDENTIFIER-MISS:\n"
        "token: WO-20260708-76827\n"
        "my_format: WO-<date>-<5 digits>, e.g. WO-20260707-00317\n"
    )
    answer = (
        f"{malformed}"
        "Some prose in between.\n"
        f"{_UUID_MISS_BLOCK}\n"
        "Tail prose."
    )
    out = sa._strip_false_identifier_miss(answer)
    assert _UUID_TOKEN not in out
    assert out.startswith("```\nIDENTIFIER-MISS:\ntoken: WO-20260708-76827")
    assert "Some prose in between." in out
    assert "Tail prose." in out
    assert out.count("IDENTIFIER-MISS") == 1


def test_unclosed_malformed_block_then_wellformed_genuine_miss_all_kept():
    # Keep direction: malformed unmatched + genuine miss kept —
    # the answer is byte-identical (the second block stays fenced).
    answer = (
        "```\n"
        "IDENTIFIER-MISS:\n"
        "token: WO-20260708-76827\n"
        "my_format: WO-<date>-<5 digits>, e.g. WO-20260707-00317\n"
        f"{_PO_TOKEN_MISS_BLOCK}\n"
        "Tail prose."
    )
    assert sa._strip_false_identifier_miss(answer) == answer


def test_bare_hex_word_is_never_a_sample_digit_token_kept():
    # An English all-hex-letter word ('cafe') in my_format prose
    # is NOT an identifier sample — a bare digit token stays fail-open KEPT.
    block = (
        "```\n"
        "IDENTIFIER-MISS:\n"
        "token: 7682\n"
        "looked_in: orders.order_no\n"
        "my_format: numeric codes assigned by the cafe kiosk\n"
        "```"
    )
    answer = f"{block}\nNo such order code."
    assert sa._strip_false_identifier_miss(answer) == answer
    _token, samples = sa._parse_identifier_miss_block(block)
    assert samples == []
    # Hex samples WITH a separator are still admitted.
    _token2, samples2 = sa._parse_identifier_miss_block(
        "```\nIDENTIFIER-MISS:\ntoken: 1234-5678\nmy_format: dead-beef\n```"
    )
    assert samples2 == ["dead-beef"]


def test_strip_leaves_unrelated_regions_byte_intact():
    # After a strip elsewhere, an unrelated code fence with
    # blank-line runs must be byte-identical (no global \n{3,} collapse).
    unrelated = (
        "Intro paragraph.\n\n\n\n"
        "```python\nprint('x')\n\n\n\ncode tail\n```\n\n\n"
        "Closing thoughts."
    )
    answer = f"{_UUID_MISS_BLOCK}\nSurviving prose.\n\n{unrelated}"
    out = sa._strip_false_identifier_miss(answer)
    assert "IDENTIFIER-MISS" not in out
    assert "Surviving prose." in out
    assert unrelated in out  # byte-identical away from the removal seam


# --------------------------------------------------------------------------- #
# 7. Seam — every Mode A answer flows through the guard (_run_mode_a_query)
# --------------------------------------------------------------------------- #
def test_run_mode_a_query_applies_guard_after_normalization(monkeypatch):
    import asyncio

    monkeypatch.setattr(
        sa, "_invoke_mode_a_agent_with_state",
        lambda _agent, _q: (f"{_UUID_MISS_BLOCK}\nNo such equipment ID exists.", {}),
    )
    monkeypatch.setattr(
        sa, "_augment_question_with_kg_context", lambda _s, q, _k: q,
    )
    out = asyncio.run(sa._run_mode_a_query(object(), "sys-a", "question"))
    assert "IDENTIFIER-MISS" not in out
    assert "No such equipment ID exists." in out


def test_run_mode_a_query_keeps_genuine_miss(monkeypatch):
    import asyncio

    monkeypatch.setattr(
        sa, "_invoke_mode_a_agent_with_state",
        lambda _agent, _q: (f"{_PO_TOKEN_MISS_BLOCK}\nNot my vocabulary.", {}),
    )
    monkeypatch.setattr(
        sa, "_augment_question_with_kg_context", lambda _s, q, _k: q,
    )
    out = asyncio.run(sa._run_mode_a_query(object(), "sys-a", "question"))
    assert "IDENTIFIER-MISS" in out
    assert "token: PO-20260708-76827" in out
