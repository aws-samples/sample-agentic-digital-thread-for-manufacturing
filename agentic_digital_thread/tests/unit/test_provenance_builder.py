# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Pure provenance-record builder (provenance plan, Wave 1 Task 1.1).

Pins the frozen contracts the parallel workers build against:

- **D2 ordinal parity** — ``citation_ordinals`` reproduces the EXACT client
  predicate (``thread-store.ts`` ``data.tool !== 'explore_graph'``): the
  bare graph tool is NOT numbered, a gateway-prefixed ``x___explore_graph``
  IS, and so is ``get_system_schema``. Deliberately diverges from
  ``query_intelligence.is_graph_query`` on the prefixed spelling.
- **D3 determinism** — ``query_ref`` is
  ``"q-" + sha256(session|tool_id)[:12]``, stable per pair.
- **D1 shape + caps** — ≤32 records, ≤8 filters × 120 chars, ≤20 rules with
  labels ≤160, total serialized payload ≤4 KB with an explicit
  ``truncated`` flag.
- **D4 honest absence** — no captured metadata ⇒ ``unverified`` with NO
  fabricated entity/scope; ``verified`` only for accepted verdict + bound
  typed evidence.
- **D6 leak-proofing** — ``tool_inputs`` (SQL) never surfaces in the payload.
- **Import purity** — stdlib-only; never imports runtime_app/chat_client.

Offline: no AWS, no network.
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

import agents.provenance as provenance
from agents.query_intelligence import is_graph_query

_QUERY_REF_RE = re.compile(r"^q-[0-9a-f]{12}$")

#: The pinned D2 parity fixture — bare graph (NOT cited), gateway-prefixed
#: graph (cited), schema tool (cited), two system queries (cited).
_PARITY_CALLS = [
    {"name": "explore_graph", "toolUseId": "t-graph"},
    {"name": "x___explore_graph", "toolUseId": "t-gwgraph"},
    {"name": "get_system_schema", "toolUseId": "t-schema"},
    {"name": "sysa___query_sysa", "toolUseId": "t-q1"},
    {"name": "sysb___query_sysb", "toolUseId": "t-q2"},
]


def _build(tool_calls=None, **overrides):
    kwargs = dict(
        session_id="sess-1",
        tool_calls=_PARITY_CALLS if tool_calls is None else tool_calls,
        tool_inputs={},
        tool_results_metadata={},
        evidence_by_tool={},
        rules_applied=[],
        verdict=None,
        captured_at="2026-08-03T19:00:00Z",
    )
    kwargs.update(overrides)
    return provenance.build_provenance_records(**kwargs)


# ---------------------------------------------------------------- D2 parity

def test_client_predicate_is_exact_string_not_is_graph_query():
    assert provenance.is_client_cited("explore_graph") is False
    # The prefixed spelling IS cited by the client — and is_graph_query
    # would say otherwise, which is exactly why it must not be used here.
    assert provenance.is_client_cited("digital-thread___explore_graph") is True
    assert is_graph_query("digital-thread___explore_graph") is True
    assert provenance.is_client_cited("get_system_schema") is True


def test_citation_ordinals_match_client_counter():
    ordinals = provenance.citation_ordinals(_PARITY_CALLS)
    assert ordinals == {"t-gwgraph": 1, "t-schema": 2, "t-q1": 3, "t-q2": 4}


def test_ordinal_counter_advances_for_unbindable_calls():
    # A cited call without a toolUseId still consumes a number (client
    # parity) — it just can't land in the map.
    calls = [
        {"name": "sysa___query_sysa"},
        {"name": "sysb___query_sysb", "toolUseId": "t-2"},
    ]
    assert provenance.citation_ordinals(calls) == {"t-2": 2}


def test_ordinals_defensive_on_junk():
    assert provenance.citation_ordinals([]) == {}
    assert provenance.citation_ordinals(None) == {}
    assert provenance.citation_ordinals(["nope", 3, {"toolUseId": "t-1"}]) == {"t-1": 1}


