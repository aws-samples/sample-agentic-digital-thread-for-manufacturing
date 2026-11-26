# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Operational graph snapshot — full JSON export + wipe-and-restore import.

This is an OPERATIONAL backup/restore tool for the knowledge graph: a steward
exports the ENTIRE graph (every node label including the ``Field``/``Property``
leaves the /api/neptune/graph view deliberately excludes, every edge, all
properties) to a JSON file, and can later restore that file into the SAME
graph (wipe-then-recreate, restore-in-place). Use cases: a safety net around
destructive onboarding experiments, and measuring how closely a fresh
re-onboarding reproduces a matured graph (via the offline diff in
``scripts/graph_snapshot.py``).

This tool is DISTINCT from the ADR-0057 ontology/semantic-layer export, which
remains deliberately not-implemented — that export is a curated semantic
boundary; this one is a raw, label-agnostic operational copy with no semantic
filtering whatsoever.

Design constraints honoured here:
- Module top-level is stdlib-only; the graph client (``agents.db``) is
  imported lazily inside the query/write resolvers so the module loads (and
  the pure helpers work) standalone — the CLI loads it via importlib.
- ``os.environ`` is never mutated.
- Every query is BOUNDED: export pages with SKIP/LIMIT, the wipe deletes in
  repeated ``LIMIT k`` batches, and import creates rows in bounded UNWIND
  chunks. No single unbounded query is ever issued.
- Edges are wired by FILE-LOCAL positional node indexes (``from``/``to`` are
  indexes into the ``nodes`` array), never by assuming an ``id`` property —
  the graph's internal ids never land in the snapshot file.
- Import is id-agnostic and two-phase: nodes are created with a temporary
  ``_import_key`` property, edges are created by MATCHing ``_import_key``
  pairs, then every ``_import_key`` is removed (in bounded batches).
- Labels and edge types from a snapshot file are UNTRUSTED input: they are
  interpolated into openCypher text, so they must match ``_NAME_RE`` or the
  import refuses (``validate_snapshot``).

Consistency note: export paginates with SKIP/LIMIT over a live graph — a
concurrent writer can shift pages. This is an operational tool for quiesced
maintenance windows, not a transactional dump.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def _mutates_value_mappings(fn):
    """``stored_value_mapping_memo.mutates_value_mappings`` (a wipe or an
    import replaces every ValueMapping). Loaded standalone outside the
    package (``scripts/graph_snapshot.py``), there is no memo in that
    process, so the function is left as it is."""
    try:
        from .stored_value_mapping_memo import mutates_value_mappings
    except ImportError:
        return fn
    return mutates_value_mappings(fn)

#: Snapshot file format identifier. Import refuses any other value.
FORMAT_VERSION = "adt-graph-snapshot/1"

#: Rows per export page (SKIP/LIMIT window).
DEFAULT_PAGE_SIZE = 500
#: Nodes per wipe / _import_key-removal batch.
DEFAULT_WIPE_BATCH = 500
#: Rows per UNWIND create batch on import.
DEFAULT_IMPORT_BATCH = 200
#: Convergence guard for the bounded wipe/remove loops.
_MAX_BATCH_ITERATIONS = 100_000

#: Safe openCypher identifier — node labels and edge types are interpolated
#: into query text, so anything else is refused at validation time.
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_TEMP_KEY = "_import_key"


def _graph_query():
    """Default graph reader (lazy import — keeps module top-level stdlib-only)."""
    from agents.db import neptune_query
    return neptune_query


def _graph_write():
    """Default graph writer (lazy import — keeps module top-level stdlib-only)."""
    from agents.db import neptune_write
    return neptune_write


def without_internal_properties(props: dict) -> dict:
    """``props`` without the operational node properties a snapshot never
    carries (``agents.graph_internal_props``; lazy import, keeps the module
    top-level stdlib-only): neither exported nor restored."""
    from agents.graph_internal_props import without_internal_properties as strip
    return strip(props)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Export ───────────────────────────────────────────────────────────────────

def _node_page(query, skip: int, limit: int) -> list[dict]:
    """One bounded page of nodes: [{gid, labels, props}] (gid = internal id).

    Both backends serialize a returned node as
    ``{"~id":..., "~labels":[...], "~properties":{...}}`` (see db._to_plain).
    """
    rows = query(
        f"MATCH (n) RETURN n ORDER BY id(n) SKIP {int(skip)} LIMIT {int(limit)}"
    )
    items = []
    for row in rows or []:
        node = row.get("n") or {}
        items.append({
            "gid": str(node.get("~id", "")),
            "labels": sorted(node.get("~labels", []) or []),
            "props": without_internal_properties(dict(node.get("~properties", {}) or {})),
        })
    return items


