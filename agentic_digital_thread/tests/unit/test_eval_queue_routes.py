# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Review-queue routes (agents/eval_queue_routes.py) — pinned API contract
(ADR-0062 D3/D4/D9).

Offline. The harness mirrors ``test_eval_routes.py`` (admin-inject middleware
over the PARENT router, so the child's inclusion is exercised, not assumed)
plus ``test_eval_queue.py``'s spy persistence seam over in-memory
``JobStore(..., table=None)`` stores.

Pins:

* 403 for non-admins on EVERY route (and a rejected POST writes nothing);
* 422 on unknown body keys and on a verdict outside the pinned enum;
* a secret-shaped note is a 400 whose detail NEVER echoes the note — nor does
  any log record emitted during the call;
* ``GET /queue`` performs ZERO writes (computed on read — spy seam);
* the machine pre-score reaches the client under its literal ``"triage-only"``
  label with NO verdict/score/confidence field anywhere in the block;
* a decided item whose source has expired surfaces ``source_expired: True``
  with its decision — never a 500;
* **no refresh route exists** (route-table pin);
* ``POST /queue/golden-draft`` (Task 2.2 / D5): the draft comes back as an
  attachment with an opaque sha-16 filename and exact
  ``Cache-Control: private, no-store`` + ``Pragma: no-cache`` headers; the
  request performs ZERO persistence of any kind (spy stores, ``tempfile``,
  write-mode ``open``, and the logging tree all record nothing); no GET route
  can produce a draft; and undecided/expired/text-less sources are distinct
  409 states rather than a fabricated draft;
* the run-ALL pin stays GOLDEN-SUITE scoped: no golden-subset selector appears
  on the queue routes, and ``POST /run`` still 422s on subset keys through the
  same parent router.
"""
from __future__ import annotations

import copy
import json
import re
import sys
import tempfile
import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import agents.eval_queue as eq  # noqa: E402
import agents.eval_promote as ep  # noqa: E402
import agents.eval_queue_routes as eqr  # noqa: E402
import agents.eval_routes as eval_routes  # noqa: E402
import agents.eval_runs as er  # noqa: E402
from agents.job_store import JobStore  # noqa: E402
from agents.storage import feedback_id_for  # noqa: E402

QUEUE_URL = "/api/admin/evals/queue"
DECISION_URL = "/api/admin/evals/queue/decision"
DRAFT_URL = "/api/admin/evals/queue/golden-draft"

# Distinctive text sentinels: if one ever reaches the store (or a rejection
# response/log) the pin fails, whatever key it hides under.
Q_SENTINEL = "ZROUTEQUESTIONSENTINEL5511"
A_SENTINEL = "ZROUTEANSWERSENTINEL5511"
NOTE_SENTINEL = "ZSTEWARDNOTESENTINEL5511"


# ── harness ─────────────────────────────────────────────────────────────────

class _AdminInjectMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, is_admin: bool, email: str = ""):
        super().__init__(app)
        self._is_admin = is_admin
        self._email = email

    async def dispatch(self, request, call_next):
        request.state.is_admin = self._is_admin
        if self._email:
            request.state.user = SimpleNamespace(email=self._email)
        return await call_next(request)


def _build_client(is_admin: bool, email: str = "") -> TestClient:
    """Mount the PARENT router — the child inclusion is under test too."""
    app = FastAPI()
    app.include_router(eval_routes.router)
    app.add_middleware(_AdminInjectMiddleware, is_admin=is_admin, email=email)
    return TestClient(app)


class _SpyStore:
    """Spy persistence seam over a real in-memory ``JobStore``."""

    def __init__(self, kind: str):
        self._inner = JobStore(kind, table=None)
        self.writes: list = []
        self.deletes: list = []

    def get(self, key, default=None):
        return self._inner.get(key, default)

    def __getitem__(self, key):
        return self._inner[key]

    def __contains__(self, key):
        return key in self._inner

    def __setitem__(self, key, value):
        self.writes.append((key, copy.deepcopy(value)))
        self._inner[key] = value

    def __delitem__(self, key):
        self.deletes.append(key)
        del self._inner[key]

    def pop(self, key, *args):
        self.deletes.append(key)
        return self._inner.pop(key, *args)

    def reset_spy(self) -> None:
        self.writes = []
        self.deletes = []


class _FakeFeedback:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.writes: list = []

    def store(self, record):  # pragma: no cover - must never be called
        self.writes.append(copy.deepcopy(record))
        return True

    def get_by_time_range(self, start, end, sentiment=None):
        out = []
        for row in self.rows:
            stamp = str(row.get("timestamp") or "")
            if not (start <= stamp <= end):
                continue
            if sentiment and row.get("sentiment") != sentiment:
                continue
            out.append(copy.deepcopy(row))
        return out


def _feedback_row(*, session_id="sess-1", message_id="msg-1",
                  sentiment="negative", days_ago=1):
    from datetime import datetime, timedelta, timezone
    stamp = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    return {
        "feedback_id": feedback_id_for(session_id, message_id),
        "timestamp": stamp,
        "feedback_date": stamp[:10],
        "session_id": session_id,
        "message_id": message_id,
        "sentiment": sentiment,
        "user_message": f"why is {Q_SENTINEL} late?",
        "assistant_response": f"because {A_SENTINEL}",
        "cited_systems": ["erp"],
    }


@pytest.fixture()
def stores(monkeypatch):
    """Fresh spied stores for the queue module and the report store it reads."""
    spies = {"decisions": _SpyStore("eval-decision"),
             "reports": _SpyStore("eval-report"),
             "jobs": _SpyStore("eval-run")}
    feedback = _FakeFeedback()
    monkeypatch.setattr(eq, "_DECISIONS", spies["decisions"])
    monkeypatch.setattr(eq, "_FEEDBACK", feedback)
    monkeypatch.setattr(er, "_REPORTS", spies["reports"])
    monkeypatch.setattr(er, "_JOBS", spies["jobs"])
    spies["feedback"] = feedback
    return spies


@pytest.fixture()
def client():
    return _build_client(is_admin=True, email="steward@example.com")


def _all_writes(stores) -> list:
    out = []
    for name in ("decisions", "reports", "jobs"):
        out.extend(stores[name].writes)
    return out


def _leaf_keys(payload) -> set:
    keys: set = set()

    def _walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                keys.add(str(key))
                _walk(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                _walk(value)

    _walk(payload)
    return keys


def _ref() -> str:
    return eq.feedback_source_ref(feedback_id_for("sess-1", "msg-1"))


def _valid_body(**over) -> dict:
    body = {"source_ref": _ref(), "verdict": "incorrect"}
    body.update(over)
    return body


# ── router wiring + the route that must NOT exist ───────────────────────────

def test_queue_routes_are_included_in_the_one_evals_parent(client):
    """D9: ONE router — the child is included into the existing parent, so
    ``server.py`` keeps its single mount and auth boundary."""
    # Behavioural (route tables are only materialised after startup in newer
    # Starlette, so introspection is a false-negative risk): the parent serves
    # both child paths, and the source carries the single include.
    assert client.get(QUEUE_URL).status_code == 200
    assert client.post(DECISION_URL, json={}).status_code == 422
    source = (Path(eval_routes.__file__)).read_text(encoding="utf-8")
    assert "router.include_router(eval_queue_routes.router)" in source


def test_no_refresh_route_exists(client):
    """There is no refresh route (the queue is computed on read):
    nothing on the Evals surface may carry that path segment."""
    # No refresh route is SERVED, and the source declares none.
    for method in (client.get, client.post):
        assert method(f"{QUEUE_URL}/refresh").status_code == 404
    # ...and no route DECORATOR in the module declares one.
    source = Path(eqr.__file__).read_text(encoding="utf-8")
    decorators = [line for line in source.splitlines()
                  if line.lstrip().startswith("@router.")]
    assert decorators and not any("refresh" in line for line in decorators)


def test_verdict_enum_is_the_eval_queue_enum():
    """One home for the verdict enum — the body model must not drift."""
    field = eqr.DecisionRequest.model_fields["verdict"]
    assert set(field.annotation.__args__) == set(eq.VERDICTS)
    assert eq.VERDICTS == ("correct", "incorrect", "needs_context")


# ── 403 on every route ──────────────────────────────────────────────────────

def test_non_admin_get_queue_forbidden(stores):
    assert _build_client(is_admin=False).get(QUEUE_URL).status_code == 403


def test_non_admin_post_decision_forbidden_and_writes_nothing(stores):
    """A VALID body — so the 403 comes from the admin gate, not validation."""
    resp = _build_client(is_admin=False).post(DECISION_URL, json=_valid_body())
    assert resp.status_code == 403
    assert _all_writes(stores) == []


# ── GET /queue: computed on read ────────────────────────────────────────────

def test_get_queue_performs_zero_writes(stores, client):
    """Computed-on-read means no writer fires during a GET (pin 21)."""
    stores["feedback"].rows = [_feedback_row()]
    resp = client.get(QUEUE_URL)
    assert resp.status_code == 200
    assert resp.json()["counts"]["total"] == 1
    assert _all_writes(stores) == []
    assert stores["feedback"].writes == []
    for name in ("decisions", "reports", "jobs"):
        assert stores[name].deletes == []


def test_get_queue_joins_display_text_at_read_time(stores, client):
    stores["feedback"].rows = [_feedback_row()]
    item = client.get(QUEUE_URL).json()["items"][0]
    assert Q_SENTINEL in item["question"]
    assert A_SENTINEL in item["answer_excerpt"]
    assert item["source"] == eq.SOURCE_FEEDBACK
    assert item["decision"] is None and item["source_expired"] is False
    # The joined text is transient: nothing was persisted by the read.
    assert _all_writes(stores) == []


def test_prescore_is_triage_only_and_carries_no_verdict_or_score(stores, client):
    stores["feedback"].rows = [_feedback_row()]
    prescore = client.get(QUEUE_URL).json()["items"][0]["machine_prescore"]
    assert prescore["label"] == "triage-only"
    assert set(prescore) == {"label", "signals"}
    assert not ({"verdict", "score", "confidence", "rank", "severity"}
                & _leaf_keys(prescore))
    assert prescore["signals"] == sorted(prescore["signals"])


def test_get_queue_forwards_window_and_limit_to_the_module(stores, client,
                                                           monkeypatch):
    """Bounds have ONE home (``compute_queue`` clamps) — the route forwards."""
    seen = {}

    def fake_compute(window_days=None, limit=None):
        seen.update(window_days=window_days, limit=limit)
        return {"items": [], "window_days": window_days, "limit": limit,
                "counts": {}}

    monkeypatch.setattr(eq, "compute_queue", fake_compute)
    assert client.get(f"{QUEUE_URL}?window=3&limit=7").status_code == 200
    assert seen == {"window_days": 3, "limit": 7}
    assert client.get(QUEUE_URL).status_code == 200
    assert seen == {"window_days": eq.DEFAULT_WINDOW_DAYS,
                    "limit": eq.DEFAULT_LIMIT}


def test_expired_source_surfaces_its_state_not_an_error(stores, client):
    """A decided feedback row aged out under its TTL: the item comes back with
    the steward decision, no text, and ``source_expired`` — never a 500."""
    row, problem = eq.decide(_ref(), "incorrect", decided_by="steward-a")
    assert problem is None and row is not None
    stores["decisions"].reset_spy()

    resp = client.get(QUEUE_URL)
    assert resp.status_code == 200
    item = resp.json()["items"][0]
    assert item["source_expired"] is True
    assert item["question"] is None and item["answer_excerpt"] is None
    assert item["decision"]["verdict"] == "incorrect"
    assert resp.json()["counts"]["source_expired"] == 1
    assert _all_writes(stores) == []  # still a pure read


# ── POST /queue/decision: body contract ─────────────────────────────────────

@pytest.mark.parametrize("body", [
    {"unknown": "x"},
    {"count": 3},
    {"ids": ["q-1"]},
    {"question_ids": ["q-1"]},
    {"category": "cross-system"},
    {"variant": "arm-b"},
    {"verdict_override": "correct"},
], ids=["unknown", "count", "ids", "question_ids", "category", "variant",
        "verdict_override"])
def test_post_decision_rejects_unknown_body_keys(stores, client, body):
    """``extra="forbid"``: an unknown key is a 422 and nothing is written —
    which is also what keeps golden-subset selectors off this surface."""
    resp = client.post(DECISION_URL, json=_valid_body(**body))
    assert resp.status_code == 422
    assert _all_writes(stores) == []


@pytest.mark.parametrize("verdict", [
    "correct ", "CORRECT", "wrong", "rejected", "needs-context", "", None, 3,
], ids=["trailing-space", "uppercase", "wrong", "rejected", "hyphenated",
        "empty", "null", "int"])
def test_post_decision_rejects_verdicts_outside_the_enum(stores, client, verdict):
    resp = client.post(DECISION_URL,
                       json={"source_ref": _ref(), "verdict": verdict})
    assert resp.status_code == 422
    assert _all_writes(stores) == []


def test_post_decision_requires_source_ref_and_verdict(stores, client):
    assert client.post(DECISION_URL, json={}).status_code == 422
    assert client.post(DECISION_URL,
                       json={"source_ref": _ref()}).status_code == 422
    assert _all_writes(stores) == []


def test_post_decision_happy_path_records_a_minimized_row(stores, client):
    resp = client.post(DECISION_URL, json=_valid_body(
        taxonomy_tags=["wrong-number"], note="checked against the ERP row"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "recorded"
    decision = body["decision"]
    assert decision["verdict"] == "incorrect"
    assert decision["taxonomy_tags"] == ["wrong-number"]
    assert decision["ref_sha"] == eq.ref_sha(_ref())
    # Every persisted leaf is allowlisted, and the row is what the store got.
    assert _leaf_keys(decision) <= eq.PERSISTED_DECISION_KEYS
    written = dict(stores["decisions"].writes)[eq.decision_key(_ref())]
    assert _leaf_keys(written) <= eq.PERSISTED_DECISION_KEYS
    assert Q_SENTINEL not in json.dumps(written)


def test_decided_by_is_the_id_shaped_actor(stores, client):
    """A '+'-tagged admin email must not become a spurious 400: the actor is
    narrowed to the charset ``eval_queue`` accepts."""
    tagged = _build_client(is_admin=True, email="steward+evals@example.com")
    resp = tagged.post(DECISION_URL, json=_valid_body())
    assert resp.status_code == 200
    decided_by = resp.json()["decision"]["decided_by"]
    assert re.fullmatch(r"[A-Za-z0-9_.:@|-]{1,128}", decided_by)
    assert decided_by == "stewardevals@example.com"


def test_decided_by_falls_back_to_unknown_without_a_user(stores, client):
    resp = _build_client(is_admin=True).post(DECISION_URL, json=_valid_body())
    assert resp.status_code == 200
    assert resp.json()["decision"]["decided_by"] == "unknown"


def test_post_decision_rejects_unrecognized_source_ref(stores, client):
    resp = client.post(DECISION_URL,
                       json={"source_ref": "why is order 42 late?",
                             "verdict": "incorrect"})
    assert resp.status_code == 400
    assert resp.json()["detail"]["reason"] == "invalid_source_ref"
    assert _all_writes(stores) == []


def test_post_decision_rejects_invalid_tags(stores, client):
    resp = client.post(DECISION_URL,
                       json=_valid_body(taxonomy_tags=["Not A Tag"]))
    assert resp.status_code == 400
    assert resp.json()["detail"]["reason"] == "invalid_tags"
    assert _all_writes(stores) == []


# ── the note: untrusted text, never echoed ──────────────────────────────────

def test_secret_shaped_note_is_rejected_and_never_echoed(stores, client, caplog):
    """400-class rejection whose detail (and log) never carries the note."""
    caplog.set_level("DEBUG")
    note = f"password: AKIAIOSFODNN7EXAMPLE {NOTE_SENTINEL}"
    resp = client.post(DECISION_URL, json=_valid_body(note=note))
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["reason"] == "secret_like_content"
    assert NOTE_SENTINEL not in resp.text
    assert "AKIAIOSFODNN7EXAMPLE" not in resp.text
    assert NOTE_SENTINEL not in caplog.text
    assert _all_writes(stores) == []


def test_over_long_note_is_rejected_without_echo(stores, client):
    note = NOTE_SENTINEL + "x" * (eq.NOTE_MAX_CHARS + 10)
    resp = client.post(DECISION_URL, json=_valid_body(note=note))
    assert resp.status_code == 400
    assert resp.json()["detail"]["reason"] == "too_long"
    assert NOTE_SENTINEL not in resp.text
    assert _all_writes(stores) == []


def test_fence_delimiter_note_is_rejected_without_echo(stores, client):
    note = f"<<<BUSINESS_CONTEXT {NOTE_SENTINEL}"
    resp = client.post(DECISION_URL, json=_valid_body(note=note))
    assert resp.status_code == 400
    assert resp.json()["detail"]["reason"] == "fence_delimiter"
    assert NOTE_SENTINEL not in resp.text
    assert _all_writes(stores) == []


# ── run-ALL pin stays golden-suite scoped ───────────────────────────────────

def test_queue_get_exposes_only_window_and_limit(stores):
    """The queue's ``limit`` is a QUEUE bound, not a golden-suite selector: no
    subset-of-goldens parameter exists on either queue route."""
    names = set(inspect.signature(eqr.get_queue).parameters) - {"request"}
    assert names == {"window", "limit"}
    body_fields = set(eqr.DecisionRequest.model_fields)
    assert body_fields == {"source_ref", "verdict", "taxonomy_tags", "note"}
    assert not ({"count", "category", "ids", "question_ids", "variant",
                 "questions", "subset"} & (names | body_fields))


def test_golden_run_all_pin_survives_the_child_inclusion(client, monkeypatch):
    """The golden-suite run route still 422s on subset keys through the SAME
    parent router the queue child now lives in."""
    calls = []
    monkeypatch.setattr(er, "start_run",
                        lambda variant=None: calls.append(variant) or ("x", None))
    assert client.post("/api/admin/evals/run",
                       json={"count": 3}).status_code == 422
    assert calls == []
    monkeypatch.setattr(er, "start_run", lambda variant=None: ("rid-1", None))
    assert client.post("/api/admin/evals/run", json={}).status_code == 202


# ── POST /queue/golden-draft: transient download (Task 2.2 / ADR-0062 D5) ───

def _decide(stores, ref=None):
    """Record a steward decision the draft route can build on."""
    row, problem = eq.decide(ref or _ref(), "incorrect", decided_by="steward-a")
    assert problem is None and row is not None
    stores["decisions"].reset_spy()
    return row


class _WriteSpy:
    """Records every write-mode ``open`` and every ``tempfile`` entry point."""

    def __init__(self, monkeypatch):
        self.opens: list = []
        self.temps: list = []
        real_open = open

        def _open(file, mode="r", *args, **kwargs):
            if any(flag in str(mode) for flag in ("w", "a", "x", "+")):
                self.opens.append((str(file), str(mode)))
            return real_open(file, mode, *args, **kwargs)

        monkeypatch.setattr("builtins.open", _open)
        for name in ("NamedTemporaryFile", "TemporaryFile", "mkstemp",
                     "mkdtemp", "TemporaryDirectory"):
            monkeypatch.setattr(
                tempfile, name,
                lambda *a, _n=name, **k: self.temps.append(_n))


def test_golden_draft_route_is_post_only(stores, client):
    """POST, never GET: a cacheable draft URL is exactly what D5 forbids."""
    # POST is served (422 on an empty body proves the route matched), GET is
    # method-not-allowed -> no cacheable draft URL exists.
    assert client.post(DRAFT_URL, json={}).status_code == 422
    assert client.get(DRAFT_URL).status_code == 405


def test_non_admin_golden_draft_forbidden_and_writes_nothing(stores):
    resp = _build_client(is_admin=False).post(DRAFT_URL,
                                             json={"source_ref": _ref()})
    assert resp.status_code == 403
    assert _all_writes(stores) == []


def test_golden_draft_returns_the_pinned_no_store_attachment(stores, client):
    stores["feedback"].rows = [_feedback_row()]
    _decide(stores)
    resp = client.post(DRAFT_URL, json={"source_ref": _ref()})
    assert resp.status_code == 200
    sha = eq.ref_sha(_ref())
    assert resp.headers["cache-control"] == "private, no-store"
    assert resp.headers["pragma"] == "no-cache"
    assert resp.headers["content-disposition"] == (
        f'attachment; filename="golden-draft-{sha}.json"')
    assert resp.headers["content-type"].startswith("application/json")
    # The filename is OPAQUE — no question text, no session/message id.
    assert Q_SENTINEL not in resp.headers["content-disposition"]


def test_golden_draft_body_is_a_loadable_bundle_without_gates_or_identity(
        stores, client, tmp_path):
    stores["feedback"].rows = [_feedback_row()]
    _decide(stores)
    body = client.post(DRAFT_URL, json={"source_ref": _ref()}).json()
    assert set(body) == {"version", "questions"}
    draft = body["questions"][0]
    assert Q_SENTINEL in draft["text"]
    assert "answer_must_match" not in draft["expect"]
    assert draft["provenance"] == {"source": "promoted",
                                   "source_ref_sha": eq.ref_sha(_ref())}
    assert not ({"decided_by", "source_ref", "steward_note", "session_id",
                 "message_id", "verdict"} & _leaf_keys(draft))
    assert "steward-a" not in json.dumps(body)
    # Round-trips through the SAME loader the CLI golden lane uses.
    path = tmp_path / "external_golden.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    from agents import eval_core
    assert len(eval_core.load_golden_set(str(path))) == 1


def test_golden_draft_persists_nothing_anywhere(stores, client, monkeypatch,
                                                caplog):
    """Persistence seam (plan index row 1): during draft generation NOTHING is
    written — no store put/delete, no temp file, no write-mode ``open``, and no
    log record carrying draft content."""
    stores["feedback"].rows = [_feedback_row()]
    _decide(stores)
    spy = _WriteSpy(monkeypatch)
    caplog.set_level("DEBUG")
    resp = client.post(DRAFT_URL, json={"source_ref": _ref()})
    assert resp.status_code == 200
    assert _all_writes(stores) == []
    for name in ("decisions", "reports", "jobs"):
        assert stores[name].deletes == []
    assert stores["feedback"].writes == []
    assert spy.opens == [] and spy.temps == []
    assert Q_SENTINEL not in caplog.text
    assert A_SENTINEL not in caplog.text
    assert resp.text not in caplog.text


def test_golden_draft_is_deterministic_across_calls(stores, client):
    stores["feedback"].rows = [_feedback_row()]
    _decide(stores)
    first = client.post(DRAFT_URL, json={"source_ref": _ref()})
    second = client.post(DRAFT_URL, json={"source_ref": _ref()})
    assert first.json() == second.json()


def test_golden_draft_hints_are_non_gating_observed_counters(stores, client):
    stores["feedback"].rows = [_feedback_row()]
    _decide(stores)
    draft = client.post(DRAFT_URL,
                        json={"source_ref": _ref()}).json()["questions"][0]
    assert draft["hints"] == {"observed_systems": 1}  # one cited system
    assert set(draft["hints"]) <= ep.HINT_KEYS
    assert not (set(draft["hints"]) & set(draft["expect"]))


def test_undecided_source_is_a_distinct_409(stores, client):
    """Only DECIDED sources may be drafted — and the refusal is a state, not a
    500 and not a draft with a guessed verdict."""
    stores["feedback"].rows = [_feedback_row()]
    resp = client.post(DRAFT_URL, json={"source_ref": _ref()})
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "not_decided"
    assert _all_writes(stores) == []


def test_expired_source_is_a_distinct_409(stores, client):
    """Decided, but the feedback row aged out under its TTL: no fabricated
    question text ever reaches a draft."""
    _decide(stores)  # no feedback row in the window
    resp = client.post(DRAFT_URL, json={"source_ref": _ref()})
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "source_expired"
    assert _all_writes(stores) == []


def test_source_without_question_text_is_a_distinct_409(stores, client,
                                                        monkeypatch):
    """A golden-failure item carries no question text (the question already
    lives in a golden set) — promotion is refused, not invented."""
    ref = eq.golden_failure_source_ref("rid-1", "g-cross-plant")
    _decide(stores, ref)
    monkeypatch.setattr(eq, "compute_queue", lambda **kwargs: {"items": [{
        "source_ref": ref, "source": eq.SOURCE_GOLDEN_FAILURE,
        "answer_excerpt": "no batch data found", "source_expired": False}]})
    resp = client.post(DRAFT_URL, json={"source_ref": ref})
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "no_question_text"
    assert _all_writes(stores) == []


def test_golden_draft_rejects_an_unrecognized_source_ref(stores, client):
    resp = client.post(DRAFT_URL,
                       json={"source_ref": f"why is {Q_SENTINEL} late?"})
    assert resp.status_code == 400
    assert resp.json()["detail"]["reason"] == "invalid_source_ref"
    assert Q_SENTINEL not in resp.text
    assert _all_writes(stores) == []


@pytest.mark.parametrize("body", [
    {"verdict": "correct"}, {"answer_must_match": ["(?i)x"]},
    {"filename": "mine.json"}, {"count": 3}, {"question_ids": ["q-1"]},
], ids=["verdict", "answer_must_match", "filename", "count", "question_ids"])
def test_golden_draft_rejects_unknown_body_keys(stores, client, body):
    """``extra="forbid"``: a client cannot inject a gate, a filename or a
    golden-subset selector into the draft."""
    payload = {"source_ref": _ref()}
    payload.update(body)
    assert client.post(DRAFT_URL, json=payload).status_code == 422
    assert _all_writes(stores) == []


def test_golden_draft_body_fields_are_exactly_the_source_ref():
    assert set(eqr.DraftRequest.model_fields) == {"source_ref"}
