# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ADR-0066 Task 2B: metric-definition proposal lifecycle (offline)."""
from __future__ import annotations

import json

import pytest

import agents.metric_definition_store as metric_definition_store
import agents.metric_layer as metric_layer
import agents.self_learning as self_learning
import agents.vocabulary as vocabulary
from agents.metric_definition import canonical_json
from agents.proposal_key import _digest as key_digest


class ConditionalCheckFailedException(Exception):
    pass


class FakeTable:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict] = {}
        self.put_calls = 0
        self.update_calls = 0

    def put_item(self, Item: dict) -> None:
        self.put_calls += 1
        self.items[(Item["mapping_id"], Item["discovered_at"])] = dict(Item)

    def by_id(self, mapping_id: str) -> dict:
        return next(item for item in self.items.values()
                    if item.get("mapping_id") == mapping_id)

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

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues=None,
                    ExpressionAttributeNames=None, ConditionExpression=None,
                    **_kwargs) -> None:
        self.update_calls += 1
        item = self.items[(Key["mapping_id"], Key["discovered_at"])]
        names = ExpressionAttributeNames or {}
        values = ExpressionAttributeValues or {}
        if ConditionExpression is not None:
            assert ConditionExpression == "#s = :expected"
            if item.get(names.get("#s", "status")) != values[":expected"]:
                raise ConditionalCheckFailedException(Key["mapping_id"])
        expression = UpdateExpression.strip()
        set_part, remove_part = expression, ""
        if " REMOVE " in expression:
            set_part, remove_part = expression.split(" REMOVE ", 1)
        if set_part.upper().startswith("SET "):
            for clause in set_part[4:].split(","):
                lhs, rhs = (token.strip() for token in clause.split("="))
                item[names.get(lhs, lhs)] = values[rhs]
        for token in remove_part.split(",") if remove_part else ():
            item.pop(names.get(token.strip(), token.strip()), None)

    def scan(self, **_kwargs) -> dict:
        return {"Items": [dict(row) for row in self.items.values()]}


def _direct(metric_id="line-utilization", *, label="Line Utilization",
            concept="batch") -> dict:
    return {
        "id": metric_id,
        "kind": "direct",
        "label": label,
        "synonyms": ["LU"],
        "unit": "percent",
        "concept": concept,
        "aggregation": "avg",
        "max_exclusive": False,
    }


def _derived(metric_id="composed-rate", ref="runtime-hours") -> dict:
    return {
        "id": metric_id,
        "kind": "derived",
        "label": "Composed Rate",
        "synonyms": [],
        "unit": "ratio",
        "formula": {"op": "sum", "inputs": [{"metric": ref}]},
        "max_exclusive": False,
    }


def _log(doc: dict, *, source="agent", raw: str | None = None) -> str:
    return self_learning.log_proposed_mapping(
        "", "", "", "", "", doc["id"], "", 0.99,
        raw if raw is not None else json.dumps(doc),
        proposed_type="metric_definition", source=source,
    )


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


def _install_context(monkeypatch, definitions=None):
    definitions = definitions or {}
    monkeypatch.setattr(vocabulary, "neptune_query", lambda *_a, **_k: [])
    monkeypatch.setattr(metric_definition_store, "load_definitions",
                        lambda: {key: dict(value) for key, value in definitions.items()})
    monkeypatch.setattr(metric_layer, "invalidate_metric_layer", lambda: None,
                        raising=False)


@pytest.mark.parametrize("source", ["user", "agent", "conversation"])
@pytest.mark.parametrize("flag", [None, "0", "1", "true", "yes", "on"])
def test_metric_definition_never_auto_accepted_for_any_source_or_flag(
        monkeypatch, table, source, flag):
    if flag is None:
        monkeypatch.delenv("SELF_LEARNING_AUTO_ACCEPT", raising=False)
    else:
        monkeypatch.setenv("SELF_LEARNING_AUTO_ACCEPT", flag)
    monkeypatch.setattr(
        self_learning, "_AUTO_ACCEPT_TYPES",
        self_learning._AUTO_ACCEPT_TYPES + ("metric_definition",))
    accepted: list[str] = []
    monkeypatch.setattr(self_learning, "accept_mapping",
                        lambda mid: accepted.append(mid) or {"status": "accepted"})

    mapping_id = _log(_direct(), source=source)

    assert table.by_id(mapping_id)["status"] == "pending"
    assert accepted == []


