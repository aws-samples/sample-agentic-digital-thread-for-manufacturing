# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline behavioral proof of the ADR-0050 vocabulary lifecycle extensions.

No AWS, no Neptune, no network. Mirrors ``test_self_learning_lifecycle.py``'s
doubles: an in-memory ``FakeTable`` wired via
``monkeypatch.setattr(self_learning, "_get_table", ...)`` and spies on the
``_neptune_write`` / ``_neptune_query`` module-global seams.

Covers:
  - ``concept_vocab``/``guidance_note`` are REGISTERED proposed_types (stored
    through ``log_proposed_mapping``) and excluded from
    ``schema_check.SCHEMA_CHECKED_TYPES`` by construction (allowlist);
  - ``_apply_concept_vocab`` add/edit/retire, incl. the mapped-retire
    ``ValueError`` guard (ledger stays pending);
  - ``_apply_guidance_note`` delegates to ``append_query_guidance_note`` (spy);
    a ``False`` return is a non-error no-op recorded via the node id;
  - ``assert_vocab``: admin → accepted + applied immediately; non-admin →
    pending, nothing applied/superseded; idempotent dedupe;
  - evidence at authoring: score recorded in ``confidence``/``evidence_score``,
    ``override_evidence`` recorded verbatim; ``score_assertion_evidence``
    reuses the existing helpers and swallows ALL failures to ``None``;
  - PINNING: an agent proposal colliding with an accepted user assertion lands
    pending with ``review_reason='conflicts-with-user-assertion'`` and never
    supersedes it.
