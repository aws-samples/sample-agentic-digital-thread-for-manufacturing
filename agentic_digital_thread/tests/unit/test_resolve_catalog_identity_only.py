# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Regression guard for Step-3 (revised) Task 2.3.

Locks the connection-vs-identity boundary in ``agents/resolve.py``:

  * resolve.py reads ONLY system IDENTITY/metadata off the catalog
    (``connection_type``, ``status``, ``schema``) — never CONNECTION data
    (``connection_string`` / ``connection_config`` / ``secret_arn``).
  * Therefore any *connection* read must come from ``resolve_connection`` —
    today there are zero connection reads in resolve.py, so the catalog
    ``catalog.get()`` identity reads legitimately remain (the graph/catalog
    identity registry stays). If a future edit adds a CONNECTION read off a
    catalog entry, ``test_no_connection_data_field_read_in_resolve`` fails.

The static tests automate the Task-2.3 grep gate
("no catalog.get() CONNECTION reads remain in resolve.py").
The behavioral test mocks ``resolve_connection`` (and ``pg``/``query_system``)
so nothing live is touched, and proves protocol routing is driven by the
catalog *identity* ``connection_type`` rather than ``resolve_connection``.

Runnable two ways:
  * pytest:      pytest agentic_digital_thread/tests/unit/test_resolve_catalog_identity_only.py
  * standalone:  python agentic_digital_thread/tests/unit/test_resolve_catalog_identity_only.py

(Note: ``.venv-local`` doesn't ship pytest; the standalone runner works there.)
"""
import ast
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Make `agents...` importable (agentic_digital_thread/ is two levels up from here).
_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

_RESOLVE_PY = _ADT_ROOT / "agents" / "resolve.py"

# The fields that constitute CONNECTION data per ADR 0008.
# Reads of these off a catalog entry must go through resolve_connection().
CONNECTION_FIELDS = frozenset({"connection_string", "connection_config", "secret_arn"})

# Identity/menu metadata that legitimately stays on the catalog identity
# registry (id/name/plant/system_type/connection_type/
# schema/query_guidance/capabilities/tool_prefix/status).
IDENTITY_FIELDS = frozenset({
    "connection_type", "status", "schema", "name", "plant", "system_type",
    "query_guidance", "capabilities", "tool_prefix", "id",
})


def _resolve_tree() -> ast.Module:
    return ast.parse(_RESOLVE_PY.read_text(encoding="utf-8"))


def _str_const(node) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _all_get_and_subscript_string_keys(tree: ast.Module) -> set[str]:
    """Every string used as a ``.get('x')`` first arg or ``[...]`` subscript key."""
    keys: set[str] = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and node.args):
            k = _str_const(node.args[0])
            if k is not None:
                keys.add(k)
        if isinstance(node, ast.Subscript):
            k = _str_const(node.slice)
            if k is not None:
                keys.add(k)
    return keys


def _catalog_get_target_names(tree: ast.Module) -> set[str]:
    """Names assigned from a ``catalog.get(...)`` call (e.g. ``entry``)."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        val = node.value
        if (isinstance(val, ast.Call)
                and isinstance(val.func, ast.Attribute)
                and val.func.attr == "get"
                and isinstance(val.func.value, ast.Name)
                and val.func.value.id == "catalog"):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    names.add(tgt.id)
    return names


def _count_catalog_get_calls(tree: ast.Module) -> int:
    n = 0
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "catalog"):
            n += 1
    return n


def _fields_read_off_names(tree: ast.Module, names: set[str]) -> set[str]:
    """Fields read via ``<name>.get('x')`` or ``<name>['x']`` for given names."""
    fields: set[str] = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in names
                and node.args):
            k = _str_const(node.args[0])
            if k is not None:
                fields.add(k)
        if (isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Name)
                and node.value.id in names):
            k = _str_const(node.slice)
            if k is not None:
                fields.add(k)
    return fields


# --------------------------------------------------------------------------- #
# Static grep-gate automation
# --------------------------------------------------------------------------- #

