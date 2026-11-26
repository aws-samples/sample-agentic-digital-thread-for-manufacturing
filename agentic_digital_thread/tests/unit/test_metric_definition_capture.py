# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ADR-0066 D5 — ask-then-learn CAPTURE: the orchestrator tool that turns the
USER's own answer to an unresolved-metric-term question into a PENDING proposal.

Fully offline (no AWS, no sockets, no LLM). This file pins the honesty contract
of the loop's closing half:

* **Pending, never applied** — filing rides the ONE ADR-0003 lifecycle with
  source ``conversation``; BOTH deny arms hold (``metric_definition`` in
  ``_NEVER_AUTO_ACCEPT_TYPES``, ``conversation`` in
  ``_NEVER_AUTO_ACCEPT_SOURCES``), and the sweep accepts nothing even when the
  type is forced into the allowlist.
* **Structured only** — an invalid document is NEVER filed; the exact validator
  message (naming the offending field) goes back to the model instead. Prose is
  never filed as a formula (ADR-0066 D9).
* **A synonym answer is not a metric** — it routes to a ``concept_vocab``
  synonym edit through the same lifecycle door, carrying existing synonyms
  forward, and fails CLOSED when they cannot be read.
* **No authority before acceptance** — a captured definition resolves NOWHERE
  (composed layer, store snapshot, metric-term pre-check) until a steward
  accepts it, and the prompt rule forbids answering from it.