"""
from __future__ import annotations

import json

import pytest

import agents.self_learning as self_learning
from tests.unit._value_mapping_write_seam import route_returning_writes
from agents.schema_check import SCHEMA_CHECKED_TYPES


# --------------------------------------------------------------------------- #
# In-memory test doubles (same shapes as test_self_learning_lifecycle.py)
# --------------------------------------------------------------------------- #
class FakeTable:
    def __init__(self) -> None:
        self._items: dict[tuple[str, str], dict] = {}

    def __len__(self) -> int:
        return len(self._items)

    def all_items(self) -> list[dict]:
        return [dict(v) for v in self._items.values()]

    def by_id(self, mapping_id: str) -> dict:
        for v in self._items.values():
            if v.get("mapping_id") == mapping_id:
                return dict(v)
        raise KeyError(mapping_id)

    def put_item(self, Item: dict) -> None:
        self._items[(Item["mapping_id"], Item["discovered_at"])] = dict(Item)

    def query(self, **kwargs) -> dict:
        index = kwargs.get("IndexName")
        values = kwargs.get("ExpressionAttributeValues", {})
        items = list(self._items.values())
        if index == "subject-key-index":
            result = [i for i in items if i.get("subject_key") == values[":sk"]]
        elif index == "status-index":
            result = [i for i in items if i.get("status") == values[":status"]]
        else:
            result = [i for i in items if i.get("mapping_id") == values[":mid"]]
        reverse = not kwargs.get("ScanIndexForward", True)
        result.sort(key=lambda x: x.get("discovered_at", ""), reverse=reverse)
        return {"Items": [dict(i) for i in result]}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues=None,
                    ExpressionAttributeNames=None, **_kwargs) -> None:
        item = self._items[(Key["mapping_id"], Key["discovered_at"])]
        names = ExpressionAttributeNames or {}
        values = ExpressionAttributeValues or {}
        expr = UpdateExpression.strip()
        if expr.upper().startswith("SET "):
            for clause in expr[4:].split(","):
                lhs, rhs = (s.strip() for s in clause.split("="))
                item[names.get(lhs, lhs)] = values[rhs]


class NeptuneWriteSpy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict | None]] = []

    def __call__(self, cypher: str, parameters: dict | None = None):
        self.calls.append((cypher, parameters))
        return True

    def find(self, fragment: str) -> list[tuple[str, dict | None]]:
        return [(c, p) for c, p in self.calls if fragment in c]


class NeptuneQueryStub:
    """Returns a canned row list for the MAPS_TO retire probe; [] otherwise."""

    def __init__(self, mapped_count: "int | None" = 0) -> None:
        self.mapped_count = mapped_count
        self.calls: list[tuple[str, dict | None]] = []

    def __call__(self, cypher: str, parameters: dict | None = None) -> list:
        self.calls.append((cypher, parameters))
        if "MAPS_TO" in cypher and "count(m)" in cypher:
            if self.mapped_count is None:
                return []  # indeterminate probe
            return [{"n": self.mapped_count}]
        return []


def _install(monkeypatch, table: FakeTable, *,
             mapped_count: "int | None" = 0) -> tuple[NeptuneWriteSpy, NeptuneQueryStub]:
    write = NeptuneWriteSpy()
    query = NeptuneQueryStub(mapped_count)
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)
    monkeypatch.setattr(self_learning, "_neptune_write", write)
    route_returning_writes(monkeypatch, write)
    monkeypatch.setattr(self_learning, "_neptune_query", query)
    monkeypatch.setattr(self_learning, "known_systems", lambda: [])
    return write, query


def _assert_concept(*, op="add", is_admin=True, name="Cycle Time",
                    concept="cycle_time", description="", synonyms=None,
                    **kwargs) -> dict:
    return self_learning.assert_vocab(
        proposed_type="concept_vocab", concept=concept, name=name,
        description=description, synonyms=synonyms, op=op,
        is_admin=is_admin, **kwargs,
    )


# --------------------------------------------------------------------------- #
# Registration / gate exclusion
# --------------------------------------------------------------------------- #
class TestRegistration:
    def test_vocab_types_excluded_from_schema_checked_allowlist(self):
        # SCHEMA_CHECKED_TYPES is an allowlist — exclusion by construction.
        assert "concept_vocab" not in SCHEMA_CHECKED_TYPES
        assert "guidance_note" not in SCHEMA_CHECKED_TYPES

    def test_concept_vocab_stores_through_log_proposed_mapping(self, monkeypatch):
        table = FakeTable()
        _install(monkeypatch, table)
        mid = self_learning.log_proposed_mapping(
            "", "", "", "", "", "Cycle Time",
            "cycle_time", 0.9, json.dumps({"op": "add"}),
            proposed_type="concept_vocab", source="user",
            provenance="human-asserted",
        )
        assert mid is not None
        item = table.by_id(mid)
        assert item["status"] == "pending"
        assert item["proposed_type"] == "concept_vocab"
        assert item["subject_key"].startswith("concept_vocab|cycle_time")

    def test_guidance_note_stores_through_log_proposed_mapping(self, monkeypatch):
        table = FakeTable()
        _install(monkeypatch, table)
        mid = self_learning.log_proposed_mapping(
            "erp", "", "", "", "", "Always filter by plant when joining orders.",
            "", 0.9, json.dumps({"op": "add"}),
            proposed_type="guidance_note", source="user",
            provenance="human-asserted",
        )
        assert mid is not None
        item = table.by_id(mid)
        assert item["status"] == "pending"
        assert item["guidance_scope"] == "query_hint"
        assert item["subject_key"].startswith("guidance_note|erp|query_hint|")

    def test_two_distinct_guidance_notes_coexist_on_one_system(self, monkeypatch):
        # Each distinct note is its own subject — accumulation, no supersede.
        table = FakeTable()
        _install(monkeypatch, table)
        a = self_learning.log_proposed_mapping(
            "erp", "", "", "", "", "note one", "", 0.9, "{}",
            proposed_type="guidance_note", source="user")
        b = self_learning.log_proposed_mapping(
            "erp", "", "", "", "", "note two", "", 0.9, "{}",
            proposed_type="guidance_note", source="user")
        assert a != b
        assert table.by_id(a)["status"] == "pending"
        assert table.by_id(b)["status"] == "pending"

    def test_never_auto_accepted(self):
        assert "concept_vocab" in self_learning._NEVER_AUTO_ACCEPT_TYPES
        assert "guidance_note" in self_learning._NEVER_AUTO_ACCEPT_TYPES


# --------------------------------------------------------------------------- #
# _apply_concept_vocab via accept_mapping
# --------------------------------------------------------------------------- #
class TestConceptVocabApply:
    def _pending(self, monkeypatch, table, *, op="add", synonyms=None,
                 description="", mapped_count=0):
        write, query = _install(monkeypatch, table, mapped_count=mapped_count)
        res = _assert_concept(op=op, is_admin=False, description=description,
                              synonyms=synonyms)
        assert res["status"] == "pending"
        return write, query, res["mapping_id"]

    def test_add_merges_concept_with_provenance(self, monkeypatch):
        table = FakeTable()
        write, _query, mid = self._pending(monkeypatch, table)
        result = self_learning.accept_mapping(mid, actor="steward@example.com")
        assert result["status"] == "accepted"
        assert result["neptune_node_id"] == "concept-cycle_time"
        merges = write.find("MERGE (c:Concept {id: $cid})")
        assert merges, "expected a Concept MERGE write"
        cypher, params = merges[0]
        assert "coalesce(c.source, 'custom')" in cypher
        assert "c.updated_at" in cypher and "c.approved_by" in cypher
        assert params["name"] == "Cycle Time"
        assert params["actor"] == "steward@example.com"
        assert table.by_id(mid)["status"] == "accepted"

    def test_edit_sets_description_and_synonyms_only_when_authored(self, monkeypatch):
        table = FakeTable()
        write, _query, mid = self._pending(
            monkeypatch, table, op="edit", description="Time per unit produced.",
            synonyms=["takt", "cycle-time"])
        self_learning.accept_mapping(mid, actor="steward@example.com")
        cypher, params = write.find("MERGE (c:Concept {id: $cid})")[0]
        assert "c.description = $desc" in cypher
        assert "c.synonyms = $syn" in cypher
        assert params["desc"] == "Time per unit produced."
        # Neptune openCypher takes no list property values: one JSON string.
        assert isinstance(params["syn"], str)
        assert json.loads(params["syn"]) == ["takt", "cycle-time"]
        assert all(not isinstance(v, (list, tuple, dict)) for v in params.values())

    def test_edit_without_description_never_blanks_it(self, monkeypatch):
        table = FakeTable()
        write, _query, mid = self._pending(monkeypatch, table, op="edit")
        self_learning.accept_mapping(mid)
        cypher, _params = write.find("MERGE (c:Concept {id: $cid})")[0]
        assert "c.description" not in cypher
        assert "c.synonyms" not in cypher

    def test_retire_unmapped_sets_soft_retired(self, monkeypatch):
        table = FakeTable()
        write, _query, mid = self._pending(monkeypatch, table, op="retire",
                                           mapped_count=0)
        result = self_learning.accept_mapping(mid, actor="steward@example.com")
        assert result["status"] == "accepted"
        retire_writes = write.find("c.retired = true")
        assert retire_writes, "expected the soft-retire SET"
        assert retire_writes[0][1]["cid"] == "cycle_time"

    def test_retire_mapped_raises_actionable_valueerror(self, monkeypatch):
        table = FakeTable()
        write, _query, mid = self._pending(monkeypatch, table, op="retire",
                                           mapped_count=3)
        with pytest.raises(ValueError, match=r"mapped to 3 field\(s\)"):
            self_learning.accept_mapping(mid)
        assert not write.find("c.retired = true")
        # Ledger stays pending — the accept never landed.
        assert table.by_id(mid)["status"] == "pending"

    def test_retire_indeterminate_probe_proceeds(self, monkeypatch):
        # _graph_rows_matched contract: [] → None → proceed (offline lane).
        table = FakeTable()
        write, _query, mid = self._pending(monkeypatch, table, op="retire",
                                           mapped_count=None)
        result = self_learning.accept_mapping(mid)
        assert result["status"] == "accepted"
        assert write.find("c.retired = true")

    def test_missing_concept_id_raises(self, monkeypatch):
        table = FakeTable()
        _install(monkeypatch, table)
        with pytest.raises(ValueError, match="concept id"):
            self_learning._apply_concept_vocab({"concept": ""}, "subj-x", 1)


def _deliver_guidance(table, item: dict, actor: str = "") -> dict:
    """Model the strict saga's terminal CAS for focused lifecycle tests."""
    extra = {
        "guidance_graph_digest": "sha256:graph",
        "guidance_delivery_digest": "sha256:delivery",
        "guidance_delivery_version": "3",
        "neptune_node_id": "guidance-test",
    }
    if actor:
        extra["accepted_by"] = actor
    self_learning._update_status(
        table, item["mapping_id"], item["discovered_at"], "accepted",
        extra, expect_status="pending")
    return {
        "status": "delivered", "outcome": "append",
        "graph_digest": "sha256:graph",
        "delivery_digest": "sha256:delivery", "delivery_version": 3,
        "preview": {"outcome": "append"},
    }


