# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``POST /api/admin/vocabulary/guidance/{system_id}/propose`` takes a scope.

A steward can file a free-text ANSWER REQUIREMENT through the ordinary propose
route, not only the word-ban sentence ``/promote-requirement`` composes. The
proposal keeps the same pending -> steward accept lifecycle, and after accept
the note lives in the document's ``answer_requirements`` array and renders in
the FINAL-ANSWER fence, never the QUERY HINTS fence.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

import agents.self_learning as learning
import agents.system_guidance as guidance
import agents.vocabulary_routes as vr
from agents.guidance_scope import parse_document, render_orchestrator_sections

SYSTEM = "system-a"
PROPOSE = f"/api/admin/vocabulary/guidance/{SYSTEM}/propose"
REQUIREMENT = ("Never present open or started rows as stuck units; say they "
               "are open and give their age.")


class _AdminInject(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        request.state.is_admin = True
        return await call_next(request)


@pytest.fixture()
def client() -> TestClient:
    app = FastAPI()
    app.include_router(vr.router)
    app.add_middleware(_AdminInject)
    return TestClient(app)


@pytest.fixture()
def known_system(monkeypatch):
    import agents.catalog as catalog_mod
    monkeypatch.setattr(catalog_mod.catalog, "get_all", lambda: [{"id": SYSTEM}])


@pytest.mark.parametrize("scope", ["query_hint", "answer_requirement"])
def test_propose_files_the_requested_scope(client, known_system, monkeypatch, scope):
    calls: list = []
    monkeypatch.setattr(learning, "log_proposed_mapping",
                        lambda **kw: calls.append(kw) or "gn-scope")
    resp = client.post(PROPOSE, json={"note": REQUIREMENT, "scope": scope})
    assert resp.status_code == 200
    assert resp.json() == {"status": "pending", "mapping_id": "gn-scope"}
    assert calls[0]["guidance_scope"] == scope
    assert calls[0]["proposed_type"] == "guidance_note"
    assert calls[0]["source"] == "user"
    assert calls[0]["provenance"] == "human-asserted"


@pytest.mark.parametrize("body", [{"note": REQUIREMENT},
                                  {"note": REQUIREMENT, "scope": None}])
def test_missing_scope_defaults_to_query_hint(client, known_system, monkeypatch, body):
    calls: list = []
    monkeypatch.setattr(learning, "log_proposed_mapping",
                        lambda **kw: calls.append(kw) or "gn-default")
    resp = client.post(PROPOSE, json=body)
    assert resp.status_code == 200
    assert calls[0]["guidance_scope"] == "query_hint"


@pytest.mark.parametrize("scope", ["legacy_unscoped", "answer", "QUERY_HINT",
                                   "", 42, ["answer_requirement"]])
def test_unsupported_scope_is_400_and_files_nothing(client, known_system,
                                                   monkeypatch, scope):
    calls: list = []
    monkeypatch.setattr(learning, "log_proposed_mapping",
                        lambda **kw: calls.append(kw) or "gn-x")
    resp = client.post(PROPOSE, json={"note": REQUIREMENT, "scope": scope})
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert "query_hint" in error and "answer_requirement" in error
    assert calls == []


def _wire_lifecycle(monkeypatch, initial: str):
    from unit.test_self_learning_lifecycle import FakeTable
    from unit.test_system_guidance import FakeBackend

    table = FakeTable()
    monkeypatch.setattr(learning, "_get_table", lambda: table)
    monkeypatch.setattr(learning, "PROPOSAL_STAGING_MIN_COREFERENCES", 1)
    graph = {"guidance": initial}
    monkeypatch.setattr(
        learning, "_neptune_query",
        lambda *_a, **_k: [{"qg": graph["guidance"]}])

    def graph_write(cypher, params=None):
        params = params or {}
        if ("SET s.query_guidance = $desired" in cypher
                and graph["guidance"] == params.get("expected")):
            graph["guidance"] = params["desired"]
        return True

    monkeypatch.setattr(learning, "_neptune_write", graph_write)
    prior = guidance._store
    guidance.set_store(guidance.SystemGuidanceStore(backend=FakeBackend()))
    return table, graph, prior


def test_free_text_requirement_lands_in_the_final_answer_fence(
        client, known_system, monkeypatch):
    table, graph, prior = _wire_lifecycle(monkeypatch, "Generic base prose.")
    try:
        resp = client.post(PROPOSE, json={"note": REQUIREMENT,
                                          "scope": "answer_requirement"})
        assert resp.status_code == 200
        mapping_id = resp.json()["mapping_id"]
        assert table.by_id(mapping_id)["status"] == "pending"
        assert REQUIREMENT not in graph["guidance"]

        preview = learning.preview_guidance_mapping(mapping_id)
        assert preview["preview"]["outcome"] == "append"
        assert preview["preview"]["scope"] == "answer_requirement"

        accepted = learning.accept_mapping(mapping_id, actor="steward")
        assert accepted["status"] == "accepted"
        assert table.by_id(mapping_id)["status"] == "accepted"

        document = parse_document(graph["guidance"])
        assert document["answer_requirements"] == [REQUIREMENT]
        assert document["query_hints"] == []
        assert document["base"] == "Generic base prose."

        sections = render_orchestrator_sections(graph["guidance"])
        assert REQUIREMENT in sections["answer_requirements"]
        assert "BEGIN FINAL-ANSWER REQUIREMENTS" in sections["answer_requirements"]
        assert REQUIREMENT not in sections["query_guidance"]
        assert REQUIREMENT not in sections["legacy_guidance"]

        mirrored = parse_document(guidance.read(SYSTEM), plain_is_base=False)
        assert mirrored["answer_requirements"] == [REQUIREMENT]
        assert mirrored["query_hints"] == []
    finally:
        guidance.set_store(prior)
