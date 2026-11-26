# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline tests for the onboarding wizard's concept suggestions panel
(``POST /api/onboard/reconcile`` and the ``POST /api/onboard/remap`` it feeds).

After a system is provisioned the wizard asks for fields in OTHER systems that
may belong to a concept THIS system's onboarding added. Pinned here:

* the model sees each candidate's real Field id (``<system>.<table>.<column>``)
  and its current tag, never a bare ``<system>: <column>`` label;
* candidates are other systems' fields only, never already mapped to that
  concept, bounded by a stable cap, with considered/sent counts reported;
* only concepts this system's onboarding created are offered; none means no
  model call and no suggestions (and so does a request without a system id);
* the model's output is validated: an unknown field id, an unknown concept, a
  bad role or the field's current tag is dropped; reasoning is passed through;
* remap reports the TRUE write count and the field ids it could not write, and
  its IdentityRule update stays inside the field's own system;
* provision records which system created a Concept, ON CREATE only.

Every graph and model seam is a fake: no AWS, no graph, no Bedrock. Fixture
ids are neutral (``system-a``, ``table_x``, ``concept-n``).
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

import agents.onboard as onboard
from agents.catalog import catalog
from agents.models import RemapRequest

_ONBOARD_SRC = Path(onboard.__file__).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class _FakeGraph:
    """Just enough of the graph for the reconcile reads and the remap write.

    ``fields`` rows: ``{id, name, system_id, schema_id, tags: [(concept, role)]}``;
    ``concepts`` rows: ``{id, name, description, created_by_system}``."""

    def __init__(self):
        self.concepts: list[dict] = []
        self.fields: list[dict] = []
        self.removing: set[str] = set()
        self.queries: list[tuple[str, dict]] = []
        self.writes: list[tuple[str, dict]] = []

    def concept(self, cid, *, by=None, run=None, name=None, description=""):
        self.concepts.append({"id": cid, "name": name or cid,
                              "description": description,
                              "created_by_system": by,
                              "created_by_run": run})

    def field(self, system_id, table, column, tags=()):
        self.fields.append({"id": f"{system_id}.{table}.{column}", "name": column,
                            "system_id": system_id,
                            "schema_id": f"{system_id}.{table}",
                            "tags": list(tags)})

    def _field(self, fid):
        return next((f for f in self.fields if f["id"] == fid), None)

    def _concept(self, cid):
        return next((c for c in self.concepts if c["id"] == cid), None)

    def query(self, cypher, params=None):
        params = dict(params or {})
        self.queries.append((cypher, params))
        if "removal_incomplete" in cypher:
            return [{"sid": s} for s in sorted(self.removing)]
        if "MATCH (f:Field)" in cypher:
            if "{role: 'identifier'}" in cypher:
                # The weak-field read: fields with no identifier-role edge.
                return [{"field_id": f["id"], "field_name": f["name"],
                         "system_id": f["system_id"]}
                        for f in self.fields
                        if not any(r == "identifier" for _c, r in f["tags"])]
            sid = params.get("sid")
            return [{"field_id": f["id"], "field_name": f["name"],
                     "system_id": f["system_id"], "schema_id": f["schema_id"],
                     "tags": ([{"concept": c, "role": r} for c, r in f["tags"]]
                              or [{"concept": None, "role": None}])}
                    for f in self.fields if f["system_id"] != sid]
        if "MATCH (c:Concept)" in cypher:
            if "created_by_run" in cypher:
                return [{"id": c["id"], "name": c["name"],
                         "description": c["description"]}
                        for c in self.concepts
                        if c["created_by_run"] is not None
                        and c["created_by_run"] == params.get("run")]
            if "created_by_system" in cypher:
                return [{"id": c["id"], "name": c["name"],
                         "description": c["description"]}
                        for c in self.concepts
                        if c["created_by_system"] == params.get("sid")]
            return [{"id": c["id"], "name": c["name"]} for c in self.concepts]
        raise AssertionError(f"unexpected graph read: {cypher}")

    def write(self, cypher, params=None):
        self.writes.append((cypher, dict(params or {})))
        return True

    def write_returning(self, cypher, params=None):
        params = dict(params or {})
        self.writes.append((cypher, params))
        if "UNWIND $rows" in cypher and "MAPS_TO" in cypher:
            out = []
            for r in params["rows"]:
                f, c = self._field(r["fid"]), self._concept(r["cid"])
                if f and c:
                    f["tags"].append((c["id"], r["role"]))
                    out.append({"field_id": f["id"], "concept_id": c["id"],
                                "system_id": f["system_id"], "field_name": f["name"],
                                "schema_id": f["schema_id"]})
            return out
        return []


