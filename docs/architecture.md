<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# Architecture

> Terminology in this document follows [`glossary.md`](glossary.md).

## The problem

Manufacturing companies run dozens of systems across multiple plants—ERP,
MES, CMMS, PLM—each from a different vendor with its own naming conventions.
The same batch might be `B-2847` in the MES, a different identifier in the ERP,
and another in the CMMS. Connecting these systems traditionally takes months of
custom integration per system.

## The approach

The Agentic Digital Thread (ADT) uses an Amazon Neptune Analytics knowledge
graph as a **rules engine**—not a data store—combined with AI-driven
onboarding. Architecturally the graph sits as a *semantic layer*—the
meaning-and-translation tier between the agents and the systems. The two terms
answer different questions: "rules engine, not a data store" states what is
*inside* the graph (metadata only, never production rows); "semantic layer"
states *where it sits*. Neither implies class hierarchies, inference, or
entity-relationship modeling—see
[ADR-0057](adr/0057-semantic-layer-export-boundary.md) for the boundary:

1. **Onboard a system.** The AI discovers the system's schema, maps its fields
   to canonical manufacturing concepts, and proposes identity rules for
   translating identifiers to and from other systems. Deep-dive:
   [`onboarding.md`](onboarding.md).
2. **Ask a question.** The orchestrator queries Neptune Analytics for the relevant
   identity rules, translates identifiers between systems, and queries the
   source databases directly for the live answer.
3. **Confirm a rule.** When brute-force matching discovers a new connection,
   the system proposes it as a precise rule. Confirming it makes the graph
   smarter for the next question. Deep-dive: [`self-learning.md`](self-learning.md).

### What the knowledge graph stores

Metadata only:

- **System** nodes—what systems exist and how to reach them.
- **Field** nodes—what columns exist and which concept each maps to.
- **IdentityRule** nodes—how to translate identifiers between systems.
- **Concept** nodes—the canonical manufacturing vocabulary (batch,
  equipment, product, work order, and so on).

### What it never stores

No batch numbers, order IDs, sensor readings, or any production data. The graph
tells the agent **where** to look and **how** to translate; the data itself
stays in the source systems.

This applies to OT time-series too: AWS IoT SiteWise event/metric asset-model
**properties** are discovered and concept-mapped into the graph as `Field` nodes
(metadata), but their **values are read live at query time and never persisted**.
See [ADR-0037](adr/0037-sitewise-event-models-richer-discovery.md).

---

## Runtime data flow

The demo source systems run on Amazon Aurora PostgreSQL and Amazon Relational
Database Service (Amazon RDS) for PostgreSQL; a deployment can onboard any
other supported source in their place.

```
Browser
  │
  ▼
Amazon CloudFront ──(AWS WAF)──► internal ALB ──► Amazon ECS on AWS Fargate (ADT app)
                                                          │
                                                          ▼
                                          Amazon Bedrock AgentCore Gateway (MCP)
                                          ┌───────────────┴───────────────┐
                                          ▼                               ▼
                                  Orchestrator                      System agents
                                  (Anthropic Claude,                (Anthropic Claude Haiku,
                                   on AgentCore Runtime)              on AgentCore Runtime)
                                          │                               │
                          ┌───────────────┼───────────────┐               │
                          ▼               ▼               ▼               ▼
                  Amazon Neptune    Aurora / RDS      OT sources      (per-system
                  Analytics         PostgreSQL        (HighByte UNS,   tools created
                  (rules engine)    (ERP/MES/         AWS IoT          during onboarding)
                                     CMMS/PLM)        SiteWise)
```

