<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# Lineage captured at resolve-time, at query-provenance depth

---
Status: accepted
---

The Lineage view exposes structural lineage plus the actual resolved SQL ("query
provenance"), with confidence and source (agent-proposed vs user-confirmed) shown on
each relationship. Provenance is **captured at resolve-time** — the resolver stamps each
value with the plan, source ids, and mapping/value-mapping versions it used — rather
than reconstructed live on click.

## Considered Options

- **Reconstructed live traversal:** rejected. Once corrections can change mappings, a
  live-rebuilt chain would show the *current* definition for an *already-rendered*
  number — the lineage could lie.
- **Row-level provenance (show contributing source rows):** deferred, not rejected.
  Capturing the resolved SQL makes row drill-through a cheap future add-on ("re-run this
  stored query on demand") with no need to store rows in v1.

## Consequences

- A displayed number and its lineage are guaranteed consistent, and lineage interacts
  correctly with re-derivation.
- The resolver's contract includes emitting a lineage record per value.