class _FakeModel:
    def __init__(self, reply=None):
        self.prompts: list[str] = []
        self.reply = reply if reply is not None else []

    def __call__(self, prompt, *_a, **_k):
        self.prompts.append(prompt)
        return json.dumps(self.reply)


class _StateMiddleware(BaseHTTPMiddleware):
    """Stand-in for AuthMiddleware: an integrator session."""

    async def dispatch(self, request, call_next):
        request.state.is_admin = False
        request.state.is_integrator = True
        return await call_next(request)


@pytest.fixture
def graph(monkeypatch):
    g = _FakeGraph()
    monkeypatch.setattr(onboard, "neptune_query", g.query)
    monkeypatch.setattr(onboard, "neptune_write", g.write)
    monkeypatch.setattr(onboard, "neptune_write_returning", g.write_returning)
    return g


@pytest.fixture
def model(monkeypatch):
    m = _FakeModel()
    monkeypatch.setattr(onboard, "bedrock_call", m)
    return m


@pytest.fixture(autouse=True)
def _neutral_catalog():
    prior = list(catalog._systems)
    catalog._systems = [
        {"id": "system-a", "name": "System A", "plant": "Site 1",
         "status": "onboarded", "system_type": "CMMS"},
        {"id": "system-b", "name": "System B", "plant": "Site 2",
         "status": "onboarded", "system_type": "MES"},
        {"id": "system-c", "name": "System C", "plant": "Site 3",
         "status": "onboarded", "system_type": "ERP"},
    ]
    try:
        yield
    finally:
        catalog._systems = prior


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(onboard.router)
    app.add_middleware(_StateMiddleware)
    return TestClient(app)


