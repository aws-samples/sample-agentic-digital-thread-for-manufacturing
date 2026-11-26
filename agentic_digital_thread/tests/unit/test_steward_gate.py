# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Steward gate (W2.3 / ADR-0050) + artifact-usage runtime hook — offline.

Server side: every proposal ACCEPTANCE/mutation route requires the
``request.state.is_admin`` verdict stamped by ``AuthMiddleware``. The gate
(``server._check_steward``) is wired as a ROUTE-REGISTRATION dependency
(``dependencies=[Depends(_check_steward)]`` on the gated ``@app.post``
decorators) and FAILS CLOSED: an explicit non-admin verdict ⇒ 403, and an
ABSENT ``is_admin`` attribute (no auth verdict at all) ⇒ 403 too. Because the
gate lives at registration, it is HTTP-layer only: bare HANDLER mounts in
offline unit harnesses carry no gate, and the scheduler's unattended sweeps
(which call ``self_learning`` directly, never through HTTP) stay ungated.
Proposing stays open: ``user-define`` and the GET list are NOT gated.
The harness below mounts the REAL ``server.app`` route objects (dependency
included) onto a bare app whose tiny middleware stamps ``is_admin`` the way
``AuthMiddleware`` does in production.

Runtime side: ``runtime_app._record_artifact_usage_hook`` is the offline-
testable sibling of ``_record_rule_usage_hook`` (same briefing gate, its own
swallow guard): it forwards the turn's tool results (plus any
typed evidence, the prefetched accepted rows, and the rule hook's
precomputed node ids — the turn is never re-scanned) to
``artifact_usage.extract_artifact_refs`` and bumps the day-bucketed ledger via
``bump_artifact_usage`` — zero behavior change when extraction returns empty,
and a raising ledger NEVER propagates. It is invoked from INSIDE
``_record_rule_usage_hook`` (the one post-run usage sequence — rule bump,
then artifact bump), so the ``_run_orchestrator`` finally block keeps a
single hook call.

