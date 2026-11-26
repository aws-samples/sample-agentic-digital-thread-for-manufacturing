# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the admin ``POST /api/admin/entities/rescan[/{system_id}]`` routes.

Exercises the real handlers with the catalog, the rescan library, and the audit log
fully mocked — no live AWS / Neptune / LLM (pattern mirrors
``test_admin_connection_test.py``). Verifies:

  * admin guard enforced  → 403 for a non-admin request (both routes)
  * unknown system        → 404
  * happy path            → 200 with the summary JSON + audit event fired
  * library failure       → 500 with a generic detail (no internals leaked)
"""
import types
from pathlib import Path
import sys
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.responses import JSONResponse

# Make `agents...` importable (agentic_digital_thread/ is two levels up from here).
_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

import agents.connection_resolver as cr  # noqa: E402
import agents.entity_rescan as entity_rescan  # noqa: E402
from agents.admin_routes import (  # noqa: E402
    rescan_entities_all,
    rescan_entities_for_system,
)


def _make_request(is_admin: bool, email: str = "admin@example.com"):
    req = types.SimpleNamespace()
    req.state = types.SimpleNamespace(
        is_admin=is_admin,
        user=types.SimpleNamespace(email=email),
    )
    return req


class _FakeCatalog:
    def __init__(self, known: set):
        self._known = known

    def get(self, system_id):
        return {"connection_type": "PostgreSQL"} if system_id in self._known else None


def _patch_catalog(known: set):
    return patch("agents.catalog.catalog", _FakeCatalog(known))


_SUMMARY = {
    "dry_run": False,
    "systems": {
        "erp-x": {"tables_scanned": 3, "entities_written": 2, "identities_written": 4,
                  "skipped_existing": 1, "tier_counts": {"curated": 1, "llm": 1},
                  "errors": []},
    },
}


def test_happy_path_returns_summary_and_audits():
    audit_calls = []
    rescan_calls = []

    def fake_rescan(system_id, **kwargs):
        rescan_calls.append(system_id)
        return dict(_SUMMARY)

    with _patch_catalog({"erp-x"}), \
            patch.object(entity_rescan, "rescan_entities", fake_rescan), \
            patch.object(cr, "audit_connection_event",
                         lambda *a, **k: audit_calls.append((a, k))):
        res = rescan_entities_for_system("erp-x", _make_request(is_admin=True))

    assert rescan_calls == ["erp-x"]
    assert res["status"] == "ok", res
    assert res["systems"]["erp-x"]["entities_written"] == 2
    assert res["systems"]["erp-x"]["tier_counts"] == {"curated": 1, "llm": 1}
    assert res["totals"] == {"systems": 1, "entities_written": 2,
                             "identities_written": 4, "skipped_existing": 1,
                             "errors": 0}
    # Audit fired with the rescan outcome + totals.
    assert audit_calls, "audit_connection_event was not called"
    (args, kwargs) = audit_calls[0]
    assert args[0] == "entity_rescan" and args[1] == "erp-x"
    assert kwargs.get("outcome") == "ok"
    assert kwargs.get("extra", {}).get("entities_written") == 2


def test_all_systems_route_passes_none_and_audits_scope_all():
    audit_calls = []
    rescan_calls = []

    def fake_rescan(system_id, **kwargs):
        rescan_calls.append(system_id)
        return dict(_SUMMARY)

    with _patch_catalog({"erp-x"}), \
            patch.object(entity_rescan, "rescan_entities", fake_rescan), \
            patch.object(cr, "audit_connection_event",
                         lambda *a, **k: audit_calls.append((a, k))):
        res = rescan_entities_all(_make_request(is_admin=True))

    assert rescan_calls == [None]
    assert res["status"] == "ok"
    (args, kwargs) = audit_calls[0]
    assert args[1] == "all"


def test_non_admin_returns_403_both_routes():
    with _patch_catalog({"erp-x"}):
        for call in (lambda: rescan_entities_for_system("erp-x", _make_request(is_admin=False)),
                     lambda: rescan_entities_all(_make_request(is_admin=False))):
            try:
                call()
                raise AssertionError("expected HTTPException(403)")
            except HTTPException as e:
                assert e.status_code == 403, e.status_code


def test_unknown_system_returns_404():
    with _patch_catalog({"erp-x"}):
        try:
            rescan_entities_for_system("does-not-exist", _make_request(is_admin=True))
            raise AssertionError("expected HTTPException(404)")
        except HTTPException as e:
            assert e.status_code == 404, e.status_code


def test_library_unknown_system_error_maps_to_404():
    """Defense in depth: if the library itself rejects the system, the route 404s."""
    def raise_unknown(system_id, **kwargs):
        raise entity_rescan.UnknownSystemError(f"Unknown system: {system_id}")

    with _patch_catalog({"erp-x"}), \
            patch.object(entity_rescan, "rescan_entities", raise_unknown):
        try:
            rescan_entities_for_system("erp-x", _make_request(is_admin=True))
            raise AssertionError("expected HTTPException(404)")
        except HTTPException as e:
            assert e.status_code == 404, e.status_code


def test_library_failure_returns_500_generic_detail():
    audit_calls = []

    def boom(system_id, **kwargs):
        raise RuntimeError("neptune endpoint 10.0.1.5:8182 unreachable")

    with _patch_catalog({"erp-x"}), \
            patch.object(entity_rescan, "rescan_entities", boom), \
            patch.object(cr, "audit_connection_event",
                         lambda *a, **k: audit_calls.append((a, k))):
        res = rescan_entities_for_system("erp-x", _make_request(is_admin=True))

    assert isinstance(res, JSONResponse) and res.status_code == 500
    body = res.body.decode()
    assert "10.0.1.5" not in body            # internals never leak to the client
    assert "Entity rescan failed" in body
    assert audit_calls and audit_calls[0][1].get("outcome") == "error"
