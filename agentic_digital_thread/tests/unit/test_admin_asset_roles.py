# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""``POST /api/admin/asset-roles/reclassify`` with the role pass and audit mocked."""
import types
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse

import agents.asset_role_pass as arp
import agents.connection_resolver as cr
from agents.admin_routes import reclassify_asset_roles


def _request(is_admin: bool):
    return types.SimpleNamespace(state=types.SimpleNamespace(
        is_admin=is_admin, user=types.SimpleNamespace(email="admin@example.com")))


class _Catalog:
    def lookup(self, sid):
        if sid == "hist":
            return {"connection_type": "SiteWise (Direct)"}
        if sid == "gone":
            return {"connection_type": "PostgreSQL", "status": "removal_failed"}
        return None


@pytest.fixture(autouse=True)
def _catalog():
    with patch("agents.catalog.catalog", _Catalog()):
        yield


def test_non_admin_is_refused():
    with pytest.raises(HTTPException) as exc:
        reclassify_asset_roles(_request(False))
    assert exc.value.status_code == 403


def test_unknown_system_is_404():
    with pytest.raises(HTTPException) as exc:
        reclassify_asset_roles(_request(True), system_id="nope")
    assert exc.value.status_code == 404


def test_removal_failed_system_is_refused_and_not_written():
    calls = []
    with patch.object(arp, "classify_system",
                      lambda sid, **kw: calls.append(sid) or {"assets": 1}), \
            patch.object(cr, "audit_connection_event", lambda *a, **k: None):
        with pytest.raises(HTTPException) as exc:
            reclassify_asset_roles(_request(True), system_id="gone")
    assert exc.value.status_code == 409
    assert "gone" in exc.value.detail and "removal" in exc.value.detail.lower()
    assert calls == []   # the role pass never ran for a half-removed system


def test_running_removal_is_refused_and_not_written():
    """A removal still running (its catalog entry still 'onboarded', the reset
    job 'running') is quarantined too: re-deriving roles mid-teardown writes to
    a system that is being deleted."""
    import agents.onboard as onboard
    calls = []
    absent = object()
    prior = onboard._RESET_JOBS.get("hist", absent)
    onboard._RESET_JOBS["hist"] = {"state": "running"}
    try:
        with patch.object(arp, "classify_system",
                          lambda sid, **kw: calls.append(sid) or {"assets": 1}), \
                patch.object(cr, "audit_connection_event", lambda *a, **k: None):
            with pytest.raises(HTTPException) as exc:
                reclassify_asset_roles(_request(True), system_id="hist")
    finally:
        # Put the shared JobStore back exactly as it was for later tests.
        if prior is absent:
            onboard._RESET_JOBS.pop("hist", None)
        else:
            onboard._RESET_JOBS["hist"] = prior
    assert exc.value.status_code == 409
    assert "hist" in exc.value.detail and "removed" in exc.value.detail.lower()
    assert calls == []


def test_switched_off_allows_only_restore(monkeypatch):
    monkeypatch.setenv("ADT_ASSET_ROLES", "off")
    with pytest.raises(HTTPException) as exc:
        reclassify_asset_roles(_request(True))
    assert exc.value.status_code == 409
    with patch.object(arp, "restore_all", lambda: {"systems": {"hist": {"restored": True}}}), \
            patch.object(cr, "audit_connection_event", lambda *a, **k: None):
        res = reclassify_asset_roles(_request(True), restore=True)
    assert res["status"] == "ok" and res["restore"] is True


def test_one_system_dry_run_is_not_audited():
    audits, calls = [], []
    with patch.object(arp, "classify_system",
                      lambda sid, **kw: calls.append((sid, kw)) or {"assets": 2}), \
            patch.object(cr, "audit_connection_event", lambda *a, **k: audits.append(a)):
        res = reclassify_asset_roles(_request(True), system_id="hist", dry_run=True)
    assert calls == [("hist", {"dry_run": True})]
    assert res["status"] == "ok" and res["systems"] == {"hist": {"assets": 2}}
    assert audits == []


def test_all_systems_write_is_audited_ok():
    audits = []
    with patch.object(arp, "classify_all", lambda **kw: {"systems": {"a": {}, "b": {}}}), \
            patch.object(cr, "audit_connection_event", lambda *a, **k: audits.append((a, k))):
        res = reclassify_asset_roles(_request(True))
    assert res["status"] == "ok" and set(res["systems"]) == {"a", "b"}
    assert audits[0][0][:2] == ("asset_roles_reclassify", "all")
    assert audits[0][1]["outcome"] == "ok" and audits[0][1]["extra"] == {"systems": 2, "failed": 0}


def test_a_failed_system_is_500_and_audited_as_error():
    audits = []
    with patch.object(arp, "classify_all",
                      lambda **kw: {"systems": {"a": {"error": "graph write failed"}, "b": {}}}), \
            patch.object(cr, "audit_connection_event", lambda *a, **k: audits.append(k)):
        res = reclassify_asset_roles(_request(True))
    assert isinstance(res, JSONResponse) and res.status_code == 500
    assert audits[0]["outcome"] == "error" and audits[0]["extra"]["failed"] == 1


def test_an_exception_is_a_generic_500():
    def boom(**kw):
        raise RuntimeError("secret internals")
    with patch.object(arp, "classify_all", boom), \
            patch.object(cr, "audit_connection_event", lambda *a, **k: None):
        res = reclassify_asset_roles(_request(True))
    assert isinstance(res, JSONResponse) and res.status_code == 500
    assert b"secret internals" not in res.body


def test_restore_has_no_dry_run():
    with pytest.raises(HTTPException) as exc:
        reclassify_asset_roles(_request(True), restore=True, dry_run=True)
    assert exc.value.status_code == 400


def test_a_failed_coverage_write_makes_the_reclassify_an_error():
    audits = []
    summary = {"system": "hist", "assets": 6, "written": True, "coverage_written": False,
               "coverage_error": "graph write failed: write_failed", "coverage_cleared": True}
    with patch.object(arp, "classify_all", lambda dry_run=False: {"systems": {"hist": summary}}), \
            patch.object(cr, "audit_connection_event", lambda *a, **k: audits.append(k)):
        res = reclassify_asset_roles(_request(True))
    assert isinstance(res, JSONResponse) and res.status_code == 500
    import json as _json
    body = _json.loads(res.body)
    assert body["status"] == "error" and body["failed"] == ["hist"]
    assert body["systems"]["hist"]["written"] is True
    assert audits[0]["outcome"] == "error"


def test_a_failed_coverage_clear_on_restore_makes_the_restore_an_error():
    class _G:
        writes = []

        def query(self, cypher, params=None):
            return [{"system": "hist", "has_system": True, "removal_incomplete": False}]

        def write(self, cypher, params=None):
            return cypher != arp._CLEAR_COVERAGE
    g = _G()
    with patch.object(arp, "_db", lambda: (g.query, g.write)), \
            patch.object(cr, "audit_connection_event", lambda *a, **k: None):
        res = reclassify_asset_roles(_request(True), restore=True)
    assert isinstance(res, JSONResponse) and res.status_code == 500
