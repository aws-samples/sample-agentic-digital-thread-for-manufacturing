# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""VALUE-MAPPING-PROPOSAL blocks that cannot be a mapping are refused at parse.

Rehearsal walks showed answers ending in blocks whose field slot lists two
fields at once (``a.x, b.y`` / ``a.x / b.y``), whose ``to_value`` is a no-match
placeholder, or whose two endpoints are the same value on the same system.
``runtime_app._ingest_value_mapping_proposals`` must refuse such a block
BEFORE ``log_proposed_mapping`` sees it: no proposal written and one bounded
info line of per-reason counts. The list rule is conservative: a single
field name that merely contains a separator files exactly as before. A
valid block keeps filing with byte-identical kwargs, and a raising pre-check
falls through to filing.

Offline; GENERIC synthetic systems/fields/values only.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

import agents.artifact_usage as artifact_usage
from agents import proposal_scoring, runtime_app
from agents.proposal_scoring import names_multiple_fields


def _block(**overrides: str) -> str:
    slots = {
        "from_system": "sys-a",
        "from_field": "records.external_id",
        "from_value": "AB-1001",
        "to_system": "sys-b",
        "to_field": "entities.canonical_id",
        "to_value": "4400001001",
    }
    slots.update(overrides)
    return "\n".join(f"{key}: {value}" for key, value in slots.items())


@pytest.fixture(autouse=True)
def no_ledger_writes(monkeypatch):
    """The refusal writes NO usage-ledger row (it would carry no honest
    evidence class); any bump here fails the test."""
    calls: list = []
    monkeypatch.setattr(artifact_usage, "bump_artifact_usage",
                        lambda *a, **k: calls.append(a) or 0)
    yield
    assert calls == []


def _ingest(blocks):
    writes: list[dict] = []
    accepted = runtime_app._ingest_value_mapping_proposals(
        blocks, prompt="original question", tool_calls_count=2,
        log_mapping=lambda **kwargs: writes.append(kwargs) or f"m-{len(writes)}",
    )
    return accepted, writes


def _refusal_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records
            if "value mapping blocks refused" in r.getMessage()]


@pytest.mark.parametrize("overrides", [
    {"from_field": "events.machine, runs.line"},
    {"to_field": "items.batch_id / orders.number"},
    {"to_field": "items.batch_id; orders.number"},
    {"to_field": "items.batch_id | orders.number"},
    {"to_field": "items.batch_id or orders.number"},
    {"from_field": 'a.x, "b.y"'},
    {"from_field": "a.x / (b.y)"},
    {"from_field": '"t"."c", u.d'},
])
def test_two_dotted_fields_in_one_slot_are_refused(caplog, overrides):
    with caplog.at_level(logging.INFO, logger=runtime_app.logger.name):
        accepted, writes = _ingest([_block(**overrides)])
    assert (accepted, writes) == (0, [])
    assert _refusal_lines(caplog) == [
        "Self-learning value mapping blocks refused: "
        "multi_field=1 placeholder_value=0 self_echo=0"]


@pytest.mark.parametrize("field", [
    "Model.Run/Stop",
    "asset.Power and Energy",
    "Order or Return",
    '"Order, Type"',
    'orders."Order, Type"',
    "runs.line_id / machine identifier",
    "items.batch_id (format AA-NNNN/BB, CC)",
    "events/runs.machine",
])
def test_single_names_containing_a_separator_still_file(caplog, field):
    with caplog.at_level(logging.INFO, logger=runtime_app.logger.name):
        accepted, writes = _ingest([_block(to_field=field)])
    assert accepted == 1 and writes[0]["to_field"] == field
    assert _refusal_lines(caplog) == []


@pytest.mark.parametrize("to_value", [
    "NO MATCH FOUND", "none", "unknown", "N/A", "(no matching record found)",
])
def test_placeholder_to_value_is_refused(to_value):
    assert _ingest([_block(to_value=to_value)]) == (0, [])


