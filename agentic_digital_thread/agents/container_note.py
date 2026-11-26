# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""What sits directly inside a container asset an ``explore_graph`` row returned.

A graph query that matches a container (a station, a line, a cell) returns the
container, not the equipment inside it, and a model can read the container as
the answer. When result rows carry an asset whose classified ``role`` is a
container tier, :func:`container_contents` reads those claimed containers
(the graph confirms each), then makes ONE bounded read of the assets directly
inside the confirmed ones and returns them for the tool envelope. A
graph with no roles (not yet classified, or ``ADT_ASSET_ROLES=off``) gets
nothing, so the role switch turns this off too.

It never reads the question and holds no names of sites, models or tiers: it
reads the result rows and the graph only, so it behaves the same on any
source. It is conservative about what it attaches:

* a row's role only proposes a container: the read returns the parent's own
  role, name and system from the graph, and only a parent the GRAPH says is
  a container gets an entry, so a role from another node's columns never
  makes one;
* a container known only by path needs its system too, since paths are only
  unique within one system; a source id matching parents in several systems
  is skipped unless the row names one of them;
* a child is listed only under its CURRENT parent: its path must be the
  container's path plus one segment, so an edge a re-walk left behind (to an
  old ancestor or another branch) is ignored;
* other containers, parts and data streams are not listed, nor is any asset
  the query already returned (matched by source id or path), nor the same
  child twice.

It lists facts only. A steward's review flags (``needs_review``, including
``identifier_gap``) stay on the AssetModelRole node: absence of another
system's identifier does not tell a machine from its container's own data.

