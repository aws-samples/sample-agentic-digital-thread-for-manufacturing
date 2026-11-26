# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for registering typed custom systems: connect_new_system + the thin
/api/onboard/connect route. Generic (synthetic identifiers only)."""
from unittest.mock import MagicMock, patch

import pytest

from agents.onboard_agent import connect_new_system


@pytest.fixture(autouse=True)
def _athena_role_rule_off(monkeypatch):
    """These tests register Athena systems to pin what the catalog entry
    keeps, not the rule that a deployed app's Athena registration needs a role
    ARN (test_connect_athena_role_required.py): run them as local
    development, where a blank role is accepted."""
    import agents.onboard_agent as onboard_agent
    monkeypatch.setattr(onboard_agent, "local_mode_env", lambda *a, **k: True)


def _fake_catalog(existing=None):
    added = {}
    cat = MagicMock()
    cat.get.return_value = existing
    cat.lookup.return_value = existing
    # unique_schema: a user PostgreSQL registration claims its schema too.
    cat.add.side_effect = lambda e, **kw: added.update(e)
    return cat, added


def test_connect_new_system_persists_connection_config():
    cat, added = _fake_catalog(existing=None)
    with patch("agents.onboard_agent.catalog", cat):
        res = connect_new_system(
            name="Warehouse Analytics", system_type="Historian", plant="Site One",
            connection_type="Athena",
            connection_config={"workgroup": "wg", "database": "db", "region": "us-west-2"},
        )
    assert res["status"] == "created"
    assert res["system_id"] == "warehouse-analytics"
    assert added["connection_type"] == "Athena"
    assert added["connection_config"] == {"workgroup": "wg", "database": "db", "region": "us-west-2"}
    assert added["status"] == "registered"


def test_connect_new_system_persists_guidance_fields():
    """A registration carrying query_guidance/capabilities/tool_prefix stores them
    on the catalog entry so provision can persist them on the System node."""
    cat, added = _fake_catalog(existing=None)
    with patch("agents.onboard_agent.catalog", cat):
        res = connect_new_system(
            name="Warehouse Analytics", system_type="Historian", plant="Site One",
            connection_type="Athena",
            connection_config={"workgroup": "wg", "database": "db"},
            query_guidance="Always list tables first.",
            capabilities="Tabular lake data.",
            tool_prefix="whx",
        )
    assert res["status"] == "created"
    assert added["query_guidance"] == "Always list tables first."
    assert added["capabilities"] == "Tabular lake data."
    assert added["tool_prefix"] == "whx"


def test_connect_new_system_defaults_tool_prefix_to_system_id():
    """Omitted tool_prefix defaults to the (sanitized) system_id."""
    cat, added = _fake_catalog(existing=None)
    with patch("agents.onboard_agent.catalog", cat):
        connect_new_system(
            name="Warehouse Analytics", system_type="Historian", plant="",
            connection_type="Athena",
        )
    assert added["tool_prefix"] == "warehouse-analytics"


def test_connect_new_system_omits_blank_guidance():
    """Blank guidance/capabilities are NOT stored (so the provision fallback to the
    generic per-connection-type default applies)."""
    cat, added = _fake_catalog(existing=None)
    with patch("agents.onboard_agent.catalog", cat):
        connect_new_system(
            name="Warehouse Analytics", system_type="Historian", plant="",
            connection_type="Athena",
        )
    assert "query_guidance" not in added
    assert "capabilities" not in added


def test_onboard_connect_route_forwards_guidance_fields():
    from agents.onboard import onboard_connect
    from agents.models import ConnectRequest

    body = ConnectRequest(
        name="Analytics X", system_type="Historian", connection_type="Athena",
        connection_config={"workgroup": "wg"},
        query_guidance="G", capabilities="C", tool_prefix="ax",
    )
    with patch("agents.onboard_agent.connect_new_system",
               return_value={"status": "created", "system_id": "analytics-x"}) as m:
        res = onboard_connect(body)
    kw = m.call_args.kwargs
    assert kw["query_guidance"] == "G"
    assert kw["capabilities"] == "C"
    assert kw["tool_prefix"] == "ax"
    assert res["status"] == "created"


def test_connect_new_system_rejects_duplicate():
    cat, _ = _fake_catalog(existing={"id": "warehouse-analytics"})
    with patch("agents.onboard_agent.catalog", cat):
        res = connect_new_system(name="Warehouse Analytics", system_type="Historian",
                                 plant="", connection_type="Athena")
    assert "error" in res


def test_connect_new_system_postgres_requires_schema():
    cat, _ = _fake_catalog(existing=None)
    with patch("agents.onboard_agent.catalog", cat):
        res = connect_new_system(name="Rel System", system_type="ERP", plant="",
                                 connection_type="PostgreSQL")
    assert "error" in res


def test_onboard_connect_route_is_thin_adapter_over_connect_new_system():
    from agents.onboard import onboard_connect
    from agents.models import ConnectRequest

    body = ConnectRequest(name="Analytics X", system_type="Historian",
                          connection_type="Athena",
                          connection_config={"workgroup": "wg", "database": "db"})
    with patch("agents.onboard_agent.connect_new_system",
               return_value={"status": "created", "system_id": "analytics-x"}) as m:
        res = onboard_connect(body)

    m.assert_called_once()
    kw = m.call_args.kwargs
    assert kw["connection_type"] == "Athena"
    assert kw["connection_config"] == {"workgroup": "wg", "database": "db"}
    assert res["status"] == "created"


# ── Explicit id passthrough (system_id) ──────────────────────────────────────

def test_connect_new_system_explicit_id_used_verbatim_not_slugified():
    """An explicit system_id is used VERBATIM (sanitized) — NOT derived by
    slugifying the name. 'Milwaukee MES' would slugify to 'milwaukee-mes'; with
    system_id='mes-mil' the id must be exactly 'mes-mil'."""
    cat, added = _fake_catalog(existing=None)
    with patch("agents.onboard_agent.catalog", cat):
        res = connect_new_system(
            name="Milwaukee MES", system_type="MES", plant="Milwaukee",
            connection_type="PostgreSQL", schema="mes_milwaukee",
            system_id="mes-mil",
        )
    assert res["status"] == "created"
    assert res["system_id"] == "mes-mil"
    assert added["id"] == "mes-mil"
    # tool_prefix defaults to the (verbatim) system_id.
    assert added["tool_prefix"] == "mes-mil"


def test_connect_new_system_no_explicit_id_still_slugifies_name():
    """Omitting system_id preserves the prior behavior: derive the id from name."""
    cat, added = _fake_catalog(existing=None)
    with patch("agents.onboard_agent.catalog", cat):
        res = connect_new_system(
            name="Milwaukee MES", system_type="MES", plant="Milwaukee",
            connection_type="PostgreSQL", schema="mes_milwaukee",
        )
    assert res["system_id"] == "milwaukee-mes"


def test_connect_new_system_explicit_id_is_sanitized():
    """An explicit id with unsafe chars is sanitized (slugify-with-validation), not
    taken raw — so a tool-name-invalid id can never be persisted."""
    cat, added = _fake_catalog(existing=None)
    with patch("agents.onboard_agent.catalog", cat):
        res = connect_new_system(
            name="Anything", system_type="Custom", plant="",
            connection_type="Athena", connection_config={"workgroup": "wg"},
            system_id="MES Mil!!",
        )
    assert res["system_id"] == "mes-mil"


def test_connect_request_accepts_id_alias_and_forwards_system_id():
    from agents.onboard import onboard_connect
    from agents.models import ConnectRequest

    # The alias 'id' populates system_id.
    body = ConnectRequest(name="X", connection_type="Athena",
                          connection_config={"workgroup": "wg"}, id="pinned-id")
    assert body.system_id == "pinned-id"
    with patch("agents.onboard_agent.connect_new_system",
               return_value={"status": "created", "system_id": "pinned-id"}) as m:
        onboard_connect(body)
    assert m.call_args.kwargs["system_id"] == "pinned-id"


# ── SiteWise (Direct) registration via /api/onboard/connect ───────────────────

def test_connect_new_system_accepts_sitewise_direct_with_region():
    """SiteWise (Direct) registers with a connection_config carrying region — no
    schema required (only PostgreSQL requires schema)."""
    cat, added = _fake_catalog(existing=None)
    with patch("agents.onboard_agent.catalog", cat):
        res = connect_new_system(
            name="Plant Telemetry", system_type="Custom", plant="",
            connection_type="SiteWise (Direct)",
            connection_config={"region": "us-west-2"},
        )
    assert res["status"] == "created"
    assert added["connection_type"] == "SiteWise (Direct)"
    assert added["connection_config"] == {"region": "us-west-2"}
    assert added["status"] == "registered"


def test_onboard_connect_route_forwards_sitewise_direct_region():
    from agents.onboard import onboard_connect
    from agents.models import ConnectRequest

    body = ConnectRequest(
        name="Plant Telemetry", system_type="Custom",
        connection_type="SiteWise (Direct)", connection_config={"region": "eu-central-1"},
    )
    with patch("agents.onboard_agent.connect_new_system",
               return_value={"status": "created", "system_id": "plant-telemetry"}) as m:
        res = onboard_connect(body)
    kw = m.call_args.kwargs
    assert kw["connection_type"] == "SiteWise (Direct)"
    assert kw["connection_config"] == {"region": "eu-central-1"}
    assert res["status"] == "created"