# --------------------------------------------------------------------------- #
# _apply_guidance_note via accept_mapping (delegation spy)
# --------------------------------------------------------------------------- #
class TestGuidanceNoteApply:
    def _pending_note(self, monkeypatch, table):
        _install(monkeypatch, table)
        res = self_learning.assert_vocab(
            proposed_type="guidance_note", system_id="erp",
            note="Always filter by plant.", is_admin=False)
        assert res["status"] == "pending"
        return res["mapping_id"]

    def test_accept_marks_terminal_only_after_strict_delivery(self, monkeypatch):
        table = FakeTable()
        mid = self._pending_note(monkeypatch, table)
        monkeypatch.setattr(
            self_learning, "_execute_guidance_item",
            lambda actual_table, item, actor, confirmation=None: (
                _deliver_guidance(actual_table, item, actor)))
        result = self_learning.accept_mapping(mid, actor="steward@example.com")
        assert result["status"] == "accepted"
        assert result["neptune_node_id"].startswith("guidance-")
        assert "noop" not in result["neptune_node_id"]
        row = table.by_id(mid)
        assert row["status"] == "accepted"
        assert row["guidance_delivery_digest"] == "sha256:delivery"

    def test_duplicate_stays_pending_and_never_emits_guidance_noop(self, monkeypatch):
        table = FakeTable()
        mid = self._pending_note(monkeypatch, table)
        monkeypatch.setattr(
            self_learning, "_execute_guidance_item",
            lambda _table, _item, _actor, confirmation=None: {
                "status": "pending", "outcome": "duplicate",
                "error": "guidance_update_failed", "http_status": 409,
            })
        result = self_learning.accept_mapping(mid)
        assert result["status"] == "pending"
        assert table.by_id(mid)["status"] == "pending"
        assert "neptune_node_id" not in table.by_id(mid)
        assert "guidance-noop-" not in repr(result)