Any failure returns ``None``: the query's own result is never put at risk.
"""
from __future__ import annotations

import logging
from typing import Callable

logger = logging.getLogger(__name__)

#: Roles of assets that hold equipment.
_CONTAINER_ROLES = frozenset({"site", "area", "work_center", "work_unit"})
#: Children not worth listing: other containers, and parts or data streams.
_SKIP_CHILD_ROLES = _CONTAINER_ROLES | {"enterprise", "equipment_part", "data_stream"}

MAX_CONTAINERS = 10
MAX_CHILDREN = 25
_READ_LIMIT = 1000
#: Rows only CLAIM a container; the graph confirms it. Every claim up to the
#: read's own bound is read, so false claims cannot crowd out real
#: containers; past it the note may omit some (never misstate them).
MAX_CANDIDATES = _READ_LIMIT

_FIELDS = ("source_id", "role", "path", "name", "system", "id")

_CHILDREN_QUERY = (
    "MATCH (c:Asset)-[:HAS_PARENT]->(p:Asset) "
    "WHERE p.source_id IN $ids OR p.path IN $paths "
    "RETURN p.source_id AS parent_id, p.path AS parent_path, p.system AS parent_system, "
    "p.role AS parent_role, p.name AS parent_name, "
    "c.name AS name, c.role AS role, c.asset_type AS asset_type, "
    "c.source_id AS source_id, c.path AS path "
    f"ORDER BY parent_path, path LIMIT {_READ_LIMIT}"
)

#: The claimed containers themselves, read first: the graph confirms each
#: (role, system) before any child is read, so the child read asks only for
#: confirmed containers, and a source id held by several systems is seen as
#: shared whatever the child read returns. (The child read is keyed by id, so
#: another system's node with the same id could still use up its limit; the
#: walkers MERGE assets on source_id alone, so they cannot create such a pair,
#: and the entry stays honest: holds [], complete false.)
_PARENTS_QUERY = (
    "MATCH (p:Asset) WHERE p.source_id IN $ids OR p.path IN $paths "
    "RETURN p.source_id AS parent_id, p.path AS parent_path, p.system AS parent_system, "
    "p.role AS parent_role, p.name AS parent_name "
    f"LIMIT {_READ_LIMIT}"
)

NOTE = (
    "Some rows are containers (a site, area, work center or work unit): they hold "
    "equipment rather than being it. inside_containers lists, for each one, the assets "
    "directly inside it that are not themselves containers, parts or data streams, with "
    "their role. An unclassified one may still be equipment. Read them before treating "
    "a container as the equipment a question asks about. 'more' counts assets not "
    "listed; complete=false means the listing was cut short."
)


def _split(column: str) -> tuple[str, str] | None:
    """``("a", "role")`` for ``a.role`` / ``a_role`` / ``role`` (prefix ``""``).
    A bare ``id`` column (``s.source_id AS station_id``) reads as the source id."""
    low = str(column).lower()
    for field in _FIELDS:
        prefix = None
        if low == field:
            prefix = ""
        else:
            for sep in (".", "_"):
                if low.endswith(sep + field):
                    prefix = low[: -len(field) - 1]
                    break
        if prefix is not None:
            return prefix, "source_id" if field == "id" else field
    return None


def _row_assets(row) -> list[dict]:
    """The assets one result row carries: node values, plus one asset per
    column prefix that has its own key (source id or path). Fields are never
    merged across prefixes."""
    if not isinstance(row, dict):
        return []
    assets, groups = [], {}
    for col, value in row.items():
        props = value.get("~properties") if isinstance(value, dict) else None
        if isinstance(props, dict):
            assets.append({f: props.get(f) for f in _FIELDS if f in props})
            continue
        split = _split(col)
        if split is not None and value is not None and not isinstance(value, (dict, list)):
            groups.setdefault(split[0], {}).setdefault(split[1], value)
    return assets + [g for g in groups.values() if g.get("source_id") or g.get("path")]


def _keys(asset: dict) -> set[str]:
    """Every key the asset is known by (an asset matched by EITHER is the same)."""
    keys = set()
    if asset.get("source_id"):
        keys.add(f"id:{asset['source_id']}")
    if asset.get("path"):
        keys.add(f"path:{asset['path']}")
    return keys


def _containers(rows: list) -> tuple[list[dict], set[str]]:
    """Claimed container assets in the rows (deduplicated, at most
    MAX_CANDIDATES) and the keys of every asset the rows carry."""
    found, seen, taken = [], set(), set()
    for row in rows:
        for asset in _row_assets(row):
            keys = _keys(asset)
            seen |= keys
            if str(asset.get("role") or "") not in _CONTAINER_ROLES or keys & taken:
                continue
            if not asset.get("source_id") and not asset.get("system"):
                continue                    # a path alone is only unique per system
            if len(found) < MAX_CANDIDATES:
                taken |= keys
                found.append(asset)
    return found, seen


def _keys_params(assets: list[dict]) -> dict:
    """Read parameters: source ids, and paths for assets known only by path."""
    return {"ids": sorted({str(a["source_id"]) for a in assets if a.get("source_id")}),
            "paths": sorted({str(a["path"]) for a in assets
                             if not a.get("source_id") and a.get("path")})}


def _parent_of(container: dict, parents: list[dict]) -> dict | None:
    """The graph's own record of this claimed container, or ``None`` (skip
    it) when the graph has no such asset, says it is not a container, or
    holds the source id in several systems and the row named none of them.
    ``parents`` is the parent read: every asset with a claimed id or path."""
    sid, system = container.get("source_id"), container.get("system")
    if sid:
        mine = [p for p in parents if p.get("parent_id") == sid]
    else:
        mine = [p for p in parents
                if p.get("parent_path") == container.get("path")
                and p.get("parent_system") == system]
    if not mine:
        return None
    systems = {p.get("parent_system") for p in mine}
    if len(systems) > 1:
        if system not in systems:
            return None
        mine = [p for p in mine if p.get("parent_system") == system]
    graph = mine[0]
    if str(graph.get("parent_role") or "") not in _CONTAINER_ROLES:
        return None                         # the graph says it is not a container
    return graph


def _children_under(graph: dict, children: list[dict]) -> list[dict]:
    """The read rows whose parent is exactly this node (id, path and system)."""
    key = (graph.get("parent_id"), graph.get("parent_path"), graph.get("parent_system"))
    return [k for k in children
            if (k.get("parent_id"), k.get("parent_path"), k.get("parent_system")) == key]


def _direct(child: dict) -> bool:
    """True when the child's path is its parent's path plus one segment (a
    re-walk can leave an edge to an old ancestor or another branch). Without
    both paths the edge is taken as it is."""
    parent_path, path = child.get("parent_path"), child.get("path")
    if not parent_path or not path:
        return True
    parent_path, path = str(parent_path), str(path)
    if not path.startswith(f"{parent_path}/"):
        return False
    rest = path[len(parent_path) + 1:]
    return "/" not in rest or rest == str(child.get("name") or "")


def _listable(children: list[dict], seen: set[str]) -> list[dict]:
    """Children worth listing, each once."""
    out, taken = [], set(seen)
    for c in children:
        keys = _keys(c)
        if str(c.get("role") or "") in _SKIP_CHILD_ROLES or not _direct(c) or keys & taken:
            continue
        taken |= keys
        out.append(c)
    return out


def _entry(graph: dict, children: list[dict], complete: bool) -> dict:
    """One entry; the container's role, name, path and id are the graph's."""
    path = graph.get("parent_path")
    name = graph.get("parent_name") or (str(path).rsplit("/", 1)[-1] if path else None)
    entry = {"container": name, "role": graph.get("parent_role"),
             "path": path, "source_id": graph.get("parent_id"),
             "holds": [{k: c.get(k) for k in ("name", "role", "asset_type", "source_id")}
                       for c in children[:MAX_CHILDREN]],
             "more": max(0, len(children) - MAX_CHILDREN)}
    if not complete:
        entry["complete"] = False
    return entry


def container_contents(rows: list, query: Callable) -> list[dict] | None:
    """``inside_containers`` entries for the container assets in ``rows``, or
    ``None`` when there are none (or a read fails). ``query(cypher, params)``
    runs a graph read: one for the claimed containers themselves (so the graph
    confirms each, and a source id held by several systems is seen as shared),
    then one for the children of the confirmed ones."""
    try:
        containers, seen = _containers(rows)
        if not containers:
            return None
        parents = query(_PARENTS_QUERY, _keys_params(containers)) or []
        if len(parents) >= _READ_LIMIT:
            return None                     # cannot confirm them all: omit, never guess
        confirmed, taken = [], set()
        for c in containers:
            graph = _parent_of(c, parents)
            key = graph and (graph.get("parent_id"), graph.get("parent_path"),
                             graph.get("parent_system"))
            if graph is not None and key not in taken:
                taken.add(key)
                confirmed.append(graph)
        if not confirmed:
            return None
        children = query(_CHILDREN_QUERY, _keys_params(
            [{"source_id": g.get("parent_id"), "path": g.get("parent_path")}
             for g in confirmed])) or []
        complete = len(children) < _READ_LIMIT
        entries = []
        for graph in confirmed:
            listable = _listable(_children_under(graph, children), seen)
            if listable or not complete:
                entries.append(_entry(graph, listable, complete))
                if len(entries) == MAX_CONTAINERS:
                    break
        return entries or None
    except Exception as exc:  # noqa: BLE001 - the query's own result must never fail
        logger.warning("container contents read failed (result returned without it): %s", exc)
        return None