def _reconcile(body=None):
    client = _client()
    if body is None:
        resp = client.post("/api/onboard/reconcile")
    else:
        resp = client.post("/api/onboard/reconcile", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _story(graph):
    """system-a was just onboarded (run RUN_ID) and created concept-n;
    system-b and system-c hold fields that may belong to it."""
    graph.concept("concept-n", by="system-a", run=RUN_ID, name="Concept N",
                  description="what concept-n means")
    graph.concept("concept-old", by=None, name="Concept Old")
    graph.field("system-a", "table_own", "col_own")
    graph.field("system-b", "table_x", "col_q")
    graph.field("system-c", "table_y", "col_r", tags=[("concept-old", "measure")])


RUN_ID = "run-0001"


def _sys(system_id="system-a", run_id=RUN_ID) -> dict:
    """The reconcile body the wizard sends: the system just onboarded and the
    id of the provision run whose concepts the panel compares against."""
    return {"system_id": system_id, "run_id": run_id}


def _suggestion(field_id, concept_id="concept-n", role="identifier",
                confidence=0.8, reasoning="same values"):
    return {"field_id": field_id, "concept_id": concept_id, "role": role,
            "confidence": confidence, "reasoning": reasoning}


# --------------------------------------------------------------------------- #
# Bug 1: the model never saw real Field ids, so its ids matched nothing.
# --------------------------------------------------------------------------- #
def test_prompt_lists_each_candidate_by_its_real_field_id(graph, model):
    _story(graph)
    _reconcile(_sys("system-a"))
    assert len(model.prompts) == 1
    prompt = model.prompts[0]
    assert "system-b.table_x.col_q" in prompt
    assert "system-c.table_y.col_r" in prompt


def test_a_suggestion_naming_a_field_id_that_was_not_sent_is_dropped(graph, model):
    _story(graph)
    model.reply = [_suggestion("system-b: col_q"),
                   _suggestion("system-b.table_x.col_q")]
    out = _reconcile(_sys("system-a"))
    assert [s["field_id"] for s in out["suggestions"]] == ["system-b.table_x.col_q"]


def test_remap_counts_only_what_the_write_matched(graph):
    _story(graph)
    body = RemapRequest(remaps=[
        {"field_id": "system-b: col_q", "concept_id": "concept-n",
         "role": "identifier", "confidence": 0.8},
    ])
    out = onboard.onboard_remap(body)
    assert out["remapped"] == 0
    assert out["not_written"] == ["system-b: col_q"]
    assert out["written"] == []


def test_remap_reports_each_written_field_and_writes_one_statement(graph):
    _story(graph)
    body = RemapRequest(remaps=[
        {"field_id": "system-b.table_x.col_q", "concept_id": "concept-n",
         "role": "identifier", "confidence": 0.8},
        {"field_id": "system-b.table_x.gone", "concept_id": "concept-n",
         "role": "measure", "confidence": 0.7},
    ])
    out = onboard.onboard_remap(body)
    assert out["remapped"] == 1
    assert out["written"] == ["system-b.table_x.col_q"]
    assert out["not_written"] == ["system-b.table_x.gone"]
    maps_to_writes = [c for c, _p in graph.writes if "MAPS_TO" in c]
    assert len(maps_to_writes) == 1


def test_remap_keeps_the_fields_other_tags(graph):
    """Remap is MERGE-only: the field's existing tag stays beside the new one."""
    _story(graph)
    out = onboard.onboard_remap(RemapRequest(remaps=[
        {"field_id": "system-c.table_y.col_r", "concept_id": "concept-n",
         "role": "identifier", "confidence": 0.8},
    ]))
    assert out["remapped"] == 1
    assert not any("DELETE" in c for c, _p in graph.writes)
    field = graph._field("system-c.table_y.col_r")
    assert ("concept-old", "measure") in field["tags"]
    assert ("concept-n", "identifier") in field["tags"]


def test_remap_reports_a_refused_write_as_an_error(graph, monkeypatch):
    _story(graph)
    monkeypatch.setattr(onboard, "neptune_write_returning", lambda *_a, **_k: None)
    out = onboard.onboard_remap(RemapRequest(remaps=[
        {"field_id": "system-b.table_x.col_q", "concept_id": "concept-n",
         "role": "identifier", "confidence": 0.8},
    ]))
    assert out["remapped"] == 0
    assert out["not_written"] == ["system-b.table_x.col_q"]
    assert out["error"]


# --------------------------------------------------------------------------- #
# Bug 2: candidates were every weak field anywhere, against every concept.
# --------------------------------------------------------------------------- #
def test_the_onboarded_systems_own_fields_are_not_candidates(graph, model):
    _story(graph)
    _reconcile(_sys("system-a"))
    assert "col_own" not in model.prompts[0]


def test_a_field_already_mapped_to_the_new_concept_is_not_a_candidate(graph, model):
    _story(graph)
    graph.field("system-b", "table_x", "col_tagged", tags=[("concept-n", "measure")])
    _reconcile(_sys("system-a"))
    assert "col_tagged" not in model.prompts[0]


def test_a_field_tagged_with_another_concept_is_sent_with_that_tag(graph, model):
    _story(graph)
    _reconcile(_sys("system-a"))
    line = next(ln for ln in model.prompts[0].splitlines()
                if "system-c.table_y.col_r" in ln)
    assert "concept-old" in line and "measure" in line
    untagged = next(ln for ln in model.prompts[0].splitlines()
                    if "system-b.table_x.col_q" in ln)
    assert "untagged" in untagged


def test_only_concepts_this_system_created_are_offered(graph, model):
    _story(graph)
    model.reply = [_suggestion("system-b.table_x.col_q", concept_id="concept-old"),
                   _suggestion("system-b.table_x.col_q", concept_id="concept-n")]
    out = _reconcile(_sys("system-a"))
    assert [s["concept_id"] for s in out["suggestions"]] == ["concept-n"]
    assert out["new_concepts"] == [{"id": "concept-n", "name": "Concept N"}]


def test_candidates_are_capped_in_a_stable_order_and_counted(graph, model):
    graph.concept("concept-n", by="system-a", run=RUN_ID)
    for i in range(onboard.RECONCILE_MAX_CANDIDATES + 7):
        graph.field("system-b", "table_x", f"col_{i:03d}")
    graph.fields.reverse()  # graph row order must not decide what is sent
    out = _reconcile(_sys("system-a"))
    assert out["candidates_considered"] == onboard.RECONCILE_MAX_CANDIDATES + 7
    assert out["candidates_sent"] == onboard.RECONCILE_MAX_CANDIDATES
    sent = [ln for ln in model.prompts[0].splitlines() if "system-b.table_x.col_" in ln]
    assert len(sent) == onboard.RECONCILE_MAX_CANDIDATES
    assert "system-b.table_x.col_000" in sent[0]


def test_candidates_whose_names_share_words_with_the_concept_come_first(graph, model):
    graph.concept("widget-count", by="system-a", run=RUN_ID, name="Widget count")
    for i in range(onboard.RECONCILE_MAX_CANDIDATES):
        graph.field("system-b", "table_x", f"col_{i:03d}")
    graph.field("system-c", "table_z", "widget_total")
    out = _reconcile(_sys("system-a"))
    assert out["candidates_considered"] == onboard.RECONCILE_MAX_CANDIDATES + 1
    assert "system-c.table_z.widget_total" in model.prompts[0]


def test_generic_audit_columns_are_not_candidates(graph, model):
    _story(graph)
    graph.field("system-b", "table_x", "created_at")
    _reconcile(_sys("system-a"))
    assert "created_at" not in model.prompts[0]


def test_fields_of_a_system_being_removed_are_not_candidates(graph, model):
    _story(graph)
    graph.removing.add("system-c")
    out = _reconcile(_sys("system-a"))
    assert "system-c.table_y.col_r" not in model.prompts[0]
    assert out["candidates_considered"] == 1


def test_no_system_id_returns_no_suggestions_and_no_model_call(graph, model):
    _story(graph)
    out = _reconcile()
    assert out["suggestions"] == []
    assert out["reason"] == "no_system_id"
    assert model.prompts == []


def test_no_new_concepts_for_this_system_means_no_model_call(graph, model):
    _story(graph)
    out = _reconcile(_sys("system-b", run_id="run-other"))
    assert out["suggestions"] == []
    assert out["reason"] == "no_new_concepts"
    assert model.prompts == []


def test_no_candidates_means_no_model_call(graph, model):
    graph.concept("concept-n", by="system-a", run=RUN_ID)
    graph.field("system-a", "table_own", "col_own")
    out = _reconcile(_sys("system-a"))
    assert out["suggestions"] == []
    assert out["reason"] == "no_candidates"
    assert model.prompts == []


# --------------------------------------------------------------------------- #
# Output validation + reasoning pass-through (bug 3's server half).
# --------------------------------------------------------------------------- #
def test_bad_roles_current_tags_and_duplicates_are_dropped(graph, model):
    _story(graph)
    graph.field("system-b", "table_x", "col_tagged", tags=[("concept-n", "measure")])
    model.reply = [
        _suggestion("system-b.table_x.col_q", role="owner"),
        _suggestion("system-b.table_x.col_tagged", role="measure"),
        _suggestion("system-b.table_x.col_q", role="measure"),
        _suggestion("system-b.table_x.col_q", role="identifier"),
        "not a dict",
    ]
    out = _reconcile(_sys("system-a"))
    assert [(s["field_id"], s["role"]) for s in out["suggestions"]] == [
        ("system-b.table_x.col_q", "measure")]


def test_a_suggestion_carries_reasoning_and_what_the_ui_shows(graph, model):
    _story(graph)
    model.reply = [_suggestion("system-c.table_y.col_r", confidence=85,
                               reasoning="values line up with concept-n ids")]
    out = _reconcile(_sys("system-a"))
    (s,) = out["suggestions"]
    assert s["reasoning"] == "values line up with concept-n ids"
    assert s["system_id"] == "system-c"
    assert s["system_name"] == "System C"
    assert s["table"] == "table_y"
    assert s["column"] == "col_r"
    assert s["concept_name"] == "Concept N"
    assert s["confidence"] == pytest.approx(0.85)
    assert s["current_tags"] == [{"concept_id": "concept-old", "role": "measure"}]
    # The suggestion adds a tag; it never asks remap to rewrite rules.
    assert "old_concept" not in s


def test_unparseable_model_output_returns_no_suggestions(graph, monkeypatch):
    _story(graph)
    monkeypatch.setattr(onboard, "bedrock_call", lambda *_a, **_k: "no json here")
    out = _reconcile(_sys("system-a"))
    assert out["suggestions"] == []


# --------------------------------------------------------------------------- #
# Bug 6: the old_concept IdentityRule update matched rules by bare field name
# in EVERY system.
# --------------------------------------------------------------------------- #
def _identity_rule_writes(graph):
    return [(c, p) for c, p in graph.writes if "IdentityRule" in c]


def test_remap_old_concept_rule_update_stays_in_the_fields_system(graph):
    _story(graph)
    graph.concept("concept-prev", by=None)
    onboard.onboard_remap(RemapRequest(remaps=[
        {"field_id": "system-b.table_x.col_q", "concept_id": "concept-n",
         "role": "identifier", "confidence": 0.8, "old_concept": "concept-prev"},
    ]))
    (cypher, params), = _identity_rule_writes(graph)
    assert "ir.source_system" in cypher
    assert "system-b" in json.dumps(params)


def test_remap_skips_the_rule_update_for_a_field_it_did_not_write(graph):
    _story(graph)
    onboard.onboard_remap(RemapRequest(remaps=[
        {"field_id": "system-b.table_x.gone", "concept_id": "concept-n",
         "role": "identifier", "confidence": 0.8, "old_concept": "concept-prev"},
    ]))
    assert _identity_rule_writes(graph) == []


# --------------------------------------------------------------------------- #
# Provenance: provision records which system created a Concept, ON CREATE only.
# --------------------------------------------------------------------------- #
def _concept_merge_literals() -> list[str]:
    """Every string-literal Cypher statement in onboard.py that MERGEs a
    Concept node, except the ValueMapping concept link: that fragment is
    used only by the steward's ValueMapping accept (``concept_link=True`` in
    self_learning), never by an onboarding."""
    tree = ast.parse(_ONBOARD_SRC)
    excluded = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "_VALUE_MAPPING_CONCEPT_LINK"
                for t in node.targets):
            excluded.update(id(n) for n in ast.walk(node.value))
    out = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in excluded
                and re.search(r"MERGE \(c:Concept \{id:", node.value)):
            out.append(node.value)
    return out