def test_deny_tuples_pin_type_and_conversation_source():
    assert "metric_definition" in self_learning._NEVER_AUTO_ACCEPT_TYPES
    assert "conversation" in self_learning._NEVER_AUTO_ACCEPT_SOURCES


def test_deny_beats_allowlist_in_direct_and_sweep_paths(monkeypatch):
    monkeypatch.setenv("SELF_LEARNING_AUTO_ACCEPT", "1")
    monkeypatch.setattr(
        self_learning, "_AUTO_ACCEPT_TYPES",
        self_learning._AUTO_ACCEPT_TYPES + ("metric_definition",))
    item = {"mapping_id": "m1", "status": "pending", "source": "agent",
            "proposed_type": "metric_definition", "confidence": "1"}
    accepted: list[str] = []
    monkeypatch.setattr(self_learning, "accept_mapping",
                        lambda mid: accepted.append(mid) or {"status": "accepted"})
    monkeypatch.setattr(self_learning, "get_pending_mappings", lambda: [item])

    assert self_learning._maybe_auto_accept("m1", item) is False
    assert self_learning.sweep_auto_accept() == {"accepted": 0, "failed": 0}
    assert accepted == []


def test_c2_canonical_json_key_dedupe_and_edit_supersede(table):
    first_doc = _direct()
    pretty = json.dumps(first_doc, indent=2, sort_keys=False)
    first = _log(first_doc, source="user", raw=pretty)
    first_row = table.by_id(first)
    canonical = canonical_json(first_doc)
    subject = "metric_definition|line-utilization"
    assert first_row["to_value"] == "line-utilization"
    assert first_row["query_context"] == canonical
    assert first_row["subject_key"] == subject
    assert first_row["target_key"] == subject + ">>" + key_digest(canonical)
    assert all(not first_row.get(name) for name in (
        "from_system", "from_field", "to_system", "to_field", "concept"))

    duplicate = _log(dict(reversed(list(first_doc.items()))), source="user")
    assert duplicate == first
    assert len(table.items) == 1

    edited = _log(_direct(label="Utilization of Line"), source="user")
    assert edited != first
    assert table.by_id(first)["status"] == "superseded"
    assert table.by_id(edited)["status"] == "pending"
    assert table.by_id(edited)["version"] == "2"


def test_c2_to_value_must_match_document_id(table):
    doc = _direct()
    with pytest.raises(ValueError, match="to_value must equal the document id"):
        self_learning.log_proposed_mapping(
            "", "", "", "", "", "different-id", "", 0.9,
            json.dumps(doc), proposed_type="metric_definition", source="user")
    assert table.items == {}


@pytest.mark.parametrize("failure", ["invalid", "collision", "cycle"])
def test_invalid_colliding_and_cyclic_accept_raise_and_stay_pending(
        monkeypatch, table, failure):
    _install_context(monkeypatch)
    doc = _direct()
    if failure == "collision":
        doc = _direct(metric_id="runtime-hours")
    elif failure == "cycle":
        doc = _derived(metric_id="loop-metric", ref="loop-metric")
    mapping_id = _log(doc, source="user")
    if failure == "invalid":
        table.by_id(mapping_id)["query_context"] = "not-json"
    writes: list[dict] = []
    monkeypatch.setattr(metric_definition_store, "write_definition",
                        lambda *args, **kwargs: writes.append(kwargs) or "unexpected")

    with pytest.raises((ValueError, RuntimeError)):
        self_learning.accept_mapping(mapping_id, actor="steward")

    assert table.by_id(mapping_id)["status"] == "pending"
    assert writes == []


