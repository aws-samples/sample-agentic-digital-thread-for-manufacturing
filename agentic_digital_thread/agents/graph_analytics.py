# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Neptune Analytics endpoints — concept coverage, impact analysis, path resolution, vocabulary stats."""
import logging
from collections import defaultdict

from fastapi import APIRouter, Query

from . import joinpath_rule_link as _jp_rule
from .rule_status import rule_active as _rule_active
from .db import neptune_query

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/thread/analytics", tags=["thread-analytics"])


@router.get("/concept-coverage")
def concept_coverage():
    try:
        mappings = neptune_query(
            "MATCH (f:Field)-[r:MAPS_TO]->(c:Concept),"
            "      (f)<-[:HAS_FIELD]-(se:SchemaEntry)<-[:HAS_SCHEMA]-(s:System) "
            "RETURN c.id AS concept, c.name AS concept_name, c.isa95_level AS level,"
            "       s.id AS system_id, s.name AS system_name,"
            "       r.role AS role, r.confidence AS confidence"
        )
        all_concepts = neptune_query(
            "MATCH (c:Concept) RETURN c.id AS id, c.name AS name, c.isa95_level AS level, c.source AS source"
        )
        systems_raw = neptune_query(
            "MATCH (s:System) RETURN s.id AS id, s.name AS name"
        )
    except Exception as e:
        logger.warning("concept-coverage failed: %s", e)
        return {"concepts": [], "systems": [], "coverage": [], "total_concepts": 0, "unmapped_count": 0}

    # Build coverage map: concept -> system -> {role -> best_confidence}
    cov: dict[str, dict[str, dict[str, float]]] = defaultdict(lambda: defaultdict(dict))
    mapped_concept_ids: set[str] = set()
    for row in mappings:
        role = row.get("role")
        if not role:
            # Every MAPS_TO edge carries a role since the S5.2 two-layer re-onboard.
            # A missing role is a data-integrity violation in the graph, not a legacy
            # case to paper over — fail loud (no silent "identifier" fallback) so the
            # bad edge is found and fixed rather than silently mis-bucketed.
            raise RuntimeError(
                f"MAPS_TO edge missing required role: concept={row['concept']!r} "
                f"system={row['system_id']!r} — every mapping edge must carry a role."
            )
        conf = row.get("confidence") or 0
        key = (row["concept"], row["system_id"])
        # Keep highest confidence per role
        prev = cov[row["concept"]][row["system_id"]].get(role, 0)
        if conf > prev:
            cov[row["concept"]][row["system_id"]][role] = conf
        mapped_concept_ids.add(row["concept"])

    concepts_out = [
        {"id": r["id"], "name": r["name"], "level": r["level"], "source": r["source"]}
        for r in all_concepts if r["id"] in mapped_concept_ids
    ]
    coverage_out = [
        {"concept": concept, "system": system,
         "roles": [{"role": role, "confidence": conf} for role, conf in roles.items()]}
        for concept, systems in cov.items()
        for system, roles in systems.items()
    ]

    return {
        "concepts": concepts_out,
        "systems": [{"id": r["id"], "name": r["name"]} for r in systems_raw],
        "coverage": coverage_out,
        "total_concepts": len(all_concepts),
        "unmapped_count": len(all_concepts) - len(mapped_concept_ids),
    }


def _impact_direct_systems(hop1: list[dict]) -> dict[str, dict]:
    """Group hop1 rows by system, deduping fields on (name, role) — one entry per
    field even when it lives in several per-site tables (first table wins), so the
    drawer never renders 'I:asset_id, I:asset_id, I:asset_id'."""
    sys_map: dict[str, dict] = {}
    seen_fields: set[tuple] = set()
    for row in hop1:
        sid = row["system_id"]
        if sid not in sys_map:
            sys_map[sid] = {"system_id": sid, "system_name": row["system_name"], "fields": []}
        key = (sid, row["field"], row["role"])
        if key in seen_fields:
            continue
        seen_fields.add(key)
        sys_map[sid]["fields"].append({"name": row["field"], "table": row["table_name"], "role": row["role"]})
    return sys_map