def test_every_onboarding_concept_merge_records_its_creator_on_create():
    merges = _concept_merge_literals()
    assert len(merges) >= 6
    for stmt in merges:
        on_create = stmt.split("ON CREATE SET", 1)
        assert len(on_create) == 2, stmt
        assert "c.created_by_system = $created_by_system" in on_create[1], stmt
        assert "c.created_at = $created_at" in on_create[1], stmt
        # Never a plain SET: an existing concept keeps its creator.
        assert "SET c.created_by_system" not in on_create[0], stmt


def test_emergent_concept_landing_carries_the_onboarding_system(monkeypatch):
    import agents.self_learning as self_learning
    writes = []
    monkeypatch.setattr(onboard, "neptune_write",
                        lambda c, p=None: writes.append((c, dict(p or {}))) or True)
    monkeypatch.setattr(self_learning, "accepted_user_concept_mappings",
                        lambda *_a, **_k: {})
    onboard._land_emergent_concepts("system-a", "", [
        {"table": "table_x", "field": "table_x.col_q",
         "mappings": [{"concept": "concept-emergent", "role": "measure",
                       "confidence": 0.7}]}])
    (cypher, params), = [(c, p) for c, p in writes if "MERGE (c:Concept" in c]
    assert params["created_by_system"] == "system-a"
    assert params["created_at"]



