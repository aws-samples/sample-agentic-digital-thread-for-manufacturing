# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Exception text never reaches a response, an SSE frame or a polled job record.

Each scenario makes one route family's backend raise an exception whose text
carries an ARN, a hostname and a Cypher fragment, then reads what the caller
would see. That text must not contain any of the three; it must carry a fixed
category message and a short reference; and the server log must carry the
detail under the same reference (digits masked, so the ARN's account id is
not in the log either).

Offline: every AWS, graph, HTTP and database seam is stubbed.
"""
from __future__ import annotations

import asyncio
import json
import logging
import queue
import re
from types import SimpleNamespace

import pytest

_ARN = "arn:aws:sts::111122223333:assumed-role/adt-task/session"
_HOST = "graph-db.internal.example.com"
_CYPHER = "MATCH (n:System) WHERE n.secret = $s RETURN n"
_LEAK = f"User: {_ARN} is not authorized at {_HOST} while running {_CYPHER}"
_MARKERS = (_ARN, "111122223333", _HOST, _CYPHER, "MATCH (n:System)")

#: The fixed public texts (agents.public_errors.PUBLIC_ERROR_MESSAGES); a
#: response must START with one of them.
_PUBLIC_PREFIXES = (
    "Authentication failed.",
    "The system could not be reached.",
    "The request timed out.",
    "The requested resource was not found.",
    "The system returned a response that could not be read.",
    "An upstream service returned an error.",
    "The knowledge graph query failed.",
    "The request failed on the server.",
)
_REF_RE = re.compile(r"Reference: ([0-9a-f]{12})\b")


class _Leaky(RuntimeError):
    """An arbitrary backend failure carrying sensitive text."""


def _boom(*_a, **_k):
    raise _Leaky(_LEAK)


async def _aboom(*_a, **_k):
    raise _Leaky(_LEAK)


def _request(is_admin=True, body=None):
    async def _json():
        return body or {}
    return SimpleNamespace(state=SimpleNamespace(is_admin=is_admin,
                                                 user=SimpleNamespace(email="a@example.com")),
                           json=_json)


def _assert_public(payload, error_text, caplog):
    """No marker anywhere in ``payload``; ``error_text`` is a public message
    with a reference; the log line for that reference carries the detail."""
    dumped = json.dumps(payload, default=str)
    for marker in _MARKERS:
        assert marker not in dumped, f"{marker!r} leaked: {dumped[:400]}"
    assert isinstance(error_text, str) and error_text, payload
    assert any(p in error_text for p in _PUBLIC_PREFIXES), error_text
    match = _REF_RE.search(error_text)
    assert match, f"no reference in {error_text!r}"
    ref = match.group(1)
    lines = [r.getMessage() for r in caplog.records if f"ref={ref}" in r.getMessage()]
    assert lines, f"no log line for ref={ref}"
    line = lines[0]
    assert _HOST in line and "MATCH (n:System)" in line, line
    assert "111122223333" not in line, "digits must be masked in the log head"


# ── graph view routes (server.py) ────────────────────────────────────────────

def _graph_view(mp):
    import agents.server as server
    mp.setattr(server, "_graph_view_fetch_nodes", _boom)
    out = server.neptune_full_graph()
    return out, out["error"]


def _graph_neighbors(mp):
    import agents.server as server
    mp.setattr(server, "neptune_query", _boom)
    out = server.neptune_node_neighbors("n-1")
    return out, out["error"]


def _kg_analyze(mp):
    import agents.db as db
    import agents.server as server
    mp.setattr(db, "bedrock_call", _boom)
    out = server.kg_analyze_node({"node_type": "System", "node_name": "erp"})
    return out, out["error"]


def _graph_repair(mp):
    import agents.graph_hygiene as gh
    import agents.server as server
    mp.setattr(gh, "run_value_mapping_hygiene", _boom)
    out = server._repair_graph_sync({"dry_run": True})
    return out, out["error"]


def _graph_backfill(mp):
    import agents.catalog as cat
    import agents.server as server
    mp.setattr(cat, "core_concept_merges", _boom)
    out = server._backfill_concepts_sync()
    return out, out["error"]


def _proposed_mappings(mp):
    import agents.self_learning as sl
    import agents.server as server
    mp.setattr(sl, "get_all_mappings", _boom)
    out = server.get_proposed_mappings()
    return out, out["error"]


# ── learning store mutations (self_learning.py) ──────────────────────────────

def _clear_rejected(mp):
    import agents.self_learning as sl
    mp.setattr(sl, "_get_table", _boom)
    out = sl.clear_rejected()
    return out, out["error"]


def _metric_correction(mp):
    import agents.metric_resolver as mr
    mp.setattr(mr, "record_user_correction", _boom)
    body = mr.CorrectMappingRequest(system_id="erp", concept="order_qty",
                                    to_field="orders.qty", from_field="", confidence=1.0)
    out = asyncio.run(mr.correct_mapping_route(body, _steward_request()))
    return out, out["error"]


# ── admin settings and observability ─────────────────────────────────────────

def _restore_runtime_auth(mp):
    import agents.admin_routes as ar
    mp.setenv("M2M_DISCOVERY_URL", "https://auth.example.com/.well-known")
    mp.setenv("M2M_CLIENT_ID", "client")
    mp.setattr("boto3.client", lambda *a, **k: object())
    mp.setattr(ar, "_repair_system_runtimes", _boom)
    out = ar.restore_runtime_auth(_request())
    return out, out["error"]


def _feedback_exploration(mp):
    import agents.admin_routes as ar
    import agents.feedback_exploration_settings as fxs
    mp.setattr(fxs, "set_enabled", _boom)
    mp.setattr(fxs, "env_kill_active", lambda: False)
    out = asyncio.run(ar.put_feedback_exploration(
        _request(body={"feedback_exploration_enabled": True})))
    return out, out["message"]


def _trace_sessions(mp):
    import agents.agents_trace_routes as tr
    mp.setenv("AGENTS_OBSERVABILITY_DRILLDOWN", "true")
    mp.setattr(tr, "_boto_session", lambda: SimpleNamespace(client=lambda *_: object()))
    mp.setattr(tr, "span_log_groups", lambda *_clients: ["aws/spans"])

    def _raise(*_a, **_k):
        raise tr.InsightsError(f"Logs Insights poll failed for /aws/x: {_LEAK}")
    mp.setattr(tr, "run_insights", _raise)
    out = tr.list_trace_sessions(_request())
    return out, out["error"]


def _notifications(mp):
    import agents.notification_routes as nr
    for _kind, fn_name in nr._SOURCES:
        mp.setattr(nr, fn_name, lambda: [])
    mp.setattr(nr, nr._SOURCES[0][1], _boom)
    out = nr.get_notifications()
    errors = [i["error"] for i in out["items"] if "error" in i]
    return out, errors[0] if errors else ""


# ── graph snapshot jobs (polled) ─────────────────────────────────────────────

def _snapshot_import_job(mp):
    import agents.graph_snapshot_routes as gsr
    mp.setattr(gsr.graph_snapshot, "import_snapshot", _boom)
    asyncio.run(gsr._run_import_job({"snapshot": {}}))
    record = gsr._IMPORT_JOBS[gsr._IMPORT_JOB_KEY]
    return record, record["error"]


def _snapshot_reconcile_error(mp):
    import agents.graph_snapshot_routes as gsr
    mp.setattr(gsr.graph_snapshot, "import_snapshot", lambda _p: {"nodes_created": 1})
    mp.setattr(gsr, "_reconcile_report", _boom)
    asyncio.run(gsr._run_import_job({"snapshot": {}}))
    record = gsr._IMPORT_JOBS[gsr._IMPORT_JOB_KEY]
    return record, record["result"]["reconcile_error"]


# ── onboarding jobs, routes and the onboarding chat SSE ──────────────────────

def _reset_job(mp):
    import agents.onboard as ob
    mp.setattr(ob, "_run_reset_steps", _aboom)
    asyncio.run(ob._run_reset_job("leaky-sys"))
    record = ob._RESET_JOBS["leaky-sys"]
    return record, record["error"]


def _sync_gateway(mp):
    import agents.onboard as ob
    import agents.runtime_provision_helpers as rph
    mp.setenv("AGENTCORE_GATEWAY_ID", "gw-1")
    mp.setattr("boto3.client", lambda *a, **k: object())
    mp.setattr(rph, "list_all_gateway_targets", _boom)
    out = ob.onboard_sync_gateway("erp")
    return out, out["error"]


def _credentials(mp):
    """The secure credential form's store of a REST API system the deployment
    registered: the Secrets Manager write fails."""
    import agents.connection_resolver as cr
    import agents.onboard as ob
    import agents.onboard_agent as oa
    from agents.catalog import SystemCatalog
    cat = SystemCatalog([{"id": "erp-api", "name": "ERP API", "plant": "", "schema": "",
                          "system_type": "ERP", "connection_type": "REST API",
                          "status": "onboarded"}])
    mp.setattr(ob, "catalog", cat)
    mp.setattr(oa, "catalog", cat)
    node = {"status": "onboarded", "connection_type": "REST API", "secret_arn": "",
            "connection_config": json.dumps({"base_url": "https://erp-api.example.com"})}
    mp.setattr(cr, "_fetch_node_config", lambda sid: dict(node) if sid == "erp-api" else None)
    mp.setattr(oa, "_store_secret", _boom)
    out = ob.onboard_credentials({"system_id": "erp-api", "credentials": {"password": "x"}})
    return out, out["error"]


def _verify_pg(mp):
    import agents.onboard as ob
    mp.setattr(ob.pgdriver, "connect", _boom)
    tables, total, err = ob._verify_pg_reachability(
        "postgresql://u:p@db.example.com/x", "erp")
    return {"tables": tables, "total": total, "error": err}, err


def _onboard_chat_sse(mp):
    import agents.onboard as ob
    mp.setattr(ob, "get_onboard_agent", _boom)
    q = queue.Queue()
    ob._run_onboard_agent(q, None, "hello", [], "")
    frame = q.get_nowait()
    assert frame["event"] == "error"
    return frame, frame["data"]["text"]


# ── connector discovery (onboard_agent.py) ───────────────────────────────────

def _discover_openapi(mp):
    import agents.onboard_agent as oa
    import agents.pinned_http as pinned_http
    # OpenAPI discovery fetches the spec through the pinned connector path.
    mp.setattr(pinned_http, "get", _boom)
    out = oa._discover_openapi({"connection_string": "https://api.example.com/openapi.json",
                                "connection_config": {}})
    return out, out["error"]


def _discover_postgres(mp):
    from agents import pgdriver
    import agents.onboard_agent as oa
    mp.setattr(pgdriver, "connect", _boom)
    mp.setattr(oa, "_discover_postgres_dsn",
               lambda _e, _r: ("postgresql://u:p@db.example.com/x", "erp"))
    out = oa._discover_postgres({"id": "erp"}, {})
    return out, out["error"]


def _discover_athena(mp):
    import agents.athena_tools as at
    import agents.onboard_agent as oa
    mp.setattr(oa, "_athena_discover_cfg", lambda *_a: {})
    mp.setattr(at, "_resolve_config", lambda _c: {"database": "lake", "workgroup": "wg"})
    mp.setattr(at, "run_athena_query", _boom)
    out = oa._discover_athena_schema("lake", {"id": "lake"}, {}, {})
    return out, out["error"]


# ── other surfaces ───────────────────────────────────────────────────────────

def _entity_rescan(mp):
    import agents.entity_rescan as er
    mp.setattr(er, "_load_schema_rows",
               lambda _q: [{"system_id": "erp", "table": "orders", "has_entity": False}])
    mp.setattr(er, "_load_table_context", _boom)
    out = er.rescan_entities(None, query=lambda *_: [], write=lambda *_: True,
                             classify=lambda *a, **k: {}, catalog_get=lambda _s: {})
    errors = out["systems"]["erp"]["errors"]
    return out, errors[0] if errors else ""


def _query_bridge(mp):
    import httpx
    import agents.query_bridge_server as qb
    mp.setenv("ADT_APP_URL", "https://app.example.com")
    mp.setattr(qb, "_gateway_headers", lambda: {})
    mp.setattr(httpx, "Client", _boom)
    out = json.loads(qb.submit_query("how many orders?"))
    return out, out["error"]


def _mqtt_last_error(mp):
    import agents.mqtt_subscriber as mq
    text = mq._sanitize_error(_Leaky(_LEAK))
    return {"message": text}, text


_SCENARIOS = {
    "graph_view": _graph_view,
    "graph_neighbors": _graph_neighbors,
    "kg_analyze": _kg_analyze,
    "graph_repair": _graph_repair,
    "graph_backfill": _graph_backfill,
    "proposed_mappings": _proposed_mappings,
    "learning_clear_rejected": _clear_rejected,
    "metric_correction": _metric_correction,
    "admin_restore_runtime_auth": _restore_runtime_auth,
    "admin_feedback_exploration": _feedback_exploration,
    "admin_trace_sessions": _trace_sessions,
    "notifications": _notifications,
    "snapshot_import_job": _snapshot_import_job,
    "snapshot_reconcile_error": _snapshot_reconcile_error,
    "onboard_reset_job": _reset_job,
    "onboard_sync_gateway": _sync_gateway,
    "onboard_credentials": _credentials,
    "onboard_verify_pg": _verify_pg,
    "onboard_chat_sse": _onboard_chat_sse,
    "discover_openapi": _discover_openapi,
    "discover_postgres": _discover_postgres,
    "discover_athena": _discover_athena,
    "entity_rescan": _entity_rescan,
    "query_bridge": _query_bridge,
    "mqtt_last_error": _mqtt_last_error,
}


@pytest.mark.parametrize("name", sorted(_SCENARIOS))
def test_exception_text_stays_out_of_the_public_surface(name, monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    payload, error_text = _SCENARIOS[name](monkeypatch)
    _assert_public(payload, error_text, caplog)


def test_a_typed_safe_reset_message_still_reaches_the_reset_job(monkeypatch):
    """ResetIncompleteError carries fixed retry guidance the UI shows as is."""
    import agents.onboard as ob

    async def _incomplete(*_a, **_k):
        raise ob.ResetIncompleteError(
            "removal of 'erp' is incomplete: retry the removal")
    monkeypatch.setattr(ob, "_run_reset_steps", _incomplete)
    asyncio.run(ob._run_reset_job("erp-safe"))
    assert "retry the removal" in ob._RESET_JOBS["erp-safe"]["error"]


def test_retire_failure_does_not_carry_the_store_error_into_the_reset_message(monkeypatch):
    """The reset-incomplete message names the failure class, never its text."""
    import agents.offboard_retirement as ret
    import agents.onboard as ob
    monkeypatch.setattr(ret, "retire_pending_for_offboarded_systems", _boom)
    with pytest.raises(ob.ResetIncompleteError) as info:
        ob._retire_or_incomplete("erp")
    message = str(info.value)
    for marker in _MARKERS:
        assert marker not in message
    assert "retry the removal" in message


def _steward_request():
    """The steward verdict ``AuthMiddleware`` stamps: the correct-mapping route
    accepts on entry only for a steward."""
    from types import SimpleNamespace
    return SimpleNamespace(state=SimpleNamespace(is_admin=True))