# --------------------------------------------------------------------------- #
# assert_vocab entry point
# --------------------------------------------------------------------------- #
class TestAssertVocab:
    def test_admin_lands_accepted_and_applies_immediately(self, monkeypatch):
        table = FakeTable()
        write, _query = _install(monkeypatch, table)
        res = _assert_concept(is_admin=True, actor="admin@example.com")
        assert res["status"] == "accepted"
        assert res["neptune_node_id"] == "concept-cycle_time"
        item = table.by_id(res["mapping_id"])
        assert item["status"] == "accepted"
        assert item["source"] == "user"
        assert item["provenance"] == "human-asserted"
        assert item["method"] == "user_asserted"
        assert item["accepted_by"] == "admin@example.com"
        assert write.find("MERGE (c:Concept {id: $cid})")

    def test_non_admin_stays_pending_nothing_applied(self, monkeypatch):
        table = FakeTable()
        write, _query = _install(monkeypatch, table)
        res = _assert_concept(is_admin=False)
        assert res["status"] == "pending"
        item = table.by_id(res["mapping_id"])
        assert item["status"] == "pending"
        assert item["provenance"] == "human-asserted"
        assert item["source"] == "user"
        assert write.calls == []

    def test_admin_supersedes_prior_active_on_subject(self, monkeypatch):
        table = FakeTable()
        _install(monkeypatch, table)
        first = _assert_concept(is_admin=True, name="Cycle Time")
        second = _assert_concept(is_admin=True, name="Cycle Time (per unit)")
        assert table.by_id(first["mapping_id"])["status"] == "superseded"
        item = table.by_id(second["mapping_id"])
        assert item["status"] == "accepted"
        assert int(item["version"]) == 2

    def test_identical_active_assertion_dedupes(self, monkeypatch):
        table = FakeTable()
        _install(monkeypatch, table)
        first = _assert_concept(is_admin=True)
        again = _assert_concept(is_admin=True)
        assert again["mapping_id"] == first["mapping_id"]
        assert again["status"] == "accepted"
        assert len(table) == 1

    def test_admin_retire_mapped_persists_nothing(self, monkeypatch):
        table = FakeTable()
        _install(monkeypatch, table, mapped_count=2)
        with pytest.raises(ValueError, match="mapped to 2 field"):
            _assert_concept(op="retire", is_admin=True)
        assert len(table) == 0  # apply runs FIRST — refused retire leaves no row

    def test_guidance_note_admin_uses_pending_first_strict_delivery(self, monkeypatch):
        table = FakeTable()
        _install(monkeypatch, table)
        seen_statuses = []

        def delivered(actual_table, item, actor, confirmation=None):
            seen_statuses.append(item["status"])
            return _deliver_guidance(actual_table, item, actor)

        monkeypatch.setattr(self_learning, "_execute_guidance_item", delivered)
        res = self_learning.assert_vocab(
            proposed_type="guidance_note", system_id="mes",
            note="Report durations in minutes.", is_admin=True,
            actor="admin@example.com")
        assert res["status"] == "accepted"
        assert seen_statuses == ["pending"]
        assert table.by_id(res["mapping_id"])["status"] == "accepted"

    def test_validation_errors(self, monkeypatch):
        table = FakeTable()
        _install(monkeypatch, table)
        with pytest.raises(ValueError, match="concept id"):
            self_learning.assert_vocab(proposed_type="concept_vocab", concept="")
        with pytest.raises(ValueError, match="add\\|edit\\|retire"):
            _assert_concept(op="delete")
        with pytest.raises(ValueError, match="system_id and note"):
            self_learning.assert_vocab(proposed_type="guidance_note", system_id="",
                                       note="x")
        with pytest.raises(ValueError, match="does not handle"):
            self_learning.assert_vocab(proposed_type="value_mapping")


