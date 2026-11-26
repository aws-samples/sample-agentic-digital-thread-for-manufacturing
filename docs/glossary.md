<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# Glossary

This is the single source of truth for terminology used across the Agentic
Digital Thread documentation. Where any other document and this glossary
disagree, this glossary wins.

---

## Product and resource names

| Term | What it means |
|------|---------------|
| **Agentic Digital Thread (ADT)** | The name of this sample and its web application (`branding.json` → `company`). |
| **`agentic_digital_thread/`** | The directory that holds the ADT application code. Runtimes and resources created by the current code use the `adt` prefix (for example the `adt_orchestrator` runtime and `adt-*` tables). |
| **ADT demo (demo dataset)** | The **fictitious demo company** whose sample data ships with this repository (see the trademark note in the root README; all names and data are invented). The brand deliberately survives the project's rename to ADT because it is demo *content*, not engine identity: the bundled sample database and schema are named `adt_demo` (seeded by `lambda-adt-seed/`, routed by `agents/demo/db_routing.py`). Some **live physical AWS resource names** also retain the historical `adt` prefix to avoid replacing deployed resources: AWS Systems Manager parameters (`/adt/*`), AWS Secrets Manager names (`adt/*`), the Amazon Neptune Analytics graph (`adt`), the AgentCore gateways (`adt-query-gw`, `adt-onboard-gw`), and the system-agent Amazon ECR repository. The ADT app's Amazon ECS task-role AWS CloudFormation construct ID **`AdtTaskRole`** (`stacks/adt_compute_stack.py`) is likewise a retained pre-rename name: changing the construct ID would replace the deployed role. |

---

## The three things called "i3X"

"i3X" refers to three distinct things, plus the underlying protocol:

