# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Shared gateway-auth helpers (P4-A / P5 fix pass).

Two small primitives used by the NEW CUSTOM_JWT gateway code paths:

* :func:`mint_client_credentials_token` — mint an OAuth2 ``client_credentials``
  access token from a Cognito token endpoint using **default TLS verification**.
* :func:`bearer_mcp_client` — build a streamable-HTTP :class:`MCPClient` that
  sends ``Authorization: Bearer <token>``.

DRY seam: the orchestrator's adt-app-service token mint
(:func:`agents.orchestrator._fetch_service_token`) and the onboarding agent's
gateway client (:func:`agents.onboard_agent._create_onboarding_gateway_client`)
both consume these instead of hand-rolling the same logic.

NOT used by ``agent_manager.py`` — its live M2M provisioning path hand-rolls its
own ``urllib`` calls, but (like this helper) now uses **default TLS verification**
(``ssl.create_default_context()`` with no ``CERT_NONE`` bypass).
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request

from mcp.client.streamable_http import streamablehttp_client
from strands.tools.mcp import MCPClient


def mint_client_credentials_token(
    *,
    token_url: str,
    client_id: str,
    client_secret: str,
    scope: str,
    timeout: int = 10,
) -> tuple[str, int]:
    """Mint an OAuth2 ``client_credentials`` access token (default TLS).

    Returns ``(access_token, expires_in_seconds)``. Uses the process/library
    default TLS context — certificate verification is ON (matching
    ``agent_manager``'s hand-rolled M2M path, which also uses default-verified
    SSL). The token is never logged by this function; callers must keep it off
    logs/memory (R4).
    """
    data = urllib.parse.urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": scope,
        }
    ).encode()
    req = urllib.request.Request(  # noqa: S310 - trusted internal Cognito token endpoint
        token_url,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:  # nosec B310 - trusted internal Cognito token endpoint  # nosemgrep: dynamic-urllib-use-detected
        payload = json.loads(r.read())
    return payload["access_token"], int(payload.get("expires_in", 3600))


def bearer_mcp_client(mcp_url: str, bearer_token: str, *, tool_filters=None,
                      startup_timeout: "int | None" = None) -> MCPClient:
    """Build a streamable-HTTP :class:`MCPClient` with a ``Bearer`` auth header.

    The token is passed only in the request headers — never logged, never echoed
    (R4 / credential guardrails). Used by both the orchestrator (query gateway,
    CUSTOM_JWT) and the onboarding agent (onboarding gateway, CUSTOM_JWT).
    ``tool_filters`` (Strands ``ToolFilters``) leaves out the gateway tools it
    rejects; without it every tool the gateway lists is loaded.
    ``startup_timeout`` (seconds) bounds the session start; without it the
    MCPClient default applies.
    """
    headers = {"Authorization": f"Bearer {bearer_token}"}
    transport = lambda url=mcp_url: streamablehttp_client(url, headers=headers)  # noqa: E731
    kwargs = {}
    if tool_filters is not None:
        kwargs["tool_filters"] = tool_filters
    if startup_timeout is not None:
        kwargs["startup_timeout"] = startup_timeout
    return MCPClient(transport, **kwargs)
