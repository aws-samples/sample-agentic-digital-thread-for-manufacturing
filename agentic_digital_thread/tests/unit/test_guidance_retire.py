# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Steward retire-a-note-by-digest (``agents/guidance_retire*.py``, ADR-0063 D7).

``GET /api/admin/guidance/{system_id}/digests`` — steward-gated, READ-ONLY:
per-note digest, scope, provenance source, an 80-char preview, created_at and
a delivered flag, plus the ``current_digest`` authority token and the
retirement usage read path (ADR-0052).

``POST /api/admin/guidance/{system_id}/retire`` — steward-gated; body
``{digest, expected_current_digest}`` (``extra="forbid"``). Delegates to the
EXISTING ``self_learning.prune_query_guidance_note`` saga (no second writer),
then proves an exact readback on BOTH the graph and the delivery store. 409 on
a stale ``expected_current_digest``, an ambiguous digest, a pending saga or a
readback mismatch. One bounded log line per call and one ledger bump per
success. Offline: the graph and the delivery store are in-memory fakes.
"""
from __future__ import annotations

import ast
import logging
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import agents.artifact_usage as artifact_usage
import agents.guidance_scope as guidance_scope
import agents.self_learning as learning
import agents.system_guidance as guidance
from agents import guidance_retire, guidance_retire_routes
from agents.guidance_acceptance import opaque_digest
from agents.guidance_scope import content_digest, delivery_document, parse_document
from unit.test_system_guidance import FakeBackend

_SYS = "sys-a"
_DIGESTS = f"/api/admin/guidance/{_SYS}/digests"
_RETIRE = f"/api/admin/guidance/{_SYS}/retire"
_KEEP = "keep this learned note"
_DROP = "drop this learned note " + "x" * 120
_CURRENT = f"Base. LEARNED: {_KEEP} LEARNED: {_DROP}"
_REAL_BUMP = artifact_usage.bump_artifact_usage
_REAL_READ = artifact_usage.read_artifact_usage


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
@pytest.fixture
def state(monkeypatch):
    """Strict in-memory graph + delivery store behind the REAL prune saga."""
    graph = {"text": _CURRENT, "events": []}

    def query(_cypher, params=None):
        graph["events"].append("graph_read")
        if (params or {}).get("sid") != _SYS:
            return []
        return [{"qg": graph["text"]}]

    def write(_cypher, params=None):
        params = params or {}
        graph["events"].append("graph_cas")
        if graph["text"] != params.get("expected"):
            return False
        graph["text"] = params["desired"]
        return True

    monkeypatch.setattr(learning, "_neptune_query", query)
    monkeypatch.setattr(learning, "_neptune_write", write)
    monkeypatch.setattr(learning, "get_accepted_for_system", lambda _sid: [])
    backend = FakeBackend()
    backend.save(_SYS, delivery_document(_CURRENT), 1)
    prior = guidance._store
    guidance.set_store(guidance.SystemGuidanceStore(backend=backend))
    bumps: list = []
    monkeypatch.setattr(artifact_usage, "bump_artifact_usage",
                        lambda refs, evidence="": bumps.append(
                            (set(refs), evidence)) or len(set(refs)))
    monkeypatch.setattr(artifact_usage, "read_artifact_usage",
                        lambda days=30: {})
    yield {"graph": graph, "backend": backend, "bumps": bumps}
    guidance.set_store(prior)


def _client(is_admin: "bool | None" = True) -> TestClient:
    """Bare app with the real router; ``is_admin=None`` stamps NO verdict."""
    app = FastAPI()
    if is_admin is not None:
        @app.middleware("http")
        async def _stamp(request, call_next):  # noqa: ANN001
            request.state.is_admin = is_admin
            return await call_next(request)
    app.include_router(guidance_retire_routes.router)
    return TestClient(app)


def _body(digest: str | None = None, current: str | None = None) -> dict:
    return {"digest": digest or content_digest(_DROP),
            "expected_current_digest": current or opaque_digest(_CURRENT)}


def _retire_logs(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records
            if r.getMessage().startswith("[GUIDANCE] retire ")]


# --------------------------------------------------------------------------- #
# READ path: GET digests
# --------------------------------------------------------------------------- #
def test_digests_lists_every_note_with_opaque_authority(state):
    resp = _client().get(_DIGESTS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["system_id"] == _SYS
    assert data["current_digest"] == opaque_digest(_CURRENT)
    assert data["count"] == 2
    assert [n["digest"] for n in data["notes"]] == [
        content_digest(_KEEP), content_digest(_DROP)]
    drop = data["notes"][1]
    assert drop["preview"] == _DROP[:80]
    assert len(drop["preview"]) == guidance_retire.PREVIEW_CHARS
    assert drop["scope"] == "legacy_unscoped"
    assert drop["delivered"] is True
    assert drop["source"] is None and drop["created_at"] is None
    assert data["retirements_30d"] == 0


def test_digests_joins_provenance_from_the_accepted_row(state, monkeypatch):
    monkeypatch.setattr(learning, "get_accepted_for_system", lambda _sid: [
        {"proposed_type": "guidance_note", "to_value": f"  {_KEEP} ",
         "source": "user", "accepted_at": "2026-09-01T00:00:00+00:00"},
        {"proposed_type": "value_mapping", "to_value": _DROP, "source": "agent"},
    ])
    notes = _client().get(_DIGESTS).json()["notes"]
    assert notes[0]["source"] == "user"
    assert notes[0]["created_at"] == "2026-09-01T00:00:00+00:00"
    assert notes[1]["source"] is None


def test_digests_flags_undelivered_and_unavailable_delivery(state, monkeypatch):
    state["backend"].store[_SYS] = delivery_document(f"LEARNED: {_KEEP}")
    notes = _client().get(_DIGESTS).json()["notes"]
    assert [n["delivered"] for n in notes] == [True, False]

    def _boom(_sid):
        raise guidance.GuidanceUnavailable("down")
    monkeypatch.setattr(guidance, "snapshot", _boom)
    notes = _client().get(_DIGESTS).json()["notes"]
    assert [n["delivered"] for n in notes] == [None, None]


def test_digests_reads_the_retirement_usage_ledger(state, monkeypatch):
    usage_id = guidance_retire.usage_artifact_id(_SYS)
    monkeypatch.setattr(artifact_usage, "read_artifact_usage", lambda days=30: {
        usage_id: {"artifact_id": usage_id, "kind": guidance_retire.USAGE_KIND,
                   "usage_30d": 3}})
    assert _client().get(_DIGESTS).json()["retirements_30d"] == 3


def test_digests_unknown_system_is_404(state):
    resp = _client().get("/api/admin/guidance/ghost/digests")
    assert resp.status_code == 404
    assert resp.json() == {"error": "system not found"}


def test_digests_get_writes_nothing(state, monkeypatch):
    def _no_write(*_a, **_k):
        raise AssertionError("GET must not write")
    for name in ("_neptune_write", "_neptune_write_loud",
                 "prune_query_guidance_note", "_guidance_graph_compare_and_set"):
        monkeypatch.setattr(learning, name, _no_write)
    for name in ("compare_and_set", "restore", "publish_exact", "write"):
        monkeypatch.setattr(guidance, name, _no_write)
    monkeypatch.setattr(artifact_usage, "bump_artifact_usage", _no_write)
    assert _client().get(_DIGESTS).status_code == 200


# --------------------------------------------------------------------------- #
# WRITE path: POST retire — refusals
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("digest", ["ABCDEF123456", "abc", "g" * 12, " " + "a" * 11])
def test_module_rejects_a_malformed_digest(state, digest):
    result = guidance_retire.retire_by_digest(_SYS, digest, opaque_digest(_CURRENT))
    assert result["http_status"] == 422
    assert result["error"] == "invalid_digest"
    assert "graph_cas" not in state["graph"]["events"]


@pytest.mark.parametrize("body", [
    {"digest": "ABCDEF123456", "expected_current_digest": opaque_digest("x")},
    {"digest": "a" * 12, "expected_current_digest": "sha256:short"},
    {"digest": "a" * 12},
    {"digest": "a" * 12, "expected_current_digest": opaque_digest("x"), "force": True},
    {"digest": 111122223333, "expected_current_digest": opaque_digest("x")},
], ids=["upper", "bad-current", "missing-current", "extra-key", "int-digest"])
def test_route_rejects_a_malformed_body_with_422(state, body):
    assert _client().post(_RETIRE, json=body).status_code == 422
    assert "graph_cas" not in state["graph"]["events"]


def test_unknown_system_is_404(state):
    resp = _client().post("/api/admin/guidance/ghost/retire", json=_body())
    assert resp.status_code == 404
    assert resp.json()["error"] == "system not found"


def test_stale_current_digest_is_409_and_never_prunes(state, monkeypatch):
    monkeypatch.setattr(learning, "prune_query_guidance_note",
                        lambda *_a: pytest.fail("stale must not prune"))
    resp = _client().post(_RETIRE, json=_body(current=opaque_digest("older")))
    assert resp.status_code == 409
    assert resp.json() == {"error": "stale_digest",
                           "current_digest": opaque_digest(_CURRENT)}


def test_unknown_digest_is_404(state, monkeypatch):
    monkeypatch.setattr(learning, "prune_query_guidance_note",
                        lambda *_a: pytest.fail("unknown must not prune"))
    resp = _client().post(_RETIRE, json=_body(digest="0" * 12))
    assert resp.status_code == 404
    assert resp.json() == {"error": "note not found"}


def test_ambiguous_digest_is_409(state, monkeypatch):
    monkeypatch.setattr(guidance_scope, "content_digest", lambda _t: "a" * 12)
    monkeypatch.setattr(learning, "prune_query_guidance_note",
                        lambda *_a: pytest.fail("ambiguous must not prune"))
    resp = _client().post(_RETIRE, json=_body(digest="a" * 12))
    assert resp.status_code == 409
    assert resp.json() == {"error": "ambiguous_digest"}


# --------------------------------------------------------------------------- #
# WRITE path: success through the REAL saga + exact readback
# --------------------------------------------------------------------------- #
def test_retire_removes_the_note_from_graph_and_delivery(state):
    resp = _client().post(_RETIRE, json=_body())
    assert resp.status_code == 200
    graph_text = state["graph"]["text"]
    assert resp.json() == {
        "status": "retired", "digest": content_digest(_DROP), "removed": 1,
        "remaining": 1, "current_digest": opaque_digest(graph_text),
        "readback": {"graph": "absent", "delivery": "absent"}}
    assert parse_document(graph_text)["legacy_unscoped"] == [_KEEP]
    assert state["backend"].store[_SYS] == delivery_document(graph_text)


def test_retire_delegates_the_exact_text_to_the_existing_saga(state, monkeypatch):
    calls = []
    real = learning.prune_query_guidance_note

    def _spy(system_id, note):
        calls.append((system_id, note))
        return real(system_id, note)
    monkeypatch.setattr(learning, "prune_query_guidance_note", _spy)
    assert _client().post(_RETIRE, json=_body()).status_code == 200
    assert calls == [(_SYS, _DROP)]


def test_readback_mismatch_is_409_without_retry(state, monkeypatch):
    calls = []
    monkeypatch.setattr(learning, "prune_query_guidance_note",
                        lambda sid, note: calls.append(note) or {
                            "status": "pruned", "removed": 1, "remaining": 1})
    resp = _client().post(_RETIRE, json=_body())
    assert resp.status_code == 409
    assert resp.json() == {
        "status": "readback_mismatch", "error": "readback_mismatch",
        "digest": content_digest(_DROP),
        "readback": {"graph": "present", "delivery": "present"}}
    assert calls == [_DROP]
    assert state["bumps"] == []


def test_readback_reports_unavailable_delivery(state, monkeypatch):
    real = learning.prune_query_guidance_note

    def _prune_then_break(system_id, note):
        result = real(system_id, note)
        monkeypatch.setattr(guidance, "snapshot", lambda _sid: (_ for _ in ()).throw(
            guidance.GuidanceUnavailable("down")))
        return result
    monkeypatch.setattr(learning, "prune_query_guidance_note", _prune_then_break)
    resp = _client().post(_RETIRE, json=_body())
    assert resp.status_code == 409
    assert resp.json()["readback"] == {"graph": "absent", "delivery": "unavailable"}


def test_pending_saga_passes_through_as_409_and_compensates(state):
    state["backend"].store[_SYS] = "drifted delivery"
    resp = _client().post(_RETIRE, json=_body())
    assert resp.status_code == 409
    body = resp.json()
    assert body["status"] == "pending"
    assert body["error"] == "guidance_update_failed"
    assert body["drift"]["delivery"] is True
    assert state["graph"]["text"] == _CURRENT
    assert state["bumps"] == []


def test_delivery_failure_mid_saga_compensates_the_graph(state, monkeypatch):
    def _fail(*_a, **_k):
        raise guidance.GuidanceUnavailable("write failed")
    monkeypatch.setattr(guidance, "restore", _fail)
    resp = _client().post(_RETIRE, json=_body())
    assert resp.status_code == 409
    assert resp.json()["status"] == "pending"
    assert state["graph"]["text"] == _CURRENT


def test_saga_note_vanished_race_is_404(state, monkeypatch):
    monkeypatch.setattr(learning, "prune_query_guidance_note",
                        lambda *_a: {"error": "note not found", "removed": 0})
    resp = _client().post(_RETIRE, json=_body())
    assert resp.status_code == 404
    assert resp.json() == {"error": "note not found", "removed": 0}


def test_saga_system_vanished_race_is_404(state, monkeypatch):
    monkeypatch.setattr(learning, "prune_query_guidance_note",
                        lambda sid, _n: {"error": f"System {sid} not found"})
    assert _client().post(_RETIRE, json=_body()).status_code == 404


@pytest.mark.parametrize("saga", [
    {"error": "delivery backend not found in region"},
    {"error": "system_id and note are required"},
    {"error": "note not found"},
], ids=["unrelated-not-found", "required", "no-removed-field"])
def test_saga_failure_maps_on_explicit_fields_not_substrings(state, monkeypatch, saga):
    """A 404 needs the saga's explicit not-found shape; an error text that
    merely CONTAINS "not found" is a 400."""
    monkeypatch.setattr(learning, "prune_query_guidance_note", lambda *_a: dict(saga))
    assert _client().post(_RETIRE, json=_body()).status_code == 400


def test_unreadable_delivery_store_logs_the_exception_type_only(state, monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="agents.guidance_retire")

    def _boom(_sid):
        raise guidance.GuidanceUnavailable("secret-token-123 down")
    monkeypatch.setattr(guidance, "snapshot", _boom)
    notes = _client().get(_DIGESTS).json()["notes"]
    assert [n["delivered"] for n in notes] == [None, None]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING
                and r.name == "agents.guidance_retire"]
    assert len(warnings) == 1
    assert "GuidanceUnavailable" in warnings[0].getMessage()
    assert "secret-token" not in warnings[0].getMessage()


@pytest.mark.parametrize("bad", ["bad%20id", "a..b", "a%2Fb"], ids=["space", "dotdot", "slash"])
def test_invalid_system_id_is_422_and_nothing_runs(state, bad):
    client = _client()
    for resp in (client.get(f"/api/admin/guidance/{bad}/digests"),
                 client.post(f"/api/admin/guidance/{bad}/retire", json=_body())):
        assert resp.status_code in (404, 422)
        if resp.status_code == 404:  # the router refused the encoded slash itself
            assert bad == "a%2Fb"
        else:
            assert resp.json() == {"error": "invalid_system_id"}
    assert state["graph"]["events"] == []


def test_sync_bodies_validate_the_system_id_directly():
    for fn in (guidance_retire_routes.guidance_digests,
               guidance_retire_routes.guidance_retire):
        resp = fn("a..b", *([] if fn is guidance_retire_routes.guidance_digests else
                            [guidance_retire_routes.RetireRequest(**_body())]))
        assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# Telemetry: one bounded line per call, one ledger bump per success
# --------------------------------------------------------------------------- #
def test_success_logs_once_and_bumps_the_ledger_once(state, caplog):
    caplog.set_level(logging.INFO, logger="agents.guidance_retire")
    _client().post(_RETIRE, json=_body())
    lines = _retire_logs(caplog)
    assert lines == [f"[GUIDANCE] retire system={_SYS} "
                     f"digest={content_digest(_DROP)} status=retired removed=1"]
    assert state["bumps"] == [(
        {(guidance_retire.USAGE_KIND, guidance_retire.usage_artifact_id(_SYS))},
        guidance_retire.USAGE_EVIDENCE)]
    assert all(_DROP[:20] not in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("body,status", [
    ({"current": opaque_digest("older")}, "stale_digest"),
    ({"digest": "0" * 12}, "note not found"),
])
def test_refusals_log_once_and_never_bump(state, caplog, body, status):
    caplog.set_level(logging.INFO, logger="agents.guidance_retire")
    _client().post(_RETIRE, json=_body(**body))
    lines = _retire_logs(caplog)
    assert len(lines) == 1 and f"status={status} removed=0" in lines[0]
    assert state["bumps"] == []


def test_log_line_is_bounded_for_hostile_inputs(state, caplog):
    caplog.set_level(logging.INFO, logger="agents.guidance_retire")
    guidance_retire.retire_by_digest("s\n" + "y" * 500, "not-a-digest\nx", "")
    (line,) = _retire_logs(caplog)
    assert "\n" not in line and "not-a-digest" not in line
    assert "digest=invalid" in line and len(line) < 200


class _LedgerTable:
    """Stateful in-memory proposals table for the REAL usage ledger: applies
    ``ADD #c :one`` per (mapping_id, discovered_at) and serves the
    ``begins_with`` scan the ledger read issues."""

    def __init__(self):
        self.items: dict[tuple[str, str], dict] = {}
        self.scans = 0

    def update_item(self, *, Key, ExpressionAttributeValues, **_kw):
        key = (Key["mapping_id"], Key["discovered_at"])
        item = self.items.setdefault(key, dict(Key, count=0))
        item["count"] += ExpressionAttributeValues[":one"]
        item["last_used_at"] = ExpressionAttributeValues[":now"]
        if ":evidence" in ExpressionAttributeValues:
            item["evidence"] = ExpressionAttributeValues[":evidence"]

    def scan(self, *, ExpressionAttributeValues, **_kw):
        self.scans += 1
        prefix = ExpressionAttributeValues[":prefix"]
        return {"Items": [dict(i) for (mid, _d), i in self.items.items()
                          if mid.startswith(prefix)]}


def test_retire_then_digests_counts_the_retirement_in_one_process(
        state, monkeypatch):
    """Walk step-12 regression: a GET before the retire primes the ledger's
    60 s read cache; the GET after a 200 retire must still report it."""
    # Swap the fixture's ledger stubs back to the REAL bump/read functions.
    monkeypatch.setattr(artifact_usage, "bump_artifact_usage", _REAL_BUMP)
    monkeypatch.setattr(artifact_usage, "read_artifact_usage", _REAL_READ)
    table = _LedgerTable()
    monkeypatch.setattr(learning, "_get_table", lambda: table)
    artifact_usage._reset_read_cache()
    try:
        client = _client()
        before = client.get(_DIGESTS).json()
        assert before["retirements_30d"] == 0 and table.scans == 1
        resp = client.post(_RETIRE, json=_body(current=before["current_digest"]))
        assert resp.status_code == 200 and resp.json()["status"] == "retired"
        after = client.get(_DIGESTS).json()
        assert after["retirements_30d"] == 1
        assert content_digest(_DROP) not in {n["digest"] for n in after["notes"]}
    finally:
        artifact_usage._reset_read_cache()


def test_ledger_failure_never_fails_the_retire(state, monkeypatch):
    monkeypatch.setattr(artifact_usage, "bump_artifact_usage",
                        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("x")))
    assert _client().post(_RETIRE, json=_body()).status_code == 200


# --------------------------------------------------------------------------- #
# Gate, offload shape, mounting
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("is_admin", [False, None], ids=["non-admin", "no-verdict"])
def test_non_steward_is_403_and_nothing_runs(state, is_admin):
    client = _client(is_admin=is_admin)
    assert client.get(_DIGESTS).status_code == 403
    assert client.post(_RETIRE, json=_body()).status_code == 403
    assert state["graph"]["events"] == []


def _async_handlers():
    tree = ast.parse(Path(guidance_retire_routes.__file__).read_text(encoding="utf-8"))
    return [n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)]


def test_every_async_handler_only_awaits_offload():
    handlers = _async_handlers()
    assert {h.name for h in handlers} == {
        "guidance_digests_route", "guidance_retire_route"}
    for handler in handlers:
        (stmt,) = handler.body[1:] if ast.get_docstring(handler) else handler.body
        assert isinstance(stmt, ast.Return) and isinstance(stmt.value, ast.Await)
        call = stmt.value.value
        assert isinstance(call, ast.Call) and call.func.id == "offload"


def test_server_app_mounts_both_routes():
    # Version-independent: FastAPI at the deployment lock keeps ``include_router``
    # LAZY, so a flat ``APIRoute`` scan of ``app.routes`` sees NO included route
    # (see test_workshop_story_routes._routing_matches). The schema answers for
    # both shapes.
    from agents.server import app
    paths = app.openapi()["paths"]
    assert set(paths["/api/admin/guidance/{system_id}/digests"]) == {"get"}
    assert set(paths["/api/admin/guidance/{system_id}/retire"]) == {"post"}