class _Ctx:
    """The fields of the provision state the concept writers read."""

    def __init__(self, system_id, committable):
        self.system_id = system_id
        self.committable = set(committable)
        self.user_corrections = {}
        self.deferred_llm_edges = {}


def test_field_edge_concept_write_carries_the_onboarding_system(monkeypatch):
    writes = []
    monkeypatch.setattr(onboard, "neptune_write",
                        lambda c, p=None: writes.append((c, dict(p or {}))) or True)
    ctx = _Ctx("system-a", {"concept-n"})
    onboard._dp_commit_concept_edges(
        ctx, "system-a.table_x.col_q",
        [{"concept": "concept-n", "role": "identifier", "confidence": 0.9}])
    (cypher, params), = [(c, p) for c, p in writes if "MERGE (c:Concept" in c]
    assert params["created_by_system"] == "system-a"
    assert params["created_at"]


def test_deferred_edge_concept_write_carries_the_onboarding_system(monkeypatch):
    writes = []
    monkeypatch.setattr(onboard, "neptune_write",
                        lambda c, p=None: writes.append((c, dict(p or {}))) or True)
    onboard._dp_write_deferred_edges(
        [{"fid": "system-a.table_x.col_q", "cid": "concept-n",
          "conf": 0.9, "role": "identifier"}], "system-a")
    (cypher, params), = [(c, p) for c, p in writes if "MERGE (c:Concept" in c]
    assert params["created_by_system"] == "system-a"
    edge, = [p for c, p in writes if "MAPS_TO" in c]
    assert "created_by_system" not in edge