@pytest.mark.parametrize("overrides", [
    # Same system, same field, same value: a value mapped to itself.
    {"to_system": "sys-a", "to_field": "records.external_id",
     "to_value": "AB-1001"},
    # Same system, different column, same literal: an echo.
    {"to_system": "SYS-A", "to_field": "entities.other_ref",
     "to_value": "AB-1001"},
])
def test_same_system_same_value_is_refused(overrides):
    assert _ingest([_block(**overrides)]) == (0, [])


def test_valid_block_files_byte_identically():
    accepted, writes = _ingest([_block() + "\nconfidence: high\nconcept: thing"])
    assert accepted == 1
    assert writes == [{
        "from_system": "sys-a", "from_field": "records.external_id",
        "from_value": "AB-1001", "to_system": "sys-b",
        "to_field": "entities.canonical_id", "to_value": "4400001001",
        "concept": "thing", "confidence": 0.9, "method": "", "reason": "",
        "query_context": "original question", "tool_calls_count": 2,
        "proposed_type": "value_mapping", "provenance": "orchestrator",
    }]


@pytest.mark.parametrize("overrides", [
    # Same column across two tables of one system stays intra-system evidence
    # (pinned in test_equal_literal_echo_gate.py) and still files.
    {"to_system": "sys-a", "from_field": "orders.line_id",
     "to_field": "lines.line_id", "to_value": "AB-1001"},
    # Equal literals across DIFFERENT systems are surfaced review-side only.
    {"to_value": "AB-1001"},
])
def test_shapes_left_to_existing_gates_still_file(overrides):
    accepted, writes = _ingest([_block(**overrides)])
    assert accepted == 1 and len(writes) == 1


def test_a_raising_pre_check_falls_through_and_files(monkeypatch, caplog):
    def boom(_field):
        raise RuntimeError("predicate fault")
    monkeypatch.setattr(proposal_scoring, "names_multiple_fields", boom)
    with caplog.at_level(logging.INFO, logger=runtime_app.logger.name):
        accepted, writes = _ingest([_block(from_field="a.x, b.y")])
    assert accepted == 1 and writes[0]["from_field"] == "a.x, b.y"
    assert _refusal_lines(caplog) == []
    assert any("pre-check failed" in r.getMessage() for r in caplog.records)


def test_refusals_are_counted_per_reason_in_one_line(caplog):
    blocks = [
        _block(from_field="a.x, b.y"), _block(to_field="a.x / b.y"),
        _block(to_value="none"), _block(), "from_system: sys-a",
    ]
    with caplog.at_level(logging.INFO, logger=runtime_app.logger.name):
        accepted, writes = _ingest(blocks)
    assert accepted == 1 and len(writes) == 1
    assert _refusal_lines(caplog) == [
        "Self-learning value mapping blocks refused: "
        "multi_field=2 placeholder_value=1 self_echo=0"]


def test_answer_text_is_not_touched(monkeypatch):
    monkeypatch.setattr(runtime_app, "_skip_post_turn_capture", lambda s: False)
    text = ("Answer.\n---VALUE-MAPPING-PROPOSAL---\n"
            + _block(to_value="NO MATCH FOUND") + "\n---END-PROPOSAL---\n")
    state = SimpleNamespace(response_text=[text], prompt="q", raw_prompt="q",
                            tool_calls_log=[], session_id="s-1")
    runtime_app._capture_value_mapping_proposals(state)
    assert state.response_text == [text]


@pytest.mark.parametrize("field, expected", [
    ("records.external_id", False),
    ("", False),
    (None, False),
    ("a.x/b.y", False),
    ("a.x, b", False),
    ("x.y; z", False),
    ("a.x, operator's b.y, c.z", True),  # unclosed apostrophe stays literal
    ('"a.x, b.y"', False),
    ("a.x, b.y", True),
    ("a.x | b.y", True),
    ("a.x AND b.y", True),
    ("[a.x], `b`.`y`", True),
])
def test_names_multiple_fields(field, expected):
    assert names_multiple_fields(field) is expected
