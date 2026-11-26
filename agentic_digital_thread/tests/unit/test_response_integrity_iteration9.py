# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Regressions for three response-integrity defects."""
from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agents import chat_client, onboard, server
from agents.response_integrity import (
    AnswerClaim, ClaimInput, ClaimKind, FinalAnswerDraft, SystemAnswerDraft,
    capture_trusted_tool_result, render_final_answer, system_answer_from_result,
    validate_final_answer,
)
from agents.response_integrity_contracts import IntegrityContext
from agents.response_integrity_security import markdown_literal


_SCHEMA = {
    "readings": {
        "relation_kind": "base_table",
        "columns": {"measure": "numeric"},
    },
}


def _validated_system_answer():
    state: dict = {}
    query = "SELECT measure FROM readings"
    capture_trusted_tool_result(
        {"columns": ["measure"], "rows": [[7]], "row_count": 1,
         "_adt_executed_query": query},
        state, system_id="system-a", boundary="sql", tool_name="query",
        executed_query=query, physical_schema=_SCHEMA,
    )
    draft = SystemAnswerDraft(claims=(AnswerClaim(
        claim_id="measure", kind=ClaimKind.VALUE, label="measure",
        inputs=(ClaimInput(
            label="measure", value=7, semantic_field="measure", locator="row:0",
        ),),
    ),))
    answer = system_answer_from_result(SimpleNamespace(structured_output=draft), state)
    assert answer is not None and len(answer.claims) == 1
    return answer


def test_map_done_is_observable_once_only_after_server_provenance(monkeypatch):
    """A poll in the former legacy/wrapper gap must not consume or corrupt the job."""
    jobs = onboard._MapJobStore("map", table=None)
    jobs["job"] = {"status": "running"}
    monkeypatch.setattr(onboard, "_map_jobs", jobs)
    intermediate_published = threading.Event()
    release_wrapper = threading.Event()

    def legacy_worker(job_id, _system_id, _tables):
        jobs[job_id] = {"status": "done", "result": {"mappings": [{
            "table": "readings", "field": "readings.measure",
        }]}}
        intermediate_published.set()
        assert release_wrapper.wait(timeout=2)

    monkeypatch.setattr(onboard, "_run_map_job_legacy", legacy_worker)
    worker = threading.Thread(target=onboard._run_map_job, args=(
        "job", "system-a", [{
            "name": "readings", "relation_kind": "base_table",
            "schema_revision": "a" * 32,
            "columns": [{"name": "measure", "data_type": "numeric"}],
        }],
    ))
    worker.start()
    assert intermediate_published.wait(timeout=2)

    racing_poll = asyncio.run(onboard.onboard_map_status("job"))
    assert racing_poll["status"] == "running"
    assert jobs.get("job")["status"] == "running"

    release_wrapper.set()
    worker.join(timeout=2)
    assert not worker.is_alive()
    terminal = asyncio.run(onboard.onboard_map_status("job"))
    assert terminal["status"] == "done"
    mapping = terminal["mappings"][0]
    assert mapping == {
        "table": "readings", "field": "readings.measure",
        "relation_kind": "base_table", "data_type": "numeric",
        "schema_revision": "a" * 32,
    }
    assert asyncio.run(onboard.onboard_map_status("job")) == {
        "error": "Unknown job_id 'job'",
    }


def test_map_hallucinated_column_is_terminal_error_without_race_state(monkeypatch):
    """A non-physical mapping is DROPPED (job stays usable); an all-non-physical
    result is a terminal error. Nested struct paths (row-typed Athena columns)
    are routinely emitted by the map model — failing the WHOLE job on one made
    onboarding unusable, so provenance now drops what it cannot rejoin."""
    jobs = onboard._MapJobStore("map", table=None)
    jobs["job"] = {"status": "running"}
    monkeypatch.setattr(onboard, "_map_jobs", jobs)
    intermediate_published = threading.Event()
    release_wrapper = threading.Event()

    def legacy_worker(job_id, _system_id, _tables):
        jobs[job_id] = {"status": "done", "result": {"mappings": [
            {"table": "readings", "field": "readings.measure"},
            {"table": "readings", "field": "readings.hallucinated_column"},
        ]}}
        intermediate_published.set()
        assert release_wrapper.wait(timeout=2)

    monkeypatch.setattr(onboard, "_run_map_job_legacy", legacy_worker)
    tables = [{
        "name": "readings", "relation_kind": "base_table",
        "schema_revision": "a" * 32,
        "columns": [{"name": "measure", "data_type": "unknown"}],
    }]
    worker = threading.Thread(target=onboard._run_map_job, args=("job", "system-a", tables))
    worker.start()
    assert intermediate_published.wait(timeout=2)

    assert asyncio.run(onboard.onboard_map_status("job")) == {
        "status": "running", "batch": 0, "total_batches": 0,
        "mappings_so_far": 0,
    }
    assert jobs.get("job") == {"status": "running"}

    release_wrapper.set()
    worker.join(timeout=2)
    assert not worker.is_alive()
    # The hallucinated mapping is dropped; the physical one survives with exact
    # connector-owned provenance attached.
    terminal = jobs.get("job")
    assert terminal["status"] == "done"
    kept = terminal["result"]["mappings"]
    assert [m["field"] for m in kept] == ["readings.measure"]
    assert kept[0]["schema_revision"] == "a" * 32
    assert kept[0]["data_type"] == "unknown"
    assert kept[0]["relation_kind"] == "base_table"


