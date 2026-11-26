# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Admin eval routes (agents/eval_routes.py) — pinned API contract.

Route harness mirrors ``test_admin_feedback_exploration.py`` (admin-inject
middleware; offline). Pins:

* 403 for non-admins on ALL THREE routes;
* **run-ALL only** — POST with any subset-selection key (``count``,
  ``category``, ``ids``, ``question_ids``) is 422 (pydantic
  ``extra="forbid"``), and an end-to-end POST drives the FULL golden list
  through the runner seam;
* 202 ``{run_id, status: "running"}`` happy path; ``variant`` is the only
  accepted body key;
* 409 with DISTINCT details for active-run vs missing/empty golden set;
* GET /runs is the bounded newest-first history; GET /runs/{id} returns
  progress while running, the full report when complete, 404 unknown;
* the eval router is wired into agents/server.py (source pin — importing
  the full server app pulls half the platform, so the include line itself
  is asserted).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import agents.eval_routes as eval_routes  # noqa: E402
import agents.eval_runs as er  # noqa: E402
from agents.events import DoneEvent, MessageEvent  # noqa: E402
from agents.job_store import JobStore  # noqa: E402

AGENTS_DIR = Path(__file__).resolve().parents[2] / "agents"


class _AdminInjectMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, is_admin: bool):
        super().__init__(app)
        self._is_admin = is_admin

    async def dispatch(self, request, call_next):
        request.state.is_admin = self._is_admin
        return await call_next(request)


def _build_client(is_admin: bool) -> TestClient:
    app = FastAPI()
    app.include_router(eval_routes.router)
    app.add_middleware(_AdminInjectMiddleware, is_admin=is_admin)
    return TestClient(app)


@pytest.fixture
def client():
    return _build_client(is_admin=True)


@pytest.fixture(autouse=True)
def _fresh_stores(monkeypatch):
    monkeypatch.setattr(er, "_JOBS", JobStore("eval-run", table=None))
    monkeypatch.setattr(er, "_REPORTS", JobStore("eval-report", table=None))


# ── 403 non-admin on all three routes ───────────────────────────────────────

def test_non_admin_post_run_forbidden():
    client = _build_client(is_admin=False)
    assert client.post("/api/admin/evals/run", json={}).status_code == 403


def test_non_admin_get_runs_forbidden():
    client = _build_client(is_admin=False)
    assert client.get("/api/admin/evals/runs").status_code == 403


def test_non_admin_get_run_detail_forbidden():
    client = _build_client(is_admin=False)
    assert client.get("/api/admin/evals/runs/abc").status_code == 403


# ── POST /run — run-ALL-only body pin ───────────────────────────────────────

@pytest.mark.parametrize("body", [
    {"count": 3},
    {"category": "cross-system"},
    {"ids": ["q-1"]},
    {"question_ids": ["q-1", "q-2"]},
    {"variant": "arm-b", "count": 3},   # subset key 422s even with variant
    {"strict": True},                    # CLI-lane features stay CLI-only
], ids=["count", "category", "ids", "question_ids",
        "count-with-variant", "strict"])
def test_post_run_rejects_subset_selection_keys(client, monkeypatch, body):
    """Run-ALL only (pinned): ANY key beyond the optional variant label is a
    422 — and the runner is never invoked."""
    calls = []
    monkeypatch.setattr(er, "start_run",
                        lambda variant=None: calls.append(variant) or ("x", None))
    resp = client.post("/api/admin/evals/run", json=body)
    assert resp.status_code == 422
    assert calls == []


def test_post_run_happy_path_202(client, monkeypatch):
    monkeypatch.setattr(er, "start_run", lambda variant=None: ("rid-1", None))
    resp = client.post("/api/admin/evals/run", json={})
    assert resp.status_code == 202
    assert resp.json() == {"run_id": "rid-1", "status": "running"}


def test_post_run_accepts_optional_variant(client, monkeypatch):
    seen = []

    def fake_start(variant=None):
        seen.append(variant)
        return "rid-2", None

    monkeypatch.setattr(er, "start_run", fake_start)
    resp = client.post("/api/admin/evals/run", json={"variant": "arm-b"})
    assert resp.status_code == 202
    assert seen == ["arm-b"]


def test_post_run_empty_body_allowed(client, monkeypatch):
    seen = []

    def fake_start(variant=None):
        seen.append(variant)
        return "rid-3", None

    monkeypatch.setattr(er, "start_run", fake_start)
    resp = client.post("/api/admin/evals/run")
    assert resp.status_code == 202
    assert seen == [None]


# ── 409 distinct details ────────────────────────────────────────────────────