def _edge_page(query, skip: int, limit: int) -> list[dict]:
    """One bounded page of edges: [{type, from_gid, to_gid, props}]."""
    rows = query(
        "MATCH (a)-[r]->(b) RETURN id(a) AS from_gid, id(b) AS to_gid, r "
        f"ORDER BY id(r) SKIP {int(skip)} LIMIT {int(limit)}"
    )
    items = []
    for row in rows or []:
        rel = row.get("r") or {}
        items.append({
            "type": str(rel.get("~type", "")),
            "from_gid": str(row.get("from_gid", "")),
            "to_gid": str(row.get("to_gid", "")),
            "props": dict(rel.get("~properties", {}) or {}),
        })
    return items


def export_chunk(cursor: str = "", query=None,
                 page_size: int = DEFAULT_PAGE_SIZE) -> dict:
    """One bounded snapshot chunk for the paginated HTTP route.

    Cursor grammar: ``""`` (start) | ``nodes:<skip>`` | ``edges:<skip>``.
    Returns ``{"format", "kind", "items", "next_cursor"}`` where
    ``next_cursor`` is ``None`` when the walk is complete. Node items carry
    the graph-internal ``gid`` so the CLIENT can wire edges to positional
    indexes when assembling the file (``assemble_snapshot``); gids never land
    in the final snapshot file. Raises ``ValueError`` on a malformed cursor.
    """
    query = query or _graph_query()
    phase, skip = _parse_cursor(cursor)
    if phase == "nodes":
        items = _node_page(query, skip, page_size)
        next_cursor = (f"nodes:{skip + page_size}" if len(items) == page_size
                       else "edges:0")
    else:
        items = _edge_page(query, skip, page_size)
        next_cursor = (f"edges:{skip + page_size}" if len(items) == page_size
                       else None)
    return {"format": FORMAT_VERSION, "kind": phase, "items": items,
            "next_cursor": next_cursor}


def _parse_cursor(cursor: str) -> tuple[str, int]:
    """Parse the chunk cursor; '' starts at nodes:0. Raises ValueError."""
    if not cursor:
        return "nodes", 0
    phase, sep, skip_text = cursor.partition(":")
    if sep != ":" or phase not in ("nodes", "edges") or not skip_text.isdigit():
        raise ValueError(f"malformed snapshot cursor: {cursor!r}")
    return phase, int(skip_text)


def assemble_snapshot(node_items: list[dict], edge_items: list[dict]) -> dict:
    """Build the snapshot file dict from exported node/edge items.

    Edge endpoints become FILE-LOCAL positional indexes into ``nodes`` (never
    an assumed ``id`` property). Raises ``ValueError`` when an edge references
    a gid missing from the node items (an inconsistent export — e.g. the graph
    mutated between pages).
    """
    index_by_gid = {item["gid"]: i for i, item in enumerate(node_items)}
    nodes = [{"labels": item["labels"], "props": item["props"]}
             for item in node_items]
    edges = []
    for item in edge_items:
        try:
            from_idx = index_by_gid[item["from_gid"]]
            to_idx = index_by_gid[item["to_gid"]]
        except KeyError as exc:
            raise ValueError(
                f"edge references unknown node id {exc} — graph changed "
                "during export; re-run the export"
            ) from exc
        edges.append({"type": item["type"], "from": from_idx, "to": to_idx,
                      "props": item.get("props", {})})
    return {
        "format": FORMAT_VERSION,
        "exported_at": _utcnow_iso(),
        "node_count": len(nodes),
        "edge_count": len(edges),
        "nodes": nodes,
        "edges": edges,
    }


def export_snapshot(query=None, page_size: int = DEFAULT_PAGE_SIZE) -> dict:
    """Full in-process export: walk every chunk and assemble the file dict."""
    query = query or _graph_query()
    node_items: list[dict] = []
    edge_items: list[dict] = []
    cursor = ""
    for _ in range(_MAX_BATCH_ITERATIONS):
        chunk = export_chunk(cursor, query=query, page_size=page_size)
        (node_items if chunk["kind"] == "nodes" else edge_items).extend(
            chunk["items"])
        cursor = chunk["next_cursor"]
        if cursor is None:
            return assemble_snapshot(node_items, edge_items)
    raise RuntimeError("graph export did not converge (page walk overflow)")