def test_no_connection_data_field_read_in_resolve():
    """No CONNECTION-data field literal is read anywhere in resolve.py.

    This is the Task-2.3 grep gate as code: connection_string / connection_config
    / secret_arn must never be read off a catalog entry in resolve.py — those
    reads belong to resolve_connection().
    """
    keys = _all_get_and_subscript_string_keys(_resolve_tree())
    leaked = CONNECTION_FIELDS & keys
    assert not leaked, (
        f"resolve.py reads CONNECTION-data field(s) {sorted(leaked)} directly; "
        "route these through resolve_connection() instead of catalog.get()."
    )


def test_catalog_get_reads_are_identity_only():
    """Every catalog.get() read in resolve.py is identity/metadata, never connection."""
    tree = _resolve_tree()
    targets = _catalog_get_target_names(tree)
    assert targets, "expected at least one variable assigned from catalog.get(...)"

    fields = _fields_read_off_names(tree, targets)
    # No connection-data field is read off a catalog entry.
    assert not (CONNECTION_FIELDS & fields), (
        f"catalog entry reads include CONNECTION data: {sorted(CONNECTION_FIELDS & fields)}"
    )
    # Every field read off a catalog entry is a known identity/metadata field.
    unexpected = fields - IDENTITY_FIELDS
    assert not unexpected, (
        f"catalog entry reads include non-identity field(s): {sorted(unexpected)}"
    )


def test_four_catalog_get_sites_present():
    """Documents the known catalog.get() identity sites. The 5th site is the ADR-0040
    entity-anchored fallback (``_try_entity_anchored``), which routes by the catalog
    *identity* ``connection_type`` exactly like the sibling ``_try_brute_force`` pass —
    still an identity-only read (asserted by ``test_catalog_get_reads_are_identity_only``)."""
    assert _count_catalog_get_calls(_resolve_tree()) == 5


# --------------------------------------------------------------------------- #
# Behavioral: protocol routing uses catalog identity, not resolve_connection
# --------------------------------------------------------------------------- #

def test_routing_uses_catalog_identity_not_resolve_connection():
    """_query_for_system routes by the catalog *identity* connection_type and does
    NOT call resolve_connection. resolve_connection is mocked; pg/query_system are
    mocked, so nothing live is touched."""
    import agents.connection_resolver as cr
    import agents.resolve as resolve

    class _FakeCatalog:
        def __init__(self, entry):
            self._entry = entry

        def get(self, _sid):
            return self._entry

    # PostgreSQL identity -> pg() path, never query_system(), never resolve_connection().
    with patch.object(resolve, "catalog", _FakeCatalog({"connection_type": "PostgreSQL"})), \
            patch.object(resolve, "pg", MagicMock(return_value={"rows": [], "columns": []})) as m_pg, \
            patch.object(resolve, "query_system", MagicMock()) as m_qs, \
            patch.object(cr, "resolve_connection", MagicMock()) as m_rc:
        resolve._query_for_system("erp", "erp_data", "SELECT 1")
        assert m_pg.call_count == 1
        m_qs.assert_not_called()
        m_rc.assert_not_called()

    # Non-PostgreSQL identity -> query_system() path, never pg(), never resolve_connection().
    with patch.object(resolve, "catalog", _FakeCatalog({"connection_type": "MCP Server"})), \
            patch.object(resolve, "pg", MagicMock()) as m_pg2, \
            patch.object(resolve, "query_system", MagicMock(return_value={"rows": []})) as m_qs2, \
            patch.object(cr, "resolve_connection", MagicMock()) as m_rc2:
        resolve._query_for_system("highbyte", "", "SELECT 1")
        m_qs2.assert_called_once()
        m_pg2.assert_not_called()
        m_rc2.assert_not_called()


if __name__ == "__main__":
    _tests = [
        test_no_connection_data_field_read_in_resolve,
        test_catalog_get_reads_are_identity_only,
        test_four_catalog_get_sites_present,
        test_routing_uses_catalog_identity_not_resolve_connection,
    ]
    _passed = 0
    for _t in _tests:
        _t()
        _passed += 1
        print(f"ok  {_t.__name__}")
    print(f"\n{_passed}/{len(_tests)} passed")
