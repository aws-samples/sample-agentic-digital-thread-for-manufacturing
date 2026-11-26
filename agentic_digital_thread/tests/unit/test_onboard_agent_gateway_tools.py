# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""P4-A (C1 resolution): the in-app onboarding agent sources its TOOLS from the
onboarding gateway (MCP client, CUSTOM_JWT) with the forwarded adt-app user bearer
token in cloud mode, and from in-process ``ONBOARD_TOOLS`` in local mode.

Mirrors ``orchestrator._get_orchestrator_tools`` / ``_create_mcp_client`` but with a
bearer JWT instead of SigV4. EXPLICIT mode check — NO silent fallback:

  * local (``AGENT_MODE=local``)         -> in-process ONBOARD_TOOLS (the app's own
                                            provision_system / reset_system in place)
  * cloud + gateway URL + user token     -> single gateway MCP client, bearer header,
                                            then the app's provision_system / reset_system /
                                            connect_new_system
  * cloud + no gateway URL               -> RuntimeError
  * cloud + no user token                -> RuntimeError

Offline: ``MCPClient`` / ``streamablehttp_client`` are spied so nothing dials AWS.
"""

import pytest

import agents.onboard_agent as oa
from agents.config import get_config


@pytest.fixture(autouse=True)
def _clean_config_env(monkeypatch):
    """Each test drives config purely from env — start from a known-clean slate and
    reset the ``lru_cache`` before AND after so ordering never leaks state."""
    for var in ("AGENT_MODE", "DEV_MODE", "ONBOARDING_GATEWAY_URL"):
        monkeypatch.delenv(var, raising=False)
    get_config.cache_clear()
    yield
    get_config.cache_clear()


# ── config surface ────────────────────────────────────────────────────────

def test_has_onboarding_gateway_true_when_url_set(monkeypatch):
    monkeypatch.setenv("ONBOARDING_GATEWAY_URL", "https://gw.example.com/mcp")
    get_config.cache_clear()
    assert get_config().has_onboarding_gateway is True
    assert get_config().onboarding_gateway_url == "https://gw.example.com/mcp"


def test_has_onboarding_gateway_false_when_unset():
    assert get_config().has_onboarding_gateway is False
    assert get_config().onboarding_gateway_url is None


def test_is_local_mode_reflects_agent_mode(monkeypatch):
    assert get_config().is_local_mode is False  # nothing set -> cloud/deployed
    monkeypatch.setenv("AGENT_MODE", "local")
    get_config.cache_clear()
    assert get_config().is_local_mode is True


# ── local mode: in-process tools ───────────────────────────────────────────

def _names(tools):
    return [t.tool_name for t in tools]


def test_local_mode_uses_in_process_tools(monkeypatch):
    import agents.onboard_chat_lifecycle as lifecycle
    monkeypatch.setenv("AGENT_MODE", "local")
    get_config.cache_clear()
    tools = oa._get_onboard_tools(None)
    # The in-process single-source-of-truth list, with the app's own
    # provision_system / reset_system in their place — no MCP client.
    assert tools == lifecycle.in_app_tools(oa.ONBOARD_TOOLS)
    assert _names(tools) == _names(oa.ONBOARD_TOOLS)
    assert not any(isinstance(t, oa.MCPClient) for t in tools)


def test_local_mode_ignores_gateway_url_and_token(monkeypatch):
    # Even with a URL present, an explicit local mode stays in-process.
    monkeypatch.setenv("AGENT_MODE", "local")
    monkeypatch.setenv("ONBOARDING_GATEWAY_URL", "https://gw.example.com/mcp")
    get_config.cache_clear()
    assert _names(oa._get_onboard_tools(None)) == _names(oa.ONBOARD_TOOLS)


# ── cloud mode: gateway MCP client with bearer ─────────────────────────────

def _spy_mcp(monkeypatch, tool_names=("connect_new_system",), probe_error=None):
    """Replace MCPClient + streamablehttp_client with spies; return the capture dict.

    The bearer client is built by the shared ``agents.gateway_auth.bearer_mcp_client``
    helper (DRY seam), so the spies are installed on that module — patching ``oa``
    would no longer intercept the transport factory.

    The spy also implements the EMPTY-TOOLBOX probe contract
    (context-manager + ``list_tools_sync``): ``tool_names`` controls what the
    probe sees; ``probe_error`` makes the probe raise (gateway unreachable).
    """
    import agents.gateway_auth as ga

    captured: dict = {}

    class _SpyMCPClient:
        def __init__(self, transport_callable, **kwargs):
            captured["transport_callable"] = transport_callable
            captured["client_kwargs"] = kwargs

        def __enter__(self):
            if probe_error is not None:
                raise probe_error
            return self

        def __exit__(self, *exc):
            return False

        def list_tools_sync(self):
            return list(tool_names)

    def _spy_streamable(url, headers=None):
        captured["url"] = url
        captured["headers"] = headers
        return ("read", "write", None)

    monkeypatch.setattr(ga, "MCPClient", _SpyMCPClient)
    monkeypatch.setattr(ga, "streamablehttp_client", _spy_streamable)
    return captured, _SpyMCPClient


def test_cloud_mode_builds_gateway_client_with_bearer(monkeypatch):
    monkeypatch.setenv("ONBOARDING_GATEWAY_URL", "https://gw.example.com/mcp")
    get_config.cache_clear()
    captured, SpyMCPClient = _spy_mcp(monkeypatch)

    tools = oa._get_onboard_tools("user-token-123")

    # A single MCP client bound to the gateway — mirrors the orchestrator's
    # single-gateway-client tool list, then the app's own tools.
    assert len(tools) == 7
    assert isinstance(tools[0], SpyMCPClient)
    assert _names(tools[1:]) == ["get_catalog", "discover", "provision_system",
                                 "reset_system", "connect_new_system", "store_credentials"]

    # Invoke the transport factory (as Strands would) to assert the bearer is
    # forwarded as the Authorization header on the streamable-HTTP transport.
    captured["transport_callable"]()
    assert captured["url"] == "https://gw.example.com/mcp"
    assert captured["headers"] == {"Authorization": "Bearer user-token-123"}


def test_cloud_mode_missing_url_raises_no_fallback(monkeypatch):
    # Cloud mode (AGENT_MODE unset) with no gateway URL must RAISE — never fall
    # back to in-process tools.
    with pytest.raises(RuntimeError, match="ONBOARDING_GATEWAY_URL"):
        oa._get_onboard_tools("user-token-123")


def test_cloud_mode_empty_toolbox_raises_never_runs_toolless(monkeypatch):
    """FAIL-CLOSED fabrication guard: zero gateway tools must RAISE.

    A broken gateway target (SYNCHRONIZE_UNSUCCESSFUL) advertises zero tools;
    a tool-less model ROLE-PLAYS its tools and fabricates catalog answers
    (observed live). The agent must never be constructed in that state.
    """
    monkeypatch.setenv("ONBOARDING_GATEWAY_URL", "https://gw.example.com/mcp")
    get_config.cache_clear()
    _spy_mcp(monkeypatch, tool_names=())

    with pytest.raises(RuntimeError, match="ZERO tools"):
        oa._get_onboard_tools("user-token-123")


def test_cloud_mode_unreachable_gateway_probe_raises_honestly(monkeypatch):
    monkeypatch.setenv("ONBOARDING_GATEWAY_URL", "https://gw.example.com/mcp")
    get_config.cache_clear()
    _spy_mcp(monkeypatch, probe_error=ConnectionError("boom"))

    with pytest.raises(RuntimeError, match="tools are unavailable"):
        oa._get_onboard_tools("user-token-123")


def test_cloud_mode_missing_token_raises_no_anonymous_call(monkeypatch):
    monkeypatch.setenv("ONBOARDING_GATEWAY_URL", "https://gw.example.com/mcp")
    get_config.cache_clear()
    with pytest.raises(RuntimeError, match="user access token"):
        oa._get_onboard_tools(None)


# ── get_onboard_agent threads the token through ────────────────────────────

def test_get_onboard_agent_forwards_token(monkeypatch):
    seen: dict = {}

    def _fake_tools(token):
        seen["token"] = token
        return []  # empty toolset keeps Agent construction cheap/offline

    monkeypatch.setattr(oa, "_get_onboard_tools", _fake_tools)
    oa.get_onboard_agent(user_access_token="tok-abc")  # nosec B106 - offline test fixture/placeholder credential, not a live secret
    assert seen["token"] == "tok-abc"


def test_get_onboard_agent_defaults_token_none(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(oa, "_get_onboard_tools", lambda token: seen.setdefault("token", token) or [])
    oa.get_onboard_agent()
    assert seen["token"] is None
