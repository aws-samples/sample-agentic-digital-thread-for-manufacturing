# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Runtime emitters for the ADDITIVE ``metadata.provenance`` key (T1.2).

Both answer boundaries emit the D1 payload through the ONE builder
(``agents/provenance.py``):

* legacy done branch — ``_provenance_events(s)`` called right after the
  ``_intelligence_summary_events(s)`` loop (source-pinned ordering below);
* enforce path — ``_integrity_provenance_events`` spliced into
  ``final_events`` between ``intelligence_summary`` and the ONE
  ``response_integrity`` marker.

Pinned here:
* exactly one ``provenance`` frame per turn, AFTER ``intelligence_summary``;
* frame ABSENT (not empty-empty) when the builder raises (swallow-guarded)
  and when there is nothing to report (zero cited calls);
* D4 on the enforce path: ``verified`` appears ONLY with an accepted verdict
  (AND bound typed evidence); the default report-only legacy path caps out
  at ``partial``/``unverified`` (``verdict=None`` — mode semantics unchanged);
* STILL exactly one ``response_integrity`` marker per enforce turn;
* D6: SQL/input/snippet text NEVER surfaces in the frame.

Offline: no AWS, no network. Enforce-path tests monkeypatch
``RESPONSE_INTEGRITY_MODE`` by project rule.
"""
from __future__ import annotations

import inspect
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from agents import runtime_app
from agents import provenance
from agents.provenance import mint_query_ref

SESSION = "sess-t12"


def _legacy_state(**overrides) -> SimpleNamespace:
    """Minimal legacy turn state (the _new_turn_state fields the emitter reads)."""
    state = SimpleNamespace(
        session_id=SESSION,
        prompt="how many open work orders",
        tool_calls_log=[
            {"name": "explore_graph", "toolUseId": "g1"},        # bare ⇒ NOT cited (D2)
            {"name": "x___explore_graph", "toolUseId": "g2"},    # prefixed ⇒ cited (D2)
            {"name": "erp___query_erp", "toolUseId": "t1"},
        ],
        tool_inputs={"t1": '{"question": "open work orders"}'},
        tool_results={"t1": '{"row_count": 2, "rows": []}'},
        rule_ids=set(),
        matched_rows=[],
    )
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


# ---------------------------------------------------------------------------
# Legacy done branch — _provenance_events(s)
# ---------------------------------------------------------------------------

def test_legacy_frame_shape_and_citation_parity():
    events = runtime_app._provenance_events(_legacy_state())
    assert len(events) == 1 and set(events[0]) == {"provenance"}
    payload = events[0]["provenance"]
    records = {r["tool_id"]: r for r in payload["records"]}
    # Bare explore_graph is never cited; the prefixed spelling IS (D2 parity).
    assert set(records) == {"g2", "t1"}
    assert records["g2"]["citation_num"] == 1
    assert records["t1"]["citation_num"] == 2
    erp = records["t1"]
    assert erp["source_system"] == "erp"
    assert erp["query_ref"] == mint_query_ref(SESSION, "t1")
    assert erp["result_shape"]["rows"] == 2
    # Report-only best case: bounded capture, no typed evidence ⇒ partial (D4).
    assert erp["verification"] == "partial"
    # No captured snippet for g2 ⇒ honest unverified, nothing fabricated.
    assert records["g2"]["verification"] == "unverified"
    assert records["g2"]["result_shape"] == {
        "rows": None, "truncated": False, "partial_reason": ""}


def test_legacy_zero_tool_calls_frame_absent():
    assert runtime_app._provenance_events(_legacy_state(tool_calls_log=[])) == []


def test_legacy_only_uncited_calls_frame_absent():
    state = _legacy_state(
        tool_calls_log=[{"name": "explore_graph", "toolUseId": "g1"}],
        tool_inputs={}, tool_results={})
    assert runtime_app._provenance_events(state) == []


def test_legacy_builder_failure_frame_absent(monkeypatch):
    import agents.provenance as provenance_module

    def boom(**kwargs):
        raise RuntimeError("builder exploded")

    monkeypatch.setattr(provenance_module, "build_provenance_records", boom)
    assert runtime_app._provenance_events(_legacy_state()) == []


def test_legacy_frame_never_surfaces_sql_or_snippet_text():
    """D6 leak pin: inputs and result snippets never enter the frame."""
    state = _legacy_state(
        tool_inputs={"t1": "SELECT secret_col FROM classified_orders WHERE x=1"},
        tool_results={"t1": "SELECT-shaped failure echo classified_orders"},
    )
    events = runtime_app._provenance_events(state)
    assert len(events) == 1
    serialized = json.dumps(events[0])
    assert "SELECT" not in serialized
    assert "classified_orders" not in serialized
    assert "secret_col" not in serialized


def test_legacy_rules_applied_reuses_extracted_rows():
    """The frame's kg_route reuses _extract_rule_usage's rows — matched rows
    render labels, unmatched ids fall back label == id; never a new query."""
    state = _legacy_state(
        rule_ids={"subj-unmatched-1"},
        matched_rows=[{"neptune_node_id": "subj-abc",
                       "from_system": "erp", "to_system": "mes",
                       "mapping_type": "value_mapping"}],
    )
    [event] = runtime_app._provenance_events(state)
    kg_route = event["provenance"]["records"][0]["kg_route"]
    ids = {row["id"] for row in kg_route["rules_applied"]}
    assert ids == {"subj-abc", "subj-unmatched-1"}
    fallback = next(r for r in kg_route["rules_applied"] if r["id"] == "subj-unmatched-1")
    assert fallback["label"] == "subj-unmatched-1"


def test_invoke_emits_provenance_after_summary_loop():
    """Ordering pin at the wiring level (the established getsource pattern):
    the legacy invoke body calls _provenance_events(s) AFTER the
    _intelligence_summary_events(s) loop, inside the same try."""
    src = inspect.getsource(runtime_app._run_orchestrator_base)
    assert "_provenance_events(s)" in src
    assert src.index("_intelligence_summary_events(s)") < src.index("_provenance_events(s)")
    # Emitted inside the streaming try (before the except/finally), so
    # AgentCore forwards it like the summary frames.
    assert src.index("_provenance_events(s)") < src.index("except Exception as exc:")


# ---------------------------------------------------------------------------
# Enforce path — final_events
# ---------------------------------------------------------------------------

def _fed_invocation(monkeypatch) -> runtime_app._RuntimeIntegrityInvocation:
    """An invocation with one cited erp call fed through consume() (the real
    tool_use + toolResult branches, retaining bounded per-call metadata)."""
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "enforce")
    monkeypatch.setattr(
        "agents.runtime_integrity.persist_typed_proposals", lambda *a, **k: 0)
    invocation = runtime_app._RuntimeIntegrityInvocation("how many open work orders")
    invocation.session_id = SESSION
    assert invocation.consume(
        {"current_tool_use": {"name": "erp___query_erp", "toolUseId": "t1",
                              "input": "{}"}}) is not None
    invocation.consume({"message": {"role": "user", "content": [{"toolResult": {
        "toolUseId": "t1", "status": "success",
        "content": [{"json": {"row_count": 3, "rows": []}}],
    }}]}})
    return invocation


def _accepted_answer() -> SimpleNamespace:
    quality = {
        "verdict": "accepted", "reason_codes": [], "current_turn_evidence": True,
        "completeness": "complete", "learning_allowed": True, "claim_count": 1,
        "current_intent": False,
    }
    return SimpleNamespace(quality_metadata=lambda: dict(quality))


def _bound_evidence_context(invocation) -> None:
    """Typed evidence bound to the erp tool — REAL contract models, so the
    real gate (rejected-verdict case) can walk the same context."""
    from agents.response_integrity_contracts import SourceEvidence, SystemAnswer
    evidence = SourceEvidence(
        evidence_id="ev-1", system_id="erp", boundary="sql",
        tool_name="erp___query_erp",
        queried_at=datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc),
        semantic_field="wo_status", locator="row:0", canonical_type="string",
    )
    invocation.context.system_answers = [SystemAnswer(
        status="complete", claims=(), evidence=(evidence,), reason_codes=())]


def test_enforce_verified_only_with_accepted_verdict(monkeypatch):
    invocation = _fed_invocation(monkeypatch)
    _bound_evidence_context(invocation)
    monkeypatch.setattr("agents.response_integrity.gate_final_result",
                        lambda result, context: ("answer text", _accepted_answer()))
    events = invocation.final_events()
    keys = [next(iter(e)) for e in events]
    # Ordering: summary, then provenance, then the ONE integrity marker.
    assert keys == ["data", "intelligence_summary", "provenance", "response_integrity"]
    assert sum(1 for e in events if "response_integrity" in e) == 1
    [record] = events[2]["provenance"]["records"]
    assert record["verification"] == "verified"
    assert record["source_system"] == "erp"
    assert record["entity"]["columns"] == ["wo_status"]
    assert record["query_ref"] == mint_query_ref(SESSION, "t1")
    assert record["result_shape"]["rows"] == 3


def test_enforce_rejected_verdict_never_verified(monkeypatch):
    """The REAL gate on an empty turn rejects (no claims) — even with bound
    evidence and a successful capture, no record may claim 'verified'."""
    invocation = _fed_invocation(monkeypatch)
    _bound_evidence_context(invocation)
    events = invocation.final_events()
    integrity = next(e["response_integrity"] for e in events if "response_integrity" in e)
    assert integrity["verdict"] == "rejected"
    provenance = next(e["provenance"] for e in events if "provenance" in e)
    assert provenance["records"]
    assert all(r["verification"] == "unverified" for r in provenance["records"])
    assert sum(1 for e in events if "response_integrity" in e) == 1


def _api_read_entry() -> dict:
    """The read entry a system agent that ran no SQL writes for its call."""
    import agents.system_agent as sa
    return sa._build_api_read_entry(1, "erp")


def _sql_entry() -> dict:
    """The statement entry a SQL system agent writes for its call."""
    return {"system_id": "erp", "tool_name": "query_erp",
            "executed_query": "SELECT wo_status FROM work_orders", "kg_artifacts": [],
            "captured_at": "2026-08-03T12:00:00Z",
            "entity": {"table": "work_orders", "columns": ["wo_status"]},
            "row_count": 3, "statement_count": 1, "origin": "system-agent"}


def _rejected_records(monkeypatch, entry) -> list[dict]:
    """The enforce frame's records for a turn the REAL gate rejects, with the
    store holding ``entry`` (normalized as the store keeps it) for the call."""
    from agents import provenance_store
    monkeypatch.setattr(
        "agents.provenance_store.get_query",
        lambda sid, ref: None if entry is None else provenance_store._normalize_entry(
            sid, ref, entry))
    invocation = _fed_invocation(monkeypatch)
    _bound_evidence_context(invocation)
    events = invocation.final_events()
    integrity = next(e["response_integrity"] for e in events if "response_integrity" in e)
    assert integrity["verdict"] == "rejected"
    records = next(e["provenance"] for e in events if "provenance" in e)["records"]
    return [{k: v for k, v in r.items() if k != "captured_at"} for r in records]


@pytest.mark.parametrize("entry", [_api_read_entry, _sql_entry], ids=["api_read", "sql"])
def test_enforce_rejected_verdict_stays_unverified_whatever_entry_exists(monkeypatch, entry):
    """Reproduction: the merge upgraded every unverified record that had a
    stored entry, including one the integrity gate rejected, so a rejected
    answer showed PARTIAL. A rejected record renders exactly as built, and
    says why it is unverified."""
    as_built = _rejected_records(monkeypatch, None)
    [record] = _rejected_records(monkeypatch, entry())
    assert record["verification"] == "unverified"
    assert "verdict" not in record  # round 3: the rejection is not repeated per record
    assert [record] == as_built


def test_enforce_accepted_without_bound_evidence_caps_at_partial(monkeypatch):
    """Accepted verdict alone is NOT enough — D4 requires bound typed
    evidence for 'verified'; a successful capture without binding is partial."""
    invocation = _fed_invocation(monkeypatch)  # no evidence bound
    monkeypatch.setattr("agents.response_integrity.gate_final_result",
                        lambda result, context: ("answer text", _accepted_answer()))
    events = invocation.final_events()
    [record] = next(e["provenance"] for e in events if "provenance" in e)["records"]
    assert record["verification"] == "partial"


def test_enforce_builder_failure_frame_absent_marker_intact(monkeypatch):
    import agents.provenance as provenance_module

    def boom(**kwargs):
        raise RuntimeError("builder exploded")

    invocation = _fed_invocation(monkeypatch)
    monkeypatch.setattr(provenance_module, "build_provenance_records", boom)
    events = invocation.final_events()
    assert not any("provenance" in e for e in events)
    # The stream is otherwise intact: summary + exactly one integrity marker.
    assert any("intelligence_summary" in e for e in events)
    assert sum(1 for e in events if "response_integrity" in e) == 1


def test_enforce_zero_tool_calls_frame_absent(monkeypatch):
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "enforce")
    monkeypatch.setattr(
        "agents.runtime_integrity.persist_typed_proposals", lambda *a, **k: 0)
    invocation = runtime_app._RuntimeIntegrityInvocation("hello")
    events = invocation.final_events()
    assert not any("provenance" in e for e in events)
    assert sum(1 for e in events if "response_integrity" in e) == 1


def test_enforce_frame_never_surfaces_result_content(monkeypatch):
    """D6 on the enforce path: a hostile result value never reaches the frame
    (only bounded count/status metadata is retained per call)."""
    invocation = _fed_invocation(monkeypatch)
    invocation.consume({"current_tool_use": {
        "name": "mes___query_mes", "toolUseId": "t2", "input": "{}"}})
    invocation.consume({"message": {"role": "user", "content": [{"toolResult": {
        "toolUseId": "t2", "status": "success",
        "content": [{"json": {"rows": [{"secret": "SELECT * FROM classified_orders"}],
                              "row_count": 1}}],
    }}]}})
    monkeypatch.setattr("agents.response_integrity.gate_final_result",
                        lambda result, context: ("answer text", _accepted_answer()))
    events = invocation.final_events()
    frame = next(e for e in events if "provenance" in e)
    serialized = json.dumps(frame)
    assert "SELECT" not in serialized
    assert "classified_orders" not in serialized


# ---------------------------------------------------------------------------
# Evidence derivation helper
# ---------------------------------------------------------------------------

def test_evidence_by_tool_maps_names_to_tool_ids():
    state = SimpleNamespace(
        tool_names={"t1": "erp___query_erp", "t2": "mes___query_mes"},
        context=SimpleNamespace(system_answers=[SimpleNamespace(evidence=[
            SimpleNamespace(tool_name="erp___query_erp", system_id="erp",
                            semantic_field="wo_status",
                            queried_at=datetime(2026, 8, 3, tzinfo=timezone.utc),
                            source_observed_start=None, source_observed_end=None),
            SimpleNamespace(tool_name="erp___query_erp", system_id="erp",
                            semantic_field="wo_id",
                            queried_at=datetime(2026, 8, 3, tzinfo=timezone.utc),
                            source_observed_start=None, source_observed_end=None),
        ])]),
    )
    bound = runtime_app._integrity_evidence_by_tool(state)
    assert set(bound) == {"t1"}  # t2 has no evidence — honestly absent
    assert bound["t1"]["system_id"] == "erp"
    assert bound["t1"]["columns"] == ["wo_status", "wo_id"]


def test_evidence_by_tool_empty_context_is_empty():
    state = SimpleNamespace(tool_names={"t1": "erp___query_erp"},
                            context=SimpleNamespace(system_answers=[]))
    assert runtime_app._integrity_evidence_by_tool(state) == {}


# ---------------------------------------------------------------------------
# T3.2 — server-only provenance_queries sidecar (D6)
# ---------------------------------------------------------------------------

_SQL = "SELECT wo_id FROM work_orders WHERE wo_status = 'open'"
_CYPHER = "MATCH (n:System) RETURN n.system_id"


def _sidecar_state(**overrides) -> SimpleNamespace:
    """Legacy state with one Mode-B SQL call, one graph call, one Mode-A NL."""
    defaults = dict(
        tool_calls_log=[
            {"name": "erp___query_erp", "toolUseId": "t1"},    # Mode B — sql
            {"name": "explore_graph", "toolUseId": "g1"},      # graph — cypher
            {"name": "mes___query_mes", "toolUseId": "t2"},    # Mode A — NL
        ],
        tool_inputs={
            "t1": json.dumps({"sql": _SQL}),
            "g1": json.dumps({"cypher": _CYPHER}),
            "t2": json.dumps({"question": "how many open work orders"}),
        },
        rule_ids={"vm-learned-2", "subj-1"},
    )
    defaults.update(overrides)
    return _legacy_state(**defaults)


def test_legacy_sidecar_sql_verbatim_keyed_by_record_query_ref():
    """D6/D3: the Mode-B SQL input lands VERBATIM, keyed by the SAME
    query_ref the provenance record carries (no coordination needed)."""
    state = _sidecar_state()
    [frame] = runtime_app._provenance_query_sidecar(state)
    assert set(frame) == {"provenance_queries"}
    entries = frame["provenance_queries"]
    # Query-bearing calls only: sql + cypher; the Mode-A NL call is ABSENT.
    assert set(entries) == {mint_query_ref(SESSION, "t1"), mint_query_ref(SESSION, "g1")}
    erp = entries[mint_query_ref(SESSION, "t1")]
    assert erp["query_text"] == _SQL  # verbatim — never truncated/reworded
    assert erp["system_id"] == "erp"
    assert erp["tool_name"] == "erp___query_erp"
    assert erp["kg_artifact_ids"] == ["subj-1", "vm-learned-2"]  # raw ids, sorted
    assert erp["captured_at"]
    graph = entries[mint_query_ref(SESSION, "g1")]
    assert graph["query_text"] == _CYPHER
    assert graph["system_id"] == ""  # bare name — honestly no gateway prefix
    # The record for the same call mints the SAME ref (D3 determinism).
    [prov] = runtime_app._provenance_events(state)
    refs = {r["tool_id"]: r["query_ref"] for r in prov["provenance"]["records"]}
    assert refs["t1"] == mint_query_ref(SESSION, "t1")


def test_legacy_sidecar_mode_a_nl_only_frame_absent():
    """Mode-A NL sub-questions are NOT query text (D6) ⇒ no frame at all."""
    state = _sidecar_state(tool_inputs={
        "t1": json.dumps({"question": "open work orders"}),
        "g1": "", "t2": json.dumps({"question": "scrap rate"}),
    })
    assert runtime_app._provenance_query_sidecar(state) == []


def test_legacy_sidecar_unparseable_input_omitted():
    """A 2 KB-truncated capture that no longer parses is honestly omitted —
    never guessed at (Tier 3 renders 'not captured')."""
    state = _sidecar_state(tool_inputs={
        "t1": '{"sql": "SELECT wo_id FROM wor',  # truncated mid-capture
        "g1": "", "t2": "",
    })
    assert runtime_app._provenance_query_sidecar(state) == []


def test_legacy_sidecar_failure_frame_absent(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("sidecar exploded")

    # The entries come from the ONE builder home (agents.provenance) — the
    # emitter's function-level import resolves it at call time.
    monkeypatch.setattr("agents.provenance.build_sidecar_entries", boom)
    assert runtime_app._provenance_query_sidecar(_sidecar_state()) == []


def test_invoke_emits_sidecar_after_provenance_frame():
    """Ordering pin (intelligence_summary → provenance → provenance_queries),
    inside the streaming try so AgentCore forwards it."""
    src = inspect.getsource(runtime_app._run_orchestrator_base)
    assert "_provenance_query_sidecar(s)" in src
    assert (src.index("_intelligence_summary_events(s)")
            < src.index("_provenance_events(s)")
            < src.index("_provenance_query_sidecar(s)")
            < src.index("except Exception as exc:"))


def test_enforce_sidecar_ordering_verbatim_and_last_delta_wins(monkeypatch):
    """Enforce mirror: sidecar frame between provenance and the ONE integrity
    marker; the LAST accumulated input delta wins (partial deltas re-enter
    _tool_use and overwrite); SQL verbatim, keyed by the record's ref."""
    invocation = _fed_invocation(monkeypatch)
    full_input = json.dumps({"sql": _SQL})
    # Accumulating deltas for the already-announced t1: partial, then complete.
    invocation.consume({"current_tool_use": {
        "name": "erp___query_erp", "toolUseId": "t1", "input": full_input[:12]}})
    invocation.consume({"current_tool_use": {
        "name": "erp___query_erp", "toolUseId": "t1", "input": full_input}})
    monkeypatch.setattr("agents.response_integrity.gate_final_result",
                        lambda result, context: ("answer text", _accepted_answer()))
    events = invocation.final_events()
    keys = [next(iter(e)) for e in events]
    assert keys == ["data", "intelligence_summary", "provenance",
                    "provenance_queries", "response_integrity"]
    entries = next(e for e in events if "provenance_queries" in e)["provenance_queries"]
    ref = mint_query_ref(SESSION, "t1")
    assert set(entries) == {ref}
    assert entries[ref]["query_text"] == _SQL
    assert entries[ref]["system_id"] == "erp"
    [record] = next(e for e in events if "provenance" in e)["provenance"]["records"]
    assert record["query_ref"] == ref
    # The D1 provenance frame itself stays input-free (D6 leak pin).
    assert "SELECT" not in json.dumps(next(e for e in events if "provenance" in e))
    assert sum(1 for e in events if "response_integrity" in e) == 1