@pytest.mark.parametrize("clause, offending_field", [
    ({"concept": "batch", "op": "in", "values": [["x"]]}, "filter[0].values"),
    ({"concept": "batch", "op": "in", "values": [{"x": 1}]}, "filter[0].values"),
    ({"concept": "batch", "op": "eq", "value": 10 ** 400}, "filter[0].value"),
])
def test_malformed_filter_literal_accept_is_named_and_stays_pending(
        monkeypatch, table, clause, offending_field):
    """ADR-0075: the accept door re-parses; a malformed predicate literal is a
    NAMED ValueError (never a raw TypeError/OverflowError), nothing is written
    and the proposal stays pending."""
    _install_context(monkeypatch)
    mapping_id = _log(_direct(), source="user")
    bad = {**_direct(), "aggregation": "count", "filter": [clause]}
    table.by_id(mapping_id)["query_context"] = json.dumps(bad)
    writes: list[dict] = []
    monkeypatch.setattr(metric_definition_store, "write_definition",
                        lambda *args, **kwargs: writes.append(kwargs) or "unexpected")

    with pytest.raises(ValueError) as exc:
        self_learning.accept_mapping(mapping_id, actor="steward")

    assert offending_field in str(exc.value)
    assert table.by_id(mapping_id)["status"] == "pending"
    assert writes == []


def test_cross_definition_cycle_uses_graph_snapshot(monkeypatch, table):
    prior = _derived(metric_id="metric-b", ref="metric-a")
    proposed = _derived(metric_id="metric-a", ref="metric-b")
    _install_context(monkeypatch, {"metric-b": prior})
    mapping_id = _log(proposed, source="user")
    monkeypatch.setattr(metric_definition_store, "write_definition",
                        lambda *_a, **_k: pytest.fail("cycle must fail before write"))

    with pytest.raises(ValueError, match="definition cycle"):
        self_learning.accept_mapping(mapping_id)

    assert table.by_id(mapping_id)["status"] == "pending"


def test_readback_mismatch_raises_and_keeps_pending(monkeypatch, table):
    monkeypatch.setattr(vocabulary, "neptune_query", lambda *_a, **_k: [])
    monkeypatch.setattr(metric_layer, "invalidate_metric_layer", lambda: None,
                        raising=False)
    writes: list[tuple[str, dict]] = []

    def query(cypher, parameters=None):
        if "OPTIONAL MATCH" in cypher:
            return [{"id": None, "definition_json": None}]
        return [{"definition_json": canonical_json(_direct(label="Wrong"))}]

    monkeypatch.setattr(metric_definition_store, "neptune_query", query)
    monkeypatch.setattr(
        metric_definition_store, "neptune_write",
        lambda cypher, params=None: writes.append((cypher, params)) or True)
    mapping_id = _log(_direct(), source="user")

    with pytest.raises(RuntimeError, match="exact readback mismatch"):
        self_learning.accept_mapping(mapping_id, actor="steward")

    assert table.by_id(mapping_id)["status"] == "pending"
    assert len(writes) == 1


def test_valid_accept_exact_readback_invalidates_and_writes_no_guidance_text(
        monkeypatch, table):
    monkeypatch.setattr(vocabulary, "neptune_query", lambda *_a, **_k: [])
    definition = canonical_json(_direct())
    graph_writes: list[tuple[str, dict]] = []
    legacy_writes: list[tuple[str, dict]] = []
    invalidations: list[bool] = []

    def query(cypher, parameters=None):
        if "OPTIONAL MATCH" in cypher:
            return [{"id": None, "definition_json": None}]
        return [{"definition_json": definition}]

    monkeypatch.setattr(metric_definition_store, "neptune_query", query)
    monkeypatch.setattr(
        metric_definition_store, "neptune_write",
        lambda cypher, params=None: graph_writes.append((cypher, params)) or True)
    monkeypatch.setattr(self_learning, "_neptune_write",
                        lambda cypher, params=None: legacy_writes.append((cypher, params)) or True)
    monkeypatch.setattr(metric_layer, "invalidate_metric_layer",
                        lambda: invalidations.append(True), raising=False)
    mapping_id = _log(_direct(), source="user")

    result = self_learning.accept_mapping(mapping_id, actor="steward")

    assert result == {"status": "accepted", "neptune_node_id":
                      "metricdef:line-utilization",
                      "proposed_type": "metric_definition", "version": 1}
    assert table.by_id(mapping_id)["status"] == "accepted"
    assert invalidations == [True]
    assert len(graph_writes) == 1 and "MetricDefinition" in graph_writes[0][0]
    assert legacy_writes == []
    all_write_text = " ".join(cypher for cypher, _ in graph_writes + legacy_writes)
    assert "query_guidance" not in all_write_text