The orchestrator (a [Strands Agents](https://github.com/strands-agents/sdk-python)
agent) reasons about a question, consults the knowledge graph for identity
rules and authority rules, then dispatches to system agents whose tools query
the source systems. Tools are created dynamically as systems are onboarded.

The ADT Amazon CloudFront distribution uses Amazon Cognito Authorization@Edge by default:
browser requests run `CheckAuth` and redirect to the Cognito hosted UI, while
`/api/*` and `/health` bypass the edge function so application API auth keeps
returning 401 rather than an OAuth redirect. The optional ERP dashboard and
HighByte UI distributions also use Authorization@Edge. HighByte additionally
uses an AWS WAF web ACL with the Common, KnownBadInputs, and IpReputation
managed rule groups; its `/mcp*`, `/i3x*`, `/i3X*`, and `/data/*` machine paths
bypass browser auth and enforce bearer authentication at the origin. With a
custom ADT domain, CDK also provisions the enabled sibling aliases
`erp.<custom-domain>` and `highbyte.<custom-domain>`, including certificates,
A/AAAA records, and Cognito callback/logout URLs. See
[ADR-0070](adr/0070-cognito-authorization-at-edge-default.md).

---

## CDK stacks

All stacks share one VPC from `ADTNetworkStack`. They are defined in `app.py` and
deploy in dependency order.

| Stack (AWS CloudFormation name) | Purpose |
|-----------------------------|---------|
| `ADTNetworkStack` | VPC, public/private subnets, NAT gateway, security groups, flow logs. |
| `ADTERPDatabaseStack` | *Opt-in (`--simulator`).* Amazon Aurora Serverless v2 PostgreSQL—live ERP data (orders, production, suppliers). |
| `ADTERPLambdaStack` | *Opt-in (`--simulator`).* An AWS Lambda function, driven by Amazon EventBridge, that seeds and simulates ERP data. |
| `ADTERPDashboardStack` | *Opt-in (`--simulator`).* Amazon ECS on AWS Fargate: the ERP Streamlit dashboard, behind Amazon CloudFront with Cognito Authorization@Edge. Also carries the `erp-sitewise-degradation-feed` AWS Lambda function (see [SiteWise degradation feed](#sitewise-degradation-feed) below). In a `live` workshop account the dashboard's feed button is replaced by a read-only caption, because the story site's OPC UA server, read by Shop Floor Connectivity (SFC), follows the story clock and no Lambda function writes to SiteWise ([ADR-0092](adr/0092-live-story-telemetry-from-the-opcua-server.md)); a live synth does not deploy the feed function. |
| `ADTSeedDataStack` | *Opt-in (`--seed`).* The `adt_demo` Amazon RDS PostgreSQL + the static-seed Lambda function (bundled sample MES/CMMS/PLM/historian schemas). Not part of the base engine. |
| `ADTDatabaseStack` | **Engine.** Amazon Neptune Analytics graph (knowledge graph) + Amazon DynamoDB (self-learning mappings). No RDS, no demo data. |
| `ADTAgentCoreStack` | Amazon ECR repository, AgentCore Gateway `adt-query-gw` (MCP, Cognito CUSTOM_JWT), the bridge-only `adt-quick-gw` when `quick_api_enabled` or `quick_connector_enabled` (`--quick-api` / `--quick-connector`; [ADR-0071](adr/0071-dedicated-gateway-for-quick-query-bridge.md), [ADR-0093](adr/0093-quick-connector-in-the-workshop-package.md)), Amazon Cognito user pools (app login + machine-to-machine), IAM roles. |
| `ADTOnboardingStack` | **Engine.** Onboarding control plane—onboarding runtime IAM role, onboarding gateway (`CUSTOM_JWT`), onboarding runtime security group. Provisioning a system runs in the app (the Step Functions provisioner that lived here had no caller and was removed on 2026-10-29). The `adt_onboarding` runtime itself is created imperatively by `deploy.sh` (ADR-0010). |
| `ADTCertificateStack` | *Optional (custom domain—`--domain` + hosted-zone flags).* AWS Certificate Manager certificates for the app distribution (plus `highbyte.<domain>` / `erp.<domain>` when those stacks are enabled). Pinned to `us-east-1` because CloudFront requires certificates there; the ARN crosses into `ADTComputeStack` through cross-Region references. |
| `ADTComputeStack` | Amazon ECS on AWS Fargate (ADT app + agents), internal Application Load Balancer (ALB), and Amazon CloudFront with AWS WAF; Cognito Authorization@Edge is the default browser entry while `/api/*` and `/health` bypass it. |
| `QuickApiGatewayStack` | *Opt-in (`--quick-api`; not sticky).* Amazon API Gateway front door for the AWS Quick web connector and SigV4 MCP clients, fronting the internal ALB; injects the `x-quick-auth` gateway token, a Secrets Manager secret generated in `ADTComputeStack`. See [`quick-integration.md`](quick-integration.md). |
| `ADTHighByteOpcuaStack` | *Optional.* Four simulated OPC UA servers (Milwaukee, Detroit, Cedar Rapids, Indianapolis; [ADR-0083](adr/0083-indianapolis-fourth-site.md)) + the HighByte Intelligence Hub instance (public `highbyte/intelligence-hub` image; runs with `ACCEPT_EULA=Y` only after the deployer accepts the HighByte EULA with `--accept-highbyte-eula`), behind CloudFront with Cognito Authorization@Edge and the standard AWS WAF managed rules. When the four servers run in `ADTOpcuaServersStack` (next row), this stack holds only the hub, which reads the servers from that stack. |
| `ADTOpcuaServersStack` | *Optional split (`-c opcua_servers_stack=true`, with HighByte).* The four simulated OPC UA servers in a stack of their own, so SFC starts without waiting for the hub's CloudFront distribution. `deploy.sh` never sets it, and without HighByte the servers stay in `ADTHighByteOpcuaStack`. An in-place update can't move the servers between the two stacks: a custom resource refuses it and the update rolls back. |
| `ADTIdentityCenterStack` | *Optional.* IAM Identity Center permission sets for demo portal access. Deploy with `-c deploy_identity_center=true`. |

> Use the **CloudFormation stack names** (`ADTComputeStack`, `ADTDatabaseStack`,
> and so on)—these are what appear in the console and in `aws cloudformation`
> commands.

**Deploy modes** (see [`deployment.md`](deployment.md#deploy-modes) for detail
and [ADR-0030](adr/0030-demo-data-is-opt-in-base-default.md) for the rationale):

- **Base (5 stacks, default):** engine only—`ADTNetworkStack`, `ADTDatabaseStack`
  (Neptune + DynamoDB), `ADTAgentCoreStack`, `ADTOnboardingStack`, `ADTComputeStack`. **No source-system
  RDS, no demo data**—the engine has no dependency on or awareness of seed/
  simulator. Onboard your own systems.
- **Seed (6 stacks):** `--seed` adds **`ADTSeedDataStack`** (the `adt_demo`
  RDS) loaded with the static sample schemas.
- **Full (9 stacks):** `--simulator` adds `ADTERPDatabaseStack`, `ADTERPLambdaStack`,
  and `ADTERPDashboardStack` (the live simulation). Implies `--seed`.
- **With HighByte (+1 stack):** adds `ADTHighByteOpcuaStack`, which runs the public
  `highbyte/intelligence-hub` image from Docker Hub (it runs with `ACCEPT_EULA=Y`,
  set only after the deployer accepts the HighByte EULA) plus four OPC UA simulators
  feeding AWS IoT SiteWise. Enabled with `--highbyte --accept-highbyte-eula` (or
  `-c highbyte_enabled=true -c highbyte_accept_eula=true`). It is **independent of the demo
  data**—it deploys on top of any data mode (base + HighByte = **6 stacks**, full +
  HighByte = **10 stacks**). When `--simulator` is also set, the HighByte setup
  additionally wires the ERP and historian PostgreSQL connections. The HighByte choice is
  sticky across later deployments (tracked in AWS Systems Manager); the
  `--seed`/`--simulator` data flags are **not** sticky. `deploy.sh` always keeps
  the servers and the hub in this one stack. A direct CDK deploy that passes both
  `-c highbyte_enabled=true` and `-c opcua_servers_stack=true` puts the servers
  in `ADTOpcuaServersStack` instead
  (see the stack table above).

---

## Data model

The demo seeds synthetic data for four manufacturing sites: the three branded
plants (Milwaukee, Detroit and Cedar Rapids) and Indianapolis, the fourth site
([ADR-0083](adr/0083-indianapolis-fourth-site.md)). The **registered** demo
system topology is **one shared ERP, three per-plant MES, one shared CMMS, one
shared PLM**, plus the OT layer (authoritative per
`agentic_digital_thread/agents/demo/systems.py::DEMO_SYSTEMS`). The
Indianapolis systems are registered separately, from
`agentic_digital_thread/agents/demo/workshop.py`; see
[Seeded schemas versus registered systems](#seeded-schemas-versus-registered-systems).

| System | Scope | Example identifiers |
|--------|-------|---------------------|
| **ERP** | All sites | batch/lot tracking, purchase orders, shipments, invoices |
| **Milwaukee MES** | Milwaukee (Bottling) | work orders `WO-YYYYMMDD-XXXXX`, runs `RUN-YYYYMMDD-XXXXX`, lots `LOT-YYYYMMDD-XXX` |
| **Detroit MES** | Detroit (AutoParts) | `DET-RUN-0001`, `CNC-MILL-04` |
| **Cedar Rapids MES** | Cedar Rapids (Food) | per-line work orders, HACCP checks |
| **CMMS** | All sites | `WO-MKE-2026-0041`, asset serials like `KF2000-MKE-01` |
| **PLM** | All sites | products, BOMs, engineering specs and revisions |
| **OT (HighByte / SiteWise)** | All sites | equipment tags like `MKE-WASH-001`, `DET-CNC1-001`, `CRR-MIXR-001` |

The same physical thing carries a different identifier in each system—that is
exactly the gap the digital thread closes. Equipment serial numbers follow a
`MODEL-SITE-SEQ` convention (for example `KF2000-MKE-01`).

> Each system is authoritative for certain concepts (MES for execution data,
> ERP for commercial data, CMMS for maintenance, PLM for engineering, the
> historian for sensor data). These authority rules are encoded in
> `catalog.py` (`CONCEPT_AUTHORITY`) and written into Neptune during onboarding.

### Seeded schemas versus registered systems

The static seed (`lambda-adt-seed/*.sql`, loaded into the `adt_demo` RDS by
`ADTSeedDataStack`) contains **more schemas than the platform registers**.
Every `--seed` deploy loads an Indianapolis defect-chain layer—`mes_line3`,
`cmms_indianapolis`, `iot_historian`—plus legacy Detroit schemas, alongside
the schemas the demo six read. There is no plant ERP: every site's orders,
lots and shipments live in the one corporate ERP
([ADR-0084](adr/0084-one-erp-and-the-workshop-story-setting.md)).

The **Indianapolis Plant Maintenance** system (`cmms_indianapolis`, seeded by
`lambda-adt-seed/011_cmms_indianapolis.sql`) and the live **Indianapolis
MES** (`mes_indianapolis`, the simulator's plant MES view in the ERP
database) are **registered** on every non-workshop demo deploy
(`agents/demo/workshop.py::DEMO_PLANT_SYSTEMS`) but onboarded by no bulk
default; the workshop registers the maintenance system in both stories and
pre-onboards the live MES in the `live` story. The frozen storyline schemas—`mes_line3` (as `mes-ind`) and
`iot_historian`—are registered, and pre-onboarded by the workshop seed, only
in the `frozen` workshop story (the `ADT_WORKSHOP` flag and the
`workshop_story` setting, which only the workshop build sets). On a
plain `--seed` or `--simulator` deploy that frozen data exists in the
database yet is invisible to the platform: it appears in no catalog, no
agent, no answer. That is by design, not a bug—there is no missing
onboarding step to look for.

### The B-2847 defect chain (seed)

The seed carries one deliberately coherent cross-system defect story,
resolved through Neptune identity rules (never foreign keys):

- **MES** (`003_mes_line3.sql`): batch `B-2847` (work order `WO-9921`) ran on
  `L3-M1` 2026-02-10 06:00–11:23 and was **Stopped** by SPC diameter drift.
- **Historian** (`009_historian.sql`): the same machine
  (`5-Axis-Milling-CMX-441`) shows escalating vibration Feb 10–14, recovering
  after the bearing replacement.
- **CMMS** (`011_cmms_indianapolis.sql`): asset `1001-CMX-441` carries the
  **deferred** 90-day spindle-bearing PM (work order `1182`, originally due
  2025-12-30—42 days before the B-2847 run, never performed) and the
  emergency bearing replacement (work order `1195`, Feb 14 22:30 → Feb 15
  12:30, inside the MES idle window).
- **PLM** (`005_plm_engineering.sql`): B-2847's product `7742` has three
  revisions and exactly one **Released**, `PN-7742-REV-C`. The identity rule
  discovered at onboarding (`value_core_match`) is `{id}` ↔
  `PN-{id}-REV-{*}` ([ADR-0086](adr/0086-identity-rule-wildcard-for-one-to-many-cores.md)):
  it finds every revision of the MES `product_code`, and the answer picks
  the Released one from the PLM's status.

The customer impact comes from the one corporate ERP, not the seed: its
machined product `MACH-HSG-7742` carries `engineering_part_number`
`PN-7742-REV-C` (`lambda-db-init/handler.py::PLM_PART_XREF`), and the
simulator's `MACHINING` customers order it. Those orders are live simulator
data, so their count and value drift.

The chain is **pinned by `tests/unit/test_seed_defect_chain.py`** (offline—it
parses the SQL as text; it also refuses a reintroduced plant-ERP schema). Do
not move B-2847's dates, identifiers, its product's PLM revision, or the PM
deferral without updating that guard. The `live` workshop story does not use
this chain; it writes its own deploy-time history into the ERP and the plant
maintenance schema ([ADR-0085](adr/0085-live-story-clock-history-and-lookup.md)).

### SiteWise degradation feed

> **Note:** With the optional OPC UA servers deployed (`--opcua`, which
> `--highbyte` and `--sfc` imply; [ADR-0083](adr/0083-indianapolis-fourth-site.md)),
> the Indianapolis OPC UA server also carries the CMX-441 story
> (`highbyte/generate_datasets.py`, `machining_tags(..., cmx441=True)`): vibration
> climbs to 2.8 mm/s across every dataset loop, the mill stops for a fixed
> bearing-replacement downtime, and the MES→CMMS bridge files the matching
> emergency work order in `cmms_indianapolis`. Outside a live account that
> playback follows the wall clock.

In a live account the feed button described below is inert
([ADR-0092](adr/0092-live-story-telemetry-from-the-opcua-server.md)). The
dashboard shows the caption "This plant's sensor feed follows the plant
clock shown in the ADT app header." instead. A live synth deploys no feed
Lambda function and sets `OT_FEED_SOURCE=opcua` on the dashboard container,
so the caption needs no database read. Without that variable the caption
appears only when a `simulation_config.story_anchor_epoch` row exists
(`streamlit_dashboard._story_clock_active`, keyed by the anchor row, never
by the story setting), and a database error falls back to the button. In a
live account the story site's OPC UA server, read into SiteWise by SFC, is
the only source of the mill's telemetry: it plays its dataset from the
story clock's anchor, which the simulator copies into the Systems Manager
parameter `/adt/workshop/story-anchor-epoch` every cycle. No Lambda function
writes to SiteWise in a live account. Frozen and demo accounts keep the
button below.

Every `--simulator` deploy ships a licensing-free path that writes one
machine's degradation history **directly into AWS IoT SiteWise**, with no
HighByte required. The ERP
dashboard's **📡 OT Telemetry (SiteWise)** control row has one button, **📉
Start machine degradation feed**, which invokes the
`erp-sitewise-degradation-feed` Lambda function (`lambda-sitewise-feed/handler.py`,
deployed by `ADTERPDashboardStack`). It backfills a 72-hour window ending now
(max 167 h) for SiteWise asset `5-Axis-Milling-CMX-441`: vibration climbing
1.8 → 2.8 mm/s (X axis; Y/Z track it lower) plus coolant temperature
68 → 84 °C, at 5-minute
samples (864 points per property at 72 h)—mirroring the seeded B-2847
historian story above. The asset is resolved by name from the `CNC` asset
model (created if absent; the models come from
`highbyte/DeploySitewiseModels.py`, which `deploy.sh` runs unconditionally).
The feed is idempotent only within one 5-minute bucket: timestamps are
grid-aligned (SiteWise upserts, never duplicates), but the ramp is anchored
to each invocation's window, so a later re-click rewrites the overlapping
window with a re-anchored ramp and extends it to the new "now". It is
**simulator-gated, not workshop-gated**—see [`highbyte.md`](highbyte.md)
for how it relates to the HighByte pipeline.

**The feed is not live telemetry.** It writes one asset
(`5-Axis-Milling-CMX-441`: `Vibration_X`, `Vibration_Y`, `Vibration_Z` and
`CoolantTemp`), it writes a one-off backfill rather than a stream, and it
runs only when someone clicks the dashboard button. Nothing in a plain
`--simulator` deploy writes telemetry for the plant lines into SiteWise, so
questions about live line or equipment telemetry have no data to answer from.
Live plant telemetry comes only from the OPC UA servers, read into SiteWise by
SFC (`--sfc`; [ADR-0082](adr/0082-sfc-writes-sitewise-for-unattended-deployments.md),
[ADR-0092](adr/0092-live-story-telemetry-from-the-opcua-server.md)) or by
HighByte (`--highbyte`). For a demo that asks live telemetry questions, deploy
with `--simulator --sfc`.

---

## Demo flow

The walkthrough: start from zero, onboard systems one at a time, and see the
orchestrator answer progressively richer cross-system questions.

1. **ERP only** → *"What orders are open?"* → single-system answer.
2. **+ Milwaukee MES** → same question → now includes production status and
   batch detail.
3. **+ CMMS** → *"Why did that batch stop?"* → traces to an equipment failure
   and its maintenance history. (The seeded `B-2847` chain is a separate,
   workshop-facing story—see [Seeded schemas versus registered systems](#seeded-schemas-versus-registered-systems).)
4. **+ PLM** → *"What spec was involved?"* → traces to the engineering tolerance.
5. **+ OT (HighByte / SiteWise)** → *"What is the current OEE for Milwaukee?"*
   → answers from live telemetry and historical trends. Live telemetry needs
   `--sfc` or `--highbyte`; see [SiteWise degradation feed](#sitewise-degradation-feed).

See [`post-deploy.md`](post-deploy.md) for the onboarding procedure (required
after every fresh deployment).

---

## Key application APIs

The ADT app is a FastAPI server (`agentic_digital_thread/agents/server.py`).
The `/api/onboard/*` pipeline is documented in depth in
[`onboarding.md`](onboarding.md); the proposal/learning surface in
[`self-learning.md`](self-learning.md).

| Endpoint | Purpose |
|----------|---------|
| `POST /api/ask` | Chat with the orchestrator (server-sent events streaming). |
| `GET /api/onboard/catalog` | List all registered systems (onboarded and not-yet-onboarded). |
| `POST /api/onboard/discover` | AI-powered schema discovery. |
| `POST /api/onboard/map` | AI concept mapping. |
| `POST /api/onboard/resolve` | AI identity-rule proposals. |
| `POST /api/onboard/provision` | Write to Neptune and create the dynamic tool. |
| `POST /api/onboard/reset` | Global reset: clear the graph and start fresh. Answers when the reset has ended (the contract older bundles rely on during a rolling deploy; a later release can retire it in favour of `/reset-all`). |
| `POST /api/onboard/reset-all` | The same reset as a background job: answers `{"status": "started", "job_id", "full"}` (409/503 when refused, nothing started). A task of an older release answers 405, and the UI falls back to `POST /reset`. |
| `GET /api/onboard/reset-all/{job_id}` | Poll a Reset All job, read consistently from the shared table: `none` / `running` / `done` / `error` (an error carries a `reason`; `reset_incomplete`, `reset_lease_lost` and `reset_stopped` are retryable); 503 when it cannot be read. |
| `POST /api/onboard/reset/{system}` | Remove ONE system (graph node + edges, agent runtime, catalog entry). Always runs as a background job (`{"status": "started"}`); a `wait` parameter is ignored. |
| `GET /api/onboard/reset/{system}/status` | Poll a removal, read consistently from the shared table: `none` / `running` / `done` / `error`; 503 when it cannot be read. |
| `POST /api/metrics/resolve` | Deterministically resolve a metric to a value + lineage (see [metric-layer.md](metric-layer.md)). |
| `POST /api/thread/resolve` | Cross-system entity resolution. |
| `POST /api/thread/learn` | Confirm a proposed identity rule. Steward (admin) only; anyone else gets 403. |
| `GET /api/onboard/status` | Onboarded systems (id, name, plant, type, connection type, status; never a connection string, settings or secret pointer) and suggested questions. |

---

## Observability and continuous evaluation

Two independent, opt-in layers watch agent quality in production:

- **Agent observability** (`--observability`, sticky): Amazon CloudWatch Transaction
  Search span ingestion + an indexing rule power the per-session drill-down on
  the Agents page. Off by default because span ingestion/storage/indexing
  bills continuously—see the
  [deployment flag](deployment.md#optional---observability-flag-sticky-default-off).
- **Managed AgentCore evaluations** (automatic when the orchestrator runtime
  exists): an `AWS::BedrockAgentCore::OnlineEvaluationConfig` owned by
  `ADTComputeStack` (`stacks/_evaluations.py`) continuously samples ~10% of
  orchestrator traces and scores them with the built-in
  `Faithfulness` / `ResponseRelevance` / `Correctness` judges and the
  code-based `adt_figure_check` (an AWS Lambda function that checks, without a
  model, that each total or count the answer states is in what that turn's
  system agents returned; it re-queries nothing, so it cannot catch agents
  that were wrong together). Results land in
  the `/aws/bedrock-agentcore/evaluations/results/*` log group and surface on
  the Agents fleet page ([agents-fleet-metrics.md](agents-fleet-metrics.md)).
  This is continuous sampled scoring, **not** a CI gate—the deployed
  grounding gate remains `scripts/smoke_prompts.py`.

## AWS services used

- **Amazon Bedrock** (and AgentCore Runtime/Gateway/Memory)—agent reasoning,
  schema analysis, concept mapping, identity-rule proposals.
- **Amazon Neptune Analytics**—the knowledge graph (rules engine).
- **Amazon Aurora** and **Amazon RDS** (PostgreSQL)—source-system data.
- **Amazon DynamoDB**—stores proposed identity-rule mappings for the
  self-learning graph.
- **Amazon ECS on AWS Fargate**—container hosting for the app and agents.
- **Amazon CloudFront** + **AWS WAF**—edge delivery and protection.
- **Amazon Cognito**—user authentication and machine-to-machine OAuth.
- **AWS IoT SiteWise**—OT asset hierarchy and time-series telemetry.
- **AWS Secrets Manager**, **AWS Systems Manager**, **Amazon CloudWatch**—secrets,
  configuration, and observability.

For the security control mapping, see the root [README](../README.md#security).
