# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Current-replica control-gate diagnostics are read-only annotations."""
import asyncio
from concurrent.futures import ThreadPoolExecutor

import agents.agents_routes as agents_routes
from agents.query_admission import get_admission


def setup_function():
    get_admission().reset()


def teardown_function():
    get_admission().reset()


def _read_route():
    return asyncio.run(agents_routes.gate_health())


def test_gate_health_route_is_under_existing_admin_agents_surface():
    route_paths = {route.path for route in agents_routes.router.routes}
    assert "/api/admin/agents/gate-health" in route_paths


def test_two_concurrent_route_reads_do_not_mutate_or_consume_admission(monkeypatch):
    admission = get_admission()
    token = admission.acquire("query")
    monkeypatch.setenv("CARD_LIVE", "1")
    monkeypatch.setenv("CARD_LIVE_MQTT_PUSH", "false")
    monkeypatch.setenv("ASK_STREAM_QUEUE_MAX", "37")

    with ThreadPoolExecutor(max_workers=2) as pool:
        payloads = list(pool.map(lambda _: _read_route(), range(2)))

    stable_snapshots = [
        {key: value for key, value in payload["admission"].items()
         if key != "oldest_token_age_seconds"}
        for payload in payloads
    ]
    assert stable_snapshots[0] == stable_snapshots[1]
    assert all(payload["admission"]["oldest_token_age_seconds"] >= 0
               for payload in payloads)
    assert payloads[0]["admission"]["state"] == "busy"
    assert payloads[0]["admission"]["tokens_outstanding"] == 1
    assert payloads[0]["admission"]["current_owner_kind"] == "query"
    assert payloads[0]["card_live_mode"] == "poll-only"
    assert payloads[0]["ask_stream_queue_bound"] == 37
    assert admission.owners() == ("query",)
    admission.release(token)


def test_route_omits_absent_measurements_instead_of_zero_filling(monkeypatch):
    monkeypatch.delenv("CARD_LIVE_MQTT_PUSH", raising=False)
    payload = _read_route()
    admission = payload["admission"]
    assert admission["tokens_outstanding"] == 0
    assert "briefings_admitted" not in admission
    assert "briefings_skipped_busy" not in admission
    assert "last_release_at" not in admission
    assert "verdict" not in payload
    assert payload["scope"] == "current-replica"


def test_gate_health_route_is_admin_scoped_by_existing_middleware():
    from agents.auth.middleware import AuthMiddleware
    assert any("/api/admin/agents/gate-health".startswith(prefix)
               for prefix in AuthMiddleware.ADMIN_PREFIXES)
