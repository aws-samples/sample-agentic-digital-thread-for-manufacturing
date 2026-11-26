# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Response-integrity and route-safety regressions: preview purity, provision
errors, trusted field annotation, Mode A reasoning, provenance tampering,
partial results, and the removed debug route."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import boto3
import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from agents import onboard, self_learning, server
from agents.chat_client import ThinkingFilter, _CloudStreamState
from agents.events import ReasoningEvent
from agents.job_store import JobStore
from agents.models import ProvisionRequest
from agents.response_integrity import (
    AnswerClaim, ClaimInput, ClaimKind, FinalAnswerDraft, IntegrityContext,
    LearningProposal, ReasonCode, SystemAnswerDraft, capture_trusted_tool_result,
    system_answer_from_result, validate_final_answer,
)
from agents.response_integrity_contracts import MAX_TOOL_CALLS_PER_TURN
from agents.runtime_integrity import _persist_one


BASE_SCHEMA = {
    "readings": {
        "relation_kind": "base_table",
        "columns": {"measure": "numeric", "state": "varchar"},
    },
}


def _onboard_app() -> FastAPI:
    app = FastAPI()

    @app.middleware("http")
    async def roles(request: Request, call_next):
        request.state.is_admin = True
        request.state.is_integrator = True
        return await call_next(request)

    app.include_router(onboard.router)
    return app


def _seed_provenance(monkeypatch, *, kind="base_table", data_type="numeric") -> str:
    revision = "a" * 32
    store = JobStore("schema-provenance", table=None)
    store[revision] = {
        "status": "ready",
        "system_id": "system-a",
        "tables": {
            "readings": {
                "relation_kind": kind,
                "columns": {"measure": data_type, "state": "varchar"},
            },
        },
    }
    monkeypatch.setattr(onboard, "_schema_provenance", store)
    return revision


def _mapping(revision: str, *, kind="base_table", data_type="numeric", field="measure") -> dict:
    return {
        "table": "readings", "field": f"readings.{field}",
        "concept": "throughput", "mappings": [],
        "relation_kind": kind, "data_type": data_type,
        "schema_revision": revision,
    }


def test_preview_route_is_a_pure_plan_with_side_effect_spies(monkeypatch):
    revision = _seed_provenance(monkeypatch)
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("preview crossed a side-effect boundary")

    for name in (
        "_do_provision", "_do_provision_base", "neptune_write", "neptune_query",
        "_persist_node_connection", "add_system_agent", "_run_provision_job",
    ):
        monkeypatch.setattr(onboard, name, forbidden)
    monkeypatch.setattr(onboard.catalog, "get", forbidden)
    monkeypatch.setattr(onboard.catalog, "update_status", forbidden)
    monkeypatch.setattr(onboard.threading, "Thread", forbidden)

    response = TestClient(_onboard_app()).post(
        "/api/onboard/provision/preview",
        json={
            "system_id": "system-a", "connection_type": "MCP Server",
            "connection_string": "https://connector.invalid/mcp",
            "mappings": [_mapping(revision)],
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] == "preview"
    assert response.json()["plan"]["mapping_count"] == 1
    assert response.json()["edges"][0]["from"] == "system-a.readings.measure"
    assert calls == []


def test_provision_error_dicts_are_never_done_for_mcp_generic_or_worker(monkeypatch):
    revision = _seed_provenance(monkeypatch)
    app = _onboard_app()
    client = TestClient(app)
    persisted = []
    monkeypatch.setattr(onboard, "_persist_schema_relation_kinds", lambda *_a: persisted.append(True))

    missing = client.post(
        "/api/onboard/provision/confirm",
        json={"system_id": "system-a", "connection_type": "MCP Server",
              "mappings": [_mapping(revision)]},
    )
    assert missing.json() == {"status": "error", "error": "provision_failed"}

    monkeypatch.setattr(onboard, "_do_provision", lambda _body: {"error": "generic_failure"})
    generic = client.post(
        "/api/onboard/provision/confirm",
        json={"system_id": "system-a", "mappings": [_mapping(revision)]},
    )
    assert generic.json() == {"status": "error", "error": "provision_failed"}

    jobs = JobStore("provision", table=None)
    monkeypatch.setattr(onboard, "_provision_jobs", jobs)
    onboard._run_provision_job(
        "job-error", ProvisionRequest(system_id="system-a", mappings=[_mapping(revision)]),
    )
    assert jobs.get("job-error") == {"status": "error", "error": "provision_failed"}
    assert persisted == []


class ProposalTable:
    def __init__(self):
        self.items = {}
        self.updates = 0

    def put_item(self, Item):
        self.items[(Item["mapping_id"], Item["discovered_at"])] = dict(Item)

    def query(self, **kwargs):
        values = kwargs.get("ExpressionAttributeValues", {})
        items = list(self.items.values())
        if kwargs.get("IndexName") == "subject-key-index":
            items = [item for item in items if item.get("subject_key") == values[":sk"]]
        else:
            items = [item for item in items if item.get("mapping_id") == values[":mid"]]
        return {"Items": [dict(item) for item in items]}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues=None,
                    ExpressionAttributeNames=None, **_kwargs):
        self.updates += 1
        item = self.items[(Key["mapping_id"], Key["discovered_at"])]
        names = ExpressionAttributeNames or {}
        values = ExpressionAttributeValues or {}
        for clause in UpdateExpression.removeprefix("SET ").split(","):
            left, right = (part.strip() for part in clause.split("="))
            item[names.get(left, left)] = values[right]