def test_identity_rule_concept_write_carries_the_onboarding_system(monkeypatch):
    writes = []
    monkeypatch.setattr(onboard, "neptune_write",
                        lambda c, p=None: writes.append((c, dict(p or {}))) or True)
    onboard._dp_link_rule_concept(_Ctx("system-a", {"concept-n"}), "ir-x", "concept-n")
    (cypher, params), = [(c, p) for c, p in writes if "MERGE (c:Concept" in c]
    assert params["created_by_system"] == "system-a"


# =========================================================================== #
# Run-scoped provenance, rule-edge repoint, bounded scan, reasoning required
# =========================================================================== #

# --------------------------------------------------------------------------- #
# Provenance carries a run id, so a re-onboard or a removed system's leftover
# leftover concepts look like concepts "this onboarding added".
# --------------------------------------------------------------------------- #
def test_a_concept_from_an_earlier_run_is_not_offered_on_a_re_onboard(graph, model):
    """A re-onboard of system-a mints a NEW run id. Concepts the earlier run
    created carry the earlier run id, so the panel does not resurface them."""
    graph.concept("concept-n", by="system-a", run="run-earlier", name="Concept N")
    graph.field("system-b", "table_x", "col_q")
    out = _reconcile(_sys("system-a", run_id="run-later"))
    assert out["suggestions"] == []
    assert out["reason"] == "no_new_concepts"
    assert model.prompts == []


def test_a_concept_left_behind_by_a_removed_system_is_not_offered(graph, model):
    """A concept whose creating system was later removed keeps its old
    created_by_system, but its run id is not the current onboarding's, so the
    next onboarding of a different system does not offer it."""
    graph.concept("orphan-concept", by="system-gone", run="run-gone",
                  name="Orphan")
    graph.concept("concept-n", by="system-a", run=RUN_ID, name="Concept N")
    graph.field("system-b", "table_x", "col_q")
    out = _reconcile(_sys("system-a", run_id=RUN_ID))
    assert [c["id"] for c in out["new_concepts"]] == ["concept-n"]
    assert "orphan-concept" not in model.prompts[0]


