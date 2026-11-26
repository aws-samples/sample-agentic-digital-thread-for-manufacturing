# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for ``agents.onboard_agent.test_connection`` delegation.

Proves the ``test_connection`` Strands ``@tool`` delegates to the single shared
probe (``connection_resolver.probe_connection``) and adapts its
``{status: pass|fail, detail, ...}`` shape back to the tool's stable
LLM-facing shape — without touching real AWS, Postgres, HTTP, or Neptune.

Runnable two ways (matches ``test_connection_resolver.py``):
  * pytest:      pytest agentic_digital_thread/tests/unit/test_onboard_agent_test_connection.py
  * standalone:  python agentic_digital_thread/tests/unit/test_onboard_agent_test_connection.py
"""
import sys
from pathlib import Path

# Make `agents...` importable (agentic_digital_thread/ is two levels up from here).
_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

import agents.connection_resolver as cr  # noqa: E402
import agents.onboard_agent as oa  # noqa: E402


def _invoke(system_id: str) -> dict:
    """Call the Strands @tool directly (returns the underlying function's dict)."""
    return oa.test_connection(system_id)


def test_unknown_system_short_circuits_without_probing():
    """An unknown system returns the not-found message and never calls the probe."""
    called = {"n": 0}

    def _probe(_sid):
        called["n"] += 1
        return cr._result("pass", 1, "PostgreSQL", "SELECT 1 ok")

    orig_get, orig_probe = oa.catalog.get, cr.probe_connection
    oa.catalog.get = lambda sid: None
    cr.probe_connection = _probe
    try:
        res = _invoke("nope")
        assert res == {"error": "System nope not found"}, res
        assert called["n"] == 0, "probe must not run for unknown systems"
    finally:
        oa.catalog.get, cr.probe_connection = orig_get, orig_probe


def test_delegates_to_probe_and_adapts_pass():
    """A 'pass' from the shared probe maps to the tool's 'connected' shape."""
    seen = {}

    def _probe(system_id):
        seen["system_id"] = system_id
        return cr._result("pass", 42, "PostgreSQL", "SELECT 1 ok")

    orig_get, orig_probe = oa.catalog.get, cr.probe_connection
    oa.catalog.get = lambda sid: {"name": "ERP System", "connection_type": "PostgreSQL"}
    cr.probe_connection = _probe
    try:
        res = _invoke("erp")
        # Delegated with the system_id.
        assert seen["system_id"] == "erp"
        # Adapted to the stable LLM-facing shape.
        assert res == {
            "status": "connected",
            "system_id": "erp",
            "name": "ERP System",
            "connection_type": "PostgreSQL",
        }, res
    finally:
        oa.catalog.get, cr.probe_connection = orig_get, orig_probe


def test_delegates_to_probe_and_adapts_fail_with_secret_free_detail():
    """A 'fail' maps to 'failed' and surfaces the probe's secret-free detail."""
    def _probe(system_id):
        assert system_id == "highbyte"
        return cr._result("fail", 7, "MCP Server", "authentication failed (401) — check token")

    orig_get, orig_probe = oa.catalog.get, cr.probe_connection
    oa.catalog.get = lambda sid: {"name": "HighByte", "connection_type": "MCP Server"}
    cr.probe_connection = _probe
    try:
        res = _invoke("highbyte")
        assert res == {
            "status": "failed",
            "error": "authentication failed (401) — check token",
            "connection_type": "MCP Server",
        }, res
    finally:
        oa.catalog.get, cr.probe_connection = orig_get, orig_probe


def test_connection_type_falls_back_to_catalog_when_probe_omits_it():
    """If the probe result lacks a connection_type, the catalog entry's type is used."""
    def _probe(system_id):
        return {"status": "pass", "latency_ms": 1, "connection_type": None, "detail": "ok"}

    orig_get, orig_probe = oa.catalog.get, cr.probe_connection
    oa.catalog.get = lambda sid: {"name": "MES", "connection_type": "REST API"}
    cr.probe_connection = _probe
    try:
        res = _invoke("mes")
        assert res["connection_type"] == "REST API", res
    finally:
        oa.catalog.get, cr.probe_connection = orig_get, orig_probe


_TESTS = [
    test_unknown_system_short_circuits_without_probing,
    test_delegates_to_probe_and_adapts_pass,
    test_delegates_to_probe_and_adapts_fail_with_secret_free_detail,
    test_connection_type_falls_back_to_catalog_when_probe_omits_it,
]


if __name__ == "__main__":
    for t in _TESTS:
        t()
        print(f"  ok  {t.__name__}")
    print(f"RESULT: PASS — {len(_TESTS)} onboard_agent.test_connection delegation tests passed")
