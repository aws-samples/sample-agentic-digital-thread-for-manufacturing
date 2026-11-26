# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``POST /api/admin/evals/run-upload`` — the uploaded customer golden set
(ADR-0064, Wave-3 rows of the Pinned-by table).

Offline: no AWS, no network. The registries are in-memory
``JobStore(..., table=None)`` and the admin posture is injected by a PURE-ASGI
middleware — ``BaseHTTPMiddleware`` would consume the request body stream, and
one pin below asserts the handler never touched that stream, so the middleware
must not either (the ``test_eval_findings_routes.py`` harness, reused).

Pins:

* **#1 nothing of the upload is echoed or logged** — a canary planted in the
  question TEXT (and in a rejected body, a rejected query-parameter NAME and a
  secret-shaped body) appears in NO response body and NO log record; the
  loader's one question-repr message is replaced rather than surfaced. The
  route reads and writes NO file (``open``/``Path.write_*``/``tempfile`` spies)
  and never reaches ``load_golden_set``/``golden_file_sha``.
* **#6 run-ALL is structural** — ``variant`` is the ONLY accepted query
  parameter (``count``/``ids``/``category``/``subset``/``limit``/… ⇒ 422
  ``unknown_query_params``, runner never called), the FULL parsed list in file
  order reaches the runner, and ``/run``'s ``extra="forbid"`` body model is
  untouched (``test_eval_routes.py`` runs unedited).
* **#10 the ingress cap runs BEFORE parsing** — an oversized
  ``Content-Length`` is 413 with the stream NEVER read (spy), and a header
  that lies or is absent still hits the ceiling mid-stream.
* **#11 the question-count ceiling is 422** ``too_many_questions`` — not a 409:
  nothing is in flight, the set is too big for one run.
* **#12 secret-shaped content is refused** with the pinned reason and a detail
  that never echoes it (the ``variant`` label too — the one place a steward
  types free text a report persists).
* the sha the route returns equals ``eval_core.golden_file_sha`` of the SAME
  bytes on disk — the byte-exact binding a steward reconciles against (#5 at
  the route level), and the run it starts is badged ``uploaded``.
* 403 for a non-admin, and the route lives on the ONE existing evals mount.
"""
from __future__ import annotations

import builtins
import json
import logging
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import agents.eval_core as eval_core  # noqa: E402
import agents.eval_routes as eval_routes  # noqa: E402
import agents.eval_runs as er  # noqa: E402
from agents.events import DoneEvent, MessageEvent  # noqa: E402
from agents.job_store import JobStore  # noqa: E402

UPLOAD_URL = "/api/admin/evals/run-upload"

#: Stands in for CUSTOMER question text. Distinctive enough that it fails the
#: pin whatever key it might hide under.
Q_CANARY = "ZCUSTOMERQUESTIONCANARY7742"

#: A client-supplied query-parameter NAME is client text too.
PARAM_CANARY = "ZPARAMNAMECANARY7742"

#: The AWS access-key-ID shape (``untrusted_text.SECRET_SHAPE_PATTERNS[0]``).
SECRET_CANARY = "AKIAIOSFODNN7EXAMPLE"  # nosec B105 - offline test fixture


# ── harness ─────────────────────────────────────────────────────────────────

class _AdminScopeMiddleware:
    """PURE-ASGI admin inject: writes ``scope["state"]`` (what
    ``request.state`` reads) WITHOUT wrapping the body stream."""

    def __init__(self, app, is_admin: bool):
        self._app = app
        self._is_admin = is_admin

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            scope.setdefault("state", {})
            scope["state"]["is_admin"] = self._is_admin
        await self._app(scope, receive, send)


def _build_client(is_admin: bool) -> TestClient:
    """Mount the PARENT router — the route's registration is under test too."""
    app = FastAPI()
    app.include_router(eval_routes.router)
    app.add_middleware(_AdminScopeMiddleware, is_admin=is_admin)
    return TestClient(app)


@pytest.fixture
def client():
    return _build_client(is_admin=True)


@pytest.fixture(autouse=True)
def _fresh_stores(monkeypatch):
    monkeypatch.setattr(er, "_JOBS", JobStore("eval-run", table=None))
    monkeypatch.setattr(er, "_REPORTS", JobStore("eval-report", table=None))
    monkeypatch.setattr(er, "_REPORTS_SHORT",
                        JobStore("eval-report", table=None))