def _impact_allowed_system_ids() -> set[str] | None:
    """Allowed system refs for JoinPath filtering (defense-in-depth vs stale graph
    artifacts): known System ids plus their short forms (id.split('-')[0] — learned
    JoinPaths may reference systems by short name). When any System node carries a
    status property, restrict to status=='onboarded'; otherwise (legacy graphs) or
    on query failure, return None to fail OPEN — availability-first for a
    read-only analytics view."""
    try:
        rows = neptune_query("MATCH (s:System) RETURN s.id AS id, s.status AS status")
    except Exception as e:
        logger.warning("impact System fetch failed — skipping stale-system filter: %s", e)
        return None
    if not rows:
        return None
    has_status = any(r.get("status") for r in rows)
    allowed: set[str] = set()
    for r in rows:
        if has_status and r.get("status") != "onboarded":
            continue
        sid = r.get("id") or ""
        if sid:
            allowed.add(sid)
            allowed.add(sid.split("-")[0])
    return allowed


def _impact_hop2(system_ids: list[str], allowed: set[str] | None) -> list[dict]:
    """JoinPath rows touching the direct systems, minus switched-off ones (their
    own status, or their IdentityRule's: joinpath_rule_link), minus degenerate self-joins
    (same system AND same field — pure noise; same-system cross-field joins are
    real intra-system links) and minus refs to stale/not-onboarded systems
    (allowed=None disables the system filter — fail open)."""
    if not system_ids:
        return []
    try:
        rows = neptune_query(
            "MATCH (jp:JoinPath) "
            "WHERE (jp.from_system IN $system_ids OR jp.to_system IN $system_ids) "
            "AND " + _jp_rule.rule_active("jp") + " "
            + _jp_rule.drop_if_rule_inactive() +
            "RETURN jp.from_system AS from_sys, jp.to_system AS to_sys,"
            "       jp.from_field AS from_field, jp.to_field AS to_field, jp.description AS description",
            {"system_ids": system_ids},
        )
    except Exception as e:
        logger.warning("impact hop2 failed: %s", e)
        return []
    out = []
    for r in rows:
        if r["from_sys"] == r["to_sys"] and r["from_field"] == r["to_field"]:
            continue  # degenerate self-join
        if allowed is not None and (r["from_sys"] not in allowed or r["to_sys"] not in allowed):
            continue  # stale/offboarded system reference
        out.append(r)
    return out


def _impact_hop3(connected_system_ids: list[str], concept_id: str) -> list[dict]:
    """Concepts reachable through the connected systems, deduped on (concept, system)."""
    if not connected_system_ids:
        return []
    try:
        hop3 = neptune_query(
            "MATCH (s:System)-[:HAS_SCHEMA]->(se:SchemaEntry)-[:HAS_FIELD]->(f:Field)-[r:MAPS_TO]->(c:Concept) "
            "WHERE s.id IN $connected_system_ids AND c.id <> $concept_id "
            "RETURN DISTINCT c.id AS concept, c.name AS concept_name, s.id AS system_id, r.role AS role",
            {"connected_system_ids": connected_system_ids, "concept_id": concept_id},
        )
    except Exception as e:
        logger.warning("impact hop3 failed: %s", e)
        return []
    seen: set[tuple] = set()
    reachable = []
    for r in hop3:
        key = (r["concept"], r["system_id"])
        if key not in seen:
            seen.add(key)
            reachable.append({"concept": r["concept"], "concept_name": r["concept_name"],
                               "via_system": r["system_id"], "role": r["role"]})
    return reachable