# --------------------------------------------------------------------------- #
# Description is part of the concept_vocab target
# digest (a description-only edit is a REAL change, never silently deduped);
# a name-less edit preserves the existing Concept name; guidance_note key
# normalization; the proposal_key recipe is consumed via the public seam.
# --------------------------------------------------------------------------- #
class TestDescriptionDigestAndNamePreservation:
    def test_description_only_edit_is_a_real_change_not_deduped(self, monkeypatch):
        table = FakeTable()
        write, _query = _install(monkeypatch, table)
        first = _assert_concept(is_admin=True,
                                description="Time per unit produced.")
        second = _assert_concept(is_admin=True,
                                 description="Elapsed time per finished unit.")
        assert second["mapping_id"] != first["mapping_id"]
        assert second["status"] == "accepted"
        assert table.by_id(first["mapping_id"])["status"] == "superseded"
        _cypher, params = write.find("MERGE (c:Concept {id: $cid})")[-1]
        assert params["desc"] == "Elapsed time per finished unit."

    def test_description_only_edit_preserves_existing_name(self, monkeypatch):
        table = FakeTable()
        write, _query = _install(monkeypatch, table)
        res = _assert_concept(is_admin=True, op="edit", name="",
                              description="Elapsed time per finished unit.")
        assert res["status"] == "accepted"
        cypher, params = write.find("MERGE (c:Concept {id: $cid})")[0]
        assert "c.name = coalesce(c.name, $cid)" in cypher
        assert "c.name = $name" not in cypher and "name" not in params
        assert params["desc"] == "Elapsed time per finished unit."

    def test_name_only_edit_leaves_description_untouched(self, monkeypatch):
        table = FakeTable()
        write, _query = _install(monkeypatch, table)
        _assert_concept(is_admin=True, op="edit", name="Cycle Time")
        cypher, params = write.find("MERGE (c:Concept {id: $cid})")[0]
        assert "c.name = $name" in cypher and params["name"] == "Cycle Time"
        assert "c.description" not in cypher and "desc" not in params