@pytest.fixture
def started(monkeypatch):
    """Capture what the route hands the runner (the runner itself is Wave-2
    territory — ``test_eval_runs_uploaded.py`` owns its behaviour)."""
    calls: list = []

    def fake_start(variant=None, *, goldens=None, golden_sha=None,
                   golden_source=eval_core.GOLDEN_SOURCE_PACKAGED):
        calls.append({"variant": variant, "goldens": goldens,
                      "golden_sha": golden_sha,
                      "golden_source": golden_source})
        return "rid-upload", None

    monkeypatch.setattr(er, "start_run", fake_start)
    return calls


@pytest.fixture
def refused(monkeypatch):
    """Drive ONE runner refusal reason through the route."""
    def _refuse(reason: str) -> list:
        calls: list = []

        def fake_start(variant=None, **kwargs):
            calls.append(variant)
            return None, reason

        monkeypatch.setattr(er, "start_run", fake_start)
        return calls
    return _refuse


def _golden_bytes(count: int = 3, canary: str = Q_CANARY) -> bytes:
    """A VALID golden set whose question text carries the canary."""
    questions = [
        {"id": f"q-{i}", "text": f"{canary} question {i}?",
         "expect": {"answer_must_match": ["the answer"]}}
        for i in range(count)]
    return json.dumps({"version": 1, "questions": questions}).encode("utf-8")


def _post(client: TestClient, content, **params):
    url = UPLOAD_URL
    if params:
        url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
    return client.post(url, content=content,
                       headers={"content-type": "application/json"})


def _refusal(resp) -> tuple:
    body = resp.json()["detail"]
    return body["reason"], body["detail"]


def _log_text(caplog) -> str:
    """Every APPLICATION log record's message.

    Scoped to the ``agents.*`` tree on purpose: the test client's own httpx
    record echoes the request URL (a harness artefact, not a server log), and
    a pin that failed on it would say nothing about what the server writes.
    """
    return "\n".join(r.getMessage() for r in caplog.records
                     if r.name.startswith("agents"))


# ── admin posture ───────────────────────────────────────────────────────────

def test_non_admin_upload_forbidden():
    client = _build_client(is_admin=False)
    assert _post(client, _golden_bytes()).status_code == 403


def test_non_admin_upload_never_reaches_the_runner(monkeypatch):
    calls: list = []
    monkeypatch.setattr(er, "start_run",
                        lambda *a, **k: calls.append(k) or ("x", None))
    assert _post(_build_client(is_admin=False),
                 _golden_bytes()).status_code == 403
    assert calls == []


# ── happy path: byte-exact sha, run-ALL, uploaded provenance ────────────────

def test_upload_happy_path_202_shape(client, started):
    raw = _golden_bytes(3)
    resp = _post(client, raw)
    assert resp.status_code == 202
    assert resp.json() == {
        "run_id": "rid-upload", "status": "running",
        "golden_set_source": "uploaded",
        "golden_set_sha": eval_core.golden_bytes_sha(raw),
        "question_count": 3,
    }


def test_returned_sha_equals_the_file_sha_of_the_same_bytes(
        client, started, tmp_path):
    """The binding a steward reconciles: the sha the tab shows is the sha of
    the file they hold (``golden_file_sha`` over the same bytes)."""
    raw = _golden_bytes(2)
    on_disk = tmp_path / "their-set.txt"
    on_disk.write_bytes(raw)
    resp = _post(client, raw)
    assert resp.json()["golden_set_sha"] == eval_core.golden_file_sha(
        str(on_disk))


def test_the_full_parsed_list_reaches_the_runner_in_file_order(client,
                                                              started):
    raw = _golden_bytes(5)
    assert _post(client, raw).status_code == 202
    goldens = started[0]["goldens"]
    assert [q["id"] for q in goldens] == [f"q-{i}" for i in range(5)]
    assert started[0]["golden_source"] == eval_core.GOLDEN_SOURCE_UPLOADED
    assert started[0]["golden_sha"] == eval_core.golden_bytes_sha(raw)


def test_variant_is_forwarded_to_the_runner(client, started):
    assert _post(client, _golden_bytes(), variant="arm-b").status_code == 202
    assert started[0]["variant"] == "arm-b"