def test_preview_returns_computed_diff_validation_and_performs_zero_writes(
        monkeypatch, table):
    current = _direct(label="Old Label")
    proposed = _direct(label="New Label")
    _install_context(monkeypatch, {proposed["id"]: current})
    mapping_id = _log(proposed, source="user")
    graph_writes: list = []
    monkeypatch.setattr(self_learning, "_neptune_write",
                        lambda *args, **kwargs: graph_writes.append((args, kwargs)) or True)
    monkeypatch.setattr(metric_definition_store, "write_definition",
                        lambda *args, **kwargs: graph_writes.append((args, kwargs)) or "bad")
    counts_before = (table.put_calls, table.update_calls)

    preview = self_learning.preview_metric_definition_mapping(mapping_id)

    assert preview["current"] == current
    assert preview["proposed"] == proposed
    assert preview["diff"]["changed_fields"] == ["label"]
    assert preview["diff"]["formula"] == {
        "from": "avg(concept:batch)", "to": "avg(concept:batch)"}
    assert preview["validation"] == {"valid": True, "error": ""}
    assert preview["collision"] is False
    assert graph_writes == []
    assert (table.put_calls, table.update_calls) == counts_before


def test_preview_detects_code_collision_and_renders_current_formula(
        monkeypatch, table):
    _install_context(monkeypatch)
    proposed = _direct(metric_id="runtime-hours")
    mapping_id = _log(proposed, source="user")

    preview = self_learning.preview_metric_definition_mapping(mapping_id)

    assert preview["collision"] is True
    assert preview["current"]["id"] == "runtime-hours"
    assert preview["diff"]["formula"]["from"] == "sum(concept:runtime-hours)"
    assert preview["diff"]["formula"]["to"] == "avg(concept:batch)"
    assert preview["validation"]["valid"] is False
    assert "collides with a code-defined metric id" in preview["validation"]["error"]


def test_record_user_correction_refuses_metric_definition_at_the_door(
        monkeypatch, table):
    """The one-door invariant holds AT the door, not by caller discipline.

    ``record_user_correction`` is accepted-on-entry and dispatches generically
    through ``_apply_to_neptune``, which HAS a ``metric_definition`` arm — so the
    refusal must be behavioural, not merely an omitted branch. Pinned here: the
    call RAISES and performs zero DynamoDB and zero graph writes.
    """
    monkeypatch.setattr(metric_definition_store, "write_definition",
                        lambda *_a, **_k: pytest.fail("a formula was MERGEd"))
    monkeypatch.setattr(self_learning, "_neptune_write",
                        lambda *_a, **_k: pytest.fail("a graph write was issued"))
    monkeypatch.setattr(self_learning, "_apply_to_neptune",
                        lambda *_a, **_k: pytest.fail("the apply path was reached"))

    with pytest.raises(ValueError, match="propose → preview → accept"):
        self_learning.record_user_correction(
            "", "", "", "", "", _direct()["id"], "",
            proposed_type="metric_definition",
            query_context=canonical_json(_direct()),
        )

    assert table.items == {}
    assert (table.put_calls, table.update_calls) == (0, 0)


@pytest.mark.parametrize("spelling", [
    "metric_definition", "METRIC_DEFINITION", "  Metric_Definition  ",
])
def test_record_user_correction_refusal_survives_type_spelling(
        monkeypatch, table, spelling):
    monkeypatch.setattr(self_learning, "_apply_to_neptune",
                        lambda *_a, **_k: pytest.fail("the apply path was reached"))

    with pytest.raises(ValueError, match="ADR-0066"):
        self_learning.record_user_correction(
            "", "", "", "", "", "line-utilization", "",
            proposed_type=spelling)

    assert table.items == {}


def test_record_user_correction_still_accepts_its_normal_types(monkeypatch, table):
    """The refusal is scoped: the ordinary value-mapping correction is untouched."""
    monkeypatch.setattr(self_learning, "_apply_to_neptune",
                        lambda *_a, **_k: "vm:node")
    monkeypatch.setattr(self_learning, "_reset_accepted_coverage_cache", lambda: None)

    result = self_learning.record_user_correction(
        "erp", "plant", "P1", "mes", "site", "S1", "site")

    assert result["status"] == "accepted"
    assert result["proposed_type"] == "value_mapping"
    assert table.put_calls == 1
