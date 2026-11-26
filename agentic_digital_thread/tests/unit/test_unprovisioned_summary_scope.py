# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Only PostgreSQL and Tulip registrations are summarised from the registration.

Before its first provision a user registration has no graph node.
``connection_resolver.describe_unprovisioned_registration`` summarises a
PostgreSQL or Tulip registration from the registration itself (a Tulip system
set up with only a Secrets Manager ARN takes its Base URL from that secret).
Every other connection type keeps the earlier behaviour: ``None``, so
``GET /api/admin/connection/{id}`` summarises it as before.

A supported MCP server with no token has no secret and keeps its URL in
``connection_string``. It must not be treated as an unreadable connection
(HTTP 500 from the current-connection read). Offline.
"""
from __future__ import annotations

import os

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.admin_routes as admin_routes  # noqa: E402
import agents.connection_resolver as cr  # noqa: E402
from tests.unit.test_connect_new_system_custom_types import (  # noqa: E402,F401 - fixtures
    SID, cat, no_dns, secrets)
from tests.unit.test_connect_new_system_credential_rotation import graph  # noqa: E402,F401
from tests.unit.test_connect_new_system_credential_source import (  # noqa: E402
    _register_mcp)


def test_an_mcp_registration_with_no_token_is_not_summarised_from_its_registration(
        cat, secrets, graph):
    assert _register_mcp(token="").get("status") == "created"
    from agents.catalog import catalog
    assert cr.describe_unprovisioned_registration(SID, catalog.get(SID)) is None


def test_the_current_connection_read_of_an_mcp_registration_with_no_token_is_not_an_error(
        cat, secrets, graph):
    assert _register_mcp(token="").get("status") == "created"
    from agents.catalog import catalog
    out = admin_routes._describe_connection_for(SID, catalog.get(SID))
    assert getattr(out, "status_code", 200) == 200, getattr(out, "body", out)


def test_an_mcp_registration_with_a_token_keeps_the_earlier_summary(cat, secrets, graph):
    """The MCP type is outside the registration summary with or without a token."""
    assert _register_mcp().get("status") == "created"
    from agents.catalog import catalog
    assert cr.describe_unprovisioned_registration(SID, catalog.get(SID)) is None