def test_no_variant_is_none(client, started):
    assert _post(client, _golden_bytes()).status_code == 202
    assert started[0]["variant"] is None


# ── #6 run-ALL is structural: variant is the ONLY query parameter ───────────

@pytest.mark.parametrize("params", [
    {"count": "3"},
    {"ids": "q-1"},
    {"question_ids": "q-1"},
    {"category": "cross-system"},
    {"subset": "half"},
    {"limit": "5"},
    {"strict": "true"},
    {"variant": "arm-b", "count": "3"},
    {"repeats": "2"},
], ids=["count", "ids", "question_ids", "category", "subset", "limit",
        "strict", "count-with-variant", "repeats"])
def test_any_other_query_parameter_is_422(client, started, params):
    resp = _post(client, _golden_bytes(), **params)
    assert resp.status_code == 422
    assert _refusal(resp)[0] == "unknown_query_params"
    assert started == []


def test_the_rejected_parameter_name_is_never_echoed(client, started, caplog):
    caplog.set_level(logging.DEBUG)
    resp = _post(client, _golden_bytes(), **{PARAM_CANARY: "1"})
    assert resp.status_code == 422
    assert PARAM_CANARY not in resp.text
    assert PARAM_CANARY not in _log_text(caplog)


def test_the_allowlist_is_exactly_variant():
    assert eval_routes.UPLOAD_QUERY_PARAMS == frozenset({"variant"})


def test_post_run_body_model_is_still_run_all_only():
    """``/run`` is byte-unchanged (ADR-0064 D3): its ``extra="forbid"`` model
    still carries the single ``variant`` key, so an inline golden set could
    not have been smuggled into the existing route."""
    assert eval_routes.RunRequest.model_config["extra"] == "forbid"
    assert set(eval_routes.RunRequest.model_fields) == {"variant"}


def test_the_upload_route_is_on_the_one_existing_mount():
    paths = {getattr(r, "path", "") for r in eval_routes.router.routes}
    assert UPLOAD_URL in paths
    assert "/api/admin/evals/run" in paths


# ── #10 the ingress cap runs BEFORE parsing ─────────────────────────────────

@pytest.fixture()
def stream_spy(monkeypatch):
    """Record every ``Request.stream()`` invocation (the body-read seam)."""
    import starlette.requests as sr
    calls: list = []
    original = sr.Request.stream

    def spy(self):
        calls.append(self.url.path)
        return original(self)

    monkeypatch.setattr(sr.Request, "stream", spy)
    return calls


def test_oversized_content_length_is_413_with_the_stream_untouched(
        client, started, stream_spy):
    resp = client.post(
        UPLOAD_URL, content=_golden_bytes(),
        headers={"content-type": "application/json",
                 "content-length": str(
                     eval_routes.MAX_GOLDEN_UPLOAD_BYTES + 1)})
    assert resp.status_code == 413
    assert _refusal(resp)[0] == "payload_too_large"
    assert stream_spy == []
    assert started == []


def test_lying_content_length_still_hits_the_stream_ceiling(client, started):
    oversized = b'{"version": 1, "questions": "' + b"x" * (
        eval_routes.MAX_GOLDEN_UPLOAD_BYTES + 64) + b'"}'
    resp = client.post(UPLOAD_URL, content=oversized,
                       headers={"content-type": "application/json",
                                "content-length": "12"})
    assert resp.status_code == 413
    assert _refusal(resp)[0] == "payload_too_large"
    assert started == []


def test_absent_content_length_still_hits_the_stream_ceiling(client, started):
    """Chunked upload (no ``Content-Length`` at all) — same ceiling."""
    chunk = b"x" * 64 * 1024

    def chunks():
        yield b'{"version": 1, "questions": "'
        for _ in range(6):
            yield chunk
        yield b'"}'

    resp = client.post(UPLOAD_URL, content=chunks(),
                       headers={"content-type": "application/json"})
    assert resp.status_code == 413
    assert started == []


def test_the_cap_is_the_documented_256kb():
    assert eval_routes.MAX_GOLDEN_UPLOAD_BYTES == 256 * 1024