# ------------------------------------------------------------ D3 query_ref

def test_mint_query_ref_shape_and_determinism():
    ref = provenance.mint_query_ref("sess-1", "t-q1")
    assert _QUERY_REF_RE.fullmatch(ref)
    assert ref == provenance.mint_query_ref("sess-1", "t-q1")
    assert ref != provenance.mint_query_ref("sess-1", "t-q2")
    assert ref != provenance.mint_query_ref("sess-2", "t-q1")


def test_builder_is_deterministic():
    assert _build() == _build()


# ------------------------------------------------------------- D1 shape

def test_parity_fixture_yields_records_1_to_4():
    payload = _build()
    assert set(payload) == {"records", "truncated"}
    assert payload["truncated"] is False
    records = payload["records"]
    assert [r["citation_num"] for r in records] == [1, 2, 3, 4]
    assert [r["tool_id"] for r in records] == ["t-gwgraph", "t-schema", "t-q1", "t-q2"]
    # The bare graph call never yields a record.
    assert all(r["tool_id"] != "t-graph" for r in records)
    for record in records:
        assert _QUERY_REF_RE.fullmatch(record["query_ref"])
        assert record["captured_at"] == "2026-08-03T19:00:00Z"
        assert record["verification"] == "unverified"
        assert set(record) == {
            "citation_num", "tool_id", "source_system", "tool_name",
            "entity", "scope", "query_ref", "result_shape", "kg_route",
            "captured_at", "verification",
        }


def test_source_system_from_gateway_prefix_and_evidence():
    payload = _build(evidence_by_tool={"t-q2": {"system_id": "sysb-real"}})
    by_id = {r["tool_id"]: r for r in payload["records"]}
    assert by_id["t-q1"]["source_system"] == "sysa"       # gateway prefix
    assert by_id["t-q2"]["source_system"] == "sysb-real"  # evidence wins
    assert by_id["t-schema"]["source_system"] == ""        # honestly empty


def test_empty_turn_yields_empty_payload():
    assert _build(tool_calls=[]) == {"records": [], "truncated": False}


# ------------------------------------------------------------- D1 caps