# ── Validation ───────────────────────────────────────────────────────────────

def validate_snapshot(payload) -> None:
    """Refuse anything that is not a well-formed FORMAT_VERSION snapshot.

    Raises ``ValueError``. Labels/edge types are checked against ``_NAME_RE``
    because they are interpolated into openCypher text on import (injection
    guard); edge endpoints must be in-range positional indexes.
    """
    if not isinstance(payload, dict):
        raise ValueError("snapshot must be a JSON object")
    fmt = payload.get("format")
    if fmt != FORMAT_VERSION:
        raise ValueError(
            f"unsupported snapshot format {fmt!r} — expected {FORMAT_VERSION!r}")
    nodes = payload.get("nodes")
    edges = payload.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise ValueError("snapshot 'nodes' and 'edges' must be arrays")
    for i, node in enumerate(nodes):
        _validate_node(i, node)
    for i, edge in enumerate(edges):
        _validate_edge(i, edge, len(nodes))
    _check_system_markers(nodes, edges)


#: The System node's onboarding marker (``catalog.ONBOARDED_NODE_STATUS``; the
#: catalog module is not imported here, see the module docstring).
_ONBOARDED_STATUS = "onboarded"


def _check_system_markers(nodes: list, edges: list) -> None:
    """Refuse a snapshot whose System node carries onboarding data (any edge:
    a SchemaEntry, an asset, a rule) but no onboarding marker. The restore,
    the catalog's graph hydration and :func:`reconcile_systems` count a system
    as onboarded only by ``status='onboarded'``, so such a system would import
    and then silently disappear. A System with no edges and no marker is the
    connection settings an Edit before onboarding leaves, which a current
    export carries as is; a half-removed one (``removal_incomplete``) restores
    as a removal to retry either way. Nothing is promoted here."""
    linked = {edge["from"] for edge in edges} | {edge["to"] for edge in edges}
    unmarked = sorted(
        str(node["props"].get("id") or f"node[{i}]") for i, node in enumerate(nodes)
        if "System" in node["labels"] and i in linked
        and node["props"].get("status") != _ONBOARDED_STATUS
        and node["props"].get("removal_incomplete") is not True)
    if unmarked:
        raise ValueError(
            "snapshot System node(s) " + ", ".join(unmarked) + " carry onboarding data "
            f"but no status '{_ONBOARDED_STATUS}', so they would import and then not be "
            "restored as onboarded. Set \"status\": \"onboarded\" in the props of the ones "
            "that are onboarded (or remove the stale ones) and import again; nothing was "
            "changed.")


def _validate_node(i: int, node) -> None:
    if not isinstance(node, dict) or not isinstance(node.get("labels"), list) \
            or not isinstance(node.get("props"), dict):
        raise ValueError(f"node[{i}] must have 'labels' (array) and 'props' (object)")
    for label in node["labels"]:
        if not isinstance(label, str) or not _NAME_RE.match(label):
            raise ValueError(f"node[{i}] has unsafe label {label!r}")


def _validate_edge(i: int, edge, node_count: int) -> None:
    if not isinstance(edge, dict) or not isinstance(edge.get("props", {}), dict):
        raise ValueError(f"edge[{i}] must be an object with object 'props'")
    etype = edge.get("type")
    if not isinstance(etype, str) or not _NAME_RE.match(etype):
        raise ValueError(f"edge[{i}] has unsafe type {etype!r}")
    for end in ("from", "to"):
        idx = edge.get(end)
        if not isinstance(idx, int) or isinstance(idx, bool) \
                or not 0 <= idx < node_count:
            raise ValueError(
                f"edge[{i}] '{end}' must be a node index in [0, {node_count})")


# ── Import (wipe-then-recreate) ─────────────────────────────────────────────

def _count(query, cypher: str) -> int:
    rows = query(cypher)
    if not rows:
        return 0
    return int(list(rows[0].values())[0] or 0)


def _batched_write_until_empty(query, write, count_cypher: str,
                               write_cypher: str) -> int:
    """Repeat a bounded write until its count query reaches zero."""
    total = 0
    for _ in range(_MAX_BATCH_ITERATIONS):
        remaining = _count(query, count_cypher)
        if remaining <= 0:
            return total
        if not write(write_cypher):
            raise RuntimeError(f"bounded graph write failed: {write_cypher!r}")
        total += remaining - _count(query, count_cypher)
    raise RuntimeError("bounded graph write loop did not converge")