def test_a_body_just_under_the_cap_is_accepted(client, started):
    """The ceiling is a ceiling, not a smaller de-facto bound."""
    padded = json.dumps({
        "version": 1, "note": "x" * 2000,
        "questions": [{"id": "q-1", "text": "a question?",
                       "expect": {"answer_must_match": ["a"]}}],
    }).encode("utf-8")
    assert len(padded) < eval_routes.MAX_GOLDEN_UPLOAD_BYTES
    assert _post(client, padded).status_code == 202


# ── bytes-level refusals: BOM / non-UTF-8 carry the computed sha ────────────

def test_a_utf8_bom_is_422_invalid_json_with_the_computed_sha(client, started):
    raw = b"\xef\xbb\xbf" + _golden_bytes()
    resp = _post(client, raw)
    assert resp.status_code == 422
    reason, detail = _refusal(resp)
    assert reason == "invalid_json"
    assert eval_core.golden_bytes_sha(raw) in detail
    assert started == []


def test_non_utf8_bytes_are_422_invalid_json_with_the_computed_sha(client,
                                                                  started):
    raw = b'{"version": 1, "questions": [{"id": "q-1", "text": "\xff\xfe"}]}'
    resp = _post(client, raw)
    assert resp.status_code == 422
    reason, detail = _refusal(resp)
    assert reason == "invalid_json"
    assert eval_core.golden_bytes_sha(raw) in detail
    assert started == []


def test_malformed_json_is_422_invalid_json_without_echoing_content(
        client, started, caplog):
    caplog.set_level(logging.DEBUG)
    resp = _post(client, f'{{"questions": "{Q_CANARY}"'.encode("utf-8"))
    assert resp.status_code == 422
    assert _refusal(resp)[0] == "invalid_json"
    assert Q_CANARY not in resp.text
    assert Q_CANARY not in _log_text(caplog)
    assert started == []


# ── schema refusals: 422 invalid_golden_set, never a content echo ───────────

def test_wrong_version_is_422_invalid_golden_set(client, started):
    raw = json.dumps({"version": 2, "questions": [
        {"id": "q-1", "text": "a?", "expect": {}}]}).encode("utf-8")
    resp = _post(client, raw)
    assert resp.status_code == 422
    assert _refusal(resp)[0] == "invalid_golden_set"
    assert started == []


def test_empty_question_list_is_422_invalid_golden_set(client, started):
    resp = _post(client, b'{"version": 1, "questions": []}')
    assert resp.status_code == 422
    assert _refusal(resp)[0] == "invalid_golden_set"
    assert started == []


def test_a_question_missing_expect_never_echoes_its_text(client, started,
                                                         caplog):
    """The ONE loader message that embeds a repr of the offending question is
    REPLACED — question text must not reach a response body or a log."""
    caplog.set_level(logging.DEBUG)
    raw = json.dumps({"version": 1, "questions": [
        {"id": "q-1", "text": f"{Q_CANARY} question?"}]}).encode("utf-8")
    resp = _post(client, raw)
    assert resp.status_code == 422
    reason, detail = _refusal(resp)
    assert reason == "invalid_golden_set"
    assert "missing id/text/expect" in detail
    assert Q_CANARY not in resp.text
    assert Q_CANARY not in _log_text(caplog)
    assert started == []


def test_duplicate_question_ids_are_422_invalid_golden_set(client, started):
    raw = json.dumps({"version": 1, "questions": [
        {"id": "q-1", "text": "a?", "expect": {"answer_must_match": ["a"]}},
        {"id": "q-1", "text": "b?", "expect": {"answer_must_match": ["b"]}},
    ]}).encode("utf-8")
    resp = _post(client, raw)
    assert resp.status_code == 422
    assert _refusal(resp)[0] == "invalid_golden_set"


def test_non_object_questions_are_422_not_a_500(client, started):
    """A list of strings makes the loader raise a non-``ValueError``; the route
    still refuses honestly rather than surfacing a server error."""
    resp = _post(client, json.dumps(
        {"version": 1, "questions": [Q_CANARY]}).encode("utf-8"))
    assert resp.status_code == 422
    assert _refusal(resp)[0] == "invalid_golden_set"
    assert Q_CANARY not in resp.text
    assert started == []


