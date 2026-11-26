# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The Connect New System MCP form's optional token: what the server does with it.

The form sends ``connection_config.token`` through the existing registration
path. The token lands only in the registration's own ``adt/conn/<id>``
secret, bound to it: never in the registration response, the catalog entry or
``GET /api/onboard/catalog``. With no token the registration writes no secret.
Offline.
"""
from __future__ import annotations

import json
import os

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

from tests.unit.test_connect_new_system_custom_types import (  # noqa: E402,F401 - fixtures
    CONN_ARN, SID, _register, cat, no_dns, secrets)

TOKEN = "form-token-SECRET-77"  # nosec B105 - fixture
URL = "https://mcp.example.com/mcp"


def _form_body(token: str | None) -> dict:
    """What the form's buildUrlConnectBody sends."""
    body = {"name": "Quality Lab", "system_type": "Custom", "connection_type": "MCP Server",
            "connection_string": URL}
    if token:
        body["connection_config"] = {"token": token}
    return body


def test_the_form_token_lands_only_in_the_bound_own_secret(cat, secrets):
    res = _register(_form_body(TOKEN))
    assert res.get("status") == "created", res
    assert TOKEN not in json.dumps(res)
    assert TOKEN not in json.dumps(cat.get(SID))
    import asyncio
    import agents.onboard as onboard
    assert TOKEN not in json.dumps(asyncio.run(onboard.onboard_catalog()), default=str)
    assert secrets == {CONN_ARN: {"token": TOKEN, "connection_origin": "user",
                                  "system_id": SID}}


def test_guard_no_token_writes_no_secret(cat, secrets):
    res = _register(_form_body(None))
    assert res.get("status") == "created", res
    assert secrets == {}