def test_no_run_id_returns_no_suggestions_and_no_model_call(graph, model):
    _story(graph)
    out = _reconcile({"system_id": "system-a"})
    assert out["suggestions"] == []
    assert out["reason"] == "no_run_id"
    assert model.prompts == []


def test_the_concept_read_is_scoped_by_run_id(graph, model):
    _story(graph)
    _reconcile(_sys("system-a", run_id=RUN_ID))
    concept_reads = [p for c, p in graph.queries if "created_by_run" in c]
    assert concept_reads and concept_reads[0]["run"] == RUN_ID


def test_a_provision_run_stamps_one_run_id_on_every_concept_it_creates(monkeypatch):
    """The provision run opens a run scope; every Concept it MERGEs ON CREATE
    carries the same run id, and the id is returned in the result."""
    writes = []
    monkeypatch.setattr(onboard, "neptune_write",
                        lambda c, p=None: writes.append((c, dict(p or {}))) or True)
    with onboard._provision_run_scope() as run_id:
        onboard._dp_commit_concept_edges(
            _Ctx("system-a", {"c1"}), "system-a.t.a",
            [{"concept": "c1", "role": "identifier", "confidence": 0.9}])
        onboard._dp_link_rule_concept(_Ctx("system-a", {"c2"}), "ir-x", "c2")
    stamped = {p["created_by_run"] for c, p in writes if "MERGE (c:Concept" in c}
    assert stamped == {run_id}
    assert run_id


def test_concept_created_by_outside_a_run_carries_no_run_id():
    """Outside a provision run (default scope) the provenance has an empty run
    id, so a stray write is never attributed to a real run."""
    assert onboard._concept_created_by("system-a")["created_by_run"] == ""


# --------------------------------------------------------------------------- #
# old_concept moves the IdentityRule property AND its
# LINKS_CONCEPT edge, which is what resolve.py selects on.
# --------------------------------------------------------------------------- #
class _RuleGraph(_FakeGraph):
    """Adds IdentityRule nodes with a concept property AND a LINKS_CONCEPT
    edge, so a remap's rule repoint can be read back."""

    def __init__(self):
        super().__init__()
        self.rules: list[dict] = []

    def rule(self, rid, *, system, field, table, concept):
        self.rules.append({"id": rid, "source_system": system,
                           "source_field": field, "source_table": table,
                           "concept": concept, "links": concept})

    def _rule_matches(self, r, p):
        return (r["source_system"] == p["sys"] and r["source_field"] == p["fname"]
                and r["concept"] == p["old"]
                and (not r["source_table"] or r["source_table"] == p["tbl"]))

    def write_returning(self, cypher, params=None):
        params = dict(params or {})
        if "IdentityRule" in cypher and "LINKS_CONCEPT" in cypher:
            self.writes.append((cypher, params))
            out = []
            for p in params["rows"]:
                for r in self.rules:
                    if self._rule_matches(r, p):
                        r["concept"] = p["new"]
                        r["links"] = p["new"]
                        out.append({"field_id": p["fid"], "rule_id": r["id"],
                                    "concept": r["concept"], "linked": r["links"]})
            return out
        return super().write_returning(cypher, params)


def test_remap_old_concept_repoints_the_links_concept_edge_not_only_the_property(monkeypatch):
    g = _RuleGraph()
    monkeypatch.setattr(onboard, "neptune_query", g.query)
    monkeypatch.setattr(onboard, "neptune_write", g.write)
    monkeypatch.setattr(onboard, "neptune_write_returning", g.write_returning)
    g.concept("concept-n", by="system-a", run=RUN_ID)
    g.concept("concept-prev", by=None)
    g.field("system-b", "table_x", "col_q")
    g.rule("ir-1", system="system-b", field="col_q", table="table_x",
           concept="concept-prev")
    out = onboard.onboard_remap(RemapRequest(remaps=[
        {"field_id": "system-b.table_x.col_q", "concept_id": "concept-n",
         "role": "identifier", "confidence": 0.8, "old_concept": "concept-prev"}]))
    rule = next(r for r in g.rules if r["id"] == "ir-1")
    assert rule["concept"] == "concept-n"
    assert rule["links"] == "concept-n"       # the edge moved, not only the property
    assert out["identity_rules_updated"] == 1


