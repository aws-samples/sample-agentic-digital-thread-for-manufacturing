# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the admin ``GET /api/admin/connection/{system_id}`` route.

Read-only masked "current connection" display (Step 3 / Task 3.2). Exercises the
real ``get_connection`` handler with the catalog and ``describe_connection`` fully
mocked — no live AWS / DB / HTTP / Neptune. Verifies:
  * 200 masked, presence-only shape (host/port/scheme/base_url + has_secret +
    secret_fields KEY NAMES) and NO token/password/secret value, NO inline-cred
    connection string (the BAKE-IN rule)
  * unknown system -> 404
  * non-admin      -> 403

A final test drives the REAL ``connection_resolver.describe_connection`` through
the route against a secret-laden resolved config to genuinely prove the
mask/describe layer strips secrets before they reach the browser.

Runnable two ways (matches ``test_admin_connection_test.py``):
  * pytest:      pytest agentic_digital_thread/tests/unit/test_admin_connection_get.py
  * standalone:  python agentic_digital_thread/tests/unit/test_admin_connection_get.py
"""
import asyncio
import json
import sys
import types
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

# Make `agents...` importable (agentic_digital_thread/ is two levels up from here).
_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

import agents.connection_resolver as cr  # noqa: E402
from agents.admin_routes import get_connection as get_connection_route  # noqa: E402


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
        return {"connection_type": "SiteWise (i3X)"} if system_id in self._known else None


def _patch_catalog(known: set):
    return patch("agents.catalog.catalog", _FakeCatalog(known))


def test_returns_200_masked_presence_only_shape_and_no_secret():
    """Happy path: 200 with the masked presence-only shape; the response body never
    contains a token/password/secret VALUE nor an inline-cred connection string."""
    # The shape describe_connection produces: non-secret fields + presence-only.
    described = {
        "system_id": "sitewise",
        "connection_type": "SiteWise (i3X)",
        "host": "i3x.example.com",
        "port": 443,
        "scheme": "https",
        "base_url": "https://***@i3x.example.com/v1",
        "has_secret": True,
        "secret_fields": ["token"],
    }
    with _patch_catalog({"sitewise"}), \
            patch.object(cr, "describe_connection", lambda sid: described):
        res = asyncio.run(get_connection_route("sitewise", _make_request(is_admin=True)))

    # Shape.
    assert res["system_id"] == "sitewise", res
    assert res["connection_type"] == "SiteWise (i3X)", res
    assert res["host"] == "i3x.example.com", res
    assert res["port"] == 443, res
    assert res["scheme"] == "https", res
    assert res["base_url"] == "https://***@i3x.example.com/v1", res
    # Presence-only secret reporting — names only, never values.
    assert res["has_secret"] is True, res
    assert res["secret_fields"] == ["token"], res
    # No raw secret-bearing field is present as a VALUE.
    assert "connection_string" not in res, res
    assert "password" not in res, res
    # secret_fields lists the KEY NAME "token"; assert no secret VALUE substring.
    blob = json.dumps(res)
    for forbidden in ("dsn-LEAK", "bu-LEAK", "live-tok-LEAK"):
        assert forbidden not in blob, blob
    # No inline-cred connection string ("user:pass@") anywhere in the body.
    assert ":@" not in blob and "LEAK@" not in blob, blob


def test_unknown_system_returns_404():
    with _patch_catalog({"sitewise"}), \
            patch.object(cr, "describe_connection", lambda sid: {}):
        try:
            asyncio.run(get_connection_route("does-not-exist", _make_request(is_admin=True)))
            raise AssertionError("expected HTTPException(404)")
        except HTTPException as e:
            assert e.status_code == 404, e.status_code


def test_non_admin_returns_403():
    with _patch_catalog({"sitewise"}), \
            patch.object(cr, "describe_connection", lambda sid: {}):
        try:
            asyncio.run(get_connection_route("sitewise", _make_request(is_admin=False)))
            raise AssertionError("expected HTTPException(403)")
        except HTTPException as e:
            assert e.status_code == 403, e.status_code


def test_real_describe_through_route_strips_secrets():
    """End-to-end: drive the REAL describe_connection through the route against a
    secret-laden resolved config (inline-cred url/base_url + token + secret_arn).
    The route body must carry the masked, presence-only summary with NO secret
    VALUE and NO inline-cred connection string — proving the server-side
    mask/describe layer is the only thing the browser ever sees."""
    resolved = {
        "connection_type": "SiteWise (i3X)",
        "connection_string": "https://u:dsn-LEAK@i3x.example.com/v1/openapi.json",
        "base_url": "https://u:bu-LEAK@i3x.example.com/v1",
        "scheme": "https",
        "host": "i3x.example.com",
        "port": 443,
        "token": "live-tok-LEAK",
        "secret_arn": "arn:secret:i3x",
    }
    # describe_connection is NOT mocked here — it runs for real, masking the
    # resolved config. Only its dependency resolve_connection is stubbed.
    with _patch_catalog({"sitewise"}), \
            patch.object(cr, "resolve_connection", lambda sid: resolved):
        res = asyncio.run(get_connection_route("sitewise", _make_request(is_admin=True)))

    blob = json.dumps(res)
    # No secret VALUE and no inline-cred fragment leaks.
    for forbidden in ("live-tok-LEAK", "bu-LEAK", "dsn-LEAK"):
        assert forbidden not in blob, f"{forbidden!r} leaked into GET body: {blob}"
    assert "connection_string" not in res, res
    assert "token" not in res and "password" not in res, res
    # Presence-only secret reporting + masked non-secret summary.
    assert res["has_secret"] is True, res
    assert res["secret_fields"] == ["token"], res
    assert res["host"] == "i3x.example.com", res
    assert res["scheme"] == "https", res
    assert res["base_url"].startswith("https://***@i3x.example.com/v1"), res


_TESTS = [
    test_returns_200_masked_presence_only_shape_and_no_secret,
    test_unknown_system_returns_404,
    test_non_admin_returns_403,
    test_real_describe_through_route_strips_secrets,
]


if __name__ == "__main__":
    for t in _TESTS:
        t()
        print(f"  ok  {t.__name__}")
    print(f"RESULT: PASS — {len(_TESTS)} admin connection-GET route tests passed "
          "(no real AWS/DB/HTTP)")
