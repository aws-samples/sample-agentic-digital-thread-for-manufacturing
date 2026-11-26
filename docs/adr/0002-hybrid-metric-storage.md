<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# Hybrid metric storage: relationships in the graph, formulas in a metric layer

---
Status: accepted
---

Per-system facts that onboarding *discovers* — field→concept mappings, roles, joins,
authority, and concept→column resolution — live in the Neptune graph. Deployment-wide
*definitions* of derived metrics — formula, group-by dimension, time window — live in a
separate, versioned metric layer that references concept ids, not columns.

## Considered Options

- **All in the graph (formulas as first-class nodes):** rejected for v1. It forces
  executable logic (math, group-by, windows) into graph data, still requires an
  evaluator, and makes formulas hard to version, validate, and unit-test. Revisit only
  if live user-editing of formulas becomes a requirement.
- **Provisioned per-system SQL views:** rejected — fast reads but fragile, regenerated
  per system, and buries the lineage we want to expose.

## Consequences

- Direct concepts render with no extra modeling; derived metrics get one shared,
  testable definition.
- Lineage spans two stores (graph + metric layer), so a startup/test check must verify
  that metric-layer concept references resolve to real concepts.
