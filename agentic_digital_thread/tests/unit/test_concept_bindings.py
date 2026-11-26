# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Safety pins for the bounded, revision-pinned concept-binding graph read.

``agents/concept_bindings.py`` supplies the ADR-0067 shadow check's ONLY
affirmative completeness marker: a binding it labels ``complete`` is the sole
state that permits a ``violation`` verdict. A wrongly-claimed ``complete`` on a
read that was actually capped, partial, stale or malformed manufactures FALSE
violations — the precise false-positive failure mode this workstream exists to
correct. Uncertainty MUST degrade to ``capped``/``unresolved`` (verdict
``unknown``), and every failure returns the honest empty answer ``({}, "")``.

Everything here uses synthetic engine-neutral ids (``system-a``,
``records.status``, ``concept-alpha``) — no customer or domain literals.
"""
from __future__ import annotations

import ast
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agents import concept_authority, concept_bindings
from agents import response_integrity_concepts as ric
from agents.concept_bindings import (
    _FIELD_CONCEPTS_CYPHER, MAX_CONCEPTS_PER_FIELD, MAX_FIELDS_PER_READ,
    MAX_GRAPH_ROUND_TRIPS_PER_READ, MAX_SYSTEMS_PER_READ, REVISION_PREFIX,
    field_specs, read_field_concepts,
)
from agents.response_integrity_concepts import (
    CONCEPT_EVIDENCE_MISMATCH, VERDICT_SUPPORTED, VERDICT_UNKNOWN,
    VERDICT_VIOLATION,
)
from agents.response_integrity_contracts import (
    AnswerClaim, ClaimInput, ClaimKind, FinalAnswerDraft, IntegrityContext,
)
from agents.response_integrity_capture import capture_trusted_tool_result
from agents.response_integrity_validation import (
    capture_orchestrator_tool_result, system_answer_from_result,
    validate_final_answer,
)

# The GENUINE seam, captured at import time — the lane-wide conftest fixture
# monkeypatches ``concept_bindings._field_concepts_graph_read`` around every
# test, so the module attribute at test time is the offline stub. Grabbing it
# here lets tests exercise the real seam body with an injected ``query``.
_REAL_GRAPH_READ = concept_bindings._field_concepts_graph_read

UTC = timezone.utc
NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)

# Synthetic vocabulary only (engine-domain neutrality).
ALPHA = "concept-alpha"
BETA = "concept-beta"
SV = "sv-1"

_MODULE = (Path(__file__).resolve().parents[2] / "agents" / "concept_bindings.py")

_WRITE_KEYWORDS = re.compile(
    r"\b(MERGE|CREATE|SET|DELETE|DETACH|REMOVE|DROP)\b", re.IGNORECASE)


def _ev(system="system-a", field="records.status"):
    """Minimal evidence-shaped object for ``field_specs`` (duck-typed)."""
    return SimpleNamespace(system_id=system, physical_field=field)


_DEFAULT_COUNT = object()


def _row(system="system-a", field="records.status", version=SV,
         concepts=(ALPHA,), count=_DEFAULT_COUNT, **extra):
    concepts = list(concepts)
    row = {"system_id": system, "field": field, "schema_version": version,
           "concept_count": len(concepts) if count is _DEFAULT_COUNT else count,
           "concepts": concepts}
    row.update(extra)
    return row


def _patch_seam(monkeypatch, rows, log=None):
    """Route reads through the REAL seam body with an injected query client."""
    def query(cypher, params):
        if log is not None:
            log.append((cypher, params))
        return rows
    monkeypatch.setattr(
        concept_bindings, "_field_concepts_graph_read",
        lambda specs: _REAL_GRAPH_READ(specs, query=query))


# --- 1. ONE traversal home ----------------------------------------------------


def test_cypher_composes_the_single_traversal_home():
    assert _FIELD_CONCEPTS_CYPHER.count(
        concept_authority.FIELD_EVIDENCE_TRAVERSAL) == 1
    # The predicate genuinely flows into the executed text.
    assert "HAS_FIELD" in _FIELD_CONCEPTS_CYPHER


def test_no_inline_traversal_fragment_in_module_source():
    """FAILS if anyone re-spells the field-evidence traversal inline.

    Every relationship fragment of the predicate must be absent from the
    module's own string constants (docstrings excluded — prose may NAME the
    edges); the only way the fragments reach the cypher is composition of
    ``concept_authority.FIELD_EVIDENCE_TRAVERSAL``.
    """
    tree = ast.parse(_MODULE.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if (isinstance(node, (ast.Module, ast.FunctionDef,
                              ast.AsyncFunctionDef, ast.ClassDef))
                and body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            docstrings.add(id(body[0].value))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in docstrings):
            for fragment in ("HAS_SCHEMA", "HAS_FIELD", "MAPS_TO"):
                assert fragment not in node.value, (
                    f"inline traversal fragment {fragment!r} in module source: "
                    f"{node.value!r} — compose concept_authority."
                    "FIELD_EVIDENCE_TRAVERSAL instead")


def test_field_cap_never_exceeds_the_resolver_entry_cap():
    """8==8 today is a load-bearing coincidence: if the read's per-field cap
    ever exceeded the resolver's entry cap, resolve_evidence_concepts would
    silently truncate a COMPLETE list and manufacture false violations."""
    assert MAX_CONCEPTS_PER_FIELD <= ric.MAX_CONCEPTS_PER_ENTRY


# --- 2. bounded read, pinned revision ------------------------------------------


def test_cypher_carries_an_explicit_per_field_cap():
    assert f"[0..{MAX_CONCEPTS_PER_FIELD}]" in _FIELD_CONCEPTS_CYPHER


def test_field_specs_are_bounded_and_deduplicated():
    evidence = [_ev(field=f"records.col_{i}") for i in range(MAX_FIELDS_PER_READ + 20)]
    assert len(field_specs(evidence)) == MAX_FIELDS_PER_READ

    fanout = [_ev(system=f"system-{i}", field="records.status")
              for i in range(MAX_SYSTEMS_PER_READ + 5)]
    specs = field_specs(fanout)
    assert len({spec["system"] for spec in specs}) == MAX_SYSTEMS_PER_READ

    dupes = [_ev(), _ev(field="RECORDS.STATUS"), _ev(field="records.status")]
    assert len(field_specs(dupes)) == 1


def test_unidentifiable_evidence_yields_no_spec():
    assert field_specs([_ev(system="")]) == ()
    assert field_specs([_ev(field="")]) == ()
    # A bare, undotted field cannot identify a graph Field unambiguously.
    assert field_specs([_ev(field="status")]) == ()


def test_revision_is_pinned_and_opaque(monkeypatch):
    _patch_seam(monkeypatch, [_row()])
    mappings, revision = read_field_concepts([_ev()])
    assert mappings == {("system-a", "records.status"):
                        {"concepts": [ALPHA], "complete": True}}
    assert revision.startswith(REVISION_PREFIX)
    assert len(revision) > len(REVISION_PREFIX)


def test_revision_changes_when_a_schema_version_changes(monkeypatch):
    _patch_seam(monkeypatch, [_row(version="sv-1")])
    _, first = read_field_concepts([_ev()])
    concept_bindings.reset()
    _patch_seam(monkeypatch, [_row(version="sv-2")])
    _, second = read_field_concepts([_ev()])
    assert first and second and first != second


def test_row_without_schema_version_cannot_pin_anything(monkeypatch):
    _patch_seam(monkeypatch, [_row(version="")])
    assert read_field_concepts([_ev()]) == ({}, "")


# --- 3. complete is affirmative; at/over cap is capped --------------------------


def test_full_enumeration_under_the_cap_is_complete(monkeypatch):
    concepts = [f"concept-{i}" for i in range(MAX_CONCEPTS_PER_FIELD - 1)]
    _patch_seam(monkeypatch, [_row(concepts=concepts)])
    mappings, _ = read_field_concepts([_ev()])
    assert mappings[("system-a", "records.status")]["complete"] is True


@pytest.mark.parametrize("count,returned", [
    # exactly AT the cap: truncation is indistinguishable from exact
    # enumeration at the boundary — must degrade to capped, never complete.
    (MAX_CONCEPTS_PER_FIELD, MAX_CONCEPTS_PER_FIELD),
    (MAX_CONCEPTS_PER_FIELD + 1, MAX_CONCEPTS_PER_FIELD),   # over the cap
])
def test_at_or_over_cap_is_capped_never_complete(monkeypatch, count, returned):
    concepts = [f"concept-{i}" for i in range(returned)]
    _patch_seam(monkeypatch, [_row(concepts=concepts, count=count)])
    mappings, revision = read_field_concepts([_ev()])
    entry = mappings[("system-a", "records.status")]
    assert entry["complete"] is False
    assert revision  # capped is still a usable (support-capable) mapping


@pytest.mark.parametrize("count", [3, None, "not-a-number", -1])
def test_untrustworthy_enumeration_count_is_capped(monkeypatch, count):
    """A count that mismatches, is absent, malformed or negative can never
    earn the affirmative marker."""
    _patch_seam(monkeypatch, [_row(concepts=[ALPHA, BETA], count=count)])
    mappings, _ = read_field_concepts([_ev()])
    assert mappings[("system-a", "records.status")]["complete"] is False


# --- 4. every uncertainty path -> ({}, ""), never a raise -----------------------


def test_missing_graph_client_returns_empty(monkeypatch):
    monkeypatch.setattr(
        concept_bindings, "_field_concepts_graph_read",
        lambda specs: (_ for _ in ()).throw(ImportError("no graph client")))
    assert read_field_concepts([_ev()]) == ({}, "")


def test_none_returning_client_returns_empty(monkeypatch):
    _patch_seam(monkeypatch, None)  # neptune_query's failure contract
    assert read_field_concepts([_ev()]) == ({}, "")


def test_graph_error_returns_empty(monkeypatch):
    monkeypatch.setattr(
        concept_bindings, "_field_concepts_graph_read",
        lambda specs: (_ for _ in ()).throw(RuntimeError("graph down")))
    assert read_field_concepts([_ev()]) == ({}, "")


def test_timeout_returns_empty_and_opens_the_breaker(monkeypatch):
    monkeypatch.setattr(concept_bindings, "READ_TIMEOUT_SECONDS", 0.05)
    calls = []

    def slow(specs):
        calls.append(1)
        time.sleep(0.3)
        return [_row()]

    monkeypatch.setattr(concept_bindings, "_field_concepts_graph_read", slow)
    assert read_field_concepts([_ev()]) == ({}, "")
    assert len(calls) == 1
    # Breaker open: a sick graph is not re-dialled.
    assert read_field_concepts([_ev()]) == ({}, "")
    assert len(calls) == 1
    # reset() recovers; give the single worker time to drain the sleeper.
    concept_bindings.reset()
    monkeypatch.setattr(concept_bindings, "READ_TIMEOUT_SECONDS", 5.0)
    _patch_seam(monkeypatch, [_row()])
    mappings, revision = read_field_concepts([_ev()])
    assert mappings and revision


def test_empty_result_set_returns_empty(monkeypatch):
    _patch_seam(monkeypatch, [])
    assert read_field_concepts([_ev()]) == ({}, "")


@pytest.mark.parametrize("rows", [
    ["not-a-mapping"],
    [{"field": "records.status", "schema_version": SV,
      "concept_count": 1, "concepts": [ALPHA]}],          # system missing
    [_row(system="")],                                    # system blank
    [_row(field="")],                                     # field blank
    [{"system_id": "system-a", "schema_version": SV,
      "concept_count": 1, "concepts": [ALPHA]}],          # field missing
    [_row(concepts=["", "   "], count=2)],                # concept ids blank
    [{"system_id": "system-a", "field": "records.status",
      "schema_version": SV, "concept_count": 1,
      "concepts": "not-a-list"}],                         # malformed column
    [_row(concepts=[], count=0)],                         # zero-concept row
])
def test_malformed_rows_return_empty(monkeypatch, rows):
    _patch_seam(monkeypatch, rows)
    assert read_field_concepts([_ev()]) == ({}, "")


def test_unresolvable_evidence_never_touches_the_graph(monkeypatch):
    calls = []
    monkeypatch.setattr(concept_bindings, "_field_concepts_graph_read",
                        lambda specs: calls.append(1) or [])
    assert read_field_concepts([_ev(field="status")]) == ({}, "")
    assert read_field_concepts([]) == ({}, "")
    assert calls == []


def test_malformed_row_never_poisons_a_good_sibling(monkeypatch):
    _patch_seam(monkeypatch, [
        _row(),                                # good
        _row(field="records.other", version=""),   # unpinnable — dropped
        "garbage",
    ])
    mappings, revision = read_field_concepts([_ev(), _ev(field="records.other")])
    assert set(mappings) == {("system-a", "records.status")}
    assert revision


# --- 5. round trips per turn -----------------------------------------------------


def test_one_round_trip_covers_many_fields_and_caches(monkeypatch):
    assert MAX_GRAPH_ROUND_TRIPS_PER_READ == 1
    log = []
    rows = [_row(), _row(field="records.other", concepts=[BETA])]
    _patch_seam(monkeypatch, rows, log)
    evidence = [_ev(), _ev(field="records.other")]
    mappings, _ = read_field_concepts(evidence)
    assert len(mappings) == 2
    assert len(log) == 1                       # ONE batched query, no per-field loop
    assert concept_bindings.round_trips() == 1
    read_field_concepts(evidence)              # cache hit
    assert len(log) == 1
    assert concept_bindings.round_trips() == 1


# --- 6. read-only ----------------------------------------------------------------


def test_cypher_constant_is_read_only():
    assert not _WRITE_KEYWORDS.search(_FIELD_CONCEPTS_CYPHER), (
        _WRITE_KEYWORDS.search(_FIELD_CONCEPTS_CYPHER))


def test_every_issued_cypher_is_read_only(monkeypatch):
    log = []
    _patch_seam(monkeypatch, [_row()], log)
    read_field_concepts([_ev()])
    assert log
    for cypher, params in log:
        assert cypher == _FIELD_CONCEPTS_CYPHER
        assert not _WRITE_KEYWORDS.search(cypher)
        assert set(params) == {"specs"}


# --- 7. end-to-end through the real validation seam --------------------------------
#
# These are the non-inertness pins: bindings arrive FILLED through the real
# ``validate_final_answer`` -> ``_shadow_concept_check`` ->
# ``concept_bindings.read_field_concepts`` chain, so a mislabelled turn reaches
# ``violation``, the same rows correctly labelled reach ``supported``, and a
# graph failure reaches ``unknown``.


def _provenance(payload):
    projections, schema = [], {}
    for index, column in enumerate(payload["columns"]):
        if column.startswith("__adt_source_"):
            source, data_type = f"observed_{index}", "timestamp"
        else:
            source = column
            values = [row[index] for row in payload["rows"] if index < len(row)]
            data_type = "varchar" if any(isinstance(v, str) for v in values) else "integer"
        schema[source] = data_type
        projections.append(f"{source} AS {column}")
    return {
        "executed_query": f"SELECT {', '.join(projections)} FROM records",  # nosec B608 - test fixture
        "physical_schema": {"records": {"relation_kind": "base_table", "columns": schema}},
    }


def _system_answer(concept=""):
    field = "status"
    payload = {"columns": [field, f"__adt_source_time__{field}"],
               "rows": [["ACTIVE", NOW.isoformat()]], "row_count": 1}
    state = {}
    capture_trusted_tool_result(
        {"content": [{"json": payload}]}, state, system_id="system-a",
        boundary="sql", tool_name="query_system_a",
        queried_at=NOW + timedelta(seconds=30), **_provenance(payload),
    )
    from agents.response_integrity_contracts import SystemAnswerDraft
    draft = SystemAnswerDraft(claims=(AnswerClaim(
        claim_id="c1", kind=ClaimKind.VALUE, label="Reported status",
        proposed_concept_id=concept, value="ACTIVE",
        inputs=(ClaimInput(label="v", value="ACTIVE", semantic_field=field,
                           locator="row:0"),),
    ),))
    answer = system_answer_from_result(SimpleNamespace(structured_output=draft), state)
    assert answer is not None and answer.claims and answer.evidence
    # The e2e pins rest on capture emitting the dotted physical spelling the
    # graph read is keyed on.
    assert answer.evidence[0].physical_field == "records.status"
    return answer


def _context(answer):
    context = IntegrityContext("What was the recorded status yesterday?")
    capture_orchestrator_tool_result(
        "system-a___query", {"content": [{"text": answer.model_dump_json()}]},
        context,
    )
    return context


def _final_draft(answer, concept):
    evidence = answer.evidence[0]
    return FinalAnswerDraft(claims=(AnswerClaim(
        claim_id="f1", kind=ClaimKind.VALUE, label="Reported status",
        proposed_concept_id=concept, value="ACTIVE",
        inputs=(ClaimInput(label="v", value="ACTIVE",
                           evidence_id=evidence.evidence_id,
                           semantic_field="status", locator="row:0"),),
    ),))


def test_end_to_end_mislabelled_turn_reaches_violation(monkeypatch):
    """THE non-inertness pin: filled bindings convict a mislabelled claim.

    The claim proposes ``concept-beta`` while its exact cited evidence field
    maps positively and COMPLETELY to ``concept-alpha`` — the reproduced
    label-vs-provenance defect. Before the graph hook this could only ever be
    ``unknown``.
    """
    log = []
    _patch_seam(monkeypatch, [_row(concepts=[ALPHA])], log)
    answer = _system_answer(concept=BETA)
    context = _context(answer)
    final = validate_final_answer(_final_draft(answer, concept=BETA), context)
    assert context.concept_shadow is not None
    entry = context.concept_shadow["claims"][0]
    assert entry["verdict"] == VERDICT_VIOLATION
    assert entry["reason"] == CONCEPT_EVIDENCE_MISMATCH
    assert entry["claimed_concept"] == BETA
    assert entry["evidence_concepts"] == [ALPHA]
    assert context.concept_shadow["violations"] == 1
    # Shadow only: the turn itself is untouched.
    assert final.claims
    # The turn spent exactly one read-only graph round trip.
    assert len(log) == 1
    assert concept_bindings.round_trips() == 1
    assert not _WRITE_KEYWORDS.search(log[0][0])


def test_end_to_end_identical_rows_correctly_labelled_are_supported(monkeypatch):
    _patch_seam(monkeypatch, [_row(concepts=[ALPHA])])
    answer = _system_answer(concept=ALPHA)
    context = _context(answer)
    final = validate_final_answer(_final_draft(answer, concept=ALPHA), context)
    entry = context.concept_shadow["claims"][0]
    assert entry["verdict"] == VERDICT_SUPPORTED
    assert context.concept_shadow["violations"] == 0
    assert final.claims


def test_end_to_end_graph_failure_is_unknown_never_violation():
    """No seam re-patch: the lane-wide conftest stub raises, exactly like a
    dead graph. Bindings stay unresolved and the verdict is honestly unknown."""
    answer = _system_answer(concept=BETA)
    context = _context(answer)
    final = validate_final_answer(_final_draft(answer, concept=BETA), context)
    assert context.concept_shadow is not None
    assert context.concept_shadow["claims"][0]["verdict"] == VERDICT_UNKNOWN
    assert context.concept_shadow["violations"] == 0
    assert final.claims


def test_end_to_end_capped_binding_is_unknown_never_violation(monkeypatch):
    """A field at its enumeration cap must degrade the mislabel verdict to
    unknown — the cap can hide the claimed concept."""
    concepts = [f"concept-{i}" for i in range(MAX_CONCEPTS_PER_FIELD)]
    _patch_seam(monkeypatch, [_row(concepts=concepts,
                                   count=MAX_CONCEPTS_PER_FIELD)])
    answer = _system_answer(concept=BETA)
    context = _context(answer)
    validate_final_answer(_final_draft(answer, concept=BETA), context)
    assert context.concept_shadow["claims"][0]["verdict"] == VERDICT_UNKNOWN
