# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Canonical knowledge-graph tool implementations.

Single source of truth for the two knowledge-graph tools the digital thread exposes:
``explore_graph`` and ``get_system_schema``. Both the in-process orchestrator
(``orchestrator.py``, wrapped with the Strands ``@tool`` decorator) and the
``digital-thread`` FastMCP tool server (``digital_thread_server.py``, registered as
gateway tools) import these functions so the Cypher AND the docstrings — which double
as the tool descriptions / semantic-search ranking text on the gateway — never drift
between the two surfaces.

These are plain functions on purpose: the docstring and signature are applied by
whichever decorator the consumer uses (Strands ``tool`` or FastMCP ``mcp.tool``), so
the description travels with the function to both runtimes.
"""

import json

from . import cypher_read_only as _read_only
from . import explore_inactive_joins as _inactive_joins
from .container_note import NOTE as CONTAINER_NOTE, container_contents
from .cypher_union_check import dotted_alias, names_property, union_column_mismatch, uses_regex_match
from .db import GraphQueryError, neptune_query, neptune_query_checked
from .graph_internal_props import INTERNAL_NODE_PROPERTIES, without_internal_properties


def explore_graph(cypher: str) -> str:
    """Explore the manufacturing knowledge graph with an openCypher query.

    GRAPH SCHEMA:
    Node types: Plant, System, SchemaEntry, Field, Concept, IdentityRule, JoinPath, ValueMapping,
    Asset, AssetModel, AssetModelRole, Correlation, AuthorityRule, DecisionBoundary, OnboardingLog.

    Edge types:
    - System -[:HAS_SCHEMA]-> SchemaEntry -[:HAS_FIELD]-> Field -[:MAPS_TO]-> Concept
    - System -[:LOCATED_AT]-> Plant
    - Asset -[:HAS_PARENT]-> Asset (child→parent; sources nest their own way, so depth
      does not say what an asset is: Asset.role does — site, area, work_center,
      work_unit, equipment, equipment_part, data_stream or unclassified, which may still be equipment)
    - Asset -[:INSTANCE_OF]-> AssetModel (the asset's equipment model/type)
    - Asset -[:FROM_SYSTEM]-> System
    - ValueMapping -[:FROM_SYSTEM]-> System, -[:TO_SYSTEM]-> System, -[:LINKS_CONCEPT]-> Concept
    - JoinPath -[:FROM_SYSTEM]-> System, -[:TO_SYSTEM]-> System
    - IdentityRule -[:LINKS_CONCEPT]-> Concept
    - AuthorityRule -[:APPLIES_TO]-> System
{rule_lifecycle}
    Returns empty results (not an error) when no matching nodes/edges exist. In
    that case the response also carries ``empty: true``, a ``graph_node_types``
    inventory (the node labels that DO exist in the graph, with counts), and a
    ``note`` telling you not to broaden or retry — if a label you searched for is
    absent from ``graph_node_types`` it does not exist in this graph.

    A query the graph engine REJECTS returns ``query_failed: true`` and the
    engine's ``error`` instead. That is not an empty result and says nothing
    about what the graph holds: correct the query and run it again.

    Every ``UNION`` branch must RETURN the same columns in the same order, each
    aliased with the same plain name (``RETURN s.id AS id, s.name AS name``).
    ``=~`` is not supported (use ``toLower(x) CONTAINS 'text'``, ``STARTS WITH``
    or ``ENDS WITH``) and an alias must be a plain name (``AS md_id``, never
    ``AS md.id``). A query breaking any of these is refused before it runs,
    with the reason (and a UNION's per-branch names) in ``error``.

    This tool only reads the graph. A query using CREATE, MERGE, SET, REMOVE,
    DELETE, DETACH, DROP, CALL, LOAD CSV or FOREACH (anywhere outside a string
    or a comment) is refused before it runs.

    Args:
        cypher: openCypher query string.
    """
    refusal = _presubmit_refusal(cypher)
    if refusal is not None:
        return refusal
    try:
        results = without_internal_properties(neptune_query_checked(cypher))
    except GraphQueryError as exc:
        return _rejected_query_result(exc)
    if not results:
        return _empty_graph_result()
    return _served_results_envelope(cypher, results)


#: The rule-lifecycle contract in the tool description. The description is
#: the docstring; this paragraph fills its ``{rule_lifecycle}`` slot (after the
#: edge list) here, so the function itself stays short.
_RULE_LIFECYCLE_DESCRIPTION = """
    Rule lifecycle: an IdentityRule or JoinPath whose status is 'deactivated' or
    'retired' is switched off (kept for audit; it is not a join or an identity
    translation), and deactivating an IdentityRule sets status 'deactivated' on
    the JoinPaths made from it. Exclude them in the query, with x the node's
    variable: WHERE coalesce(x.status, '') <> 'deactivated' AND
    coalesce(x.status, '') <> 'retired'. A result row that returns a switched-off
    JoinPath or IdentityRule (its node or its exact id) is withheld, a list item
    holding one is dropped, and ``inactive_join_paths`` /
    ``inactive_identity_rules`` name them. Counts, aggregates and other
    projections are returned as the query computes them, so they include
    switched-off rules unless the query filters on status.
"""
explore_graph.__doc__ = (explore_graph.__doc__ or "").replace(
    "{rule_lifecycle}", _RULE_LIFECYCLE_DESCRIPTION, 1)

def _served_results_envelope(cypher: str, results: list) -> str:
    """:func:`_results_envelope` for the rows left once switched-off
    JoinPaths and IdentityRules are withheld by exact identity
    (:func:`explore_inactive_joins.withhold`); withheld rows are never
    reported as an empty graph."""
    results, inactive = _inactive_joins.withhold(cypher, results, neptune_query_checked)
    if not results:
        return json.dumps({"results": [], **inactive}, default=str)
    return _results_envelope(results, inactive)


def _results_envelope(results: list, extra: "dict | None" = None) -> str:
    """A non-empty result, plus ``inside_containers`` (with its note) when a
    row is a container asset (agents/container_note.py), plus ``extra`` (the
    switched-off JoinPaths and IdentityRules block, when there is one)."""
    inside = container_contents(results, neptune_query)
    out: dict = {"results": results}
    if inside:
        out.update({"inside_containers": inside, "inside_containers_note": CONTAINER_NOTE})
    out.update(extra or {})
    return json.dumps(out, default=str)


_REJECTED_NOTE = (
    "The graph engine rejected this openCypher, so the result says nothing about "
    "what the graph contains. This is NOT an empty result. Correct the query "
    "against the GRAPH SCHEMA and run it again. The graph holds system metadata "
    "and rules only, never business records: rows such as orders, shipments or "
    "batches come from the owning system's agent."
)
_EMPTY_NOTE = (
    "No nodes/edges matched this query. The graph contains ONLY the node types "
    "listed in graph_node_types. If a type you searched for is absent from that "
    "list, it does not exist in this graph — do NOT broaden or retry the search "
    "for it."
)


def _rejected_query_result(exc: GraphQueryError) -> str:
    """The failure envelope for a query the engine rejected. ``error`` leads so
    the result HEAD carries the failure marker (query_intelligence.failure_marker
    and the UI classifier): a rejected query is a failed call, never a grounded
    empty one."""
    return json.dumps(
        {
            "error": f"Graph query failed ({exc.category}): {exc.detail}",
            "results": [],
            "query_failed": True,
            "note": _REJECTED_NOTE,
        },
        default=str,
    )


_REGEX_MATCH_ERROR = (
    "Graph query failed (regular-expression match is not supported, not run): use "
    "toLower(x) CONTAINS 'text', STARTS WITH or ENDS WITH instead of =~."
)
_DOTTED_ALIAS_ERROR = (
    "Graph query failed (dotted alias, not run): an alias must be a plain name, "
    "e.g. AS md_id, not AS md.id."
)
_INTERNAL_PROPERTY_ERROR = (
    "Graph query failed (internal property, not run): {name} is the app's own "
    "provisioning state, not part of the graph's data. Leave it out of the query."
)


def _refused_result(error: str, **extra: object) -> str:
    """The failure envelope for a query refused before submit: same shape as
    an engine rejection (``error`` leads so the result HEAD carries the
    failure marker), plus any check-specific detail."""
    return json.dumps(
        {"error": error, "results": [], "query_failed": True, **extra,
         "note": _REJECTED_NOTE},
        default=str,
    )


def _union_mismatch_result(columns: list[list[str]]) -> str:
    """The failure envelope for a UNION refused before submit, with each
    branch's RETURN names spelled out."""
    branches = "; ".join(
        f"branch {i} returns [{', '.join(names)}]" for i, names in enumerate(columns, 1)
    )
    return _refused_result(
        f"Graph query failed (UNION column mismatch, not run): {branches}. "
        "Every UNION branch must RETURN the same column names in the same "
        "order; alias every column with the same plain name in every branch "
        "(e.g. RETURN a.name AS name in each branch).",
        union_columns=columns,
    )


def _read_only_error(keyword: str) -> str:
    """The refusal text for a query that is not a plain read."""
    if keyword == _read_only.UNREADABLE:
        return (
            "Graph query failed (could not be checked as read-only, not run): "
            "close every string, backtick name and /* */ comment, then run it again."
        )
    return (
        "Graph query failed (read-only, not run): graph exploration only reads "
        f"the graph, and {keyword} can change it or reach outside a read. Read "
        "with MATCH, OPTIONAL MATCH, WHERE, WITH, UNWIND and RETURN."
    )


def _presubmit_refusal(cypher: str) -> str | None:
    """The failure envelope for a query that must not or would never run, or
    ``None`` to submit it unchanged. The read-only check comes first and fails
    CLOSED (``cypher_read_only``). A query naming an internal node property
    is refused next, whatever its spelling (``names_property`` fails closed):
    its value is the graph's operational state, never data, and a scalar
    projection of it would reach the model under another column name. The
    shape checks after those refuse only a confident match and fail open on
    an uncertain parse (``cypher_union_check``)."""
    keyword = _read_only.write_clause(cypher)
    if keyword is not None:
        return _refused_result(_read_only_error(keyword))
    internal = names_property(cypher, INTERNAL_NODE_PROPERTIES)
    if internal is not None:
        return _refused_result(_INTERNAL_PROPERTY_ERROR.format(name=internal))
    if uses_regex_match(cypher):
        return _refused_result(_REGEX_MATCH_ERROR)
    if dotted_alias(cypher) is not None:
        return _refused_result(_DOTTED_ALIAS_ERROR)
    mismatch = union_column_mismatch(cypher)
    if mismatch is not None:
        return _union_mismatch_result(mismatch)
    return None


def _empty_graph_result() -> str:
    """Data-driven "why empty": one inventory query returns the node-type census
    so the model can conclude "that type doesn't exist here" from evidence
    instead of blindly broadening the query 3-4 more times. No label names are
    hardcoded — the inventory is read live from the graph."""
    inventory = neptune_query(
        "MATCH (n) UNWIND labels(n) AS label "
        "RETURN label, count(*) AS count ORDER BY count DESC"
    )
    return json.dumps(
        {"results": [], "empty": True, "graph_node_types": inventory, "note": _EMPTY_NOTE},
        default=str,
    )


def _schema_query(system_id: str) -> list[dict]:
    return neptune_query(
        "MATCH (s:System {id: $sid})-[:HAS_SCHEMA]->(se:SchemaEntry)-[:HAS_FIELD]->(f:Field) "
        "OPTIONAL MATCH (f)-[r:MAPS_TO]->(c:Concept) "
        "RETURN se.name AS table_name, se.relation_kind AS relation_kind, f.name AS field_name, "
        "c.id AS concept, r.role AS role, f.disambiguation AS disambiguation, "
        "f.is_partition AS is_partition, f.data_type AS data_type, "
        "se.discovery_complete AS discovery_complete, se.discovered_columns AS discovered_columns "
        "ORDER BY se.name, f.name",
        {"sid": system_id},
    )


def _append_schema_row(schema: dict, row: dict) -> None:
    table, field = row.get("table_name", ""), row.get("field_name", "")
    if not table or not field:
        return
    if table not in schema:
        kind = str(row.get("relation_kind") or "unknown").strip().lower().replace(" ", "_")
        schema[table] = {"columns": [], "concept_map": {}, "relation_kind": kind}
    record = schema[table]
    if row.get("discovery_complete") is not None and "discovery" not in record:
        # ADR-0077 D3: only a table that RECORDED the fact carries the block.
        record["discovery"] = {"complete": bool(row["discovery_complete"]),
                               "discovered_columns": int(row.get("discovered_columns") or 0)}
    if field not in record["columns"]:
        record["columns"].append(field)
    if row.get("data_type"):
        record.setdefault("column_types", {})[field] = row["data_type"]
    if row.get("concept"):
        record["concept_map"][field] = {"concept": row["concept"], "role": row.get("role")}
    if row.get("disambiguation"):
        record["concept_map"].setdefault(field, {})["note"] = row["disambiguation"]
    if row.get("is_partition"):
        keys = record.setdefault("partition_keys", [])
        if field not in keys:
            keys.append(field)


def _fallback_schema_rows(system_id: str) -> list[dict]:
    try:
        return neptune_query(
            "MATCH (s:System {id: $sid})-[:HAS_SCHEMA]->(se:SchemaEntry)-[:HAS_FIELD]->(f:Field) "
            "RETURN se.name AS table_name, se.relation_kind AS relation_kind, f.name AS field_name "
            "ORDER BY se.name, f.name", {"sid": system_id},
        )
    except Exception:
        return []


def get_system_schema(system_id: str) -> str:
    """Get exact table and column names for a system from the knowledge graph.

    ALWAYS call this before querying any system agent. It returns precise
    columns and relation provenance stored during onboarding, so callers never
    guess SQL lineage. Missing provenance is returned as ``unknown``. A
    per-table ``discovery`` block says whether onboarding's physical discovery
    was complete (ADR-0077 D3) — a table without it never recorded the fact.
    """
    schema: dict[str, dict] = {}
    for row in _schema_query(system_id):
        _append_schema_row(schema, row)
    if not schema:
        for row in _fallback_schema_rows(system_id):
            _append_schema_row(schema, row)
    if not schema:
        return json.dumps({"error": f"No schema found for system '{system_id}'. Is it onboarded?"})
    return json.dumps(schema, default=str)


_VALUE_MAPPING_QUERY = (
    "MATCH (r:ValueMapping) WHERE r.from_system = $source_system "
    "AND r.from_field = $source_field AND r.from_value = $source_identifier "
    "AND r.to_system = $target_system AND r.to_field = $target_field "
    "AND r.to_value = $target_identifier RETURN r.id AS graph_record_id, "
    "r.from_system AS source_system, r.from_value AS source_identifier, "
    "r.from_field AS source_field, r.to_system AS target_system, "
    "r.to_value AS target_identifier, r.to_field AS target_field, "
    "r.mapping_semantics AS mapping_semantics, r.transform AS stored_transform LIMIT 2"
)
_PROOF_FIELDS = (
    "graph_record_id", "source_system", "source_identifier", "source_field",
    "target_system", "target_identifier", "target_field",
)


def _relationship_params(values: tuple[str, ...]) -> dict[str, str] | None:
    if any(not isinstance(value, str) or not value or len(value) > 300 for value in values):
        return None
    keys = (
        "source_system", "source_identifier", "source_field",
        "target_system", "target_identifier", "target_field",
    )
    return dict(zip(keys, values))


def _stored_value_mapping(row: object, params: dict[str, str]) -> tuple[dict | None, str]:
    if not isinstance(row, dict) or any(not isinstance(row.get(key), str) or not row[key] for key in _PROOF_FIELDS):
        return None, "malformed_stored_record"
    for key in _PROOF_FIELDS[1:]:
        if row[key] != params[key]:
            return None, "stored_endpoint_mismatch"
    semantics = str(row.get("mapping_semantics") or "explicit_pair").lower()
    transform = str(row.get("stored_transform") or "").strip().lower()
    if semantics not in {"explicit_pair", "exact", "equality", "identity"} or transform not in {"", "identity", "exact"}:
        return None, "unsupported_mapping_semantics"
    proof = {key: row[key] for key in _PROOF_FIELDS}
    proof.update(relationship_type="value_mapping", direction="outbound")
    return proof, ""


def verify_relationship(
    source_system: str,
    source_identifier: str,
    source_field: str,
    target_system: str,
    target_identifier: str,
    target_field: str,
    relationship_type: str,
) -> str:
    """Mint identity proof only from one exact stored ValueMapping record."""
    if relationship_type != "value_mapping":
        return json.dumps({"verified": False, "records": [], "reason": "relationship_type_not_allowed"})
    values = (
        source_system, source_identifier, source_field,
        target_system, target_identifier, target_field,
    )
    params = _relationship_params(values)
    if params is None:
        return json.dumps({"verified": False, "records": [], "reason": "invalid_endpoint"})
    rows = neptune_query(_VALUE_MAPPING_QUERY, params) or []
    if len(rows) != 1:
        reason = "not_found" if not rows else "ambiguous"
        return json.dumps({"verified": False, "records": [], "reason": reason})
    proof, reason = _stored_value_mapping(rows[0], params)
    if proof is None:
        return json.dumps({"verified": False, "records": [], "reason": reason})
    return json.dumps({"verified": True, "records": [proof]}, separators=(",", ":"))