def _impact_extras(concept_id: str) -> tuple[list, list]:
    """Correlations + decision boundaries for the concept (best-effort)."""
    try:
        correlations = neptune_query(
            "MATCH (cor:Correlation) WHERE cor.concept_a = $concept_id OR cor.concept_b = $concept_id "
            "RETURN cor.id AS id, cor.concept_a AS concept_a, cor.concept_b AS concept_b,"
            "       cor.pattern AS pattern, cor.confidence AS confidence",
            {"concept_id": concept_id},
        )
        boundaries = neptune_query(
            "MATCH (d:DecisionBoundary) WHERE d.concept = $concept_id "
            "RETURN d.id AS id, d.rule AS rule, d.threshold AS threshold, d.action AS action",
            {"concept_id": concept_id},
        )
        return correlations, boundaries
    except Exception as e:
        logger.warning("impact correlations/boundaries failed: %s", e)
        return [], []


def _impact_concept_name(concept_id: str) -> str:
    try:
        cn_rows = neptune_query(
            "MATCH (c:Concept {id: $concept_id}) RETURN c.name AS name", {"concept_id": concept_id}
        )
        return cn_rows[0]["name"] if cn_rows else concept_id
    except Exception:
        return concept_id


@router.get("/impact/{concept_id}")
def impact(concept_id: str):
    empty = {
        "concept": {"id": concept_id, "name": ""},
        "direct_systems": [], "join_paths": [], "reachable_concepts": [],
        "correlations": [], "boundaries": [],
        "hop_summary": {"hop_1_systems": 0, "hop_2_systems": 0, "hop_3_concepts": 0},
    }
    try:
        hop1 = neptune_query(
            "MATCH (f:Field)-[r:MAPS_TO]->(c:Concept {id: $concept_id}),"
            "      (f)<-[:HAS_FIELD]-(se:SchemaEntry)<-[:HAS_SCHEMA]-(s:System) "
            "RETURN DISTINCT s.id AS system_id, s.name AS system_name, f.name AS field, r.role AS role, se.name AS table_name",
            {"concept_id": concept_id},
        )
    except Exception as e:
        logger.warning("impact hop1 failed: %s", e)
        return empty

    sys_map = _impact_direct_systems(hop1)
    system_ids = list(sys_map.keys())
    allowed = _impact_allowed_system_ids() if system_ids else None
    hop2 = _impact_hop2(system_ids, allowed)
    connected_system_ids = list({r["from_sys"] for r in hop2} | {r["to_sys"] for r in hop2} - set(system_ids))
    reachable = _impact_hop3(connected_system_ids, concept_id)
    correlations, boundaries = _impact_extras(concept_id)

    return {
        "concept": {"id": concept_id, "name": _impact_concept_name(concept_id)},
        "direct_systems": list(sys_map.values()),
        "join_paths": hop2,
        "reachable_concepts": reachable,
        "correlations": correlations,
        "boundaries": boundaries,
        "hop_summary": {
            "hop_1_systems": len(sys_map),
            "hop_2_systems": len(connected_system_ids),
            "hop_3_concepts": len(reachable),
        },
    }


def _path_queries(from_system: str, to_system: str) -> tuple[list, list, list]:
    """The three /paths graph reads (direct, two-hop indirect, join paths).
    ACTIVE rules only — soft-deactivated identity rules, and the JoinPaths
    switched off with them or on their own, are excluded from path discovery
    (they remain visible in the topology drawer for audit)."""
    direct_raw = neptune_query(
        "MATCH (ir:IdentityRule) "
        "WHERE ((ir.source_system = $from AND ir.target_system = $to)"
        "   OR (ir.source_system = $to AND ir.target_system = $from)) "
        f"  AND {_rule_active('ir')} "
        "RETURN ir.id AS id, ir.concept AS concept, ir.source_system AS source_system,"
        "       ir.source_pattern AS source_pattern, ir.target_system AS target_system,"
        "       ir.target_pattern AS target_pattern, ir.confidence AS confidence",
        {"from": from_system, "to": to_system},
    )
    indirect_raw = neptune_query(
        "MATCH (ir1:IdentityRule), (ir2:IdentityRule) "
        "WHERE ir1.source_system = $from AND ir2.target_system = $to AND ir1.target_system = ir2.source_system "
        f"  AND {_rule_active('ir1')} AND {_rule_active('ir2')} "
        "RETURN ir1.id AS hop1_id, ir1.concept AS hop1_concept, ir1.target_system AS via_system,"
        "       ir2.id AS hop2_id, ir2.concept AS hop2_concept,"
        "       ir1.confidence AS hop1_conf, ir2.confidence AS hop2_conf",
        {"from": from_system, "to": to_system},
    )
    join_raw = neptune_query(
        "MATCH (jp:JoinPath) "
        "WHERE ((jp.from_system = $from AND jp.to_system = $to)"
        "   OR (jp.from_system = $to AND jp.to_system = $from)) "
        "  AND " + _jp_rule.rule_active("jp") + " "
        + _jp_rule.drop_if_rule_inactive() +
        "RETURN jp.id AS id, jp.from_system AS from_sys, jp.to_system AS to_sys,"
        "       jp.from_field AS from_field, jp.to_field AS to_field, jp.description AS description",
        {"from": from_system, "to": to_system},
    )
    return direct_raw, indirect_raw, join_raw


