# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Write asset-model roles into the graph (the impure side of ``asset_roles``).

Reads one system's ``:Asset`` tree, its accepted value mappings and any
steward-confirmed roles, classifies every asset model with
:func:`asset_roles.classify_models`, then writes:

* per ``:Asset`` — ``role``, ``depth`` (from its root, root = 0) and ``level``
  derived from the role (an unclassified model keeps the walker's label). The
  walkers stamp ``walker_level`` on every write, so the walker's own label is
  never inferred and :func:`restore_system` can put it back;
* per ``(system, model)`` — one ``:AssetModelRole`` node with the role, the
  rule that decided it, a plain-sentence reason and ``needs_review``. A node
  whose ``status`` is ``steward`` is the steward's override: it is read as
  input and the write cannot touch it. Derived nodes for models the system no
  longer has are deleted;
* per ``:System`` — as the last write, ``asset_coverage`` (one compact JSON
  string: for each site, how many assets of each stored level sit under it,
  :mod:`asset_coverage`) and ``asset_coverage_at``. A system with no assets
  left, and the rollback, remove both. A failed coverage write is reported in
  the result (``coverage_written``/``coverage_error``) and followed by one
  best-effort clear (``coverage_cleared``); a failed removal on rollback
  raises, since the restore is then incomplete.

Every read is checked: a graph read that fails stops that system before any
write (an empty read is never taken for "no overrides" or "no mappings").
Writes stop at the first failure and the result says so.

The model of an asset is its ``asset_type`` (re-set on every walk) and its
parent the ``HAS_PARENT`` target with the longest path that prefixes the
asset's own, so edges a re-walk left behind never decide a role. An asset
whose parent still can't be told, and everything below it, is kept out of the
model tree (it would read as a root), takes its model's role and keeps its
depth.

It runs after every provision (a new system's value mappings can change
another system's roles) and from ``POST /api/admin/asset-roles/reclassify``.
``ADT_ASSET_ROLES=off`` turns the pass into a rollback: levels go back to the
walker's, roles and derived role nodes are removed, steward roles are kept.
The rollback runs at the next provision; to apply it at once, set the switch
and POST ``/api/admin/asset-roles/reclassify?restore=true``. Per-system
writes are not one transaction: model nodes are written before assets, and a
failure is reported, never hidden.
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timezone

from .asset_coverage import coverage_json, parse_coverage
from .asset_roles import (AssetRow, asset_depths, classify_models,
                          equipment_hits_from_mappings, matched_assets_from_mappings)

logger = logging.getLogger(__name__)

#: Serialises every role pass and rollback, per system call, inside the app
#: process. A pass reads, computes, then writes role nodes and assets in
#: separate operations, so two overlapping passes on one system (an admin
#: reclassify and a provision post-pass, or a reclassify and a restore) could
#: leave an asset's role out of step with its AssetModelRole. One lock is
#: enough because every writer runs in the one app process (one uvicorn
#: worker, ECS desired count 1), the same assumption as RUNTIME_CONFIG_LOCK.
#: A walk does not take it: every walk is followed by a post-pass that does,
#: so the last pass to run always reads after the walk before it. Held only
#: in worker threads (the admin route is a sync route, the post-pass runs in
#: the provision worker), never on the event loop.
ROLE_PASS_LOCK = threading.Lock()

_WRITE_CHUNK = 500

_READ_ASSETS = (
    "MATCH (a:Asset {system: $sys}) "
    "OPTIONAL MATCH (a)-[:HAS_PARENT]->(p:Asset) "
    "RETURN a.source_id AS id, a.name AS name, a.asset_type AS asset_type, "
    "a.path AS path, a.level AS level, a.walker_level AS walker_level, "
    "p.source_id AS parent_id, p.path AS parent_path"
)
_READ_MAPPINGS = (
    "MATCH (vm:ValueMapping) WHERE vm.from_system = $sys OR vm.to_system = $sys "
    "RETURN vm.from_system AS from_system, vm.from_value AS from_value, "
    "vm.to_system AS to_system, vm.to_value AS to_value, "
    "vm.to_element_id AS to_element_id, vm.concept AS concept"
)
_READ_OVERRIDES = (
    "MATCH (r:AssetModelRole {system: $sys}) WHERE r.status = 'steward' "
    "RETURN r.model AS model, r.role AS role"
)
_READ_SYSTEMS = (
    "MATCH (a:Asset) WHERE a.system IS NOT NULL "
    "OPTIONAL MATCH (s:System {id: a.system}) "
    "RETURN DISTINCT a.system AS system, s IS NOT NULL AS has_system, "
    "coalesce(s.removal_incomplete, false) AS removal_incomplete "
    "UNION "
    "MATCH (s:System) WHERE s.asset_coverage IS NOT NULL "
    "RETURN s.id AS system, true AS has_system, "
    "coalesce(s.removal_incomplete, false) AS removal_incomplete"
)
_WRITE_ASSETS = (
    "UNWIND $rows AS r MATCH (a:Asset {source_id: r.id}) WHERE a.system = $sys "
    "SET a.role = r.role, a.depth = coalesce(r.depth, a.depth), a.level = r.level, "
    "a.walker_level = coalesce(a.walker_level, r.walker_level)"
)
_WRITE_MODELS = (
    "UNWIND $rows AS r MERGE (mr:AssetModelRole {system: $sys, model: r.model}) "
    "WITH mr, r WHERE coalesce(mr.status, '') <> 'steward' "
    "SET mr.role = r.role, mr.source = r.source, mr.evidence = r.evidence, "
    "mr.needs_review = r.needs_review, mr.status = 'derived', mr.updated_at = $now"
)
_SWEEP_MODELS = (
    "MATCH (mr:AssetModelRole {system: $sys}) "
    "WHERE NOT mr.model IN $models AND coalesce(mr.status, '') <> 'steward' "
    "DETACH DELETE mr"
)
_LINK_MODELS = (
    "MATCH (mr:AssetModelRole {system: $sys}), (s:System {id: $sys}) "
    "MERGE (mr)-[:FROM_SYSTEM]->(s)"
)
_RESTORE_ASSETS = (
    "MATCH (a:Asset {system: $sys}) WHERE a.walker_level IS NOT NULL "
    "SET a.level = a.walker_level REMOVE a.role, a.depth"
)
_RESTORE_MODELS = (
    "MATCH (mr:AssetModelRole {system: $sys}) WHERE coalesce(mr.status, '') <> 'steward' "
    "DETACH DELETE mr"
)
# MATCH, never MERGE: a system deleted (offboarded) while a pass runs must not
# be recreated as a bare node carrying counts.
_WRITE_COVERAGE = (
    "MATCH (s:System {id: $sys}) "
    "SET s.asset_coverage = $coverage, s.asset_coverage_at = $now"
)
_CLEAR_COVERAGE = (
    "MATCH (s:System {id: $sys}) REMOVE s.asset_coverage, s.asset_coverage_at"
)


class RolePassError(RuntimeError):
    """A read or write of the role pass failed; nothing after it ran."""


def enabled() -> bool:
    return os.environ.get("ADT_ASSET_ROLES", "on").strip().lower() not in ("off", "0", "false", "no")


def _db():
    from .db import neptune_query_checked, neptune_write
    return neptune_query_checked, neptune_write


def _read(query, cypher: str, params: dict | None = None) -> list:
    try:
        return query(cypher, params) or []
    except Exception as e:  # noqa: BLE001 - any failed read stops the system
        raise RolePassError(f"graph read failed: {type(e).__name__}") from e


def _current_parent(path: str, parents: dict[str, str]) -> tuple[str | None, bool]:
    """``(parent id, ambiguous)``: the one parent, or — when a re-walk left
    several — the one with the LONGEST path that prefixes the asset's own (a
    stale ancestor's path is a proper prefix of the current parent's)."""
    if len(parents) <= 1:
        return next(iter(parents), None), False
    prefixed = {pid: pp for pid, pp in parents.items() if pp and path.startswith(f"{pp}/")}
    if not prefixed:
        return None, True
    longest = max(len(pp) for pp in prefixed.values())
    best = [pid for pid, pp in prefixed.items() if len(pp) == longest]
    return (best[0], False) if len(best) == 1 else (None, True)


def _read_assets(query, system_id: str) -> tuple[list[AssetRow], list[AssetRow], dict[str, str]]:
    """``(placed, unplaced, walker label by id)``. One read row per parent
    edge, grouped here. An asset whose current parent can't be told is
    UNPLACED: kept out of the model tree (it would read as a root) but still
    given its model's role."""
    seen: dict[str, dict] = {}
    for r in _read(query, _READ_ASSETS, {"sys": system_id}):
        aid = r.get("id")
        if not aid:
            continue
        entry = seen.setdefault(aid, {"row": r, "parents": {}})
        if r.get("parent_id"):
            entry["parents"][r["parent_id"]] = r.get("parent_path") or ""
    placed, unplaced, walker = [], [], {}
    for aid, entry in seen.items():
        r = entry["row"]
        parent, unsure = _current_parent(str(r.get("path") or ""), entry["parents"])
        row = AssetRow(id=aid, name=r.get("name") or aid,
                       model=r.get("asset_type") or "Unknown", parent_id=parent)
        (unplaced if unsure else placed).append(row)
        walker[aid] = r.get("walker_level") or r.get("level") or ""
    # Below an unplaced asset nothing is placed either: its children would
    # otherwise read as roots (depth 0, "stands alone").
    lost = {a.id for a in unplaced}
    while True:
        moved = [a for a in placed if a.parent_id in lost]
        if not moved:
            break
        lost.update(a.id for a in moved)
        unplaced.extend(moved)
        placed = [a for a in placed if a.id not in lost]
    return placed, unplaced, walker


def _asset_writes(placed, unplaced, roles, walker: dict[str, str]) -> list[dict]:
    depths = asset_depths(placed)

    def row(a: AssetRow, depth) -> dict:
        role = roles.get(a.model)
        return {"id": a.id, "role": role.role if role else "unclassified", "depth": depth,
                "level": (role.level if role else None) or walker.get(a.id) or None,
                "walker_level": walker.get(a.id) or None}
    return ([row(a, depths.get(a.id, 0)) for a in placed]
            + [row(a, None) for a in unplaced])


def _write_chunks(write, cypher: str, rows: list[dict], params: dict) -> None:
    for i in range(0, len(rows), _WRITE_CHUNK):
        if not write(cypher, {**params, "rows": rows[i:i + _WRITE_CHUNK]}):
            raise RolePassError("graph write failed")


def _write(write, cypher: str, params: dict) -> None:
    if not write(cypher, params):
        raise RolePassError("graph write failed")


def _summary(system_id, assets, roles, asset_rows, ambiguous) -> dict:
    return {"system": system_id, "assets": len(assets), "models": len(roles),
            "ambiguous_parents": ambiguous,
            "level_changes": sum(1 for r in asset_rows if r["level"] != r["walker_level"]),
            "needs_review": sorted(m for m, r in roles.items() if r.needs_review),
            "roles": {m: {"role": r.role, "level": r.level, "source": r.source,
                          "evidence": r.evidence, "needs_review": r.needs_review}
                      for m, r in sorted(roles.items())},
            "written": False}


def _coverage_write(write, cypher: str, params: dict, system_id: str) -> str:
    """Run one coverage write; ``""`` on success, else the failure category
    (logged at WARNING). Never raises: the role writes before it have already
    landed and the result must not read as if they had not."""
    category = ""
    try:
        if not write(cypher, params):
            category = "write_failed"
    except Exception as e:  # noqa: BLE001 - returned as a category, see above
        category = getattr(e, "category", "") or type(e).__name__
    if category:
        logger.warning("asset coverage for %s not written (roles unaffected): category=%s",
                       system_id, category)
    return category


def _write_coverage(write, system_id: str, coverage: str, summary: dict) -> None:
    """Store the coverage; on failure make one best-effort clear so the seed
    does not serve the previous counts, and report both outcomes."""
    category = _coverage_write(write, _WRITE_COVERAGE,
                               {"sys": system_id, "coverage": coverage,
                                "now": datetime.now(timezone.utc).isoformat()}, system_id)
    summary["coverage_written"] = not category
    if category:
        summary["coverage_error"] = f"graph write failed: {category}"
        summary["coverage_cleared"] = not _coverage_write(
            write, _CLEAR_COVERAGE, {"sys": system_id}, system_id)


def _clear_coverage(write, system_id: str, summary: dict) -> None:
    category = _coverage_write(write, _CLEAR_COVERAGE, {"sys": system_id}, system_id)
    summary["coverage_written"] = not category
    if category:
        summary["coverage_error"] = f"graph write failed: {category}"


def classify_system(system_id: str, *, dry_run: bool = False, query=None, write=None) -> dict:
    """Classify one system's asset models and (unless ``dry_run``) write them.
    Raises :class:`RolePassError` when a read or write fails. Holds
    :data:`ROLE_PASS_LOCK` from the first read to the last write. The last
    write records the system's per-site asset coverage
    (:mod:`asset_coverage`) on its System node, or removes it when the system
    has no assets left; that write's failure is reported, not raised."""
    if query is None or write is None:
        default_query, default_write = _db()
        query, write = query or default_query, write or default_write
    with ROLE_PASS_LOCK:
        return _classify_system(system_id, dry_run, query, write)


def _classify_system(system_id: str, dry_run: bool, query, write) -> dict:
    placed, unplaced, walker = _read_assets(query, system_id)
    if not placed and not unplaced:
        summary = {**_summary(system_id, [], {}, [], 0), "coverage": None}
        if not dry_run:
            _clear_coverage(write, system_id, summary)
        return summary
    vms = _read(query, _READ_MAPPINGS, {"sys": system_id})
    overrides = {r["model"]: r["role"] for r in _read(query, _READ_OVERRIDES, {"sys": system_id})
                 if r.get("model") and r.get("role")}
    matched = matched_assets_from_mappings(placed + unplaced, vms, system_id)
    hits = equipment_hits_from_mappings(placed + unplaced, vms, system_id)
    roles = classify_models(placed, hits, overrides, matched)
    asset_rows = _asset_writes(placed, unplaced, roles, walker)
    summary = _summary(system_id, placed + unplaced, roles, asset_rows, len(unplaced))
    coverage = coverage_json(placed, {r["id"]: r["level"] for r in asset_rows}, len(unplaced))
    summary["coverage"] = parse_coverage(coverage)
    if dry_run:
        return summary
    model_rows = [{"model": m, "role": r.role, "source": r.source, "evidence": r.evidence,
                   "needs_review": r.needs_review}
                  for m, r in roles.items() if m not in overrides]
    # Model nodes first: a failed asset write then leaves assets on the last
    # run's roles rather than ahead of their role nodes.
    _write_chunks(write, _WRITE_MODELS, model_rows,
                  {"sys": system_id, "now": datetime.now(timezone.utc).isoformat()})
    _write(write, _SWEEP_MODELS, {"sys": system_id, "models": sorted(roles)})
    _write(write, _LINK_MODELS, {"sys": system_id})
    _write_chunks(write, _WRITE_ASSETS, asset_rows, {"sys": system_id})
    summary["written"] = True
    _write_coverage(write, system_id, coverage, summary)
    return summary


def restore_system(system_id: str, *, write=None) -> dict:
    """Undo the pass for one system: walker levels back, roles and derived
    role nodes removed (steward roles kept), asset coverage removed. Raises
    :class:`RolePassError` on a failed write, including a failed coverage
    removal (after the role writes): coverage the seed can still read means
    the restore is incomplete."""
    write = write or _db()[1]
    with ROLE_PASS_LOCK:
        _write(write, _RESTORE_ASSETS, {"sys": system_id})
        _write(write, _RESTORE_MODELS, {"sys": system_id})
        category = _coverage_write(write, _CLEAR_COVERAGE, {"sys": system_id}, system_id)
    if category:
        raise RolePassError(f"asset coverage not removed: {category}")
    return {"system": system_id, "restored": True}


def asset_systems(query=None) -> list[str]:
    """Every system the pass visits: those with ``:Asset`` nodes, plus those
    whose System node still carries ``asset_coverage`` (a re-walk that dropped
    every asset leaves counts only the pass can remove). A system whose
    ``:System`` node is mid-removal (``removal_incomplete = true``) is left out
    so an unrelated provision's all-system post-pass does not re-run role
    writes on a half-removed system; assets with no owning ``:System`` node
    (orphans) are left out too. Skipped ids are logged at INFO with the
    reason."""
    query = query or _db()[0]
    kept, skipped = [], []
    for r in _read(query, _READ_SYSTEMS):
        sid = r.get("system")
        if not sid:
            continue
        if r.get("removal_incomplete") is True:
            skipped.append((sid, "removal in progress"))
        elif r.get("has_system") is not True:
            skipped.append((sid, "no owning :System node"))
        else:
            kept.append(sid)
    if skipped:
        logger.info("asset role pass skipped %d system(s): %s", len(skipped),
                    ", ".join(f"{sid} ({why})" for sid, why in sorted(skipped)))
    return sorted(set(kept))


def _each_system(fn, systems) -> dict:
    out = {}
    for s in systems:
        try:
            out[s] = fn(s)
        except RolePassError as e:
            out[s] = {"system": s, "error": str(e), "written": False}
    return {"systems": out}


def classify_all(*, dry_run: bool = False, query=None, write=None) -> dict:
    """:func:`classify_system` for every system that has ``:Asset`` nodes; a
    failed system is reported (``error``) and the others still run."""
    return _each_system(lambda s: classify_system(s, dry_run=dry_run, query=query, write=write),
                        asset_systems(query))


def restore_all(*, query=None, write=None) -> dict:
    return _each_system(lambda s: restore_system(s, write=write), asset_systems(query))


def run_after_provision() -> None:
    """Provision post-pass over every asset-bearing system (a rollback when
    the switch is off). Non-fatal: it never fails onboarding."""
    try:
        if not enabled():
            for sid, s in restore_all()["systems"].items():
                if s.get("error"):
                    logger.warning("asset role rollback for %s incomplete: %s", sid, s["error"])
            return
        for sid, s in classify_all()["systems"].items():
            if s.get("error"):
                logger.warning("asset roles for %s not written: %s", sid, s["error"])
                continue
            logger.info("asset roles for %s: %d assets, %d models, %d level changes, "
                        "%d need review, %d with an unresolvable parent", sid, s["assets"],
                        s["models"], s["level_changes"], len(s["needs_review"]),
                        s["ambiguous_parents"])
    except Exception as e:  # noqa: BLE001 - a role pass never fails onboarding
        logger.warning("asset role pass failed (non-fatal): %s: %s", type(e).__name__, e)
