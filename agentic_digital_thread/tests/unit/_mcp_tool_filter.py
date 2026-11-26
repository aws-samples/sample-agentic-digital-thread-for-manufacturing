# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Ask an MCPClient whether its tool filters keep a tool.

strands-agents 1.48 moved the filter check from ``_should_include_tool(tool)``
to ``_should_include_tool_with_filters(tool, filters)``. Production code only
passes ``tool_filters=`` to the constructor, which both versions honour, so the
tests that pin the gateway's hidden tools go through this one seam.
"""
from __future__ import annotations


def client_keeps(client, tool) -> bool:
    """True when the client's constructor ``tool_filters`` keep ``tool``."""
    with_filters = getattr(client, "_should_include_tool_with_filters", None)
    if with_filters is not None:
        return with_filters(tool, client._tool_filters)
    return client._should_include_tool(tool)
