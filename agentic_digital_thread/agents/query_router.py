# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Protocol-aware query dispatcher for cross-system entity resolution.

Routes queries to the appropriate backend based on a system's connection_type.
Used by resolve.py and provision validation — NOT by system agents (they have
their own tools).
"""

import json
import logging

from .catalog import catalog
from .connection_resolver import resolve_connection
from .db import query_pg_bounded as pg
from .response_integrity_query import (
    MAX_QUERY_RESULT_BYTES, bounded_query_result,
)

logger = logging.getLogger(__name__)


def query_system(system_id: str, sql: str, params=None) -> dict:
    """Query a system using its native protocol.

    Args:
        system_id: The system to query.
        sql: SQL query (for PostgreSQL) or GET path (for OpenAPI)
             or natural language (for MCP).
        params: Query parameters (PostgreSQL only).

    Returns:
        dict with {columns, rows, row_count}. Empty result on error.
    """
    entry = catalog.get(system_id)
    if not entry:
        logger.warning("query_system: system %s not found", system_id)
        return {"columns": [], "rows": [], "row_count": 0}

    conn_type = entry.get("connection_type", "PostgreSQL")
    schema = entry.get("schema", "")

    try:
        if conn_type == "PostgreSQL":
            return pg(schema, sql, params)
        elif conn_type == "REST API":
            return _query_openapi(system_id, sql)
        elif conn_type == "MCP Server":
            return _query_mcp(system_id, sql)
        else:
            logger.warning("query_system: unsupported connection_type %s", conn_type)
            return {"columns": [], "rows": [], "row_count": 0}
    except Exception:
        logger.warning("query_system failed: system=%s", system_id)
        return {"columns": [], "rows": [], "row_count": 0}


def _openapi_target(system_id: str, request: str) -> tuple[str, dict[str, str]]:
    resolved = resolve_connection(system_id)
    base_url = (
        resolved.get("base_url") or resolved.get("url")
        or resolved.get("connection_string", "")
    ).rstrip("/")
    headers: dict[str, str] = {}
    # The resolver persists/returns ``auth_scheme`` (bearer|bearer_literal|api_key|
    # basic); fall back to the legacy ``auth_type`` spelling for older nodes. The
    # credential is ``token`` (``key`` accepted as the legacy api_key alias).
    token = resolved.get("token", "") or resolved.get("key", "")
    scheme = resolved.get("auth_scheme") or resolved.get("auth_type") or "none"
    if token:
        if scheme == "bearer_literal":
            headers["Authorization"] = token
        elif scheme == "basic":
            # token is the pre-encoded base64 of ``apiKey:secret`` (HTTP Basic).
            headers["Authorization"] = f"Basic {token}"
        elif scheme == "api_key":
            headers[resolved.get("header_name", "X-API-Key")] = token
        else:  # "bearer" (and unknown → default to Bearer)
            headers["Authorization"] = f"Bearer {token}"
    parts = request.strip().split(None, 1)
    path = parts[1] if len(parts) == 2 else parts[0]
    return f"{base_url}/{path.lstrip('/')}", headers


def _openapi_result(data) -> dict:
    if isinstance(data, list) and data:
        columns = list(data[0].keys()) if isinstance(data[0], dict) else ["value"]
        rows = [list(item.values()) if isinstance(item, dict) else [item] for item in data]
    elif isinstance(data, dict):
        columns, rows = list(data.keys()), [list(data.values())]
    else:
        columns, rows = ["result"], [[str(data)]]
    return bounded_query_result(columns, rows, truncated=False)


def _query_openapi(system_id: str, request: str) -> dict:
    """Execute a stream-capped GET request against a resolved REST API, on the
    address the SSRF guard validates for it (``pinned_http``)."""
    from . import pinned_http
    from .response_integrity_query import read_http_body_capped

    url, headers = _openapi_target(system_id, request)
    with pinned_http.client(connection_type="REST API", timeout=30.0) as client:
        with client.stream("GET", url, headers=headers) as response:
            response.raise_for_status()
            raw_body, truncated = read_http_body_capped(response)
            content_type = response.headers.get("content-type", "")
    if truncated:
        return bounded_query_result(
            ["result"], [[raw_body.decode("utf-8", "replace")]], truncated=True,
            reason="response_limit",
        )
    if content_type.startswith("application/json"):
        data = json.loads(
            raw_body,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON constant {value}")
            ),
        )
    else:
        data = {"raw": raw_body.decode("utf-8", "replace")}
    return _openapi_result(data)


def _query_mcp(system_id: str, query: str) -> dict:
    """Invoke an MCP tool with a query."""
    import asyncio
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    # Source the live MCP endpoint from the resolver, not the catalog snapshot.
    resolved = resolve_connection(system_id)
    mcp_url = resolved.get("url") or resolved.get("connection_string", "")

    from .response_integrity_query import capped_mcp_http_client_factory

    async def _invoke():
        async with streamablehttp_client(
            mcp_url, httpx_client_factory=capped_mcp_http_client_factory,
        ) as (rs, ws, _):
            async with ClientSession(rs, ws) as session:
                await session.initialize()
                tools = await session.list_tools()
                if not tools.tools:
                    return {"columns": [], "rows": [], "row_count": 0}
                # Use the first tool
                tool = tools.tools[0]
                result = await session.call_tool(tool.name, {"query": query})
                chunks: list[str] = []
                used = 0
                truncated = False
                for item in getattr(result, "content", None) or []:
                    chunk = item.text if hasattr(item, "text") else str(item)
                    encoded = chunk.encode("utf-8", "replace")
                    remaining = MAX_QUERY_RESULT_BYTES - used
                    if remaining <= 0 or len(encoded) > remaining:
                        truncated = True
                        break
                    chunks.append(chunk)
                    used += len(encoded)
                text = "".join(chunks)
                return _mcp_content_result(text, truncated)

    return asyncio.run(_invoke())


def _mcp_content_result(text: str, truncated: bool) -> dict:
    """Shape an MCP tool's text content into the bounded result.

    ``truncated`` means the engine dropped content past the byte cap: a
    ``response_limit`` cut with no row order. A tool's own ``truncated`` flag
    on parsed rows is ``source_partial``: its meaning is the tool's, so the
    note says only that the source marked it partial.
    """
    try:
        data = json.loads(text)
        if isinstance(data, dict) and isinstance(data.get("rows"), list):
            columns = data.get("columns") if isinstance(data.get("columns"), list) else []
            reason = ("response_limit" if truncated
                      else "source_partial" if data.get("truncated") else "")
            return bounded_query_result(
                columns, data["rows"], truncated or bool(data.get("truncated")),
                reason=reason)
    except (json.JSONDecodeError, TypeError):
        pass
    return bounded_query_result(["result"], [[text]], truncated,
                                reason="response_limit")
