<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# ADT demo — Cloud Integration Harness (S0.2)

A **cloud-first, reproducible, hardened** integration harness. It makes the
**real** deployment reproducible: an idempotent path from nothing/dirty to a
verified known-good graph — `(re)deploy/reset → onboard cmms → verify shape →
score mapping quality` — and runs the deterministic resolver against that real
graph plus a **local** PostgreSQL value-read lane.

Cost is not the concern here; thoroughness, reproducibility, and a short
feedback cycle are. The offline/deterministic graph backend is **explicitly
deferred** — see [Deferred work](#deferred-work).

> **Layout.** Reproducibility orchestration lives here in `tests/harness/`
> (`harness.sh` + the `_guard` / `capture_env` / `graph_reset` helpers). The
> resolver integration suite, the local PostgreSQL substrate, and the
> mapping-quality eval live in `tests/integration/` (see its `README.md`).

## The reproducibility loop — COLD → WARM → HOT

Three tiers, each a strict superset of feedback below it. Run from
`agentic_digital_thread/`.

| Tier | Scope | Command | Time |
|---|---|---|---|
| **COLD** | Full one-script deploy: 7/9 CDK stacks (9 with `--tar`) + core-concept Neptune seed + orchestrator runtime `READY`, then capture env. The **only** tier that can land NEW seed SQL (e.g. cmms2) into the deployed demo RDS bundle. | `tests/harness/harness.sh cold --profile <dev> [--tar <hb.tar>]` | tens of minutes |
| **WARM** | Reset Neptune **without a stack redeploy** → re-onboard `cmms` to runtime-`READY` → verify shape **and** run the mapping-quality eval. | `tests/harness/harness.sh warm --yes` | minutes (LLM-bound) |
| **HOT** | Re-run the resolver integration suite + graph-shape audit + mapping eval against the existing graph + local PG. | `tests/harness/harness.sh hot` | seconds–low minutes |

`hot` expands to:

```bash
pytest tests/integration tests/audit -m "integration or audit"
```

**WARM graph-reset-without-redeploy is feasible** — that is the whole point of
the WARM tier. Two routes (see [`graph_reset.py`](graph_reset.py)):

- **endpoint** (default): `POST {ADT_BASE_URL}/api/onboard/reset-all`, the
  same server route the app's "Reset" button calls (`agents.onboard.onboard_reset_all_route`),
  then polls `GET /api/onboard/reset-all/{job_id}` until the job ends (an app of
  the previous release answers 405, and the harness falls back to the
  synchronous `POST /api/onboard/reset`): it
  wipes the onboarding labels, reseeds `CORE_CONCEPT_DEFINITIONS`, and cleans up
  the AgentCore runtimes/targets.
- **direct** (fallback when the app is unreachable): `DETACH DELETE` the exact
  onboarding label list, then re-`MERGE` the core vocabulary from
  `catalog.core_concept_merges()` (the same single seed source `deploy.sh` step
  `[2/6]` and `onboard_reset` run, ADR 0020). Uses `agents.db.neptune_write` only.

`teardown [--yes]` tears the environment down (wraps the repo-root `cleanup.sh`
+ `docker compose down -v`).

## How the cloud graph is (re)created / reset / verified

| Phase | COLD | WARM |
|---|---|---|
| (re)create / reset graph | `deploy.sh` → core-concept seed (`[2/6]`) + orchestrator runtime `READY` (`[4/6]`) | `graph_reset.py reset` (endpoint or direct) |
| onboard `cmms` | Playwright onboarding (`tests/e2e/test_onboarding.py -m e2e`) — `status=="onboarded"` flips **only** when the runtime reaches `READY` | same Playwright onboarding |
| verify shape | `tests/audit` `run_audit` (read-only) | `graph_reset.py verify` → `run_audit` |
| verify **quality** | mapping eval | mapping eval (`tests/integration/mapping_eval.py`) |

Shape verification is delegated to the existing read-only audit
(`tests.audit.audit_onboarding_graph.run_audit`), which writes
`tests/audit/last-run-report.json` and exits non-zero only on `status=="fail"`.

## Environment / config the runner needs

Capture everything from a live deployment into a sourceable, **gitignored**
`tests/harness/.harness-env` (written `chmod 600`; the password is referenced by
name only, never printed):

```bash
python -m tests.harness.capture_env --profile <dev> --region us-east-1
source tests/harness/.harness-env
```

| Var | Source (mirrors `deploy.sh` `cfn_output()`) | Consumed by |
|---|---|---|
| `GRAPH_IDENTIFIER` | `ADTDatabaseStack.NeptuneGraphId` | `agents.db.neptune_query` / `neptune_write` (graph reads/writes) |
| `AWS_REGION` | resolved Region | `agents.db` Neptune client + the guards |
| `ADT_BASE_URL` | `ADTComputeStack.ADTCloudFrontUrl` | onboarding (Playwright) + `graph_reset` endpoint mode |
| `ADT_USERNAME` | constant `ADT` (the demo Cognito user; ambient override wins) | e2e auth/nav tests + `graph_reset` |
| `ADT_PASSWORD` | Secrets Manager `adt-demo-password` | onboarding login + `graph_reset` reset login |
| `AWS_PROFILE` | resolved profile (if any) | boto3 session for all of the above |

- **`POST /api/auth/login` takes `{"email", "password"}`** — the JSON field is
  named `email` (it carries the Cognito *username*, e.g. `ADT`). Scripts that
  send `{"username": ...}` get a 422; see `agents/auth/routes.py::LoginRequest`.
- **AWS credentials** come from the ambient SSO / profile / env chain via
  boto3 — **never committed**.
- **`POSTGRES_*`** defaults already match the container
  (`agents.db.get_conn_params()` → `localhost:5432`, db `adt_demo`,
  user/password `postgres`), so the local value-read lane needs no extra config.
- `tests/harness/.harness-env` is **gitignored** (`.gitignore` line:
  `agentic_digital_thread/tests/harness/.harness-env`).

## Safety — every destructive op is gated AND guarded

Destructive operations are `reset`, `teardown`, and `redeploy`. Each one, before
touching any state, must clear **all** of the following (see
[`_guard.py`](_guard.py)):

1. **Opt-in** — `require_opt_in()`: proceeds only with an explicit `--yes` flag
   **or** `ADT_HARNESS_ALLOW_DESTRUCTIVE=1`. No opt-in ⇒ refuse, no write.
2. **`assert_dev_account()`** — STS `get_caller_identity` must resolve to the dev
   account `111122223333` **and** Region `us-east-1` (overridable via
   `ADT_HARNESS_DEV_ACCOUNT` / `ADT_HARNESS_DEV_REGION`). Wrong account/Region ⇒
   `GuardError`.
3. **`assert_target_graph()`** — `GRAPH_IDENTIFIER` must be set, must not be the
   placeholder `"adt"`, and must match `expected_graph_id` when
   given. Otherwise ⇒ `GuardError`.

The guards run in that order (cheapest first, AWS calls last) and are pure /
read-only. Additional invariants:

- **Local PostgreSQL binds `127.0.0.1` only** (never `0.0.0.0`) — it must not be
  reachable off-host.
- **No public ingress** is created by the harness.
- **No LLM in resolve/render** (ADR-0001): the `no_llm_guard` fixture turns any
  `agents.db.bedrock_call` / `bedrock_stream` during a resolve/render path into a
  hard error. The mapping eval is `neptune_query`-only and adds **no** new graph
  writer.

## Skip behavior — green without cloud or PostgreSQL

Every external dependency is **gated by a skip, never a hard failure**, mirroring
the e2e `ADT_PASSWORD` pattern:

- **No reachable graph** (missing `GRAPH_IDENTIFIER` / `AWS_REGION` / creds, or
  nothing onboarded) ⇒ the `live_graph` fixture **skips**; the mapping-eval CLI
  prints `nothing_to_eval` and exits `0`.
- **No local PostgreSQL** ⇒ the `local_postgres` fixture **skips**; `seed_pg.py`
  prints `no local Postgres — skipping` and exits `0`.

With neither graph nor PG, `pytest tests/integration` collects clean and reports
**all-skipped, zero errors** — and no AWS/PG call fires at collection time.

## Mapping-quality eval (ADR 0012)

Structural checks (`≥1 MAPS_TO`, status onboarded, value non-null) prove a
mapping is *present*, not *correct*. Because the mapping is LLM-produced and
non-deterministic, exact-match is too brittle and structural-only proves nothing.
So `tests/integration/mapping_eval.py` scores the **live** `MAPS_TO` edges
against an **authored ground-truth key** with thresholds:

- **Expected-mapping key** — `tests/integration/expected_mappings/cmms.yaml`,
  keyed `"<table>.<column>"` → an **acceptable set** of concept ids (a field may
  legitimately map to one of several defensible concepts; only a mapping outside
  the set is a real miss). Every concept id is validated against
  `catalog.CORE_CONCEPTS`.
- **Two-level threshold + confidence gate** — a small **must-map tracer set**
  (the 9 fields the demo narrative depends on, e.g.
  `assets.asset_tag → equipment-id`,
  `spare_parts.quantity_on_hand → inventory-level`) must pass at **100%**; an
  **overall accuracy floor of ≥ 0.90** covers the rest; every must-map field
  needs `confidence ≥ 0.60`.
- **Demo-artifact score line** — the eval prints, e.g.,
  `cmms mapping eval: mapped N/M fields correctly (≥90%), all 9 critical fields
  exact`, and writes `mapping-eval-cmms.json`. The score doubles as a demo
  artifact, not just a test result.

```bash
python -m tests.integration.mapping_eval --system-id cmms
```

> **INTEGRITY RULE (ADR 0012, verbatim intent).** The ground-truth key is
> authored **from what the fields MEAN, never by transcribing observed LLM
> output**. A key written from model output is a circular, self-fulfilling eval
> and is explicitly disallowed.

**Where it runs.** The eval runs in the cloud **WARM** and **HOT** loops; it is
**NOT a gating CI check**. Threshold flakiness against a non-deterministic mapper
is not a release blocker. (ADR 0012 ties this stance to the offline/deterministic
lane being deferred — once that lane exists, the eval can move into gating CI.)

## GATE G0 — both halves

GATE G0 wording (Phase-0): *"we can programmatically read one real
concept value, a local verification harness exists, and onboarding a system
end-to-end (graph seeded **and** its AgentCore system-agent runtime reaches
READY) completes via the one-script deploy."*

- **"a local verification harness exists"** ✅ — `tests/integration/` runs the
  real resolver against a live Neptune graph + a local PostgreSQL via the tiered
  `harness.sh`, with documented credential-free skips.
- **"onboarding end-to-end via the one-script deploy (graph seeded AND runtime
  READY)"** ✅ — COLD `deploy.sh` seeds the core concepts and waits for the
  `adt_orchestrator` runtime to reach `READY`; Playwright onboarding flips
  `status=="onboarded"` **only** when the AgentCore runtime is `READY`
  (ADR-0010). WARM reproduces the graph half without a redeploy.
