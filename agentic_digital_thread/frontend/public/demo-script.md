<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# Agentic Digital Thread — guided walkthrough

> **Audience:** Anyone presenting the sample to manufacturing and technology stakeholders
> **Duration:** 10–15 minutes

---

## Before you start

Open the following tabs in your browser before you begin:

1. **Dashboard** → [/#dashboard](/#dashboard)
2. From the **Admin panel** (shield icon, top-right of the banner), open each quick-link in a new tab:
   - **HighByte Console** — the OPC UA Unified Namespace (if the optional OT/UNS layer is deployed)
   - **ERP Simulation Dashboard** — live order/production/MES metrics

> **Warning:** Do not choose **Disconnect** on the Onboard page. Reconnecting requires a full onboarding process.

---

## Demo flow

### 1 · Dashboard — the starting point

**View:** [/#dashboard](/#dashboard)

> *"Let's start with the dashboard. This is what a plant manager or operations lead would see first thing in the morning."*

**Operations Summary & KPI Cards** (left side)

- These are your traditional dashboard widgets — Orders, Production, Quality, Lines, Suppliers.
- Each card is tied to a specific onboarded system and only shows what that system exposes.
- Choose any card to drill into the underlying data table.
- **Key point:** This is exactly what every dashboard does today — it shows you what it was programmed to show, nothing more.

**AI Operations Briefing** (right side, wider panel)

- This is different. Every 5 minutes, the **Orchestrator Agent** queries *across all onboarded systems simultaneously* and produces a holistic, plain-English briefing.
- It surfaces things no single system would catch on its own — like a quality issue in MES correlating with a backorder spike in ERP.
- Specific action items are called out so the operator knows exactly what to follow up on.

> *"The difference is: the KPI cards tell you what happened in one system. The AI briefing tells you what it means across all of them."*

**Manufacturing Intelligence Panel** (chat panel, right side)

- This is where you interact directly with the orchestrator agent.
- Use the **persona dropdowns** to switch between roles — Plant Manager, Quality Engineer, Digital Thread Builder, and so on.
- Each persona has pre-built queries relevant to their job. Select one from the dropdown to run it instantly.
- You can also type your own question in natural language.

> *"Ask it something like: 'Why is Line 2 at Detroit showing reduced throughput?' — it will query MES, CMMS, and the knowledge graph to give you a root-cause answer."*

---

### 2 · Demo mode — hands-free showcase

**Button:** `▶ Demo` in the top navigation bar

- Choosing **Demo** starts an automated loop that cycles through all the pre-built Manufacturing Intelligence queries one by one.
- The Investigation view (Architecture tab) updates in real time showing exactly what the agents are doing.
- Choose **Demo** again to stop.

> *"If you want to leave this running unattended, choose Demo and it will keep cycling through queries automatically."*

---

### 3 · Agents — the running agent fleet

**View:** [Admin → AgentCore Metrics](/admin.html#tab-agents)

- Shows live metrics for every agent running in **Amazon Bedrock AgentCore**.
- Each onboarded system has its own dedicated System Agent — ERP, MES (Milwaukee, Detroit, Cedar Rapids, and Indianapolis under the workshop profile), CMMS, PLM, AWS IoT SiteWise.
- The **Orchestrator Agent** sits above them all, routing queries and synthesizing answers.
- Each agent card shows its LLM model (Anthropic Claude Haiku 4.5 for system agents and Anthropic Claude Sonnet 5 for the orchestrator by default) and metrics: invocations, latency, tool calls.
- The **Pricing Reference** section shows token usage and cost breakdown per model.

> *"This is the agent fleet. Each system agent knows its own schema deeply. The orchestrator knows how to ask the right questions to the right agents and stitch the answers together."*

---

### 4 · Architecture — see the knowledge graph

**View:** [/#architecture](/#architecture)

Four tabs:

**System Map**
- Visual map of all onboarded systems and how they connect to the AI Orchestrator.
- Shows the ISA-95 levels: Level 4 (ERP/PLM), Level 3 (MES/CMMS), Level 2 (SiteWise/Historian).
- Hover over any node for details. When a query runs, edges animate to show data flow.

**Investigation**
- This view suits a technical audience.
- When you run a query from the Manufacturing Intelligence panel, switch to this tab.
- The agent's reasoning trace appears in real time — which systems it queried, what it found, and how it assembled the answer.
- The graph animates as each tool call completes.

> *"This is full transparency into what the AI is actually doing. Nothing is hidden."*

**Knowledge Graph**
- The complete graph stored in **Amazon Neptune Analytics**.
- Nodes represent Systems, Plants, Tables, Fields, Concepts, Assets, Asset Models, Identity Rules, Join Paths, and Value Mappings.
- Edges represent relationships — how a field in MES maps to the same concept as a field in ERP, how assets connect to their hierarchy.
- The **Graph Explorer** panel (right side) lets you filter by node and edge type using checkboxes. Choose any node for details and AI-powered explanation.
- This is what enables the agent to join data across systems without hardcoded integrations.

**Self-Learning**
- Shows discoveries the agent has made during normal query execution.
- When the orchestrator resolves an identity across systems (for example, maps a CMMS asset tag to a SiteWise asset name), it proposes a new mapping.
- Each proposal shows: type (JoinPath, ValueMapping, IdentityRule), source/target fields, confidence score, and the query context that triggered it.
- You can **Accept** (writes to the knowledge graph permanently), **Reject** (discards), or **Accept All** pending proposals.
- The **Intelligence** metrics at the top show how many proposals have been made and what percentage of queries are now deterministic (resolved through the graph rather than re-discovered each time).

> *"This is how the system gets smarter over time. Every query teaches it something new. Accept a mapping once, and it never has to re-discover that relationship again."*

---

### 5 · Onboard — how systems get connected

**View:** [/#onboard](/#onboard)

- This is where new systems are connected to the platform.
- The onboarding agent walks through 5 steps: Discover → Map → Resolve → Provision → Verify.
- It explores the system's schema, maps columns to ISA-95 manufacturing concepts, discovers cross-system identity rules, and writes everything to the knowledge graph.
- Supports PostgreSQL, REST APIs, MCP Servers (HighByte UNS), AWS IoT SiteWise (Direct), and Amazon S3 Tables.
- **Choose any connected system name** to see its tables and concept mappings.

> **Warning:** Do not choose **Disconnect** — this removes the system from the knowledge graph and requires a full re-onboard.

> *"Once a system is onboarded, it stays connected. The agent learns the schema once and uses that knowledge for every query going forward."*

---

## Key messages to land

| Theme | What to Say |
|-------|-------------|
| **Not another dashboard** | "Traditional dashboards show you what they were programmed to show. This platform lets you ask questions that span every system at once." |
| **No hardcoded integrations** | "The knowledge graph is how the agent understands relationships between systems. There's no ETL pipeline, no custom connector — just schema understanding." |
| **Self-learning** | "Every query teaches the system something new. It proposes identity mappings, and once accepted, those become permanent knowledge — deterministic, not re-discovered." |
| **Transparency** | "The Investigation view shows every query the agent ran, every system it touched. You can audit every answer." |
| **Built on AWS** | "This runs on Amazon Bedrock AgentCore, Amazon Neptune Analytics, AWS IoT SiteWise, and Amazon ECS on AWS Fargate — managed AWS services that scale with demand." |
| **Real data, live** | "The ERP and MES simulators are running right now — orders, production runs, downtime events. The OPC UA data flows through HighByte into SiteWise while the simulators run." |

---

## Common questions

**"How long does onboarding take?"**
> It depends on the system: a larger schema takes longer, and the first onboarding of a system also creates its agent runtime. The agent reads the schema, samples the data, maps to manufacturing concepts, and builds the knowledge graph automatically.

**"Does it work with our existing systems?"**
> If it has a database, API, or OPC UA endpoint, yes. The platform supports PostgreSQL, REST APIs, MCP protocol, AWS IoT SiteWise, and Amazon S3 Tables.

**"What models does it use?"**
> The orchestrator uses Anthropic Claude Sonnet through Amazon Bedrock. System agents use Anthropic Claude Haiku by default — configurable per system from the Admin panel.

**"Is the data leaving our environment?"**
> Data stays in your AWS account. The agents query your systems directly, and only the retrieved context needed for a response is sent to Amazon Bedrock in your Region.

**"How does it handle multiple sites?"**
> Each site's MES is onboarded as a separate system agent. The knowledge graph stores plant/site hierarchy from SiteWise, so the orchestrator knows which agent to query for which location.

**"What's the knowledge graph for?"**
> It stores the semantic understanding of your data — what each field means, how systems relate, identity mappings between them. Without it, the agent would have to re-discover relationships on every query.

---

*Guided walkthrough — Agentic Digital Thread*