def _state_answer(payload: dict, claim: AnswerClaim):
    state = {}
    query = "SELECT measure, state FROM readings"
    capture_trusted_tool_result(
        {**payload, "_adt_executed_query": query}, state,
        system_id="system-a", boundary="sql", tool_name="read",
        executed_query=query, physical_schema=BASE_SCHEMA,
    )
    result = SimpleNamespace(structured_output=SystemAnswerDraft(claims=(claim,)))
    return state, system_answer_from_result(result, state)


def test_field_annotation_requires_trusted_qualified_endpoint_and_writes_neptune(monkeypatch):
    table = ProposalTable()
    writes = []
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)
    monkeypatch.setattr(
        self_learning, "_known_systems_graph_read",
        lambda: [{"id": "system-a", "name": "System A", "system_type": "MES", "plant": "Site"}],
    )
    self_learning._reset_known_systems_cache()
    monkeypatch.setattr(self_learning, "_neptune_query", lambda *_a, **_k: [{"n": 1}])
    monkeypatch.setattr(
        self_learning, "_neptune_write",
        lambda query, params=None: writes.append((query, params)) or True,
    )
    claim = AnswerClaim(
        claim_id="state", kind=ClaimKind.VALUE, label="state",
        inputs=(ClaimInput(label="state", value="RUN", semantic_field="state", locator="row:0"),),
    )
    _state, answer = _state_answer(
        {"columns": ["measure", "state"], "rows": [[1, "RUN"]], "row_count": 1}, claim,
    )
    assert answer is not None and answer.evidence[0].physical_field == "readings.state"
    context = IntegrityContext("historical state")
    context.system_answers.append(answer)
    proposal = LearningProposal(
        proposal_id="qualified", kind="field_annotation", from_system="system-a",
        from_field="readings.state", concept="operating state semantics",
        evidence_ids=(answer.evidence[0].evidence_id,),
        relationship_type="field_annotation", direction="self",
    )
    final = validate_final_answer(
        FinalAnswerDraft(claims=(answer.claims[0],), proposals=(proposal,)), context,
    )
    assert final.learning_allowed and final.proposals == (proposal,)
    _persist_one(proposal, 1)
    item = next(iter(table.items.values()))
    accepted = self_learning.accept_mapping(item["mapping_id"])
    assert accepted["status"] == "accepted"
    assert any("SET f.disambiguation" in query for query, _params in writes)

    unqualified = proposal.model_copy(update={"proposal_id": "bad", "from_field": "state"})
    rejected = validate_final_answer(
        FinalAnswerDraft(claims=(answer.claims[0],), proposals=(unqualified,)), context,
    )
    assert rejected.proposals == () and not rejected.learning_allowed
    with pytest.raises(ValueError, match="qualified physical"):
        _persist_one(unqualified, 1)

    malformed = dict(item)
    malformed.update(mapping_id="malformed", from_field="state", status="pending")
    table.put_item(Item=malformed)
    before = table.updates
    assert self_learning.accept_mapping("malformed")["error"] == "Mapping is non-actionable and cannot be accepted"
    assert table.updates == before


def test_cloud_mode_a_drops_upstream_reasoning_but_keeps_fixed_progress():
    class Log:
        def info(self, *_a, **_k):
            pass

    state = _CloudStreamState(
        ThinkingFilter(), integrity_mode=True, log=Log(), owner_id="u",
        session_id="s", runtime_session_id="runtime",
    )
    assert state.events_for({"reasoningText": "raw private reasoning secret"}) == []
    fixed = state.events_for({
        "current_tool_use": {
            "toolUseId": "tool-1", "name": "read", "input": '{"secret":"value"}',
        },
    })
    assert len(fixed) == 1 and isinstance(fixed[0], ReasoningEvent)
    assert fixed[0].text == "preparing read query…"
    assert "secret" not in fixed[0].text and "value" not in fixed[0].text
    state.close()