def test_record_count_and_payload_byte_caps():
    calls = [{"name": f"sys{i}___query", "toolUseId": f"t-{i}"} for i in range(40)]
    payload = _build(tool_calls=calls)
    assert payload["truncated"] is True
    assert len(payload["records"]) <= provenance.MAX_PROVENANCE_RECORDS
    size = len(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    assert size <= provenance.MAX_PAYLOAD_BYTES


def test_filters_and_columns_capped():
    calls = [{"name": "sysa___query_sysa", "toolUseId": "t-q1"}]
    evidence = {"t-q1": {
        "table": "orders" * 100,
        "columns": [f"col_{i}" + "x" * 300 for i in range(30)],
        "filters": [f"filter {i} " + "y" * 300 for i in range(12)],
    }}
    payload = _build(tool_calls=calls, evidence_by_tool=evidence)
    record = next(r for r in payload["records"] if r["tool_id"] == "t-q1")
    assert len(record["entity"]["table"]) <= 160
    assert len(record["entity"]["columns"]) <= provenance.MAX_ENTITY_COLUMNS
    assert all(len(c) <= provenance.MAX_FILTER_LEN for c in record["entity"]["columns"])
    filters = record["scope"]["filters"]
    assert len(filters) <= provenance.MAX_SCOPE_FILTERS
    assert all(len(f) <= provenance.MAX_FILTER_LEN for f in filters)


def test_rules_applied_capped_and_vm_ids_split_out():
    calls = [{"name": "sysa___query_sysa", "toolUseId": "t-q1"}]
    rules = [{"id": f"subj-{i:016x}", "label": f"r{i}"} for i in range(25)]
    rules[0] = {"id": "subj-long", "label": "L" * 500}
    rules[3] = {"id": "vm-learned-0a1b2c3d", "label": "unit translation"}
    payload = _build(tool_calls=calls, rules_applied=rules)
    route = payload["records"][0]["kg_route"]
    assert len(route["rules_applied"]) <= 20
    assert all(len(r["label"]) <= 160 for r in route["rules_applied"])
    assert route["value_mappings"] == ["vm-learned-0a1b2c3d"]


def test_rule_label_falls_back_to_id():
    payload = _build(rules_applied=[{"id": "subj-abc"}])
    row = payload["records"][0]["kg_route"]["rules_applied"][0]
    assert row == {"id": "subj-abc", "label": "subj-abc"}


# --------------------------------------------------- D4 honest verification

def test_no_metadata_means_unverified_and_nothing_fabricated():
    payload = _build()
    for record in payload["records"]:
        assert record["verification"] == "unverified"
        assert record["entity"] == {"table": "", "columns": []}
        assert record["scope"] == {"filters": [], "window": None}
        assert record["result_shape"] == {"rows": None, "truncated": False,
                                          "partial_reason": ""}


def test_successful_capture_without_evidence_is_partial():
    meta = {"t-q1": {"tool": "sysa___query", "transport_status": "success",
                     "app_status": "success", "row_count": 12}}
    payload = _build(tool_results_metadata=meta)
    by_id = {r["tool_id"]: r for r in payload["records"]}
    assert by_id["t-q1"]["verification"] == "partial"
    assert by_id["t-q1"]["result_shape"]["rows"] == 12
    assert by_id["t-q2"]["verification"] == "unverified"


def test_partial_capture_reports_truncation_honestly():
    meta = {"t-q1": {"app_status": "partial", "item_count": 4,
                     "truncated": True, "partial_reason": "row_limit"}}
    payload = _build(tool_results_metadata=meta)
    record = next(r for r in payload["records"] if r["tool_id"] == "t-q1")
    assert record["verification"] == "partial"
    assert record["result_shape"] == {"rows": 4, "truncated": True,
                                      "partial_reason": "row_limit"}


def test_error_capture_is_unverified():
    meta = {"t-q1": {"app_status": "error", "safe_error_category": "tool_error"}}
    payload = _build(tool_results_metadata=meta)
    record = next(r for r in payload["records"] if r["tool_id"] == "t-q1")
    assert record["verification"] == "unverified"


def test_verified_requires_accepted_verdict_and_bound_evidence():
    meta = {"t-q1": {"app_status": "success", "row_count": 3}}
    evidence = {"t-q1": {"system_id": "sysa", "table": "orders"}}
    # Accepted + bound evidence ⇒ verified (enforce-path best case).
    verified = _build(tool_results_metadata=meta, evidence_by_tool=evidence,
                      verdict="accepted")
    assert next(r for r in verified["records"]
                if r["tool_id"] == "t-q1")["verification"] == "verified"
    # Accepted but NO evidence binding ⇒ partial at best.
    partial = _build(tool_results_metadata=meta, verdict="accepted")
    assert next(r for r in partial["records"]
                if r["tool_id"] == "t-q1")["verification"] == "partial"
    # Rejected verdict ⇒ unverified even with evidence + capture.
    rejected = _build(tool_results_metadata=meta, evidence_by_tool=evidence,
                      verdict="rejected")
    assert all(r["verification"] == "unverified" for r in rejected["records"])


def test_a_rejected_frame_holds_as_many_records_as_a_plain_one():
    """Reproduction: the rejection was repeated on every record,
    and at the frame's byte cap a rejected 32-call turn kept 9 records where a
    plain one keeps 10. The rejection is not on the wire at all (the emitters
    hand it to the sidecar merge), so a rejected frame is the plain frame."""
    calls = [{"name": f"system-{i}___query_system_{i}", "toolUseId": f"tool-{i}"}
             for i in range(32)]

    def frame(verdict):
        return provenance.build_provenance_records(
            session_id="session-budget", tool_calls=calls, tool_inputs={},
            tool_results_metadata={}, evidence_by_tool={}, rules_applied=[],
            verdict=verdict, captured_at="2026-10-27T00:00:00Z")

    plain, rejected = frame(None), frame("rejected")
    assert plain["truncated"] is True  # the cap is what is being measured
    assert len(rejected["records"]) == len(plain["records"])
    assert rejected == plain


def test_evidence_window_and_captured_at_flow_through():
    evidence = {"t-q1": {
        "window": {"start": "2026-08-01T00:00:00Z", "end": "2026-08-02T00:00:00Z"},
        "queried_at": "2026-08-03T18:59:00Z",
    }}
    payload = _build(evidence_by_tool=evidence)
    record = next(r for r in payload["records"] if r["tool_id"] == "t-q1")
    assert record["scope"]["window"] == {"start": "2026-08-01T00:00:00Z",
                                         "end": "2026-08-02T00:00:00Z"}
    assert record["captured_at"] == "2026-08-03T18:59:00Z"


def test_observed_range_keys_also_build_a_window():
    evidence = {"t-q1": {"source_observed_start": "2026-07-01T00:00:00Z"}}
    payload = _build(evidence_by_tool=evidence)
    record = next(r for r in payload["records"] if r["tool_id"] == "t-q1")
    assert record["scope"]["window"] == {"start": "2026-07-01T00:00:00Z", "end": ""}


# ------------------------------------------------------- D6 leak-proofing

def test_tool_input_sql_never_surfaces():
    inputs = {"t-q1": {"sql": "SELECT secret_col FROM classified_table_9911"}}
    payload = _build(tool_inputs=inputs)
    serialized = json.dumps(payload)
    assert "SELECT" not in serialized
    assert "classified_table_9911" not in serialized
    assert "9911" not in serialized


# ------------------------------------------------------------ import purity

def test_module_imports_are_stdlib_plus_query_intelligence_only():
    """AST import pin: stdlib + the pure ``query_intelligence`` sibling and
    NOTHING else — never boto3, never runtime_app/chat_client (both emitters
    import THIS module; a reverse import would break the offline lane)."""
    tree = ast.parse(Path(provenance.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert imported == {"__future__", "ast", "hashlib", "json", "typing",
                        "agents.query_intelligence"}


# --------------------------------------------------------------------------- #
# Wave-5 F1 reserved-arg context (provenance_context / parse_provenance_context)
# --------------------------------------------------------------------------- #

def test_provenance_context_round_trips():
    ref = provenance.mint_query_ref("s", "t")
    ctx = provenance.provenance_context("s", ref)
    assert ctx == {"session_id": "s", "query_ref": ref}
    assert provenance.parse_provenance_context(ctx) == ("s", ref)


@pytest.mark.parametrize("bad", [
    None,
    "q-abc",
    [],
    {},
    {"session_id": "s"},
    {"query_ref": "q-0123456789ab"},
    {"session_id": "s", "query_ref": "not-a-ref"},
    {"session_id": "s", "query_ref": "q-0123456789AB"},   # uppercase hex
    {"session_id": "s", "query_ref": "q-0123456789abcd"}, # too long
    {"session_id": "", "query_ref": "q-0123456789ab"},
    {"session_id": "bad session!", "query_ref": "q-0123456789ab"},
    {"session_id": "s" * 300, "query_ref": "q-0123456789ab"},
    {"session_id": 42, "query_ref": "q-0123456789ab"},
    {"session_id": "s", "query_ref": 42},
])
def test_parse_provenance_context_malformed_returns_none(bad):
    assert provenance.parse_provenance_context(bad) is None


def test_reserved_provenance_arg_spelling_frozen():
    assert provenance.RESERVED_PROVENANCE_ARG == "_adt_provenance"