def test_every_refusal_detail_is_bounded(client, started):
    """No loader message reaches a client unbounded."""
    raw = json.dumps({"version": 1, "questions": [
        {"id": "q-1", "text": "a?", "expect": {"answer_must_match": ["a"]},
         "leak_class": "x" * 4000}]}).encode("utf-8")
    resp = _post(client, raw)
    assert resp.status_code == 422
    assert len(_refusal(resp)[1]) <= eval_routes.DETAIL_MAX_CHARS


# ── #11 the question-count ceiling is a 422, never a 409 ───────────────────

def test_over_the_question_ceiling_is_422_too_many_questions(client):
    """Through the REAL runner refusal (the ceiling has ONE home:
    ``eval_runs.MAX_UPLOADED_QUESTIONS``)."""
    resp = _post(client, _golden_bytes(er.MAX_UPLOADED_QUESTIONS + 1))
    assert resp.status_code == 422
    reason, detail = _refusal(resp)
    assert reason == "too_many_questions"
    assert str(er.MAX_UPLOADED_QUESTIONS) in detail
    assert Q_CANARY not in resp.text


def test_at_the_question_ceiling_the_upload_is_accepted(client, started):
    resp = _post(client, _golden_bytes(er.MAX_UPLOADED_QUESTIONS))
    assert resp.status_code == 202
    assert resp.json()["question_count"] == er.MAX_UPLOADED_QUESTIONS


def test_the_ceiling_refusal_did_not_claim_the_run_slot(client):
    """A refused upload must leave the shared one-run lock free."""
    _post(client, _golden_bytes(er.MAX_UPLOADED_QUESTIONS + 1))
    assert er._jobs().get("active") is None


# ── the shared one-run lock: 409 with the SAME pinned details as /run ───────

def test_an_active_run_is_409_with_the_pinned_detail(client, refused):
    refused(er.REASON_ACTIVE)
    resp = _post(client, _golden_bytes())
    assert resp.status_code == 409
    reason, detail = _refusal(resp)
    assert reason == "active_run"
    assert detail == eval_routes._DETAIL_ACTIVE == "eval run already active"


def test_a_missing_golden_set_reason_is_409_with_its_own_detail(client,
                                                               refused):
    refused(er.REASON_NO_GOLDENS)
    resp = _post(client, _golden_bytes())
    assert resp.status_code == 409
    reason, detail = _refusal(resp)
    assert reason == "no_goldens"
    assert detail == eval_routes._DETAIL_NO_GOLDENS


def test_the_refusal_reasons_are_all_distinct():
    reasons = [eval_routes.REASON_TOO_LARGE, eval_routes.REASON_INVALID_JSON,
               eval_routes.REASON_INVALID_GOLDEN,
               eval_routes.REASON_TOO_MANY_QUESTIONS,
               eval_routes.REASON_UNKNOWN_PARAMS,
               eval_routes.REASON_UPLOAD_ACTIVE,
               eval_routes.REASON_UPLOAD_NO_GOLDENS]
    assert len(set(reasons)) == len(reasons)


# ── #12 secret-shaped content and the variant label ────────────────────────

def test_secret_shaped_content_is_refused_without_an_echo(client, started,
                                                          caplog):
    caplog.set_level(logging.DEBUG)
    raw = json.dumps({"version": 1, "questions": [
        {"id": "q-1", "text": f"why did {SECRET_CANARY} fail?",
         "expect": {"answer_must_match": ["a"]}}]}).encode("utf-8")
    resp = _post(client, raw)
    assert resp.status_code == eval_routes.REJECT_STATUS
    reason, detail = _refusal(resp)
    assert reason == "secret_like_content"
    assert SECRET_CANARY not in resp.text
    assert SECRET_CANARY not in detail
    assert SECRET_CANARY not in _log_text(caplog)
    assert started == []


def test_a_secret_shaped_variant_label_is_refused(client, started):
    """The ``variant`` label is the ONE place a steward types free text that a
    report persists, so it gets the same treatment (ADR-0064 D5)."""
    resp = _post(client, _golden_bytes(), variant=SECRET_CANARY)
    assert resp.status_code == eval_routes.REJECT_STATUS
    assert _refusal(resp)[0] == "secret_like_content"
    assert SECRET_CANARY not in resp.text
    assert started == []