def test_map_all_hallucinated_columns_is_terminal_error(monkeypatch):
    """When NO mapping rejoins connector metadata the job fails terminally."""
    jobs = onboard._MapJobStore("map", table=None)
    jobs["job"] = {"status": "running"}
    monkeypatch.setattr(onboard, "_map_jobs", jobs)

    def legacy_worker(job_id, _system_id, _tables):
        jobs[job_id] = {"status": "done", "result": {"mappings": [
            {"table": "readings", "field": "readings.hallucinated_column"},
        ]}}

    monkeypatch.setattr(onboard, "_run_map_job_legacy", legacy_worker)
    onboard._run_map_job("job", "system-a", [{
        "name": "readings", "relation_kind": "base_table",
        "schema_revision": "a" * 32,
        "columns": [{"name": "measure", "data_type": "unknown"}],
    }])
    # The job store normalizes terminal error text to the stable 'map_failed'.
    assert jobs.get("job") == {"status": "error", "error": "map_failed"}


def test_local_sync_mode_a_uses_integrity_pipeline_and_mode_b_stays_legacy(monkeypatch):
    answer = _validated_system_answer()
    raw_model_text = "fabricated raw value 999 must never return"

    class Agent:
        callback_handler = None

        def __init__(self):
            self.hook = None
            self.hooks = SimpleNamespace(add_hook=self._add_hook)

        def _add_hook(self, hook):
            self.hook = hook

        def __call__(self, _prompt, **kwargs):
            assert kwargs["structured_output_model"] is FinalAnswerDraft
            self.callback_handler(data=raw_model_text)
            self.hook._on_tool_done(SimpleNamespace(
                tool_use={"name": "system-a___query", "toolUseId": "tool-1", "input": {}},
                result={"content": [{"json": answer.model_dump(mode="json")}]},
            ))
            return SimpleNamespace(structured_output=FinalAnswerDraft(
                claims=(answer.claims[0],),
            ))

    monkeypatch.setattr(server, "get_config", lambda: SimpleNamespace(has_runtime=False))
    monkeypatch.setattr(chat_client, "build_orchestrator", lambda *_a, **_k: Agent())
    monkeypatch.setenv("SYSTEM_QUERY_MODE", "agent")
    # Enforce-path semantics (ADR-0046) — opt in per ADR-0047.
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "enforce")
    app = FastAPI()
    app.post("/api/ask/sync")(server.ask_sync)
    with TestClient(app) as client:
        response = client.post("/api/ask/sync", json={
            "question": "Show historical measure", "session_id": "sync-a",
        })
    assert response.status_code == 200
    rendered = response.json()["answer"]
    assert "**measure**: 7" in rendered
    assert answer.evidence[0].evidence_id.replace("-", "\\-") in rendered
    assert raw_model_text not in rendered

    monkeypatch.setenv("SYSTEM_QUERY_MODE", "orchestrator")
    monkeypatch.setattr(server, "get_orchestrator", lambda: lambda _question: raw_model_text)
    with TestClient(app) as client:
        legacy = client.post("/api/ask/sync", json={
            "question": "legacy request", "session_id": "sync-b",
        })
    assert legacy.json() == {"answer": raw_model_text}


def test_exact_server_evidence_id_bypasses_phone_redaction_only_as_citation():
    answer = _validated_system_answer()
    context = IntegrityContext("Show historical measure")
    context.system_answers.append(answer)
    final = validate_final_answer(FinalAnswerDraft(claims=(answer.claims[0],)), context)
    exact_id = "ev-12345678901234567890"
    evidence = final.evidence[0].model_copy(update={"evidence_id": exact_id})
    claim = final.claims[0].model_copy(update={"evidence_ids": (exact_id,)})
    rendered = render_final_answer(final.model_copy(update={
        "claims": (claim,), "evidence": (evidence,),
    }))

    assert exact_id.replace("-", "\\-") in rendered
    assert "REDACTED_PHONE" not in rendered
    assert "OUTPUT_SENSITIVITY_REJECTED" not in rendered
    arbitrary = markdown_literal("call 12345678901234567890 now")
    assert "12345678901234567890" not in arbitrary and "REDACTED" in arbitrary
