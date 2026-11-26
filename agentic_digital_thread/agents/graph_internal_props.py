# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Graph node properties that are operational state, never user- or model-facing data.

A property listed here is read only by the code that writes it. Every reader
that hands whole graph nodes (or their property maps) to a person, a model or
a durable file removes it with :func:`without_internal_properties`: the
knowledge-graph view and node details (``server``), the chat's
``explore_graph`` (``graph_tools``) and graph snapshots (``graph_snapshot``,
export and restore). Stdlib only, no I/O.
"""
from __future__ import annotations

#: The System node's mark while a re-provision rewrites its schema subgraph
#: (``onboard._mark_schema_rewrite`` / ``_finish_schema_rewrite``): epoch ms.
#: Kept beside the same mark in the shared store (``schema_rewrite_state``)
#: until the move's second release stops writing it; a model query looping
#: over ``keys(s)`` can still see it until then.
SCHEMA_REWRITE_PROPERTY = "schema_rewrite_started"
#: The System node's marker of a rewrite that ended before its field concepts
#: were settled (``onboard._finish_schema_rewrite``): epoch ms.
SCHEMA_REWRITE_FAILED_PROPERTY = "schema_rewrite_failed"

INTERNAL_NODE_PROPERTIES = frozenset({SCHEMA_REWRITE_PROPERTY, SCHEMA_REWRITE_FAILED_PROPERTY})


def without_internal_properties(value):
    """``value`` with every key in :data:`INTERNAL_NODE_PROPERTIES` removed
    from every map in it: a serialized node's ``~properties``, a
    ``properties(n)`` or map projection, at any depth of lists and maps.
    Returns new containers; other values pass through. Pure."""
    if isinstance(value, dict):
        return {k: without_internal_properties(v) for k, v in value.items()
                if k not in INTERNAL_NODE_PROPERTIES}
    if isinstance(value, (list, tuple)):
        return type(value)(without_internal_properties(v) for v in value)
    return value