Fixture ids/terms are NEUTRAL placeholders (the neutrality ratchet scans tests).
"""
from __future__ import annotations

import inspect
import json

import pytest

import agents.metric_definition_store as metric_definition_store
import agents.metric_layer as metric_layer
import agents.metric_term_resolution as metric_term_resolution
import agents.orchestrator as orch
import agents.self_learning as self_learning
import agents.vocabulary as vocabulary
from agents.metric_definition import canonical_json

# The in-process tool under test, unwrapped from the Strands @tool decorator.
CAPTURE = orch.propose_metric_definition
_CALL = CAPTURE._tool_func if hasattr(CAPTURE, "_tool_func") else CAPTURE

_TERM = "LU"
_METRIC_ID = "line-utilization"
_CONCEPT = "batch"


# --------------------------------------------------------------------------- #
# Fakes — the house per-file FakeTable (proposal store) + graph seams
# --------------------------------------------------------------------------- #
class FakeTable:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict] = {}
        self.put_calls = 0

    def put_item(self, Item: dict) -> None:
        self.put_calls += 1
        self.items[(Item["mapping_id"], Item["discovered_at"])] = dict(Item)

    def by_id(self, mapping_id: str) -> dict:
        return next(item for item in self.items.values()
                    if item.get("mapping_id") == mapping_id)

    def rows(self, proposed_type: str) -> list[dict]:
        return [dict(row) for row in self.items.values()
                if row.get("proposed_type") == proposed_type]

    def query(self, **kwargs) -> dict:
        index = kwargs.get("IndexName")
        values = kwargs.get("ExpressionAttributeValues", {})
        rows = list(self.items.values())
        if index == "subject-key-index":
            rows = [row for row in rows if row.get("subject_key") == values[":sk"]]
        elif index == "status-index":
            rows = [row for row in rows if row.get("status") == values[":status"]]
        else:
            rows = [row for row in rows if row.get("mapping_id") == values[":mid"]]
        rows.sort(key=lambda row: row.get("discovered_at", ""),
                  reverse=not kwargs.get("ScanIndexForward", True))
        return {"Items": [dict(row) for row in rows]}

    def update_item(self, **_kwargs) -> None:  # pragma: no cover - not exercised
        raise AssertionError("the capture tool must never update a row")

    def scan(self, **_kwargs) -> dict:
        return {"Items": [dict(row) for row in self.items.values()]}


@pytest.fixture
def table(monkeypatch) -> FakeTable:
    fake = FakeTable()
    monkeypatch.setattr(self_learning, "_get_table", lambda: fake)
    monkeypatch.setattr(self_learning, "PROPOSAL_STAGING_MIN_COREFERENCES", 1)
    return fake


@pytest.fixture(autouse=True)
def _clean_metric_snapshot():
    metric_definition_store.invalidate()
    yield
    metric_definition_store.invalidate()


@pytest.fixture(autouse=True)
def _graph_seams(monkeypatch):
    """No graph: core concepts only, no graph-resident definitions."""
    monkeypatch.setattr(vocabulary, "neptune_query", lambda *_a, **_k: [])
    monkeypatch.setattr(metric_definition_store, "load_definitions", lambda: {})
    monkeypatch.setattr(metric_definition_store, "neptune_query",
                        lambda *_a, **_k: [])
    monkeypatch.setattr(metric_layer, "invalidate_metric_layer", lambda: None,
                        raising=False)


def _direct_doc(metric_id: str = _METRIC_ID, *, concept: str = _CONCEPT,
                synonyms: list[str] | None = None) -> dict:
    doc = {
        "id": metric_id,
        "kind": "direct",
        "label": "Line Utilization",
        "unit": "percent",
        "concept": concept,
        "aggregation": "avg",
        "valid_range": [0, 100],
        "max_exclusive": False,
    }
    if synonyms is not None:
        doc["synonyms"] = synonyms
    return doc


_UNSET = object()


def _capture(term: str = _TERM, doc: object = _UNSET,
             synonym_of_concept: str = "") -> str:
    if doc is _UNSET:
        doc = "" if synonym_of_concept else _direct_doc()
    if doc is None or isinstance(doc, str):
        payload = doc or ""
    else:
        payload = json.dumps(doc)
    return _CALL(term=term, definition_json=payload,
                 synonym_of_concept=synonym_of_concept)


# --------------------------------------------------------------------------- #
# 1. The happy path files a PENDING metric_definition with source=conversation
# --------------------------------------------------------------------------- #
def test_capture_files_one_pending_row_with_conversation_source(table):
    result = _capture()

    rows = table.rows("metric_definition")
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "pending"
    assert row["source"] == "conversation"
    assert row["provenance"] == "conversation-captured"
    # C2 item-field mapping: metric id in to_value, every system/field slot empty.
    assert row["to_value"] == _METRIC_ID
    assert all(row.get(field, "") == ""
               for field in ("from_system", "from_field", "from_value",
                             "to_system", "to_field", "concept"))
    # The filed bytes are the CANONICAL document (the digest/readback form).
    filed = json.loads(row["query_context"])
    assert row["query_context"] == canonical_json(filed)
    assert filed["id"] == _METRIC_ID
    # Honest status relayed to the model: filed, pending, not active.
    assert "FILED FOR STEWARD REVIEW — NOT YET ACTIVE" in result
    assert "pending steward review" in result
    assert row["mapping_id"] in result


def test_capture_records_the_asked_term_as_a_synonym(table):
    """Without this the accepted definition would leave the very term that
    triggered the question unresolved — the loop would not close."""
    _capture()
    filed = json.loads(table.rows("metric_definition")[0]["query_context"])
    assert _TERM in filed["synonyms"]


def test_capture_never_duplicates_an_already_listed_synonym(table):
    _capture(doc=_direct_doc(synonyms=[_TERM, "line util"]))
    filed = json.loads(table.rows("metric_definition")[0]["query_context"])
    assert filed["synonyms"] == [_TERM, "line util"]


def test_capture_is_idempotent_for_the_same_document(table):
    first = _capture()
    second = _capture()
    assert len(table.rows("metric_definition")) == 1
    assert table.rows("metric_definition")[0]["mapping_id"] in first
    assert "pending steward review" in second


# --------------------------------------------------------------------------- #
# 2. Never auto-accepted — BOTH deny arms, and the sweep proves it
# --------------------------------------------------------------------------- #
def test_both_deny_arms_cover_a_conversational_capture():
    assert "metric_definition" in self_learning._NEVER_AUTO_ACCEPT_TYPES
    assert "conversation" in self_learning._NEVER_AUTO_ACCEPT_SOURCES
    assert orch._CAPTURE_SOURCE == "conversation"


@pytest.mark.parametrize("flag", [None, "1", "true", "on"])
def test_captured_row_is_never_auto_accepted(monkeypatch, table, flag):
    if flag is None:
        monkeypatch.delenv("SELF_LEARNING_AUTO_ACCEPT", raising=False)
    else:
        monkeypatch.setenv("SELF_LEARNING_AUTO_ACCEPT", flag)
    # Force the type into the ALLOWLIST: the deny tuples must still win.
    monkeypatch.setattr(
        self_learning, "_AUTO_ACCEPT_TYPES",
        self_learning._AUTO_ACCEPT_TYPES + ("metric_definition",))
    accepted: list[str] = []
    monkeypatch.setattr(self_learning, "accept_mapping",
                        lambda mid, **_k: accepted.append(mid) or {"status": "accepted"})

    _capture()
    mapping_id = table.rows("metric_definition")[0]["mapping_id"]
    self_learning.sweep_auto_accept()

    assert accepted == []
    assert table.by_id(mapping_id)["status"] == "pending"


def test_the_tool_holds_no_accept_path():
    for func in (_CALL, orch._capture_metric_definition, orch._capture_document,
                 orch._capture_concept_synonym, orch._synonym_target_error):
        src = inspect.getsource(func)
        assert "accept_mapping" not in src
        assert "is_admin" not in src
        assert "_apply_to_neptune" not in src


# --------------------------------------------------------------------------- #
# 3. Validation errors come BACK to the model; nothing is filed
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("payload,needle", [
    # Free-text prose — the model must never file it as a formula.
    ("good units divided by total units at the line", "id"),
    # A prose blob shoved into the formula slot.
    (json.dumps({"id": "prose-metric", "kind": "derived", "label": "Prose",
                 "formula": "good units divided by total units"}), "formula"),
    # Bad id charset.
    (json.dumps(_direct_doc("Line Utilization")), "id"),
    # Unknown concept reference (cross-reference validation).
    (json.dumps(_direct_doc(concept="not-a-known-concept")), "concept"),
    # Closed vocabulary: the aggregation must be a known one.
    (json.dumps({**_direct_doc(), "aggregation": "median-ish"}), "aggregation"),
    # Self-referential derived definition (cycle detection).
    (json.dumps({"id": "cyclic-metric", "kind": "derived", "label": "Cyclic",
                 "formula": {"op": "sum", "inputs": [{"metric": "cyclic-metric"}]}}),
     "cycle"),
])
def test_invalid_document_returns_the_exact_error_and_files_nothing(
        table, payload, needle):
    result = _capture(doc=payload)

    assert table.put_calls == 0
    assert result.startswith("CAPTURE REJECTED — NOTHING FILED:")
    assert needle in result
    assert "pending steward review" not in result


def test_code_metric_collision_is_rejected_not_filed(table):
    code_id = sorted(m.id for m in metric_layer.CodeMetricLayer().list_metrics())[0]
    result = _capture(doc=_direct_doc(code_id))

    assert table.put_calls == 0
    assert "CAPTURE REJECTED — NOTHING FILED:" in result
    assert "code" in result


def test_the_returned_error_is_the_validator_message_verbatim(table):
    from agents.metric_definition import MetricDefinitionError, parse_definition

    payload = json.dumps(_direct_doc("Line Utilization"))
    with pytest.raises(MetricDefinitionError) as exc:
        parse_definition(payload)

    assert str(exc.value) in _capture(doc=payload)
    assert table.put_calls == 0


@pytest.mark.parametrize("term", ["", "   ", "x" * 65])
def test_unusable_term_files_nothing(table, term):
    result = _capture(term=term)
    assert table.put_calls == 0
    assert "NOTHING FILED" in result


@pytest.mark.parametrize("term,filed", [
    ("bell\x07term", "bellterm"),
    ("\x1b[31mLU", "[31mLU"),
    ("  LU  ", "LU"),
])
def test_hostile_term_text_is_sanitized_before_it_reaches_a_row(table, term, filed):
    """ADR-0054 — the SAME sanitize-then-validate treatment the C1 text fields
    get: control characters never reach the proposal row."""
    _capture(term=term)
    synonyms = json.loads(table.rows("metric_definition")[0]["query_context"])["synonyms"]
    assert synonyms == [filed]
    assert not any(ord(ch) < 32 for entry in synonyms for ch in entry)


@pytest.mark.parametrize("kwargs", [
    {"doc": None, "synonym_of_concept": ""},
    {"doc": _direct_doc(), "synonym_of_concept": _CONCEPT},
])
def test_exactly_one_definition_form_is_required(table, kwargs):
    result = _capture(**kwargs)
    assert table.put_calls == 0
    assert "EXACTLY ONE" in result


def test_a_filing_failure_never_claims_a_capture(monkeypatch, table):
    def _boom(*_a, **_k):
        raise RuntimeError("proposal store unavailable")

    monkeypatch.setattr(self_learning, "log_proposed_mapping", _boom)
    result = _capture()
    assert "CAPTURE FAILED" in result
    assert "FILED FOR STEWARD REVIEW" not in result


# --------------------------------------------------------------------------- #
# 4. A synonym answer routes to concept_vocab, not to a formula
# --------------------------------------------------------------------------- #
def test_synonym_answer_files_a_concept_vocab_row(monkeypatch, table):
    monkeypatch.setattr(vocabulary, "neptune_query",
                        lambda *_a, **_k: [{"synonyms": ["work order batch"]}])

    result = _capture(synonym_of_concept=_CONCEPT)

    assert table.rows("metric_definition") == []
    rows = table.rows("concept_vocab")
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "pending"
    assert row["source"] == "conversation"
    assert row["provenance"] == "conversation-captured"
    assert row["concept"] == _CONCEPT
    context = json.loads(row["query_context"])
    assert context["op"] == "edit"
    # Existing synonyms are carried FORWARD (the apply SETs the whole list).
    assert context["synonyms"] == ["work order batch", _TERM]
    assert "pending steward review" in result


def test_synonym_answer_is_also_deny_armed():
    assert "concept_vocab" in self_learning._NEVER_AUTO_ACCEPT_TYPES


def test_already_known_synonym_files_nothing(monkeypatch, table):
    monkeypatch.setattr(vocabulary, "neptune_query",
                        lambda *_a, **_k: [{"synonyms": [_TERM.lower()]}])
    result = _capture(synonym_of_concept=_CONCEPT)
    assert table.put_calls == 0
    assert "ALREADY KNOWN — NOTHING FILED" in result


def test_unknown_concept_target_files_nothing(table):
    result = _capture(synonym_of_concept="not-a-known-concept")
    assert table.put_calls == 0
    assert "not a known" in result


def test_metric_id_target_is_redirected_not_filed(table):
    from agents.catalog import CORE_CONCEPTS

    # A code metric id that is NOT also a concept id (several concepts and
    # metrics deliberately share a name — those resolve as concepts).
    metric_only = sorted(m.id for m in metric_layer.CodeMetricLayer().list_metrics()
                         if m.id not in CORE_CONCEPTS)[0]
    result = _capture(synonym_of_concept=metric_only)
    assert table.put_calls == 0
    assert "METRIC id" in result


def test_synonym_read_failure_fails_closed(monkeypatch, table):
    def _boom(*_a, **_k):
        raise RuntimeError("graph unavailable")

    # The concept universe still resolves; only the synonym read fails.
    monkeypatch.setattr(self_learning, "_metric_definition_context",
                        lambda: (frozenset({_CONCEPT}), frozenset(), frozenset(), {}))
    monkeypatch.setattr(vocabulary, "neptune_query", _boom)

    result = _capture(synonym_of_concept=_CONCEPT)
    assert table.put_calls == 0
    assert "CAPTURE FAILED — NOTHING FILED" in result


# --------------------------------------------------------------------------- #
# 5. A captured definition has NO authority until acceptance
# --------------------------------------------------------------------------- #
def test_captured_definition_resolves_nowhere_before_acceptance(table):
    """The pending row must not reach ANY read path: the composed metric layer,
    the store snapshot, or the metric-term pre-check."""
    question = f"What is the {_TERM} rate for the plant?"
    assert _TERM in metric_term_resolution.unresolved_metric_terms(question)

    _capture()
    assert table.rows("metric_definition")[0]["status"] == "pending"

    # No graph node was written, so no read path can see the definition.
    assert _METRIC_ID not in metric_definition_store.load_definitions()
    with pytest.raises(KeyError):
        metric_layer.get_metric_layer().get_metric(_METRIC_ID)
    assert _METRIC_ID not in {m.id for m in metric_layer.get_metric_layer().list_metrics()}
    # The pre-check still reports the term as unresolved — the honest answer.
    assert _TERM in metric_term_resolution.unresolved_metric_terms(question)
    assert _TERM.lower() not in metric_term_resolution.known_metric_vocabulary()


def test_capture_writes_nothing_to_the_graph(monkeypatch, table):
    def _tripwire(*_a, **_k):
        raise AssertionError("the capture tool must never write to the graph")

    monkeypatch.setattr(metric_definition_store, "write_definition", _tripwire)
    monkeypatch.setattr(metric_definition_store, "neptune_write", _tripwire)
    monkeypatch.setattr(self_learning, "_neptune_write_loud", _tripwire)

    assert "FILED FOR STEWARD REVIEW" in _capture()


def test_prompt_rule_forbids_answering_from_a_captured_definition():
    rule = orch._METRIC_TERM_CAPTURE_RULE
    assert "MUST NOT use it to compute, estimate, or\npresent a number" in rule
    assert "pending steward review" in rule
    assert "not in this turn and not in any later turn" in rule
    assert "NEVER pass prose as a\nformula" in rule
    for mode in ("agent", "orchestrator"):
        assert rule in orch._build_orchestrator_prompt(mode)


# --------------------------------------------------------------------------- #
# 6. Registration + the reserved-argument rule
# --------------------------------------------------------------------------- #
def _tool_names(tools):
    return {getattr(t, "tool_name", getattr(t, "__name__", "")) for t in tools}


@pytest.mark.parametrize("mode", ["agent", "orchestrator"])
def test_tool_is_registered_in_both_modes(monkeypatch, mode):
    from types import SimpleNamespace

    monkeypatch.setenv("SYSTEM_QUERY_MODE", mode)
    monkeypatch.setattr(
        orch, "get_config",
        lambda: SimpleNamespace(has_gateway=False, agentcore_gateway_url=None))
    monkeypatch.setattr(orch, "_mcp_clients", {})
    assert "propose_metric_definition" in _tool_names(orch._get_orchestrator_tools())


def test_reserved_identity_argument_is_not_in_the_signature():
    # ADR-0049 — `_adt_identity` is a RESERVED tool-call argument name.
    params = inspect.signature(_CALL).parameters
    assert "_adt_identity" not in params
    assert set(params) == {"term", "definition_json", "synonym_of_concept"}