def test_post_run_409_active_run_detail(client, monkeypatch):
    monkeypatch.setattr(er, "start_run",
                        lambda variant=None: (None, er.REASON_ACTIVE))
    resp = client.post("/api/admin/evals/run", json={})
    assert resp.status_code == 409
    assert resp.json()["detail"] == "eval run already active"


def test_post_run_409_no_goldens_detail(client, monkeypatch):
    monkeypatch.setattr(er, "start_run",
                        lambda variant=None: (None, er.REASON_NO_GOLDENS))
    resp = client.post("/api/admin/evals/run", json={})
    assert resp.status_code == 409
    assert resp.json()["detail"] == "golden set missing or empty"


def test_409_details_are_distinct():
    assert eval_routes._DETAIL_ACTIVE != eval_routes._DETAIL_NO_GOLDENS


# ── GET /runs + GET /runs/{id} ──────────────────────────────────────────────

def test_get_runs_returns_bounded_newest_first_history(client):
    for i in range(60):
        er._update_index({"run_id": f"run-{i}", "run_path": "server",
                          "started_at": i})
    body = client.get("/api/admin/evals/runs").json()
    assert len(body["runs"]) == 50
    assert body["runs"][0]["run_id"] == "run-59"
    assert body["runs"][0]["run_path"] == "server"


def test_get_runs_empty_state(client):
    assert client.get("/api/admin/evals/runs").json() == {"runs": []}


def test_get_run_progress_while_running(client):
    er._jobs()["rid-run"] = {"status": "running",
                             "progress": {"current": 2, "total": 10}}
    body = client.get("/api/admin/evals/runs/rid-run").json()
    assert body["status"] == "running"
    assert body["progress"] == {"current": 2, "total": 10}
    assert "report" not in body


def test_get_run_full_report_when_complete(client):
    er._jobs()["rid-done"] = {"status": "complete",
                              "progress": {"current": 2, "total": 2}}
    er._reports()["rid-done"] = {"run_id": "rid-done", "run_path": "server",
                                 "suite": {"pass": 2, "fail": 0},
                                 "questions": []}
    body = client.get("/api/admin/evals/runs/rid-done").json()
    assert body["status"] == "complete"
    assert body["report"]["run_path"] == "server"


def test_get_run_unknown_404(client):
    assert client.get("/api/admin/evals/runs/nope").status_code == 404


# ── end-to-end: POST drives the FULL golden list through the seam ───────────

def test_post_run_streams_full_golden_list(client, monkeypatch, tmp_path):
    """Route → start_run → worker → seam: the seam sees EVERY golden (the
    run-ALL-only pin, end to end), sessions are eval-run- prefixed, and the
    finished run serves its run_path="server" report through the route."""
    questions = [
        {"id": f"q-{i}", "text": f"question {i}?",
         "expect": {"answer_must_match": ["the answer"]}}
        for i in range(4)]
    golden_path = tmp_path / "goldens.json"
    golden_path.write_text(json.dumps({"version": 1, "questions": questions}))
    monkeypatch.setattr(er, "_golden_set_path", lambda: str(golden_path))

    class _InlineThread:
        def __init__(self, target=None, args=(), kwargs=None, daemon=None):
            self._target, self._args, self._kwargs = target, args, kwargs or {}

        def start(self):
            self._target(*self._args, **self._kwargs)

    monkeypatch.setattr(er, "threading",
                        SimpleNamespace(Thread=_InlineThread))
    calls: list[tuple] = []

    def factory(prompt, session_id):
        calls.append((prompt, session_id))

        def gen():
            yield MessageEvent(content="the answer")
            yield DoneEvent(text="the answer")
        return gen()

    monkeypatch.setattr(er, "_stream_factory", factory)

    resp = client.post("/api/admin/evals/run", json={})
    assert resp.status_code == 202
    run_id = resp.json()["run_id"]
    assert len(calls) == len(questions)  # the FULL list reached the seam
    assert [p for p, _ in calls] == [q["text"] for q in questions]
    assert all(sid.startswith("eval-run-") for _, sid in calls)

    detail = client.get(f"/api/admin/evals/runs/{run_id}").json()
    assert detail["status"] == "complete"
    assert detail["report"]["run_path"] == "server"
    assert len(detail["report"]["questions"]) == len(questions)

    history = client.get("/api/admin/evals/runs").json()
    assert history["runs"][0]["run_id"] == run_id
    assert history["runs"][0]["run_path"] == "server"


# ── server wiring pin ───────────────────────────────────────────────────────

def test_eval_router_is_wired_into_server():
    """Importing the full server app pulls half the platform, so the include
    line itself is pinned at source level (the include_router block)."""
    source = (AGENTS_DIR / "server.py").read_text(encoding="utf-8")
    assert "from .eval_routes import router as eval_router" in source
    assert "app.include_router(eval_router)" in source
