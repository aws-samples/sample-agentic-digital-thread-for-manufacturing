# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""`onboarding` MCP tool server.

A thin, stateless FastMCP server exposing the onboarding tools as gateway tools —
NOT an LLM agent and NOT an orchestrator (no model, no reasoning loop). It hosts the
SAME tool implementations the in-process onboarding agent uses (imported from
``onboard_agent`` via ``ONBOARD_TOOLS``, the single source of truth), without the
ones the app runs itself in a deployment (``onboard_chat_lifecycle.NOT_ON_GATEWAY``:
``get_catalog``, ``provision_system``, ``reset_system``, ``connect_new_system``,
``store_credentials``). That leaves only explore-tier tools: the deployed
onboarding chat calls them through the onboarding AgentCore Gateway to probe and
discover a system, assess vocabulary fit and preview concept proposals
(read-only, ADR-0068), map fields to concepts, resolve cross-system identities
and verify. ``discover`` stays because the chat forwards a runtime-owned case to
it. No exported tool writes a secret: the gateway accepts any app token and has
no per-tool policy attached, so ``store_credentials`` runs in the app.

Hosted on AgentCore Runtime in MCP mode as a reserved/built-in system
(``SYSTEM_ID=onboarding`` — dispatched in ``runtime_app``), launched in VPC mode so
the tools can reach VPC-private resources (the PostgreSQL/MCP probes, Neptune
Analytics, and the DynamoDB onboarding-session store; ADR-0024/0032). On the gateway
these tools are exposed prefixed as ``onboarding___<tool_name>`` (the prefix derives
from the SYSTEM_ID-based target name).

The runtime/ASGI wiring (``/ping``, ``/invocations`` warmup, the ``/mcp`` vs ``/mcp/``
alias) lives in ``runtime_app._run_onboarding`` — mirroring how
``digital_thread_server.create_digital_thread_server`` pairs with
``runtime_app._run_digital_thread``.

Semantic search OFF for the small, fixed onboarding toolset is a *gateway* setting
(ADR-0032, Task 2.1), not this server's concern; the server just keeps the toolset
small and ordered (explore then commit).
"""

import logging
import os

from mcp.server.fastmcp import FastMCP

from .onboard_agent import ONBOARD_TOOLS
from .onboard_chat_lifecycle import gateway_tools

logger = logging.getLogger(__name__)

# The gateway target name; the gateway prefixes exposed tools as
# ``<TARGET_NAME>___<tool_name>``.
TARGET_NAME = "onboarding"

#: The tools this server exports: ``ONBOARD_TOOLS`` without the ones the app
#: runs itself in a deployment (``onboard_chat_lifecycle.NOT_ON_GATEWAY``,
#: derived from the names the chat's gateway filter hides). The gateway accepts
#: any app token and has no per-tool policy attached yet, so a tool the app
#: answers itself must not be callable on it.
RUNTIME_TOOLS = gateway_tools(ONBOARD_TOOLS)


def _assert_required_env() -> None:
    """Fail CLOSED at server construction when required env is absent (ADR-0069).

    The onboarding runtime shipped for its whole life without
    ``GRAPH_IDENTIFIER``; ``db._neptune_execute`` then used the literal
    ``"adt"`` as the Neptune Analytics endpoint HOSTNAME, every graph read
    returned ``[]`` (the load-bearing degrade contract), and the agent told
    stewards the catalog was empty — false and customer-facing. Raising at
    boot with the key names makes a half-provisioned runtime visible in
    CloudWatch instead of hiding behind that degrade path.

    ``runtime_env_contract.missing_required`` is the ONE validator; skipped
    in local/DEV_MODE (``config.local_mode_env``) where the env is
    intentionally minimal (ADR-0023 — local mode uses in-process tools and
    ``GRAPH_BACKEND=local``).
    """
    from .config import local_mode_env
    if local_mode_env():
        return
    from .runtime_env_contract import missing_required
    missing = missing_required("onboarding", os.environ)
    if missing:
        raise RuntimeError(
            "onboarding runtime env incomplete — missing/empty required "
            f"keys: {', '.join(missing)}. Re-run deploy.sh (§2e rebuilds the "
            "runtime env through the contract), or set AGENT_MODE=local for "
            "local development.")


def create_onboarding_server(host: str = "0.0.0.0", port: int = 8000) -> FastMCP:  # nosec B104 - container service binds all interfaces
    """Build the `onboarding` FastMCP server exposing the gateway onboarding tools.

    Registers exactly :data:`RUNTIME_TOOLS`: ``onboard_agent.ONBOARD_TOOLS``, the
    SINGLE SOURCE OF TRUTH shared with the in-process ``get_onboard_agent``,
    without ``onboard_chat_lifecycle.NOT_ON_GATEWAY``, the tools the app runs
    itself, so the wired agent set, the chat's gateway filter and the
    gateway-exported set cannot drift. Each tool is a Strands
    ``@tool`` object; it is registered under its ``tool_name`` with its docstring as
    the description (same pattern ``system_agent`` uses for its i3X tools) so the
    gateway-prefixed names (``onboarding___<tool>``) and the semantic-search ranking
    text match the in-process tools exactly (no drift). Because only that list
    is iterated, a defined-but-unwired ``@tool`` can never auto-export as an orphaned
    extra tool (F1/F9). Returns the configured ``FastMCP`` instance; the caller mounts
    it (see ``runtime_app._run_onboarding``).

    Cloud construction FAILS CLOSED on an incomplete contract env
    (``_assert_required_env`` — ADR-0069); local/DEV_MODE skips the check.
    """
    _assert_required_env()
    mcp = FastMCP(name=f"adt-{TARGET_NAME}", host=host, port=port, stateless_http=True)

    for t in RUNTIME_TOOLS:
        mcp.tool(name=t.tool_name, description=t.__doc__ or t.tool_name)(t)

    logger.info(
        "onboarding MCP server built: tools=%s",
        [t.tool_name for t in RUNTIME_TOOLS],
    )
    return mcp
