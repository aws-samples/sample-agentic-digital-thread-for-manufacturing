# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""B2 try-it async job (ADR-0052 (d)).

Offline: ``fastapi.testclient`` against the bare learning router with every
impure seam monkeypatched (the ``test_learning_routes_health.py`` idiom); the
job registry is an in-memory ``JobStore("tryit", table=None)`` (the
``test_onboard_reset_async.py`` idiom). Pins:

- POST validation: 202 + job id / 409 one-concurrent-job / 404 unknown /
  400 no usable test question (and the non-pending / non-injectable
  rejections); an optional steward-supplied ``question`` (stripped, ≤500
  chars) overrides ``query_context`` for BOTH runs and is echoed on every
  job payload (running/done/error) so GET shows what was asked;
- job lifecycle running → done with the pinned result shape and an HONEST
  diff — and the two runs STRICTLY SERIAL, baseline FIRST (one runtime, one
  stream at a time);
- an errored or over-clamp run marks the job ``error`` with the pinned
  ``error="tryit_failed"`` sentinel + a sanitized one-line ``error_detail``
  ("<which run> failed: <ExceptionType>" — never message/stack/credentials);
- every run carries ``injected: bool`` (X-4 — candidate actually entered the
  route seed), the result echoes ``candidate_injected``, and a non-injectable
  environment stamps the diff ``note``;
- the runtime pops the RESERVED ``tryItCandidate`` payload key
  unconditionally (the ``_adt_identity`` posture, ADR-0049) — it can never
  reach the prompt; Mode B ignores it;
