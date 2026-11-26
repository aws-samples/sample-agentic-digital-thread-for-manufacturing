# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the generic admin ``PUT /api/admin/system-type/{system_id}`` route
and the ``SystemCatalog.set_system_type`` persistence seam.

The route sets a FREE-FORM ``system_type`` label (no dataset/system-id literal is
hardcoded in the handler — the value is supplied at runtime), persists it to BOTH the
durable Neptune ``System`` node and the in-process catalog entry, and is admin-gated.
Everything external (catalog, neptune_write) is mocked — no live AWS / DB / Neptune.

Verifies:
  * happy path -> 200 {status:ok, system_type} + node write cypher + catalog entry updated
  * label is applied GENERICALLY (any string; handler hardcodes none)
  * empty string clears the label
  * unknown system -> 404
  * non-admin -> 403
  * non-string / missing field -> 400
  * a graph write failure (neptune_write -> False) -> 500

Runnable two ways:
  * pytest:      pytest agentic_digital_thread/tests/unit/test_admin_system_type.py
  * standalone:  python agentic_digital_thread/tests/unit/test_admin_system_type.py
"""
import asyncio
import sys
import types
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

# Make `agents...` importable (agentic_digital_thread/ is two levels up from here).
_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

import agents.admin_routes as admin_routes  # noqa: E402
import agents.db as agents_db  # noqa: E402
from agents.admin_routes import set_system_type as set_system_type_route  # noqa: E402
from agents.catalog import SystemCatalog  # noqa: E402


def _make_request(is_admin: bool, body, email: str = "admin@example.com"):
    """A fake Starlette Request: sync .state + async .json()."""
    async def _json():
        if isinstance(body, Exception):
            raise body
        return body

    req = types.SimpleNamespace()
    req.state = types.SimpleNamespace(
        is_admin=is_admin,
        user=types.SimpleNamespace(email=email),
    )
    req.json = _json
    return req


class _FakeCatalog:
    """Minimal catalog with a real set_system_type over an in-memory entry."""
    def __init__(self, known: set):
        self._entries = {sid: {"id": sid, "system_type": ""} for sid in known}

    def get(self, system_id):
        return self._entries.get(system_id)

    def set_system_type(self, system_id, system_type):
        if system_id in self._entries:
            self._entries[system_id]["system_type"] = system_type
            return True
        return False


def _run(route_coro):
    return asyncio.run(route_coro)


def test_happy_path_persists_node_and_catalog():
    """200 + the node write is issued with SET s.system_type, and the catalog entry updates."""
    writes = []
    cat = _FakeCatalog({"lake-athena"})

    def _fake_write(cypher, params=None):
        writes.append((cypher, params))
        return True

    with patch("agents.catalog.catalog", cat), \
            patch.object(agents_db, "neptune_write", _fake_write):
        res = _run(set_system_type_route(
            "lake-athena", _make_request(is_admin=True, body={"system_type": "MES"})))

    assert res["status"] == "ok", res
    assert res["system_id"] == "lake-athena", res
    assert res["system_type"] == "MES", res
    # Durable: exactly one node write, targeting the System node's system_type.
    assert len(writes) == 1, writes
    cypher, params = writes[0]
    assert "SET s.system_type" in cypher, cypher
    assert params == {"sid": "lake-athena", "stype": "MES"}, params
    # Immediate: the in-process catalog entry reflects the new label.
    assert cat.get("lake-athena")["system_type"] == "MES"


def test_label_is_generic_any_value():
    """The handler hardcodes NO label — an arbitrary free-form value round-trips."""
    cat = _FakeCatalog({"sys1"})
    with patch("agents.catalog.catalog", cat), \
            patch.object(agents_db, "neptune_write", lambda *a, **k: True):
        res = _run(set_system_type_route(
            "sys1", _make_request(is_admin=True, body={"system_type": "  Historian  "})))
    # Whitespace is trimmed; the value is otherwise verbatim (generic, no coercion).
    assert res["system_type"] == "Historian", res
    assert cat.get("sys1")["system_type"] == "Historian"


def test_empty_string_clears_label():
    """An empty label is accepted (clears the type)."""
    cat = _FakeCatalog({"sys1"})
    cat.set_system_type("sys1", "MES")
    with patch("agents.catalog.catalog", cat), \
            patch.object(agents_db, "neptune_write", lambda *a, **k: True):
        res = _run(set_system_type_route(
            "sys1", _make_request(is_admin=True, body={"system_type": ""})))
    assert res["status"] == "ok", res
    assert res["system_type"] == "", res
    assert cat.get("sys1")["system_type"] == ""


def test_unknown_system_404():
    cat = _FakeCatalog(set())
    with patch("agents.catalog.catalog", cat), \
            patch.object(agents_db, "neptune_write", lambda *a, **k: True):
        try:
            _run(set_system_type_route(
                "nope", _make_request(is_admin=True, body={"system_type": "MES"})))
            raise AssertionError("expected HTTPException(404)")
        except HTTPException as e:
            assert e.status_code == 404, e


def test_non_admin_403():
    cat = _FakeCatalog({"sys1"})
    with patch("agents.catalog.catalog", cat), \
            patch.object(agents_db, "neptune_write", lambda *a, **k: True):
        try:
            _run(set_system_type_route(
                "sys1", _make_request(is_admin=False, body={"system_type": "MES"})))
            raise AssertionError("expected HTTPException(403)")
        except HTTPException as e:
            assert e.status_code == 403, e


def test_missing_field_400():
    cat = _FakeCatalog({"sys1"})
    with patch("agents.catalog.catalog", cat), \
            patch.object(agents_db, "neptune_write", lambda *a, **k: True):
        res = _run(set_system_type_route(
            "sys1", _make_request(is_admin=True, body={"other": "x"})))
    # JSONResponse — inspect status_code on the response object.
    assert getattr(res, "status_code", 200) == 400, res


def test_non_string_400():
    cat = _FakeCatalog({"sys1"})
    with patch("agents.catalog.catalog", cat), \
            patch.object(agents_db, "neptune_write", lambda *a, **k: True):
        res = _run(set_system_type_route(
            "sys1", _make_request(is_admin=True, body={"system_type": 123})))
    assert getattr(res, "status_code", 200) == 400, res


def test_graph_write_failure_500():
    """neptune_write returning False (write failed) surfaces as a 500 (fail-loud)."""
    cat = _FakeCatalog({"sys1"})
    with patch("agents.catalog.catalog", cat), \
            patch.object(agents_db, "neptune_write", lambda *a, **k: False):
        res = _run(set_system_type_route(
            "sys1", _make_request(is_admin=True, body={"system_type": "MES"})))
    assert getattr(res, "status_code", 200) == 500, res


def test_catalog_set_system_type_seam():
    """SystemCatalog.set_system_type updates a matching entry and reports miss."""
    cat = SystemCatalog([{"id": "a", "system_type": "", "status": "onboarded"}])
    assert cat.set_system_type("a", "MES") is True
    assert cat.get("a")["system_type"] == "MES"
    assert cat.set_system_type("missing", "X") is False


_TESTS = [
    test_happy_path_persists_node_and_catalog,
    test_label_is_generic_any_value,
    test_empty_string_clears_label,
    test_unknown_system_404,
    test_non_admin_403,
    test_missing_field_400,
    test_non_string_400,
    test_graph_write_failure_500,
    test_catalog_set_system_type_seam,
]


if __name__ == "__main__":
    for t in _TESTS:
        t()
        print(f"  ok  {t.__name__}")
    print(f"RESULT: PASS — {len(_TESTS)} admin system-type tests passed")
