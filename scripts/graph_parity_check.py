#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
graph_parity_check.py — DRY / in-sync guard for the local graph backend.

The app runs the SAME openCypher against both Amazon Neptune Analytics (cloud,
default) and a local Memgraph (GRAPH_BACKEND=local) — there is ONE set of
queries, switched inside agents/db.py (see ADR 0003). This script replays a
representative corpus of the app's real query *shapes* against the configured
local graph (MEMGRAPH_URI) so dialect drift is caught early: if someone adds a
Neptune-ism the local engine can't run (or vice versa), this fails.

It is NOT a substitute for the real-Neptune fidelity gate (a deploy smoke test).
It is the cheap, local, always-runnable half of "stay in sync".

Usage:
    docker compose -f docker-compose.local.yml up -d   # memgraph on :7687
    .venv-local/bin/python scripts/graph_parity_check.py
Exit code 0 = all representative shapes ran; 1 = at least one FAIL.
"""
import os
import sys

try:
    from neo4j import GraphDatabase
except ImportError:
    print("neo4j driver missing — install the local extra: pip install -e './agentic_digital_thread[local]'")
    sys.exit(2)

URI = os.environ.get("MEMGRAPH_URI", "bolt://localhost:7687")

# Representative shapes drawn from agents/{server,onboard,self_learning,
# graph_analytics,resolve,catalog}.py. Keep in sync when a genuinely new query
# *shape* (not just new literals) is introduced.
CASES = [
    ("wipe", "MATCH (n) DETACH DELETE n", None, "write"),
    ("seed MERGE ON CREATE SET",
     "MERGE (c:Concept {id: $cid}) ON CREATE SET c.name=$cid, c.description=$d, c.source='core'",
     {"cid": "batch", "d": "x"}, "write"),
    ("create System",
     "MERGE (s:System {id:$sid}) SET s.name=$n, s.schema=$sc, s.plant=$p, s.status='onboarded'",
     {"sid": "mes-det", "n": "Detroit MES", "sc": "mes_detroit", "p": "Detroit"}, "write"),
    ("System-HAS_SCHEMA->SchemaEntry",
     "MATCH (s:System {id:$sid}) MERGE (s)-[:HAS_SCHEMA]->(se:SchemaEntry {id:$se}) SET se.name=$n",
     {"sid": "mes-det", "se": "mes-det:production_runs", "n": "production_runs"}, "write"),
    ("SchemaEntry-HAS_FIELD->Field",
     "MATCH (se:SchemaEntry {id:$se}) MERGE (se)-[:HAS_FIELD]->(f:Field {id:$fid}) SET f.name=$n",
     {"se": "mes-det:production_runs", "fid": "mes-det:production_runs:run_id", "n": "run_id"}, "write"),
    ("Field-MAPS_TO{role,conf}->Concept",
     "MATCH (f:Field {id:$fid}),(c:Concept {id:$cid}) MERGE (f)-[r:MAPS_TO]->(c) SET r.role=$role, r.confidence=$conf",
     {"fid": "mes-det:production_runs:run_id", "cid": "batch", "role": "identifier", "conf": 0.95}, "write"),
    ("create IdentityRule",
     "CREATE (ir:IdentityRule {id:$id, concept:$c, source_system:$s, target_system:$t, confidence:$cf})",
     {"id": "ir1", "c": "batch", "s": "mes", "t": "erp", "cf": 0.9}, "write"),
    ("concept-coverage (multi-pattern MATCH)",
     "MATCH (f:Field)-[r:MAPS_TO]->(c:Concept),(f)<-[:HAS_FIELD]-(se:SchemaEntry)<-[:HAS_SCHEMA]-(s:System) "  # deliberate implicit concatenation of one multi-line string  # nosemgrep: string-concat-in-list
     "RETURN c.id AS concept, s.id AS system_id, r.role AS role, r.confidence AS confidence", None, "read"),
    ("vocabulary (OPTIONAL MATCH + count(DISTINCT) + ORDER BY)",
     "MATCH (c:Concept) OPTIONAL MATCH (c)<-[r:MAPS_TO]-(f:Field) "  # deliberate implicit concatenation of one multi-line string  # nosemgrep: string-concat-in-list
     "RETURN c.source AS source, count(DISTINCT c.id) AS cc, count(DISTINCT f) AS mf ORDER BY source", None, "read"),
    ("impact hop2 (IN $list + OR)",
     "MATCH (jp:JoinPath) WHERE jp.from_system IN $ids OR jp.to_system IN $ids RETURN jp.id AS id",
     {"ids": ["mes-det", "erp"]}, "read"),
    ("paths direct (compound WHERE)",
     "MATCH (ir:IdentityRule) WHERE (ir.source_system=$f AND ir.target_system=$t) OR (ir.source_system=$t AND ir.target_system=$f) "  # deliberate implicit concatenation of one multi-line string  # nosemgrep: string-concat-in-list
     "RETURN ir.id AS id, ir.confidence AS confidence", {"f": "mes", "t": "erp"}, "read"),
    ("restore systems (DISTINCT + rel)",
     "MATCH (s:System)-[:HAS_SCHEMA]->(se:SchemaEntry) RETURN DISTINCT s.id AS id, s.schema AS schema", None, "read"),
    ("whole-node return (graph-viz)", "MATCH (n) WHERE n:System OR n:Concept RETURN n LIMIT 5", None, "read"),
    ("orphan cleanup (NOT (pattern) + IS NULL)",
     "MATCH (c:Concept) WHERE NOT (c)<-[:MAPS_TO]-() AND c.isa95_level IS NULL AND c.source IS NULL DETACH DELETE c",
     None, "write"),
    ("chained DETACH DELETE",
     "MATCH (s:System {id:$sid})-[:HAS_SCHEMA]->(se:SchemaEntry)-[:HAS_FIELD]->(f:Field) DETACH DELETE f",
     {"sid": "mes-det"}, "write"),
]


def main() -> int:
    try:
        drv = GraphDatabase.driver(URI, auth=("", ""))
    except Exception as e:  # noqa: BLE001
        print(f"Cannot connect to {URI}: {e}")
        return 2
    failures = 0
    print(f"{'STATUS':6} STATEMENT")
    print("-" * 70)
    for name, cypher, params, _kind in CASES:
        try:
            with drv.session() as s:
                list(s.run(cypher, **(params or {})))
            print(f"{'PASS':6} {name}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"{'FAIL':6} {name} -> {type(e).__name__}: {str(e)[:120]}")
    drv.close()
    print("-" * 70)
    print(f"{'OK' if not failures else 'DRIFT DETECTED'}: {len(CASES) - failures}/{len(CASES)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