class TestVocabKeyRecipe:
    def test_concept_vocab_description_changes_target_not_subject(self):
        base = dict(concept="cycle_time", to_value="Cycle Time",
                    annotation='{"op": "edit"}')
        a = self_learning._vocab_canonical_keys(
            "concept_vocab", description="d1", **base)
        b = self_learning._vocab_canonical_keys(
            "concept_vocab", description="d2", **base)
        assert a.subject_key == b.subject_key
        assert a.target_key != b.target_key
        again = self_learning._vocab_canonical_keys(
            "concept_vocab", description="d1", **base)
        assert again.target_key == a.target_key  # idempotent re-assertion

    def test_guidance_note_keys_are_normalized(self):
        a = self_learning._vocab_canonical_keys(
            "guidance_note", from_system="ERP",
            to_value="Always filter by plant.")
        b = self_learning._vocab_canonical_keys(
            "guidance_note", from_system=" erp ",
            to_value="  ALWAYS FILTER BY PLANT.  ")
        assert a.subject_key == b.subject_key
        assert a.target_key == b.target_key

    def test_key_recipe_uses_proposal_key_public_seam(self):
        # the seam ALIASES proposal_key's primitives — never a copied recipe
        from agents import proposal_key
        assert self_learning.key_norm is proposal_key._norm
        assert self_learning.key_digest is proposal_key._digest