def test_enforce_sidecar_absent_without_query_inputs(monkeypatch):
    """The NL/no-input fixture produces NO sidecar frame (honest absence) —
    and the exact-keys pin above stays the byte-level proof."""
    invocation = _fed_invocation(monkeypatch)  # input "{}" — no query field
    monkeypatch.setattr("agents.response_integrity.gate_final_result",
                        lambda result, context: ("answer text", _accepted_answer()))
    events = invocation.final_events()
    assert not any("provenance_queries" in e for e in events)


def test_executed_query_from_input_field_selection():
    """Query-bearing fields only (sql/query/cypher); NL question ⇒ '' —
    the ONE extractor home is agents.provenance; the
    runtime's strict JSON posture is the default (no literal-eval)."""
    from agents.provenance import executed_query_from_input as extract
    assert extract(json.dumps({"sql": _SQL})) == _SQL
    assert extract(json.dumps({"query": _CYPHER})) == _CYPHER
    assert extract(json.dumps({"cypher": _CYPHER})) == _CYPHER
    assert extract(json.dumps({"question": "open work orders"})) == ""
    assert extract(json.dumps({"sql": "   "})) == ""  # blank ⇒ not query text
    assert extract(json.dumps(["sql"])) == ""
    assert extract("") == ""
    assert extract(None) == ""
    # Python-literal capture needs the opt-in tolerance (chat_client's) —
    # the runtime default stays strict JSON.
    assert extract("{'sql': 'SELECT 1'}") == ""
    assert extract("{'sql': 'SELECT 1'}", literal_eval_fallback=True) == "SELECT 1"


# ---------------------------------------------------------------------------
# 50-line gate on every touched/new function (project invariant)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("func", [
    runtime_app._provenance_events,
    runtime_app._integrity_provenance_events,
    runtime_app._integrity_evidence_by_tool,
    runtime_app._provenance_query_sidecar,
    runtime_app._integrity_query_sidecar,
    provenance.build_sidecar_entries,
    provenance.executed_query_from_input,
    runtime_app._RuntimeIntegrityInvocation.__init__,
    runtime_app._RuntimeIntegrityInvocation._tool_use,
    runtime_app._RuntimeIntegrityInvocation._one_tool_result,
    runtime_app._RuntimeIntegrityInvocation.final_events,
])
def test_touched_functions_stay_under_the_50_line_gate(func):
    assert len(inspect.getsource(func).splitlines()) <= 50
