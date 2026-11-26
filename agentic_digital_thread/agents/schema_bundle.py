# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Shared schema *bundle* builder — the single place that turns the knowledge
graph's metadata for a system (via ``graph_tools.get_system_schema``) into a
structured, renderable bundle that both Mode A and Mode B consumers use.

RULES-ENGINE CONSTRAINT: the graph is a rules engine, not a data store. This
builder reads ONLY metadata (columns / types / concept mappings / partition keys)
from the graph; sample VALUES are NEVER read from or written to the graph. Samples,
when included, are merged in from a caller-supplied dict (Mode A provision reads /
Mode B live-samples them in the runtime that holds the DB credentials) and are
capped + PII-redacted before rendering.

The single graph read is the only IO; sample merging is pure. Import-light (only a
lazy ``graph_tools`` import for the graph read) so the module stays offline-testable.
"""

import json


def build_schema_bundle(
    system_id: str,
    sample_values: dict | None = None,
    *,
    include_samples: bool = True,
) -> dict:
    """Build a schema bundle for ``system_id`` from the knowledge graph.

    Reads ``graph_tools.get_system_schema`` (metadata only: columns, column_types,
    concept_map, partition_keys), then — when ``include_samples`` and a
    ``sample_values`` (``{table: {column: [values]}}``) dict is supplied — merges
    capped + PII-redacted samples per non-PII column.

    Returns::

        {
          "system_id": str,
          "tables": {tbl: {columns, column_types, concept_map, partition_keys[, samples]}},
          "rendered": "<format_schema_with_samples output>",
        }

    On a missing/errored graph schema, returns an empty-tables bundle
    (``{"system_id", "tables": {}, "rendered": ""}``). ``samples`` is emitted on a
    table ONLY when ``include_samples`` is True and a sample dict was supplied.
    """
    from . import schema_format
    from .graph_tools import get_system_schema
    from .pii import column_looks_pii

    raw = get_system_schema(system_id)
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        parsed = None
    if not isinstance(parsed, dict) or "error" in parsed:
        return {"system_id": system_id, "tables": {}, "rendered": ""}

    sample_values = sample_values if (include_samples and sample_values) else None

    tables: dict = {}
    table_columns: dict = {}  # {tbl: [(col, type)]} for rendering
    render_samples: dict = {}  # {tbl: {col: [values]}} for rendering
    for tbl, meta in parsed.items():
        if not isinstance(meta, dict):
            continue
        cols = list(meta.get("columns", []) or [])
        col_types = dict(meta.get("column_types", {}) or {})
        entry: dict = {
            "columns": cols,
            "column_types": col_types,
            "concept_map": dict(meta.get("concept_map", {}) or {}),
            "partition_keys": list(meta.get("partition_keys", []) or []),
        }
        table_columns[tbl] = [(c, col_types.get(c, "")) for c in cols]

        if sample_values is not None:
            tbl_in = sample_values.get(tbl) or {}
            samples_out: dict = {}
            for c in cols:
                if column_looks_pii(c):
                    continue  # never surface samples for a PII-named column
                vals = schema_format.cap_redact_sample_list(tbl_in.get(c))
                if vals:
                    samples_out[c] = vals
            entry["samples"] = samples_out
            render_samples[tbl] = samples_out

        tables[tbl] = entry

    rendered = schema_format.format_schema_with_samples(
        table_columns, render_samples if sample_values is not None else None
    )
    return {"system_id": system_id, "tables": tables, "rendered": rendered}


def render_bundle(bundle: dict) -> str:
    """Return the pre-rendered schema string from a bundle (empty string if none)."""
    return (bundle or {}).get("rendered", "") or ""