# --------------------------------------------------------------------------- #
# Evidence at authoring
# --------------------------------------------------------------------------- #
class TestEvidence:
    def test_score_and_override_recorded_on_item(self, monkeypatch):
        table = FakeTable()
        _install(monkeypatch, table)
        monkeypatch.setattr(self_learning, "score_assertion_evidence",
                            lambda concept, system, field: 0.72)
        res = _assert_concept(is_admin=False, evidence_system="erp",
                              evidence_field="orders.cycle_time",
                              override_evidence=True)
        assert res["evidence_score"] == 0.72
        item = table.by_id(res["mapping_id"])
        assert item["confidence"] == "0.72"
        assert item["evidence_score"] == "0.72"
        assert item["override_evidence"] == "true"

    def test_no_evidence_inputs_defaults_confidence(self, monkeypatch):
        table = FakeTable()
        _install(monkeypatch, table)
        res = _assert_concept(is_admin=False)
        assert res["evidence_score"] is None
        item = table.by_id(res["mapping_id"])
        assert item["confidence"] == "1.0"
        assert "evidence_score" not in item
        assert "override_evidence" not in item

    def test_score_assertion_evidence_reuses_existing_helpers(self, monkeypatch):
        import agents.catalog as catalog_mod
        import agents.concept_suggest as cs
        import agents.resolve_evidence as re_mod
        monkeypatch.setattr(catalog_mod.catalog, "get",
                            lambda sid: {"connection_type": "PostgreSQL",
                                         "schema": "public",
                                         "connection_config": {}})
        sampled: list[dict] = []
        monkeypatch.setattr(
            re_mod, "sample_field_values",
            lambda **kw: sampled.append(kw) or ["v-1", "v-2", "v-3"])
        monkeypatch.setattr(
            cs, "_peer_concept_populations",
            lambda sid: {"cycle_time": {"values": ["v-1", "v-2"],
                                        "systems": ["mes"]}})
        monkeypatch.setattr(
            re_mod, "value_overlap",
            lambda a, b: {"overlap_count": 2, "ratio": 1.0, "examples": [],
                          "a_tokens": 3, "b_tokens": 2})
        score = self_learning.score_assertion_evidence(
            "cycle_time", "erp", "orders.cycle_time")
        # ratio 1.0 → evidence_confidence cap (0.85) — real helper, not a copy.
        assert score == pytest.approx(0.85)
        assert sampled[0]["table"] == "orders"
        assert sampled[0]["field"] == "cycle_time"

    def test_score_swallows_all_failures_to_none(self, monkeypatch):
        import agents.catalog as catalog_mod
        monkeypatch.setattr(catalog_mod.catalog, "get",
                            lambda sid: (_ for _ in ()).throw(RuntimeError("boom")))
        assert self_learning.score_assertion_evidence(
            "cycle_time", "erp", "orders.cycle_time") is None
        # Missing inputs → None (no backend touched).
        assert self_learning.score_assertion_evidence("", "erp", "f") is None
        assert self_learning.score_assertion_evidence("c", "", "f") is None
        assert self_learning.score_assertion_evidence("c", "s", "") is None

    def test_score_none_when_no_samples_or_population(self, monkeypatch):
        import agents.catalog as catalog_mod
        import agents.concept_suggest as cs
        import agents.resolve_evidence as re_mod
        monkeypatch.setattr(catalog_mod.catalog, "get", lambda sid: {})
        monkeypatch.setattr(re_mod, "sample_field_values", lambda **kw: [])
        assert self_learning.score_assertion_evidence(
            "cycle_time", "erp", "orders.cycle_time") is None
        monkeypatch.setattr(re_mod, "sample_field_values", lambda **kw: ["v-1"])
        monkeypatch.setattr(cs, "_peer_concept_populations", lambda sid: {})
        assert self_learning.score_assertion_evidence(
            "cycle_time", "erp", "orders.cycle_time") is None


# --------------------------------------------------------------------------- #
# Pinning — accepted user assertions are never superseded by agent learning
# --------------------------------------------------------------------------- #
class TestPinning:
    def test_agent_concept_vocab_over_user_assertion_lands_pending_with_reason(
            self, monkeypatch):
        table = FakeTable()
        _install(monkeypatch, table)
        user = _assert_concept(is_admin=True, name="Cycle Time")
        mid = self_learning.log_proposed_mapping(
            "", "", "", "", "", "Machine Cycle Duration",
            "cycle_time", 0.8, json.dumps({"op": "edit"}),
            proposed_type="concept_vocab", source="agent",
        )
        assert mid is not None
        agent_item = table.by_id(mid)
        assert agent_item["status"] == "pending"
        assert agent_item["review_reason"] == "conflicts-with-user-assertion"
        # The user assertion is untouched — never superseded.
        assert table.by_id(user["mapping_id"])["status"] == "accepted"

    def test_agent_value_mapping_over_user_correction_pinned(self, monkeypatch):
        table = FakeTable()
        _install(monkeypatch, table)
        corr = self_learning.record_user_correction(
            "erp", "orders.customer_id", "c-100",
            "crm", "accounts.id", "acct-7", "customer")
        assert corr["status"] == "accepted"
        mid = self_learning.log_proposed_mapping(
            "erp", "orders.customer_id", "c-100",
            "crm", "accounts.id", "acct-9",
            "customer", 0.9, "ctx", proposed_type="value_mapping",
            source="agent")
        assert mid is not None
        item = table.by_id(mid)
        assert item["status"] == "pending"
        assert item["review_reason"] == "conflicts-with-user-assertion"
        accepted = [i for i in table.all_items()
                    if i.get("status") == "accepted"]
        assert len(accepted) == 1 and accepted[0]["source"] == "user"