@_mutates_value_mappings
def wipe_graph(query=None, write=None,
               batch_size: int = DEFAULT_WIPE_BATCH) -> int:
    """Delete EVERY node (and, via DETACH, every edge) in bounded batches.

    Never a single unbounded ``MATCH (n) DETACH DELETE n`` — repeated
    ``LIMIT k`` batches keep each request small. Returns nodes deleted.
    """
    query = query or _graph_query()
    write = write or _graph_write()
    return _batched_write_until_empty(
        query, write,
        "MATCH (n) RETURN count(n) AS n",
        f"MATCH (n) WITH n LIMIT {int(batch_size)} DETACH DELETE n",
    )


def _chunks(rows: list, size: int):
    for start in range(0, len(rows), size):
        yield rows[start:start + size]


def _create_nodes(write, nodes: list[dict], batch_size: int) -> int:
    """Phase 1: batch-create nodes carrying a temporary ``_import_key``.

    Grouped by label set (labels must appear in the query text), one bounded
    UNWIND per chunk. The key is the node's FILE-LOCAL positional index —
    id-agnostic by construction.
    """
    by_labels: dict[tuple, list[dict]] = {}
    for idx, node in enumerate(nodes):
        props = {k: v for k, v in without_internal_properties(node["props"]).items()
                 if k != _TEMP_KEY}
        by_labels.setdefault(tuple(node["labels"]), []).append(
            {"key": idx, "props": props})
    created = 0
    for labels, rows in by_labels.items():
        label_text = "".join(f":{label}" for label in labels)
        cypher = (f"UNWIND $rows AS row CREATE (n{label_text}) "
                  f"SET n += row.props, n.{_TEMP_KEY} = row.key")
        for chunk in _chunks(rows, batch_size):
            if not write(cypher, {"rows": chunk}):
                raise RuntimeError(f"node import batch failed (labels={labels})")
            created += len(chunk)
    return created


def _create_edges(write, edges: list[dict], batch_size: int) -> int:
    """Phase 2: batch-create edges by MATCHing ``_import_key`` pairs."""
    by_type: dict[str, list[dict]] = {}
    for edge in edges:
        by_type.setdefault(edge["type"], []).append({
            "from_key": edge["from"], "to_key": edge["to"],
            "props": edge.get("props", {}),
        })
    created = 0
    for etype, rows in by_type.items():
        cypher = (
            f"UNWIND $rows AS row "
            f"MATCH (a {{{_TEMP_KEY}: row.from_key}}) "
            f"MATCH (b {{{_TEMP_KEY}: row.to_key}}) "
            f"CREATE (a)-[r:{etype}]->(b) SET r += row.props"
        )
        for chunk in _chunks(rows, batch_size):
            if not write(cypher, {"rows": chunk}):
                raise RuntimeError(f"edge import batch failed (type={etype})")
            created += len(chunk)
    return created


def _remove_import_keys(query, write, batch_size: int) -> int:
    """Phase 3: strip every temporary ``_import_key``, in bounded batches."""
    return _batched_write_until_empty(
        query, write,
        f"MATCH (n) WHERE n.{_TEMP_KEY} IS NOT NULL RETURN count(n) AS n",
        f"MATCH (n) WHERE n.{_TEMP_KEY} IS NOT NULL "
        f"WITH n LIMIT {int(batch_size)} REMOVE n.{_TEMP_KEY}",
    )


@_mutates_value_mappings
def import_snapshot(payload: dict, query=None, write=None,
                    wipe_batch: int = DEFAULT_WIPE_BATCH,
                    import_batch: int = DEFAULT_IMPORT_BATCH) -> dict:
    """Wipe the graph and restore it from a snapshot file dict.

    DESTRUCTIVE — the caller (route/CLI) owns confirmation. Order is pinned:
    validate → wipe (bounded batches) → create nodes (+temp key) → create
    edges (key pairs) → remove temp keys. Refuses unknown format versions.
    """
    validate_snapshot(payload)
    query = query or _graph_query()
    write = write or _graph_write()
    with _long_statements():
        return _import_validated(payload, query, write, wipe_batch, import_batch)