- **"read one real concept value"** ✅ — the resolver integration tracer reads
  `inventory-level → cmms_maintenance.spare_parts.quantity_on_hand` from the
  local PG (closes S0.1 F9).
- **Mapping quality (ADR 0012)** ✅ — the eval scores live `MAPS_TO` vs the
  authored key (must-map @ 100% + overall ≥ 0.90 + confidence ≥ 0.60).

> **Honest caveat.** The offline/deterministic, credential-free **graph** lane is
> **DEFERRED**. GATE G0's "local verification harness" is satisfied cloud-first:
> the resolver and PG lanes are local and deterministic, but the graph reads hit
> a live dev Neptune. There is no in-memory/Dockerized graph double yet.

## Deferred work

1. **Offline + deterministic + credential-free graph lane** — an in-memory
   openCypher double or a Dockerized engine (Memgraph / Neo4j) with a
   `neptune_query` → Bolt adapter, built backwards from the cloud graph shape.
   Once it exists, the mapping eval can move into **gating CI** (ADR-0012 ties
   the "not gating CI" stance to this lane being deferred).
2. **ADR-0011 F1 resolver-level cross-system guard** — `resolve.py` reads
   `RESOLVES` while onboarding writes `LINKS_CONCEPT`; the read-side fix is an
   S1.x change. The two-system guard is **graph-level + xfail** today;
   un-`xfail` `test_f1_resolver_cross_system_resolve` when S1.x F1 lands.
3. **Expected-mapping key drift** — `expected_mappings/*.yaml` are authored
   alongside the seed schemas; if a seed schema changes, its key must be
   re-authored **from meaning** (never from output).

## See also

- `tests/integration/README.md` — resolver integration suite, local PG
  substrate, and the mapping-quality eval (scorer + keys).
- `tests/e2e/README.md` — Playwright onboarding/browser suite.
- `docs/adr/0012-mapping-quality-eval.md` — the mapping-eval spec.
- `docs/adr/0011-*` — canonical graph shape the eval/audit read.