def test_server_owned_provenance_rejects_map_and_provision_tampering(monkeypatch):
    store = JobStore("schema-provenance", table=None)
    monkeypatch.setattr(onboard, "_schema_provenance", store)
    discovered = onboard._store_discovered_provenance("system-a", {
        "tables": [{
            "name": "readings", "relation_kind": "view",
            "columns": [{"name": "measure", "data_type": "numeric"}],
        }],
    })
    revision = discovered["schema_revision"]

    def no_background(*_a, **_k):
        raise AssertionError("tampered provenance reached a background job")

    monkeypatch.setattr(onboard.threading, "Thread", no_background)
    client = TestClient(_onboard_app())
    map_response = client.post("/api/onboard/map", json={
        "system_id": "system-a",
        "tables": [{
            "name": "readings", "schema_revision": revision,
            "relation_kind": "base_table",
            "columns": [{"name": "measure", "data_type": "integer"}],
        }],
    })
    assert map_response.status_code == 400
    assert map_response.json() == {"detail": "schema_provenance_invalid"}

    provision_response = client.post("/api/onboard/provision", json={
        "system_id": "system-a", "mappings": [{
            "table": "readings", "field": "readings.measure",
            "schema_revision": revision, "relation_kind": "base_table",
            "data_type": "integer",
        }],
    })
    assert provision_response.status_code == 400
    assert provision_response.json() == {"detail": "schema_provenance_invalid"}


def test_partial_prefix_and_quota_never_produce_complete_set_claims_or_learning():
    aggregate = AnswerClaim(
        claim_id="sum", kind=ClaimKind.AGGREGATE, label="sum", operation="sum",
        inputs=(
            ClaimInput(label="a", value=2, semantic_field="measure", locator="row:0"),
            ClaimInput(label="b", value=3, semantic_field="measure", locator="row:1"),
        ),
    )
    state, partial = _state_answer({
        "columns": ["measure", "state"], "rows": [[2, "RUN"], [3, "RUN"]],
        "row_count": 2, "truncated": True,
    }, aggregate)
    assert partial is not None and partial.claims == ()
    assert all(getattr(claim, "value", None) != 5 for claim in partial.claims)
    partial_context = IntegrityContext("historical sum")
    partial_context.system_answers.append(partial)
    partial_final = validate_final_answer(FinalAnswerDraft(), partial_context)
    assert partial_final.claims == () and partial_final.proposals == ()
    assert not partial_final.learning_allowed
    assert ReasonCode.SOURCE_PARTIAL in partial_final.reason_codes

    quota_state = {}
    query = "SELECT measure FROM readings"
    for _ in range(MAX_TOOL_CALLS_PER_TURN + 1):
        capture_trusted_tool_result(
            {"columns": ["measure"], "rows": [[1]], "row_count": 1,
             "_adt_executed_query": query}, quota_state,
            system_id="system-a", boundary="sql", tool_name="read",
            executed_query=query, physical_schema=BASE_SCHEMA,
        )
    quota_result = SimpleNamespace(structured_output=SystemAnswerDraft(claims=(AnswerClaim(
        claim_id="latest", kind=ClaimKind.VALUE, label="latest",
        inputs=(ClaimInput(label="latest", value=1, semantic_field="measure", locator="row:0"),),
    ),)))
    quota_answer = system_answer_from_result(quota_result, quota_state)
    assert quota_answer is not None and quota_answer.claims == () and quota_answer.evidence == ()
    assert ReasonCode.TRAVERSAL_BUDGET_EXCEEDED in quota_answer.reason_codes
    quota_context = IntegrityContext("historical latest")
    quota_context.system_answers.append(quota_answer)
    quota_final = validate_final_answer(FinalAnswerDraft(), quota_context)
    assert quota_final.claims == () and quota_final.evidence == () and quota_final.proposals == ()
    assert not quota_final.learning_allowed


def test_debug_bedrock_route_removed_and_exception_secret_cannot_escape(monkeypatch):
    secret = "exception-secret-must-not-escape"  # nosec B105 - not a credential: field/env/secret NAME, sentinel, or offline test fixture
    calls = []

    def forbidden_client(*_a, **_k):
        calls.append(True)
        raise RuntimeError(secret)

    monkeypatch.setattr(boto3, "client", forbidden_client)
    from agents.auth import middleware
    user = SimpleNamespace(user_id="ordinary", email="ordinary@example.invalid", username="ordinary")
    monkeypatch.setattr(middleware, "get_config", lambda: SimpleNamespace(has_cognito=True))
    monkeypatch.setattr(middleware, "CognitoAuth", lambda: object())
    monkeypatch.setattr(middleware.AuthMiddleware, "_decode_session", staticmethod(lambda _raw: {"access_token": "x"}))
    monkeypatch.setattr(middleware.AuthMiddleware, "_validate_session", staticmethod(lambda *_a: (user, {"access_token": "x"})))
    monkeypatch.setattr(middleware.AuthMiddleware, "_identity_roles", staticmethod(lambda *_a: (user, False, False)))

    async def request():
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get("/api/debug/bedrock", cookies={"adt_session": "x"})

    response = asyncio.run(request())
    assert response.status_code == 404
    assert secret not in response.text
    assert calls == []
    assert "/api/debug/bedrock" not in {getattr(route, "path", "") for route in server.app.routes}
    assert not hasattr(server, "debug_bedrock")