@router.get("/paths")
def paths(from_system: str = Query(...), to_system: str = Query(...)):
    empty = {
        "from_system": from_system, "to_system": to_system,
        "direct_paths": [], "indirect_paths": [], "join_paths": [], "total_paths": 0,
    }
    try:
        direct_raw, indirect_raw, join_raw = _path_queries(from_system, to_system)
    except Exception as e:
        logger.warning("paths query failed: %s", e)
        return empty

    direct = sorted(
        [{"rule_id": r["id"], "concept": r["concept"], "source_pattern": r["source_pattern"],
          "target_pattern": r["target_pattern"], "confidence": r.get("confidence", 0)}
         for r in direct_raw],
        key=lambda x: x["confidence"], reverse=True,
    )
    indirect = sorted(
        [{"hops": [
              {"rule_id": r["hop1_id"], "concept": r["hop1_concept"], "via_system": r["via_system"]},
              {"rule_id": r["hop2_id"], "concept": r["hop2_concept"], "via_system": to_system},
          ],
          "aggregate_confidence": (r.get("hop1_conf") or 0) * (r.get("hop2_conf") or 0)}
         for r in indirect_raw],
        key=lambda x: x["aggregate_confidence"], reverse=True,
    )

    return {
        "from_system": from_system,
        "to_system": to_system,
        "direct_paths": direct,
        "indirect_paths": indirect,
        "join_paths": join_raw,
        "total_paths": len(direct) + len(indirect) + len(join_raw),
    }


@router.get("/vocabulary")
def vocabulary():
    empty = {
        "by_source": [], "total_concepts": 0, "mapped_concepts": 0,
        "unmapped_concepts": 0, "total_fields": 0, "coverage_pct": 0.0,
    }
    try:
        rows = neptune_query(
            "MATCH (c:Concept) "
            "OPTIONAL MATCH (c)<-[r:MAPS_TO]-(f:Field) "
            "RETURN c.source AS source, count(DISTINCT c.id) AS concept_count, count(DISTINCT f) AS mapped_fields "
            "ORDER BY source"
        )
        mapped_row = neptune_query(
            "MATCH (c:Concept)<-[:MAPS_TO]-(:Field) RETURN count(DISTINCT c.id) AS mapped_concepts"
        )
    except Exception as e:
        logger.warning("vocabulary query failed: %s", e)
        return empty

    total_concepts = sum(r.get("concept_count", 0) for r in rows)
    total_fields = sum(r.get("mapped_fields", 0) for r in rows)
    mapped_concepts = mapped_row[0].get("mapped_concepts", 0) if mapped_row else 0

    return {
        "by_source": [
            {"source": r["source"], "concepts": r.get("concept_count", 0), "mapped_fields": r.get("mapped_fields", 0)}
            for r in rows
        ],
        "total_concepts": total_concepts,
        "mapped_concepts": mapped_concepts,
        "unmapped_concepts": total_concepts - mapped_concepts,
        "total_fields": total_fields,
        "coverage_pct": round(mapped_concepts / total_concepts * 100, 1) if total_concepts else 0.0,
    }