def _long_statements():
    """``db.graph_statement_limit()``: a restore's batches touch the whole
    graph (the edge batches MATCH by an unlabeled ``_import_key``) and none
    has been timed, so they get the long server limit. The import only runs
    as the admin route's background job (``graph_snapshot_routes``), never
    behind a request a caller waits on (lazy import, see the module
    docstring)."""
    from agents.db import graph_statement_limit
    return graph_statement_limit()


def _import_validated(payload: dict, query, write, wipe_batch: int, import_batch: int) -> dict:
    """The body of :func:`import_snapshot` after validation."""
    started = _utcnow_iso()
    wiped = wipe_graph(query=query, write=write, batch_size=wipe_batch)
    nodes_created = _create_nodes(write, payload["nodes"], import_batch)
    edges_created = _create_edges(write, payload["edges"], import_batch)
    keys_removed = _remove_import_keys(query, write, wipe_batch)
    logger.info(
        "graph snapshot import: wiped=%d nodes=%d edges=%d keys_removed=%d",
        wiped, nodes_created, edges_created, keys_removed)
    return {
        "format": FORMAT_VERSION,
        "started_at": started,
        "finished_at": _utcnow_iso(),
        "nodes_wiped": wiped,
        "nodes_created": nodes_created,
        "edges_created": edges_created,
        "import_keys_removed": keys_removed,
    }


# ── Reconcile (husk detection) + repair ─────────────────────────────────────
#
# An imported snapshot can restore System nodes whose status says 'onboarded'
# while the AgentCore runtime / gateway target they name no longer exist
# (deleted by an offboard between export and import) — a HUSK. The check is
# read-only and cheap (two control-plane listings); the REPAIR reuses the
# exact machinery the app already trusts on every ECS restart:
# server._restore rebuilds each onboarded system from graph truth, and
# start_agent's cloud path recreates a missing runtime/target (including
# dangling-target detection). Nothing here re-implements provisioning.


def _onboarded_system_ids(query=None) -> list[str]:
    """System ids the graph claims are onboarded (status property)."""
    if query is None:
        from .db import neptune_query as query
    rows = query(
        "MATCH (s:System) WHERE s.status = 'onboarded' "
        "RETURN s.id AS id ORDER BY id") or []
    return [r["id"] for r in rows if r.get("id")]


def reconcile_systems(*, manager, control, query=None) -> list[dict]:
    """Report, per onboarded System node: runtime exists AND is READY, and
    gateway target exists. Names derive ONLY from the single-source
    convention (``manager._runtime_name``) — never re-derived here.

    Read-only. Returns ``[{system_id, runtime_ok, target_ok, healthy}]``."""
    runtimes: dict[str, str] = {}
    token = None
    while True:  # paginate: a deployed env easily exceeds one page
        kwargs = {}
        if token:
            kwargs["nextToken"] = token
        resp = control.list_agent_runtimes(**kwargs)
        for rt in resp.get("agentRuntimes", []):
            runtimes[rt.get("agentRuntimeName", "")] = rt.get("status", "")
        token = resp.get("nextToken")
        if not token:
            break
    from .runtime_provision_helpers import list_all_gateway_targets
    targets: set[str] = {
        item.get("name", "")
        for item in list_all_gateway_targets(control, manager.gateway_id)
    }
    report = []
    for sid in _onboarded_system_ids(query):
        runtime_ok = runtimes.get(manager._runtime_name(sid)) == "READY"
        target_ok = sid in targets
        report.append({"system_id": sid, "runtime_ok": runtime_ok,
                       "target_ok": target_ok,
                       "healthy": runtime_ok and target_ok})
    return report


def repair_systems(report: list[dict], *, restore_fn) -> dict:
    """Re-provision every unhealthy system in *report* by re-running the
    app's own startup restore (``server._restore`` — graph truth in,
    runtime/target/agent-config out). ONE restore pass covers all husks: it
    walks every onboarded system; ``start_agent`` recreates whatever is
    missing while healthy systems take the fast path (no control-plane
    writes). Business context rides the same path (the restored graph is
    the source ``refresh_business_context`` reads).

    Returns ``{"repaired": [ids], "healthy": [ids]}``; raises on restore
    failure (fail-loud — a silent half-repair IS the husk bug again)."""
    unhealthy = [r["system_id"] for r in report if not r["healthy"]]
    healthy = [r["system_id"] for r in report if r["healthy"]]
    if unhealthy:
        restore_fn()
    return {"repaired": unhealthy, "healthy": healthy}
