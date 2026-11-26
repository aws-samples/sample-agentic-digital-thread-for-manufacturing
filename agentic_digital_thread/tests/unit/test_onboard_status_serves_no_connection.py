# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``GET /api/onboard/status`` serves no connection details.

Walk finding 2026-10-19: the route returned every onboarded catalog entry as
it is stored, so each seeded system's ``connection_string`` (with the database
admin password inline) reached the browser, while ``GET /api/onboard/catalog``
masks the same field. The status route's readers (the prompt library's system
dimming, the boot landing, the suggestion chips) read only ``id`` and
``system_type`` of a system, so the route serves an allowlisted projection
(id, name, plant, system_type, connection_type, status) and nothing else.

Fixture identifiers are synthetic and differ from any demo dataset, so the
test pins the rule, not one seed. Offline: no sockets, no AWS.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))
os.environ.setdefault("GRAPH_IDENTIFIER", "g-unittest")

import agents.onboard as onboard  # noqa: E402
from agents.catalog import SystemCatalog  # noqa: E402

# Values that must never appear anywhere in the response. nosec B105: offline
# test fixtures, not credentials.
_SECRETS = (
    "Qv7-ledger-pw",            # inline password of a PostgreSQL connection string
    "ledger_admin",             # its user name
    "cfg-pw-123",               # a password inside connection_config
    "mcp-tok-456",              # a token in an MCP URL's query string
    "cfg-tok-789",              # a token inside connection_config
    "hdr-bearer-000",           # a bearer header inside connection_config
    "top-api-key-111",          # a stray credential field on the entry itself
    "AbCdEf",                   # the tail of a secret pointer
)

_ALLOWED = {"id", "name", "plant", "system_type", "connection_type", "status"}

#: Any key that names a connection or a credential.
_CREDENTIAL_KEY = re.compile(
    r"connection_string|connection_config|secret|password|passwd|token|api_?key|"
    r"credential|auth|bearer|dsn|url", re.IGNORECASE)


def _entries() -> list[dict]:
    return [
        {"id": "ledger-north", "name": "Ledger North", "plant": "Site North",
         "system_type": "Finance", "connection_type": "PostgreSQL",
         "connection_string": ("postgresql://ledger_admin:Qv7-ledger-pw"  # nosec B105
                               "@ledger-db.example.internal:5432/ledger"),
         "schema": "ledger", "status": "onboarded",
         "connection_config": {"host": "ledger-db.example.internal", "port": "5432",
                               "password": "cfg-pw-123"},  # nosec B105
         "secret_arn": ("arn:aws:secretsmanager:us-west-2:111122223333:"
                        "secret:adt/conn/ledger-north-AbCdEf")},
        {"id": "hub-south", "name": "Hub South", "plant": "Site South",
         "system_type": "Gateway", "connection_type": "MCP Server",
         "connection_string": "https://hub.example.internal/mcp?token=mcp-tok-456",
         "schema": "", "status": "onboarded",
         "connection_config": {"url": "https://hub.example.internal/mcp",
                               "token": "cfg-tok-789",  # nosec B105
                               "headers": {"Authorization": "Bearer hdr-bearer-000"}},
         "api_key": "top-api-key-111",  # nosec B105
         "query_guidance": "free text"},
        {"id": "queue-east", "name": "Queue East", "plant": "Site East",
         "system_type": "Planning", "connection_type": "PostgreSQL",
         "connection_string": "postgresql://ledger_admin:Qv7-ledger-pw@q.example.internal/q",
         "schema": "queue", "status": "registered"},
    ]


@pytest.fixture
def status_seams(monkeypatch):
    monkeypatch.setattr(onboard, "catalog", SystemCatalog(_entries()))
    monkeypatch.setattr(onboard, "neptune_query", lambda *a, **k: [])
    monkeypatch.setattr(onboard, "get_tool_count", lambda: 2)


def _walk_keys(value, path=""):
    if isinstance(value, dict):
        for k, v in value.items():
            yield f"{path}.{k}", k
            yield from _walk_keys(v, f"{path}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _walk_keys(v, f"{path}[{i}]")


def _assert_no_connection(body: dict) -> None:
    text = json.dumps(body)
    leaked = [s for s in _SECRETS if s in text]
    assert not leaked, f"credential values served by /status: {leaked}"
    bad_keys = [p for p, k in _walk_keys(body) if _CREDENTIAL_KEY.search(str(k))]
    assert not bad_keys, f"credential-bearing keys served by /status: {bad_keys}"
    for system in body["onboarded_systems"]:
        assert set(system) <= _ALLOWED, sorted(set(system) - _ALLOWED)


def test_status_serves_only_the_allowlisted_fields_of_each_system(status_seams):
    body = onboard.onboard_status()
    _assert_no_connection(body)


def test_status_keeps_what_its_readers_read(status_seams):
    """The prompt library dims prompts by ``id`` and ``system_type``; only
    onboarded systems are listed, and the suggestions still build."""
    body = onboard.onboard_status()
    assert [(s["id"], s["system_type"]) for s in body["onboarded_systems"]] == [
        ("ledger-north", "Finance"), ("hub-south", "Gateway")]
    assert body["onboarded_systems"][0] == {
        "id": "ledger-north", "name": "Ledger North", "plant": "Site North",
        "system_type": "Finance", "connection_type": "PostgreSQL", "status": "onboarded"}
    assert body["setup_complete"] is True
    assert isinstance(body["suggested_questions"], list)
    assert body["tool_count"] == 2


def test_status_does_not_change_the_stored_entries(status_seams):
    """The projection is a copy: the catalog keeps the connection it needs."""
    onboard.onboard_status()
    stored = onboard.catalog.get("ledger-north")
    assert stored["connection_string"].startswith("postgresql://ledger_admin:")
    assert stored["connection_config"]["password"] == "cfg-pw-123"  # nosec B105


def test_the_status_route_serves_no_connection(status_seams):
    app = FastAPI()
    app.include_router(onboard.router)
    app.dependency_overrides[onboard.require_onboard_access] = lambda: None
    resp = TestClient(app).get("/api/onboard/status")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    _assert_no_connection(body)
    assert {s["id"] for s in body["onboarded_systems"]} == {"ledger-north", "hub-south"}