- try-it session ids (``tryit-…``) never write usage-ledger rows;
- the chat-client ``extra_payload`` seam is additive-only.
"""
from __future__ import annotations

import inspect
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import agents.learning_routes as lr
import agents.runtime_app as runtime_app
from agents.events import (
    DoneEvent, ErrorEvent, MessageEvent, MetadataEvent, ToolUseEvent,
)
from agents.job_store import JobStore


@pytest.fixture(autouse=True)
def _fresh_tryit_jobs(monkeypatch):
    """Fresh in-memory job registry per test (instance-scoped, so swap it)."""
    store = JobStore("tryit", table=None)
    monkeypatch.setattr(lr, "_TRYIT_JOBS", store)
    yield store


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(lr.router)
    return TestClient(app)


_ROW = {
    "mapping_id": "cand-mapping-1", "status": "pending",
    "proposed_type": "join_path",
    "from_system": "sys-a", "from_field": "orders.op_ref",
    "to_system": "sys-b", "to_field": "events.op_id",
    "concept": "operation.identity", "confidence": "0.7",
    "query_context": "which operations map across the two systems?",
}


def _summary(rules, planned=None):
    block = {"rules_applied": rules}
    if planned is not None:
        block["planned_with"] = planned
    return MetadataEvent(data={"intelligence_summary": block})


def _fake_stream_factory(calls, injectable=True, retained=True):
    """Two distinguishable fake runs; records strict start/end ordering.
    Mirrors the ``(stream, injected)`` seam — ``injected`` True only when a
    candidate is forwarded AND the environment can inject (runtime path).
    ``retained``: the candidate run's routing summary says the candidate is
    in its route seed (``planned_with.tryit_candidate``), as the runtime
    reports it."""

    def fake_stream(question, session_id, candidate):
        def events():
            calls.append(("start", session_id, candidate))
            yield ToolUseEvent(tool_name="sys_a___run_query")
            if candidate is not None:
                yield ToolUseEvent(tool_name="sys_b___run_query")
                planned = {"shared_concepts": ["operation.identity"], "join_paths": 2}
                if retained:
                    planned["tryit_candidate"] = True
                yield _summary(
                    [{"id": "ir-1", "label": "rule one"},
                     {"id": "ir-2", "label": "rule two"}],
                    planned=planned,
                )
            else:
                yield _summary([{"id": "ir-1", "label": "rule one"}])
            yield MessageEvent(content="the answer " * 60)
            yield DoneEvent()
            calls.append(("end", session_id, candidate))

        return events(), injectable and candidate is not None

    return fake_stream


# ── POST validation ─────────────────────────────────────────────────────────

def test_second_concurrent_post_409(client, monkeypatch):
    monkeypatch.setattr(lr, "_find_mapping", lambda mid: dict(_ROW))
    monkeypatch.setattr(lr, "_run_tryit_job", lambda job_id, row, question: None)

    first = client.post("/api/admin/learning/tryit",
                        json={"mapping_id": "cand-mapping-1"})
    assert first.status_code == 202
    assert first.json()["job_id"]

    second = client.post("/api/admin/learning/tryit",
                         json={"mapping_id": "cand-mapping-1"})
    assert second.status_code == 409
    assert second.json() == {"error": "try-it already running"}


def test_stale_running_job_is_taken_over(client, monkeypatch, _fresh_tryit_jobs):
    monkeypatch.setattr(lr, "_find_mapping", lambda mid: dict(_ROW))
    monkeypatch.setattr(lr, "_run_tryit_job", lambda job_id, row, question: None)
    _fresh_tryit_jobs[lr._TRYIT_ACTIVE_KEY] = {
        "status": "running", "job_id": "old",
        "started_at": time.time() - lr._TRYIT_STALE_SECONDS - 1,
    }

    response = client.post("/api/admin/learning/tryit",
                           json={"mapping_id": "cand-mapping-1"})
    assert response.status_code == 202


def test_unknown_mapping_404(client, monkeypatch):
    monkeypatch.setattr(lr, "_find_mapping", lambda mid: None)
    response = client.post("/api/admin/learning/tryit",
                           json={"mapping_id": "nope"})
    assert response.status_code == 404


def test_mapping_without_usable_query_context_400(client, monkeypatch):
    """No steward question + empty/whitespace/<8-char query_context (prompt
    fragments confuse the baseline run) ⇒ the pinned 400 copy. A
    whitespace-only supplied question counts as absent."""
    for context in ("", "   ", "fix it"):
        row = {**_ROW, "query_context": context}
        monkeypatch.setattr(lr, "_find_mapping", lambda mid, r=row: r)
        response = client.post(
            "/api/admin/learning/tryit",
            json={"mapping_id": "cand-mapping-1", "question": "   "})
        assert response.status_code == 400
        assert response.json()["error"] == (
            "proposal has no usable test question — supply one")


def test_supplied_question_overrides_and_is_echoed_on_get(client, monkeypatch):
    """An explicit 'question' (stripped) overrides query_context and rides
    the running job payload so GET shows what was asked."""
    monkeypatch.setattr(lr, "_find_mapping", lambda mid: dict(_ROW))
    monkeypatch.setattr(lr, "_run_tryit_job",
                        lambda job_id, row, question: None)

    response = client.post(
        "/api/admin/learning/tryit",
        json={"mapping_id": "cand-mapping-1",
              "question": "  which operator ran work order 42?  "})
    assert response.status_code == 202

    job = client.get(
        f"/api/admin/learning/tryit/{response.json()['job_id']}").json()
    assert job["status"] == "running"
    assert job["question"] == "which operator ran work order 42?"


def test_supplied_question_bounded_to_500_chars(client, monkeypatch):
    monkeypatch.setattr(lr, "_find_mapping", lambda mid: dict(_ROW))
    monkeypatch.setattr(lr, "_run_tryit_job",
                        lambda job_id, row, question: None)

    response = client.post(
        "/api/admin/learning/tryit",
        json={"mapping_id": "cand-mapping-1", "question": "q" * 600})
    assert response.status_code == 202
    job = client.get(
        f"/api/admin/learning/tryit/{response.json()['job_id']}").json()
    assert job["question"] == "q" * lr._TRYIT_QUESTION_MAX_CHARS


def test_supplied_question_rescues_unusable_context(client, monkeypatch):
    row = {**_ROW, "query_context": ""}
    monkeypatch.setattr(lr, "_find_mapping", lambda mid: row)
    monkeypatch.setattr(lr, "_run_tryit_job",
                        lambda job_id, row, question: None)
    response = client.post(
        "/api/admin/learning/tryit",
        json={"mapping_id": "cand-mapping-1",
              "question": "which operations map across the two systems?"})
    assert response.status_code == 202


def test_non_string_question_falls_back_to_query_context(client, monkeypatch):
    monkeypatch.setattr(lr, "_find_mapping", lambda mid: dict(_ROW))
    monkeypatch.setattr(lr, "_run_tryit_job",
                        lambda job_id, row, question: None)
    response = client.post(
        "/api/admin/learning/tryit",
        json={"mapping_id": "cand-mapping-1", "question": 123})
    assert response.status_code == 202
    job = client.get(
        f"/api/admin/learning/tryit/{response.json()['job_id']}").json()
    assert job["question"] == _ROW["query_context"]


def test_non_pending_and_non_injectable_400(client, monkeypatch):
    for row in ({**_ROW, "status": "accepted"},
                {**_ROW, "proposed_type": "correlation_insight"}):
        monkeypatch.setattr(lr, "_find_mapping", lambda mid, r=row: r)
        response = client.post("/api/admin/learning/tryit",
                               json={"mapping_id": "cand-mapping-1"})
        assert response.status_code == 400


def test_unknown_job_poll_404(client):
    assert client.get("/api/admin/learning/tryit/nope").status_code == 404
    # The internal active-slot key is not a pollable job.
    assert client.get(
        f"/api/admin/learning/tryit/{lr._TRYIT_ACTIVE_KEY}").status_code == 404


# ── Job lifecycle ───────────────────────────────────────────────────────────

def test_job_lifecycle_running_done(monkeypatch, _fresh_tryit_jobs):
    calls: list = []
    monkeypatch.setattr(lr, "_tryit_stream", _fake_stream_factory(calls))

    job_id = lr._claim_tryit_job()
    assert _fresh_tryit_jobs[job_id]["status"] == "running"

    lr._run_tryit_job(job_id, dict(_ROW), _ROW["query_context"])

    job = _fresh_tryit_jobs[job_id]
    assert job["status"] == "done"
    result = job["result"]
    assert result["question"] == _ROW["query_context"]
    for run_key in ("baseline", "candidate"):
        run = result[run_key]
        assert set(run) == {"systems_queried", "rules_applied",
                            "translation_steps", "answer_excerpt",
                            "elapsed_seconds", "injected"}
        assert len(run["answer_excerpt"]) <= lr._TRYIT_EXCERPT_CHARS
    assert result["baseline"]["systems_queried"] == ["sys-a"]
    assert result["candidate"]["systems_queried"] == ["sys-a", "sys-b"]
    assert result["candidate"]["translation_steps"] == [
        "shared concepts in routing seed: operation.identity",
        "join paths in routing seed: 2",
    ]
    # X-4: baseline never injects; candidate did; top-level echo.
    assert result["baseline"]["injected"] is False
    assert result["candidate"]["injected"] is True
    assert result["candidate_injected"] is True
    diff = result["diff"]
    assert diff == {
        "systems_added": ["sys-b"], "systems_removed": [],
        "rules_added": [{"id": "ir-2", "label": "rule two"}],
        "rules_removed": [],
    }
    # X-1 pinned contract: systems are PLAIN STRINGS, rules are OBJECTS
    # [{id, label}] — never flattened; injected env carries no note.
    assert all(isinstance(s, str)
               for s in diff["systems_added"] + diff["systems_removed"])
    assert all(set(r) == {"id", "label"}
               for r in diff["rules_added"] + diff["rules_removed"])
    assert "note" not in diff
    # The honest-diff contract: no derived scores, and the deleted
    # tool_calls_before/after fiction must never return.
    flattened = str(result)
    assert "tool_calls_before" not in flattened
    assert "tool_calls_after" not in flattened


def test_runs_are_strictly_serial_baseline_first(monkeypatch):
    calls: list = []
    monkeypatch.setattr(lr, "_tryit_stream", _fake_stream_factory(calls))

    job_id = lr._claim_tryit_job()
    lr._run_tryit_job(job_id, dict(_ROW), _ROW["query_context"])

    phases = [(phase, session.rsplit("-", 1)[-1], candidate is None)
              for phase, session, candidate in calls]
    # Baseline (-a, no candidate) fully completes BEFORE the candidate run
    # (-b) starts — never interleaved, never parallel.
    assert phases == [("start", "a", True), ("end", "a", True),
                      ("start", "b", False), ("end", "b", False)]
    baseline_candidate = calls[0][2]
    candidate_payload = calls[2][2]
    assert baseline_candidate is None
    assert candidate_payload["proposed_type"] == "join_path"
    assert "query_context" not in candidate_payload
    assert "from_value" not in candidate_payload


def test_a_value_mapping_candidate_carries_its_one_bounded_value_pair():
    # A value mapping is served only with its values; its candidate carries
    # the one pair it proposes (bounded), every other kind still none.
    row = {**_ROW, "proposed_type": "value_mapping", "from_value": "CELL-7",
           "to_value": "X" * 400}
    candidate = lr._candidate_from_mapping(row)
    assert candidate["from_value"] == "CELL-7" and candidate["to_value"] == "X" * 256
    joined = lr._candidate_from_mapping({**_ROW, "from_value": "A-77", "to_value": "4400077"})
    assert "from_value" not in joined and "to_value" not in joined

def test_worker_asks_the_supplied_question_in_both_runs(monkeypatch,
                                                        _fresh_tryit_jobs):
    """The resolved question (override path) reaches BOTH runs verbatim and
    is echoed on the done payload (top-level + result)."""
    asked: list = []

    def stream(question, session_id, candidate):
        def events():
            asked.append(question)
            yield _summary([])
            yield MessageEvent(content="ok")
            yield DoneEvent()
        return events(), candidate is not None

    monkeypatch.setattr(lr, "_tryit_stream", stream)
    override = "what changed on line 3 last shift?"

    job_id = lr._claim_tryit_job(override)
    lr._run_tryit_job(job_id, dict(_ROW), override)

    assert asked == [override, override]
    job = _fresh_tryit_jobs[job_id]
    assert job["question"] == override
    assert job["result"]["question"] == override


def test_run_error_marks_job_error(monkeypatch, _fresh_tryit_jobs):
    def erroring_stream(question, session_id, candidate):
        def events():
            yield ErrorEvent(message="Query failed.")
        return events(), False

    monkeypatch.setattr(lr, "_tryit_stream", erroring_stream)

    job_id = lr._claim_tryit_job()
    lr._run_tryit_job(job_id, dict(_ROW), _ROW["query_context"])

    job = _fresh_tryit_jobs[job_id]
    assert job["status"] == "error"
    assert "result" not in job
    # X-5: the pinned sentinel + one sanitized steward-readable line —
    # which run failed + the exception TYPE, never message/stack.
    assert job["error"] == "tryit_failed"
    assert job["error_detail"] == "baseline run failed: RuntimeError"
    assert job["question"] == _ROW["query_context"]  # GET still shows the ask
    assert "Query failed." not in str(job)
    # The single app-wide slot is released for the next steward action.
    assert _fresh_tryit_jobs[lr._TRYIT_ACTIVE_KEY]["status"] == "idle"


def test_candidate_run_error_detail_names_the_run(monkeypatch,
                                                  _fresh_tryit_jobs):
    """X-5: a failure in the SECOND (candidate) run is attributed to it."""
    def stream(question, session_id, candidate):
        def events():
            if candidate is not None:
                yield ErrorEvent(message="internal secret=hunter2")
                return
            yield _summary([])
            yield MessageEvent(content="baseline ok")
            yield DoneEvent()
        return events(), candidate is not None

    monkeypatch.setattr(lr, "_tryit_stream", stream)

    job_id = lr._claim_tryit_job()
    lr._run_tryit_job(job_id, dict(_ROW), _ROW["query_context"])

    job = _fresh_tryit_jobs[job_id]
    assert job["status"] == "error"
    assert job["error"] == "tryit_failed"
    assert job["error_detail"] == "candidate run failed: RuntimeError"
    assert "hunter2" not in str(job)   # never internals/credentials


def test_wall_clamp_marks_job_error(monkeypatch, _fresh_tryit_jobs):
    calls: list = []
    monkeypatch.setattr(lr, "_tryit_stream", _fake_stream_factory(calls))
    monkeypatch.setattr(lr, "_TRYIT_RUN_CLAMP_SECONDS", -1)

    job_id = lr._claim_tryit_job()
    lr._run_tryit_job(job_id, dict(_ROW), _ROW["query_context"])

    job = _fresh_tryit_jobs[job_id]
    assert job["status"] == "error"
    assert job["error"] == "tryit_failed"
    assert job["error_detail"] == "baseline run failed: TimeoutError"


def test_local_mode_runs_report_not_injected(monkeypatch, _fresh_tryit_jobs):
    """X-4: a local/Mode-B environment can't inject the candidate — every
    run reports ``injected=False``, the top level echoes it, and the diff
    carries the honest injectability note (an empty diff must never read
    as "the candidate changed nothing")."""
    calls: list = []
    monkeypatch.setattr(lr, "_tryit_stream",
                        _fake_stream_factory(calls, injectable=False))

    job_id = lr._claim_tryit_job()
    lr._run_tryit_job(job_id, dict(_ROW), _ROW["query_context"])

    result = _fresh_tryit_jobs[job_id]["result"]
    assert result["baseline"]["injected"] is False
    assert result["candidate"]["injected"] is False
    assert result["candidate_injected"] is False
    assert result["diff"]["note"] == (
        "candidate not injectable in this environment")


def test_a_candidate_forwarded_but_not_in_the_runs_seed_reports_not_injected(
        monkeypatch, _fresh_tryit_jobs):
    """The runtime path forwards the candidate, but the route seed it built
    does not hold it (trimmed to fit the routing budget, or unshapeable): the
    run reports ``injected=False`` and the diff says why, so the steward is
    never shown the same prompt twice as a test of the proposal."""
    calls: list = []
    monkeypatch.setattr(lr, "_tryit_stream", _fake_stream_factory(calls, retained=False))

    job_id = lr._claim_tryit_job()
    lr._run_tryit_job(job_id, dict(_ROW), _ROW["query_context"])

    result = _fresh_tryit_jobs[job_id]["result"]
    assert calls[2][2] is not None          # it was forwarded
    assert result["candidate"]["injected"] is False
    assert result["candidate_injected"] is False
    assert result["diff"]["note"] == "candidate did not fit this run's routing context"

# ── Runtime reserved-key pop (ADR-0049 posture) ─────────────────────────────

def test_runtime_pops_tryit_candidate_key(monkeypatch):
    seen: list = []

    def fake_prelude(prompt, candidate=None):
        seen.append(candidate)
        return prompt, [], None

    monkeypatch.setattr(runtime_app, "_prelude_with_planned", fake_prelude)
    payload = {"prompt": "real question", "sessionId": "s-1",
               "tryItCandidate": {"proposed_type": "join_path",
                                  "from_field": "secret.column"}}

    state = runtime_app._new_turn_state(payload, SimpleNamespace(session_id="rt"))

    assert "tryItCandidate" not in payload          # popped unconditionally
    assert seen == [{"proposed_type": "join_path",
                     "from_field": "secret.column"}]
    assert "secret.column" not in state.prompt      # never in the token path
    assert state.raw_prompt == "real question"


def test_runtime_pops_malformed_candidate_too(monkeypatch):
    monkeypatch.setattr(runtime_app, "_prelude_with_planned",
                        lambda prompt, candidate=None: (prompt, [], None))
    payload = {"prompt": "q", "tryItCandidate": "not-a-dict"}

    runtime_app._new_turn_state(payload, SimpleNamespace(session_id="rt"))

    assert "tryItCandidate" not in payload


def test_integrity_entrypoint_pops_reserved_key_at_its_top():
    src = inspect.getsource(runtime_app._invoke_integrity_runtime)
    assert "_pop_tryit_candidate(payload)" in src
    assert "_prelude_with_planned(raw_prompt, tryit_candidate, **routing)" in src


def test_mode_b_ignores_candidate(monkeypatch):
    import agents.orchestrator as orch
    monkeypatch.setenv("SYSTEM_QUERY_MODE", "orchestrator")
    monkeypatch.setattr(orch, "build_graph_first_seed", lambda: None)

    def forbidden(*args, **kwargs):
        raise AssertionError("Mode B must never build the route seed")

    monkeypatch.setattr(orch, "build_route_seed", forbidden)

    result = runtime_app._graph_first_prelude(
        "q", {"proposed_type": "join_path"})
    assert result == ("q", [], None)


def test_prelude_stub_seam_still_tolerates_single_arg(monkeypatch):
    monkeypatch.setattr(runtime_app, "_graph_first_prelude",
                        lambda p: (p, []))
    assert runtime_app._prelude_with_planned("hi") == ("hi", [], None)


# ── Try-it turns never write usage-ledger rows ──────────────────────────────

def test_tryit_session_skips_usage_ledger(monkeypatch):
    recorded = []
    import agents.self_learning as sl
    monkeypatch.setattr(sl, "record_rule_usage",
                        lambda *a, **k: recorded.append("rule") or 1)
    monkeypatch.setattr(runtime_app, "_record_artifact_usage_hook",
                        lambda *a, **k: recorded.append("artifact") or 1)
    monkeypatch.setattr(runtime_app, "_maybe_bump_route_intersection",
                        lambda *a, **k: recorded.append("route") or 1)

    bumped = runtime_app._record_rule_usage_hook(
        {}, "real user question", precomputed_ids={"ir-x"},
        prefetched_rows=[{"neptune_node_id": "ir-x"}],
        route_ctx={"seed": [], "tools": [], "systems": None},
        session_id="tryit-abc123-a")

    assert bumped == 0
    assert recorded == []


def test_non_tryit_session_still_records(monkeypatch):
    recorded = []
    import agents.self_learning as sl
    monkeypatch.setattr(sl, "record_rule_usage",
                        lambda *a, **k: recorded.append("rule") or 1)
    monkeypatch.setattr(runtime_app, "_record_artifact_usage_hook",
                        lambda *a, **k: recorded.append("artifact") or 1)

    bumped = runtime_app._record_rule_usage_hook(
        {}, "real user question", precomputed_ids={"ir-x"},
        prefetched_rows=[{"neptune_node_id": "ir-x"}],
        session_id="chat-session-1")

    assert bumped == 1
    assert recorded == ["rule", "artifact"]


def test_both_call_sites_thread_session_id():
    legacy = inspect.getsource(runtime_app._run_orchestrator_base)
    integrity = inspect.getsource(runtime_app._integrity_post_turn_learning)
    assert "session_id=s.session_id" in legacy
    assert "session_id=session_id" in integrity


# ── Chat-client extra_payload seam ──────────────────────────────────────────

def test_extra_payload_additive_only():
    from agents.chat_client import _compose_runtime_payload

    base = {"prompt": "q", "sessionId": "s-1"}
    merged = _compose_runtime_payload(
        base, {"tryItCandidate": {"a": 1}, "prompt": "OVERRIDE"})
    assert merged["tryItCandidate"] == {"a": 1}     # additive key rides along
    assert merged["prompt"] == "q"                  # base keys always win
    assert _compose_runtime_payload(base, None) is base   # None = unchanged


def test_cloud_tryit_stream_forwards_candidate_only_when_present(monkeypatch):
    sent = []

    class FakeClient:
        def stream(self, **kwargs):
            sent.append(kwargs)
            return iter(())

    import agents.chat_client as chat_client
    monkeypatch.setattr(lr, "_tryit_jobs", lambda: None)  # unused here
    monkeypatch.setattr(chat_client, "AgentCoreChatClient", lambda: FakeClient())
    from agents import config as config_module
    monkeypatch.setattr(config_module, "get_config",
                        lambda: SimpleNamespace(has_runtime=True))

    _, injected_a = lr._tryit_stream("q", "tryit-j-a", None)
    _, injected_b = lr._tryit_stream("q", "tryit-j-b",
                                     {"proposed_type": "join_path"})

    assert sent[0]["extra_payload"] is None
    assert sent[0]["write_memory"] is False
    assert sent[1]["extra_payload"] == {
        "tryItCandidate": {"proposed_type": "join_path"}}
    # X-4: injected mirrors the actual forwarding — runtime path only.
    assert injected_a is False
    assert injected_b is True


def test_local_tryit_stream_never_injects(monkeypatch):
    """X-4: no runtime ⇒ the candidate has nowhere to go (the route seed is
    Mode A's runtime prelude) — ``injected`` is honestly False even when a
    candidate is supplied, and the local client never sees it."""
    sent = []

    class FakeLocal:
        def stream(self, **kwargs):
            sent.append(kwargs)
            return iter(())

    import agents.chat_client as chat_client
    monkeypatch.setattr(chat_client, "LocalChatClient", lambda: FakeLocal())
    from agents import config as config_module
    monkeypatch.setattr(config_module, "get_config",
                        lambda: SimpleNamespace(has_runtime=False))

    _, injected = lr._tryit_stream("q", "tryit-j-b",
                                   {"proposed_type": "join_path"})

    assert injected is False
    assert "extra_payload" not in sent[0]
    assert sent[0]["write_memory"] is False
