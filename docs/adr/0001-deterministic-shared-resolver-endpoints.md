<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# Deterministic shared resolver endpoints for KPIs

---
Status: accepted
---

KPI values are computed by deterministic resolver endpoints (concept/metric → query
plan → SQL → value), with no LLM in the render path. The dashboard renders these
endpoints and agents call the identical endpoints as tools, guaranteeing the dashboard
and an agent's answer to the same question return byte-identical numbers.

## Considered Options

- **Agent-driven data loading** (agents compose queries freely): rejected because the
  dashboard and agents would diverge on the same question, and results would be
  non-reproducible.

## Consequences

- The resolver is the single source of truth for *values*; the graph is the source of
  truth for *relationships*; the metric layer is the source of truth for *formulas*.
- Agents are responsible for interpretation and narration, not computation.
