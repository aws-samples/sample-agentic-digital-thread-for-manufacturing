<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# Orchestrator topology is graph-derived, never hardcoded in prompts

---
Status: accepted
---

The Investigation Trace orchestrator (the Digital Thread Orchestrator agent) derives the set
of systems, plants, and their query guidance **exclusively** from the knowledge graph at query
time — `MATCH (s:System) …` plus each node's `query_guidance` / `capabilities` / `tool_prefix`.
No plant name, system id, or system count is hardcoded in the orchestrator system prompt or in
the suggested ("sample") questions. The agent must answer correctly for *any* onboarded set, not
only the demo's Milwaukee/Detroit/Cedar Rapids topology.

## Context

The resolution *mechanism* is already dynamic: Step 1 of the orchestrator workflow discovers
systems via `MATCH (s:System)` and drives itself off graph-stored guidance, and tool
availability is dynamic (AgentCore gateway auto-discovery in cloud mode, per-onboard MCP
registration in local mode). But two pieces of *scaffolding* embed the demo's specific topology:

- the orchestrator system prompt's example queries (e.g. `MATCH (site:Asset {name:'Milwaukee'})`),
- the canned sample questions in `agents/prompts.py` (e.g. "query ALL THREE MES systems
  (Milwaukee, Detroit, Cedar Rapids)").

If the onboarded set changes, discovery adapts but the guidance and suggestions go stale and
mislead the model — a silent inconsistency with the dynamic mechanism, and the same
"static assumption vs. live data" contradiction ADR 0001 removes from the dashboard render path.

## Considered Options

- **Leave hardcoded plant names as harmless examples:** rejected. They bias the model toward a
  fixed topology, can produce queries against systems that are not onboarded, and quietly rot
  when the demo's system set changes.
- **Hand-maintain a parallel list of plants/systems for the prompt:** rejected. A second source
  of truth for "what's onboarded" guarantees drift from the graph.

## Consequences

- Example/guidance text in the orchestrator prompt is generic (placeholder-shaped) or assembled
  from `MATCH (s:System)`; sample questions are derived from the onboarded set, not fixed strings.
- The orchestrator is correct for any onboarded topology; adding or removing a system needs no
  prompt edits.
- Slightly more work at prompt-assembly time (a graph read to template guidance) in exchange for
  removing a hidden topology coupling.
- Tracked as a item.
