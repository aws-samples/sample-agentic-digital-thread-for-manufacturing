<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# ADT demo — Integration Suite (S0.2)

The **real** deterministic resolver exercised against the real backends it
depends on: a **live** Neptune graph (read via `agents.db.neptune_query`) and a
**local** disposable PostgreSQL value-read lane. Plus the **mapping-quality eval**
(ADR 0012) that scores live `MAPS_TO` edges against an authored ground-truth key.

> **Orchestration + the full COLD → WARM → HOT reproducibility loop, env capture,
> safety guards, GATE-G0 mapping, and deferred work are documented in
> [`../harness/README.md`](../harness/README.md). Read that first.**

## What's here

| Path | Purpose |
|---|---|
| `docker-compose.yml` | Disposable `postgres:16`, published on `127.0.0.1:5432` **only** (never `0.0.0.0`), ephemeral storage. |
| `seed_pg.py` | Idempotent local seeder; applies the unchanged repo-root `lambda-adt-seed/{001_schemas,004_cmms_maintenance}.sql`. Prints `no local Postgres — skipping` and exits `0` when PG is down. |
| `conftest.py` | `live_graph` / `local_postgres` / `resolve_fn` / `no_llm_guard` fixtures — each gated by a **skip, never a hard failure**. |
| `test_resolver_integration.py` | concept → SQL → value loop, incl. the `inventory-level` S0.1 F9 tracer. |
| `mapping_eval.py` | ADR-0012 scorer + CLI (`python -m tests.integration.mapping_eval --system-id cmms`). |
| `test_mapping_eval.py` | asserts must-map @ 100% + overall ≥ 0.90 (skips cleanly with no graph). |
| `expected_mappings/cmms.yaml` | the authored ground-truth key — **written from field MEANING, never from LLM output** (ADR 0012 integrity rule). |
| `test_story_lookup_pg.py` | ADR-0085 facilitator story lookup on real PostgreSQL: builds a db-init ERP database + a seed database (`lambda-adt-seed` 001/003/005/011), `EXPLAIN`s every lookup statement, runs frozen and live (after the story history) end to end. Gated by **`ERP_SIM_TEST_PG_DSN`** (a throwaway server it may `CREATE`/`DROP DATABASE` on; the same variable as `erp_simulation/tests`), skipped otherwise — independent of the compose Postgres below. |

## Run

```bash
# from agentic_digital_thread/
export POSTGRES_PASSWORD='<your-local-test-password>'   # required by the compose file
docker compose -f tests/integration/docker-compose.yml up -d
python tests/integration/seed_pg.py
source tests/harness/.harness-env            # for the live-graph cases

pytest tests/integration -m integration
docker compose -f tests/integration/docker-compose.yml down -v
```

The story-lookup case needs no compose stack, only a disposable server:

```bash
ERP_SIM_TEST_PG_DSN=postgresql://postgres:<pw>@127.0.0.1:<port>/postgres \
  PYTHONPATH=. python -m pytest tests/integration/test_story_lookup_pg.py -q
```

With neither a reachable graph nor a local Postgres, `pytest tests/integration`
collects clean and reports **all-skipped, zero errors** (no AWS/PG call at
collection). See [`../harness/README.md`](../harness/README.md) for the skip
matrix, safety guards, and the mapping-eval thresholds.