Style mirrors ``test_usage_hook.py`` (extracted-hook invocation + source
tripwires) and ``test_query_guidance_routes.py`` (minimal FastAPI app mounting
the REAL handlers, driven via ``TestClient``). No AWS, no Neptune, no network.
"""
from __future__ import annotations

import importlib.util
import inspect
import sys
import types

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

# ``agents.server`` mounts ``agents.vocabulary_routes`` at import time (the
# W2.3 wiring). If the parallel-built router module is absent in this
# checkout, install a contract-shaped stub BEFORE the import so this file
# stays self-contained (the real module wins whenever it exists).
if importlib.util.find_spec("agents.vocabulary_routes") is None:  # pragma: no cover
    _stub = types.ModuleType("agents.vocabulary_routes")
    _stub.router = APIRouter(prefix="/api/admin/vocabulary")
    sys.modules["agents.vocabulary_routes"] = _stub

import agents.artifact_usage as artifact_usage
import agents.runtime_app as runtime_app
import agents.self_learning as self_learning
import agents.server as server


# --------------------------------------------------------------------------- #
# Harness: mount the REAL server.app route objects (steward dependency and
# all) on a bare app; a tiny middleware stamps request.state.is_admin the way
# AuthMiddleware does in production. is_admin=None mounts NO middleware, so
# the attribute is ABSENT — the fail-closed posture under test.
# --------------------------------------------------------------------------- #
from fastapi.routing import APIRoute

import agents.value_promotion as value_promotion

_HARNESS_PATHS = [
    ("/api/proposed-mappings", "GET"),
    ("/api/proposed-mappings/{mapping_id}/accept", "POST"),
    ("/api/proposed-mappings/{mapping_id}/reject", "POST"),
    ("/api/proposed-mappings/{mapping_id}/requeue", "POST"),
    ("/api/proposed-mappings/{mapping_id}/reopen", "POST"),
    ("/api/proposed-mappings/{mapping_id}/edit", "POST"),
    ("/api/proposed-mappings/accept-all", "POST"),
    ("/api/proposed-mappings/reset", "POST"),
    ("/api/proposed-mappings/clear-rejected", "POST"),
    ("/api/proposed-mappings/clear-all", "POST"),
    ("/api/proposed-mappings/reconcile-blocked", "POST"),
    ("/api/proposed-mappings/accept-high-confidence", "POST"),
    ("/api/proposed-mappings/adjudicate-batch", "POST"),
    ("/api/proposed-mappings/reject-degenerate", "POST"),
    ("/api/proposed-mappings/promote-values", "POST"),
    ("/api/proposed-mappings/user-define", "POST"),
    ("/api/query-guidance/{system_id}/prune", "POST"),
    ("/api/query-guidance/{system_id}/clear", "POST"),
]


def _real_route(path: str, method: str = "POST") -> APIRoute:
    for r in server.app.routes:
        if isinstance(r, APIRoute) and r.path == path and method in r.methods:
            return r
    raise AssertionError(f"route not registered on server.app: {method} {path}")


def _client(is_admin: "bool | None" = None) -> TestClient:
    app = FastAPI()
    if is_admin is not None:
        @app.middleware("http")
        async def _stamp(request, call_next):  # noqa: ANN001
            request.state.is_admin = is_admin
            return await call_next(request)
    for path, method in _HARNESS_PATHS:
        app.router.routes.append(_real_route(path, method))
    return TestClient(app)


# One row per GATED route: (url, json body, seam, 200-path return). Seams live
# in ``self_learning`` unless prefixed ``value_promotion.``; for
# reject-degenerate the 200-path monkeypatches get_pending_mappings with
# ``dict([]) == {}`` (empty iterable ⇒ nothing to reject ⇒ 200).
GATED_ROUTES = [
    ("/api/proposed-mappings/m1/accept", None,
     "accept_mapping", {"status": "accepted", "mapping_id": "m1"}),
    ("/api/proposed-mappings/m1/reject", None,
     "reject_mapping", {"status": "rejected", "mapping_id": "m1"}),
    ("/api/proposed-mappings/m1/requeue", None,
     "requeue_mapping", {"status": "requeued", "mapping_id": "m1",
                         "graph_artifact": "removed"}),
    ("/api/proposed-mappings/m1/reopen", None,
     "reopen_mapping", {"mapping_id": "m1", "status": "pending",
                        "review_reason": "re-opened by steward"}),
    ("/api/proposed-mappings/m1/edit", {"description": "x"},
     "edit_mapping", {"mapping_id": "m1", "proposed_type": "insight"}),
    ("/api/proposed-mappings/accept-all", None,
     "accept_all_pending", {"status": "ok", "accepted": 0}),
    ("/api/proposed-mappings/reset", None,
     "reset_all_accepted", {"status": "reset", "count": 0}),
    ("/api/proposed-mappings/clear-rejected", None,
     "clear_rejected", {"status": "cleared", "removed": 0}),
    ("/api/proposed-mappings/clear-all", None,
     "clear_all_learnings", {"status": "cleared", "removed": 0}),
    ("/api/proposed-mappings/reconcile-blocked", None,
     "reconcile_blocked_proposals", {"status": "ok", "purged": 0}),
    ("/api/proposed-mappings/accept-high-confidence", None,
     "sweep_auto_accept", {"accepted": 0, "failed": 0}),
    # ADR-0063 D4: the explicit-list batch runs the single-row reject path per
    # id, so its 200-path seam is reject_mapping — same seam, one hop away.
    ("/api/proposed-mappings/adjudicate-batch",
     {"action": "reject", "mapping_ids": ["m1"]},
     "reject_mapping", {"status": "rejected", "mapping_id": "m1"}),
    ("/api/proposed-mappings/reject-degenerate", None,
     "get_pending_mappings", []),
    ("/api/proposed-mappings/promote-values", None,
     "value_promotion.sweep_value_promotion",
     {"promoted": 0, "subsumed": 0, "skipped_below_threshold": 0}),
    ("/api/query-guidance/erp/prune", {"note": "drop me"},
     "prune_query_guidance_note", {"status": "pruned", "removed": 1, "remaining": 0}),
    ("/api/query-guidance/erp/clear", None,
     "clear_query_guidance", {"status": "cleared", "removed": 0}),
]
_IDS = [url.rsplit("/", 1)[-1] + ("-qg" if "query-guidance" in url else "")
        for url, *_ in GATED_ROUTES]


def _seam_target(seam: str):
    """Resolve a seam name to (module, attribute) — default ``self_learning``."""
    if seam.startswith("value_promotion."):
        return value_promotion, seam.split(".", 1)[1]
    return self_learning, seam


# --------------------------------------------------------------------------- #
# 403 for non-admin on EVERY gated route — and the write seam is NEVER hit.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("url,body,seam,ok", GATED_ROUTES, ids=_IDS)
def test_non_admin_is_403_and_never_reaches_the_lifecycle(monkeypatch, url, body, seam, ok):
    def _boom(*_a, **_k):
        raise AssertionError(f"{seam} must not run behind a failed steward gate")

    mod, attr = _seam_target(seam)
    monkeypatch.setattr(mod, attr, _boom)
    resp = _client(is_admin=False).post(url, json=body)
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Steward (admin) role required"


# --------------------------------------------------------------------------- #
# 200-path preserved for admin on EVERY gated route.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("url,body,seam,ok", GATED_ROUTES, ids=_IDS)
def test_admin_200_path_preserved(monkeypatch, url, body, seam, ok):
    mod, attr = _seam_target(seam)
    monkeypatch.setattr(mod, attr, lambda *_a, **_k: dict(ok))
    resp = _client(is_admin=True).post(url, json=body)
    assert resp.status_code == 200


@pytest.mark.parametrize("url,body,seam,ok", GATED_ROUTES, ids=_IDS)
def test_absent_is_admin_fails_closed(monkeypatch, url, body, seam, ok):
    # No middleware ⇒ request.state.is_admin is ABSENT ⇒ the gate DENIES
    # (fail closed). AuthMiddleware stamps the attribute on every request
    # that reaches server.app's routes, so a missing verdict can only mean
    # an unexpected path around the auth layer — deny, never allow.
    def _boom(*_a, **_k):
        raise AssertionError(f"{seam} must not run without an auth verdict")

    mod, attr = _seam_target(seam)
    monkeypatch.setattr(mod, attr, _boom)
    resp = _client(is_admin=None).post(url, json=body)
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Steward (admin) role required"


def test_bare_handler_mount_carries_no_gate(monkeypatch):
    # The gate is a ROUTE-REGISTRATION dependency, i.e. HTTP-layer only:
    # mounting the raw handler FUNCTION (the sibling harnesses' pattern, and
    # the moral equivalent of the scheduler's direct self_learning calls)
    # carries no gate. This pins the seam the unattended sweep relies on.
    monkeypatch.setattr(self_learning, "sweep_auto_accept",
                        lambda *_a, **_k: {"accepted": 0, "failed": 0})
    app = FastAPI()
    app.add_api_route("/api/proposed-mappings/accept-high-confidence",
                      server.accept_high_confidence_mappings, methods=["POST"])
    resp = TestClient(app).post("/api/proposed-mappings/accept-high-confidence")
    assert resp.status_code == 200


# --------------------------------------------------------------------------- #
# Proposing stays OPEN: user-define and the GET list carry no steward gate.
# --------------------------------------------------------------------------- #
def test_user_define_requires_no_admin():
    # Empty body reaches the handler's OWN presence validation (400 with the
    # missing-fields message) — NOT a 403 steward gate.
    resp = _client(is_admin=False).post("/api/proposed-mappings/user-define", json={})
    assert resp.status_code != 403
    assert resp.status_code == 400


def test_get_list_requires_no_admin(monkeypatch):
    monkeypatch.setattr(self_learning, "get_all_mappings",
                        lambda *_a, **_k: [], raising=False)
    resp = _client(is_admin=False).get("/api/proposed-mappings")
    assert resp.status_code == 200


def test_check_steward_is_the_single_shared_helper():
    # Every gated route registers the ONE helper as a route-level dependency
    # on the REAL server.app (no per-route forks, no in-handler duplicates),
    # and the open routes never reference it.
    gated = [(p, m) for p, m in _HARNESS_PATHS
             if (p, m) not in {("/api/proposed-mappings", "GET"),
                               ("/api/proposed-mappings/user-define", "POST")}]
    for path, method in gated:
        route = _real_route(path, method)
        deps = [d.dependency for d in route.dependencies]
        assert server._check_steward in deps, f"{method} {path} lost its gate"
        # getsource includes the decorator (where the Depends registration
        # lives); the handler BODY must not duplicate the gate call.
        assert "_check_steward(request)" not in inspect.getsource(route.endpoint), \
            f"{path}: gate must live at registration, not in the handler"
    for path, method in (("/api/proposed-mappings/user-define", "POST"),
                         ("/api/proposed-mappings", "GET")):
        route = _real_route(path, method)
        assert all(d.dependency is not server._check_steward
                   for d in route.dependencies), f"{method} {path} must stay open"
        assert "_check_steward" not in inspect.getsource(route.endpoint), path


# --------------------------------------------------------------------------- #
# Runtime artifact-usage hook (sibling of _record_rule_usage_hook).
# --------------------------------------------------------------------------- #
def test_artifact_hook_extracts_then_bumps_with_forwarded_args(monkeypatch):
    seen = {}

    def _extract(tool_results, evidence=None, *, accepted_rows=None,
                 precomputed_ids=None):
        seen["extract"] = (tool_results, evidence, accepted_rows, precomputed_ids)
        return {("value_mapping", "subj-a1b2c3d4e5f60718")}

    def _bump(refs, **_k):
        seen["bump"] = set(refs)
        return len(seen["bump"])

    monkeypatch.setattr(artifact_usage, "extract_artifact_refs", _extract)
    monkeypatch.setattr(artifact_usage, "bump_artifact_usage", _bump)
    rows = [{"neptune_node_id": "subj-a1b2c3d4e5f60718", "proposed_type": "value_mapping"}]
    evidence = object()
    bumped = runtime_app._record_artifact_usage_hook(
        {"t1": "joined via subj-a1b2c3d4e5f60718"}, "compare ERP and MES yields",
        evidence=evidence, accepted_rows=rows,
        precomputed_ids={"subj-a1b2c3d4e5f60718"})
    assert bumped == 1
    assert seen["extract"] == ({"t1": "joined via subj-a1b2c3d4e5f60718"},
                               evidence, rows, {"subj-a1b2c3d4e5f60718"})
    assert seen["bump"] == {("value_mapping", "subj-a1b2c3d4e5f60718")}


def test_artifact_hook_empty_extraction_is_a_no_op(monkeypatch):
    monkeypatch.setattr(artifact_usage, "extract_artifact_refs",
                        lambda *_a, **_k: set())

    def _boom(_refs):
        raise AssertionError("bump must not run when extraction is empty")

    monkeypatch.setattr(artifact_usage, "bump_artifact_usage", _boom)
    assert runtime_app._record_artifact_usage_hook({"t1": "plain"}, "q") == 0


def test_artifact_hook_skips_briefings(monkeypatch):
    def _boom(*_a, **_k):
        raise AssertionError("extraction must not run for briefings")

    monkeypatch.setattr(artifact_usage, "extract_artifact_refs", _boom)
    assert runtime_app._record_artifact_usage_hook(
        {"t1": "subj-a1b2c3d4e5f60718"},
        "Generate the morning operations briefing") == 0


def test_artifact_hook_swallows_a_raising_ledger(monkeypatch):
    monkeypatch.setattr(artifact_usage, "extract_artifact_refs",
                        lambda *_a, **_k: {("rule", "subj-a1b2c3d4e5f60718")})

    def _raise(_refs, **_k):
        raise RuntimeError("ledger down")

    monkeypatch.setattr(artifact_usage, "bump_artifact_usage", _raise)
    # Must swallow — a usage bump can never raise into the stream.
    assert runtime_app._record_artifact_usage_hook({"t1": "x"}, "q") == 0


def test_artifact_hook_swallows_a_raising_extractor(monkeypatch):
    def _raise(*_a, **_k):
        raise RuntimeError("extractor down")

    monkeypatch.setattr(artifact_usage, "extract_artifact_refs", _raise)
    assert runtime_app._record_artifact_usage_hook({"t1": "x"}, "q") == 0


def test_rule_hook_body_calls_artifact_sibling_swallow_guarded():
    # Source tripwire (mirrors test_usage_hook.py): the artifact bump lives
    # INSIDE _record_rule_usage_hook — inside its own try/except, forwarding
    # the prefetched accepted rows — so the _run_orchestrator finally block
    # keeps its single, HEAD-shaped hook call (which
    # test_response_integrity_cloud.py monkeypatches as one seam).
    src = inspect.getsource(runtime_app._record_rule_usage_hook)
    call = "_record_artifact_usage_hook(tool_results, prompt"
    assert call in src
    call_idx = src.index(call)
    assert "try:" in src[max(0, call_idx - 250):call_idx], \
        "artifact hook call must sit inside a try block"
    assert "except Exception" in src[call_idx:call_idx + 250], \
        "artifact hook call must be followed by a swallow guard"
    assert "accepted_rows=prefetched_rows" in src[call_idx:call_idx + 250], \
        "artifact hook must reuse the turn's prefetched rows"
    assert "precomputed_ids=ids" in src[call_idx:call_idx + 250], \
        "artifact hook must reuse the rule hook's computed ids (no re-scan)"
    # The orchestrator finally block calls ONLY the rule hook (the artifact
    # bump rides inside it — no second call site to drift).
    orch_src = inspect.getsource(runtime_app._run_orchestrator_base)
    assert "_record_rule_usage_hook(_tool_results, prompt" in orch_src
    assert "_record_artifact_usage_hook" not in orch_src


def test_rule_hook_forwards_results_ids_and_rows_to_artifact_sibling(monkeypatch):
    seen = {}

    def _artifact_spy(tool_results, prompt, *, evidence=None,
                      accepted_rows=None, precomputed_ids=None,
                      evidence_class="tool-text"):
        seen["args"] = (tool_results, prompt, evidence, accepted_rows,
                        precomputed_ids, evidence_class)
        return 7

    monkeypatch.setattr(runtime_app, "_record_artifact_usage_hook", _artifact_spy)
    monkeypatch.setattr(self_learning, "record_rule_usage",
                        lambda _ids, **_k: 2)
    rows = [{"neptune_node_id": "subj-a1b2c3d4e5f60718",
             "proposed_type": "value_mapping"}]
    bumped = runtime_app._record_rule_usage_hook(
        {"t1": "joined via subj-a1b2c3d4e5f60718"}, "compare ERP and MES yields",
        precomputed_ids={"subj-a1b2c3d4e5f60718"}, prefetched_rows=rows)
    # Return value is the RULE count — the artifact count never alters it.
    assert bumped == 2
    assert seen["args"] == ({"t1": "joined via subj-a1b2c3d4e5f60718"},
                            "compare ERP and MES yields", None, rows,
                            {"subj-a1b2c3d4e5f60718"}, "tool-text")


def test_rule_hook_runs_artifact_sibling_even_with_no_rule_ids(monkeypatch):
    called = {}
    monkeypatch.setattr(runtime_app, "_record_artifact_usage_hook",
                        lambda *a, **k: called.setdefault("yes", True) or 1)

    def _boom(*_a, **_k):
        raise AssertionError("record_rule_usage must not run with empty ids")

    monkeypatch.setattr(self_learning, "record_rule_usage", _boom)
    assert runtime_app._record_rule_usage_hook(
        {"t1": "plain"}, "q", precomputed_ids=set()) == 0
    assert called.get("yes") is True


def test_rule_hook_swallows_a_raising_artifact_sibling(monkeypatch):
    def _raise(*_a, **_k):
        raise RuntimeError("artifact ledger down")

    monkeypatch.setattr(runtime_app, "_record_artifact_usage_hook", _raise)
    monkeypatch.setattr(self_learning, "record_rule_usage",
                        lambda _ids, **_k: 3)
    # The rule count survives an artifact-side failure.
    assert runtime_app._record_rule_usage_hook(
        {"t1": "subj-a1b2c3d4e5f60718"}, "q",
        precomputed_ids={"subj-a1b2c3d4e5f60718"}) == 3


def test_rule_hook_briefing_gate_covers_the_artifact_path(monkeypatch):
    def _boom(*_a, **_k):
        raise AssertionError("artifact hook must not run for briefings")

    monkeypatch.setattr(runtime_app, "_record_artifact_usage_hook", _boom)
    assert runtime_app._record_rule_usage_hook(
        {"t1": "subj-a1b2c3d4e5f60718"},
        "Generate the morning operations briefing") == 0