def test_a_bearer_token_shaped_body_is_refused(client, started):
    raw = json.dumps({"version": 1, "questions": [
        {"id": "q-1", "text": "bearer abcdef0123456789abcdef",
         "expect": {"answer_must_match": ["a"]}}]}).encode("utf-8")
    assert _post(client, raw).status_code == eval_routes.REJECT_STATUS
    assert started == []


# ── #1 no echo, no log record, no filesystem seam ──────────────────────────

def test_the_happy_path_logs_only_the_sha_and_the_count(client, started,
                                                        caplog):
    caplog.set_level(logging.DEBUG)
    raw = _golden_bytes(2)
    resp = _post(client, raw, variant="arm-b")
    assert resp.status_code == 202
    text = _log_text(caplog)
    assert Q_CANARY not in text
    assert eval_core.golden_bytes_sha(raw) in text
    assert "question_count=2" in text


def test_the_response_never_carries_question_text(client, started):
    resp = _post(client, _golden_bytes(3))
    assert Q_CANARY not in resp.text


def test_the_route_touches_no_filesystem_seam(client, started, monkeypatch):
    """No write seam and no read seam: an uploaded set must never acquire a
    filesystem home (ADR-0064 D4)."""
    touched: list = []
    real_open = builtins.open

    def spy_open(file, mode="r", *args, **kwargs):
        if any(flag in str(mode) for flag in ("w", "a", "x", "+")):
            touched.append(("open", str(file)))
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", spy_open)
    for name in ("write_text", "write_bytes"):
        monkeypatch.setattr(Path, name,
                            lambda self, *a, **k: touched.append((name, self)))
    for name in ("NamedTemporaryFile", "TemporaryFile", "mkstemp", "mkdtemp"):
        monkeypatch.setattr(tempfile, name,
                            lambda *a, **k: touched.append((name, a)))
    monkeypatch.setattr(shutil, "copyfile",
                        lambda *a, **k: touched.append(("copyfile", a)))
    assert _post(client, _golden_bytes()).status_code == 202
    assert touched == []


def test_the_route_reads_no_golden_file(client, started, monkeypatch):
    """The uploaded path reaches ``parse_golden_set`` ONLY — never the
    packaged-path readers (the server's ONE filesystem golden source stays
    the packaged demo set)."""
    reads: list = []
    monkeypatch.setattr(eval_core, "load_golden_set",
                        lambda path: reads.append(path))
    monkeypatch.setattr(eval_core, "golden_file_sha",
                        lambda path: reads.append(path))
    assert _post(client, _golden_bytes()).status_code == 202
    assert reads == []


# ── end to end: the run the route starts is badged uploaded and redacted ───

def test_the_started_run_is_badged_uploaded_and_persists_no_text(client,
                                                                monkeypatch):
    """Route → runner → worker → report, with the worker INLINE: the finished
    report carries the uploaded provenance, the caller's sha and no
    answer-derived text, and no persisted record carries question text."""
    class _InlineThread:
        def __init__(self, target=None, args=(), kwargs=None, daemon=None):
            self._target, self._args, self._kwargs = target, args, kwargs or {}

        def start(self):
            self._target(*self._args, **self._kwargs)

    monkeypatch.setattr(er, "threading", SimpleNamespace(Thread=_InlineThread))
    monkeypatch.setattr(er, "_stream_factory", lambda prompt, session_id: iter(
        [MessageEvent(content="the answer"), DoneEvent(text="the answer")]))

    raw = _golden_bytes(2)
    resp = _post(client, raw, variant="arm-b")
    assert resp.status_code == 202
    run_id = resp.json()["run_id"]

    report = client.get(f"/api/admin/evals/runs/{run_id}").json()["report"]
    assert report["golden_set_source"] == "uploaded"
    assert report["golden_set"] == "uploaded"
    assert report["golden_set_sha"] == eval_core.golden_bytes_sha(raw)
    assert report[eval_core.ANSWER_TEXT_REDACTED_KEY] is True
    assert all(row["answer_excerpt"] == "" for row in report["questions"])
    assert Q_CANARY not in json.dumps(report)

    history = client.get("/api/admin/evals/runs").json()["runs"][0]
    assert history["golden_set_source"] == "uploaded"
    assert history["question_count"] == 2
    assert Q_CANARY not in json.dumps(history)