| Term | What it is |
|------|------------|
| **i3X protocol** | Industrial Information Interoperability eXchange—an open specification for browsing industrial namespaces, object types, objects, and live values. See [i3x.dev](https://i3x.dev). |
| **HighByte i3X feature** | A capability inside HighByte Intelligence Hub that exposes its Unified Namespace through an i3X-compliant API. Disabled by default; enabled in **Settings → I3X** in the HighByte UI. |
| **AWS IoT SiteWise i3X Server** | A **separate repository** (`sitewise-i3x-server`) that puts an i3X API in front of AWS IoT SiteWise. Optional; not part of this repository and not deployed by this CDK app. |

When this documentation says "enable i3X in HighByte," it means the HighByte
i3X feature. The onboarding tile for the historian is labeled
"AWS IoT SiteWise (i3X)" in the application UI.

---

## Plants and system topology

The demo models **four** manufacturing sites. The first three are the plants
named in `branding.json` and `agentic_digital_thread/agents/catalog.py`;
Indianapolis is the fourth site
([ADR-0083](adr/0083-indianapolis-fourth-site.md)), and its systems are
registered from `agentic_digital_thread/agents/demo/workshop.py`.

| Plant | Site code | Industry modeled |
|-------|-----------|------------------|
| **Milwaukee** | MKE | Beverage bottling |
| **Detroit** | DET | Automotive parts |
| **Cedar Rapids** | CR | Food processing |
| **Indianapolis** | IND | Precision machining |

> **Note:** `CR` versus `CRR`. The `CR` site code appears in ERP location codes (for
> example `CR-FG`, `CR-WH-RAW`), but Cedar Rapids **equipment and asset tags**
> in the CMMS and OT data use a `CRR` prefix (for example `CRR-MIXR-001`,
> `CRR-OVEN-002`). This is a deliberate cross-system identifier divergence in
> the sample data—exactly the kind of gap the digital thread resolves.

The systems available for onboarding (`catalog.py`):

| System | Scope | Backing store |
|--------|-------|---------------|
| **ERP** (Enterprise Resource Planning) | All sites (one shared system) | Amazon Aurora, schema `erp_data` |
| **Milwaukee MES** (Manufacturing Execution System) | Milwaukee only | Amazon RDS, schema `mes_milwaukee` |
| **Detroit MES** | Detroit only | Amazon RDS, schema `mes_detroit` |
| **Cedar Rapids MES** | Cedar Rapids only | Amazon RDS, schema `mes_cedarrapids` |
| **CMMS** (Computerized Maintenance Management System) | All sites (one shared system) | Amazon RDS, schema `cmms_maintenance` |
| **PLM** (Product Lifecycle Management) | All sites (one shared system) | Amazon RDS, schema `plm_engineering` |
| **HighByte Intelligence Hub** | All sites (live OT namespace) | MCP server (current state only) |
| **AWS IoT SiteWise (Direct)** | All sites (historical OT) | AWS IoT SiteWise asset hierarchy |
| **AWS IoT SiteWise (i3X)** | All sites (historical OT) | Onboard any i3X server through **Connect New System** (endpoint URL + bearer token); `SITEWISE_I3X_URL` only pre-wires the demo quick-link |

> This is **one ERP, three MES, one CMMS, one PLM**, plus the OT layer, not a
> per-plant ERP/CMMS layout. The fourth site adds the Indianapolis MES and
> Indianapolis Plant Maintenance systems
> ([architecture.md](architecture.md#seeded-schemas-versus-registered-systems)).

---

## Core concepts

| Term | Definition |
|------|------------|
| **Knowledge graph (rules engine)** | The Amazon Neptune Analytics graph. It stores **metadata only**—which systems exist, what fields they have, how to translate identifiers between systems, and canonical manufacturing concepts. It never stores production data (no batch numbers, order IDs, or sensor readings). It tells an agent *where* to look and *how* to translate. |
| **Identity rule** | A learned mapping that translates an identifier from one system's convention to another's (for example, MES `B-2847` to the corresponding ERP batch). Stored as `IdentityRule` nodes in the Amazon Neptune Analytics graph. |
| **Concept** | A canonical manufacturing term (batch, equipment, product, work order, OEE, and so on) that fields from different systems map to. |
| **Onboarding** | The process of connecting a system: the AI discovers its schema, maps fields to concepts, and proposes identity rules. Required after every fresh deployment before the chat can answer questions. |
| **Digital thread** | The cross-system trace that links the same physical thing (a batch, an order, an asset) across ERP, MES, CMMS, PLM, and OT despite different naming conventions. |

---

## Platform and AWS terms

| Term | Definition |
|------|------------|
| **Amazon Bedrock AgentCore** | The runtime service for the agents. **AgentCore Runtime** hosts the orchestrator and system agents; **AgentCore Gateway** exposes tools over MCP with IAM and OAuth authentication; **AgentCore Memory** persists conversation context. |
| **Strands Agents** | The open-source agentic framework the orchestrator and system agents are built on. See [strands-agents/sdk-python](https://github.com/strands-agents/sdk-python). |
| **MCP (Model Context Protocol)** | The protocol agents use to call tools—both the AgentCore Gateway and the HighByte MCP server speak it. |
| **UNS (Unified Namespace)** | The consolidated, hierarchical view that HighByte Intelligence Hub provides over live data from OPC UA, MES, and ERP sources. Queryable as an MCP tool. |
| **OPC UA** | Open Platform Communications Unified Architecture—the industrial protocol the simulated factory servers use. The servers are reachable only within the private VPC; demo connections are unencrypted and anonymous for simplicity. Enable OPC UA message security (`SignAndEncrypt`) and authentication for production OT. |
| **OEE** | Overall Equipment Effectiveness—availability × performance × quality. A common demo question target. |

---

## Where things live

| Path | Contents |
|------|----------|
| `app.py`, `stacks/` | The CDK app and all stack definitions. |
| `agentic_digital_thread/` | The ADT application (container image, agents, web UI). |
| `lambda-adt-seed/` | SQL schema and seed data for the MES/CMMS/PLM database. |
| `lambda-db-init/`, `erp_simulation/` | ERP database initialization and simulation. |
| `highbyte/` | HighByte assets, OPC UA dataset generation, and AWS IoT SiteWise model deployment. |
| `docs/` | This documentation set. |