def test_remap_counts_a_rule_updated_only_when_the_readback_agrees(monkeypatch):
    """A rule whose readback does not show both the property and the edge on
    the new concept is not counted as updated."""
    g = _RuleGraph()
    monkeypatch.setattr(onboard, "neptune_query", g.query)
    monkeypatch.setattr(onboard, "neptune_write", g.write)

    def half_write(cypher, params=None):
        # The write runs but the readback reports the edge still on the old concept.
        if "IdentityRule" in cypher and "LINKS_CONCEPT" in cypher:
            return [{"rule_id": "ir-1", "concept": "concept-n",
                     "linked": "concept-prev"}]
        return g.write_returning(cypher, params)

    monkeypatch.setattr(onboard, "neptune_write_returning", half_write)
    g.concept("concept-n", by="system-a", run=RUN_ID)
    g.field("system-b", "table_x", "col_q")
    g.rule("ir-1", system="system-b", field="col_q", table="table_x",
           concept="concept-prev")
    out = onboard.onboard_remap(RemapRequest(remaps=[
        {"field_id": "system-b.table_x.col_q", "concept_id": "concept-n",
         "role": "identifier", "confidence": 0.8, "old_concept": "concept-prev"}]))
    assert out["identity_rules_updated"] == 0


# --------------------------------------------------------------------------- #
# The candidate scan is bounded in the graph, not only capped afterwards.
# --------------------------------------------------------------------------- #
def test_the_candidate_read_is_bounded_in_the_graph(graph, model):
    _story(graph)
    _reconcile(_sys("system-a"))
    (read,) = [(c, p) for c, p in graph.queries
               if "MATCH (f:Field)" in c and "created_by_run" not in c]
    cypher, params = read
    assert "LIMIT" in cypher
    assert params.get("lim") == onboard.RECONCILE_SCAN_LIMIT
    # The bound applies to the Fields before their tags are expanded and
    # collected, so the graph never aggregates every Field first.
    assert cypher.index("LIMIT $lim") < cypher.index("OPTIONAL MATCH")
    assert cypher.index("LIMIT $lim") < cypher.index("collect(")


def test_a_truncated_scan_is_reported_not_counted_as_the_full_total(graph, model):
    graph.concept("concept-n", by="system-a", run=RUN_ID)
    # The fake returns exactly the scan limit of rows: the server cannot know
    # whether more exist, so it must flag the count as truncated.
    for i in range(onboard.RECONCILE_SCAN_LIMIT):
        graph.field("system-b", "table_x", f"col_{i:04d}")
    out = _reconcile(_sys("system-a"))
    assert out["candidates_truncated"] is True


def test_a_short_scan_is_not_flagged_as_truncated(graph, model):
    _story(graph)
    out = _reconcile(_sys("system-a"))
    assert out["candidates_truncated"] is False


# --------------------------------------------------------------------------- #
# Validation requires a non-empty reasoning string.
# --------------------------------------------------------------------------- #
def test_a_suggestion_without_a_reasoning_string_is_dropped(graph, model):
    _story(graph)
    model.reply = [
        {"field_id": "system-b.table_x.col_q", "concept_id": "concept-n",
         "role": "identifier", "confidence": 0.8},                       # missing
        {"field_id": "system-c.table_y.col_r", "concept_id": "concept-n",
         "role": "measure", "confidence": 0.8, "reasoning": "   "},      # blank
        {"field_id": "system-b.table_x.col_q", "concept_id": "concept-n",
         "role": "identifier", "confidence": 0.8, "reasoning": 5},       # non-string
    ]
    out = _reconcile(_sys("system-a"))
    assert out["suggestions"] == []
